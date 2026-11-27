# Relatório de Testes e Validação — Etapa 9 (Conformidade, Resiliência, Benchmark E2E e Documentação)

- **Data:** 25/11/2026
- **Branch / Commit base:** `waldeney` (`ed58065225485a67c391ca8990e05ea4f91b8e0c`)
- **Ambiente validado:** `.venv` Windows 11, **Python `3.14.7`**, `fastapi 0.165.0`, `uvicorn 0.54.0`, `langgraph 1.2.24`, `pydantic 2.14.3`, `pytest 9.2.0`, `pytest-cov 7.1.0`, `ruff 0.17.6`, `mypy 1.20.2`
- **Escopo:** Auditoria de conformidade, testes de resiliência HTTP, benchmark E2E HTTP sintético, preparação do teste opt-in `ebay_live`, revisão operacional, auditoria de qualidade (`ruff`, `mypy`, `pytest-cov`), investigação dos 18 warnings e documentação final — **sem iniciar a Etapa 10**.

---

## Quadro Resumo de Classificação dos Requisitos

| # | Requisito / Seção | Classificação | Evidência principal |
| :-: | :--- | :---: | :--- |
| 1 | `T9.1` — Auditoria de conformidade API e eBay (`tests/test_compliance_ebay.py`) | **PASSOU** | `4` testes integrados aprovados (`sys.addaudithook`, snapshot, `weakref`, AST, OAuth/Quota/429/5xx) |
| 2 | `T9.2` — Resiliência e falhas HTTP (`tests/test_resilience.py`) | **PASSOU** | `14` testes/casos aprovados (eBay, Gemini, FastAPI, cancelamento, concorrência mista) |
| 3 | `T9.3` — Benchmark E2E HTTP sintético (`scripts/benchmark_api_e2e.py`) | **PASSOU** | `100` reqs sintéticas (`10` cenários x `10`), `1.0` compliance, `ndcg_at_3=0.9669`, `mrr=1.0`, `p@3=1.0` |
| 4 | `T9.4` — Integração real controlada (`tests/integration/test_api_ebay_live.py`) | **NÃO EXECUTADO** | Implementada e isolada sob `RUN_EBAY_INTEGRATION_TESTS=1` (opt-in manual; `2` testes `ebay_live` desmarcados) |
| 5 | `T9.5` — Revisão operacional da API (`README.md`, `BUSCAAI_EMBEDDING_PRELOAD=false`) | **PASSOU** | Documentado comportamento `503 not_ready` sem download automático, `.env`, Uvicorn e multi-instância |
| 6 | `T9.6` — `ruff`, `mypy` e `pytest-cov` em `[dev]` | **PASSOU** | Instalados no `.venv`; `0` erros `ruff` nas Etapas 8–9, `0` erros `mypy --follow-imports=silent src/buscai/api`, `92%` cobertura |
| 7 | Investigação dos `18` warnings da Etapa 8 | **PASSOU** | `17` externos em `langsmith` + `1` externo em `google-genai`; `0` em código do projeto |
| 8 | `T9.7` — Documentação final (`README.md`, `IMPLEMENTACOES.md`, `RELATORIO_TESTES_ETAPA_9.md`) | **PASSOU** | `AUDITORIA.md` preservado intacto; nenhum relatório apagado recriado |
| 9 | Suíte offline completa (`python -m pytest -m "not ebay_live"` e `pytest -m "not ebay_live"`) | **PASSOU** | `370 passed, 2 deselected, 18 warnings` em ambas as execuções (`0` regressões frente aos `352` da Etapa 8) |
| 10 | Preservação de `D1`–`D9` e escopo (`POST /v1/chat` não implementado) | **PASSOU** | `git diff --name-only -- src/buscai/app/ AUDITORIA.md` vazio; `.env` intocado |

---

## 1. Auditoria de conformidade (`T9.1`)

