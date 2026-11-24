"""Copy a locally configured OpenAI key to an explicitly selected AWS secret."""

import argparse
import json
import os

import boto3

from agent import load_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret-arn", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    load_environment()
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise ValueError("Configure OPENAI_API_KEY in your local environment or .env first")
    value = {"OPENAI_API_KEY": key, "OPENAI_MODEL": os.getenv("OPENAI_MODEL", "gpt-6.2-astra")}
    boto3.client("secretsmanager", region_name=args.region).put_secret_value(
        SecretId=args.secret_arn, SecretString=json.dumps(value),
    )
    print("Updated the selected AWS secret. No secret value was printed.")


if __name__ == "__main__":
    main()
