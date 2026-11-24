# Lambda provides the Python runtime and local runtime-interface emulator.
# CI builds a single linux/amd64 image; dataset bytes are verified before build.
FROM public.ecr.aws/lambda/python:3.15@sha256:9ccea91653eed7f28f53c6653ad8398b2096d63b4e8f57b128af1c137d7c2718
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
# Runtime never installs RPMs. Remove package tooling and its vulnerable parser
# libraries after dependency installation; keep the Lambda runtime unchanged.
RUN rpm -e --nodeps microdnf libdnf rpm rpm-libs
COPY agent.py philgeps.py mcp_server.py api.py lambda_handler.py cloud_runtime.py ${LAMBDA_TASK_ROOT}/
COPY .build/DATASETS_PHILGEPS.csv ${LAMBDA_TASK_ROOT}/data/DATASETS_PHILGEPS.csv
CMD ["lambda_handler.handler"]
