# Auditoria Técnica do Repositório — BuscaAI

> **Data da auditoria:** 19/11/2026  
> **Modo de execução:** Somente leitura (nenhum código, dependência ou configuração foi alterado).  
> **Referência:** [`PROJETO_BUSCAAI_ANTIGRAVITY.md`](PROJETO_BUSCAAI_ANTIGRAVITY.md)  
> **Nota de segurança:** Nenhum valor de `.env` ou segredo foi lido ou exposto. Os nomes de variáveis listados foram extraídos exclusivamente do código-fonte e de `.env.example`.

---

## Sumário Executivo

O repositório atual do **BuscaAI** corresponde a um **protótipo inicial em Python** evoluído a partir da "Etapa 1" (extração de intenção com Google Gemini + Pydantic), acrescido de:

- um fluxo linear de busca textual sobre catálogos (`DummyJsonCatalog` e `MercadoLivreCatalog`, sendo apenas o primeiro conectado à aplicação);
- um adaptador determinístico de restrições rígidas (`src/buscai/adapters/intent.py`);
- um protótipo visual em **Streamlit** (`src/buscai/app/`);
- um script solto e não rastreado pelo Git (`scripts/check_ebay_key.py`) testando OAuth e busca na eBay.

**Principais conclusões factuais:**

1. **Não existe API HTTP B2B** (FastAPI/Flask/endpoints `/v1/*`), autenticação por chave de API ou rate limiting.
2. **Não existe busca semântica, embeddings, validação por evidência com estado `desconhecido`, perfis de uso configuráveis, relaxamento de filtros, re-ranking multicritério, explicabilidade ou orquestração por grafo de estados.**
3. **Conformidade eBay em risco imediato caso a integração fosse ligada hoje:** há `print()` de debug que despejam objetos inteiros de produtos no `stdout` (`src/buscai/tools/products.py:104`, `src/buscai/app/service/agent_gateway.py:79`) e o script `scripts/check_ebay_key.py:35` imprime títulos e preços da eBay no terminal.
4. **Incompatibilidade estrutural de arquitetura:** `CatalogProvider.list_products()` + `search_products()` (`src/buscai/tools/products.py:102-128`) varrem **todas as páginas do catálogo** até `skip >= page.total`, o que é inviável para a eBay Browse API (milhares de itens, `total` dinâmico e cota diária).
5. **Incidente histórico de segurança no Git:** `.env` está hoje protegido pelo `.gitignore` e não rastreado, **porém foi commitado no histórico** (`commit 3b28fae`) e removido dois dias depois (`commit 90663f3`).
6. **Baseline de testes já possui 2 falhas:** na execução atual de `pytest`, **64 testes passam e 2 falham** (`tests/test_product_search.py` e `tests/test_schema.py`), além de 2 casos com gabarito inconsistente em `data/intent_eval_50.jsonl`.

---

## 1. Mapa do Repositório

### 1.1. Árvore de pastas e arquivos relevantes

```text
BuscaAi/
├── .env                              # Presente localmente (não rastreado; ignorado pelo .gitignore; NÃO lido)
├── .env.example                      # Modelo público de variáveis de ambiente (incompleto para eBay)
├── .gitignore                        # Protege .env, .venv, caches e data/predictions_*.jsonl
├── PROJETO_BUSCAAI_ANTIGRAVITY.md    # [Untracked no Git] Documento de diretrizes do projeto
├── README.md                         # Desatualizado (ainda documenta apenas "UserIntent — Etapa 1")
├── pyproject.toml                    # Metadados antigos ("userintent-project") + config do pytest
├── requirements.txt                  # 5 dependências declaradas (incompleto frente aos scripts)
├── data/
│   └── intent_eval_50.jsonl          # 50 casos sintéticos de avaliação de UserIntent em PT-BR
├── scripts/
│   ├── check_dummyjson_catalog.py    # CLI de smoke test DummyJSON
│   ├── check_ebay_key.py             # [Untracked no Git] Smoke test mínimo da eBay Browse API
│   ├── check_mercadolivre_catalog.py # CLI de consulta ao Mercado Livre (bug de ordem de import)
│   ├── demo_catalog_search.py        # CLI de busca textual manual sem LLM (DummyJSON)
│   ├── evaluate_intent.py            # Runner de avaliação sobre data/intent_eval_50.jsonl
│   ├── extract_intent.py             # CLI que extrai UserIntent de uma string via Gemini
│   ├── run_search_flow.py            # CLI texto -> Gemini -> DummyJSON
│   └── run_single_intent.py          # Script fixo de extração de um exemplo via Gemini
├── src/
│   ├── .streamlit/
│   │   └── config.toml               # Tema Web 1.0 e gatherUsageStats=false
│   ├── userintent/                   # Pacote da Etapa 1: extração e avaliação de intenção
│   │   ├── __init__.py
│   │   ├── schema.py                 # Pydantic: PriceConstraint, UserIntent
│   │   ├── prompt.py                 # SYSTEM_PROMPT em PT-BR
│   │   ├── llm.py                    # IntentExtractor (Google Gemini Structured Outputs)
│   │   └── evaluator.py              # Métricas e core_pass para avaliação de UserIntent
│   └── buscai/                       # Pacote principal de catálogo, busca e protótipo UI
│       ├── __init__.py
│       ├── catalog/
│       │   ├── __init__.py
│       │   ├── models.py             # Dataclasses congeladas: Product, ProductPage
│       │   ├── provider.py           # Protocolo CatalogProvider (list_products)
│       │   ├── dummyjson.py          # DummyJsonCatalog + CatalogError (usado globalmente!)
│       │   └── mercadolivre.py       # MercadoLivreCatalog (catálogo + detalhes paralelos)
│       ├── tools/
│       │   ├── __init__.py
│       │   └── products.py           # search_products(), normalize_text(), SearchResultPage
│       ├── adapters/
│       │   ├── __init__.py
│       │   └── intent.py             # adapt_intent(), search_intent(), validações rígidas
│       ├── agent/
│       │   ├── __init__.py
│       │   └── search_flow.py        # run_search_flow() (fluxo linear sem ranking/explicação)
│       └── app/                      # Protótipo front-end Streamlit (fora do escopo backend)
│           ├── app.py                # 3 telas: home, busca, resultados
│           ├── service/
│           │   └── agent_gateway.py  # Ponte Streamlit -> Mock ou DummyJSON+Gemini
│           └── ui/
│               ├── components.py     # HTML/SVG dos componentes Web 1.0
│               └── styles.py         # CSS global
└── tests/
    ├── test_catalog.py               # 8 testes (DummyJSON)
    ├── test_evaluator.py             # 1 teste (userintent.evaluator)
    ├── test_intent_adapter.py        # 13 testes (buscai.adapters.intent)
    ├── test_mercadolivre.py          # 16 testes (MercadoLivreCatalog)
    ├── test_product_search.py        # 17 testes (buscai.tools.products + demo_catalog_search)
    ├── test_schema.py                # 2 testes (userintent.schema)
    ├── test_search_flow.py           # 9 testes (buscai.agent.search_flow + run_search_flow CLI)
    └── test_search_product_no_filter_product.py  # Script solto (0 testes coletados pelo pytest)
```

### 1.2. Stack, dependências e versões encontradas

