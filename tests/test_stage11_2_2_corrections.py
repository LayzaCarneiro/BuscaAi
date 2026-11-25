"""Testes offline da Etapa 11.2.2: correções P0, P1 e P2 do diagnóstico Gemini (31 cenários)."""

from __future__ import annotations

import io
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from google.genai import errors as genai_errors

from buscai.config import load_settings
from scripts import run_stage11_live_validation as runner
from userintent import IntentExtractor, LLMUnavailableError, UserIntent
from userintent.evaluator import (
    compute_failed_fields,
    evaluate_case,
    normalize_label,
    price_match,
)
from userintent.schema import PriceConstraint

ROOT = Path(__file__).resolve().parents[1]


def _load_eval_cases() -> dict[int, dict[str, Any]]:
    path = ROOT / "data" / "intent_eval_50.jsonl"
    return {
        int(row["case_id"]): row
        for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    }


def _offline_settings() -> Any:
    return load_settings(
        environ={
            "GEMINI_API_KEY": "AIzaOfflineSecretKeyStage11_2_2",
            "GEMINI_MODEL": "gemini-3.5-flash-lite",
            "EBAY_CLIENT_ID": "offline_ebay_id_1122",
            "EBAY_CLIENT_SECRET": "offline_ebay_secret_1122",
            "USD_BRL_RATE": "5.50",
        }
    )


def _ok_resp(intent: UserIntent, *, total_tokens: int = 120) -> object:
    return SimpleNamespace(
        parsed=intent,
        text=None,
        usage_metadata=SimpleNamespace(
            prompt_token_count=90,
            candidates_token_count=max(0, total_tokens - 90),
            total_token_count=total_tokens,
        ),
    )


def _fake_api_error(
    status_code: int,
    *,
    message: str = "api error",
    retry_after: str | None = None,
    secret_header: str | None = None,
) -> genai_errors.APIError:
    headers: dict[str, str] = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    if secret_header is not None:
        headers["Authorization"] = secret_header
        headers["X-Goog-Api-Key"] = secret_header
    req = httpx.Request("POST", "https://generativelanguage.invalid/v1beta/models/test:generateContent")
    resp = httpx.Response(
        status_code,
        request=req,
        headers=headers,
        json={"error": {"code": status_code, "message": message}},
    )
    return genai_errors.APIError(status_code, {"error": {"message": message}}, resp)


class _StubModels:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls = 0
        self.configs: list[Any] = []
        self.contents: list[str] = []

    def generate_content(self, *, model: str, contents: str, config: Any) -> object:
        del model
        self.calls += 1
        self.configs.append(config)
        self.contents.append(contents)
        return self._handler(self.calls, contents, config)


class _StubClient:
    def __init__(self, handler: Any) -> None:
        self.models = _StubModels(handler)


@pytest.fixture(autouse=True)
def _block_external_network() -> Any:
    violations: list[str] = []

    def _audit(event: str, args: tuple[object, ...]) -> None:
        if event == "socket.connect":
            addr = args[1] if len(args) >= 2 else None
            host = addr[0] if isinstance(addr, tuple) and addr else None
            if host not in {"127.0.0.1", "::1"}:
                violations.append(f"{event}:{host}")
        elif event == "urllib.Request":
            violations.append(event)

    sys.addaudithook(_audit)  # type: ignore[arg-type]
    yield
    assert violations == [], f"Chamadas externas detectadas: {violations}"


# ---------------------------------------------------------------------------
# P0 (#1 a #13): Runner, Backoff, Jitter, HTTP RetryOptions, Pacing, 429, Concorrência e Métricas
# ---------------------------------------------------------------------------


