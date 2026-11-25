# Relatório de Testes e Verificação — Etapa 10 (`POST /v1/chat`)

- **Data:** 25/11/2026
- **Branch Git:** `waldeney` (commit base `ed58065225485a67c391ca8990e05ea4f91b8e0c`, sem commits ou push realizados)
- **Escopo:** Implementação do endpoint conversacional multi-turn stateless `POST /v1/chat`, contrato `ChatRequest`/`ChatResponse`, política de sanitização de histórico, resposta determinística em PT-BR baseada em evidências, 8 cenários sintéticos multi-turn, segurança/privacidade/concorrência, coleção Postman e atualização documental.
- **Modo de execução da suíte:** 100% offline com `TestClient`, mocks e stubs sintéticos (`ebay_live` mantido opt-in via `RUN_EBAY_INTEGRATION_TESTS=1`).

---

## Quadro-Resumo de Classificação dos Requisitos

| Requisito / Item | Evidência principal | Classificação |
| :--- | :--- | :---: |
| **T10.1** Contrato `ChatRequest` / `ChatHistoryMessage` (`extra="forbid"`, limites) | [`src/buscai/api/schemas.py`](src/buscai/api/schemas.py), [`tests/test_api_chat.py`](tests/test_api_chat.py) | **PASSOU** |
| **T10.2** Continuidade conversacional e Política 5.1 (sanitização eBay) | [`src/userintent/prompt.py`](src/userintent/prompt.py), [`src/buscai/api/chat.py`](src/buscai/api/chat.py) | **PASSOU** |
| **T10.3** Integração direta com `SearchOrchestrator.run()` (`POST /v1/chat`) | [`src/buscai/api/routes.py`](src/buscai/api/routes.py) | **PASSOU** |
| **T10.4** Estados conversacionais (`ok`, `clarification_needed`, `no_results`, `all_rejected`, erros) | [`src/buscai/api/chat.py`](src/buscai/api/chat.py), [`tests/test_api_chat.py`](tests/test_api_chat.py) | **PASSOU** |
| **T10.5** Contrato `ChatResponse` (reutilizando `SearchResponseSchema`) | [`src/buscai/api/schemas.py`](src/buscai/api/schemas.py), OpenAPI `/openapi.json` | **PASSOU** |
| **T10.6** Resposta textual determinística em PT-BR fundamentada em evidências | [`src/buscai/api/chat.py`](src/buscai/api/chat.py) (`build_chat_response`) | **PASSOU** |
| **T10.7** Segurança, autenticação, rate limit, não persistência e privacidade | [`tests/test_api_chat.py`](tests/test_api_chat.py) (`sys.addaudithook`, logs, `X-API-Key`) | **PASSOU** |
| **T10.8** Testes automatizados offline de contratos, fluxo, erros e segurança | [`tests/test_api_chat.py`](tests/test_api_chat.py) | **PASSOU** |
| **T10.9** Testes integrados sintéticos multi-turn (8 cenários distintos) | [`tests/test_api_chat_multiturn.py`](tests/test_api_chat_multiturn.py) | **PASSOU** |
| **T10.10** Coleção Postman (+8 reqs), guias, `README.md`, `IMPLEMENTACOES.md`, relatório | [`postman/BUSCAAI_Etapa_8.postman_collection.json`](postman/BUSCAAI_Etapa_8.postman_collection.json), docs | **PASSOU** |
| **Escopo proibido** (zero DB, Redis, checkpointer, sessões server-side, frontend) | Inspeção de AST/imports e `git diff` em `src/buscai/app/` | **PASSOU** |
| **Qualidade (`ruff`, `mypy`, `pip check`, `pytest-cov`)** | Ferramentas locais no `.venv` | **PASSOU** |
| **Chamadas reais Gemini/eBay (`ebay_live`) e avaliação empírica `multilingual-e5-small`** | Fora da suíte offline padrão (`RUN_EBAY_INTEGRATION_TESTS=1` opt-in) | **NÃO EXECUTADO (opt-in / pendente)** |
| **Sessões server-side (`InMemorySessionStore` / `session_id`)** | Substituído pelo desenho stateless obrigatório | **NÃO APLICÁVEL** |

---

## 1. Arquitetura Conversacional