| Aspecto | Encontrado no repositório / `.venv` local | Observações |
| :--- | :--- | :--- |
| **Linguagem** | Python `>=3.11` (`pyproject.toml:9`); `.venv` local em **Python `3.14.7`** | `Python 3.14.7` exige atenção especial na instalação futura de `torch`/`sentence-transformers` (wheels). |
| **Gerenciador de pacotes** | `pip 26.2.1` + `requirements.txt` + `pyproject.toml` (`setuptools>=69`) | `pyproject.toml` não lista `dependencies`, não está instalado em modo editable (`pip list`) e ainda usa `name = "userintent-project"`. |
| **Framework Web / API** | **Nenhum framework de API REST** implementado | Apenas **Streamlit `1.66.1`** (`requirements.txt`: `streamlit>=1.40,<1.67`) para protótipo visual. `starlette 1.10.0` e `uvicorn 0.54.0` constam no `.venv` como transitivos, mas não há FastAPI nem rotas HTTP. |
| **Provedor e SDK de LLM** | Google Gemini via `google-genai 1.75.0` (`>=1.0,<2`) | Modelo padrão em código e `.env.example`: `gemini-3.5-flash-lite`. Emite `DeprecationWarning` sobre `_UnionGenericAlias` no Python 3.14. |
| **Validação de dados** | `pydantic 2.14.3` (`>=2.10,<3`) | Usado no `UserIntent` / `PriceConstraint`; o pacote `buscai` usa `@dataclass(frozen=True)`. |
| **Bibliotecas de IA / Busca Semântica / Orquestração** | **Nenhuma instalada ou declarada** | Não há `sentence-transformers`, `torch`, `transformers`, `langgraph`, `langchain`, `scikit-learn` ou cliente vetorial. Apenas `numpy 2.5.5` e `pandas 3.1.1` como transitivos do Streamlit. |
| **Cliente HTTP** | `urllib.request` (stdlib) em `dummyjson.py` e `mercadolivre.py`; `requests 2.34.2` em `scripts/check_ebay_key.py` | `requests` não está em `requirements.txt`. `httpx 0.28.1` e `tenacity 9.1.4` já existem no `.venv` como transitivos de `google-genai`. |
| **Configuração** | `python-dotenv 1.2.4` (`>=1.0,<2`) | `load_dotenv()` é invocado no nível de módulo em `llm.py` e `agent_gateway.py`. |
| **Testes** | `pytest 9.2.0` (`>=8,<10`) + `unittest` em `test_mercadolivre.py` | `pyproject.toml` configura `pythonpath = ["src"]` e `testpaths = ["tests"]`. |

### 1.3. Comandos atuais de execução e testes

- **Suíte de testes (PowerShell):** `.\.venv\Scripts\python.exe -m pytest -v`
- **Extração de intenção (exemplo fixo):** `.\.venv\Scripts\python.exe scripts/run_single_intent.py`
- **Extração de intenção (texto livre):** `$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe scripts/extract_intent.py "notebook até 4000"`
- **Avaliação dos 50 casos:** `$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe scripts/evaluate_intent.py`
- **Fluxo de busca CLI (DummyJSON):** `.\.venv\Scripts\python.exe scripts/run_search_flow.py "Quero um batom"`
- **Protótipo UI Streamlit:** executar a partir de `src/buscai/app/` com `$env:PYTHONPATH="C:\...\BuscaAi\src"; ..\..\..\.venv\Scripts\streamlit.exe run app.py` (falha se executado da raiz por imports relativos `from service...` e `from ui...`).

---

## 2. Estado Funcional

### 2.1. Implementado e funcional (com evidência no código/testes)

| Funcionalidade | Evidência no código | Estado verificado |
| :--- | :--- | :--- |
| **Contrato `UserIntent` e `PriceConstraint`** | `src/userintent/schema.py` | Valida faixa de preço (`max_price >= min_price`), normaliza/deduplica listas (`clean_list`). Ressalva: não rejeita campos extras (`extra="forbid"` ausente). |
| **Prompt de extração em PT-BR** | `src/userintent/prompt.py` | Regras claras para não inventar dados, diferenciar hard/soft price, evitar perguntas redundantes e usar structured output. |
| **Extrator via Gemini Structured Outputs** | `src/userintent/llm.py` (`IntentExtractor.extract`) | Usa `response_mime_type="application/json"` + `response_schema=UserIntent`, com fallback para `model_validate_json(response.text)`. |
| **Avaliador de extração de intenção + 50 casos** | `src/userintent/evaluator.py`, `data/intent_eval_50.jsonl`, `scripts/evaluate_intent.py` | Calcula `core_pass_rate`, acurácia de categoria/preço/clarificação, F1 por lista e Jaccard de query. Ressalva: 2 casos com gabarito incorreto (`case_id` 6 e 43). |
| **Modelos básicos de catálogo (`Product`, `ProductPage`, `CatalogProvider`)** | `src/buscai/catalog/models.py`, `provider.py` | Dataclasses imutáveis, preço em `Decimal`, moeda explícita opcional (`str \| None`). |
| **Integração DummyJSON** | `src/buscai/catalog/dummyjson.py` | Funcional e coberta por 8 testes unitários (`tests/test_catalog.py` passando). |
| **Integração Mercado Livre** | `src/buscai/catalog/mercadolivre.py` | Funcional em testes unitários (16 testes em `tests/test_mercadolivre.py` passando), mas **não integrada** ao fluxo principal nem à UI. |
| **Busca textual simples em memória** | `src/buscai/tools/products.py` | Normaliza acentos, separa dígitos de letras (`16GB` -> `16 gb`), verifica número por palavra exata (`8` não casa com `128`). |
| **Adaptador determinístico de intenção** | `src/buscai/adapters/intent.py` | Converte `UserIntent` em `IntentSearchCriteria`, aplica filtros rígidos (`category`, `price`, `excluded_brands`, `required_features`, `excluded_features`) e lista `unapplied_preferences`. |
| **Fluxo linear `run_search_flow`** | `src/buscai/agent/search_flow.py` | Extrai intenção; se `clarification_needed`, retorna pergunta sem consultar catálogo; senão chama `search_intent`. 9 testes passando. |
| **Protótipo UI Streamlit (Web 1.0)** | `src/buscai/app/app.py`, `ui/components.py`, `ui/styles.py`, `service/agent_gateway.py` | Telas `home`, `busca` e `resultados` com filtro local de preço e paginação na sessão. Fora do escopo desta fase backend. |

### 2.2. Parcialmente implementado ou com defeitos identificados

1. **Protótipo eBay (`scripts/check_ebay_key.py` — untracked):** faz uma chamada OAuth `client_credentials` + uma chamada `item_summary/search` fixa (`q="gaming laptop"`, `limit=5`, `EBAY_US`), sem timeout, sem retry, sem cache de token, sem tratamento de erros HTTP, usando `requests` (não declarado em `requirements.txt`), e **imprime títulos e preços reais no stdout** (risco de conformidade).
2. **Suíte de testes com 2 falhas pré-existentes:**
   - `tests/test_product_search.py::test_rejects_invalid_search_arguments[kwargs3]`: espera `ValueError` para `limit=51`, mas `src/buscai/tools/products.py:16` alterou `MAX_RESULTS_PER_PAGE = 200` (inclusive a docstring/help em `scripts/demo_catalog_search.py:25` e `scripts/run_search_flow.py:37` ainda diz `"1 a 50"`).
   - `tests/test_schema.py::test_extra_fields_are_rejected`: falha porque `UserIntent` não define `model_config = ConfigDict(extra="forbid")`. Além disso, `tests/test_schema.py:1` importa `from src.userintent.schema import UserIntent` em vez de `from userintent.schema import UserIntent`.