def test_p0_1_runner_preserves_exponential_backoff_without_half_second_override() -> None:
    retry_waits: list[float] = []

    def handler(call_idx: int, _contents: str, _config: Any) -> object:
        if call_idx in {1, 2}:
            raise _fake_api_error(503, message="model unavailable")
        return _ok_resp(
            UserIntent(
                category="notebook",
                price=PriceConstraint(max_price=4000, is_hard_constraint=True, currency="BRL"),
                use_cases=["programação", "jogos"],
                query="notebook",
            )
        )

    client = _StubClient(handler)
    report = runner.run_validation(
        mode="gemini",
        max_gemini_calls=3,
        sleep_between_calls=0.0,
        retry_sleep=retry_waits.append,
        max_retries=2,
        base_backoff_seconds=0.25,
        max_backoff_seconds=2.0,
        jitter_ratio=0.0,
        settings=_offline_settings(),
        gemini_client=client,
        run_gemini_flag=True,
    )
    assert retry_waits == [0.25, 0.5]
    assert report["global_usage_and_limits"]["gemini"]["retries_counted"] == 2
    assert report["environment"]["base_backoff_seconds"] == 0.25


def test_p0_2_exponential_backoff_applies_deterministic_jitter_and_sdk_retry_attempts_1() -> None:
    # (#2) Jitter determinístico via random_fn e (#3) http_options.retry_options.attempts == 1
    extractor_sleeps: list[float] = []
    client = _StubClient(
        lambda idx, _c, _cfg: (
            (_ for _ in ()).throw(_fake_api_error(503, message="temporarily overloaded"))
            if idx <= 2
            else _ok_resp(UserIntent(category="notebook", query="notebook"))
        )
    )
    extractor = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=client,  # type: ignore[arg-type]
        max_retries=2,
        base_backoff_seconds=1.0,
        max_backoff_seconds=8.0,
        jitter_ratio=0.5,
        random_fn=lambda: 0.4,
        sleep=extractor_sleeps.append,
    )
    res = extractor.extract("Quero um notebook", request_id="test-jitter")
    assert res.category == "notebook"
    # attempt 0 -> base=1.0 + 1.0*0.5*0.4 = 1.2; attempt 1 -> base=2.0 + 2.0*0.5*0.4 = 2.4
    assert extractor_sleeps == pytest.approx([1.2, 2.4])
    assert client.models.configs[0].http_options.retry_options.attempts == 1


def test_p0_3_runner_sdk_config_forces_single_sdk_attempt() -> None:
    captured_attempts: list[int | None] = []

    def handler(_idx: int, _c: str, config: Any) -> object:
        captured_attempts.append(config.http_options.retry_options.attempts)
        return _ok_resp(UserIntent(category="notebook", query="notebook"))

    client = _StubClient(handler)
    runner.run_validation(
        mode="gemini",
        max_gemini_calls=2,
        sleep_between_calls=0.0,
        settings=_offline_settings(),
        gemini_client=client,
        run_gemini_flag=True,
    )
    assert captured_attempts == [1, 1]


def test_p0_4_pacing_configurable_and_independent_of_retry_backoff(tmp_path: Path) -> None:
    pacing_sleeps: list[float] = []
    retry_sleeps: list[float] = []

    def handler(call_idx: int, _c: str, _cfg: Any) -> object:
        if call_idx == 2:
            raise _fake_api_error(503, message="503 retryable")
        return _ok_resp(UserIntent(category="notebook", query="notebook"))

    args = runner.parse_args(["--mode", "gemini", "--sleep-between-calls", "3.5"])
    assert args.sleep_between_calls == 3.5

    client = _StubClient(handler)
    out = tmp_path / "pacing.json"
    report = runner.run_validation(
        mode="gemini",
        max_gemini_calls=4,
        sleep_between_calls=3.5,
        pacing_sleep=pacing_sleeps.append,
        retry_sleep=retry_sleeps.append,
        jitter_ratio=0.0,
        base_backoff_seconds=1.0,
        settings=_offline_settings(),
        gemini_client=client,
        output_path=out,
        run_gemini_flag=True,
    )
    # 3 cenários executados (1+1 retry +1 -> 4 chamadas): 2 pausas de pacing entre cenários
    assert pacing_sleeps == [3.5, 3.5]
    assert retry_sleeps == [1.0]
    assert report["pacing"]["configured_sleep_between_calls_seconds"] == 3.5
    assert report["pacing"]["pauses_applied"] == 2
    assert report["pacing"]["total_pacing_sleep_seconds"] == 7.0
    assert report["global_usage_and_limits"]["pacing"]["pauses_applied"] == 2


