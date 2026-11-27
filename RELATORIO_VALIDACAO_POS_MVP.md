# Relatório de Validação Pós-MVP — Etapa 11 (BUSCAAI)

- **Data:** 25/11/2026
- **Branch Git:** `waldeney` (commit base `ed58065225485a67c391ca8990e05ea4f91b8e0c`, sem commits, push ou alteração de histórico)
- **Escopo:** Validação empírica controlada pós-MVP (E5 real, Gemini real, eBay real e E2E), saneamento de qualidade (`ruff`/`mypy`/warnings), auditoria final de segurança e plano técnico operacional de implantação.
- **Princípio de classificação:** Nenhuma métrica sintética ou de stub é apresentada como desempenho real; nenhum resultado foi inventado; chamadas externas obedeceram estritamente à política opt-in e aos tetos aprovados.

---

## Quadro de Classificação dos Itens da Etapa 11

| Item | Escopo | Classificação | Resumo objetivo |
| :--- | :--- | :---: | :--- |
| **T11.1** | Avaliação real `intfloat/multilingual-e5-small` | **BLOQUEADO** | Pesos ausentes no cache local e backends (`sentence-transformers`, `fastembed`, `torch`, `onnxruntime`) não instalados; download proibido nesta etapa; dataset rotulado ainda insuficiente (`4` consultas / `6` pares vs mínimo recomendado `30` / `100`). |
| **T11.2** | Integração real controlada com eBay (`EBAY_US`) | **PASSOU** | `1` OAuth HTTP `200` + `5` Browse HTTP `200` (teto `8`) cobrindo as 4 categorias MVP + `getItem` seletivo; `0` escritas em disco e `0` vazamentos em logs. |
| **T11.3** | Avaliação real do Google Gemini API | **FALHOU** *(captura)* / **BLOQUEADO** *(teto 30)* | A execução única (`task-525`) rodou o Bloco A (`T11.3`) durante `~267s`, mas foi interrompida antes da emissão final pelo `TypeError` em `HTTPTransport(timeout=15.0)` no Bloco B (`linha 665`), impedindo a captura das métricas. Pela regra de não reiniciar contadores nem exceder `30` chamadas totais, nenhuma nova chamada Gemini foi feita sem nova autorização. |
| **T11.4** | Avaliação integrada Ponta a Ponta (Gemini + eBay) | **NÃO EXECUTADO** | Bloqueado preventivamente em razão do teto global de `30` chamadas Gemini após a interrupção de `T11.3` (conforme Seção 7). |
| **T11.5** | Qualidade, lint, tipagem e warnings | **PASSOU** | `ruff`: `9 -> 0` erros; `mypy` (todos os 68 arquivos): `32 -> 0` erros; `pip check`: OK; `406 passed, 1 skipped, 2 deselected`, `92%` cobertura; 18 warnings isolados em libs externas. |
| **T11.6** | Plano operacional de implantação (sem deploy) | **PASSOU** | Plano completo elaborado cobrindo Uvicorn, env vars, readiness, rate limit/quota por processo, concorrência, memória, monitoramento sem tracing, riscos e custos. |
| **T11.7** | Revisão final de segurança e privacidade | **PASSOU** *(com ressalva histórica)* | Controles ativos validados; registrado alerta de segurança para rotação de credenciais eventualmente presentes em 3 commits históricos antigos com `.env` (`252ee1d`, `90663f3`, `3b28fae`). |
| **Testes offline & regressões** | Suíte offline completa pré e pós-etapa | **PASSOU** | `406 passed, 1 skipped` (`test_gemini_live.py`), `2 deselected` (`ebay_live`), `0` falhas. |
| **Provisionamento/Deploy em nuvem** | AWS / Azure / alteração de produção | **NÃO APLICÁVEL** | Proibido pelo escopo da Etapa 11. |

---

## 1. Ambiente Utilizado