3. **Gabarito inconsistente em `data/intent_eval_50.jsonl`:** os casos `case_id=6` ("por menos de R$ 3.000") e `case_id=43` ("menos de 2500") registram `"min_inclusive": false, "max_inclusive": true` em vez de `"min_inclusive": true, "max_inclusive": false`, contradizendo a regra explícita de `src/userintent/prompt.py:9`.
4. **Suporte multi-turno rudimentar:** `src/buscai/app/service/agent_gateway.py:45` apenas concatena todas as falas do usuário com `"\n".join(...)` e reenvia ao `IntentExtractor`, descartando as perguntas anteriores feitas pelo assistente.
5. **Preferências flexíveis ignoradas:** `brands`, `preferred_features`, `use_cases`, `profile_context` e `price` flexível nunca são usados na busca nem no ranking — apenas devolvidos em `IntentSearchResult.unapplied_preferences`.
6. **Bug no modo mock do Streamlit:** `agent_gateway._mock` (`src/buscai/app/service/agent_gateway.py:57`) devolve um `dict` (`{"query": ...}`), que `app.py::search_from` (`src/buscai/app/app.py:127-130`) descarta como `None` por não possuir atributo `.results`.
7. **Inconsistência de nome de variável na documentação:** docstring de `agent_gateway.py:7-8` indica `BUSCAI_USE_MOCK`, mas o código (`linha 19`) e `.env.example:3` usam `BUSCAAI_USE_MOCK`.
8. **Bug de ordem de import em script CLI:** `scripts/check_mercadolivre_catalog.py:24-26` importa `buscai.*` antes de inserir `src/` no `sys.path` (`linhas 30-33`).

### 2.3. Não implementado

- Camada HTTP B2B (`POST /v1/search`, `POST /v1/chat`, `GET /v1/health`), OpenAPI, `request_id`, autenticação B2B (API keys) e rate limiting por consumidor.
- Abstração de busca orientada a query/filtros (`SearchBackend.search(query, filters) -> list[Candidate]`) e implementação produtiva `EbayLiveBackend` (OAuth com reuso em memória, `item_summary/search`, `getItem` seletivo, backoff/retry para `429`/`5xx`, normalização defensiva).
- Conversão cambial configurável BRL ↔ USD com metadados transparentes de estimativa.
- Geração/tradução de consultas de busca adequadas ao catálogo em inglês da eBay (`EBAY_US`).
- Deduplicação de candidatos.
- Embeddings multilíngues e reordenação semântica em memória.
- Perfis de uso configuráveis (ex.: jogos casuais, programação, edição de vídeo) e validador de requisitos estruturado (`atende` / `parcial` / `nao_atende` / `desconhecido` com evidência explícita).
- Relaxamento limitado e transparente de filtros quando há poucos ou zero candidatos.
- Re-ranking multicritério configurável com componentes de score expostos.
- Geração de explicações fiéis aos dados recuperados (anti-alucinação).
- Orquestração por grafo de estados (ex.: LangGraph).
- Logging estruturado sanitizado e testes automatizados de conformidade eBay (não persistência, não vazamento em logs, ausência de cache entre requisições).
- Benchmark sintético ponta a ponta (Precision@K, Recall@K, NDCG@K, MRR, taxa de afirmações sem evidência, latência e contagem de chamadas).

### 2.4. Não foi possível verificar nesta auditoria

- **Credenciais reais e conectividade externa:** conforme restrições, `.env` não foi lido e nenhuma chamada de rede à eBay ou ao Google Gemini foi executada.
- **Status efetivo da conta eBay de produção:** não foi possível verificar no portal da eBay se a isenção *"Not persisting eBay data"* (opt-out de Marketplace Account Deletion) já está aprovada/ativa, nem as cotas diárias vigentes (`item_summary/search`, `getItem`, Analytics `/rate_limit`).
- **Desempenho real do modelo `gemini-3.5-flash-lite`** nos 50 casos de `data/intent_eval_50.jsonl` (não há arquivo `data/predictions_*.jsonl` no repositório).
- **Alcance do histórico remoto no GitHub:** verificou-se a presença de `.env` no histórico local do `.git` (`commit 3b28fae`), mas não se inspecionou o repositório remoto.

---

## 3. Comparação Arquitetural (Código Atual vs. Arquitetura-Alvo da Seção 4)