- O endpoint `POST /v1/chat` atua como uma camada fina e **estritamente stateless** sobre a pipeline existente da Etapa 7 (`SearchOrchestrator.run()`).
- Nenhuma sessão, histórico de conversa, resultado de busca ou embedding é armazenado no servidor entre requisições.
- O cliente mantém e reenvia `conversation_history` a cada turno, utilizando `conversation_context.assistant_turn_summary` como resumo seguro opcional para o próximo turno do assistente.
- Conforme documentado em [`IMPLEMENTACOES.md`](IMPLEMENTACOES.md), o rascunho anterior que previa `InMemorySessionStore` foi formalmente substituído pelo modelo stateless para atendimento integral aos requisitos da Etapa 10 e às decisões **D1–D9**.

## 2. Contrato `POST /v1/chat`

- Implementados em [`src/buscai/api/schemas.py`](src/buscai/api/schemas.py) com `ConfigDict(extra="forbid")` em todos os modelos HTTP:
  - `ChatHistoryMessage`: aceita exclusivamente `role` em `{"user", "assistant"}` e `content` textual não vazio.
  - `ChatRequest`: `message` (obrigatória, normalizada e não vazia), `conversation_history` (lista validada, default `[]`), `budget_brl`, `profile_id` e `limit` (reutilizando os mesmos helpers de validação de `SearchRequest` sem duplicação).
  - `ChatConversationContextSchema`: metadados seguros e não persistentes (`detected_category`, `profile_id`, `applied_budget_brl`, `clarification_needed`, `relaxed`, `assistant_turn_summary`).
  - `ChatResponse`: `status` (`"ok" | "clarification_needed" | "no_results" | "all_rejected"`), `request_id`, `assistant_message`, `clarification_needed`, `clarification_question`, `search_results` (`SearchResponseSchema | None`, reutilizado integralmente sem duplicação), `conversation_context` e `warnings`.
- Rota registrada em [`src/buscai/api/routes.py`](src/buscai/api/routes.py) e documentada automaticamente em `/openapi.json` e `/docs` com `APIKeyHeader`.

## 3. Utilização de `conversation_history`

- `sanitize_conversation_history()` em [`src/buscai/api/chat.py`](src/buscai/api/chat.py) prepara o histórico no formato exato `tuple[dict[str, str], ...]` esperado por `SearchOrchestrator.run()` e `IntentExtractor.extract()`.
- **Política 5.1 aplicada:** turnos `"assistant"` são filtrados por segmento antes do envio ao `IntentExtractor`, removendo qualquer referência a títulos de anúncios, URLs (`http://`, `https://`, `ebay.com`), identificadores (`v1|...|...`, `item_id`), preços/moedas (`R$`, `US$`, `USD`, `BRL`), vendedores ou listas de ofertas, mantendo apenas perguntas de esclarecimento e resumos seguros de estado/categoria/perfil.
- **Não duplicação:** a mensagem atual (`message`) nunca é anexada ao histórico enviado e, caso o cliente a envie repetida como último turno `"user"`, a duplicata final é removida antes da extração de intenção.

## 4. Continuidade entre Mensagens

- Adicionada a regra `15` ao `SYSTEM_PROMPT` em [`src/userintent/prompt.py`](src/userintent/prompt.py) instruindo explicitamente a combinação do `"Histórico recente da conversa"` com a mensagem atual:
  - Preserva categoria, finalidade de uso, marcas, requisitos e orçamento rígido válidos que não tenham sido modificados.
  - Mantém novas preferências em `preferred_features` sem promovê-las indevidamente a `required_features`.
  - Aplica alterações ou remoções explícitas (troca de orçamento, remoção de especificação ou troca de perfil) e evita repetir perguntas já respondidas.
- Reconciliação de `budget_brl` continua utilizando exclusivamente `merge_explicit_budget()` dentro de `build_search_plan_from_text()`.

## 5. Integração com `SearchOrchestrator`

- `POST /v1/chat` delega diretamente para `await run_in_threadpool(container.orchestrator.run, payload.message, budget_brl=payload.budget_brl, profile_id=payload.profile_id, limit=max(30, effective_limit), request_id=request_id, conversation_history=sanitized_history)`.
- Não existe segunda pipeline de busca nem segunda chamada ao Gemini (nem para reinterpretar histórico nem para redigir texto).
- Erros `ExecutionStatus.ERROR` e `ExecutionStatus.INVALID_PLAN` são convertidos pelos mesmos mecanismos da Etapa 8 (`map_orchestration_error` e HTTP `400 invalid_search_plan`), nunca virando busca vazia.

## 6. Comportamento de Esclarecimento