- **Sistema Operacional:** `Windows-11-10.0.26200-SP0`
- **CPU / Memória do host:** `28` threads lógicas (`os.cpu_count()`), RAM física total `15.69 GB` (`6.44 GB` disponíveis no momento da inspeção).
- **Python / Ambiente virtual:** `Python 3.14.7` em `.venv` isolada.
- **Pacotes principais observados:** `fastapi 0.165.0`, `uvicorn 0.54.0`, `starlette 1.10.0`, `langgraph 1.2.24`, `pydantic 2.14.3`, `httpx`, `google-genai`, `numpy`, `tenacity`, `pytest 9.2.0`, `pytest-cov 7.1.0`, `ruff 0.17.6`, `mypy 1.20.2`.
- **Configuração carregada via `Settings.safe_summary()` (sem segredos):**
  - `gemini_configured=True`, `gemini_model="gemini-3.5-flash-lite"`
  - `ebay_configured=True`, `ebay_marketplace_id="EBAY_US"`, `ebay_daily_quota_limit=5000`
  - `embedding_model_name="intfloat/multilingual-e5-small"`, `embedding_backend="sentence_transformers"`, `embedding_preload=False`
  - `api_auth_configured=False` no `.env` base (testes HTTP injetam chave efêmera em memória), `rate_limit_per_minute=30`

---

## 2. Validação do `intfloat/multilingual-e5-small` (T11.1)

- **Classificação:** **`BLOQUEADO`**
- **Inspeção técnica do ambiente:**
  - `sentence-transformers`: **não instalado** (`False`)
  - `fastembed`: **não instalado** (`False`)
  - `torch` e `onnxruntime`: **não instalados** (`False`)
  - Cache local Hugging Face (`~/.cache/huggingface/hub`): **inexistente (`False`)**, pesos ausentes (`False`).
  - Cache local FastEmbed (`~/.cache/fastembed`): **inexistente (`False`)**, pesos ausentes (`False`).
- **Comparação dos backends suportados (`pyproject.toml` e `src/buscai/semantic/embeddings.py`):**
  - **`sentence-transformers` (`[semantic]`):** usa PyTorch; maior footprint em disco/RAM (~1,5–2,5 GB entre runtime + modelo) e maior tempo de import/inicialização, sendo o backend padrão configurado (`embedding_backend="sentence_transformers"`).
  - **`fastembed` (`[semantic-onnx]`):** usa ONNX Runtime; footprint substancialmente menor em CPU (~300–600 MB em disco/RAM), porém requer verificação de compatibilidade de wheels `onnxruntime` para Python `3.14` no Windows antes de instalar.
  - **Modelo `intfloat/multilingual-e5-small`:** arquitetura E5 multilíngue (384 dimensões), licença pública **MIT** no Hugging Face, exigindo obrigatoriamente os prefixos assimétricos `query: ` e `passage: ` já implementados em [`embeddings.py`](src/buscai/semantic/embeddings.py) e [`representation.py`](src/buscai/semantic/representation.py).
- **Lacuna de dataset rotulado:** o arquivo existente [`data/eval_semantic_synthetic.jsonl`](data/eval_semantic_synthetic.jsonl) contém `4` consultas PT-BR (`retrieval`) e `6` pares de deduplicação (`deduplication`), abaixo da referência mínima recomendada de `30` consultas PT-BR e `100` pares rotulados.
- **Resultado:** conforme regra explícita de não baixar pesos nem substituir o modelo real por stub, a avaliação empírica do E5 permanece **`BLOQUEADO`**. *(O benchmark sintético anterior com `DeterministicStubEmbeddingProvider` permanece apenas como referência offline de código, não como validação do modelo neural).*

---

## 3. Avaliação Real do Gemini (T11.3)

- **Classificação:** **`FALHOU` (execução interrompida sem captura) / `BLOQUEADO` (limite global de 30 chamadas)**
- **Infraestrutura opt-in criada e mantida:**
  - Marcador `gemini_live` adicionado em [`pyproject.toml`](pyproject.toml).
  - Teste opt-in [`tests/integration/test_gemini_live.py`](tests/integration/test_gemini_live.py) exigindo `RUN_GEMINI_INTEGRATION_TESTS=1` (verificado como `1 skipped` na suíte offline padrão).
  - Runner controlado [`scripts/run_stage11_live_validation.py`](scripts/run_stage11_live_validation.py) com proxy `_GlobalGeminiModelsProxy(max_calls=30)` e 18 cenários pré-rotulados (10 single-turn de [`data/intent_eval_50.jsonl`](data/intent_eval_50.jsonl) + 8 cenários multi-turn/negação/USD/remoção explícita).