- **Classificação:** **PASSOU**
- Arquivo criado: [`tests/test_compliance_ebay.py`](tests/test_compliance_ebay.py).
- **Dados e privacidade verificados:**
  - Uso exclusivo em memória com descarte comprovado após a resposta (`weakref.ref` + `gc.collect()` retornando `None`).
  - `sys.addaudithook` global (não restrito a `tmp_path`) e snapshots de `tempfile.gettempdir()` e `src/` comprovando **zero escritas em disco** e **zero sockets externos**.
  - Zero cache de anúncios entre requisições: duas chamadas consecutivas `POST /v1/search` disparam `2` chamadas à Browse API (`MockTransport`) e refletem imediatamente a mudança de preço (`450.00` → `499.00`).
  - `SafeJsonFormatter` + `capsys` em respostas `200`, `401`, `502`, `503` e `500` sem sentinelas de anúncio (`SENTINEL_EBAY_TITLE_9101`, URLs, vendedores, descrições), sem tokens OAuth, sem `GEMINI_API_KEY` e sem `X-API-Key`, com chaves restritas a `ALLOWED_LOG_FIELDS`.
  - LangGraph confirmado com `checkpointer is None`, `store is None` e bloqueio de `TRACING_ENV_VARS` (`HTTP 503`).
  - Checagem AST em `src/buscai/api/*.py` confirmando zero imports de bancos, Redis, cache HTTP/disco, `pickle`/`shelve` ou tracing.
- **Conformidade eBay verificada:** reuso de token OAuth em memória (`1` emissão para `2` buscas), `QuotaGuard`, `RequestCallBudget`, retries em `5xx` e `429` (`Retry-After`) e enriquecimento seletivo via `getItem` dentro do orçamento.

## 2. Testes de resiliência (`T9.2`)

- **Classificação:** **PASSOU**
- Arquivo criado: [`tests/test_resilience.py`](tests/test_resilience.py).
- **Cobertura integrada via `TestClient` + `SearchOrchestrator` + `MockTransport` + stub `genai.Client`:**
  - **eBay:** HTTP `400` (`502`), `401` (`502`), `403` (`502`), `429` + `Retry-After: 9` (`503` com `Retry-After: 9`), `500`/`502`/`503`/`504` (`503`), `httpx.ReadTimeout` (`503`), JSON inválido (`502`), resposta vazia (`200 no_results`), item corrompido descartado + item mínimo sem `seller`/`shipping`/`condition`/`image_url` (`200 ok` com `known=False` e `warnings`), falha parcial em `getItem` (`200 ok`), cota esgotada (`503`, nunca confundida com `no_results`).
  - **Gemini (`IntentExtractor`):** timeout com retries esgotados (`503 llm_unavailable`), `503` transitório com retries (`503 llm_unavailable`), resposta vazia (`502 llm_error`), JSON malformado (`502 llm_error` após ajuste em [`src/buscai/api/errors.py`](src/buscai/api/errors.py)), erro de autenticação `403` sem retry (`502 llm_error`), `clarification_needed` (`200` com `0` chamadas eBay).
  - **FastAPI e concorrência:** payload JSON malformado (`422`), auth ausente/inválida (`401`), rate limit (`429`), `orchestrator=None` (`503`), erro inesperado sanitizado (`500` sem stacktrace/segredos), simulação de cancelamento (`CancelledError` contido com recuperação na chamada seguinte) e concorrência mista (`ThreadPoolExecutor` com perfis, orçamentos, `X-Request-ID`, erros e rate limit simultâneos).

## 3. Benchmark HTTP E2E (`T9.3`)

- **Classificação:** **PASSOU**
- Arquivo criado: [`scripts/benchmark_api_e2e.py`](scripts/benchmark_api_e2e.py).
- Percorre a pilha real (`HTTP -> FastAPI -> Auth/RateLimit -> SearchOrchestrator -> QueryBuilder -> EbayLiveBackend(MockTransport) -> SemanticSearchProcessor(Stub) -> ValidationPipeline -> RankingPipeline -> HTTP Response`) cobrindo os **10 cenários obrigatórios** (notebook estudo com orçamento, smartphone armazenamento mínimo, monitor 4K, headphones Bluetooth ANC, preferência relaxada, sem resultados, rejeição por hard constraint, falha de provedor `503`, sem autenticação `401` e rate limit `429`).

## 4. Métricas e metodologia

