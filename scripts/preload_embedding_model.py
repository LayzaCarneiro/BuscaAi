"""Pré-carrega e verifica em memória o modelo de embeddings configurado sem ler segredos."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np

from buscai.semantic import RECOMMENDED_MODEL_NAME, SentenceTransformerEmbeddingProvider


def _parse_bool_flag(raw: str | None, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    norm = raw.strip().casefold()
    if norm in {"1", "true", "yes", "on"}:
        return True
    if norm in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Valor booleano inválido para BUSCAAI_EMBEDDING_LOCAL_FILES_ONLY: {raw!r}.")


def main() -> int:
    model_name = os.getenv("BUSCAAI_EMBEDDING_MODEL", RECOMMENDED_MODEL_NAME).strip() or RECOMMENDED_MODEL_NAME
    cache_dir_raw = os.getenv("BUSCAAI_EMBEDDING_CACHE_DIR", "").strip()
    cache_dir = str(Path(cache_dir_raw)) if cache_dir_raw else None
    local_only = _parse_bool_flag(os.getenv("BUSCAAI_EMBEDDING_LOCAL_FILES_ONLY"), default=False)

    started = time.perf_counter()
    provider = SentenceTransformerEmbeddingProvider(
        model_name=model_name,
        device="cpu",
        cache_folder=cache_dir,
        local_files_only=local_only,
    )
    provider.ensure_loaded()
    vec = provider.embed_queries(["notebook 16GB RAM warmup"])
    elapsed_s = round(time.perf_counter() - started, 3)

    if vec.ndim != 2 or vec.shape[0] != 1 or not np.all(np.isfinite(vec)):
        raise RuntimeError("Vetor de warmup inválido gerado durante o pré-carregamento do modelo.")

    info = provider.info()
    provider.unload()

    summary = {
        "event": "embedding_preload_verified",
        "model_name": info.model_name,
        "backend": info.backend,
        "dimension": info.dimension,
        "local_files_only": local_only,
        "cache_dir_configured": cache_dir is not None,
        "elapsed_seconds": elapsed_s,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
