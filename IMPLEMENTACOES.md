# Registro Técnico de Implementações — BUSCAAI

> **Fonte de verdade:** código-fonte atual em `src/`, `tests/`, `scripts/` e `pyproject.toml`, complementado pelos relatórios disponíveis (`AUDITORIA_INDEPENDENTE_BUSCAAI.md`, `RELATORIO_RECUPERACAO_AMBIENTE.md`, `RELATORIO_TESTES_ETAPA_4_7.md`, `RELATORIO_TESTES_ETAPA_5.md`, `RELATORIO_TESTES_ETAPA_6.md` e `RELATORIO_TESTES_ETAPA_7.md`).  
> **Nota documental:** os relatórios individuais das Etapas 1–4 foram removidos intencionalmente do repositório; por isso, este registro não fabrica contagens históricas de testes para essas etapas.

---

## 1. Visão Geral

- **Objetivo do projeto:** construir um backend modular de busca inteligente e recomendação de produtos em marketplaces integrado à **eBay Browse API** (`EBAY_US`), convertendo consultas em linguagem natural (PT-BR) em recomendações rankeadas por múltiplos critérios e explicadas por evidências concretas, sem persistência de anúncios ou embeddings.
- **Arquitetura em camadas:**
  - `src/userintent/` — extração estruturada de intenção (`UserIntent`, `PriceConstraint`) via Google GenAI SDK e reconciliação de orçamento (`merge_explicit_budget`).
  - `src/buscai/config.py` e `src/buscai/observability/` — configuração segura via `Settings` (`SecretStr`) e logging JSON sanitizado por allowlist (`ALLOWED_LOG_FIELDS`).
  - `src/buscai/pricing/` — conversão cambial configurável USD/BRL com `Decimal` (`CurrencyConverter`, `FixedEnvExchangeRateProvider`).
  - `src/buscai/search/` — protocolo `SearchBackend`, `EbayOAuthClient`, `QuotaGuard`, `RequestCallBudget`, `EbayLiveBackend`, `ebay_mapper` e `QueryBuilder`.
  - `src/buscai/semantic/` — representação técnica por categoria, provedores de embedding (`intfloat/multilingual-e5-small` + stub offline), deduplicação híbrida conservadora e similaridade semântica em memória.
  - `src/buscai/catalog_profiles.json` e `src/buscai/catalog_registry.py` — registro declarativo único das 4 categorias MVP e 6 perfis de uso.
  - `src/buscai/validation/` — extração de evidências e contradições, validação `hard` vs `soft`, enriquecimento seletivo `getItem` e relaxamento controlado.
  - `src/buscai/ranking/` — reranking multicritério em dois níveis (grupos de produtos + ofertas preservadas) e explicabilidade determinística em PT-BR.
  - `src/buscai/orchestration/` — grafo fino LangGraph (`StateGraph`, `SearchOrchestrator`) sem checkpointer e sem tracing.
  - `src/buscai/catalog/`, `src/buscai/adapters/`, `src/buscai/agent/`, `src/buscai/app/` — isolamento de `DummyJSON` para testes offline e preservação integral do fluxo legado e do protótipo Streamlit.
- **Tecnologias declaradas (`pyproject.toml`):** `python >= 3.11` (validado em `3.13.17`), `google-genai>=1.0,<2`, `httpx>=0.28,<1`, `langgraph>=1.2,<2`, `numpy>=2,<3`, `pydantic>=2.10,<3`, `python-dotenv>=1.0,<2`, `tenacity>=9,<10`; extras `dev` (`pytest>=8,<10`), `semantic` (`sentence-transformers>=3,<6`), `semantic-onnx` (`fastembed>=0.5,<1`) e `ui` (`streamlit>=1.40,<1.67`).

---

## 2. Decisões Arquiteturais (D1–D9)

