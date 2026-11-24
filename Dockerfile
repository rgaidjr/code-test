# Lambda provides the Python runtime and local runtime-interface emulator.
# CI builds a single linux/amd64 image; dataset bytes are verified before build.
FROM public.ecr.aws/lambda/python:3.12@sha256:c1f818c2235d002529c955a6c0889a3d5c6de020205e37dff65039d73a453d78
ARG APP_REVISION=local
ARG DATASET_SHA256=local
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_REVISION=${APP_REVISION} \
    DATASET_SHA256=${DATASET_SHA256} \
    PHILGEPS_CSV=/var/task/data/DATASETS_PHILGEPS.csv \
    PHILGEPS_CACHE_DIR=/tmp/philgeps \
    AUTH_MODE=aws_iam
COPY requirements.lock ${LAMBDA_TASK_ROOT}/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY agent.py philgeps.py mcp_server.py api.py lambda_handler.py cloud_runtime.py ${LAMBDA_TASK_ROOT}/
COPY .build/DATASETS_PHILGEPS.csv ${LAMBDA_TASK_ROOT}/data/DATASETS_PHILGEPS.csv
CMD ["lambda_handler.handler"]