def test_p0_5_6_7_http_429_stops_immediately_vs_503_retries_and_captures_retry_after_safely(
    tmp_path: Path,
) -> None:
    # (#5) Parada imediata em 429 sem retry, preservando cenários anteriores e marcando not_run
    # (#6) Captura segura de Retry-After sem vazar headers sensíveis
    # (#7) 503 separado de 429 (503 retenta, 429 interrompe fatal_stop=True)
    retry_sleeps: list[float] = []
    secret_token = "Bearer TOP_SECRET_DO_NOT_LOG_999"

    def handler(call_idx: int, _c: str, _cfg: Any) -> object:
        if call_idx == 1:
            # Cenário 1: passa normalmente
            return _ok_resp(
                UserIntent(
                    category="notebook",
                    price=PriceConstraint(max_price=4000, is_hard_constraint=True, currency="BRL"),
                    use_cases=["programação", "jogos"],
                    query="notebook",
                )
            )
        if call_idx == 2:
            # Cenário 2, tentativa 1: 503 transitório (DEVE retentar)
            raise _fake_api_error(503, message="Service Unavailable")
        if call_idx == 3:
            # Cenário 2, tentativa 2: sucesso
            return _ok_resp(
                UserIntent(
                    category="notebook",
                    price=PriceConstraint(max_price=3500, is_hard_constraint=True, currency="BRL"),
                    brands=["Lenovo"],
                    use_cases=["estudos"],
                    query="notebook Lenovo",
                )
            )
        # Cenário 3: 429 com Retry-After + header secreto -> DEVE parar imediatamente sem retry!
        raise _fake_api_error(
            429,
            message="Quota exhausted on per-minute RPM",
            retry_after="12.5",
            secret_header=secret_token,
        )

    client = _StubClient(handler)
    stream = io.StringIO()
    out = tmp_path / "stop_429.json"
    report = runner.run_validation(
        mode="both",
        max_gemini_calls=20,
        sleep_between_calls=0.0,
        retry_sleep=retry_sleeps.append,
        jitter_ratio=0.0,
        base_backoff_seconds=1.0,
        settings=_offline_settings(),
        gemini_client=client,
        progress_stream=stream,
        output_path=out,
        run_gemini_flag=True,
        run_ebay_flag=True,
    )

    assert client.models.calls == 4
    assert retry_sleeps == [1.0]  # apenas o 503 gerou retry; 429 não gerou retry nem sleep
    gem_global = report["global_usage_and_limits"]["gemini"]
    assert gem_global["fatal_stop"] is True
    assert gem_global["stop_reason"] == "http_429_rate_limit"
    assert gem_global["retry_after_seconds"] == 12.5
    assert gem_global["error_counts_by_category"] == {
        "http_503_unavailable": 1,
        "http_429_rate_limit": 1,
    }
    assert report["t11_4_e2e"]["status"] == "SKIPPED_GEMINI_BUDGET"

    gem_block = report["t11_3_gemini_eval"]
    assert gem_block["interrupted"] is True
    assert gem_block["interruption_reason"] == "http_429_rate_limit"
    assert gem_block["single_turn_results"][0]["status"] == "passed"
    assert gem_block["single_turn_results"][1]["status"] == "passed"
    assert gem_block["single_turn_results"][2]["status"] == "error"
    assert gem_block["single_turn_results"][2]["http_status"] == 429
    assert gem_block["single_turn_results"][2]["error_category"] == "http_429_rate_limit"
    assert gem_block["single_turn_results"][3]["status"] == "not_run"
    assert gem_block["single_turn_results"][3]["not_run_reason"] == "http_429_rate_limit"
    assert gem_block["multiturn_results"][0]["status"] == "not_run"

    # Sem vazamento de secrets no JSON ou stream
    raw_json = out.read_text(encoding="utf-8")
    assert secret_token not in raw_json
    assert secret_token not in stream.getvalue()

    # Verifica também chamada direta no IntentExtractor com retry_on_429=False (0 retries, levanta LLMUnavailableError)
    sleeps_direct: list[float] = []
    client_429 = _StubClient(
        lambda *_args: (_ for _ in ()).throw(_fake_api_error(429, message="rate limit", retry_after="9"))
    )
    extractor_429 = IntentExtractor(
        model="gemini-3.5-flash-lite",
        client=client_429,  # type: ignore[arg-type]
        max_retries=2,
        retry_on_429=False,
        sleep=sleeps_direct.append,
    )
    with pytest.raises(LLMUnavailableError):
        extractor_429.extract("Quero um notebook", request_id="test-429-no-retry")
    assert client_429.models.calls == 1
    assert sleeps_direct == []