- **Ocorrência na execução única (`task-525`):**
  - A execução única rodou o Bloco A (`T11.3`) por `~267s` fazendo chamadas reais ao modelo `gemini-3.5-flash-lite`.
  - Como o JSON consolidado era impresso apenas ao final de `main()`, o `TypeError: HTTPTransport.__init__() got an unexpected keyword argument 'timeout'` ocorrido logo na abertura do Bloco B (`linha 665`) abortou o processo antes de imprimir o bloco JSON com métricas, acurácia por campo, latências e contagem exata de tokens/retries do Bloco A.
  - Em estrita observância à regra *"Não reiniciar os contadores entre scripts"* e *"Não ultrapassar 30 chamadas totais"*, **nenhuma nova chamada Gemini foi executada** nesta rodada sem nova autorização explícita do usuário.
  - O script [`scripts/run_stage11_live_validation.py`](scripts/run_stage11_live_validation.py) foi corrigido para emitir progresso incremental com `flush=True`, pronto para reexecução controlada se o usuário autorizar novo orçamento Gemini.

---

## 4. Integração Real Controlada com eBay (`EBAY_US` — T11.2)

- **Classificação:** **`PASSOU`** *(0 chamadas Gemini usadas; `RUN_EBAY_INTEGRATION_TESTS=1` em execução única de [`scripts/run_stage11_live_validation.py`](scripts/run_stage11_live_validation.py))*
- **Métricas reais observadas (`EBAY_US`, `limit=3` por categoria, nenhuma informação individual de anúncio registrada):**
  - **OAuth 2.0 (`EbayOAuthClient`):** `1` chamada HTTP `200` (`822.81 ms`), token reutilizado em memória (`oauth_token_reused_in_memory=True`) em todas as requisições seguintes.
  - **Browse API (`item_summary/search` + `getItem` seletivo):** `5` chamadas HTTP `200` dentro do teto máximo `8` (`fatal_stop=False`):

