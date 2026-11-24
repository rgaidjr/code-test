"""HTTP entry point for containers and Lambda; the existing CLI remains available."""

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import secrets
import sys
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agent import format_cli_error, investigate, make_client
from cloud_runtime import load_cloud_secrets
from philgeps import describe_dataset, ensure_database, query_dataset


logger = logging.getLogger("philgeps.api")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    logger.addHandler(logging.StreamHandler(sys.stdout))


@asynccontextmanager
async def lifespan(app):
    # Secrets and data setup fail before readiness succeeds.
    load_cloud_secrets()
    await asyncio.to_thread(ensure_database)
    yield


app = FastAPI(title="PhilGEPS Explorer", version="1.0.0", lifespan=lifespan)


def authorized(request):
    mode = os.getenv("AUTH_MODE", "bearer")
    if mode == "aws_iam":
        # Mangum supplies this trusted event from Lambda. An HTTP header cannot
        # manufacture an IAM identity. The Function URL itself also enforces IAM.
        event = request.scope.get("aws.event", {})
        iam = event.get("requestContext", {}).get("authorizer", {}).get("iam", {})
        return bool(iam.get("userArn"))
    if mode == "bearer":
        token = os.getenv("SERVICE_API_TOKEN", "")
        expected = f"Bearer {token}"
        return bool(token) and secrets.compare_digest(request.headers.get("authorization", "").encode(), expected.encode())
    return False


@app.middleware("http")
async def access_and_logging(request: Request, call_next):
    request_id = str(uuid.uuid4())
    started = time.monotonic()
    # Local container health probes are public; AWS additionally authenticates
    # all Function URL requests before invoking the application.
    if request.url.path != "/health" and not authorized(request):
        response = JSONResponse({"detail": "Authentication required"}, status_code=401)
    else:
        try:
            response = await call_next(request)
        except Exception:
            # Neither SQL, model prompts, headers, nor raw exception bodies are logged.
            response = JSONResponse({"detail": "Internal service error", "request_id": request_id}, status_code=500)
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store"
    logger.info(json.dumps({"request_id": request_id, "method": request.method,
                            "service": os.getenv("AWS_LAMBDA_FUNCTION_NAME", "local"),
                            "path": request.url.path, "status": response.status_code,
                            "elapsed_ms": round((time.monotonic() - started) * 1000)}))
    return response


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1, max_length=12000)


class InvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    max_calls: int = Field(default=6, ge=1, le=8)


@app.get("/health")
def health():
    return {"status": "ok", "revision": os.getenv("APP_REVISION", "local"),
            "dataset_sha256": os.getenv("DATASET_SHA256", "local")}


@app.get("/dataset")
async def dataset():
    return await asyncio.to_thread(describe_dataset)


@app.post("/query")
async def query(body: QueryRequest):
    try:
        return await asyncio.to_thread(query_dataset, body.sql)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from None


@app.post("/investigate")
async def investigation(body: InvestigationRequest):
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(503, detail="Configure the OpenAI API secret to enable investigations")
    try:
        async with asyncio.timeout(120):
            async with make_client() as client:
                answer = await investigate(body.question, client,
                    model=os.getenv("OPENAI_MODEL", "gpt-6.2-astra"), max_calls=body.max_calls)
        return {"answer": answer}
    except TimeoutError:
        raise HTTPException(504, detail="Investigation exceeded the 120-second time budget") from None
    except Exception as exc:
        raise HTTPException(502, detail=format_cli_error(exc)) from None