- Quando `result.status == ExecutionStatus.CLARIFICATION_NEEDED`:
  - `ChatResponse` retorna `status="clarification_needed"`, `clarification_needed=True`, `clarification_question` preenchida, `assistant_message` com a pergunta e `search_results=None`.
  - Comprovado em [`tests/test_api_chat.py`](tests/test_api_chat.py) e no Cenário 5 de [`tests/test_api_chat_multiturn.py`](tests/test_api_chat_multiturn.py) que **0 chamadas** à eBay são realizadas nesse estado e o turno seguinte com a resposta do usuário dispara a busca completa normalmente.

## 7. Respostas Baseadas em Evidências

- `build_chat_response()` em [`src/buscai/api/chat.py`](src/buscai/api/chat.py) gera `assistant_message` em PT-BR de forma 100% determinística a partir de `SearchResponseSchema` (que encapsula `Explanation`, `RankedProductGroup`, `RankedOffer`, `OfferValidationSchema`, `RelaxationSchema` e `warnings`):
  - **`ok`:** informa total de produtos/ofertas, categoria, perfil ativo, principais destaques usando exclusivamente `canonical_title` + `explanation.summary`, indicação de aprovação com lacunas (`approved_with_unknowns`), preferências flexíveis relaxadas e avisos de frete/critérios desconhecidos.
  - **`no_results`:** informa claramente que nenhum anúncio foi encontrado para os critérios, sem sugerir relaxamento de requisitos obrigatórios.
  - **`all_rejected`:** distingue explicitamente que candidatos foram encontrados (`rejected_count`), porém todos rejeitados pelas restrições obrigatórias, afirmando expressamente que requisitos obrigatórios não são relaxados automaticamente e indicando quando preferências flexíveis não bastaram.

## 8. Segurança e Privacidade

- Reutilização integral de `require_authenticated_client` (`X-API-Key` obrigatória via `secrets.compare_digest`), `InMemoryRateLimiter` (`429` + `Retry-After` + `X-RateLimit-*`), `ensure_tracing_disabled()` (variáveis LangSmith/LangChain retornam `503 policy_violation`) e `RequestContextMiddleware`.
- Auditoria em [`tests/test_api_chat.py`](tests/test_api_chat.py) com `sys.addaudithook` comprovou:
  - **0 escritas em disco** durante requisições HTTP `/v1/chat`.
  - **0 conexões externas de rede** (permitido apenas loopback local `127.0.0.1`/`::1` exigido pelo `TestClient` no Windows).
  - **0 vazamentos** nos logs operacionais (campos restritos estritamente a `ALLOWED_LOG_FIELDS`, sem `X-API-Key`, texto da conversa, histórico, títulos, URLs ou vendedores) e nas mensagens de erro HTTP.
  - **0 retenção entre requisições** (chamada subsequente sem histórico recebe `conversation_history == []` no extrator).

## 9. Limites do Histórico

- Configuráveis em `Settings` ([`src/buscai/config.py`](src/buscai/config.py)) e documentados em [`.env.example`](.env.example):
  - `BUSCAAI_CHAT_MAX_HISTORY_MESSAGES` (padrão `12`, teto estrutural `50`)
  - `BUSCAAI_CHAT_MAX_MESSAGE_CHARS` (padrão `500`, teto estrutural `2000`)
  - `BUSCAAI_CHAT_MAX_HISTORY_TOTAL_CHARS` (padrão `4000`, teto estrutural `20000`)
- Validados por `validate_chat_limits()` retornando HTTP `422 validation_error` com detalhes por campo e `request_id`.
- Também rejeitados com `422`: `message` vazia ou excedendo limite, `role` fora de `user`/`assistant` (`system`, `tool`), histórico não-lista ou malformado, propriedades extras em qualquer nível e soma total acima do limite.

## 10. Testes Multi-Turn

Executados os **8 cenários sintéticos ponta a ponta** em [`tests/test_api_chat_multiturn.py`](tests/test_api_chat_multiturn.py) via `TestClient` passando pela pilha completa (`POST /v1/chat -> SearchOrchestrator -> QueryBuilder -> Catálogo sintético -> SemanticSearchProcessor -> ValidationPipeline -> RankingPipeline -> build_chat_response`):