| Componente (Seção 4) | O que existe hoje | Qualidade aparente | Arquivos / Símbolos relacionados | Recomendação inicial |
| :--- | :--- | :--- | :--- | :--- |
| **4.1 Fluxo lógico / Orquestração** | Fluxo linear de 2 passos (`extract` -> `search_intent`) com único desvio para clarificação. Sem grafo, sem relaxamento, sem validação por evidência, sem explicação. | Simples e bem testado para o escopo mínimo, mas insuficiente para o fluxo de 9 etapas. | `src/buscai/agent/search_flow.py` (`run_search_flow`, `SearchFlowResult`) | **Adaptar / Evoluir** para grafo de estados tipado (LangGraph, após aprovação) preservando compatibilidade de interface ou migrando chamadores. |
| **4.2 Contrato HTTP B2B** | **Inexistente.** Apenas UI Streamlit acoplada diretamente a funções Python via `agent_gateway.py`. | Não aplicável (não existe API). | Nenhum (apenas `src/buscai/app/service/agent_gateway.py` como referência de resposta `AgentReply`) | **Criar** camada FastAPI (`POST /v1/search`, `POST /v1/chat`, `GET /v1/health`, schemas Pydantic, auth B2B, rate limit, `request_id`). |
| **4.3 Abstração de fonte de busca** | `CatalogProvider.list_products(limit, skip) -> ProductPage` + `Product` minimalista (7 campos + `attributes: dict`). | Boa abstração para catálogo estático paginado, mas **inadequada para busca remota em marketplace** (não recebe `query` nem `filters` estruturados). | `src/buscai/catalog/provider.py`, `models.py`, `tools/products.py` | **Adaptar / Criar** protocolo `SearchBackend.search(query, filters)` e DTO rico `Candidate` (mantendo ou deprecando gradualmente `CatalogProvider` enquanto `DummyJsonCatalog` fica isolado em testes). |
| **4.4 Integração eBay + Câmbio** | Apenas script protótipo untracked (`check_ebay_key.py`). Sem cliente HTTP estruturado, sem OAuth manager, sem `getItem`, sem conversão BRL↔USD. | Protótipo descartável (sem tratamento de erros, imprime anúncios no stdout). | `scripts/check_ebay_key.py` | **Criar** `EbayOAuthClient`, `EbayLiveBackend` e serviço configurável de câmbio BRL↔USD; **substituir/higienizar** `check_ebay_key.py`. |
| **4.5a Entendimento de intenção** | `UserIntent`, `PriceConstraint`, `SYSTEM_PROMPT`, `IntentExtractor` (Gemini Structured Outputs), `evaluator.py`, 50 casos. | **Alta qualidade conceitual** (base mais madura do projeto). Precisa apenas de resiliência (timeout/retry), ajuste de `extra="forbid"`, suporte a contexto multi-turno e correção de 2 casos no dataset. | `src/userintent/schema.py`, `prompt.py`, `llm.py`, `evaluator.py`, `data/intent_eval_50.jsonl` | **Manter e Adaptar** incrementalmente (não reescrever do zero!). |
| **4.5b Geração de consultas eBay** | `UserIntent.query` em PT-BR usado direto em busca textual local (`_matches`). | Inadequado para `EBAY_US` (catálogo predominantemente em inglês). | `src/userintent/schema.py` (`query`), `src/buscai/adapters/intent.py` | **Criar** gerador de queries e filtros para eBay (tradução/expansão controlada PT-BR → EN-US + `filter=price:[...],priceCurrency:USD`). |
| **4.5c Validação determinística, perfis de uso e validador com evidências** | `adapters/intent.py` faz checagem rígida binária e lança `ConstraintUnavailableError` se qualquer campo faltar. Sem perfis de uso e sem estado `desconhecido`. | Rígido demais para anúncios heterogêneos da eBay; aborta busca inteira por 1 item sem marca/descrição. | `src/buscai/adapters/intent.py` (`_satisfies_constraints`, `_same_category`, `_matches_feature`) | **Adaptar / Criar**: preservar verificações determinísticas de orçamento/exclusões, introduzir perfis de uso configuráveis e validador por critério (`atende`/`parcial`/`nao_atende`/`desconhecido` + evidência). |
| **4.6 Deduplicação, Embeddings e Busca Semântica** | Apenas matching léxico por substrings/palavras (`tools/products.py::_matches`). Sem deduplicação nem embeddings. | Funcional só para termos exatos no mesmo idioma; falha em semântica e cross-lingual PT-BR ↔ EN-US. | `src/buscai/tools/products.py` (`normalize_text`, `_matches`) | **Criar** deduplicador em memória + motor de embeddings multilíngues em memória (`sentence-transformers`) com comparação reproduzível de modelos. |
| **4.1 (Passo 7) Relaxamento limitado** | Inexistente. Retorna 0 resultados quando filtros são restritivos. | Não implementado. | — | **Criar** política de relaxamento limitado e transparente (sem violar restrições rígidas silenciosamente; sempre reportar `relaxations`). |
| **4.7 Re-ranking multicritério** | Inexistente. Produtos retornam na ordem do catálogo. | Não implementado. | — | **Criar** ranker multicritério com pesos configuráveis, tratamento neutro para campos ausentes e breakdown de score na resposta. |
| **4.1 (Passo 9) Explicabilidade** | Inexistente (docstring de `search_flow.py:1` explicita: *"sem gerar recomendação"*). | Não implementado. | — | **Criar** gerador de explicações estritamente ancorado nas evidências validadas e atributos recuperados. |
| **4.8 Qualidade e Avaliação** | Existe apenas avaliação de `UserIntent` (`evaluator.py` + 50 casos). Não há avaliação de recuperação, ranking, explicabilidade ou latência. | Boa base para a etapa de intenção; ausente para busca/ranking/explicações. | `src/userintent/evaluator.py`, `scripts/evaluate_intent.py`, `data/intent_eval_50.jsonl` | **Manter** avaliação de intenção e **Criar** benchmark sintético ponta a ponta (sem dados reais da eBay). |
| **UI Streamlit existente** | App funcional Web 1.0 acoplado a `DummyJsonCatalog` em `agent_gateway.py`. | Bem estruturado visualmente, mas fora do escopo desta fase 100% backend e contém `print("DEBUG: ...")`. | `src/buscai/app/**`, `src/.streamlit/config.toml` | **Preservar isolado** (não expandir nesta fase; limpar `print` de debug e decidir com a equipe se será adaptado futuramente para consumir a API). |

---

## 4. Inventário de Integrações de Dados Atuais

### 4.1. DummyJSON (a remover do caminho de produção / isolar em testes)

- **Módulo principal:** `src/buscai/catalog/dummyjson.py` (`DummyJsonCatalog`, `CatalogError`, `PRODUCTS_URL = "https://dummyjson.com/products"`, `SOURCE = "dummyjson"`).
- **Acoplamento crítico oculto (`CatalogError`):** a exceção base `CatalogError` está definida em `dummyjson.py:20`, mas é importada por **7 arquivos externos**:
  - `src/buscai/catalog/mercadolivre.py:39`
  - `src/buscai/tools/products.py:12`
  - `src/buscai/app/service/agent_gateway.py:67`
  - `scripts/check_mercadolivre_catalog.py:24`
  - `scripts/demo_catalog_search.py:17`
  - `tests/test_mercadolivre.py:14`
  - `tests/test_product_search.py:10` e `tests/test_search_flow.py:12`
- **Uso ativo no caminho padrão / produção atual:**
  - `src/buscai/tools/products.py:97`: `provider = catalog if catalog is not None else DummyJsonCatalog()` (fallback padrão silencioso!).
  - `src/buscai/app/service/agent_gateway.py:22-29, 74-76`: instancia `DummyJsonCatalog(currency="BRL")` e `DUMMYJSON_CATEGORY_MAP` hardcoded.
  - `scripts/run_search_flow.py:20-26, 45`: usa `DummyJsonCatalog()` e `DUMMYJSON_CATEGORY_MAP` como padrão.
- **Scripts e testes relacionados:** `scripts/check_dummyjson_catalog.py`, `scripts/demo_catalog_search.py`, `scripts/run_search_flow.py`, `tests/test_catalog.py`, `tests/test_search_product_no_filter_product.py`.
- **Variáveis de ambiente:** nenhuma.
- **Dependências:** stdlib (`urllib.request`, `json`, `decimal`).

### 4.2. Mercado Livre (a remover completamente do produto final)

- **Módulo principal:** `src/buscai/catalog/mercadolivre.py` (`MercadoLivreCatalog`, `API_URL = "https://api.mercadolibre.com"`, `SOURCE = "mercadolivre"`, `TOKEN_ENV_VAR = "MERCADOLIVRE_ACCESS_TOKEN"`).
- **Onde é usado:** **não está conectado** a `run_search_flow`, `search_intent` nem `agent_gateway`. É referenciado apenas em:
  - `scripts/check_mercadolivre_catalog.py`
  - `tests/test_mercadolivre.py`
  - `.env.example:5` (`MERCADOLIVRE_ACCESS_TOKEN=INSERT_TOKEN_HERE`)
- **Dependências:** stdlib (`urllib.request`, `concurrent.futures.ThreadPoolExecutor`) + `python-dotenv` no script.
- **Padrões úteis a reaproveitar conceitualmente (antes da remoção):** o uso de `Decimal` no parse JSON (`parse_float=Decimal`), a estrutura de enriquecimento paralelo com `ThreadPoolExecutor` com `max_workers` limitado, e a validação defensiva de tipos/campos opcionais no mapeamento de DTO.

### 4.3. eBay (embrião atual — ainda não integrado)

