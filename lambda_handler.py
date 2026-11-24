"""AWS Lambda adapter. IAM authentication is enforced by the Function URL."""

import os

from mangum import Mangum

os.environ.setdefault("AUTH_MODE", "aws_iam")
os.environ.setdefault("PHILGEPS_CACHE_DIR", "/tmp/philgeps")

from api import app  # noqa: E402

handler = Mangum(app, lifespan="on")
