from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Optional

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from buscai.config import Settings, load_settings
from buscai.observability import log_operational_event, sanitize_text

from .prompt import SYSTEM_PROMPT
from .schema import UserIntent

DEFAULT_LLM_TIMEOUT_SECONDS = 15.0
_TRANSIENT_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})
_TRANSIENT_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "unavailable",
    "timeout",
    "timed out",
    "rate limit",
    "resource_exhausted",
    "try again",
)


class IntentExtractionError(RuntimeError):
    """Falha ao extrair ou validar a intenção estruturada via LLM."""


class LLMUnavailableError(IntentExtractionError):
    """Indisponibilidade transitória ou timeout do provedor LLM após tentativas."""


def _validate_positive_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} deve ser numérico.")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError(f"{name} deve ser finito e maior que zero.")
    return numeric


def _validate_non_negative_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} deve ser numérico.")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError(f"{name} deve ser finito e não negativo.")
    return numeric


def _extract_http_status(exc: Exception) -> int | None:
    for attr in ("code", "status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and not isinstance(val, bool):
            return val
    resp = getattr(exc, "response", None)
    if resp is not None:
        status = getattr(resp, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            return status
    return None


def _extract_retry_after_seconds(exc: Exception) -> float | None:
    direct = getattr(exc, "retry_after", None)
    if isinstance(direct, (int, float)) and not isinstance(direct, bool):
        numeric = float(direct)
        if math.isfinite(numeric) and numeric >= 0.0:
            return numeric
    headers_candidates = (
        getattr(getattr(exc, "response", None), "headers", None),
        getattr(exc, "headers", None),
    )
    for headers in headers_candidates:
        if isinstance(headers, Mapping):
            for key, value in headers.items():
                if str(key).strip().casefold() == "retry-after":
                    try:
                        parsed = float(str(value).strip())
                    except ValueError:
                        return None
                    if math.isfinite(parsed) and parsed >= 0.0:
                        return parsed
                    return None
    return None


def _is_429_error(exc: Exception) -> bool:
    if _extract_http_status(exc) == 429:
        return True
    msg = str(exc).casefold()
    return any(marker in msg for marker in ("429", "rate limit", "resource_exhausted"))


def _is_transient_llm_error(exc: Exception) -> bool:
    if type(exc).__name__ == "BudgetExhaustedError":
        return False
    status = _extract_http_status(exc)
    if isinstance(exc, (genai_errors.APIError, httpx.HTTPStatusError)) and status is not None:
        return status in _TRANSIENT_HTTP_CODES
    if isinstance(exc, (TimeoutError, httpx.TimeoutException, httpx.HTTPError)):
        return True
    if isinstance(exc, genai_errors.APIError):
        return exc.code in _TRANSIENT_HTTP_CODES
    msg = str(exc).casefold()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def _format_contents(
    user_text: str,
    conversation_history: Sequence[Mapping[str, str]] | None,
) -> str:
    if not isinstance(user_text, str) or not user_text.strip():
        raise ValueError("A mensagem do usuário não pode ser vazia.")
    cleaned_current = user_text.strip()
    if conversation_history is None:
        return cleaned_current
    if isinstance(conversation_history, (str, bytes)):
        raise ValueError("conversation_history deve ser uma sequência de turnos.")

    formatted_turns: list[str] = []
    for turn in conversation_history:
        if not isinstance(turn, Mapping):
            raise ValueError("Cada turno de conversation_history deve ser um mapeamento com role e content.")
        raw_role = turn.get("role")
        raw_content = turn.get("content")
        if not isinstance(raw_role, str) or not isinstance(raw_content, str):
            raise ValueError("Cada turno deve informar role e content textuais.")
        role = raw_role.strip().casefold()
        content = raw_content.strip()
        if role not in {"user", "assistant"}:
            raise ValueError("role em conversation_history deve ser 'user' ou 'assistant'.")
        if not content:
            raise ValueError("content em conversation_history não pode ser vazio.")
        label = "Usuário" if role == "user" else "Assistente"
        formatted_turns.append(f"- {label}: {content}")

    if not formatted_turns:
        return cleaned_current

    history_block = "\n".join(formatted_turns)
    return (
        f"Histórico recente da conversa:\n{history_block}\n\n"
        f"Mensagem atual do usuário:\n{cleaned_current}"
    )


class IntentExtractor:
    """Extrai UserIntent usando Gemini Structured Outputs + Pydantic com retry e timeout."""

    def __init__(
        self,
        model: Optional[str] = None,
        client: Optional[genai.Client] = None,
        *,
        settings: Optional[Settings] = None,
        timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
        max_retries: int = 2,
        base_backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 4.0,
        jitter_ratio: float = 0.0,
        random_fn: Callable[[], float] = random.random,
        retry_on_429: bool = True,
        on_retry: Callable[[int, float, int | None, float | None], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        logger: Optional[logging.Logger] = None,
    ):
        self.timeout_seconds = _validate_positive_finite(timeout_seconds, "timeout_seconds")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries deve ser um inteiro não negativo.")
        self.max_retries = max_retries
        self.base_backoff_seconds = _validate_non_negative_finite(base_backoff_seconds, "base_backoff_seconds")
        self.max_backoff_seconds = _validate_non_negative_finite(max_backoff_seconds, "max_backoff_seconds")
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("max_backoff_seconds deve ser maior ou igual a base_backoff_seconds.")
        self.jitter_ratio = _validate_non_negative_finite(jitter_ratio, "jitter_ratio")
        self._random_fn = random_fn
        self.retry_on_429 = bool(retry_on_429)
        self._on_retry = on_retry
        self._sleep = sleep
        self._logger = logger

        active_settings = settings
        if active_settings is None and (model is None or client is None):
            active_settings = load_settings(load_dotenv_file=False)

        if model is not None:
            if not isinstance(model, str) or not model.strip():
                raise ValueError("model deve ser uma string não vazia.")
            self.model = model.strip()
        else:
            assert active_settings is not None
            self.model = active_settings.gemini_model

        if client is not None:
            self.client = client
        else:
            assert active_settings is not None
            api_key = (
                active_settings.gemini_api_key.get_secret_value()
                if active_settings.gemini_api_key is not None
                else None
            )
            try:
                self.client = genai.Client(api_key=api_key)
            except ValueError as exc:
                raise IntentExtractionError(
                    "Credencial da API Gemini (GEMINI_API_KEY) ausente ou inválida."
                ) from exc

    def __repr__(self) -> str:
        return (
            f"IntentExtractor(model={self.model!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, max_retries={self.max_retries!r})"
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        **kwargs: object,
    ) -> IntentExtractor:
        """Constrói IntentExtractor a partir de Settings centralizado."""
        if not settings.is_gemini_configured:
            raise IntentExtractionError("GEMINI_API_KEY não configurada em Settings.")
        return cls(settings=settings, **kwargs)  # type: ignore[arg-type]

    def extract(
        self,
        user_text: str,
        *,
        conversation_history: Sequence[Mapping[str, str]] | None = None,
        request_id: str | None = None,
    ) -> UserIntent:
        """Converte texto livre (e histórico opcional) em UserIntent validado pelo Pydantic."""
        contents = _format_contents(user_text, conversation_history)
        timeout_ms = max(1, int(round(self.timeout_seconds * 1000)))
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=UserIntent,
            http_options=types.HttpOptions(
                timeout=timeout_ms,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

        response = self._generate_with_retry(contents=contents, config=config, request_id=request_id)

        # O SDK atual pode expor a resposta estruturada em `parsed`.
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, UserIntent):
            return parsed
        if parsed is not None:
            return UserIntent.model_validate(parsed)

        # Fallback robusto caso a versão do SDK não preencha `response.parsed`.
        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise IntentExtractionError("Gemini não retornou conteúdo para o UserIntent.")
        return UserIntent.model_validate_json(text)

    def _generate_with_retry(
        self,
        *,
        contents: str,
        config: types.GenerateContentConfig,
        request_id: str | None,
    ) -> object:
        retry_attempt = 0
        while True:
            started = time.monotonic()
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                duration_ms = round((time.monotonic() - started) * 1000.0, 2)
                safe_detail = sanitize_text(str(exc)) or type(exc).__name__

                if _is_transient_llm_error(exc):
                    http_status = _extract_http_status(exc)
                    retry_after = _extract_retry_after_seconds(exc)
                    allow_retry = self.retry_on_429 or not _is_429_error(exc)
                    if allow_retry and retry_attempt < self.max_retries:
                        base_delay = min(
                            self.max_backoff_seconds,
                            self.base_backoff_seconds * (2**retry_attempt),
                        )
                        if self.jitter_ratio > 0.0:
                            unit = max(0.0, min(1.0, float(self._random_fn())))
                            delay = base_delay + (base_delay * self.jitter_ratio * unit)
                        else:
                            delay = base_delay
                        if retry_after is not None and retry_after > delay:
                            delay = min(retry_after, self.max_backoff_seconds)
                        retry_attempt += 1
                        self._log_event(
                            logging.WARNING,
                            "intent_extraction_retry",
                            request_id=request_id,
                            duration_ms=duration_ms,
                            retry_attempt=retry_attempt,
                            error_type="LLMUnavailableError",
                        )
                        if self._on_retry is not None:
                            self._on_retry(retry_attempt, delay, http_status, retry_after)
                        self._sleep(delay)
                        continue

                    self._log_event(
                        logging.ERROR,
                        "intent_extraction_unavailable",
                        request_id=request_id,
                        duration_ms=duration_ms,
                        error_type="LLMUnavailableError",
                    )
                    raise LLMUnavailableError(
                        f"Provedor Gemini indisponível ou expirado: {safe_detail}"
                    ) from exc

                self._log_event(
                    logging.ERROR,
                    "intent_extraction_failed",
                    request_id=request_id,
                    duration_ms=duration_ms,
                    error_type="IntentExtractionError",
                )
                raise IntentExtractionError(f"Falha ao extrair intenção: {safe_detail}") from exc

            duration_ms = round((time.monotonic() - started) * 1000.0, 2)
            self._log_event(
                logging.INFO,
                "intent_extraction_completed",
                request_id=request_id,
                duration_ms=duration_ms,
                llm_calls=retry_attempt + 1,
            )
            return response

    def _log_event(self, level: int, event: str, **fields: object) -> None:
        if self._logger is not None:
            log_operational_event(
                self._logger,
                level,
                event,
                stage="intent_extraction",
                provider="gemini",
                **fields,
            )