| ID | Diretriz aplicada | Status no código atual |
| :-: | :--- | :--- |
| **D1** | eBay Browse API (`EBAY_US`) como fonte de produção, limite padrão de `5.000` chamadas/dia e política estrita de não persistência de anúncios. | Aplicada (`search/`, `orchestration/`) |
| **D2** | Não reescrever histórico Git remoto; manter higiene estrita de segredos em código, logs e variáveis de ambiente. | Aplicada (`config.py`, `observability/`) |
| **D3** | **4 categorias MVP** (`notebook`, `smartphone`, `monitor`, `fone de ouvido`) e **6 perfis extensíveis** (`best_value`, `best_performance`, `lowest_price`, `professional_use`, `study`, `everyday_use`). | Aplicada (`catalog_profiles.json`, `ranking/`) |
| **D4** | MVP prioriza `GET /v1/health` e `POST /v1/search` (Etapa 8); `POST /v1/chat` reservado à Etapa 10 com gancho leve (`conversation_history`). | Preservada (`query_builder.py`, `orchestration/`) |
| **D5** | Autenticação B2B via `X-API-Key` e rate limiting em memória planejados para a camada HTTP (Etapa 8). | Preparada em `config.py`; HTTP pendente |
| **D6** | Taxa cambial USD/BRL configurável na v1 (`FixedEnvExchangeRateProvider`) com preços convertidos marcados como estimativa (`Decimal`). | Aplicada (`pricing/currency.py`) |
| **D7** | LangGraph fino com `StateGraph` (`TypedDict`), sem checkpointer, sem store e com tracing LangSmith/LangChain desabilitado. | Aplicada (`orchestration/`) |
| **D8** | `UserIntent` e `PriceConstraint` com `ConfigDict(extra="ignore")` para compatibilidade com Gemini; `extra="forbid"` reservado aos schemas HTTP (Etapa 8). | Aplicada (`userintent/schema.py`) |
| **D9** | `DummyJsonCatalog` isolado para testes offline, `MercadoLivreCatalog` removido e `src/buscai/app/` (Streamlit) integralmente preservado. | Aplicada e verificada |

---

## 3. Histórico de Implementação por Etapa

### Etapas 0–1 — Fundação e Configuração do Backend
1. **Objetivo:** estabelecer configuração segura, logging sanitizado, contratos de busca, controle de cota e desacoplamento de fontes legadas.
2. **Principais implementações:** `Settings` com `SecretStr` e validações; `SafeJsonFormatter` e `log_operational_event` restritos a `ALLOWED_LOG_FIELDS`; `SearchBackend` (`Protocol`), `QuotaGuard` com janela deslizante de 24h e `RequestCallBudget` por requisição; remoção do `MercadoLivreCatalog` e isolamento do `DummyJsonCatalog`.
3. **Módulos relevantes:** `src/buscai/config.py`, `src/buscai/observability/logging.py`, `src/buscai/catalog/errors.py`, `src/buscai/catalog/dummyjson.py`, `src/buscai/search/backend.py`, `src/buscai/search/quota.py`.
4. **Decisões técnicas:** adoção da allowlist estrita em logs, segregação entre `SearchBackend` e provedores offline, preservação de `src/buscai/app/`.
5. **Testes realizados:** suítes unitárias de fronteira arquitetural, configuração e cota (`tests/test_architecture_boundaries.py`, `tests/test_config.py`, `tests/test_quota.py`, etc.).
6. **Resultado da validação:** código presente e validado nas regressões subsequentes; **contagem individual original não registrada** devido à remoção intencional dos relatórios individuais das Etapas 1–4.
7. **Limitações e pendências:** sem histórico individual preservado das Etapas 1–4.

### Etapa 2 — Integração com eBay Browse API e Câmbio USD/BRL
1. **Objetivo:** integrar OAuth 2.0, busca e enriquecimento de itens na eBay Browse API (`EBAY_US`) e conversão monetária transparente.
2. **Principais implementações:** `EbayOAuthClient` (token apenas em memória, margem de renovação e `threading.Lock`), `EbayLiveBackend` (`item_summary/search` e `getItem` / `enrich_candidates` com retries `tenacity` e respeito a `Retry-After`), `ebay_mapper` defensivo em `Decimal`, `CurrencyConverter` e `FixedEnvExchangeRateProvider`.
3. **Módulos relevantes:** `src/buscai/search/ebay_auth.py`, `src/buscai/search/ebay_backend.py`, `src/buscai/search/ebay_mapper.py`, `src/buscai/search/models.py`, `src/buscai/pricing/currency.py`.
4. **Decisões técnicas:** não persistir respostas da eBay; usar `Decimal` para preços e câmbio; marcar conversões como estimativa (`EstimatedPriceBRL`).
5. **Testes realizados:** testes offline com `httpx.MockTransport` (`tests/test_ebay_backend.py`, `tests/test_ebay_auth.py`, `tests/test_currency.py`) e teste opt-in marcado com `ebay_live`.
6. **Resultado da validação:** suíte offline aprovada nas rodadas posteriores; teste `ebay_live` permanece opt-in.
7. **Limitações e pendências:** possível corrida de renovação OAuth sob múltiplos `401` concorrentes (`A9` da auditoria independente) permanece pendente.

