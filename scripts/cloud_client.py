"""Sign calls to an IAM-protected Lambda Function URL with local/CI AWS credentials."""

import argparse
import hashlib
import json
from urllib.parse import urlparse

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import httpx2


def call(url, path, *, region, method="GET", payload=None, session=None):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(f".lambda-url.{region}.on.aws"):
        raise ValueError("Expected an HTTPS Lambda Function URL in the selected AWS region")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Function URL must not include credentials, queries, or fragments")
    session = session or boto3.Session(region_name=region)
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError("Configure AWS credentials before invoking the service")
    body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
    target = url.rstrip("/") + "/" + path.lstrip("/")
    headers = {"Content-Type": "application/json", "X-Amz-Content-Sha256": hashlib.sha256(body).hexdigest()}
    request = AWSRequest(method=method, url=target, data=body, headers=headers)
    SigV4Auth(credentials.get_frozen_credentials(), "lambda", region).add_auth(request)
    response = httpx2.request(method, target, headers=dict(request.headers), content=body, timeout=180)
    response.raise_for_status()
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("path", choices=["health", "dataset", "query", "investigate"])
    parser.add_argument("--region", required=True)
    parser.add_argument("--sql")
    parser.add_argument("--question")
    args = parser.parse_args()
    payload = None
    if args.path == "query":
        if not args.sql:
            parser.error("query requires --sql")
        payload = {"sql": args.sql}
    elif args.path == "investigate":
        if not args.question:
            parser.error("investigate requires --question")
        payload = {"question": args.question}
    print(json.dumps(call(args.url, args.path, region=args.region,
                         method="POST" if payload else "GET", payload=payload), indent=2))


if __name__ == "__main__":
    main()