- **Classificação:** **PASSOU**
- Execução reproduzível executada em `25/11/2026` (`python scripts/benchmark_api_e2e.py --iterations 10 --concurrency 4`):
  - **Ambiente e amostra:** `Python 3.14.7`, `Windows-11-10.0.26200-SP0`, `28` CPUs lógicas, `10` cenários × `10` iterações = **`100` requisições**, concorrência `4`, fronteiras externas stubadas em memória (não representa latência de rede real/produção).
  - **Desempenho HTTP sintético:**
    - `total_wall_seconds`: `0.719s` | `throughput_rps`: `139.15 req/s`
    - `mean_ms`: `26.802` | `p50_ms`: `26.121` | `p95_ms`: `46.793` | `p99_ms`: `54.763`
    - `http_2xx_rate`: `0.70` | `http_expected_error_rate`: `0.30` | `scenario_status_compliance_rate`: **`1.0`**
  - **Qualidade de ranking e validação (`50` execuções avaliadas):**
    - `hard_constraint_compliance_rate`: **`1.0`**
    - `offer_preservation_rate`: **`1.0`**
    - `ndcg_at_3`: **`0.9669`** | `mrr`: **`1.0`** | `precision_at_3`: **`1.0`**
    - `relaxation_accuracy`: **`1.0`**

## 5. Integração real opt-in com a eBay (`T9.4`)

- **Classificação:** **NÃO EXECUTADO** (preparada para execução opt-in manual deliberada).
- Arquivo criado: [`tests/integration/test_api_ebay_live.py`](tests/integration/test_api_ebay_live.py).
- **Limites rígidos configurados:**
  - Exige `RUN_EBAY_INTEGRATION_TESTS=1` (desativado por padrão; `pytest -m "not ebay_live"` desmarca os `2` testes `ebay_live`).
  - Testa as **4 categorias MVP** via `TestClient` + `SearchOrchestrator` + `EbayOAuthClient.from_settings()` + `EbayLiveBackend` com extrator determinístico (`0` chamadas Gemini), stub de embeddings (`0` downloads) e `RequestCallBudget(max_search_calls=1, max_get_item_calls=1)` por busca (teto total `1` OAuth + `<= 8` chamadas Browse API).
  - Não foi executada automaticamente conforme restrição do escopo.

## 6. Revisão operacional (`T9.5`)

- **Classificação:** **PASSOU**
- Documentação operacional revisada e atualizada em [`README.md`](README.md), cobrindo:
  - Criação/ativação `.venv`, instalação (`[dev]` e extras semânticos), `.env`, inicialização via `uvicorn buscai.api.app:app`, `BUSCAAI_API_KEYS` (rejeição de placeholders e fail-closed `503`), rate limiting local ao processo e limitação em múltiplos workers.
  - Comportamento preservado de `BUSCAAI_EMBEDDING_PRELOAD=false`: `GET /v1/health` reporta `liveness="alive"` e `readiness="not_ready"` (**HTTP `503`**) quando o modelo de embeddings ou credenciais obrigatórias não estão carregados, sem baixar pesos automaticamente.

## 7. Ruff, mypy e cobertura (`T9.6`)

- **Classificação:** **PASSOU** (instalados no extra `[dev]` em [`pyproject.toml`](pyproject.toml) e no `.venv`, compatíveis com Python `3.14.7`; `pip check` retornou `No broken requirements found.`).
- **Ruff (`0.17.6`):**
  - Nos arquivos das Etapas 8 e 9 (`src/buscai/api`, `tests/test_api_*.py`, `tests/test_compliance_ebay.py`, `tests/test_resilience.py`, `tests/integration/test_api_ebay_live.py`, `scripts/benchmark_api_e2e.py`): **`All checks passed!` (`0` erros)**.
  - No restante legado das Etapas 0–7 (`src`, `tests`, `scripts`), permanecem apontamentos de imports não usados/estilo (`F401`, `E731`) preservados para não alterar arquivos fora de escopo sem necessidade.
- **Mypy (`1.20.2`):**
  - `mypy --follow-imports=silent src/buscai/api`: **`Success: no issues found in 8 source files` (`0` erros)**.
  - Sem `--follow-imports=silent`, reporta `27` erros pré-existentes em `5` arquivos das Etapas 3–7 (`query_builder.py`, `validator.py`, `reranker.py`, `orchestration/nodes.py`, `orchestration/graph.py`), preservados sem refatoração de contratos internos.
