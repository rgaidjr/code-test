"""Keep the large CSV out of Git and lock deployments to exact S3 versions."""

import argparse
import base64
import csv
import hashlib
import json
from pathlib import Path
import shutil

import boto3

from philgeps import COLUMNS

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "dataset.lock.json"
DESTINATION = ROOT / ".build" / "DATASETS_PHILGEPS.csv"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate(path, lock):
    if Path(path).stat().st_size != lock["bytes"] or digest(path) != lock["sha256"]:
        raise ValueError("Dataset bytes do not match dataset.lock.json")


def upload(source, bucket, region=None):
    sha = digest(source)
    key = f"datasets/{sha}/DATASETS_PHILGEPS.csv"
    with Path(source).open("rb") as stream:
        result = boto3.client("s3", region_name=region).put_object(
            Bucket=bucket, Key=key, Body=stream, ServerSideEncryption="AES256",
            ContentType="text/csv", ChecksumSHA256=base64.b64encode(bytes.fromhex(sha)).decode(),
            Metadata={"sha256": sha},
        )
    version = result.get("VersionId")
    if not version or version == "null":
        raise ValueError("The dataset bucket must have versioning enabled")
    with Path(source).open(encoding="cp1252", newline="") as stream:
        count = sum(1 for _ in csv.DictReader(stream))
    lock = {"bucket": bucket, "key": key, "version_id": version, "sha256": sha,
            "bytes": Path(source).stat().st_size, "records": count, "encoding": "cp1252"}
    LOCK.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return lock


def download(region=None):
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    if not lock.get("bucket") or not lock.get("version_id"):
        raise ValueError("Upload the dataset first and commit the completed dataset.lock.json")
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    boto3.client("s3", region_name=region).download_file(
        lock["bucket"], lock["key"], str(DESTINATION), ExtraArgs={"VersionId": lock["version_id"]},
    )
    try:
        validate(DESTINATION, lock)
    except ValueError:
        DESTINATION.unlink(missing_ok=True)
        raise
    return lock


def sample():
    """PR builds use one clearly labeled fixture; no AWS access is needed."""
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    row = {header: "NULL" for header, _, _ in COLUMNS}
    row.update({"Procuring Entity (PE)": "CI SMOKE FIXTURE", "Region": "NCR",
                "Bid Reference No.": "CI-1", "Notice Title": "Synthetic CI record",
                "Approved Budget of the Contract": "100.00", "Published Date": "01/07/2026"})
    with DESTINATION.open("w", encoding="cp1252", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[c[0] for c in COLUMNS])
        writer.writeheader()
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["upload", "download", "local", "sample"])
    parser.add_argument("--bucket")
    parser.add_argument("--region")
    parser.add_argument("--source", type=Path, default=ROOT / "DATASETS_PHILGEPS.csv")
    args = parser.parse_args()
    if args.command == "upload":
        if not args.bucket:
            parser.error("upload requires --bucket")
        print(json.dumps(upload(args.source, args.bucket, args.region), indent=2))
    elif args.command == "download":
        print(json.dumps(download(args.region), indent=2))
    elif args.command == "sample":
        sample()
    else:
        validate(args.source, json.loads(LOCK.read_text(encoding="utf-8")))
        DESTINATION.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.source, DESTINATION)


if __name__ == "__main__":
    main()
