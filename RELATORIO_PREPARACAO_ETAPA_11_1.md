# Relatório de Preparação — Etapa 11.1 (Correção do Runner, Auditoria de Credenciais e Benchmarks Offline)

- **Data:** 25/11/2026
- **Branch Git:** `waldeney` (commit base `ed58065225485a67c391ca8990e05ea4f91b8e0c`, sem commits, push, alterações em `.env`, alterações em `src/buscai/app/` ou reescrita de histórico)
- **Natureza da execução:** 100% offline — **0 chamadas reais à eBay, 0 chamadas reais ao Gemini e 0 downloads de pesos de modelos**.

---

## Quadro de Classificação dos Itens da Etapa 11.1

| Item | Escopo | Classificação | Evidência principal |
| :--- | :--- | :---: | :--- |
| **1. Correção do runner Gemini/eBay** | Remoção de `timeout` em `httpx.HTTPTransport()` (API `httpx 0.28.1`) mantendo timeouts nos `httpx.Client` | **PASSOU** | [`scripts/run_stage11_live_validation.py`](scripts/run_stage11_live_validation.py), [`tests/test_stage11_1_preparation.py`](tests/test_stage11_1_preparation.py) |
| **2. Captura progressiva & contador global** | Progresso `flush=True`, `--output` com preservação parcial após falha, retries contabilizados, `--mode gemini\|e2e\|both` | **PASSOU** | `ProgressiveMetricsRecorder`, `_GlobalGeminiModelsProxy` (`1..30`) |
| **3. Auditoria de commits históricos `.env`** | Análise segura sem exibir valores de `252ee1d`, `90663f3` e `3b28fae` | **PASSOU** *(ação manual pendente)* | Seção 4 deste relatório (`3b28fae` exige rotação manual da `GEMINI_API_KEY`) |
| **4. Dataset semântico v2 controlado** | `36` consultas PT-BR + `112` pares rotulados, disjunção estrita calibração vs avaliação | **PASSOU** | [`data/eval_semantic_v2_calibration.jsonl`](data/eval_semantic_v2_calibration.jsonl), [`data/eval_semantic_v2_evaluation.jsonl`](data/eval_semantic_v2_evaluation.jsonl), [`data/README_EVAL_SEMANTIC_V2.md`](data/README_EVAL_SEMANTIC_V2.md) |
| **5. Recomendação técnica de backend E5** | Análise `Python 3.14.7` + comparação `fastembed` vs `sentence-transformers` + comandos futuros | **PASSOU** | Seção 6 deste relatório |
| **6. Testes offline, lint e tipagem** | Novos testes offline + suíte completa + `ruff` + `mypy` + `pip check` | **PASSOU** | `411 passed, 1 skipped, 2 deselected`, `ruff=0`, `mypy=0` |
| **7. Execução real `T11.1`, `T11.3` e `T11.4`** | Inferência E5 real, Gemini real e E2E combinado | **NÃO EXECUTADO** *(aguarda autorização)* | Mantidas `[ ]` em [`IMPLEMENTACOES.md`](IMPLEMENTACOES.md) |
| **8. Repetição isolada da eBay (`T11.2`)** | Já validada na Etapa 11 (`1 OAuth + 5 Browse HTTP 200`); não repetida automaticamente | **NÃO APLICÁVEL** | Padrão `--mode both` omite `t11_2_ebay_live` |

---

## 1. Causa do Erro do Gemini / Runner

- **Erro observado na execução única da Etapa 11 (`task-525`):**
  ```text
  TypeError: HTTPTransport.__init__() got an unexpected keyword argument 'timeout'
  ```
- **Causa raiz confirmada por inspeção (`httpx 0.28.1` no `.venv`):**
  - Na assinatura real de `httpx.HTTPTransport.__init__` (`verify, cert, trust_env, http1, http2, limits, proxy, uds, local_address, retries, socket_options`), **não existe o parâmetro `timeout`**.
  - No `httpx 0.28.1`, `timeout` é parâmetro de `httpx.Client.__init__` (que já é configurado nativamente em `EbayOAuthClient` e `EbayLiveBackend` quando instanciados).
  - Além disso, o runner imprimia o JSON consolidado apenas no final de `main()`, de modo que o `TypeError` ocorrido na abertura do Bloco B abortou o processo sem gravar as métricas já obtidas no Bloco A (`T11.3`).

