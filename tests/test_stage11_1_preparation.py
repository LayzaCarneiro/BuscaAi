"""Testes offline da Etapa 11.1: correção HTTPTransport, captura progressiva, dataset v2 e auditoria sem rede."""

from __future__ import annotations

import inspect
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from buscai.config import load_settings
from buscai.search import QuotaGuard, SearchQuery
from scripts import benchmark_embeddings
from scripts import run_stage11_live_validation as runner
from userintent.schema import PriceConstraint, UserIntent

ROOT = Path(__file__).resolve().parents[1]


class _StubModelsSequence:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls = 0

    def generate_content(self, *, model: str, contents: str, config: object) -> object:
        self.calls += 1
        return self._handler(self.calls, contents)


class _StubGeminiClient:
    def __init__(self, handler) -> None:
        self.models = _StubModelsSequence(handler)


def _offline_settings(*, with_ebay: bool = True, with_gemini: bool = True) -> object:
    env: dict[str, str] = {"USD_BRL_RATE": "5.50", "GEMINI_MODEL": "gemini-3.5-flash-lite"}
    if with_gemini:
        env["GEMINI_API_KEY"] = "AIzaOfflineFakeSecretKeyStage11"
    if with_ebay:
        env["EBAY_CLIENT_ID"] = "offline_ebay_client_id"
        env["EBAY_CLIENT_SECRET"] = "offline_ebay_secret_xyz"
    return load_settings(environ=env)


def test_httpx_transport_does_not_accept_timeout_and_clients_preserve_timeout() -> None:
    sig = inspect.signature(httpx.HTTPTransport.__init__)
    assert "timeout" not in sig.parameters

    mock_inner = httpx.MockTransport(lambda _r: httpx.Response(200, json={"access_token": "tok_ok", "expires_in": 3600}))
    guarded = runner._GuardedEbayTransport(max_calls=2, kind="browse", inner_transport=mock_inner)
    assert isinstance(guarded, httpx.BaseTransport)

    settings = _offline_settings()
    quota = QuotaGuard(daily_limit=8, default_max_search_per_request=1, default_max_get_item_per_request=0)
    oauth_transport = runner._GuardedEbayTransport(
        max_calls=1,
        kind="oauth",
        inner_transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"access_token": "tok_ok", "expires_in": 3600})
        ),
    )
    browse_transport = runner._GuardedEbayTransport(
        max_calls=2,
        kind="browse",
        inner_transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"total": 0, "itemSummaries": []})
        ),
    )
    oauth, backend = runner.build_ebay_clients(
        settings,  # type: ignore[arg-type]
        oauth_transport=oauth_transport,
        browse_transport=browse_transport,
        quota_guard=quota,
        max_retries=0,
    )
    try:
        # Confirma que timeout permanece configurado no httpx.Client, não no HTTPTransport.
        assert oauth._client.timeout is not None
        assert backend._client.timeout is not None
        resp = backend.search(SearchQuery(query_text="laptop", limit=1))
        assert resp.candidates == ()
        assert oauth_transport.calls_made == 1
        assert browse_transport.calls_made == 1
    finally:
        backend.close()
        oauth.close()


def test_guarded_ebay_transport_failure_401_and_budget_limit() -> None:
    err_transport = runner._GuardedEbayTransport(
        max_calls=5,
        kind="browse",
        inner_transport=httpx.MockTransport(lambda _r: httpx.Response(401, json={"error": "unauthorized"})),
    )
    req = httpx.Request("GET", "https://api.ebay.invalid/buy/browse/v1/item_summary/search")
    resp = err_transport.handle_request(req)
    assert resp.status_code == 401
    assert err_transport.fatal_stop is True
    with pytest.raises(runner.BudgetExhaustedError):
        err_transport.handle_request(req)

    cap_transport = runner._GuardedEbayTransport(
        max_calls=1,
        kind="oauth",
        inner_transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"access_token": "ok", "expires_in": 60})),
    )
    assert cap_transport.handle_request(httpx.Request("POST", "https://api.ebay.invalid/oauth")).status_code == 200
    with pytest.raises(runner.BudgetExhaustedError):
        cap_transport.handle_request(httpx.Request("POST", "https://api.ebay.invalid/oauth"))