- **Arquivo atual:** `scripts/check_ebay_key.py` (untracked no Git).
- **Endpoints usados no protótipo:**
  - `POST https://api.ebay.com/identity/v1/oauth2/token` (`grant_type=client_credentials`, `scope=https://api.ebay.com/oauth/api_scope`)
  - `GET https://api.ebay.com/buy/browse/v1/item_summary/search` (`X-EBAY-C-MARKETPLACE-ID: EBAY_US`)
- **Variáveis de ambiente:** `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` (ambas **ausentes** em `.env.example`).
- **Dependências:** `requests` (presente no `.venv`, ausente em `requirements.txt`), `python-dotenv`.
- **Notas técnicas verificadas na documentação oficial da eBay (a confirmar na conta real):**
  - A geração de token OAuth (`/identity/v1/oauth2/token`) possui limite de **1.000 requisições por 24 horas** por aplicação (torna obrigatório o reuso do token em memória até perto da expiração, tipicamente ~2h).
  - `item_summary/search` aceita `filter=price:[min..max],priceCurrency:USD`, `limit` e `offset`, retornando resumos leves (`itemSummaries` com `itemId`, `title`, `price`, `condition`, `seller`, `shippingOptions`, etc.).
  - Atributos granulares (`localizedAspects`) e descrição completa geralmente exigem `GET /buy/browse/v1/item/{item_id}` (`getItem`), que consome chamadas adicionais — logo deve ser seletivo (top-K) e controlado.
  - A cota/rate limit da conta pode ser consultada pela Developer Analytics API (`GET /rate_limit`).

### 4.4. Google Gemini (LLM)

- **Módulo principal:** `src/userintent/llm.py` (`IntentExtractor`).
- **Onde é usado:** `src/buscai/agent/search_flow.py:42`, `src/buscai/app/service/agent_gateway.py:68`, `scripts/extract_intent.py`, `scripts/run_single_intent.py`, `scripts/evaluate_intent.py`.
- **Variáveis de ambiente:** `GEMINI_API_KEY`, `GEMINI_MODEL` (default `"gemini-3.5-flash-lite"`).
- **Dependências:** `google-genai>=1.0,<2`, `python-dotenv`, `pydantic`.

---

## 5. Auditoria de Persistência e Conformidade eBay (Seção 3)

Abaixo está a verificação dos 10 vetores obrigatórios de persistência direta ou indireta de dados de anúncios da eBay (e dados derivados), sob a abordagem conservadora da isenção **"Not persisting eBay data"**:

| Vetor auditado | Situação encontrada no repositório | Risco de conformidade | Ação requerida no plano |
| :--- | :--- | :--- | :--- |
| **1. Banco de dados e ORM** | Inexistente (sem SQLite, PostgreSQL, SQLAlchemy, etc.). | **Baixo (hoje)** | Manter sem persistência de anúncios ou vetores derivados nesta fase. |
| **2. Cache em memória e em disco** | (a) `MercadoLivreCatalog._category_names` (`mercadolivre.py:107`) faz cache de categorias em memória por instância; (b) `st.session_state.search_result` (`app.py:40, 105`) guarda resultados de produtos em memória na sessão Streamlit entre interações. | **Médio/Alto** se replicado para eBay | Proibir cache de anúncios/respostas/embeddings entre requisições. Permitir em memória apenas o **token OAuth** (com TTL) e os **pesos do modelo de embedding** (que não contêm dados de anúncios). |
| **3. Bibliotecas HTTP e cache HTTP** | `urllib.request` e `requests` não possuem cache HTTP habilitado por padrão. `httpx` (no `.venv`) também não habilita cache por padrão. | **Baixo** | Ao adotar `httpx`, não acoplar plugins de cache HTTP (`hishel`, `requests-cache`) nem proxies de cache. |
| **4. Logs estruturados e `stdout` (`print`)** | **PONTO CRÍTICO ENCONTRADO:** não há `logging` configurado, mas existem `print()` despejando produtos inteiros no `stdout`:<br>- `src/buscai/tools/products.py:104` (`print(f'\nDEBUG: Página de produtos: {page}')`)<br>- `src/buscai/app/service/agent_gateway.py:79` (`print(f"DEBUG: resultado = {result}")`)<br>- `src/buscai/app/service/agent_gateway.py:94` (`print(f"Erro: {ex}")`)<br>- `scripts/check_ebay_key.py:35` (`print(it["title"], it["price"])`) | **CRÍTICO** (stdout coletado em logs de servidor/container persiste payloads de anúncios!) | Remover imediatamente todos os `print()` de debug com payloads; criar logger estruturado restrito a metadados (`request_id`, status HTTP, duração, contagem de itens); adicionar teste automatizado verificando ausência de títulos/preços/URLs nos logs. |
| **5. Tracing e monitoramento de erros** | Nenhum SDK (Sentry, OpenTelemetry, LangSmith) instalado hoje. | **Baixo (hoje) / Alto (ao adotar LangGraph ou FastAPI)** | Ao introduzir `langgraph`, forçar tracing desabilitado (`LANGCHAIN_TRACING_V2=false`, `LANGSMITH_TRACING=false`). No FastAPI, garantir `debug=False` e desativar exposição de stacktraces/locals. |
| **6. Arquivos temporários e escritas em disco** | Único ponto de escrita em disco: `scripts/evaluate_intent.py:19-23` grava `data/predictions_gemini.jsonl` (contém apenas texto sintético do usuário + `UserIntent`, sem dados de anúncios). `check_mercadolivre_catalog.py:9` sugere `--json > resultado.json`. | **Baixo (hoje)** | Proibir flags de dump JSON para eBay; adicionar teste de conformidade monitorando escritas em disco durante o fluxo de busca. |
| **7. Dumps de exceções e variáveis locais** | Sem handler global de erros; exceções propagam objetos Python. Em servidores web ou `pytest --showlocals`, frames locais podem capturar `Candidate`/`Product`. | **Médio** | Implementar exception handlers padronizados na API que sanitizam mensagens e nunca serializam objetos de anúncio em logs de erro. |
| **8. Notebooks e ferramentas dev** | Nenhum `.ipynb` encontrado. `src/.streamlit/config.toml:14` define `gatherUsageStats = false`. | **Baixo** | Manter política de não versionar notebooks com saídas reais da eBay nem salvar fixtures reais. |
| **9. Filas, sessões e estado** | Sem Redis/Celery/filas. Apenas `st.session_state` no Streamlit (`app.py`). | **Médio (em `POST /v1/chat`)** | No futuro `POST /v1/chat`, sessões em memória (com TTL) devem guardar **somente** histórico de mensagens do usuário e estado de intenção/critérios — **jamais** candidatos, embeddings ou resultados da eBay. Não usar checkpointer persistente do LangGraph para estado contendo candidatos. |
| **10. Relatórios e ferramentas de avaliação** | Apenas `scripts/evaluate_intent.py` + `data/intent_eval_50.jsonl` (100% sintético). | **Baixo** | Toda avaliação futura de busca/ranking/explicações deve rodar exclusivamente sobre fixtures sintéticas versionadas, nunca sobre capturas reais da eBay. |

---

## 6. Auditoria de Segurança

### 6.1. Incidente histórico crítico: `.env` no histórico do Git

Por inspeção de metadados do Git (`git log --all --name-status --oneline -- .env`, sem leitura de conteúdo/diff):