## 2. Correções Realizadas

No arquivo [`scripts/run_stage11_live_validation.py`](scripts/run_stage11_live_validation.py):

1. **Transporte corrigido (`_GuardedEbayTransport`) e `build_ebay_clients()`:**
   - `_GuardedEbayTransport` agora instancia `httpx.HTTPTransport()` (sem `timeout`) ou aceita `inner_transport` injetável para testes offline com `httpx.MockTransport`, preservando integralmente os timeouts originais nos objetos `httpx.Client` dos clientes `EbayOAuthClient` e `EbayLiveBackend`.
2. **Captura progressiva e preservação parcial (`ProgressiveMetricsRecorder`):**
   - Emite um evento JSON com `flush=True` (no `progress_stream`, padrão `sys.stderr`) **ao final de cada cenário individual** com contagem acumulada de chamadas Gemini, retries, tokens disponíveis, chamadas Browse/OAuth e latência.
   - Com `--output <caminho>`, grava de forma atômica (`.tmp` -> `replace`) o relatório parcial agregado após cada cenário e no bloco `finally:`, preservando os resultados do Bloco A mesmo que um bloco posterior falhe.
   - Não armazena respostas brutas do Gemini, segredos, títulos, URLs, vendedores ou payloads de anúncios da eBay.
3. **Contador global compartilhado e modos independentes:**
   - `_GlobalGeminiModelsProxy` recebe `--max-gemini-calls` (`1..30`, padrão `30`), conta cada tentativa HTTP real e diferencia `retries_counted` por requisição lógica (`begin_logical_request()` / `end_logical_request()`), sendo compartilhado entre **Avaliação A (`gemini`)** e **Avaliação B (`e2e`)** sem nunca reiniciar após erro.
   - CLI adicionada com `--mode {gemini,e2e,both,ebay}` (padrão `both`), omitindo `t11_2_ebay_live` por padrão para não repetir chamadas eBay isoladas já aprovadas na Etapa 11.
   - Função testável `run_validation(...)` desacoplada para verificação completa sem conexões externas.

## 3. Testes Offline Executados

Criada a suíte [`tests/test_stage11_1_preparation.py`](tests/test_stage11_1_preparation.py) (`5` funções de teste cobrindo: assinatura `httpx 0.28.1` e timeout nos `httpx.Client`, falhas `401` e limite em `_GuardedEbayTransport`, captura progressiva `flush=True` + retry + tokens + preservação parcial em `--output` após falha simulada do Bloco B, contador global compartilhado entre A e B + ausência de repetição de `T11.2`, e integridade completa/disjunção do dataset semântico v2 — tudo sob `sys.addaudithook` confirmando `0` conexões de rede externas).

Resultados pós-edição final:

| Verificação | Resultado |
| :--- | :--- |
| `.\.venv\Scripts\python.exe -m pytest -m "not ebay_live" -q` *(sem flags opt-in)* | **`411 passed, 1 skipped, 2 deselected, 18 warnings`** (`7.97s`) |
| `.\.venv\Scripts\pytest.exe -m "not ebay_live" -q` *(sem flags opt-in)* | **`411 passed, 1 skipped, 2 deselected, 18 warnings`** (`7.99s`) |
| `.\.venv\Scripts\ruff.exe check --no-cache src/ tests/ scripts/ --exclude src/buscai/app` | **`All checks passed!`** (`0` erros) |
| `.\.venv\Scripts\mypy.exe --cache-dir "$env:TEMP\buscai_mypy_cache" src/buscai src/userintent --exclude "src/buscai/app"` | **`Success: no issues found in 68 source files`** (`0` erros) |
| `.\.venv\Scripts\python.exe -m pip check` | **`No broken requirements found.`** |
| `git diff --name-only -- src/buscai/app/ AUDITORIA.md .env` | **Vazio** (arquivos protegidos intactos) |