### Etapa 3 — Extração de Intenção e QueryBuilder
1. **Objetivo:** evoluir a extração de intenção em PT-BR e construir planos determinísticos de consulta para a eBay.
2. **Principais implementações:** `UserIntent` e `PriceConstraint` com `extra="ignore"` (`D8`), `IntentExtractor` sobre `google-genai`, reconciliação de orçamento (`merge_explicit_budget`), `QueryBuilder` gerando `SearchPlan` com consulta principal e alternativas, e integrador `build_search_plan_from_text`.
3. **Módulos relevantes:** `src/userintent/schema.py`, `src/userintent/prompt.py`, `src/userintent/llm.py`, `src/userintent/budget.py`, `src/userintent/evaluator.py`, `src/buscai/search/query_builder.py`.
4. **Decisões técnicas:** compatibilizar schema Pydantic com `response_schema` do Gemini (`extra="ignore"`); manter gancho opcional `conversation_history` para a Etapa 10.
5. **Testes realizados:** `tests/test_schema.py`, `tests/test_intent_llm.py`, `tests/test_query_builder.py`.
6. **Resultado da validação:** validado offline com mocks (corrigido posteriormente na Etapa 4.7 quanto ao vazamento de termos PT-BR e orçamento entre moedas).
7. **Limitações e pendências:** avaliação empírica do prompt da Etapa 3 contra o Gemini real e extensão do `evaluator.py` (`A11`) seguem pendentes.

### Etapa 4 — Representação Semântica, Embeddings e Deduplicação
1. **Objetivo:** implementar representação textual de produtos, serviço de embeddings em memória, deduplicação conservadora e similaridade semântica.
2. **Principais implementações:** `extract_product_attributes` e `build_candidate_embedding_text`, `EmbeddingService` com provedores `SentenceTransformers`, `FastEmbed` e `DeterministicStubEmbeddingProvider`, `ProductDeduplicator`, `InMemorySemanticRanker` e `SemanticSearchProcessor`.
3. **Módulos relevantes:** `src/buscai/semantic/representation.py`, `src/buscai/semantic/embeddings.py`, `src/buscai/semantic/dedup.py`, `src/buscai/semantic/ranker.py`, `src/buscai/semantic/pipeline.py`, `scripts/benchmark_embeddings.py`.
4. **Decisões técnicas:** escolha arquitetural de `intfloat/multilingual-e5-small`; processamento estritamente em memória sem banco vetorial.
5. **Testes realizados:** suíte semântica offline e auditoria independente registrada em `AUDITORIA_INDEPENDENTE_BUSCAAI.md`.
6. **Resultado da validação:** arquitetura aprovada com ressalvas críticas registradas na auditoria independente (tratadas na Etapa 4.7).
7. **Limitações e pendências:** qualidade real do E5 e calibração empírica de limiares (`A2`) seguem pendentes.

### Etapa 4.6.1 — Recuperação do Ambiente e Correção das Regressões de Merge
1. **Objetivo:** recuperar o ambiente virtual Python após troca de máquina e sanear regressões introduzidas pelo merge `947302a`.
2. **Principais implementações:** criação da `.venv` com Python `3.13.17` via `uv` (`34` pacotes), remoção de inicialização duplicada com `os.getenv` em `src/userintent/llm.py`, remoção de arquivos legados reintroduzidos do Mercado Livre, limpeza de `print` de debug em `src/buscai/tools/products.py`, correção de import em `tests/test_schema.py` e adição de `pythonpath = ["src", "."]` em `pyproject.toml` (resolvendo `A8`).
3. **Módulos relevantes:** `pyproject.toml`, `src/userintent/llm.py`, `src/buscai/tools/products.py`, `.env.example`, `tests/test_schema.py`, `tests/test_intent_llm.py`.
4. **Decisões técnicas:** manter apenas dependências declaradas sem baixar pesos nem instalar extras de modelos; preservar `.gitignore` local.
5. **Testes realizados:** suíte completa offline via `python -m pytest` e `pytest` direto, AST em `src/`/`tests/`/`scripts/` e verificação de integridade.
6. **Resultado da validação:** **190 passed, 1 deselected (`ebay_live`), 0 failed** (`RELATORIO_RECUPERACAO_AMBIENTE.md`).
7. **Limitações e pendências:** achados `C1`, `A1`–`A7`, `A9`–`A14` encaminhados à Etapa 4.7 e etapas seguintes.

