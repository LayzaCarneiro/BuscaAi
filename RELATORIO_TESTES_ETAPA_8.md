# Relatório de Testes e Validação — Etapa 8 (API HTTP FastAPI)

- **Data:** 25/11/2026
- **Branch / Commit base:** `waldeney` (`ed58065225485a67c391ca8990e05ea4f91b8e0c`)
- **Ambiente validado:** `.venv` Windows, **Python `3.14.7`**, `fastapi 0.165.0`, `uvicorn 0.54.0`, `starlette 1.10.0`, `langgraph 1.2.24`, `pydantic 2.14.3`, `pytest 9.2.0`
- **Escopo:** Exposição da camada HTTP B2B (`GET /v1/health`, `POST /v1/search`) em `src/buscai/api/` sem duplicar regras de domínio da Etapa 7 e sem iniciar a Etapa 9.

---

## Quadro Resumo de Classificação dos Requisitos

| # | Seção / Requisito | Classificação | Evidência principal |
| :-: | :--- | :---: | :--- |
| 1 | Arquitetura da API (`src/buscai/api/` + `create_app(...)`) | **PASSOU** | `src/buscai/api/{__init__,app,auth,dependencies,errors,rate_limit,routes,schemas}.py` |
| 2 | Dependências FastAPI + Uvicorn (`pyproject.toml`, `requirements.txt`, Python 3.14.7) | **PASSOU** | `fastapi 0.165.0`, `uvicorn 0.54.0`, `pip check` sem conflitos |
| 3 | `GET /v1/health` (liveness vs readiness, obrigatório vs opcional, zero download/segredos) | **PASSOU** | `tests/test_api_health_and_lifespan.py` (6 testes) |
| 4 | `POST /v1/search` (via `SearchOrchestrator.run()` em threadpool) | **PASSOU** | `src/buscai/api/routes.py`, `tests/test_api_search_and_schemas.py` |
| 5 | Schemas HTTP estritos (`extra="forbid"`, D8 preservado, conversão explícita) | **PASSOU** | `src/buscai/api/schemas.py`, `tests/test_api_search_and_schemas.py` |
| 6 | Autenticação `X-API-Key` (`secrets.compare_digest`, rejeição de placeholders, fail-closed) | **PASSOU** | `src/buscai/api/auth.py`, `tests/test_api_auth_and_rate_limit.py` |
| 7 | Rate limiting em memória (janela deslizante, thread-safe, 429 + `Retry-After`, limpeza) | **PASSOU** | `src/buscai/api/rate_limit.py`, `tests/test_api_auth_and_rate_limit.py` |
| 8 | Tratamento centralizado de erros HTTP (400/401/422/429/500/502/503, zero vazamento) | **PASSOU** | `src/buscai/api/errors.py`, `tests/test_api_search_and_schemas.py` |
| 9 | Request ID (`X-Request-ID` validado/sanitizado e logging por allowlist) | **PASSOU** | `src/buscai/api/app.py`, `tests/test_api_security_and_concurrency.py` |
| 10 | Lifespan e dependências compartilhadas (sem bloqueio de event loop, sem preload forçado) | **PASSOU** | `src/buscai/api/dependencies.py`, `tests/test_api_health_and_lifespan.py` |
| 11 | Concorrência e isolamento por requisição | **PASSOU** | `tests/test_api_security_and_concurrency.py`, `tests/test_api_auth_and_rate_limit.py` |
| 12 | OpenAPI e Swagger (`/docs`, `/openapi.json`) | **PASSOU** | `test_openapi_schema_documents_endpoints_and_security_without_secrets` |
| 13 | Guia e coleção Postman (`GUIA_TESTES_POSTMAN_ETAPA_8.md`, `postman/`) | **PASSOU** | JSON validado (`10` requisições, variáveis `base_url` e `api_key` vazia) |
| 14 | Suíte offline completa (`python -m pytest -m "not ebay_live"` e `pytest -m "not ebay_live"`) | **PASSOU** | `352 passed, 1 deselected, 0 failed` em ambos os comandos |
| 15 | Comparação com a nova linha de base pré-Etapa-8 | **PASSOU** | Linha de base `294 passed` -> pós-Etapa 8 `352 passed` (`+58` novos testes, `0` regressões) |
| 16 | Auditoria de segurança e privacidade (logs, erros, disco, rede, tracing) | **PASSOU** | `tests/test_api_security_and_concurrency.py`, `sys.addaudithook` |
| 17 | Ferramentas auxiliares (`ruff`, `mypy`, `pytest-cov`) | **BLOQUEADO** | Não instaladas no `.venv` local (não instaladas arbitrariamente) |
| 18 | Preservação de `D1`–`D9` e integridade de `src/buscai/app/` | **PASSOU** | `git diff --name-only -- src/buscai/app/ AUDITORIA.md` vazio |
| 19 | Escopo proibido da Etapa 9/10 (`POST /v1/chat`, Redis, banco, frontend novo) | **NÃO APLICÁVEL** | Mantido fora de escopo conforme Seção 20 |