1. Notebook para estudo até R$ 4.000 seguido de "Prefiro Lenovo e pelo menos 16 GB de RAM" (preserva orçamento `4000.0 BRL`, categoria e uso).
2. Smartphone com pelo menos 256 GB seguido de alteração explícita de orçamento para R$ 3.500 (preserva `hard_feature:256gb` e atualiza teto para `3500.0 BRL`).
3. Monitor 4K seguido de preferência por 27 polegadas (preserva `4K` e mantém 27 polegadas como preferência flexível sem rejeitar outros tamanhos).
4. Headphones ANC seguido de "Quero novo" (preserva categoria/ANC e aplica condição `New`).
5. Busca inicial ambígua ("Quero um presente legal") com `clarification_needed=True` e `0` chamadas eBay, seguida de esclarecimento acionando a busca completa (`1` chamada).
6. Preferência flexível (`preferred_features`: tela OLED / caneta stylus) relaxada automaticamente pelo `RelaxationEngine` quando há baixa cobertura (`relaxed=True`, explicado em `assistant_message`).
7. Requisito obrigatório (orçamento rígido até R$ 500) não relaxável resultando em `status="all_rejected"` com `rejected_count >= 1` e mensagem explícita sem afirmar relaxamento de hard constraint.
8. Troca explícita de perfil (`best_value` -> `best_performance`) e remoção explícita de preferência anterior ("não precisa ter touchscreen").

## 11. Testes de Concorrência

- `test_chat_concurrency_isolation_and_openapi_contract` em [`tests/test_api_chat.py`](tests/test_api_chat.py) disparou 12 requisições concorrentes em 8 threads via `ThreadPoolExecutor` alternando categorias (`monitor` e `notebook`), `X-Request-ID` distintos e históricos próprios.
- Todas as 12 respostas retornaram `200`, `request_id` correspondente e `detected_category` correta sem contaminação entre requisições simultâneas.

## 12. Resultados de Lint, Tipagem e Cobertura

Todos os comandos abaixo foram executados após a última edição de código Python em `.venv` (Python `3.14.7`, Windows 11):

| Verificação | Comando | Resultado |
| :--- | :--- | :--- |
| **Suíte offline (`python -m pytest`)** | `.\.venv\Scripts\python.exe -m pytest -m "not ebay_live" -q` *(fora da sandbox)* | **`406 passed, 2 deselected, 18 warnings`** (`8.89s`) |
| **Suíte offline (`pytest`)** | `.\.venv\Scripts\pytest.exe -m "not ebay_live" -q` *(fora da sandbox)* | **`406 passed, 2 deselected, 18 warnings`** (`9.17s`) |
| **Cobertura (`pytest-cov`)** | `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -m "not ebay_live" --cov=buscai --cov=userintent --cov-config=pyproject.toml --cov-report=term-missing -q` | **`92%` global** (`6653` stmts, `544` miss; `api/chat.py` **`97%`**, `api/schemas.py` **`97%`**, `api/routes.py` **`98%`**, `api/app.py` **`100%`**) |
| **Lint (`ruff`)** | `.\.venv\Scripts\ruff.exe check --no-cache src/buscai/api src/buscai/config.py src/userintent/prompt.py tests/test_api_chat.py tests/test_api_chat_multiturn.py` | **`All checks passed!`** (`0` erros) |
| **Tipagem (`mypy`)** | `.\.venv\Scripts\mypy.exe --follow-imports=silent --cache-dir "$env:TEMP\buscai_mypy_cache" src/buscai/api` | **`Success: no issues found in 9 source files`** (`0` erros) |
| **Integridade de dependências** | `.\.venv\Scripts\python.exe -m pip check` | **`No broken requirements found.`** |
| **AST, imports, TOML e JSON Postman** | Verificação programática em `src/` (excl. `buscai/app/`), `pyproject.toml` e coleção Postman | **`OK mods= 68 chat_requests= 8`** (`0` erros) |

> **Observação sobre os 18 warnings:** todos os 18 warnings registrados vêm de bibliotecas de terceiros sob Python `3.14.7` (`17` em `langsmith/_internal/_beta_decorator.py:92` e `1` em `google/genai/types.py:42`), com `0` warnings originados no código do projeto.

## 13. Comparação com a Linha de Base (Etapa 9)

| Métrica | Linha de base (Etapa 9) | Pós-Etapa 10 | Delta |
| :--- | :---: | :---: | :---: |
| **Testes offline aprovados (`not ebay_live`)** | `370` | **`406`** | **`+36`** (`28` em `test_api_chat.py` + `8` em `test_api_chat_multiturn.py`) |
| **Falhas na suíte offline** | `0` | **`0`** | `0` regressões |
| **Testes `ebay_live` desmarcados** | `2` | **`2`** | Inalterado (`test_ebay_live.py` e `test_api_ebay_live.py`) |
| **Cobertura global (`src/buscai` excl. `app/` + `src/userintent`)** | `92%` (`6425` stmts, `540` miss) | **`92%`** (`6653` stmts, `544` miss) | **`+228` stmts cobertos em `92%`** |
| **Módulos verificados em `src/` (AST + import)** | `67` | **`68`** | **`+1`** (`src/buscai/api/chat.py`) |

