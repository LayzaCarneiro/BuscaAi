r"""Teste de integração opt-in controlado contra a API real do Google Gemini.

Desativado por padrão para evitar consumo involuntário de cota/custos.
Para executar manualmente (com GEMINI_API_KEY configurada):
    $env:RUN_GEMINI_INTEGRATION_TESTS="1"
    .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -v -m gemini_live
"""

from __future__ import annotations

import io
import os
import threading

import pytest

from buscai.config import load_settings
from buscai.observability import configure_logging
from userintent import IntentExtractor, UserIntent

pytestmark = [
    pytest.mark.gemini_live,
    pytest.mark.skipif(
        os.getenv("RUN_GEMINI_INTEGRATION_TESTS") != "1",
        reason=(
            "Teste real do Gemini desativado por padrão; "
            "defina RUN_GEMINI_INTEGRATION_TESTS=1 para habilitar (limite rígido de chamadas)."
        ),
    ),
]


class _CountingGeminiModels:
    def __init__(self, wrapped_models: object, *, max_calls: int) -> None:
        self._wrapped = wrapped_models
        self._max_calls = max_calls
        self._lock = threading.Lock()
        self.calls = 0

    def generate_content(self, *args: object, **kwargs: object) -> object:
        with self._lock:
            if self.calls >= self._max_calls:
                raise RuntimeError(f"Orçamento máximo Gemini excedido ({self._max_calls} chamadas).")
            self.calls += 1
        return self._wrapped.generate_content(*args, **kwargs)  # type: ignore[attr-defined]


class _CountingGeminiClient:
    def __init__(self, wrapped_client: object, *, max_calls: int) -> None:
        self.models = _CountingGeminiModels(wrapped_client.models, max_calls=max_calls)  # type: ignore[attr-defined]


def test_gemini_live_controlled_single_and_multiturn_extraction() -> None:
    settings = load_settings(load_dotenv_file=True)
    assert settings.is_gemini_configured, "GEMINI_API_KEY deve estar configurada no .env."

    log_stream = io.StringIO()
    logger = configure_logging("INFO", stream=log_stream, logger_name="buscai.test.gemini_live")
    raw_extractor = IntentExtractor.from_settings(settings, max_retries=0, logger=logger)
    guarded_client = _CountingGeminiClient(raw_extractor.client, max_calls=5)
    extractor = IntentExtractor(
        model=raw_extractor.model,
        client=guarded_client,  # type: ignore[arg-type]
        max_retries=0,
        logger=logger,
    )

    first = extractor.extract("Quero um notebook para estudar até R$ 4.000.", request_id="gem-live-1")
    assert isinstance(first, UserIntent)
    assert first.category == "notebook"
    assert first.price.max_price == 4000

    follow_up = extractor.extract(
        "Prefiro Lenovo e pelo menos 16 GB de RAM.",
        conversation_history=[
            {"role": "user", "content": "Quero um notebook para estudar até R$ 4.000."},
            {"role": "assistant", "content": "Busca realizada na categoria notebook até R$ 4000."},
        ],
        request_id="gem-live-2",
    )
    assert isinstance(follow_up, UserIntent)
    assert follow_up.category == "notebook"
    assert guarded_client.models.calls <= 2

    logs_raw = log_stream.getvalue()
    assert settings.gemini_api_key is not None
    assert settings.gemini_api_key.get_secret_value() not in logs_raw