def test_p0_8_9_budget_bounds_and_thread_safety_under_concurrency() -> None:
    # (#8) Validação de faixa 1..30 e contagem exata
    with pytest.raises(ValueError):
        runner.run_validation(mode="gemini", max_gemini_calls=0, run_gemini_flag=True)
    with pytest.raises(ValueError):
        runner.run_validation(mode="gemini", max_gemini_calls=True, run_gemini_flag=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        runner.run_validation(mode="gemini", max_gemini_calls=31, run_gemini_flag=True)

    # (#9) Proxy thread-safe sob concorrência respeitando teto exato
    barrier = threading.Barrier(12)
    raw_models = _StubModels(lambda idx, _c, _cfg: _ok_resp(UserIntent(query=f"q{idx}")))
    proxy = runner._GlobalGeminiModelsProxy(raw_models, max_calls=5)
    successes: list[int] = []
    budget_errors: list[Exception] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            proxy.generate_content(model="m", contents=f"msg {idx}", config={})
            with lock:
                successes.append(idx)
        except runner.BudgetExhaustedError as exc:
            with lock:
                budget_errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(successes) == 5
    assert len(budget_errors) == 7
    assert proxy.calls_made == 5
    assert raw_models.calls == 5


def test_p0_10_11_12_13_partial_snapshots_four_states_and_zero_denominator(tmp_path: Path) -> None:
    # (#10) global_usage_and_limits atualizado a CADA snapshot parcial (verificado durante a execução)
    # (#11) 4 estados: passed, failed, error, not_run
    # (#12) pass_rate_over_planned e pass_rate_over_completed com interrupção parcial
    # (#13) denominador zero retorna None (null em JSON)
    out = tmp_path / "live_snapshots.json"
    observed_snapshots: list[dict[str, Any]] = []

    def handler(call_idx: int, _c: str, _cfg: Any) -> object:
        # Antes da 2a e 3a chamadas, o arquivo --output já deve conter global_usage_and_limits atualizado!
        if call_idx >= 2 and out.exists():
            observed_snapshots.append(json.loads(out.read_text(encoding="utf-8")))
        if call_idx == 1:
            # single_1 -> passed
            return _ok_resp(
                UserIntent(
                    category="notebook",
                    price=PriceConstraint(max_price=4000, is_hard_constraint=True, currency="BRL"),
                    use_cases=["programação", "jogos"],
                    query="notebook",
                ),
                total_tokens=100,
            )
        if call_idx == 2:
            # single_2 -> failed (marca errada)
            return _ok_resp(
                UserIntent(
                    category="notebook",
                    price=PriceConstraint(max_price=3500, is_hard_constraint=True, currency="BRL"),
                    brands=["Dell"],
                    use_cases=["estudos"],
                    query="notebook",
                ),
                total_tokens=110,
            )
        # single_3 -> error (400 não retentável / fatal)
        raise _fake_api_error(400, message="bad request")

    report = runner.run_validation(
        mode="gemini",
        max_gemini_calls=10,
        sleep_between_calls=0.0,
        settings=_offline_settings(),
        gemini_client=_StubClient(handler),
        output_path=out,
        run_gemini_flag=True,
    )

    assert len(observed_snapshots) >= 1
    mid = observed_snapshots[0]
    assert mid["global_usage_and_limits"]["gemini"]["calls_made"] == 1
    assert mid["global_usage_and_limits"]["gemini"]["total_tokens_sum"] == 100
    assert mid["global_usage_and_limits"]["execution_state"]["scenarios_passed"] == 1

    st_sum = report["t11_3_gemini_eval"]["single_turn_summary"]
    assert st_sum == {
        "planned_count": 10,
        "passed_count": 1,
        "failed_count": 1,
        "error_count": 1,
        "not_run_count": 7,
        "completed_count": 2,
        "pass_rate_over_planned": 0.1,
        "pass_rate_over_completed": 0.5,
    }
    mt_sum = report["t11_3_gemini_eval"]["multiturn_summary"]
    assert mt_sum == {
        "planned_count": 8,
        "passed_count": 0,
        "failed_count": 0,
        "error_count": 0,
        "not_run_count": 8,
        "completed_count": 0,
        "pass_rate_over_planned": 0.0,
        "pass_rate_over_completed": None,
    }
    assert json.loads(out.read_text(encoding="utf-8"))["t11_3_gemini_eval"]["multiturn_summary"]["pass_rate_over_completed"] is None

    # Denominador zero quando somente erros ocorrem em single_turn (e 0 completed)
    zero_out = tmp_path / "zero_completed.json"
    zero_report = runner.run_validation(
        mode="gemini",
        max_gemini_calls=2,
        sleep_between_calls=0.0,
        settings=_offline_settings(),
        gemini_client=_StubClient(lambda *_args: (_ for _ in ()).throw(_fake_api_error(429, message="429"))),
        output_path=zero_out,
        run_gemini_flag=True,
    )
    assert zero_report["t11_3_gemini_eval"]["single_turn_core_pass_rate"] is None
    assert zero_report["t11_3_gemini_eval"]["single_turn_summary"]["pass_rate_over_planned"] == 0.0
    assert zero_report["t11_3_gemini_eval"]["single_turn_summary"]["error_count"] == 1
    assert zero_report["t11_3_gemini_eval"]["single_turn_summary"]["not_run_count"] == 9


# ---------------------------------------------------------------------------
# P1 (#14 a #24): PriceConstraint, Evaluator e Alinhamento do Benchmark
# ---------------------------------------------------------------------------


def test_p1_14_15_price_constraint_normalization_and_bounded_preservation() -> None:
    # (#14) Normalização quando min_price=None e max_price=None
    empty_weird = PriceConstraint(
        min_price=None,
        max_price=None,
        min_inclusive=False,
        max_inclusive=False,
        is_hard_constraint=True,
    )
    assert empty_weird.min_inclusive is True
    assert empty_weird.max_inclusive is True
    assert empty_weird.is_hard_constraint is False

    # (#15) Preservação com limite numérico
    bounded = PriceConstraint(
        min_price=100.0,
        max_price=2500.0,
        min_inclusive=False,
        max_inclusive=False,
        is_hard_constraint=True,
    )
    assert bounded.min_inclusive is False
    assert bounded.max_inclusive is False
    assert bounded.is_hard_constraint is True

    # price_match normaliza mesmo que o JSON esperado traga flags não-neutras sem bounds
    assert price_match(
        UserIntent(query="teste", price=PriceConstraint()),
        {"min_price": None, "max_price": None, "min_inclusive": False, "max_inclusive": False, "is_hard_constraint": True},
    )


def test_p1_16_17_18_19_dataset_single_21_single_3_and_explicit_photography() -> None:
    cases = _load_eval_cases()

    # (#16) Caso single_21 passa mesmo se modelo emitir flags divergentes em PriceConstraint vazio
    c21 = cases[21]["expected"]
    pred21 = UserIntent(
        category=None,
        price=PriceConstraint(min_price=None, max_price=None, min_inclusive=False, is_hard_constraint=True),
        query="presente para meu pai",
        clarification_needed=True,
        clarification_question="Qual produto ele gosta e qual é o seu orçamento máximo?",
    )
    res21 = evaluate_case(pred21, c21, 21)
    assert res21.price_correct is True
    assert res21.core_pass is True

    # (#17) Caso single_3 não exige "fotografia" por causa de "boa câmera"
    c3 = cases[3]["expected"]
    assert c3["use_cases"] == []
    pred3_ok = UserIntent(
        category="smartphone",
        price=PriceConstraint(max_price=2500, is_hard_constraint=True, currency="BRL"),
        brands=["Samsung"],
        use_cases=[],
        preferred_features=["boa câmera", "bateria longa"],
        query="smartphone Samsung boa câmera bateria longa",
    )
    assert evaluate_case(pred3_ok, c3, 3).core_pass is True

    # (#19) "boa câmera" (preferência) não equivale a "fotografia" (use_case):
    # Se caso explícito de fotografia (15 e 30) não tiver "fotografia" em use_cases, falha!
    c15 = cases[15]["expected"]
    c30 = cases[30]["expected"]
    assert c15["use_cases"] == ["fotografia", "uso diário"]
    assert c30["use_cases"] == ["fotografia"]
    pred15_missing_photo = UserIntent(
        category="smartphone",
        price=PriceConstraint(max_price=2000, is_hard_constraint=True, currency="BRL"),
        excluded_brands=["Motorola"],
        use_cases=["uso diário"],
        preferred_features=["boa câmera"],
        query="smartphone para fotos sem Motorola",
    )
    assert evaluate_case(pred15_missing_photo, c15, 15).core_pass is False

    # (#18) Quando explícito, "fotografia" é exigido e passa
    pred30_ok = UserIntent(
        category="câmera",
        price=PriceConstraint(max_price=4500, is_hard_constraint=True, currency="BRL"),
        use_cases=["fotografia"],
        query="câmera fotografia profissional",
    )
    assert evaluate_case(pred30_ok, c30, 30).core_pass is True


def test_p1_20_21_22_23_24_semantic_distinctions_gamer_normalization_and_hard_rules() -> None:
    cases = _load_eval_cases()

    # (#20) Diferenciação entre trabalho e escritório
    assert normalize_label("trabalho") != normalize_label("escritório")
    c29 = cases[29]["expected"]
    assert c29["use_cases"] == ["escritório"]

    # (#21) Normalização de estética gamer (mantendo "cadeira gamer" distinta)
    assert normalize_label("estética gamer") == normalize_label("gamer")
    assert normalize_label("teclado gamer") == normalize_label("estética gamer")
    assert normalize_label("estilo gamer") == normalize_label("estética gamer")
    assert normalize_label("cadeira gamer") == "cadeira gamer"

    pred29 = UserIntent(
        category="teclado",
        use_cases=["escritório"],
        required_features=["sem fio"],
        excluded_features=["RGB", "teclado gamer"],
        query="teclado sem fio escritório sem RGB",
    )
    assert evaluate_case(pred29, c29, 29).core_pass is True

    # (#22 e #23) Photoshop e design sem equivalência indevida + separação perfil vs use_case vs preferência
    assert normalize_label("Photoshop") != normalize_label("design")
    c32 = cases[32]["expected"]
    assert c32["use_cases"] == ["Photoshop", "edição de vídeo"]
    assert c32["profile_context"] == ["designer"]

    pred32 = UserIntent(
        category="notebook",
        price=PriceConstraint(max_price=6000, is_hard_constraint=False, currency="BRL"),
        use_cases=["Photoshop", "edição de vídeo"],
        profile_context=["designer"],
        preferred_features=[],
        query="notebook Photoshop edição de vídeo",
    )
    res32 = evaluate_case(pred32, c32, 32)
    assert res32.core_pass is True
    assert res32.profile_f1 == 1.0

    # (#24) Preservação de restrições rígidas, exclusões e esclarecimentos
    c6 = cases[6]["expected"]  # max_inclusive=False ("menos de R$ 3.000")
    pred6_bad_inclusive = UserIntent(
        category="tv",
        price=PriceConstraint(max_price=3000, max_inclusive=True, is_hard_constraint=True, currency="BRL"),
        use_cases=["entretenimento"],
        required_features=["55 polegadas"],
        query="TV 55 polegadas",
    )
    res6 = evaluate_case(pred6_bad_inclusive, c6, 6)
    assert res6.price_correct is False
    assert res6.core_pass is False
    assert compute_failed_fields(res6, pred6_bad_inclusive, c6) == [
        {"field": "price_constraint", "category": "price_inclusivity_mismatch"}
    ]


# ---------------------------------------------------------------------------
# P2 (#25 a #31): Observabilidade, failed_fields, Escrita Atômica, Segurança e Limpeza
# ---------------------------------------------------------------------------


def test_p2_25_to_31_attempt_trail_failed_fields_atomic_no_leaks_and_sanity_removed(
    tmp_path: Path,
) -> None:
    secret_prompt = "PROMPT_SECRETO_USUARIO_XYZ_777"
    secret_raw = "RESPOSTA_BRUTA_CONFIDENCIAL_ABC"
    out = tmp_path / "p2_audit.json"
    stream = io.StringIO()

    def handler(call_idx: int, _c: str, _cfg: Any) -> object:
        if call_idx == 1:
            raise _fake_api_error(503, message=f"transient {secret_raw}", retry_after="0.2")
        if call_idx == 2:
            # single_1 -> failed intencional (marca inesperada + uso errado + preço errado)
            return _ok_resp(
                UserIntent(
                    category="notebook",
                    price=PriceConstraint(max_price=9999, is_hard_constraint=True, currency="BRL"),
                    brands=["MarcaErrada"],
                    use_cases=["programação", "jogos"],
                    query=secret_raw,
                ),
                total_tokens=150,
            )
        if call_idx == 3:
            # single_2 -> passed
            return _ok_resp(
                UserIntent(
                    category="notebook",
                    price=PriceConstraint(max_price=3500, is_hard_constraint=True, currency="BRL"),
                    brands=["Lenovo"],
                    use_cases=["estudos"],
                    query="notebook Lenovo faculdade",
                ),
                total_tokens=130,
            )
        raise _fake_api_error(429, message="rate limit", retry_after="5")

    report = runner.run_validation(
        mode="gemini",
        max_gemini_calls=6,
        sleep_between_calls=0.0,
        retry_sleep=lambda _s: None,
        base_backoff_seconds=0.1,
        max_backoff_seconds=1.0,
        jitter_ratio=0.0,
        settings=_offline_settings(),
        gemini_client=_StubClient(handler),
        progress_stream=stream,
        output_path=out,
        run_gemini_flag=True,
    )

    # (#31) set_f1_sanity removido definitivamente
    assert "set_f1_sanity" not in report["t11_3_gemini_eval"]

    # (#27) Escrita atômica sem arquivo .tmp residual e JSON válido
    assert not out.with_name(f"{out.name}.tmp").exists()
    saved_text = out.read_text(encoding="utf-8")
    saved = json.loads(saved_text)

    # (#25) Trilha completa por tentativa HTTP
    trail = saved["global_usage_and_limits"]["gemini"]["attempt_trail"]
    assert len(trail) == 4
    required_keys = {
        "scenario_id",
        "attempt",
        "operation",
        "http_status",
        "outcome",
        "latency_ms",
        "tokens",
        "is_retry",
        "backoff_seconds",
        "interrupted",
        "error_category",
        "retry_after_seconds",
    }
    for entry in trail:
        assert required_keys.issubset(entry.keys())
    assert trail[0]["http_status"] == 503
    assert trail[0]["backoff_seconds"] == 0.2
    assert trail[0]["retry_after_seconds"] == 0.2
    assert trail[1]["is_retry"] is True
    assert trail[1]["tokens"] == 150
    assert trail[3]["http_status"] == 429
    assert trail[3]["interrupted"] is True
    assert trail[3]["retry_after_seconds"] == 5.0

    # (#26) failed_fields registrado em falha semântica
    first_row = saved["t11_3_gemini_eval"]["single_turn_results"][0]
    assert first_row["status"] == "failed"
    field_names = {item["field"] for item in first_row["failed_fields"]}
    assert {"price_constraint", "brands"}.issubset(field_names)

    # (#28) Cenário concluído (#2) preservado mesmo após 429 no cenário #3
    assert saved["t11_3_gemini_eval"]["single_turn_results"][1]["status"] == "passed"

    # (#29) Ausência de vazamento de secrets, resposta bruta ou prompt
    for blob in (saved_text, stream.getvalue()):
        assert "AIzaOfflineSecretKeyStage11_2_2" not in blob
        assert "offline_ebay_secret_1122" not in blob
        assert secret_prompt not in blob
        assert secret_raw not in blob


def test_p2_multiturn_failed_fields_and_summary_counts(tmp_path: Path) -> None:
    def handler(call_idx: int, _c: str, _cfg: Any) -> object:
        # 1..10: single-turn passam de forma genérica ou falham; após 10 -> M1 (passa) e M2 (falha) e M3 (429)
        if call_idx <= 10:
            return _ok_resp(UserIntent(category="notebook", query="notebook"))
        if call_idx == 11:
            # M1 passa
            return _ok_resp(
                UserIntent(
                    category="notebook",
                    price=PriceConstraint(max_price=4000, is_hard_constraint=True, currency="BRL"),
                    use_cases=["estudos"],
                    query="notebook para estudar",
                )
            )
        if call_idx == 12:
            # M2 falha em brands, required_features e price_constraint
            return _ok_resp(
                UserIntent(
                    category="notebook",
                    price=PriceConstraint(max_price=None, is_hard_constraint=False),
                    brands=[],
                    required_features=[],
                    query="notebook",
                )
            )
        raise _fake_api_error(429, message="429 stop")

    out = tmp_path / "mt_failed.json"
    report = runner.run_validation(
        mode="gemini",
        max_gemini_calls=15,
        sleep_between_calls=0.0,
        settings=_offline_settings(),
        gemini_client=_StubClient(handler),
        output_path=out,
        run_gemini_flag=True,
    )
    mt_rows = report["t11_3_gemini_eval"]["multiturn_results"]
    assert mt_rows[0]["id"] == "M1"
    assert mt_rows[0]["status"] == "passed"
    assert mt_rows[0]["failed_fields"] == []

    assert mt_rows[1]["id"] == "M2"
    assert mt_rows[1]["status"] == "failed"
    mt2_fields = {f["field"] for f in mt_rows[1]["failed_fields"]}
    assert {"brands", "required_features", "price_constraint"}.issubset(mt2_fields)

    assert mt_rows[2]["id"] == "M3"
    assert mt_rows[2]["status"] == "error"
    assert mt_rows[3]["status"] == "not_run"
    assert mt_rows[3]["not_run_reason"] == "http_429_rate_limit"

    mt_sum = report["t11_3_gemini_eval"]["multiturn_summary"]
    assert mt_sum["passed_count"] == 1
    assert mt_sum["failed_count"] == 1
    assert mt_sum["error_count"] == 1
    assert mt_sum["not_run_count"] == 5
    assert mt_sum["pass_rate_over_completed"] == 0.5
    assert mt_sum["pass_rate_over_planned"] == 0.125
