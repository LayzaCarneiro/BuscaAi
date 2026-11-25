"""Benchmark sintético reproduzível de embeddings multilíngues e deduplicação híbrida.

Execução offline padrão com `--backend stub` (sem download de pesos); suporta também
`--backend sentence_transformers` ou `--backend fastembed` para comparação real em CPU.
Imprime apenas métricas numéricas agregadas (nunca títulos ou dados individuais de anúncios).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import tracemalloc
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from buscai.search.models import Candidate
from buscai.semantic import (
    ALTERNATIVE_MODEL_NAME,
    RECOMMENDED_MODEL_NAME,
    DeterministicStubEmbeddingProvider,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingService,
    FastEmbedEmbeddingProvider,
    InMemorySemanticRanker,
    ProductDeduplicator,
    SentenceTransformerEmbeddingProvider,
    cosine_similarity_matrix,
)

DEFAULT_DATASET = ROOT / "data" / "eval_semantic_synthetic.jsonl"


def _candidate_from_synthetic(raw: dict[str, object], *, default_price: str = "500.00") -> Candidate:
    return Candidate(
        item_id=str(raw["item_id"]),
        source="synthetic",
        title=str(raw["title"]),
        price=Decimal(default_price),
        currency="USD",
        condition=str(raw["condition"]) if raw.get("condition") else None,
        brand=str(raw["brand"]) if raw.get("brand") else None,
        model=str(raw["model"]) if raw.get("model") else None,
        short_description=str(raw["short_description"]) if raw.get("short_description") else None,
    )


def _dcg_at_k(relevances: Sequence[int], k: int) -> float:
    return sum((rel / math.log2(idx + 2)) for idx, rel in enumerate(relevances[:k]))


def _build_provider(backend: str, model_name: str) -> EmbeddingProvider:
    if backend == "stub":
        return DeterministicStubEmbeddingProvider(model_name=model_name)
    if backend == "fastembed":
        return FastEmbedEmbeddingProvider(model_name=model_name)
    if backend == "sentence_transformers":
        return SentenceTransformerEmbeddingProvider(model_name=model_name)
    raise ValueError(f"Backend desconhecido: {backend!r}.")


def evaluate_model(
    dataset_rows: Sequence[dict[str, object]],
    *,
    backend: str,
    model_name: str,
    k: int = 3,
) -> dict[str, object]:
    provider = _build_provider(backend, model_name)
    service = EmbeddingService(provider, batch_size=32)
    ranker = InMemorySemanticRanker(service)
    dedup = ProductDeduplicator(service)

    retrieval_rows = [r for r in dataset_rows if r.get("task") == "retrieval"]
    dedup_rows = [r for r in dataset_rows if r.get("task") == "deduplication"]

    tracemalloc.start()
    started = time.monotonic()

    precision_at_2: list[float] = []
    precision_scores: list[float] = []
    ndcg_scores: list[float] = []
    reciprocal_ranks: list[float] = []

    for row in retrieval_rows:
        query_pt = str(row["query_pt"])
        raw_cands = row["candidates"]
        assert isinstance(raw_cands, list)
        rel_by_id = {str(item["item_id"]): int(item["relevance"]) for item in raw_cands}
        candidates = [_candidate_from_synthetic(item) for item in raw_cands]

        ranked = ranker.rank(query_pt, candidates)
        ranked_rels = [rel_by_id[entry.candidate.item_id] for entry in ranked]

        top_2 = ranked_rels[:2]
        precision_at_2.append(sum(1 for rel in top_2 if rel >= 2) / min(2, len(ranked_rels)))

        top_k = ranked_rels[:k]
        precision_scores.append(sum(1 for rel in top_k if rel >= 2) / min(k, len(ranked_rels)))

        ideal = sorted(ranked_rels, reverse=True)
        idcg = _dcg_at_k(ideal, k)
        ndcg_scores.append((_dcg_at_k(ranked_rels, k) / idcg) if idcg > 0 else 0.0)

        rr = 0.0
        for idx, rel in enumerate(ranked_rels):
            if rel >= 2:
                rr = 1.0 / (idx + 1)
                break
        reciprocal_ranks.append(rr)

    dedup_correct = 0
    false_merges = 0
    non_dup_total = 0

    for row in dedup_rows:
        expected = str(row["expected_label"])
        left = _candidate_from_synthetic(row["left"])  # type: ignore[arg-type]
        right = _candidate_from_synthetic(row["right"])  # type: ignore[arg-type]
        vecs = service.embed_candidates([left, right])
        sim = float(cosine_similarity_matrix(vecs[0:1], vecs[1:2])[0, 0])
        del vecs

        ev = dedup.classify_pair(left, right, semantic_similarity=sim)
        if ev.classification == expected:
            dedup_correct += 1
        if expected != "probable_duplicate":
            non_dup_total += 1
            if ev.classification == "probable_duplicate":
                false_merges += 1

    elapsed_ms = round((time.monotonic() - started) * 1000.0, 2)
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    info = service.model_info()
    return {
        "model": info.model_name,
        "backend": info.backend,
        "dimension": info.dimension,
        "dtype": info.dtype,
        "retrieval_cases": len(retrieval_rows),
        "precision_at_2": round(sum(precision_at_2) / len(precision_at_2), 4) if precision_at_2 else 0.0,
        f"precision_at_{k}": round(sum(precision_scores) / len(precision_scores), 4) if precision_scores else 0.0,
        f"ndcg_at_{k}": round(sum(ndcg_scores) / len(ndcg_scores), 4) if ndcg_scores else 0.0,
        "mrr": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4) if reciprocal_ranks else 0.0,
        "dedup_cases": len(dedup_rows),
        "dedup_accuracy": round(dedup_correct / len(dedup_rows), 4) if dedup_rows else 0.0,
        "false_dedup_rate": round(false_merges / non_dup_total, 4) if non_dup_total else 0.0,
        "latency_ms": elapsed_ms,
        "peak_traced_kb": round(peak_bytes / 1024.0, 2),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compara modelos/backends de embeddings multilíngues e deduplicação sobre dados sintéticos."
    )
    parser.add_argument(
        "--backend",
        choices=("stub", "sentence_transformers", "fastembed"),
        default="stub",
        help="Provedor a avaliar (padrão: stub offline sem download).",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=[RECOMMENDED_MODEL_NAME, ALTERNATIVE_MODEL_NAME],
        help="Lista de modelos para comparação.",
    )
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="Caminho para o dataset sintético JSONL.")
    args = parser.parse_args(argv)

    dataset_path = Path(args.dataset)
    rows = [json.loads(line) for line in dataset_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    for model_name in args.models:
        try:
            summary = evaluate_model(rows, backend=args.backend, model_name=model_name)
        except EmbeddingError as exc:
            print(json.dumps({"model": model_name, "backend": args.backend, "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