## 14. Arquivos Modificados e Criados na Etapa 10

- **Novos:**
  - [`src/buscai/api/chat.py`](src/buscai/api/chat.py)
  - [`tests/test_api_chat.py`](tests/test_api_chat.py)
  - [`tests/test_api_chat_multiturn.py`](tests/test_api_chat_multiturn.py)
  - [`GUIA_TESTES_POSTMAN_CHAT_ETAPA_10.md`](GUIA_TESTES_POSTMAN_CHAT_ETAPA_10.md)
  - `RELATORIO_TESTES_ETAPA_10.md` *(este relatório)*
- **Alterados:**
  - [`src/buscai/config.py`](src/buscai/config.py)
  - [`.env.example`](.env.example)
  - [`src/userintent/prompt.py`](src/userintent/prompt.py)
  - [`src/buscai/api/schemas.py`](src/buscai/api/schemas.py)
  - [`src/buscai/api/routes.py`](src/buscai/api/routes.py)
  - [`src/buscai/api/__init__.py`](src/buscai/api/__init__.py)
  - [`postman/BUSCAAI_Etapa_8.postman_collection.json`](postman/BUSCAAI_Etapa_8.postman_collection.json)
  - [`GUIA_TESTES_POSTMAN_ETAPA_8.md`](GUIA_TESTES_POSTMAN_ETAPA_8.md)
  - [`README.md`](README.md)
  - [`IMPLEMENTACOES.md`](IMPLEMENTACOES.md)
- **Intocados (verificado via `git diff --name-only` e `git status --short`):**
  - `src/buscai/app/**`, `AUDITORIA.md`, `.env` (sem alteração; zero commits ou push).

## 15. Limitações e Pendências

- **Qualidade semântica real (`multilingual-e5-small`):** a avaliação empírica do modelo real de embeddings com dataset rotulado de anúncios reais da eBay permanece pendente (os testes atuais usam `DeterministicStubEmbeddingProvider` conforme regra offline).
- **Chamadas reais externas (`ebay_live` e Gemini real):** não executadas na suíte offline padrão; testes reais contra `EBAY_US` seguem opt-in via `RUN_EBAY_INTEGRATION_TESTS=1`.
- **Responsabilidade do cliente pelo histórico:** por ser estritamente stateless, o cliente HTTP deve enviar `conversation_history` dentro dos limites configurados em cada requisição; recomenda-se enviar `conversation_context.assistant_turn_summary` nos turnos do assistente.
- **Execução no Windows dentro da sandbox:** `TestClient` (Starlette/AnyIO) utiliza `socketpair()` em `127.0.0.1`; em sandboxes com rede de loopback bloqueada, a execução dos testes HTTP requer permissão local de loopback.

## 16. Confirmação de Preservação de D1–D9

- **D1 (eBay Browse API como fonte de produção):** preservada; nenhum mock ou DummyJSON no caminho produtivo (`tests/test_architecture_boundaries.py` aprovado).
- **D2 (Não persistência de anúncios e embeddings):** preservada; zero escrita em disco, zero cache de anúncios e zero armazenamento server-side de sessões/conversas.
- **D3 (4 categorias MVP e 6 perfis):** preservada (`best_value`, `best_performance`, `lowest_price`, `professional_use`, `study`, `everyday_use`).
- **D4 (`POST /v1/chat` após MVP `POST /v1/search`):** cumprida com `POST /v1/chat` implementado na Etapa 10 preservando integralmente `/v1/health` e `/v1/search`.
- **D5 (Segurança B2B e rate limiting em memória):** preservada (`X-API-Key` e `InMemoryRateLimiter` compartilhados por `/v1/search` e `/v1/chat`, sem DB/Redis).
- **D6 (Conversão USD/BRL explícita com `Decimal`):** preservada em `SearchResponseSchema` dentro de `ChatResponse`.
- **D7 (LangGraph fino sem checkpointer/store/tracing):** preservada integralmente.
- **D8 (`extra="ignore"` interno / `extra="forbid"` HTTP):** preservada (`UserIntent`/`PriceConstraint` mantêm `extra="ignore"`; `ChatRequest`/`ChatHistoryMessage`/`ChatResponse`/`ChatConversationContextSchema` usam `extra="forbid"`).
- **D9 (Preservação de `src/buscai/app/` e `AUDITORIA.md`):** confirmada (`git diff --name-only -- src/buscai/app/ AUDITORIA.md` retornou vazio).