def test_runner_progressive_capture_partial_preservation_global_budget_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    net_violations: list[str] = []

    def _audit(event: str, args: tuple[object, ...]) -> None:
        if event == "socket.connect":
            addr = args[1] if len(args) >= 2 else None
            host = addr[0] if isinstance(addr, tuple) and addr else None
            if host not in {"127.0.0.1", "::1"}:
                net_violations.append(f"{event}:{host}")
        elif event == "urllib.Request":
            net_violations.append(event)

    sys.addaudithook(_audit)  # type: ignore[arg-type]

    # Confirma recusa de limite > 30
    with pytest.raises(ValueError, match="30"):
        runner.run_validation(mode="gemini", max_gemini_calls=31, run_gemini_flag=True)

    # 1. Contabilização de retry + tokens + captura progressiva + preservação após falha no Bloco B (E2E)
    def gemini_handler(call_idx: int, _contents: str) -> object:
        if call_idx == 2:
            raise TimeoutError("transient timeout on attempt 1")
        parsed = UserIntent(
            category="notebook",
            brands=["Lenovo"],
            price=PriceConstraint(max_price=4000, is_hard_constraint=True, currency="BRL"),
            use_cases=["programação", "jogos"],
            query="notebook",
        )
        return SimpleNamespace(
            parsed=parsed,
            text=None,
            usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=40, total_token_count=140),
        )

    stream = io.StringIO()
    out_file = tmp_path / "partial_metrics.json"

    def _failing_e2e(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("Simulated downstream E2E failure with SENTINEL_SECRET=AIzaOfflineFakeSecretKeyStage11")

    monkeypatch.setattr(runner, "_run_e2e_integrated", _failing_e2e)
    settings = _offline_settings()
    stub_gemini = _StubGeminiClient(gemini_handler)
    oauth_mock = httpx.MockTransport(
        lambda _r: httpx.Response(200, json={"access_token": "tok_ephem", "expires_in": 3600})
    )
    browse_mock = httpx.MockTransport(
        lambda _r: httpx.Response(200, json={"total": 0, "itemSummaries": []})
    )

    # 1a. Com max_gemini_calls=5, Bloco A consome as 5 tentativas (4 cenários + 1 retry)
    # e Bloco B é ignorado com status SKIPPED_GEMINI_BUDGET.
    report_cap = runner.run_validation(
        mode="both",
        max_gemini_calls=5,
        settings=settings,  # type: ignore[arg-type]
        gemini_client=stub_gemini,
        oauth_inner_transport=oauth_mock,
        browse_inner_transport=browse_mock,
        progress_stream=stream,
        output_path=out_file,
        run_gemini_flag=True,
        run_ebay_flag=True,
    )
    assert report_cap["t11_3_gemini_eval"]["calls_used_in_block"] == 5
    assert report_cap["global_usage_and_limits"]["gemini"]["retries_counted"] == 1
    assert report_cap["global_usage_and_limits"]["gemini"]["total_tokens_sum"] == 4 * 140
    assert report_cap["t11_4_e2e"]["status"] == "SKIPPED_GEMINI_BUDGET"

    # 1b. Com max_gemini_calls=22, Bloco A conclui 18 cenários (19 tentativas contando retry),
    # Bloco B inicia e falha propositalmente, preservando Bloco A e globais no arquivo --output!
    stream_fail = io.StringIO()
    out_fail = tmp_path / "partial_after_e2e_fail.json"
    stub_gemini_2 = _StubGeminiClient(gemini_handler)
    with pytest.raises(RuntimeError, match="Simulated downstream E2E"):
        runner.run_validation(
            mode="both",
            max_gemini_calls=22,
            settings=settings,  # type: ignore[arg-type]
            gemini_client=stub_gemini_2,
            oauth_inner_transport=oauth_mock,
            browse_inner_transport=browse_mock,
            progress_stream=stream_fail,
            output_path=out_fail,
            run_gemini_flag=True,
            run_ebay_flag=True,
        )

    progress_lines = [json.loads(line) for line in stream_fail.getvalue().strip().splitlines()]
    assert len(progress_lines) == 18
    assert progress_lines[-1]["gemini_calls_made"] == 19
    assert progress_lines[-1]["gemini_retries"] == 1

    saved = json.loads(out_fail.read_text(encoding="utf-8"))
    assert saved["t11_2_ebay_live"]["status"] == "NOT_RUN"
    assert saved["t11_3_gemini_eval"]["calls_used_in_block"] == 19
    assert saved["t11_4_e2e"]["status"] == "PARTIAL_ERROR"
    assert saved["global_usage_and_limits"]["gemini"]["calls_made"] == 19
    assert saved["global_usage_and_limits"]["gemini"]["retries_counted"] == 1
    assert saved["global_usage_and_limits"]["gemini"]["total_tokens_sum"] == 18 * 140
    raw_saved = out_fail.read_text(encoding="utf-8")
    assert "AIzaOfflineFakeSecretKeyStage11" not in raw_saved
    assert "offline_ebay_secret_xyz" not in raw_saved
    assert "AIzaOfflineFakeSecretKeyStage11" not in stream_fail.getvalue()
    assert net_violations == []


def test_runner_shares_global_counter_between_a_and_b_and_respects_optin_flags(tmp_path: Path) -> None:
    # Sem flags opt-in, retorna SKIPPED sem fazer chamadas.
    skipped = runner.run_validation(mode="both", run_gemini_flag=False, run_ebay_flag=False)
    assert skipped["status"] == "SKIPPED"

    # Modo 'both' compartilha contador global entre A e B e não repete T11.2 isolada.
    def simple_handler(_idx: int, _contents: str) -> object:
        return SimpleNamespace(
            parsed=UserIntent(category="notebook", query="notebook"),
            text=None,
            usage_metadata=SimpleNamespace(prompt_token_count=50, candidates_token_count=20, total_token_count=70),
        )

    stream = io.StringIO()
    out_file = tmp_path / "shared_budget.json"
    stub_gemini = _StubGeminiClient(simple_handler)
    oauth_mock = httpx.MockTransport(
        lambda _r: httpx.Response(200, json={"access_token": "tok_ephem_2", "expires_in": 3600})
    )
    browse_mock = httpx.MockTransport(
        lambda _r: httpx.Response(200, json={"total": 0, "itemSummaries": []})
    )

    # Teto=20 -> 18 chamadas em A + até 2 chamadas no Bloco B (depois B pula cenários restantes por limite!)
    report = runner.run_validation(
        mode="both",
        max_gemini_calls=20,
        max_browse_calls=4,
        settings=_offline_settings(),  # type: ignore[arg-type]
        gemini_client=stub_gemini,
        oauth_inner_transport=oauth_mock,
        browse_inner_transport=browse_mock,
        progress_stream=stream,
        output_path=out_file,
        run_gemini_flag=True,
        run_ebay_flag=True,
    )
    assert report["t11_2_ebay_live"]["status"] == "NOT_RUN"
    assert report["t11_3_gemini_eval"]["calls_used_in_block"] == 18
    assert report["t11_4_e2e"]["status"] == "EXECUTED"
    assert report["global_usage_and_limits"]["gemini"]["calls_made"] == 20
    assert any(row.get("skipped_reason") == "gemini_budget_or_fatal_stop" for row in report["t11_4_e2e"]["scenarios"])


def test_semantic_v2_dataset_integrity_coverage_and_split_separation() -> None:
    cal_path = ROOT / "data" / "eval_semantic_v2_calibration.jsonl"
    eval_path = ROOT / "data" / "eval_semantic_v2_evaluation.jsonl"
    assert cal_path.exists()
    assert eval_path.exists()

    cal_rows = [json.loads(line) for line in cal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    eval_rows = [json.loads(line) for line in eval_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    assert all(row["synthetic"] is True and row["split"] == "calibration" for row in cal_rows)
    assert all(row["synthetic"] is True and row["split"] == "evaluation" for row in eval_rows)

    cal_ret = [r for r in cal_rows if r["task"] == "retrieval"]
    eval_ret = [r for r in eval_rows if r["task"] == "retrieval"]
    cal_ded = [r for r in cal_rows if r["task"] == "deduplication"]
    eval_ded = [r for r in eval_rows if r["task"] == "deduplication"]

    assert len(cal_ret) + len(eval_ret) >= 30
    assert len(cal_ded) + len(eval_ded) >= 100

    # Separação estrita entre calibração e avaliação (zero sobreposição de consultas, item_ids ou pares).
    assert {r["query_pt"].casefold() for r in cal_ret}.isdisjoint({r["query_pt"].casefold() for r in eval_ret})
    cal_pairs = {(r["left"]["title"].casefold(), r["right"]["title"].casefold()) for r in cal_ded}
    eval_pairs = {(r["left"]["title"].casefold(), r["right"]["title"].casefold()) for r in eval_ded}
    assert cal_pairs.isdisjoint(eval_pairs)

    all_rows = cal_rows + eval_rows
    raw_text = cal_path.read_text(encoding="utf-8") + eval_path.read_text(encoding="utf-8")
    assert "ebay.com/itm" not in raw_text.casefold()
    assert "v1|" not in raw_text

    # Cobertura obrigatória de idiomas, variantes, condições, specs e incompletude.
    all_langs = {lang for r in all_rows for lang in r["languages"]}
    assert {"pt-BR", "en-US"}.issubset(all_langs)

    all_dedup = cal_ded + eval_ded
    labels = {r["expected_label"] for r in all_dedup}
    assert {"probable_duplicate", "similar_product", "different_product"} == labels

    tags = " ".join(r["case_tag"] for r in all_dedup)
    for required_variant in ("mini", "plus", "pro", "max", "ultra", "se", "fe"):
        assert f"variant_{required_variant}" in tags

    conditions_seen = {
        side.get("condition")
        for r in all_dedup
        for side in (r["left"], r["right"])
        if side.get("condition") is not None
    }
    assert {"New", "Used", "Refurbished", "Open Box"}.issubset(conditions_seen)
    assert any("diff_ram" in r["case_tag"] for r in all_dedup)
    assert any("diff_storage" in r["case_tag"] for r in all_dedup)
    assert any("incompat_" in r["case_tag"] for r in all_dedup)
    assert any("incomplete_info" in r["case_tag"] for r in all_dedup)
    assert any("same_brand_diff_model" in r["case_tag"] for r in all_dedup)
    assert any("equiv_" in r["case_tag"] for r in all_dedup)

    # Validação via benchmark_embeddings com stub apenas para verificar compatibilidade de schema/condition (não modelo real).
    cal_summary = benchmark_embeddings.evaluate_model(cal_rows, backend="stub", model_name="intfloat/multilingual-e5-small")
    eval_summary = benchmark_embeddings.evaluate_model(eval_rows, backend="stub", model_name="intfloat/multilingual-e5-small")
    assert cal_summary["retrieval_cases"] == len(cal_ret)
    assert eval_summary["dedup_cases"] == len(eval_ded)