- **Cobertura (`pytest-cov 7.1.0`, excl. `src/buscai/app/`):**
  - **`92%` global** (`6425` statements, `540` missed) sobre `370 passed, 2 deselected`.
  - Módulos `src/buscai/api/`: `__init__.py` (`100%`), `app.py` (`100%`), `routes.py` (`97%`), `auth.py` (`96%`), `schemas.py` (`96%`), `errors.py` (`90%`), `rate_limit.py` (`90%`), `dependencies.py` (`75%`, linhas não cobertas correspondem ao wiring ao vivo com credenciais reais de produção).

## 8. Warnings identificados

- **Classificação:** **PASSOU**
- Investigação dos `18` warnings da Etapa 8 (mantidos exatamente os mesmos `18` na Etapa 9, **nenhum originado em código do projeto**):
  1. `17` ocorrências de `DeprecationWarning` em `.venv\Lib\site-packages\langsmith\_internal\_beta_decorator.py:92` (`'asyncio.iscoroutinefunction' is deprecated and slated for removal in Python 3.16`).
  2. `1` ocorrência de `DeprecationWarning` em `.venv\Lib\site-packages\google\genai\types.py:42` (`'_UnionGenericAlias' is deprecated and slated for removal in Python 3.17`).
- Nenhuma supressão global de warnings foi adicionada.

## 9. Atualizações documentais (`T9.7`)

- **Classificação:** **PASSOU**
- [`README.md`](README.md), [`IMPLEMENTACOES.md`](IMPLEMENTACOES.md) e [`RELATORIO_TESTES_ETAPA_9.md`](RELATORIO_TESTES_ETAPA_9.md) atualizados de acordo com os resultados reais observados nesta máquina.
- [`AUDITORIA.md`](AUDITORIA.md) original preservado sem alterações; nenhum relatório histórico apagado foi recriado.

## 10. Arquivos modificados

- **Criados na Etapa 9:**
  - `tests/test_compliance_ebay.py`
  - `tests/test_resilience.py`
  - `scripts/benchmark_api_e2e.py`
  - `tests/integration/test_api_ebay_live.py`
  - `RELATORIO_TESTES_ETAPA_9.md`
- **Modificados na Etapa 9:**
  - `src/buscai/api/errors.py` (mapeamento `ValidationError` de `prepare_intent_and_plan` → `502 llm_error`)
  - `src/buscai/api/dependencies.py` (ajustes de tipagem para `mypy`)
  - `pyproject.toml` (`[project.optional-dependencies].dev` com `mypy`, `pytest-cov`, `ruff` + seções `[tool.ruff]`, `[tool.mypy]`, `[tool.coverage.run]`)
  - `README.md` e `IMPLEMENTACOES.md`
- **Preservados intactos:** `.env`, `AUDITORIA.md`, `src/buscai/app/**`.

## 11. Pendências

1. Execução opt-in manual de `tests/integration/test_ebay_live.py` e `tests/integration/test_api_ebay_live.py` (`RUN_EBAY_INTEGRATION_TESTS=1`) contra a API real de produção da eBay (`T9.4`).
2. Avaliação empírica do modelo `intfloat/multilingual-e5-small` sobre anúncios reais da eBay e calibração de limiares.
3. Correção futura opcional dos `27` apontamentos de `mypy` e `F401`/`E731` nos módulos das Etapas 0–7.
4. **Etapa 10 (Pós-MVP — `POST /v1/chat`)**: pendente e **não iniciada**.

## 12. Preservação de D1–D9

- **Classificação:** **PASSOU**
- **D1–D9** integralmente preservadas: eBay Browse API sem persistência (`D1`), zero segredos versionados (`D2`), `4` categorias e `6` perfis (`D3`), apenas `/v1/health` e `/v1/search` (`D4`), `X-API-Key` + rate limit local em memória (`D5`), câmbio `USD -> BRL` como estimativa (`D6`), LangGraph sem checkpointer/store/tracing (`D7`), `extra="ignore"` interno / `extra="forbid"` HTTP (`D8`), e `src/buscai/app/` + `DummyJSON` isolado intactos (`D9`).
