# syntax=docker/dockerfile:1.7
# One image for all Python services (api, agent-worker, verifier, llm-stub, indexer); the command differs.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10 HF_HOME=/opt/hf TOKENIZERS_PARALLELISM=false
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
# CPU-only torch keeps the image ~1 GB smaller than the default CUDA build
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.5.1
COPY requirements.txt .
# `wheels` is an optional build context (compose: WHEELHOUSE_DIR). Empty by default -> pip downloads from PyPI.
# On slow networks, pre-download wheels on the host (see README "Slow network") so the build installs from local files.
# The pip cache mount lets a build interrupted by a network timeout resume instead of re-downloading everything.
RUN --mount=type=bind,from=wheels,target=/wheels --mount=type=cache,target=/root/.cache/pip \
    if ls /wheels/*.whl >/dev/null 2>&1; then OFFLINE=--no-index; fi; \
    PIP_NO_CACHE_DIR=0 pip install $OFFLINE --find-links /wheels -r requirements.txt
# Bake the embedding model into the image so containers start without network access to the model hub.
# `hfseed` is an optional build context holding a pre-downloaded Hugging Face hub cache (empty by default).
ARG EMBEDDING_MODEL
RUN --mount=type=bind,from=hfseed,target=/hfseed \
    mkdir -p /opt/hf/hub && cp -r /hfseed/. /opt/hf/hub/ && \
    test -n "$EMBEDDING_MODEL" && python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ['EMBEDDING_MODEL'])"
COPY grievance ./grievance
COPY eval ./eval
COPY tests ./tests
COPY corpus /corpus
RUN useradd -m app && mkdir -p /data && chown -R app /data /opt/hf /app/eval
USER app