---

## 1. Arquitetura da API

- **Estado:** **PASSOU**
- A camada HTTP foi implementada exclusivamente em `src/buscai/api/`, desacoplada do Streamlit (`src/buscai/app/`) e consumindo diretamente `SearchOrchestrator.run()` da Etapa 7 sem duplicar regras de intenção, consulta, semântica, validação ou ranking:
  - [`src/buscai/api/__init__.py`](src/buscai/api/__init__.py) — exportações públicas da camada HTTP.
  - [`src/buscai/api/app.py`](src/buscai/api/app.py) — fábrica `create_app(...)`, instância `app` (`uvicorn buscai.api.app:app`), `lifespan` e middleware ASGI `RequestContextMiddleware`.
  - [`src/buscai/api/routes.py`](src/buscai/api/routes.py) — roteador `/v1` com `GET /v1/health` e `POST /v1/search`.
  - [`src/buscai/api/schemas.py`](src/buscai/api/schemas.py) — contratos HTTP estritos (`ConfigDict(extra="forbid")`) e conversor `build_search_response(...)`.
  - [`src/buscai/api/dependencies.py`](src/buscai/api/dependencies.py) — `AppContainer`, construção compartilhada no `lifespan`, avaliação de readiness e dependência de autenticação/rate limit.
  - [`src/buscai/api/auth.py`](src/buscai/api/auth.py) — `APIKeyAuthenticator` com `X-API-Key`.
  - [`src/buscai/api/rate_limit.py`](src/buscai/api/rate_limit.py) — `InMemoryRateLimiter` por cliente em memória.
  - [`src/buscai/api/errors.py`](src/buscai/api/errors.py) — `APIError`, sanitização de `X-Request-ID`, mapeamento de `OrchestrationError` e exception handlers centralizados.

## 2. Dependências adicionadas

- **Estado:** **PASSOU**
- Declaradas em [`pyproject.toml`](pyproject.toml) e [`requirements.txt`](requirements.txt):
  - `fastapi>=0.115,<1` (instalada `fastapi 0.165.0` no `.venv`)
  - `uvicorn>=0.34,<1` (`uvicorn 0.54.0` já presente no `.venv`, agora formalizada nos manifestos de produção)
- Além disso, foi instalado no `.venv` o pacote `langgraph>=1.2,<2` (`langgraph 1.2.24`), já declarado em `pyproject.toml`/`requirements.txt` desde a Etapa 7, viabilizando a execução local dos testes de orquestração.
- `pip check` retornou `No broken requirements found.` em Python `3.14.7`.

## 3. GET /v1/health

- **Estado:** **PASSOU**
- Endpoint público em [`src/buscai/api/routes.py`](src/buscai/api/routes.py), sem chamadas externas, sem buscas e sem download automático de pesos.
- Diferencia explicitamente:
  - `liveness`: `"alive"` enquanto o processo estiver respondendo.
  - `readiness`: `"ready"` (HTTP `200`) somente quando **todos** os componentes obrigatórios (`api_auth`, `search_orchestrator`, `embeddings`, `tracing_policy`) estão `"ready"`; caso contrário retorna `"not_ready"` (HTTP `503`).
  - Componentes obrigatórios vs opcionais: `exchange_rate` possui `required=false`.
- Quando `EmbeddingService.is_ready()` é `False` (modelo ainda não carregado ou runtime ausente), `GET /v1/health` reporta `embeddings` como `not_ready` e retorna HTTP `503`.

## 4. POST /v1/search