### Etapa 4.7 — Correções da Auditoria Técnica Independente
1. **Objetivo:** endereçar os achados prioritários de `AUDITORIA_INDEPENDENTE_BUSCAAI.md` (`T4.7.1`–`T4.7.6`).
2. **Principais implementações:** preservação de variantes (`mini`, `+` -> `plus`, `Pro/Max/Ultra/SE/FE`), veto por condição divergente e incerteza em modelos ausentes (`C1`); preservação de ofertas distintas em `DuplicateGroup.offers` removendo `len(title)` do desempate (`A1`); remoção do pass-through PT-BR em `_translate_feature_en` e limite `MAX_QUERY_TEXT_LENGTH = 100` (`A3`); conversão cambial no `merge_explicit_budget` preservando teto rígido mais restritivo (`A4`); normalização segura de exceções em `EmbeddingService` e testes de auditoria de disco via `sys.addaudithook` (`A5`/`A6`); pré-filtro `O(1)` e desativação padrão de `pair_evaluations` (`A10`); priorização de aspectos técnicos no texto de embedding (`A13`) e prefixo `query:` para similaridade simétrica no E5 (`A14`).
3. **Módulos relevantes:** `src/buscai/semantic/*`, `src/buscai/search/query_builder.py`, `src/userintent/budget.py`, e 7 arquivos de teste correspondentes.
4. **Decisões técnicas:** na dúvida na deduplicação, preservar variantes e ofertas distintas para a Etapa 5/6 decidir.
5. **Testes realizados:** +22 novos testes unitários, integração e conformidade (`RELATORIO_TESTES_ETAPA_4_7.md`).
6. **Resultado da validação:** **212 passed, 1 deselected (`ebay_live`), 0 failed**.
7. **Limitações e pendências:** `A2`, `A7`, `A9`, `A11`, `A12` mantidos como pendências registradas.

### Etapa 5 — Perfis, Validação por Evidências, Enriquecimento e Relaxamento
1. **Objetivo:** construir a camada determinística de validação de candidatos e eliminar triplicação de conhecimento de categorias/perfis.
2. **Principais implementações:** registro declarativo único (`src/buscai/catalog_profiles.json`, `src/buscai/catalog_registry.py`) alimentando `QueryBuilder` e `representation.py`; extração de evidências e detecção de contradições (`evidence.py`); `EvidenceValidator` separando `hard` vs `soft` e validando orçamento com `Decimal` e frete (`validator.py`); `SelectiveEnricher` acionando `getItem` apenas quando necessário dentro da cota (`enrichment.py`); `RelaxationEngine` flexibilizando 1 preferência por vez sem violar *hard constraints* (`relaxation.py`); `ValidationPipeline` (`pipeline.py`).
3. **Módulos relevantes:** `src/buscai/catalog_profiles.json`, `src/buscai/catalog_registry.py`, `src/buscai/validation/*`.
4. **Decisões técnicas:** nunca inventar evidências para critérios `unknown`; nunca relaxar orçamento rígido, marca, modelo ou requisitos obrigatórios.
5. **Testes realizados:** 46 novos testes em `tests/test_validation_registry.py`, `tests/test_validator.py`, `tests/test_validation_enrichment.py`, `tests/test_relaxation.py` e `tests/test_validation_integration_and_compliance.py`.
6. **Resultado da validação:** **258 passed, 1 deselected (`ebay_live`), 0 failed** (`RELATORIO_TESTES_ETAPA_5.md`).
7. **Limitações e pendências:** validação com respostas reais de `localizedAspects`/`getItem` da eBay e qualidade real do E5 (`A2`).