- **`commit 3b28fae` (23/09/2026 — "fix llm model and userIntent struct"):** adicionou `.env` (`A .env`) e arquivos `__pycache__/*.pyc` ao repositório.
- **`commit 90663f3` (25/09/2026 — "feat: adiciona catálogo DummyJSON e fluxo de busca por intenção"):** removeu `.env` (`D .env`) e criou o `.gitignore`.
- **Estado atual do working tree:** `.env` **não está rastreado** (`git ls-files .env` vazio) e está protegido por `.gitignore:8-10` (`.env`, `.env.*`, `!.env.example`).

> **Ação humana urgente:** qualquer credencial que existia em `.env` na data do commit `3b28fae` (por exemplo, `GEMINI_API_KEY`) deve ser considerada **comprometida** e **rotacionada imediatamente**. A equipe deve decidir se realizará limpeza do histórico Git (`git filter-repo`) no repositório remoto.

### 6.2. Inventário de variáveis de ambiente (apenas nomes e locais)

| Variável | Em `.env.example`? | Locais de uso no código | Situação |
| :--- | :---: | :--- | :--- |
| `GEMINI_API_KEY` | Sim (`linha 1`) | `src/userintent/llm.py:25`, `tests/test_product_search.py:155`, `tests/test_search_flow.py:145,161` | Manter; centralizar leitura em `config.py`. |
| `GEMINI_MODEL` | Sim (`linha 2`, default `gemini-3.5-flash-lite`) | `src/userintent/llm.py:24` | Manter; centralizar em `config.py`. |
| `BUSCAAI_USE_MOCK` | Sim (`linha 3`, `0`) | `src/buscai/app/service/agent_gateway.py:19` (docstring diz `BUSCAI_USE_MOCK`) | Específica do protótipo Streamlit; corrigir docstring se mantido; **nunca** permitir mock silencioso na API de produção. |
| `MERCADOLIVRE_ACCESS_TOKEN` | Sim (`linha 5`) | `src/buscai/catalog/mercadolivre.py:45`, `scripts/check_mercadolivre_catalog.py:36`, `tests/test_mercadolivre.py:240,242` | **Planejar remoção** junto com a integração Mercado Livre. |
| `EBAY_CLIENT_ID` | **Não** | `scripts/check_ebay_key.py:5` | **Adicionar** em `.env.example` (sem valor) e em `config.py`. |
| `EBAY_CLIENT_SECRET` | **Não** | `scripts/check_ebay_key.py:6` | **Adicionar** em `.env.example` (sem valor) e em `config.py`. |

### 6.3. Outras lacunas de segurança identificadas

1. **Efeito colateral no import:** `load_dotenv()` é executado no import de `src/userintent/llm.py:13` e `src/buscai/app/service/agent_gateway.py:18`, dificultando isolamento de testes e controle explícito de configuração.
2. **Sem autenticação nem proteção B2B:** não há API keys, rate limiting, limites de tamanho de payload (`query` sem `max_length` em `UserIntent` ou entrada HTTP) nem timeout nas chamadas ao Gemini (`IntentExtractor.extract`) e no protótipo eBay (`check_ebay_key.py`).
3. **HTML inline na UI Streamlit:** `src/buscai/app/app.py:193` usa `st.markdown(f'<div class="form-error">{ss.filter_error}</div>', unsafe_allow_html=True)` sem `html.escape` (hoje usa apenas strings estáticas; baixo risco, fora do escopo backend).
4. **Sem exposição de credenciais hardcoded** no código-fonte rastreado nem em `scripts/check_ebay_key.py` (confirmado por inspeção integral de todos os arquivos).

---

## 7. Inventário e Diagnóstico de Testes

Execução verificada em modo somente leitura (`PYTHONDONTWRITEBYTECODE=1 .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -v`):  
**Resultado atual: 66 coletados — 64 passaram, 2 falharam, 1 warning (1.35s).**

| Arquivo de teste | Itens | Resultado atual | Escopo coberto | Impacto na migração |
| :--- | :---: | :---: | :--- | :--- |
| `tests/test_catalog.py` | 8 | 8 PASSED | `DummyJsonCatalog` (normalização, paginação, erros HTTP/JSON, moeda) | Manter isolado caso `DummyJsonCatalog` permaneça como fixture offline; atualizar import de `CatalogError`. |
| `tests/test_evaluator.py` | 1 | 1 PASSED | `userintent.evaluator.evaluate_case` (apenas 1 caso feliz) | Manter e ampliar (testar casos de falha, `max_inclusive`, `clarification`). |
| `tests/test_intent_adapter.py` | 13 | 13 PASSED | `adapt_intent`, `search_intent`, restrições rígidas, `ConstraintUnavailableError`, `ClarificationRequired` | **Alto impacto:** os testes que exigem `ConstraintUnavailableError` para campos ausentes conflitam com a nova validação por evidência (`desconhecido`) exigida pela eBay. |
| `tests/test_mercadolivre.py` | 16 | 16 PASSED | `MercadoLivreCatalog` (mapeamento, filtros, erros, HTTP, cache de categorias) | **Será removido** quando a integração Mercado Livre for removida. |
| `tests/test_product_search.py` | 17 | **16 PASSED / 1 FAILED** | `search_products`, `normalize_text`, paginação total, `demo_catalog_search` | **Falha pré-existente:** `limit=51` não lança `ValueError` porque `MAX_RESULTS_PER_PAGE=200`. **Alto impacto:** o modelo de varredura exaustiva de catálogo não se aplica à eBay. |
| `tests/test_schema.py` | 2 | **1 PASSED / 1 FAILED** | `UserIntent` (formato válido e rejeição de campos extras) | **Falha pré-existente:** `UserIntent` não configura `extra="forbid"`, e o import usa `src.userintent.schema`. |
| `tests/test_search_flow.py` | 9 | 9 PASSED | `run_search_flow` + CLI `scripts/run_search_flow.py` | **Médio impacto:** acoplado ao `CatalogProvider` antigo e `IntentSearchResult`; precisará ser mantido como regressão ou evoluído com a nova orquestração. |
| `tests/test_search_product_no_filter_product.py` | 0 | Não coletado | Script manual que faz chamada **real de rede** ao DummyJSON se executado via `python` | **Remover ou mover** para `scripts/` (não é um teste automatizado). |

### Principais lacunas de cobertura

- `src/userintent/llm.py` (`IntentExtractor`): **0% coberto** (nem com SDK mockado).
- `src/buscai/app/**`: **0% coberto**.
- Sem testes de contrato HTTP, autenticação B2B, rate limiting, cliente eBay, conversão cambial, embeddings/similaridade, validador por evidência, re-ranking, explicabilidade, resiliência (`429`/`5xx`/timeout) ou conformidade (logs, cache, disco).

---

## 8. Execução Local — Diagnóstico e Problemas Encontrados

### Como configurar e executar hoje (estado real)

1. Ambiente virtual existente em `.venv/` usando **Python 3.14.7**.
2. Instalar dependências declaradas: `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` (mas notar lacunas abaixo).
3. Configurar `.env` a partir de `.env.example` (não exibir valores).
4. Rodar testes: `.\.venv\Scripts\python.exe -m pytest` (usa `pythonpath = ["src"]` do `pyproject.toml`).

### Problemas reais encontrados na configuração local