| Categoria MVP | Consulta controlada EN-US | HTTP | Candidatos retornados (`limit=3`) | Total estimado eBay | Moedas | Preço válido | Condição | Vendedor | Frete informado | Latência (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `notebook` | `laptop` (`<= $1000`) | `200` | `3` | `716.091` | `USD` | `3/3` | `3/3` | `3/3` | `3/3` | `2418.30` |
| `smartphone` | `smartphone 128GB` (`<= $900`) | `200` | `3` | `87.279` | `USD` | `3/3` | `3/3` | `3/3` | `3/3` | `857.73` |
| `monitor` | `monitor 27 inch` (`<= $800`) | `200` | `3` | `20.989` | `USD` | `3/3` | `3/3` | `3/3` | `3/3` | `700.70` |
| `fone de ouvido` | `bluetooth headphones` (`<= $450`) | `200` | `3` | `273.078` | `USD` | `3/3` | `3/3` | `3/3` | `2/3` | `774.31` |

  - **Enriquecimento `getItem` (`get_item_details`):** executado `1` vez no primeiro candidato de `notebook` (`get_item_called=True`, `get_item_parsed=True`, `get_item_details_loaded=True`, `get_item_error=None`).
  - **Observação empírica relevante:** em `fone de ouvido`, `1` de `3` ofertas retornou sem dados de frete (`with_shipping=2`), comprovando em dados reais da eBay a necessidade das regras de validação/ranking que proíbem assumir frete desconhecido como grátis.
  - **Auditoria na execução real:** `disk_writes_during_run=0`, `log_leak_detected=False`, `secret_leak_in_logs=False`.

---

## 5. Avaliação Ponta a Ponta (Gemini + eBay — T11.4)

- **Classificação:** **`NÃO EXECUTADO`**
- O bloco E2E (`_run_e2e_integrated`) não foi executado após a interrupção anterior de `T11.3`, preservando o compromisso de não ultrapassar o teto global de `30` chamadas Gemini sem autorização adicional. Além disso, o bloco dependia de `DeterministicStubEmbeddingProvider` devido ao bloqueio de `T11.1`.

---

## 6. Quantidade de Chamadas Realizadas a Cada Serviço

| Serviço | Orçamento máximo da Etapa 11 | Chamadas efetivamente contabilizadas com resultado | Observações |
| :--- | :---: | :---: | :--- |
| **eBay OAuth (`identity/v1/oauth2/token`)** | `1` | **`1`** | Status HTTP `200` na execução `T11.2`. |
| **eBay Browse API (`buy/browse/v1`)** | `8` | **`5`** | `4` `item_summary/search` + `1` `getItem`, todas HTTP `200` (`0` erros, `0` retries). |
| **Google Gemini API** | `30` | **Não contabilizadas na saída (Bloco A interrompido em `~267s`); `0` na execução final da eBay** | `task-525` executou chamadas reais em `T11.3` até o limite de 30 antes do erro no construtor eBay; por cautela orçamentária, `0` chamadas posteriores foram disparadas. |
| **Hugging Face / Download de pesos** | `0` *(proibido)* | **`0`** | Nenhuma tentativa de download executada. |

---

## 7. Tokens Consumidos

- **`0` tokens reportados com captura final:** os contadores de `usage_metadata` coletados pelo `_GlobalGeminiModelsProxy` durante `task-525` foram perdidos na interrupção antes da impressão do JSON consolidado. Na execução controlada da eBay (`T11.2`), foram consumidos `0` tokens Gemini. Não foram feitas estimativas artificiais.

---

## 8. Latência Observada

- **eBay OAuth (real, `1` chamada):** `822.81 ms`.
- **eBay Browse API (real, `5` chamadas incluindo `getItem`):** média `1148.28 ms`, `p50` `853.71 ms`, `p95` `2412.00 ms`, mínima `697.52 ms`, máxima `2412.00 ms`.
- **Gemini real:** sem série capturada pelos motivos descritos na Seção 3.

---

## 9. Métricas Agregadas por Modalidade de Evidência

| Categoria de evidência | Resultados confirmados |
| :--- | :--- |
| **Validado offline (mocks/stubs)** | **`406 passed, 1 skipped, 2 deselected`** em `python -m pytest -m "not ebay_live"` (`9.24s`) e `pytest -m "not ebay_live"` (`9.02s`); cobertura global **`92%`** (`6662` stmts, `544` miss); `0` regressões após limpeza `ruff`/`mypy`. |
| **Validado com integração real (`EBAY_US`)** | **`1/1`** OAuth HTTP `200`; **`4/4`** categorias MVP retornando candidatos reais parseados (`3` cada); **`1/1`** `getItem` com `details_loaded=True`; **`5/5`** chamadas Browse dentro do teto `8`; **`0`** escritas em disco e **`0`** vazamentos em logs. |
| **Validado com modelo real (E5 / Gemini)** | **Não validado nesta entrega:** E5 **`BLOQUEADO`** (sem pesos locais); Gemini **`FALHOU` / `BLOQUEADO`** (saída da tentativa única perdida por erro subsequente antes do `flush`, sem reexecução automática por limite de cota). |
| **Preparado, não executado** | **`tests/integration/test_gemini_live.py`** (`1 skipped` por padrão), [`tests/integration/test_api_ebay_live.py`](tests/integration/test_api_ebay_live.py) e bloco **`T11.4`** em [`scripts/run_stage11_live_validation.py`](scripts/run_stage11_live_validation.py). |

---

## 10. Limitações e Bloqueios

1. **Ausência de pesos reais do `multilingual-e5-small` (`T11.1`):** sem backends instalados e sem pesos em cache local; download proibido nesta etapa.
2. **Dataset semântico insuficiente (`data/eval_semantic_synthetic.jsonl`):** apenas `4` consultas PT-BR e `6` pares de deduplicação, abaixo da meta recomendada de `30` consultas e `100` pares para calibrar limiares de produção (`0.86` / `0.68`).
3. **Ausência de métricas capturadas do Gemini real (`T11.3`) e E2E combinado (`T11.4`):** requer nova autorização explícita do usuário (novo orçamento de até 30 chamadas) com o script já corrigido.
4. **Cota/Rate Limit locais ao processo:** `InMemoryRateLimiter` e `QuotaGuard` não compartilham estado entre múltiplos workers/instâncias.

---

## 11. Resultados de Ruff, Mypy, Pytest-Cov e Análise de Warnings

| Ferramenta | Antes (abertura Etapa 11) | Depois (após T11.5) | Escopo verificado |
| :--- | :---: | :---: | :--- |
| **Ruff (`ruff check --no-cache`)** | `9` erros (`1` `E731` + `8` `F401`) | **`0` erros (`All checks passed!`)** | `src/`, `tests/`, `scripts/` (excl. `src/buscai/app`) |
| **Mypy (`mypy`)** | `32` erros em `7` arquivos | **`0` erros em `68` arquivos fonte** | `src/buscai`, `src/userintent` (excl. `src/buscai/app`) |
| **Pytest offline (`not ebay_live`)** | `406 passed, 2 deselected` | **`406 passed, 1 skipped, 2 deselected`** | Suíte completa + `test_gemini_live.py` opt-in |
| **Cobertura (`pytest-cov`)** | `92%` (`6653` stmts, `544` miss) | **`92%` (`6662` stmts, `544` miss)** | `src/buscai` (excl. `app/`) + `src/userintent` |
| **Dependências (`pip check`)** | `No broken requirements found.` | **`No broken requirements found.`** | `.venv` completo |

- **Análise dos 18 warnings:** **0 warnings** vêm de `src/` ou `tests/`. Todos os 18 vêm de dependências de terceiros sob Python `3.14.7`:
  - `17` `DeprecationWarning` em `.venv/Lib/site-packages/langsmith/_internal/_beta_decorator.py:92` (`asyncio.iscoroutinefunction`).
  - `1` `DeprecationWarning` em `.venv/Lib/site-packages/google/genai/types.py:42` (`_UnionGenericAlias`).

---

## 12. Auditoria Final de Segurança (T11.7)

| Controle auditado | Situação observada |
| :--- | :--- |
| **Autenticação `X-API-Key` e fail-closed** | **PASSOU** — comparação constante (`secrets.compare_digest`), rejeição de placeholders e HTTP `503` quando `BUSCAAI_API_KEYS` não está configurada. |
| **Separação de credenciais** | **PASSOU** — `BUSCAAI_API_KEYS` (clientes HTTP) isolada de `GEMINI_API_KEY` e `EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET`; chaves não aparecem em `/v1/health`, `/openapi.json`, respostas ou logs. |
| **Rate limiting** | **PASSOU (processo único)** — janela deslizante com chave opaca HMAC, teto `max_clients`, eviction de buckets expirados, `429` + `Retry-After`. |
| **Sanitização de logs e mensagens de erro** | **PASSOU** — `ALLOWED_LOG_FIELDS` + redação de `Bearer`, `Basic`, `AIza...` e segredos; respostas de erro nunca incluem stack trace ou anúncios. |
| **Histórico conversacional (`POST /v1/chat`)** | **PASSOU** — stateless, `extra="forbid"`, `role` restrito a `user`/`assistant`, limites configuráveis e remoção de URLs/preços/IDs/vendedores de turnos anteriores antes do Gemini. |
| **Não persistência e ausência de tracing** | **PASSOU** — `0` escritas em disco comprovadas na suíte offline e na execução real da eBay; `checkpointer=None`, `store=None`; variáveis LangSmith/LangChain bloqueadas em runtime (`503 policy_violation`). |
| **`.env`, `.env.example` e dependências** | **PASSOU** — `.env` não versionado no HEAD, não lido/impresso, `.env.example` limpo e `pip check` sem conflitos. |
| **Histórico Git (commits antigos)** | **ALERTA OPERACIONAL:** `git log --all --full-history --oneline -- .env` indicou **3 commits antigos** no histórico (`252ee1d`, `90663f3`, `3b28fae`) que tocaram `.env` no passado. O histórico não foi alterado conforme regra; **é obrigatório avaliar e rotacionar** quaisquer chaves presentes nesses commits antes de operar em produção caso ainda estejam ativas. |

---

## 13. Plano Operacional de Implantação (T11.6 — Sem Execução de Deploy)

### 13.1. Requisitos Mínimos Recomendados
- **Runtime:** Python `3.11+` (idealmente Python `3.12`/`3.13` para compatibilidade madura com `sentence-transformers`/`onnxruntime`, ou `3.14` com backend compatível verificado).
- **Computação / Memória:** mínimo `2 vCPU` e **`2 GB RAM` livres** usando `fastembed` (ONNX), ou **`4 GB RAM`** usando `sentence-transformers` (PyTorch CPU), para manter modelo E5 em memória + objetos transitórios por requisição.
- **Armazenamento:** disco apenas para código-fonte e cache somente leitura dos pesos pré-baixados em imagem/volume (zero persistência de anúncios ou conversas).

### 13.2. Variáveis de Ambiente
- **Obrigatórias:** `GEMINI_API_KEY`, `GEMINI_MODEL`, `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `EBAY_MARKETPLACE_ID=EBAY_US`, `BUSCAAI_API_KEYS` (chaves fortes separadas por vírgula), `BUSCAAI_EMBEDDING_PRELOAD=true` (em produção com pesos já embutidos localmente).
- **Segurança / Operação:** `USD_BRL_RATE`, `USD_BRL_RATE_UPDATED_AT`, `USD_BRL_RATE_SOURCE`, `EBAY_DAILY_QUOTA_LIMIT=5000`, `BUSCAAI_RATE_LIMIT_PER_MINUTE`, `BUSCAAI_RATE_LIMIT_WINDOW_SECONDS`, `BUSCAAI_SEARCH_DEFAULT_LIMIT`, `BUSCAAI_SEARCH_MAX_LIMIT`, `BUSCAAI_CHAT_MAX_HISTORY_MESSAGES`, `BUSCAAI_CHAT_MAX_MESSAGE_CHARS`, `BUSCAAI_CHAT_MAX_HISTORY_TOTAL_CHARS`, `BUSCAAI_LOG_LEVEL=INFO`.
- **Nunca definir em produção:** `LANGSMITH_TRACING`, `LANGCHAIN_TRACING_V2` ou chaves `RUN_*_INTEGRATION_TESTS=1`.

### 13.3. Procedimento de Inicialização
1. Ativar `.venv` e garantir instalação com extra semântico aprovado (`pip install -e ".[semantic]"` ou `".[semantic-onnx]"` após validar compatibilidade da versão Python).
2. Validar presença local dos pesos `intfloat/multilingual-e5-small` (evitando download em runtime durante startup de produção).
3. Iniciar processo único de worker (ver item 13.5):
   ```powershell
   uvicorn buscai.api.app:app --host 0.0.0.0 --port 8000 --workers 1
   ```

### 13.4. Health e Readiness Esperados
- Executar sonda em `GET /v1/health`:
  - Enquanto `api_auth`, `search_orchestrator`, `embeddings` ou `tracing_policy` não estiverem prontos, a API responde `HTTP 503` com `liveness="alive"` e `readiness="not_ready"`.
  - Somente quando os pesos estiverem carregados em memória (`BUSCAAI_EMBEDDING_PRELOAD=true`) e todas as credenciais obrigatórias estiverem válidas, `/v1/health` retorna `HTTP 200` com `readiness="ready"`.

### 13.5. Limitações de Escala (Multi-Worker e Multi-Instância)
- **Por que `--workers > 1` NÃO é transparente nem seguro por si só:**
  1. **`QuotaGuard` (`5.000/dia`) é em memória por processo:** com `N` workers ou `N` contêineres, cada worker enxerga seu próprio contador independente, podendo multiplicar o consumo real de cota eBay por até `N` vezes.
  2. **`InMemoryRateLimiter` é em memória por processo:** um cliente B2B com limite `30 req/min` pode atingir `30 * N req/min` se o balanceador alternar entre `N` workers.
  3. **Memória do modelo de embeddings:** cada worker carrega sua própria cópia de `multilingual-e5-small` na RAM (`~0,5–2 GB` cada) e mantém seu próprio cache OAuth em memória.
- **Mitigação recomendada no MVP:** operar com `--workers 1` por instância e concorrência via `run_in_threadpool` para I/O (e ajustar `ebays_daily_limit` proporcionalmente se houver `> 1` réplica, ou introduzir gateway externo/rate limit distribuído somente quando o crescimento exigir nova avaliação arquitetural).

### 13.6. Estratégia de Monitoramento Permitida
- Coletar apenas os eventos JSON estruturados emitidos em `stdout` pelo `SafeJsonFormatter` (`ALLOWED_LOG_FIELDS`: `timestamp`, `level`, `logger`, `event`, `stage`, `method`, `path`, `status_code`, `duration_ms`, `candidate_count`, `result_count`, `ebay_calls`, `llm_calls`, `error_type`, `provider`, `quota_remaining`, `retry_attempt`).
- Monitorar externamente código HTTP de `GET /v1/health`, taxa de `429`/`502`/`503` e consumo diário agregado (`ebay_calls` vs `5000`).
- **Proibido:** habilitar LangSmith/tracing externo ou logar payloads de anúncios, consultas completas de usuários ou cabeçalhos de autorização.

### 13.7. Riscos Operacionais
- Dependência de latência de rede e disponibilidade de dois provedores externos síncronos (Google Gemini + eBay Browse API) na rota crítica.
- Estouro silencioso da cota diária eBay (`5.000`) caso sejam escaladas múltiplas réplicas sem dividir `EBAY_DAILY_QUOTA_LIMIT`.
- Credenciais históricas expostas nos commits antigos citados na Seção 12 caso não sejam rotacionadas.
- Falha no carregamento de embeddings no startup se a imagem não incluir cache local ou se faltar RAM.

### 13.8. Pontos de Custo a Estimar
- Custo por 1M tokens de entrada/saída no Gemini (`gemini-3.5-flash-lite` ou substituto configurado em `GEMINI_MODEL`).
- Limite gratuito vs plano pago da eBay Developer API acima de `5.000` chamadas/dia.
- Computação (RAM/vCPU para `multilingual-e5-small` residente em memória) e tráfego HTTP de saída.

---

## 14. Arquivos Alterados e Criados na Etapa 11

- **Criados:**
  - [`tests/integration/test_gemini_live.py`](tests/integration/test_gemini_live.py)
  - [`scripts/run_stage11_live_validation.py`](scripts/run_stage11_live_validation.py)
  - `RELATORIO_VALIDACAO_POS_MVP.md` *(este relatório)*
- **Alterados (limpeza de qualidade T11.5, marcador opt-in e documentação):**
  - [`pyproject.toml`](pyproject.toml), [`README.md`](README.md), [`IMPLEMENTACOES.md`](IMPLEMENTACOES.md)
  - [`src/buscai/adapters/intent.py`](src/buscai/adapters/intent.py), [`src/buscai/catalog/dummyjson.py`](src/buscai/catalog/dummyjson.py), [`src/buscai/catalog_registry.py`](src/buscai/catalog_registry.py), [`src/buscai/orchestration/graph.py`](src/buscai/orchestration/graph.py), [`src/buscai/orchestration/nodes.py`](src/buscai/orchestration/nodes.py), [`src/buscai/ranking/reranker.py`](src/buscai/ranking/reranker.py), [`src/buscai/search/query_builder.py`](src/buscai/search/query_builder.py), [`src/buscai/validation/evidence.py`](src/buscai/validation/evidence.py), [`src/buscai/validation/pipeline.py`](src/buscai/validation/pipeline.py), [`src/buscai/validation/validator.py`](src/buscai/validation/validator.py)
  - [`tests/test_orchestration_routing_and_nodes.py`](tests/test_orchestration_routing_and_nodes.py), [`tests/test_validation_enrichment.py`](tests/test_validation_enrichment.py), [`tests/test_validation_integration_and_compliance.py`](tests/test_validation_integration_and_compliance.py), [`tests/test_validation_registry.py`](tests/test_validation_registry.py)
- **Preservados intactos:** `src/buscai/app/**`, `AUDITORIA.md`, `.env`, relatórios das Etapas 8–10.

---

## 15. Recomendação Final Honesta de Prontidão

**Veredito atual: NÃO RECOMENDADO PARA PRODUÇÃO IMEDIATA (APTO COMO MVP VALIDADO OFFLINE + INTEGRAÇÃO REAL EBAY COMPROVADA, COM PENDÊNCIAS EMPÍRICAS OBRIGATÓRIAS).**

O sistema apresenta engenharia sólida em arquitetura, conformidade eBay (D1–D9), cobertura offline (`406` testes em `92%`), qualidade estática limpa (`0` erros em `ruff` e `mypy`) e comunicação real validada contra `EBAY_US` nas 4 categorias MVP (`OAuth 200` + `5` Browse `200`, `getItem` e zero persistência). Contudo, **antes de colocar em produção**, é indispensável concluir:

1. **Instalar backend semântico e avaliar empiricamente `intfloat/multilingual-e5-small`** com dataset rotulado adequado (`T11.1`, hoje `BLOQUEADO`).
2. **Concluir a captura e medição real do Gemini (`T11.3`) e do fluxo ponta a ponta Gemini + eBay (`T11.4`)** mediante nova autorização explícita de chamadas.
3. **Avaliar e rotacionar as credenciais históricas** eventualmente presentes nos commits antigos com `.env`, e definir `BUSCAAI_API_KEYS` produtiva em cofre/ambiente seguro.