### Etapa 6 — Reranking Multicritério e Explicabilidade
1. **Objetivo:** ordenar candidatos validados conforme os 6 perfis e 4 categorias MVP com explicabilidade determinística ancorada em evidências.
2. **Principais implementações:** contratos de ranking (`models.py`), matrizes e justificativas de pesos por perfil/categoria com `semantic_relevance <= 0.15` e toggle (`weights.py`), 8 avaliadores determinísticos sem coerção de ausentes para zero (`criteria.py`), normalização min-max inversa em BRL (`Decimal`) com redistribuição proporcional, `evidence_coverage`, `confidence_factor` e penalidade por contradições (`normalization.py`), explicador determinístico PT-BR (`explainer.py`), `MultiCriteriaReranker` em 2 níveis (Nível 1 grupos / Nível 2 ofertas) com desempate invariante à permutação (`reranker.py`), `RankingPipeline` (`pipeline.py`) e benchmark sintético offline (`data/eval_ranking_synthetic.jsonl`, `scripts/benchmark_ranking.py`).
3. **Módulos relevantes:** `src/buscai/ranking/*`, `data/eval_ranking_synthetic.jsonl`, `scripts/benchmark_ranking.py`.
4. **Decisões técnicas:** dados ausentes não viram `0.0`; frete desconhecido impede afirmação de "custo total verificado"; score semântico limitado e desabilitável até validação empírica (`A2`).
5. **Testes realizados:** testes de pesos/normalização, 6 perfis nas 4 categorias, preservação de ofertas, hard constraints, explicabilidade, invariância a permutações e pipeline integrado desde `UserIntent` (`tests/test_ranking_weights_and_normalization.py`, `tests/test_reranker.py`, `tests/test_explainer.py`, `tests/test_ranking_integration_and_compliance.py`).
6. **Resultado da validação:** `RELATORIO_TESTES_ETAPA_6.md` registra **277 passed, 1 deselected, 0 failed** e benchmark sintético com `ndcg_at_3 = 1.0`, `mrr = 1.0`, `precision_at_3 = 0.6667`, `hard_constraint_compliance_rate = 1.0` e `offer_preservation_rate = 1.0` (ver §4 sobre a divergência `273` vs `277`).
7. **Limitações e pendências:** métricas obtidas em dataset sintético offline; calibração real de `A2` pendente.

### Etapa 7 — Orquestração do Fluxo com LangGraph
1. **Objetivo:** conectar as Etapas 0–6 por meio de `StateGraph` fino em memória, sem checkpointer e sem tracing.
2. **Principais implementações:** adição de `langgraph>=1.2,<2` (sem `langchain`); estado tipado `SearchGraphState`, `ExecutionStatus`, `OrchestrationError`, `OrchestrationMetrics` e `OrchestrationResult` (`state.py`); 6 nós puros (`prepare_intent_and_plan`, `search_products`, `process_semantics`, `validate_candidates`, `rank_candidates`, `finalize` em `nodes.py`); 4 roteadores condicionais (`routing.py`); `build_search_graph`, `SearchOrchestrator` stateless e guardrail `ensure_tracing_disabled()` (`graph.py`).
3. **Módulos relevantes:** `src/buscai/orchestration/*`, `pyproject.toml`, `requirements.txt`.
4. **Decisões técnicas:** compilação obrigatória com `checkpointer=None, store=None` e `config={"callbacks": []}`; proibição explícita de `tracing_v2_enabled` (cuja passagem de `False` posicional ativaria `LangChainTracer`) e bloqueio de variáveis `LANGSMITH_*`/`LANGCHAIN_TRACING*` por `TracingPolicyViolationError`; limpeza de referências transitórias ao retornar.
5. **Testes realizados:** 17 novos testes em `tests/test_orchestration_routing_and_nodes.py`, `tests/test_orchestration_graph.py`, `tests/test_orchestration_scenarios.py` (6 cenários sintéticos `T7.8`) e `tests/test_orchestration_security_and_compliance.py` (concorrência com `ThreadPoolExecutor`, `weakref` + `gc.collect()`, `sys.addaudithook`, AST e logs).
6. **Resultado da validação:** **294 passed, 1 deselected (`ebay_live`), 0 failed** (`RELATORIO_TESTES_ETAPA_7.md`).
7. **Limitações e pendências:** validação end-to-end contra serviços reais permanece opt-in/pendente; Etapa 8 não iniciada.

---

## 4. Consolidação dos Resultados de Testes