1. **`README.md` obsoleto e com erros:** ainda documenta apenas a Etapa 1 (`userintent_project`), contém typo (`source source path/to/venv/bin/activate` na linha 10), referencia `scripts/test_single_intent.py` (o arquivo real é `scripts/run_single_intent.py`), e não menciona `buscai`, Streamlit, Mercado Livre ou eBay.
2. **`requirements.txt` incompleto e `pyproject.toml` desatualizado:** `requests` (usado por `scripts/check_ebay_key.py`) não está listado em `requirements.txt`; `pyproject.toml` não declara `dependencies` e mantém `name = "userintent-project"`.
3. **Execução de scripts CLI inconsistente:** alguns scripts inserem `src/` no `sys.path` (`run_single_intent.py`, `run_search_flow.py`, `demo_catalog_search.py`, `check_dummyjson_catalog.py`), outros exigem `PYTHONPATH=src` (`extract_intent.py`, `evaluate_intent.py`), e `check_mercadolivre_catalog.py` importa `buscai` antes de alterar `sys.path`.
4. **Execução do Streamlit frágil:** `src/buscai/app/app.py` importa `from service.agent_gateway` e `from ui import components`, exigindo `cwd = src/buscai/app` + `PYTHONPATH` apontando para `src/`.
5. **Suíte de testes quebrada no HEAD atual:** `pytest` termina com código de saída `1` devido às 2 falhas descritas na seção 7.
6. **Compatibilidade Python 3.14.7:** embora FastAPI, LangGraph e PyTorch 2.9+/sentence-transformers possuam suporte/wheels para Python 3.14 em 2026, a instalação local de `torch`/`sentence-transformers` em Windows + Python 3.14.7 precisará ser validada na prática (CPU-only recomendada para ambiente acadêmico leve), e `google-genai 1.75.0` já emite warning de depreciação no Python 3.14.

---

## 9. Riscos, Regressões e Decisões Questionáveis

| # | Risco / Problema encontrado | Severidade | Origem no código / Contexto | Impacto e Mitigação proposta |
| :-: | :--- | :---: | :--- | :--- |
| **R1** | **Exposição histórica de `.env` no Git** | **Crítica** | `commit 3b28fae` (`A .env`) | Possível vazamento de `GEMINI_API_KEY` ou outras chaves antigas. Rotacionar chaves imediatamente e avaliar limpeza de histórico. |
| **R2** | **Vazamento de payloads de produtos em `stdout`/logs** | **Crítica (Conformidade)** | `products.py:104`, `agent_gateway.py:79`, `check_ebay_key.py:35` | Violação direta da política conservadora da seção 3 (não registrar títulos/preços/corpos). Remover todos os `print` de debug e blindar logging com testes automatizados. |
| **R3** | **Varredura exaustiva de catálogo (`list_products`) incompatível com eBay** | **Crítica (Arquitetura)** | `src/buscai/tools/products.py:100-128`, `catalog/provider.py` | Loop `while skip < page.total` esgotaria cota diária em 1 busca e falharia com `total` dinâmico. Criar abstração `SearchBackend.search(query, filters)` com paginação estritamente limitada. |
| **R4** | **Abortar busca inteira quando falta atributo (`ConstraintUnavailableError`)** | **Alta** | `src/buscai/adapters/intent.py:100-164` | Anúncios reais da eBay frequentemente omitem marca, descrição ou specs no resumo. Substituir por validador por candidato que marca ausência como `desconhecido`. |
| **R5** | **Acoplamento oculto de `CatalogError` em `dummyjson.py`** | **Média (Regressão)** | `src/buscai/catalog/dummyjson.py:20` + 7 importadores | Remover/mover `dummyjson.py` quebraria imports em vários módulos e testes. Extrair `CatalogError` para módulo neutro antes de isolar DummyJSON. |
| **R6** | **Cota eBay OAuth (1.000 tokens/24h) e explosão de chamadas `getItem`** | **Alta** | Documentação oficial eBay + ausência de gestão de token/orçamento | Pedir token por requisição (como em `check_ebay_key.py`) esgota cota rapidamente; chamar `getItem` para 50+ itens gera latência e 429. Reusar token em memória até perto do vencimento e restringir `getItem` ao top-K com concorrência limitada. |
| **R7** | **Desalinhamento idioma/moeda (PT-BR / BRL vs EN-US / USD)** | **Alta** | `prompt.py` gera `query` em PT-BR; `EBAY_US` é majoritariamente EN-US/USD | Buscar em PT-BR no `EBAY_US` derruba recall; comparar BRL direto com USD invalida filtro de preço. Implementar gerador de queries em inglês, embeddings multilíngues e conversão cambial explícita. |
| **R8** | **Persistência acidental via LangGraph / FastAPI / sessões de chat** | **Alta (Conformidade)** | Futura adoção de `langgraph`, `POST /v1/chat` e FastAPI | Tracing LangSmith, checkpointers persistentes, `debug=True` ou guardar anúncios na sessão violam a seção 3. Desativar tracing, nunca salvar anúncios em sessão/disco e cobrir com testes de conformidade. |
| **R9** | **Quebra do protótipo Streamlit durante evolução do backend** | **Média (Regressão)** | `src/buscai/app/service/agent_gateway.py` acoplado a `DummyJsonCatalog` e `search_intent` | Alterar assinaturas de `search_intent` ou remover `DummyJsonCatalog` sem cuidado quebra a UI existente. Preservar contratos legados ou desacoplar `agent_gateway.py` explicitamente conforme decisão da equipe. |
| **R10** | **Chamadas ao Gemini sem timeout, retry ou fallback estruturado** | **Média** | `src/userintent/llm.py:29-37` | Falha transitória ou `503` de capacidade do Gemini trava ou derruba requisições HTTP. Adicionar timeout, retry limitado e tratamento de erro padronizado (HTTP `503`). |
| **R11** | **Overhead/compatibilidade de modelo de embeddings local em Python 3.14.7** | **Média** | `.venv` em Python `3.14.7`, Windows | `torch` + modelos grandes aumentam RAM, tempo de startup e latência em CPU. Validar wheels Python 3.14, usar PyTorch CPU e comparar modelos compactos (`multilingual-e5-small` vs `paraphrase-multilingual-MiniLM-L12-v2`). |
| **R12** | **Baseline de testes e benchmark com inconsistências** | **Baixa/Média** | `test_schema.py`, `test_product_search.py`, `intent_eval_50.jsonl` (casos 6 e 43) | Falsos negativos/positivos mascaram regressões reais. Corrigir na Etapa 0 antes de novas features. |

---

## 10. Perguntas para a Equipe (Dependem de Decisão Humana)

1. **Conformidade e acesso de produção na eBay (BLOQUEIO EXTERNO):**  
   *Contexto:* O planejamento depende da API real de produção da eBay sob a isenção *"Not persisting eBay data"*.  
   *Pergunta:* A isenção (opt-out de Marketplace Account Deletion) já está confirmada/ativa no portal da eBay para as credenciais de produção, e a conta tem permissão confirmada para `item_summary/search` e `getItem` no `EBAY_US`? Qual é a cota diária atual?  
   *Impacto:* Define se podemos executar validação integrada real e qual orçamento máximo de chamadas (`search` + `getItem`) por requisição devemos configurar.

