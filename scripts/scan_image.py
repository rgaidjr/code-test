"""Fail a release if ECR's basic image scan contains HIGH or CRITICAL findings."""

import argparse
import json

import boto3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    client = boto3.client("ecr", region_name=args.region)
    parameters = {"repositoryName": args.repository, "imageId": {"imageDigest": args.digest}}
    client.get_waiter("image_scan_complete").wait(**parameters, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    result = client.describe_image_scan_findings(**parameters)
    if result.get("imageScanStatus", {}).get("status") != "COMPLETE":
        raise RuntimeError("Image vulnerability scan did not complete")
    counts = result.get("imageScanFindings", {}).get("findingSeverityCounts", {})
    print(json.dumps({"image_digest": args.digest, "vulnerability_counts": counts}))
    if counts.get("HIGH", 0) or counts.get("CRITICAL", 0):
        raise RuntimeError("Release blocked by HIGH/CRITICAL container vulnerabilities; update dependencies/base image")


if __name__ == "__main__":
    main()