## 4. Auditoria dos Commits Históricos com `.env` (Sem Revelar Valores)

| Commit | Estado de `.env` no commit | Variáveis encontradas | Classificação | Recomendação |
| :--- | :--- | :--- | :--- | :--- |
| `252ee1d` *(Merge pull request #1)* | `.env` ausente na árvore do commit | — | Sem arquivo `.env` no tree | Nenhuma ação específica neste commit (mas o ancestral `3b28fae` permanece alcançável no histórico) |
| `90663f3` *(feat: DummyJSON...)* | `D .env` (remoção do arquivo do índice) | — | Remoção de `.env` | Nenhuma credencial introduzida neste commit |
| `3b28fae` *(fix llm model and userIntent struct)* | `A .env` (adição histórica de `.env`) | `GEMINI_API_KEY` | **Valor não vazio com aparência de segredo** | **ROTAÇÃO MANUAL RECOMENDADA** pelo proprietário no Google AI Studio caso a chave ainda esteja ativa |
| `3b28fae` *(idem)* | `A .env` | `GEMINI_MODEL` | Configuração não secreta (não vazio) | Não aplicável (nome de modelo público) |

> Nenhuma credencial eBay (`EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`) ou `BUSCAAI_API_KEYS` foi encontrada nesses commits; apenas `GEMINI_API_KEY` em `3b28fae` requer rotação manual preventiva caso não tenha sido revogada após a criação do repositório.

## 5. Dataset Semântico v2 Preparado

Mantido [`data/eval_semantic_synthetic.jsonl`](data/eval_semantic_synthetic.jsonl) intacto para regressão da Etapa 4 e criada a versão controlada **v2** (100% sintética, sem dados reais da eBay), documentada em [`data/README_EVAL_SEMANTIC_V2.md`](data/README_EVAL_SEMANTIC_V2.md):

| Split / Arquivo | Consultas PT-BR (`retrieval`) | Pares de deduplicação (`deduplication`) |
| :--- | :---: | :---: |
| [`data/eval_semantic_v2_calibration.jsonl`](data/eval_semantic_v2_calibration.jsonl) | **12** | **40** |
| [`data/eval_semantic_v2_evaluation.jsonl`](data/eval_semantic_v2_evaluation.jsonl) | **24** | **72** |
| **Total v2 (conjuntos estritamente disjuntos)** | **36** *(meta >= 30)* | **112** *(meta >= 100)* |

- Cobertura validada por testes automatizados: PT-BR e EN-US (incluindo cross-lingual), modelos equivalentes (`probable_duplicate`), modelos distintos da mesma marca, todas as 7 variantes (`Mini`, `Plus`, `Pro`, `Max`, `Ultra`, `SE`, `FE`), todas as 4 condições (`New`, `Used`, `Refurbished`, `Open Box`), diferenças de RAM e armazenamento, resoluções/specs incompatíveis e casos de informação incompleta.
- [`scripts/benchmark_embeddings.py`](scripts/benchmark_embeddings.py) atualizado para propagar `condition` opcional dos candidatos sintéticos quando presente.

## 6. Backend E5 Recomendado e Compatibilidade

- **Ambiente atual:** `Python 3.14.7` (Windows 11 64-bit, `15.69 GB` RAM), sem `sentence-transformers`, `fastembed`, `torch` ou `onnxruntime` instalados em `.venv`.
- **Comparação técnica (`pyproject.toml` extras):**
  1. **`fastembed>=0.5,<1` (extra `semantic-onnx`):** **recomendado para CPU em produção leve** (~300–600 MB em disco/RAM usando ONNX Runtime quantizado/FP32, menor latência de startup, sem necessidade do stack PyTorch completo).
  2. **`sentence-transformers>=3,<6` (extra `semantic`):** alternativa de referência caso não haja wheel `onnxruntime` compatível publicada no PyPI para Python `3.14` no Windows; requer `torch` (~1,5–2,5 GB entre runtime e modelo em RAM/disco).
- **Ponto crítico de compatibilidade (Python `3.14.7`):** no Windows, wheels pré-compiladas de `onnxruntime` e `torch` frequentemente têm disponibilidade mais rápida para **Python `3.12` ou `3.13`** do que para `3.14` recém-lançado. Caso `pip install -e ".[semantic-onnx]"` falhe por ausência de wheel `cp314`, recomenda-se criar um segundo ambiente virtual dedicado em Python `3.12` ou `3.13` (`requires-python = ">=3.11"` já suporta ambos no `pyproject.toml`) em vez de tentar compilar C++/ONNX localmente.

## 7. Comandos para Futura Execução (Mediante Autorização Explícita)

### A. Avaliação real controlada do Gemini (`--mode gemini`, ou combinada A+B com `--mode both`)
```powershell
$env:RUN_GEMINI_INTEGRATION_TESTS = "1"
# Para --mode both (inclui Bloco B E2E Gemini+eBay): definir também $env:RUN_EBAY_INTEGRATION_TESTS = "1"
.\.venv\Scripts\python.exe -u scripts/run_stage11_live_validation.py --mode gemini --max-gemini-calls 20 --output .tmp_stage11_gemini_metrics.json
```

### B. Avaliação ponta a ponta combinada Gemini + eBay (`--mode both` ou `--mode e2e`, compartilhando contador global)
```powershell
$env:RUN_GEMINI_INTEGRATION_TESTS = "1"
$env:RUN_EBAY_INTEGRATION_TESTS = "1"
.\.venv\Scripts\python.exe -u scripts/run_stage11_live_validation.py --mode both --max-gemini-calls 30 --max-browse-calls 8 --output .tmp_stage11_both_metrics.json
```

### C. Ativação futura e benchmark real do `intfloat/multilingual-e5-small` (Etapa 11 / T11.1)
```powershell
# 1. Instalar SOMENTE o backend escolhido após confirmar compatibilidade da versão Python (preferir Python 3.12/3.13 se faltarem wheels cp314):
pip install -e ".[semantic-onnx]"
# 2. Avaliar separadamente calibração e avaliação cega (a primeira execução baixará intfloat/multilingual-e5-small do Hugging Face):
.\.venv\Scripts\python.exe scripts/benchmark_embeddings.py --backend fastembed --models intfloat/multilingual-e5-small --dataset data/eval_semantic_v2_calibration.jsonl
.\.venv\Scripts\python.exe scripts/benchmark_embeddings.py --backend fastembed --models intfloat/multilingual-e5-small --dataset data/eval_semantic_v2_evaluation.jsonl
```

## 8. Limites de Quotas Configurados

- **Gemini API:** `--max-gemini-calls` limitado estruturalmente entre `1` e **`30`** (contabilizando tentativas e retries de forma global entre Bloco A e Bloco B, sem reset).
- **eBay Browse API (`EBAY_US`):** `--max-browse-calls` limitado entre `1` e **`8`**, e `--max-oauth-calls` fixado em **`1`**.
- **Regra de não repetição eBay:** o modo padrão `--mode both` **não** reexecuta `t11_2_ebay_live` isolado (já aprovado anteriormente com `1 OAuth + 5 Browse`).

## 9. Pendências que Ainda Precisam de Autorização Explícita

1. **Rotação manual da `GEMINI_API_KEY`** exposta no commit histórico `3b28fae` (se ainda estiver ativa), antes de autorizar nova bateria Gemini.
2. **Autorização para instalar backend (`fastembed` / `.venv` Python 3.12/3.13 se necessário) e baixar pesos reais de `intfloat/multilingual-e5-small`** para execução de `T11.1` nos datasets v2.
3. **Autorização explícita de novo orçamento de chamadas** para executar `scripts/run_stage11_live_validation.py --mode gemini` / `--mode both` (`T11.3` e `T11.4`).