2. **Segurança — Rotação de chaves do commit `3b28fae`:**  
   *Contexto:* `.env` foi commitado em `3b28fae` e deletado em `90663f3`, permanecendo no histórico Git.  
   *Pergunta:* As credenciais presentes naquele commit já foram revogadas/rotacionadas? A equipe quer reescrever o histórico Git do repositório remoto após a auditoria?  
   *Impacto:* Segurança imediata das chaves de API (Gemini e quaisquer outras que estivessem no arquivo).

3. **Categorias prioritárias e perfis de uso do MVP:**  
   *Contexto:* Perfis de uso (ex.: jogos casuais, programação, edição de vídeo) precisam de regras técnicas configuráveis e revisáveis pela equipe (seção 4.5).  
   *Pergunta:* Quais categorias de produto entram no escopo obrigatório da primeira entrega do backend (ex.: notebooks, smartphones, monitores, fones de ouvido — ou um subconjunto menor)?  
   *Impacto:* Define os perfis configuráveis, o mapeamento de categorias da eBay e o conjunto de avaliação sintético.

4. **Escopo de endpoints na primeira entrega (`POST /v1/chat`):**  
   *Contexto:* A seção 4.2 lista `POST /v1/search`, `POST /v1/health` e `POST /v1/chat` (*"caso o projeto realmente necessite de estado conversacional"*).  
   *Pergunta:* `POST /v1/chat` (com sessões em memória contendo apenas intenção/mensagens do usuário, sem persistência entre instâncias) é obrigatório nesta fase ou pode ficar como segunda entrega logo após `POST /v1/search`?  
   *Impacto:* Escopo da Etapa 7 e Etapa 8 (gerenciamento de sessão com TTL e testes multi-turno).

5. **Autenticação B2B e limites de uso no MVP:**  
   *Contexto:* A direção desejada é API B2B com autenticação e limites compatíveis com fase acadêmica/MVP.  
   *Pergunta:* Aprovação para autenticação via header `X-API-Key` (chaves configuradas por variável de ambiente, sem banco de dados) + rate limiting em memória por chave (ex.: N req/min e M req/dia)?  
   *Impacto:* Define o desenho da camada de segurança da Etapa 8 sem introduzir banco de dados desnecessário.

6. **Mecanismo de conversão cambial BRL ↔ USD:**  
   *Contexto:* A seção 4.4 sugere taxa fixa via variável de ambiente como primeira versão, identificada como estimativa transparente.  
   *Pergunta:* Confirmam começar com taxa fixa configurável em ambiente (`USD_BRL_RATE` + `USD_BRL_RATE_UPDATED_AT`) nesta primeira entrega, deixando cotação automática externa como evolução futura?  
   *Impacto:* Simplicidade, zero nova dependência externa e previsibilidade nos testes.

7. **Orquestração: LangGraph vs. grafo de estados nativo em Python:**  
   *Contexto:* A seção 2 indica preferência por LangGraph se adequado à stack. O repositório hoje não tem `langgraph` instalado.  
   *Pergunta:* Confirmam a adoção de `langgraph` (com tracing explicitamente desligado e sem checkpointer de anúncios), ou preferem uma máquina de estados tipada leve em Python puro (menor árvore de dependências)?  
   *Impacto:* Dependências do projeto e arquitetura da Etapa 7.

8. **Política de `extra="forbid"` no `UserIntent` (`src/userintent/schema.py`):**  
   *Contexto:* `tests/test_schema.py` testa `extra="forbid"`, que hoje falha porque `UserIntent` não o configura (alteração ocorrida no commit `3b28fae`).  
   *Pergunta:* A remoção de `extra="forbid"` foi intencional por limitação do schema JSON do Gemini (`additionalProperties`), ou pode ser restaurada em `UserIntent` (ou apenas na validação pós-LLM)?  
   *Impacto:* Define como corrigir `tests/test_schema.py` sem quebrar `IntentExtractor` no Gemini real.

9. **Destino do protótipo Streamlit (`src/buscai/app/`) e do `DummyJsonCatalog`:**  
   *Contexto:* O escopo desta fase é 100% backend; o Streamlit atual usa `DummyJsonCatalog` diretamente.  
   *Pergunta:* (a) Confirmam manter `DummyJsonCatalog` isolado apenas para testes offline (fora do caminho de produção)? (b) O diretório `src/buscai/app/` deve ser preservado como protótipo legado (sem investir esforço de UI agora, apenas evitando quebrá-lo desnecessariamente)?  
   *Impacto:* Estratégia de desacoplamento na Etapa 1 e preservação de código existente.

---

## 11. Recomendações Técnicas

### Fatos encontrados vs. Recomendações

- **Fatos que justificam reaproveitamento (não reescrever):**
  - O pacote `src/userintent/` (`schema.py`, `prompt.py`, `llm.py`, `evaluator.py`) e o dataset `data/intent_eval_50.jsonl` são sólidos, legíveis e aderentes ao domínio.
  - A cultura de tipagem com `Decimal` para dinheiro, preservação explícita de moeda desconhecida (`None`), separação de módulos (`catalog`, `adapters`, `agent`, `tools`) e testes unitários sem rede em `tests/` é excelente e deve ser mantida.
- **Recomendações técnicas para a transformação do backend:**
  1. **Adotar FastAPI** para a camada HTTP B2B: `pydantic 2.14.3`, `starlette 1.10.0` e `uvicorn 0.54.0` já existem no ambiente, garantindo OpenAPI automático e integração nativa com os schemas Pydantic já usados.
  2. **Adotar `httpx` + `tenacity`** para o cliente eBay: ambos já estão presentes no `.venv` (como dependências transitivas de `google-genai`), evitam adicionar clientes redundantes, oferecem timeouts granulares, pool de conexões e `httpx.MockTransport` para testes determinísticos sem rede.
  3. **Criar uma nova abstração `SearchBackend`** em vez de forçar a eBay no `CatalogProvider.list_products()` antigo: a eBay exige busca por `q` + `filter` + paginação controlada, e tentar adaptá-la ao loop exaustivo de `search_products()` causaria falhas e consumo abusivo de cota.
  4. **Estratégia em dois estágios para a Browse API (`search` -> ranking semântico -> `getItem` seletivo):** usar `item_summary/search` para recuperar candidatos iniciais (ex.: 30–50 itens em 1 chamada), deduplicar e reordenar semanticamente em memória, e acionar `getItem` apenas para o top-K (ex.: 5–10 candidatos) quando forem necessários `localizedAspects`/atributos detalhados para validar requisitos técnicos — mantendo latência e consumo de cota estritamente sob controle.
  5. **Explicabilidade determinística/híbrida com guarda de evidência:** gerar justificativas preferencialmente a partir das evidências estruturadas produzidas pelo validador (ou com pós-validação estrita sobre qualquer texto de LLM), garantindo fidelidade aos atributos recuperados e taxa próxima de zero de afirmações sem evidência.
  6. **Validar embeddings multilíngues com experimento reproduzível:** comparar ao menos dois modelos leves de `sentence-transformers` (ex.: `intfloat/multilingual-e5-small` vs. `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`) em CPU com casos sintéticos PT-BR ↔ EN-US antes de fixar o modelo padrão.
  7. **Atacar primeiro higiene, segurança e conformidade (Etapa 0):** corrigir os `print()` de debug, os 2 testes quebrados, o acoplamento de `CatalogError` e o módulo de configuração/logging antes de introduzir código novo.
