FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BUSCAAI_EMBEDDING_MODEL=intfloat/multilingual-e5-small \
    BUSCAAI_EMBEDDING_BACKEND=sentence_transformers \
    BUSCAAI_EMBEDDING_CACHE_DIR=/opt/buscai/model_cache \
    HF_HOME=/opt/buscai/model_cache \
    BUSCAAI_EMBEDDING_PRELOAD=true \
    BUSCAAI_EMBEDDING_LOCAL_FILES_ONLY=true \
    BUSCAAI_MAX_REQUEST_BODY_BYTES=65536 \
    BUSCAAI_LOG_LEVEL=INFO \
    OMP_NUM_THREADS=4 \
    TORCH_NUM_THREADS=4

WORKDIR /app

RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin appuser

COPY requirements.lock ./
RUN python -m pip install --require-hashes --no-deps -r requirements.lock

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY scripts/preload_embedding_model.py ./scripts/

RUN python -m pip install --no-deps --no-build-isolation . \
    && mkdir -p /opt/buscai/model_cache \
    && BUSCAAI_EMBEDDING_LOCAL_FILES_ONLY=false python scripts/preload_embedding_model.py \
    && chown -R appuser:appuser /app /opt/buscai

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=3); assert r.status == 200"

CMD ["uvicorn", "buscai.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
