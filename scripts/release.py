"""Deploy an immutable image, test a candidate, then move the live alias."""

import argparse
import json
from pathlib import Path
import re

import boto3
from botocore.exceptions import ClientError
import httpx2

from scripts.cloud_client import call

ROOT = Path(__file__).resolve().parents[1]


def get_alias(client, function, alias):
    try:
        return client.get_alias(FunctionName=function, Name=alias)
    except client.exceptions.ResourceNotFoundException:
        return None


def set_alias(client, function, alias, version):
    existing = get_alias(client, function, alias)
    if existing:
        return client.update_alias(FunctionName=function, Name=alias, FunctionVersion=version,
                                   RevisionId=existing["RevisionId"], RoutingConfig={"AdditionalVersionWeights": {}})
    return client.create_alias(FunctionName=function, Name=alias, FunctionVersion=version)


def url_for(client, function, alias):
    try:
        config = client.get_function_url_config(FunctionName=function, Qualifier=alias)
    except client.exceptions.ResourceNotFoundException:
        config = client.create_function_url_config(FunctionName=function, Qualifier=alias, AuthType="AWS_IAM")
    if config["AuthType"] != "AWS_IAM":
        raise RuntimeError("Refusing to use a Function URL without IAM authentication")
    return config["FunctionUrl"]


def smoke(url, region, revision, dataset_sha, expected_records=None):
    # Test both sides of authentication against the real AWS endpoint.
    if httpx2.get(url.rstrip("/") + "/health", timeout=30).status_code not in (401, 403):
        raise RuntimeError("Unauthenticated cloud request was not rejected")
    health = call(url, "health", region=region)
    if health.get("revision") != revision or health.get("dataset_sha256") != dataset_sha:
        raise RuntimeError("Cloud release identity does not match the expected code and dataset")
    coverage = call(url, "dataset", region=region)["coverage"]
    count = coverage["raw_rows"]
    if expected_records is not None and count != expected_records:
        raise RuntimeError("Cloud dataset count does not match the locked snapshot")
    result = call(url, "query", region=region, method="POST", payload={"sql": "SELECT COUNT(*) AS raw_rows FROM records"})
    if result["rows"] != [[count]] or count < 1:
        raise RuntimeError("Cloud query smoke test failed")


def apply_stack(cfn, stack, parameters):
    arguments = {"StackName": stack, "TemplateBody": (ROOT / "infra" / "application.yaml").read_text(),
                 "Parameters": [{"ParameterKey": key, "ParameterValue": str(value)} for key, value in parameters.items()]}
    try:
        cfn.describe_stacks(StackName=stack)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ValidationError" or "does not exist" not in str(exc):
            raise
        cfn.create_stack(**arguments)
        cfn.get_waiter("stack_create_complete").wait(StackName=stack)
    else:
        try:
            cfn.update_stack(**arguments)
        except ClientError as exc:
            if "No updates are to be performed" not in str(exc):
                raise
        else:
            cfn.get_waiter("stack_update_complete").wait(StackName=stack)


def publish(client, function, revision, image_uri):
    description = f"git:{revision} image:{image_uri.rsplit('@', 1)[1]}"
    latest = client.get_function(FunctionName=function)["Configuration"]
    for page in client.get_paginator("list_versions_by_function").paginate(FunctionName=function):
        for version in page["Versions"]:
            same_config = all(version.get(key) == latest.get(key) for key in
                              ("Environment", "Role", "MemorySize", "Timeout", "Architectures", "EphemeralStorage"))
            if version["Version"] != "$LATEST" and version.get("Description") == description and version["CodeSha256"] == latest["CodeSha256"] and same_config:
                return version["Version"]
    return client.publish_version(FunctionName=function, Description=description,
                                  CodeSha256=latest["CodeSha256"], RevisionId=latest["RevisionId"])["Version"]


def promote(client, function, version, region, revision, dataset_sha, expected_records=None):
    previous = get_alias(client, function, "live")
    set_alias(client, function, "candidate", version)
    candidate_url = url_for(client, function, "candidate")
    smoke(candidate_url, region, revision, dataset_sha, expected_records)
    set_alias(client, function, "live", version)
    try:
        live_url = url_for(client, function, "live")
        smoke(live_url, region, revision, dataset_sha, expected_records)
    except Exception:
        current = get_alias(client, function, "live")
        if current and current["FunctionVersion"] == version:
            if previous:
                set_alias(client, function, "live", previous["FunctionVersion"])
            else:
                client.delete_function_url_config(FunctionName=function, Qualifier="live")
                client.delete_alias(FunctionName=function, Name="live")
        raise
    return {"function": function, "url": live_url, "version": version,
            "previous_version": previous["FunctionVersion"] if previous else None,
            "revision": revision, "dataset_sha256": dataset_sha}


def deploy(args):
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,30}", args.project):
        raise ValueError("Invalid project name")
    if not re.fullmatch(r".+@sha256:[a-f0-9]{64}", args.image_uri):
        raise ValueError("Deploy by immutable image digest, never a mutable tag")
    function = f"{args.project}-{args.environment}"
    parameters = {"FunctionName": function, "ImageUri": args.image_uri,
        "ExecutionRoleArn": args.execution_role_arn, "SecretArn": args.secret_arn,
        "DatasetSha256": args.dataset_sha256, "Revision": args.revision}
    session = boto3.Session(region_name=args.region)
    apply_stack(session.client("cloudformation"), function, parameters)
    client = session.client("lambda")
    client.get_waiter("function_updated_v2").wait(FunctionName=function)
    version = publish(client, function, args.revision, args.image_uri)
    result = promote(client, function, version, args.region, args.revision, args.dataset_sha256, args.records)
    result["image_uri"] = args.image_uri
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    deployment = sub.add_parser("deploy")
    deployment.add_argument("--environment", choices=["staging", "production"], required=True)
    deployment.add_argument("--project", default="philgeps")
    for name in ("image-uri", "execution-role-arn", "secret-arn", "dataset-sha256", "revision", "region"):
        deployment.add_argument("--" + name, required=True)
    deployment.add_argument("--records", required=True, type=int)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--function", required=True)
    rollback.add_argument("--version", required=True)
    rollback.add_argument("--region", required=True)
    args = parser.parse_args()
    if args.command == "deploy":
        result = deploy(args)
    else:
        if not re.fullmatch(r"[1-9][0-9]*", args.version):
            raise ValueError("Rollback requires a published numeric function version")
        client = boto3.client("lambda", region_name=args.region)
        config = client.get_function(FunctionName=args.function, Qualifier=args.version)["Configuration"]
        env = config["Environment"]["Variables"]
        result = promote(client, args.function, args.version, args.region, env["APP_REVISION"], env["DATASET_SHA256"])
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