- **Estado:** **PASSOU**
- Delega a execução integralmente para `container.orchestrator.run(...)` via `fastapi.concurrency.run_in_threadpool`, evitando bloqueio do event loop do Uvicorn.
- Mapeia explicitamente os estados finais:
  - `ExecutionStatus.SUCCESS` -> HTTP `200`, `status="ok"`.
  - `ExecutionStatus.CLARIFICATION_NEEDED` -> HTTP `200`, `status="clarification_needed"`, `clarification_question`.
  - `ExecutionStatus.NO_RESULTS` -> HTTP `200`, `status="no_results"`, `products=[]`.
  - `ExecutionStatus.ALL_REJECTED` -> HTTP `200`, `status="all_rejected"`, `rejected_count` e aviso explícito.
  - `ExecutionStatus.INVALID_PLAN` -> HTTP `400` (`invalid_search_plan`).
  - `ExecutionStatus.ERROR` -> mapeado por `map_orchestration_error(...)` para `400`/`500`/`502`/`503`.

## 5. Schemas HTTP

- **Estado:** **PASSOU**
- Todos os schemas HTTP em [`src/buscai/api/schemas.py`](src/buscai/api/schemas.py) utilizam `ConfigDict(extra="forbid")`.
- Decisão **D8** preservada integralmente: `UserIntent` e `PriceConstraint` em [`src/userintent/schema.py`](src/userintent/schema.py) permanecem com `ConfigDict(extra="ignore")`.
- `SearchRequest` valida: `query` obrigatória (não vazia, até 500 chars), `budget_brl` opcional (`Decimal > 0`, rejeitando negativos, zero, `bool`, `NaN` e `Inf`), `profile_id` opcional validado contra `load_default_registry()`, e `limit` opcional (`int` entre `1` e `search_max_limit`, rejeitando `bool` e floats).
- `SearchResponseSchema` expõe de forma tipada (sem devolver `SearchGraphState`): `status`, `request_id`, `profile_id`, `detected_category`, `clarification_needed`, `clarification_question`, `total_products`, `total_offers`, `rejected_count`, `relaxed`, `relaxations`, `exchange_rate_info`, `warnings` (incluindo avisos sobre estimativa cambial, frete desconhecido e critérios `unknown`) e `products` (grupos rankeados com ofertas preservadas, scores, explicações, critérios, evidências, validação, preço/moeda, conversão BRL estimada, frete, condição e vendedor quando disponível).

## 6. Autenticação X-API-Key

- **Estado:** **PASSOU**
- Implementada em [`src/buscai/api/auth.py`](src/buscai/api/auth.py) sobre [`src/buscai/config.py`](src/buscai/config.py) (`Settings.api_keys` via `BUSCAAI_API_KEYS`):
  - Comparação em tempo constante com `secrets.compare_digest`.
  - Rejeição automática de placeholders (`chave_cliente_*`, `coloque_sua_chave_aqui`, `placeholder`, `your_api_key`, etc.).
  - Fail-closed com HTTP `503` (`auth_not_configured`) quando nenhuma chave válida está configurada.
  - Rejeição de chave ausente ou inválida com HTTP `401` (`WWW-Authenticate: ApiKey`).
  - Chaves nunca são gravadas em logs ou retornadas em respostas HTTP.
  - `.env` não foi modificado; `.env.example` foi atualizado mantendo `# BUSCAAI_API_KEYS=` comentado.

## 7. Rate limiting

