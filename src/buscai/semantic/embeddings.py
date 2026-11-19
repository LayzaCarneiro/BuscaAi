"""Serviço modular e transitório de embeddings multilíngues em memória (sem persistência de anúncios)."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from buscai.config import Settings
from buscai.observability import log_operational_event, sanitize_text
from buscai.search.models import Candidate
from buscai.semantic.representation import build_candidate_embedding_text, build_query_embedding_text

RECOMMENDED_MODEL_NAME = "intfloat/multilingual-e5-small"
ALTERNATIVE_MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class EmbeddingError(RuntimeError):
    """Erro base de geração de embeddings semânticos."""


class EmbeddingModelLoadError(EmbeddingError):
    """Falha ao carregar o modelo ou runtime de embeddings em memória."""


class EmbeddingInferenceError(EmbeddingError):
    """Falha durante a inferência ou validação numérica dos vetores gerados."""


class EmbeddingModelInfo(BaseModel):
    """Metadados técnicos e contratuais de um modelo de embeddings."""

    model_config = ConfigDict(frozen=True)

    model_name: str
    backend: Literal["sentence_transformers", "fastembed", "stub"]
    dimension: int = Field(ge=1)
    dtype: str = "float32"
    max_tokens: int = Field(ge=1)
    requires_prefixes: bool = False
    query_prefix: str = ""
    document_prefix: str = ""
    license_name: str
    supports_pt_en: bool = True


SUPPORTED_MODEL_SPECS: dict[str, EmbeddingModelInfo] = {
    RECOMMENDED_MODEL_NAME: EmbeddingModelInfo(
        model_name=RECOMMENDED_MODEL_NAME,
        backend="sentence_transformers",
        dimension=384,
        dtype="float32",
        max_tokens=512,
        requires_prefixes=True,
        query_prefix="query: ",
        document_prefix="passage: ",
        license_name="MIT",
        supports_pt_en=True,
    ),
    ALTERNATIVE_MODEL_NAME: EmbeddingModelInfo(
        model_name=ALTERNATIVE_MODEL_NAME,
        backend="sentence_transformers",
        dimension=384,
        dtype="float32",
        max_tokens=256,
        requires_prefixes=False,
        query_prefix="",
        document_prefix="",
        license_name="Apache-2.0",
        supports_pt_en=True,
    ),
}


def resolve_model_spec(
    model_name: str,
    *,
    backend: Literal["sentence_transformers", "fastembed", "stub"],
    dimension: int | None = None,
) -> EmbeddingModelInfo:
    """Resolve metadados conhecidos do modelo, inferindo prefixos E5 quando aplicável."""
    cleaned = model_name.strip()
    if not cleaned:
        raise ValueError("model_name não pode ser vazio.")

    known = SUPPORTED_MODEL_SPECS.get(cleaned)
    if known is not None:
        updates: dict[str, object] = {"backend": backend}
        if dimension is not None:
            updates["dimension"] = dimension
        return known.model_copy(update=updates)

    resolved_dim = 384 if dimension is None else dimension
    is_e5 = "e5" in cleaned.casefold()
    return EmbeddingModelInfo(
        model_name=cleaned,
        backend=backend,
        dimension=resolved_dim,
        dtype="float32",
        max_tokens=512 if is_e5 else 256,
        requires_prefixes=is_e5,
        query_prefix="query: " if is_e5 else "",
        document_prefix="passage: " if is_e5 else "",
        license_name="external",
        supports_pt_en=True,
    )


class EmbeddingProvider(Protocol):
    """Protocolo desacoplado de provedores de embeddings em memória."""

    def info(self) -> EmbeddingModelInfo:
        """Retorna metadados de dimensão, tipo e configuração do modelo."""
        ...

    def is_loaded(self) -> bool:
        """Indica se os pesos do modelo estão carregados em memória."""
        ...

    def embed_queries(self, texts: Sequence[str], *, batch_size: int = 32) -> np.ndarray:
        """Gera matriz 2D `float32` normalizada (N x D) para consultas."""
        ...

    def embed_documents(self, texts: Sequence[str], *, batch_size: int = 32) -> np.ndarray:
        """Gera matriz 2D `float32` normalizada (N x D) para textos de produtos."""
        ...


def l2_normalize(vectors: np.ndarray) -> np.ndarray:
    """Normaliza cada vetor linha pela norma L2 em `np.float32`, validando finitude."""
    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    if arr.ndim != 2 or arr.shape[0] == 0 or arr.shape[1] == 0:
        raise EmbeddingInferenceError("Matriz de embeddings inválida: esperado formato 2D não vazio.")
    if not np.all(np.isfinite(arr)):
        raise EmbeddingInferenceError("Matriz de embeddings contém valores NaN ou infinitos.")

    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    if np.any(norms <= 0):
        safe_norms = np.where(norms > 0, norms, 1.0).astype(np.float32)
        return (arr / safe_norms).astype(np.float32)
    return (arr / norms).astype(np.float32)


def cosine_similarity_matrix(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """Computa matriz de similaridade de cosseno em `[-1.0, 1.0]` entre vetores L2-normalizados."""
    a = l2_normalize(left)
    b = l2_normalize(right)
    if a.shape[1] != b.shape[1]:
        raise EmbeddingInferenceError(
            f"Dimensões incompatíveis para similaridade de cosseno: {a.shape[1]} != {b.shape[1]}."
        )
    sim = np.matmul(a, b.T).astype(np.float32)
    return np.clip(sim, -1.0, 1.0)


def clamp_unit_scores(scores: np.ndarray) -> np.ndarray:
    """Limita similaridades de cosseno ao intervalo normalizado `[0.0, 1.0]`."""
    arr = np.asarray(scores, dtype=np.float32)
    if not np.all(np.isfinite(arr)):
        raise EmbeddingInferenceError("Scores de similaridade contêm valores inválidos.")
    return np.clip(arr, 0.0, 1.0)


def _validate_text_batch(texts: Sequence[str], *, role: str) -> list[str]:
    if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
        raise ValueError(f"Lista de textos ({role}) deve ser uma sequência não vazia.")
    if len(texts) == 0:
        raise ValueError(f"Lista de textos ({role}) não pode ser vazia.")
    cleaned: list[str] = []
    for item in texts:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Todos os itens de {role} devem ser strings não vazias.")
        cleaned.append(" ".join(item.strip().split()))
    return cleaned


def _validate_batch_size(batch_size: int) -> int:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size deve ser um inteiro positivo (>= 1).")
    return batch_size


class SentenceTransformerEmbeddingProvider:
    """Provedor local via `sentence-transformers` com lazy loading e reuso seguro de pesos."""

    def __init__(
        self,
        model_name: str = RECOMMENDED_MODEL_NAME,
        *,
        device: str = "cpu",
        model_loader: Callable[[str, str], Any] | None = None,
        dimension: int | None = None,
    ) -> None:
        self._info = resolve_model_spec(model_name, backend="sentence_transformers", dimension=dimension)
        self._device = device.strip() or "cpu"
        self._model_loader = model_loader
        self._model: Any = None
        self._load_lock = threading.Lock()

    def info(self) -> EmbeddingModelInfo:
        return self._info

    def is_loaded(self) -> bool:
        return self._model is not None

    def ensure_loaded(self) -> Any:
        """Carrega os pesos em memória uma única vez de forma thread-safe."""
        with self._load_lock:
            if self._model is not None:
                return self._model
            try:
                if self._model_loader is not None:
                    loaded = self._model_loader(self._info.model_name, self._device)
                else:
                    from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

                    loaded = SentenceTransformer(self._info.model_name, device=self._device)
            except ImportError as exc:
                raise EmbeddingModelLoadError(
                    "Dependência opcional 'sentence-transformers' não instalada neste ambiente."
                ) from exc
            except Exception as exc:
                safe_msg = sanitize_text(str(exc)) or type(exc).__name__
                raise EmbeddingModelLoadError(
                    f"Falha ao carregar modelo SentenceTransformer '{self._info.model_name}': {safe_msg}"
                ) from exc

            get_dim = getattr(loaded, "get_sentence_embedding_dimension", None)
            if callable(get_dim):
                detected = get_dim()
                if isinstance(detected, int) and detected > 0 and detected != self._info.dimension:
                    self._info = self._info.model_copy(update={"dimension": detected})

            self._model = loaded
            return self._model

    def unload(self) -> None:
        """Libera a referência em memória aos pesos do modelo."""
        with self._load_lock:
            self._model = None

    def embed_queries(self, texts: Sequence[str], *, batch_size: int = 32) -> np.ndarray:
        return self._encode(texts, prefix=self._info.query_prefix, role="queries", batch_size=batch_size)

    def embed_documents(self, texts: Sequence[str], *, batch_size: int = 32) -> np.ndarray:
        return self._encode(texts, prefix=self._info.document_prefix, role="documents", batch_size=batch_size)

    def _encode(
        self,
        texts: Sequence[str],
        *,
        prefix: str,
        role: str,
        batch_size: int,
    ) -> np.ndarray:
        cleaned = _validate_text_batch(texts, role=role)
        size = _validate_batch_size(batch_size)
        model = self.ensure_loaded()
        prepared = [f"{prefix}{text}" if prefix else text for text in cleaned]

        try:
            raw = model.encode(
                prepared,
                batch_size=size,
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            vectors = l2_normalize(np.asarray(raw, dtype=np.float32))
        except EmbeddingInferenceError:
            raise
        except Exception as exc:
            safe_msg = sanitize_text(str(exc)) or type(exc).__name__
            raise EmbeddingInferenceError(f"Falha de inferência no SentenceTransformer: {safe_msg}") from exc

        if vectors.shape[0] != len(cleaned) or vectors.shape[1] != self._info.dimension:
            raise EmbeddingInferenceError(
                f"Formato inesperado de saída: {vectors.shape}, esperado ({len(cleaned)}, {self._info.dimension})."
            )
        return vectors


class FastEmbedEmbeddingProvider:
    """Alternativa leve local via FastEmbed (ONNX Runtime) sem dependência de PyTorch."""

    def __init__(
        self,
        model_name: str = RECOMMENDED_MODEL_NAME,
        *,
        model_loader: Callable[[str], Any] | None = None,
        dimension: int | None = None,
    ) -> None:
        self._info = resolve_model_spec(model_name, backend="fastembed", dimension=dimension)
        self._model_loader = model_loader
        self._model: Any = None
        self._load_lock = threading.Lock()

    def info(self) -> EmbeddingModelInfo:
        return self._info

    def is_loaded(self) -> bool:
        return self._model is not None

    def ensure_loaded(self) -> Any:
        with self._load_lock:
            if self._model is not None:
                return self._model
            try:
                if self._model_loader is not None:
                    loaded = self._model_loader(self._info.model_name)
                else:
                    from fastembed import TextEmbedding  # type: ignore[import-not-found]

                    loaded = TextEmbedding(model_name=self._info.model_name)
            except ImportError as exc:
                raise EmbeddingModelLoadError(
                    "Dependência opcional 'fastembed' não instalada neste ambiente."
                ) from exc
            except Exception as exc:
                safe_msg = sanitize_text(str(exc)) or type(exc).__name__
                raise EmbeddingModelLoadError(
                    f"Falha ao carregar modelo FastEmbed '{self._info.model_name}': {safe_msg}"
                ) from exc
            self._model = loaded
            return self._model

    def unload(self) -> None:
        with self._load_lock:
            self._model = None

    def embed_queries(self, texts: Sequence[str], *, batch_size: int = 32) -> np.ndarray:
        return self._encode(texts, prefix=self._info.query_prefix, role="queries", batch_size=batch_size)

    def embed_documents(self, texts: Sequence[str], *, batch_size: int = 32) -> np.ndarray:
        return self._encode(texts, prefix=self._info.document_prefix, role="documents", batch_size=batch_size)

    def _encode(
        self,
        texts: Sequence[str],
        *,
        prefix: str,
        role: str,
        batch_size: int,
    ) -> np.ndarray:
        cleaned = _validate_text_batch(texts, role=role)
        size = _validate_batch_size(batch_size)
        model = self.ensure_loaded()
        prepared = [f"{prefix}{text}" if prefix else text for text in cleaned]

        try:
            chunks: list[np.ndarray] = []
            for start in range(0, len(prepared), size):
                batch = prepared[start : start + size]
                embedded = list(model.embed(batch))
                if embedded:
                    chunks.append(np.asarray(embedded, dtype=np.float32))
            if not chunks:
                raise EmbeddingInferenceError("FastEmbed não retornou vetores.")
            vectors = l2_normalize(np.vstack(chunks))
        except EmbeddingInferenceError:
            raise
        except Exception as exc:
            safe_msg = sanitize_text(str(exc)) or type(exc).__name__
            raise EmbeddingInferenceError(f"Falha de inferência no FastEmbed: {safe_msg}") from exc

        if vectors.shape[0] != len(cleaned) or vectors.shape[1] != self._info.dimension:
            raise EmbeddingInferenceError(
                f"Formato inesperado de saída: {vectors.shape}, esperado ({len(cleaned)}, {self._info.dimension})."
            )
        return vectors


# Léxico bilíngue estático apenas para projeção semântica determinística do provedor stub offline.
_BILINGUAL_CONCEPT_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("concept:laptop", ("notebook", "notebooks", "laptop", "laptops", "ultrabook", "macbook", "chromebook")),
    ("concept:smartphone", ("smartphone", "smartphones", "celular", "celulares", "iphone", "galaxy", "pixel")),
    ("concept:monitor", ("monitor", "monitores", "display", "screen", "tela", "ultrawide")),
    ("concept:headphones", ("fone", "fones", "headphone", "headphones", "headset", "earbuds", "earphones", "airpods")),
    ("concept:4k", ("4k", "uhd", "2160p", "3840x2160")),
    ("concept:fhd", ("1080p", "fhd", "fullhd")),
    ("concept:qhd", ("1440p", "qhd", "wqhd")),
    ("concept:anc", ("anc", "noise", "cancelling", "canceling", "ruido")),
    ("concept:wireless", ("wireless", "bluetooth", "sem fio", "tws")),
    ("concept:work", ("trabalho", "work", "office", "produtividade", "design")),
    ("concept:study", ("estudo", "estudos", "faculdade", "study", "student")),
    ("concept:coding", ("programacao", "coding", "developer", "programming")),
    ("concept:gaming", ("gamer", "gaming", "jogos", "144hz", "165hz", "freesync")),
    ("concept:ram16", ("16gb", "16 gb")),
    ("concept:ssd512", ("512gb", "512 gb")),
    ("concept:storage256", ("256gb", "256 gb")),
    ("concept:storage128", ("128gb", "128 gb")),
    ("concept:usbc", ("usb-c", "usbc", "type-c", "thunderbolt")),
    ("concept:5g", ("5g",)),
    ("concept:nfc", ("nfc",)),
    ("concept:ips", ("ips",)),
    ("concept:oled", ("oled", "amoled")),
)


class DeterministicStubEmbeddingProvider:
    """Provedor determinístico offline para testes e benchmarks sem download de modelos.

    Não retém textos nem vetores entre chamadas; projeta conceitos bilíngues PT-BR<->EN-US
    e n-gramas normalizados por hash criptográfico estável (`blake2b`) em `np.float32`.
    """

    def __init__(
        self,
        *,
        model_name: str = RECOMMENDED_MODEL_NAME,
        dimension: int = 384,
        model_loader: Callable[[], object] | None = None,
    ) -> None:
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension < 16:
            raise ValueError("dimension do stub deve ser um inteiro >= 16.")
        self._info = EmbeddingModelInfo(
            model_name=model_name,
            backend="stub",
            dimension=dimension,
            dtype="float32",
            max_tokens=512,
            requires_prefixes=False,
            query_prefix="",
            document_prefix="",
            license_name="internal-test-stub",
            supports_pt_en=True,
        )
        self._model_loader = model_loader
        self._loaded = False

    def info(self) -> EmbeddingModelInfo:
        return self._info

    def is_loaded(self) -> bool:
        return self._loaded

    def _ensure_ready(self) -> None:
        if self._loaded:
            return
        if self._model_loader is not None:
            try:
                self._model_loader()
            except Exception as exc:
                safe_msg = sanitize_text(str(exc)) or type(exc).__name__
                raise EmbeddingModelLoadError(f"Falha ao inicializar stub de embeddings: {safe_msg}") from exc
        self._loaded = True

    def embed_queries(self, texts: Sequence[str], *, batch_size: int = 32) -> np.ndarray:
        return self._encode_batch(texts, role="queries", batch_size=batch_size)

    def embed_documents(self, texts: Sequence[str], *, batch_size: int = 32) -> np.ndarray:
        return self._encode_batch(texts, role="documents", batch_size=batch_size)

    def _encode_batch(self, texts: Sequence[str], *, role: str, batch_size: int) -> np.ndarray:
        cleaned = _validate_text_batch(texts, role=role)
        _validate_batch_size(batch_size)
        self._ensure_ready()

        dim = self._info.dimension
        matrix = np.zeros((len(cleaned), dim), dtype=np.float32)
        for row_idx, text in enumerate(cleaned):
            matrix[row_idx] = self._project_text(text, dim)
        return l2_normalize(matrix)

    @staticmethod
    def _project_text(text: str, dim: int) -> np.ndarray:
        vec = np.zeros(dim, dtype=np.float32)
        decomposed = unicodedata.normalize("NFKD", text.casefold())
        norm = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
        norm = " ".join(norm.split())

        for concept_key, triggers in _BILINGUAL_CONCEPT_ALIASES:
            if any(re.search(rf"(?<!\w){re.escape(tr)}(?!\w)", norm) for tr in triggers):
                DeterministicStubEmbeddingProvider._accumulate_hash(vec, concept_key, weight=2.2)

        tokens = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", norm)
        for raw_tok in tokens:
            joined_tok = raw_tok.replace("-", "")
            DeterministicStubEmbeddingProvider._accumulate_hash(vec, f"tok:{joined_tok}", weight=1.2)
            padded = f"<{joined_tok}>"
            for i in range(max(1, len(padded) - 2)):
                trigram = padded[i : i + 3]
                DeterministicStubEmbeddingProvider._accumulate_hash(vec, f"ng:{trigram}", weight=0.35)

        if not np.any(vec):
            vec[0] = 1.0
        return vec

    @staticmethod
    def _accumulate_hash(vec: np.ndarray, key: str, *, weight: float) -> None:
        digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
        raw_int = int.from_bytes(digest, byteorder="big", signed=False)
        idx = raw_int % vec.shape[0]
        sign = 1.0 if ((raw_int >> 63) & 1) == 0 else -1.0
        vec[idx] += np.float32(sign * weight)


class EmbeddingService:
    """Fachada desacoplada de geração em lote de embeddings transitórios em memória."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        batch_size: int = 32,
        logger: logging.Logger | None = None,
    ) -> None:
        self._provider = provider
        self._batch_size = _validate_batch_size(batch_size)
        self._logger = logger

    def model_info(self) -> EmbeddingModelInfo:
        """Expõe metadados do modelo sem reter dados de requisições."""
        return self._provider.info()

    def is_ready(self) -> bool:
        """Informa se o provedor subjacente já carregou o modelo em memória."""
        return self._provider.is_loaded()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        provider: EmbeddingProvider | None = None,
        logger: logging.Logger | None = None,
    ) -> EmbeddingService:
        """Instancia `EmbeddingService` respeitando o backend e modelo definidos em Settings."""
        if provider is not None:
            return cls(provider=provider, batch_size=settings.embedding_batch_size, logger=logger)

        if settings.embedding_backend == "fastembed":
            chosen: EmbeddingProvider = FastEmbedEmbeddingProvider(model_name=settings.embedding_model_name)
        else:
            chosen = SentenceTransformerEmbeddingProvider(model_name=settings.embedding_model_name)
        return cls(provider=chosen, batch_size=settings.embedding_batch_size, logger=logger)

    def embed_query(self, query_text: str, *, request_id: str | None = None) -> np.ndarray:
        """Gera vetor `float32` L2-normalizado `(1, D)` para uma consulta textual."""
        normalized = build_query_embedding_text(query_text)
        return self.embed_queries([normalized], request_id=request_id)

    def embed_queries(self, query_texts: Sequence[str], *, request_id: str | None = None) -> np.ndarray:
        """Gera matriz `float32` L2-normalizada `(N, D)` para consultas."""
        cleaned = _validate_text_batch(query_texts, role="queries")
        started = time.monotonic()
        vectors = self._provider.embed_queries(cleaned, batch_size=self._batch_size)
        self._log_event("embedding_queries_generated", item_count=len(cleaned), started=started, request_id=request_id)
        return vectors

    def embed_documents(self, texts: Sequence[str], *, request_id: str | None = None) -> np.ndarray:
        """Gera matriz `float32` L2-normalizada `(N, D)` em lotes para textos de produtos."""
        cleaned = _validate_text_batch(texts, role="documents")
        started = time.monotonic()
        vectors = self._provider.embed_documents(cleaned, batch_size=self._batch_size)
        self._log_event("embedding_documents_generated", item_count=len(cleaned), started=started, request_id=request_id)
        return vectors

    def embed_candidates(
        self,
        candidates: Sequence[Candidate],
        *,
        request_id: str | None = None,
    ) -> np.ndarray:
        """Gera embeddings transitórios para `Candidate`s usando apenas atributos semânticos."""
        if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
            raise ValueError("candidates deve ser uma sequência válida de Candidate.")
        if len(candidates) == 0:
            raise ValueError("Lista de candidates para embedding não pode ser vazia.")

        texts = [build_candidate_embedding_text(cand) for cand in candidates]
        started = time.monotonic()
        vectors = self._provider.embed_documents(texts, batch_size=self._batch_size)
        self._log_event(
            "embedding_candidates_generated",
            item_count=len(candidates),
            started=started,
            request_id=request_id,
        )
        return vectors

    def _log_event(self, event: str, *, item_count: int, started: float, request_id: str | None) -> None:
        if self._logger is not None:
            info = self._provider.info()
            log_operational_event(
                self._logger,
                logging.INFO,
                event,
                stage="semantic_embedding",
                provider=info.backend,
                item_count=item_count,
                duration_ms=round((time.monotonic() - started) * 1000.0, 2),
                request_id=request_id,
            )


__all__ = [
    "ALTERNATIVE_MODEL_NAME",
    "DeterministicStubEmbeddingProvider",
    "EmbeddingError",
    "EmbeddingInferenceError",
    "EmbeddingModelInfo",
    "EmbeddingModelLoadError",
    "EmbeddingProvider",
    "EmbeddingService",
    "FastEmbedEmbeddingProvider",
    "RECOMMENDED_MODEL_NAME",
    "SUPPORTED_MODEL_SPECS",
    "SentenceTransformerEmbeddingProvider",
    "clamp_unit_scores",
    "cosine_similarity_matrix",
    "l2_normalize",
    "resolve_model_spec",
]
