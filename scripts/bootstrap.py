"""Plan and create the foundation using a named AWS profile and a change set."""

import argparse
import json
from pathlib import Path
import time

import boto3

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--oidc-subject-prefix", required=True,
                        help="Exact sub_claim_prefix from the GitHub repository OIDC configuration API")
    parser.add_argument("--project", default="philgeps")
    parser.add_argument("--execute", action="store_true", help="Execute the validated change set")
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    region = session.region_name
    identity = session.client("sts").get_caller_identity()
    print(json.dumps({"account": identity["Account"], "region": region, "repository": args.repository}), flush=True)
    iam = session.client("iam")
    provider = ""
    for item in iam.list_open_id_connect_providers().get("OpenIDConnectProviderList", []):
        details = iam.get_open_id_connect_provider(OpenIDConnectProviderArn=item["Arn"])
        if details["Url"].removeprefix("https://") == "token.actions.githubusercontent.com":
            provider = item["Arn"]
            break
    cfn = session.client("cloudformation")
    stack = f"{args.project}-foundation"
    existing = next((item for page in cfn.get_paginator("list_stacks").paginate()
                     for item in page.get("StackSummaries", [])
                     if item["StackName"] == stack and item["StackStatus"] != "DELETE_COMPLETE"), None)
    # Never reclassify this stack's own provider as an external provider on an
    # update: changing the condition would delete the provider the roles trust.
    if existing and existing["StackStatus"] != "REVIEW_IN_PROGRESS":
        if stack_owns_oidc_provider(cfn, stack):
            provider = ""
    operation = "UPDATE" if existing and existing["StackStatus"] != "REVIEW_IN_PROGRESS" else "CREATE"
    response = cfn.create_change_set(
        StackName=stack, ChangeSetName=f"bootstrap-{int(time.time())}", ChangeSetType=operation,
        TemplateBody=(ROOT / "infra/foundation.yaml").read_text(encoding="utf-8"),
        Parameters=[{"ParameterKey": key, "ParameterValue": value} for key, value in {
            "ProjectName": args.project, "GitHubRepository": args.repository,
            "GitHubSubjectPrefix": args.oidc_subject_prefix,
            "ExistingOidcProviderArn": provider}.items()],
        Capabilities=["CAPABILITY_IAM"],
        Description="PhilGEPS deployment foundation, validated before execution",
    )
    arn = response["Id"]
    print("Preparing change set", arn, flush=True)
    cfn.get_waiter("change_set_create_complete").wait(ChangeSetName=arn, WaiterConfig={"Delay": 5, "MaxAttempts": 60})
    changes = cfn.describe_change_set(ChangeSetName=arn)
    print(json.dumps({"changes": [{"action": item["ResourceChange"]["Action"],
        "resource": item["ResourceChange"]["LogicalResourceId"], "type": item["ResourceChange"]["ResourceType"]}
        for item in changes.get("Changes", [])]}, indent=2), flush=True)
    validations = []
    paginator = cfn.get_paginator("describe_events")
    for page in paginator.paginate(ChangeSetName=arn):
        validations.extend(event for event in page.get("OperationEvents", []) if event.get("EventType") == "VALIDATION_ERROR")
    print("Pre-deployment validation events:", json.dumps(validations), flush=True)
    if any(event.get("ValidationStatus") == "FAILED" and event.get("ValidationFailureMode") == "FAIL" for event in validations):
        raise RuntimeError("Blocking pre-deployment validation findings")
    if not args.execute:
        print("Change set validated; no infrastructure resources provisioned. Use --execute for deployment.")
        return
    cfn.execute_change_set(ChangeSetName=arn)
    print("Foundation deployment started", flush=True)
    waiter = "stack_update_complete" if operation == "UPDATE" else "stack_create_complete"
    cfn.get_waiter(waiter).wait(StackName=stack, WaiterConfig={"Delay": 5, "MaxAttempts": 180})
    outputs = {item["OutputKey"]: item["OutputValue"] for item in cfn.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]}
    destination = ROOT / ".build/foundation-outputs.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(outputs, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(outputs, indent=2))


def stack_owns_oidc_provider(cfn, stack):
    return any(item["LogicalResourceId"] == "GitHubOidcProvider" and item["ResourceStatus"] != "DELETE_COMPLETE"
               for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack)
               for item in page.get("StackResourceSummaries", []))


if __name__ == "__main__":
    main()