| Etapa | Fonte documental | Offline aprovados (`-m "not ebay_live"`) | Falhas | `ebay_live` |
| :--- | :--- | :---: | :---: | :---: |
| **Etapas 0–4** | Relatórios individuais removidos intencionalmente | *Não registrado nos relatórios restantes* | — | — |
| **Etapa 4.6.1** | [`RELATORIO_RECUPERACAO_AMBIENTE.md`](RELATORIO_RECUPERACAO_AMBIENTE.md) | `190 passed` | `0` | `1 deselected` |
| **Etapa 4.7** | [`RELATORIO_TESTES_ETAPA_4_7.md`](RELATORIO_TESTES_ETAPA_4_7.md) | `212 passed` | `0` | `1 deselected` |
| **Etapa 5** | [`RELATORIO_TESTES_ETAPA_5.md`](RELATORIO_TESTES_ETAPA_5.md) | `258 passed` | `0` | `1 deselected` |
| **Etapa 6** | [`RELATORIO_TESTES_ETAPA_6.md`](RELATORIO_TESTES_ETAPA_6.md) | `277 passed` *(ver nota abaixo)* | `0` | `1 deselected` |
| **Etapa 7** | [`RELATORIO_TESTES_ETAPA_7.md`](RELATORIO_TESTES_ETAPA_7.md) | `294 passed` | `0` | `1 deselected` |

> **Observação documental (273 vs. 277 na Etapa 6):** o enunciado de abertura da Etapa 7 e a mensagem inicial de entrega da Etapa 6 citaram `273` testes aprovados, enquanto o [`RELATORIO_TESTES_ETAPA_6.md`](RELATORIO_TESTES_ETAPA_6.md) consolidado após a expansão da Etapa 6 e a execução da Etapa 7 em [`RELATORIO_TESTES_ETAPA_7.md`](RELATORIO_TESTES_ETAPA_7.md) registram linha de base de `277` testes antes dos `17` testes da Etapa 7 (`277 + 17 = 294`). Ambos os valores ficam registrados por transparência documental.

---

## 5. Pendências Técnicas Atuais

- **Validação empírica do modelo E5 (`A2`):** executar `scripts/benchmark_embeddings.py` com `sentence-transformers` / `fastembed` sobre dataset rotulado real (`>= 100` pares de deduplicação e `>= 30` consultas PT-BR) para calibrar limiares e medir falso-positivo de fusão.
- **Avaliação do prompt de intenção no Gemini (`A11`):** rodar `scripts/evaluate_intent.py` (opt-in) e estender `src/userintent/evaluator.py` para pontuar `models`, `condition` e `currency`.
- **Concorrência OAuth sob HTTP 401 (`A9`):** `EbayOAuthClient` possui `threading.Lock` para leitura/renovação, mas chamadas concorrentes após `401` em `enrich_candidates` ainda podem disparar múltiplos `invalidate()` / `force_refresh=True` sem checagem de versão de token.
- **Ciclo de vida do modelo na camada HTTP (`A7`):** pré-carregamento do modelo de embeddings no `lifespan` do FastAPI, `HF_HUB_OFFLINE=1` e uso de `EmbeddingService.is_ready()` no health check (a implementar na Etapa 8).
- **Pendências operacionais e de qualidade da auditoria:** rotação de chaves eventualmente presentes em commits antigos de `.env` (`A12`); melhorias pontuais listadas na auditoria (`M1`–`M7`).
- **Ferramentas de análise estática e cobertura:** `ruff`, `mypy` e `pytest-cov` permanecem indisponíveis (`BLOQUEADO`) no `.venv` atual.

---

## 6. Próximas Etapas (`NÃO INICIADAS`)

- **Etapa 8 — `NÃO INICIADA`:** API HTTP B2B em FastAPI (`GET /v1/health`, `POST /v1/search`), schemas HTTP com `extra="forbid"` (`D8`), autenticação `X-API-Key` e rate limiting em memória (`D5`), preload no `lifespan` (`A7`).
- **Etapa 9 — `NÃO INICIADA`:** suíte de conformidade HTTP, testes de resiliência, benchmark E2E, validação controlada em ambiente real e documentação final da API.
- **Etapa 10 — `NÃO INICIADA` (Pós-MVP):** interação conversacional multi-turn (`POST /v1/chat`) com sessões em memória armazenando apenas histórico textual e `UserIntent` acumulado (`D4`).
