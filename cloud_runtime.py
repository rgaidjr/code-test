"""Load cloud-only secrets once per process; never print secret values."""

import json
import os
from functools import lru_cache

import boto3


@lru_cache(maxsize=1)
def load_cloud_secrets():
    arn = os.getenv("APP_SECRET_ARN")
    if not arn:
        return
    try:
        secret = json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"])
        for key in ("OPENAI_API_KEY", "OPENAI_MODEL"):
            value = secret.get(key)
            if value:
                if not isinstance(value, str):
                    raise ValueError("Invalid secret configuration")
                os.environ[key] = value
    except Exception:
        raise RuntimeError("Cloud secret configuration could not be loaded") from None
