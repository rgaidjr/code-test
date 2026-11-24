"""Fail a release if ECR's basic image scan contains HIGH or CRITICAL findings."""

import argparse
import json
import time

import boto3


def scan(client, repository, image_digest, *, attempts=60, delay=10):
    parameters = {"repositoryName": repository, "imageId": {"imageDigest": image_digest}}
    started = False
    for attempt in range(attempts):
        try:
            result = client.describe_image_scan_findings(**parameters)
        except client.exceptions.ScanNotFoundException:
            # Per-repository scanOnPush is deprecated; explicitly start a scan
            # rather than relying on account-wide registry scanning settings.
            if not started:
                client.start_image_scan(**parameters)
                started = True
        else:
            status = result.get("imageScanStatus", {}).get("status")
            if status == "COMPLETE":
                counts = result.get("imageScanFindings", {}).get("findingSeverityCounts", {})
                if counts.get("HIGH", 0) or counts.get("CRITICAL", 0):
                    raise RuntimeError("Release blocked by HIGH/CRITICAL container vulnerabilities; update dependencies/base image")
                return counts
            if status in {"FAILED", "UNSUPPORTED_IMAGE", "SCAN_ELIGIBILITY_EXPIRED", "FINDINGS_UNAVAILABLE", "LIMIT_EXCEEDED"}:
                raise RuntimeError(f"Image scan did not complete successfully: {status}")
        if attempt < attempts - 1:
            time.sleep(delay)
    raise TimeoutError("Image scan did not complete within the release time budget")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    client = boto3.client("ecr", region_name=args.region)
    counts = scan(client, args.repository, args.digest)
    print(json.dumps({"image_digest": args.digest, "vulnerability_counts": counts}))


if __name__ == "__main__":
    main()