- **Estado:** **PASSOU**
- Implementado em [`src/buscai/api/rate_limit.py`](src/buscai/api/rate_limit.py) (`InMemoryRateLimiter`):
  - Limite (`BUSCAAI_RATE_LIMIT_PER_MINUTE`, default `30`) e janela (`BUSCAAI_RATE_LIMIT_WINDOW_SECONDS`, default `60`) configuráveis.
  - Thread-safe via `threading.Lock`.
  - Chaveado por `client_id` opaco derivado via `HMAC-SHA256` com salt aleatório por processo — nenhuma API Key em texto puro é armazenada nos buckets.
  - Retorna HTTP `429` (`rate_limit_exceeded`) com `Retry-After` e cabeçalhos `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.
  - Limpeza periódica e sob demanda de buckets expirados + teto `max_clients` para controle de memória.
  - **Documentação multi-instância:** explicitado na docstring do módulo e no `README.md` que o contador é local ao processo.

## 8. Tratamento de erros

- **Estado:** **PASSOU**
- Handlers centralizados em [`src/buscai/api/errors.py`](src/buscai/api/errors.py) para `APIError`, `RequestValidationError` (`422`), `TracingPolicyViolationError` (`503`), `StarletteHTTPException` (`404`/`405`) e `Exception` (`500`).
- Envelope JSON consistente: `{"error": {"code", "message", "request_id", "details?"}}`.
- Nunca retorna `str(exc)` bruto nem conteúdo de anúncios (`title`, URLs, vendedores) e não confunde falhas de provedores (`502`/`503`) com `no_results` (`200`).

## 9. Request ID

- **Estado:** **PASSOU**
- `RequestContextMiddleware` valida `X-Request-ID` com regex estrita `^[A-Za-z0-9._-]{1,64}$`, rejeitando `\r`, `\n`, caracteres de controle, espaços e comprimentos acima de 64 chars (gerando `uuid4().hex` quando ausente ou inválido).
- Propaga `X-Request-ID` no cabeçalho HTTP, no corpo de respostas/erros e nos logs operacionais restritos a `ALLOWED_LOG_FIELDS`.

## 10. Lifespan e readiness

- **Estado:** **PASSOU**
- `lifespan` em [`src/buscai/api/app.py`](src/buscai/api/app.py) instancia uma única vez o `AppContainer` compartilhado e fecha recursos HTTP no encerramento.
- Preload de embeddings é controlado por `BUSCAAI_EMBEDDING_PRELOAD` (padrão `false`), evitando download automático no startup e permitindo execução offline com injeção de stubs em `create_app(...)`.
- Execução de `SearchOrchestrator.run()` ocorre em threadpool (`run_in_threadpool`) sem bloquear o event loop.

## 11. Concorrência

- **Estado:** **PASSOU**
- Validado com `ThreadPoolExecutor` em `tests/test_api_security_and_concurrency.py` e `tests/test_api_auth_and_rate_limit.py`: requisições simultâneas sobre o mesmo `SearchOrchestrator` mantêm `request_id`, `budget_brl`, `profile_id` e falhas completamente isolados, e o rate limiter contabiliza exatamente o limite sem condições de corrida.

## 12. OpenAPI e Swagger

- **Estado:** **PASSOU**
- `/openapi.json` e `/docs` habilitados em `create_app(...)` com `debug=False`, descrevendo `/v1/health`, `/v1/search`, esquema de segurança `APIKeyHeader` (`X-API-Key`), códigos de status e exemplos sintéticos sem segredos ou dados reais da eBay.

## 13. Guia e coleção Postman

- **Estado:** **PASSOU**
- Criados [`GUIA_TESTES_POSTMAN_ETAPA_8.md`](GUIA_TESTES_POSTMAN_ETAPA_8.md) e [`postman/BUSCAAI_Etapa_8.postman_collection.json`](postman/BUSCAAI_Etapa_8.postman_collection.json) com variáveis `base_url` e `api_key` (vazia) e os 10 cenários solicitados.

## 14. Resultados dos testes

- **Estado:** **PASSOU** (58 novos testes da Etapa 8 adicionados em 4 arquivos, 100% offline com `TestClient` + stubs/mocks).
- **Nota técnica sobre execução no Windows sandbox:** `TestClient` -> `anyio` -> `asyncio.ProactorEventLoop._make_self_pipe()` requer loopback local no Windows (`127.0.0.1`), portanto os dois comandos completos abaixo foram executados fora do sandbox de rede (com zero chamadas externas comprovadas via `sys.addaudithook`):
  - `python -m pytest -m "not ebay_live" -q` -> **`352 passed, 1 deselected, 18 warnings in 6.15s`** (**PASSOU**)
  - `pytest -m "not ebay_live" -q` -> **`352 passed, 1 deselected, 18 warnings in 6.06s`** (**PASSOU**)
- **AST e importação de módulos:** `67` módulos de `src/` (excl. `src/buscai/app/`) validados via `ast.parse` + `importlib.import_module` com `0` erros (**PASSOU**).
- **Validação de manifestos:** `tomllib.loads(pyproject.toml)` e `json.loads(postman/BUSCAAI_Etapa_8.postman_collection.json)` executados com sucesso (**PASSOU**).
- **Ferramentas auxiliares (`ruff`, `mypy`, `pytest-cov`):** **BLOQUEADO** — pacotes ausentes no `.venv` local (não instalados arbitrariamente).

## 15. Comparação com a nova linha de base

- **Estado:** **PASSOU**
- **Linha de base inicial na abertura da retomada (pré-instalação de `langgraph`):** `4 collection errors` (`ModuleNotFoundError: No module named 'langgraph'`); ignorando os 4 arquivos de orquestração: `277 passed, 1 deselected`.
- **Nova linha de base completa pré-Etapa-8 (após instalar `langgraph 1.2.24`):** **`294 passed, 1 deselected, 0 failed`**.
- **Pós-Etapa 8:** **`352 passed, 1 deselected, 0 failed`** (`+58` testes novos, **`0` regressões**).

## 16. Auditoria de segurança e privacidade

- **Estado:** **PASSOU**
- Comprovado em `tests/test_api_security_and_concurrency.py`, `tests/test_api_auth_and_rate_limit.py` e `tests/test_api_health_and_lifespan.py`:
  - Zero escrita em disco e zero chamadas externas de rede (`sys.addaudithook`).
  - Zero cache de anúncios entre requisições (chamadas repetidas invocam o backend todas as vezes).
  - Zero segredos, consultas completas ou payloads de anúncios em respostas, erros ou logs JSON (`ALLOWED_LOG_FIELDS`).
  - Zero tracing (`TRACING_ENV_VARS` bloqueadas com `TracingPolicyViolationError` -> HTTP `503`).

## 17. Arquivos modificados

- **Criados:**
  - `src/buscai/api/__init__.py`
  - `src/buscai/api/app.py`
  - `src/buscai/api/auth.py`
  - `src/buscai/api/dependencies.py`
  - `src/buscai/api/errors.py`
  - `src/buscai/api/rate_limit.py`
  - `src/buscai/api/routes.py`
  - `src/buscai/api/schemas.py`
  - `tests/test_api_health_and_lifespan.py`
  - `tests/test_api_auth_and_rate_limit.py`
  - `tests/test_api_search_and_schemas.py`
  - `tests/test_api_security_and_concurrency.py`
  - `postman/BUSCAAI_Etapa_8.postman_collection.json`
  - `GUIA_TESTES_POSTMAN_ETAPA_8.md`
  - `RELATORIO_TESTES_ETAPA_8.md`
- **Modificados:**
  - `pyproject.toml`, `requirements.txt`, `src/buscai/config.py`, `.env.example`, `IMPLEMENTACOES.md`, `README.md`
- **Preservados intactos:** `.env`, `AUDITORIA.md`, `src/buscai/app/**`.

## 18. Limitações e pendências

- **Ferramentas de lint/tipagem/cobertura:** `ruff`, `mypy` e `pytest-cov` permanecem **BLOQUEADOS** por não estarem instalados no `.venv`.
- **Escopo reservado à Etapa 9 (não iniciada):** suíte dedicada `tests/test_compliance_ebay.py`, `tests/test_resilience.py`, benchmark E2E HTTP completo (`T9.3`) e bateria integrada controlada contra a API real de produção da eBay (`T9.4`).
- **Rate limiting e cota em memória:** limitados ao processo local (`1` worker).
- **Embeddings locais:** execução real do `intfloat/multilingual-e5-small` depende da instalação opcional de `[semantic]` / `[semantic-onnx]` e validação empírica futura.

## 19. Confirmação de preservação de D1–D9

- **Estado:** **PASSOU**
- **D1:** eBay Browse API permanece como provedor de produção com `QuotaGuard` (`5.000/dia`) e não persistência.
- **D2:** zero histórico Git reescrito; zero segredos versionados; `.env` intocado.
- **D3:** 4 categorias MVP e 6 perfis validados em `SearchRequest` via `load_default_registry()`.
- **D4:** apenas `GET /v1/health` e `POST /v1/search` implementados; `POST /v1/chat` mantido fora de escopo (Etapa 10).
- **D5:** autenticação `X-API-Key` + rate limiting em memória com nota multi-instância documentada.
- **D6:** conversão cambial `USD -> BRL` explícita com `is_estimate=True`, taxa, data e origem.
- **D7:** orquestração LangGraph preservada sem checkpointer, sem store e com tracing bloqueado.
- **D8:** `UserIntent` e `PriceConstraint` mantidos com `ConfigDict(extra="ignore")`; schemas HTTP com `ConfigDict(extra="forbid")`.
- **D9:** `src/buscai/app/` intacto; `DummyJSON` isolado fora de produção; `tests/test_architecture_boundaries.py` aprovado.
