# Relatório Técnico — Etapa 11.2.2: Correções P0, P1 e P2 do Diagnóstico Gemini (100% Offline)

## 1. Resumo Executivo

Foram implementadas e validadas **exclusivamente offline** todas as correções de confiabilidade operacional (**P0**), consistência semântica (**P1**) e observabilidade (**P2**) identificadas em [`RELATORIO_DIAGNOSTICO_GEMINI_11_2_1.md`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/RELATORIO_DIAGNOSTICO_GEMINI_11_2_1.md).

Principais resultados observados após a última edição:

- **Zero chamadas externas** (Gemini, eBay e OAuth não foram chamados; verificações de rede via `sys.addaudithook` aprovaram 100% dos testes sem conexões externas).
- **Suíte offline ampliada:** evolução de **411 aprovados** (Etapa 11.1) para **423 aprovados, 1 skipped** ([`test_gemini_live.py`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/tests/integration/test_gemini_live.py)) e **2 deselected** (`ebay_live`), com **12 novos testes dedicados** cobrindo todos os 31 cenários P0, P1 e P2 em [`tests/test_stage11_2_2_corrections.py`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/tests/test_stage11_2_2_corrections.py).
- **Cobertura global mantida em 92%** (`6780` declarações, `547` ausentes; destaque para `userintent/evaluator.py` em `97%`, `userintent/schema.py` em `96%` e `userintent/llm.py` em `92%`).
- **Qualidade estática limpa:** `ruff` com **0 erros**, `mypy` com **0 erros em 68 arquivos**, `pip check` sem conflitos e validação AST/importação aprovada em todos os módulos sob `src/`.
- **Importante:** A validação real com o Google Gemini **ainda não foi reexecutada**.

---

## 2. Estado Inicial do Repositório

- **Branch / HEAD:** `waldeney` @ `ed58065` (`ed58065225485a67c391ca8990e05ea4f91b8e0c`), preservando histórico Git sem commits ou push.
- **Estado prévio à Etapa 11.2.2:** alterações locais das Etapas 8–11.2.1 presentes no working tree, incluindo o arquivo de métricas da execução anterior [`.tmp_stage11_gemini_metrics.json`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/.tmp_stage11_gemini_metrics.json) e o relatório de diagnóstico [`RELATORIO_DIAGNOSTICO_GEMINI_11_2_1.md`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/RELATORIO_DIAGNOSTICO_GEMINI_11_2_1.md).
- **Linha de base de testes prévia:** `411 passed, 1 skipped, 2 deselected`, 92% de cobertura.
- **Arquivos protegidos preservados integralmente:** `src/buscai/app/**`, `AUDITORIA.md` e `.env` permaneceram inalterados (`git diff --name-only` vazio para esses caminhos).

---

## 3. Correções P0 Implementadas

### 3.1 Preservação do Backoff Exponencial e Jitter Configurável
- Removida de [`scripts/run_stage11_live_validation.py`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/scripts/run_stage11_live_validation.py) a sobrescrita fixa `sleep=lambda _: time.sleep(0.5)` que anulava o backoff exponencial do `IntentExtractor`.
- Em [`src/userintent/llm.py`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/userintent/llm.py), `IntentExtractor.__init__` recebeu os parâmetros opcionais `jitter_ratio: float = 0.0`, `random_fn: Callable[[], float] = random.random`, `retry_on_429: bool = True` e `on_retry: Callable[[int, float, int | None, float | None], None] | None = None`:
  - Mantém determinismo nos contratos legados quando `jitter_ratio == 0.0` (preservando `[0.25, 0.5]`).
  - Aplica jitter proporcional `base_delay + (base_delay * jitter_ratio * random_fn())` quando `jitter_ratio > 0.0` (no runner controlado: `jitter_ratio=0.25`, `base_backoff_seconds=1.0`, `max_backoff_seconds=8.0`).
  - Permite injeção de `retry_sleep` e `random_fn` em [`run_validation`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/scripts/run_stage11_live_validation.py#L1260-L1500) para execução instantânea em testes offline.

### 3.2 Pacing Configurável Entre Cenários
- Adicionada a opção `--sleep-between-calls` em [`parse_args`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/scripts/run_stage11_live_validation.py) (padrão `4.0` s em modo real e `0.0` s quando `gemini_client` stub é injetado sem parâmetro explícito).
- Helper `_apply_pacing` aplica pausas **apenas entre cenários consecutivos**, de forma independente do backoff de retries, com `pacing_sleep` injetável e telemetria registrada em `report["pacing"]` e `report["global_usage_and_limits"]["pacing"]`.

### 3.3 Parada Imediata em HTTP `429` e Separação de HTTP `503`
- Em [`src/userintent/llm.py`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/userintent/llm.py), adicionados `_extract_http_status`, `_extract_retry_after_seconds` e `_is_429_error`. Quando `retry_on_429=False` (ativado no runner controlado), ocorrências de HTTP `429` **não disparam retry** nem espera de backoff, levantando `LLMUnavailableError` imediatamente.
- Em [`_GlobalGeminiModelsProxy`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/scripts/run_stage11_live_validation.py), `_classify_gemini_exception` classifica `429` como `"http_429_rate_limit"`, ativa `fatal_stop = True` e `stop_reason = "http_429_rate_limit"`, e captura `Retry-After` numérico com segurança sem registrar ou expor cabeçalhos HTTP.
- Erros `503` são classificados separadamente como `"http_503_unavailable"`, preservando retries controlados até `max_retries` com backoff exponencial + jitter.

### 3.4 Controle Explícito de Retry no SDK `google-genai`
- Em [`IntentExtractor.extract`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/userintent/llm.py), `types.HttpOptions` agora define explicitamente:
  ```python
  http_options=types.HttpOptions(
      timeout=timeout_ms,
      retry_options=types.HttpRetryOptions(attempts=1),
  )
  ```
  impedindo retries ocultos do SDK fora do contador global.

### 3.5 Auditoria Thread-Safe do Contador Global Gemini
- `_GlobalGeminiModelsProxy` reserva a cota sob `threading.Lock()` antes de invocar `self._wrapped.generate_content`, recusa execuções após `fatal_stop` ou `calls_made >= max_calls` (`1..30`) levantando `BudgetExhaustedError`, e mantém contexto lógico por thread (`_active_logical`) compatível com chamadas sequenciais, worker threads do `TestClient` e concorrência direta.

### 3.6 Atualização Completa de `global_usage_and_limits` em Snapshots Parciais
- [`ProgressiveMetricsRecorder`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/scripts/run_stage11_live_validation.py) passou a executar o callback `_refresh_globals()` em **todo** `record_step()` e `flush_final()` antes da gravação atômica, mantendo `calls_made`, `max_allowed`, `retries_counted`, `total_tokens_sum`, `execution_state`, `pacing`, `attempt_trail` e estado de interrupção sincronizados em cada snapshot parcial.

### 3.7 Classificação Explícita em 4 Estados e Métricas Com Denominador Seguro
- Substituída a filtragem frágil baseada em presença de chave por classificação explícita em 4 estados (`passed`, `failed`, `error`, `not_run`) para os **10 cenários single-turn planejados** e **8 cenários multi-turn (`M1`..`M8`)**.
- Todos os cenários planejados são inicializados como `not_run` e anotados com `not_run_reason` em caso de interrupção.
- `_summarize_status_counts` emite `planned_count`, `passed_count`, `failed_count`, `error_count`, `not_run_count`, `completed_count`, `pass_rate_over_planned` (`passed / planned`) e `pass_rate_over_completed` (`passed / (passed + failed)`, retornando `None`/`null` quando o denominador é zero).

---

## 4. Correções P1 Implementadas

### 4.1 Normalização de `PriceConstraint` Sem Limites Numéricos
- Adicionado `@model_validator(mode="after") def _normalize_empty_bounds(self)` em [`PriceConstraint`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/userintent/schema.py#L93-L108): quando `min_price is None and max_price is None`, força `min_inclusive=True`, `max_inclusive=True` e `is_hard_constraint=False`.
- Quando `min_price` ou `max_price` é definido, todas as flags inclusivas e de rigidez são preservadas integralmente.
- [`price_match`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/userintent/evaluator.py) em `src/userintent/evaluator.py` aplica normalização equivalente defensiva nos dois lados da comparação.

### 4.2 Alinhamento Semântico Sem Distorcer o Extrator Nem Inflar Métricas
- **Preferência vs. Caso de Uso (`single_3` vs. `single_15` / `single_30`):** mantido o comportamento fiel do extrator em não inventar `"fotografia"` quando o usuário solicita apenas `"boa câmera"` (`preferred_features`), preservando `"fotografia"` obrigatória onde há declaração explícita de uso fotográfico (`case_id=15` e `case_id=30`).
- **Diferenciação `"trabalho"` vs. `"escritório"`:** não foi adicionado alias colapsando `"trabalho"` e `"escritório"`; permanecem conceitos distintos, corrigindo apenas a duplicação redundante no gabarito de `case_id=29`.
- **Normalização de `"estética gamer"`:** adicionados em [`_RAW_ALIASES`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/userintent/evaluator.py#L11-L51) mapeamentos controlados (`"estetica gamer"`, `"estética gamer"`, `"estilo gamer"`, `"visual gamer"`, `"design gamer"`, `"aparencia gamer"`, `"aparência gamer"`, `"teclado gamer"`, `"mouse gamer"`, `"gamer"` -> `"estetica gamer"`), preservando intacto `"cadeira gamer"` e demais termos.
- **`"Photoshop"` vs. `"design"` e Separação Perfil / Uso / Preferência:** não foi criada equivalência artificial entre `"Photoshop"` e `"design"`; em `case_id=32`, o gabarito passou a refletir fielmente `profile_context: ["designer"]` e `use_cases: ["Photoshop", "edição de vídeo"]`.
- **Comparação de categoria no avaliador:** `evaluate_case` agora compara categorias via `normalize_label(...)` para respeitar os aliases canônicos já definidos em `_RAW_ALIASES`.

---

## 5. Correções P2 Implementadas

- **Trilha sanitizada por tentativa HTTP (`attempt_trail`):** registrada em `_GlobalGeminiModelsProxy` e exposta em `global_usage_and_limits.gemini.attempt_trail` com os campos: `scenario_id`, `attempt`, `operation`, `http_status`, `outcome`, `latency_ms`, `tokens`, `is_retry`, `backoff_seconds`, `interrupted`, `error_category` e `retry_after_seconds`.
- **Diagnóstico estruturado em falhas semânticas (`failed_fields`):** funções [`compute_failed_fields`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/userintent/evaluator.py) e `compute_informational_divergences` (para single-turn) e verificadores estruturados por campo em `_multiturn_specs` (para `M1..M8`) registram apenas o nome do campo e a categoria de divergência (ex.: `price_inclusivity_mismatch`, `missing_expected_items`), **sem armazenar respostas brutas do LLM ou prompts completos**.
- **Escrita atômica e preservação de progresso:** gravação via arquivo temporário `.tmp` seguido de `replace()`, sem deixar `.tmp` residual.
- **Remoção de campo artificial:** removida definitivamente a chave `set_f1_sanity` do relatório do runner.

---

## 6. Arquivos Alterados e Criados

| Arquivo | Tipo | Finalidade na Etapa 11.2.2 |
|---|---|---|
| [`src/userintent/llm.py`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/userintent/llm.py) | Alterado | `HttpRetryOptions(attempts=1)`, jitter opcional com `random_fn`, `retry_on_429`, callback `on_retry`, extração segura de status HTTP e `Retry-After` |
| [`src/userintent/schema.py`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/userintent/schema.py) | Alterado | Validador pós-modelo em `PriceConstraint` normalizando bounds nulos |
| [`src/userintent/evaluator.py`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/userintent/evaluator.py) | Alterado | `price_match` defensivo, aliases de estética gamer, categoria via `normalize_label`, `compute_failed_fields` e `compute_informational_divergences` |
| [`data/intent_eval_50.jsonl`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/data/intent_eval_50.jsonl) | Alterado | Correção fundamentada dos rótulos `case_id=3`, `case_id=29` e `case_id=32` |
| [`scripts/run_stage11_live_validation.py`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/scripts/run_stage11_live_validation.py) | Alterado | Pacing `--sleep-between-calls`, parada fatal em 429 vs retry em 503, backoff preservado, snapshots completos, 4 estados, `attempt_trail`, `failed_fields` e remoção de `set_f1_sanity` |
| [`tests/test_stage11_2_2_corrections.py`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/tests/test_stage11_2_2_corrections.py) | Criado | Suíte com 12 funções de teste cobrindo todos os 31 itens P0, P1 e P2 sem rede |
| [`RELATORIO_CORRECOES_GEMINI_11_2_2.md`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/RELATORIO_CORRECOES_GEMINI_11_2_2.md) | Criado | Relatório técnico final da Etapa 11.2.2 |

---

## 7. Alterações no Benchmark `data/intent_eval_50.jsonl`

Todas as demais 47 entradas do dataset (incluindo `case_id=21`, cujas flags vazias passam a ser tratadas pela normalização estrutural do `PriceConstraint`) permaneceram intactas. As 3 alterações realizadas foram:

| ID | Esperado Anterior | Esperado Novo | Razão Semântica | Regra do Sistema (`SYSTEM_PROMPT`) |
|---|---|---|---|---|
| `case_id: 3` | `"use_cases": ["fotografia"]` | `"use_cases": []` | O enunciado (*"com boa câmera e bateria longa"*) expressa característica preferida (`preferred_features`), não o caso de uso explícito de fotografar (`case_id: 15` e `30` mantêm `fotografia`). | Regras 1 e 3 (*extrair apenas o que foi declarado; não inventar uso não citado*). |
| `case_id: 29` | `"use_cases": ["trabalho", "escritório"]` | `"use_cases": ["escritório"]` | O texto diz explicitamente *"para escritório"* — exigir simultaneamente `"trabalho"` e `"escritório"` impunha redundância artificial e mascarava a distinção semântica entre trabalho e ambiente de escritório. | Regra 1 (*fidelidade estrita ao texto, sem duplicar termos redundantes*). |
| `case_id: 32` | `"use_cases": ["design", "edição de vídeo"]` | `"use_cases": ["Photoshop", "edição de vídeo"]` | O perfil profissional (*"Sou designer"*) já está capturado em `profile_context: ["designer"]`; o texto declara *"para Photoshop e edição de vídeo"`. Evita equiparar indevidamente o software `Photoshop` com a atividade ampla `design`. | Regra 1 e separação entre `profile_context`, `use_cases` e `preferred_features`. |

---

## 8. Novos Testes Adicionados e Resultados

Arquivo: [`tests/test_stage11_2_2_corrections.py`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/tests/test_stage11_2_2_corrections.py) — **12 testes aprovados, 0 falhas**, cobrindo integralmente os **31 itens obrigatórios**:

- **P0 (#1–#13):**
  - `test_p0_1_runner_preserves_exponential_backoff_without_half_second_override` (#1)
  - `test_p0_2_exponential_backoff_applies_deterministic_jitter_and_sdk_retry_attempts_1` (#2, #3)
  - `test_p0_3_runner_sdk_config_forces_single_sdk_attempt` (#3)
  - `test_p0_4_pacing_configurable_and_independent_of_retry_backoff` (#4, #31 CLI)
  - `test_p0_5_6_7_http_429_stops_immediately_vs_503_retries_and_captures_retry_after_safely` (#5, #6, #7)
  - `test_p0_8_9_budget_bounds_and_thread_safety_under_concurrency` (#8, #9)
  - `test_p0_10_11_12_13_partial_snapshots_four_states_and_zero_denominator` (#10, #11, #12, #13)
- **P1 (#14–#24):**
  - `test_p1_14_15_price_constraint_normalization_and_bounded_preservation` (#14, #15)
  - `test_p1_16_17_18_19_dataset_single_21_single_3_and_explicit_photography` (#16, #17, #18, #19)
  - `test_p1_20_21_22_23_24_semantic_distinctions_gamer_normalization_and_hard_rules` (#20, #21, #22, #23, #24)
- **P2 (#25–#31):**
  - `test_p2_25_to_31_attempt_trail_failed_fields_atomic_no_leaks_and_sanity_removed` (#25, #26, #27, #28, #29, #30, #31)
  - `test_p2_multiturn_failed_fields_and_summary_counts` (#11, #12, #26 multi-turn)

---

## 9. Resultados Completos da Suíte Offline e Comparação com Linha de Base

Execução após a última edição de código:

| Execução | Resultado | Comparação com Linha de Base (Etapa 11.1) |
|---|---|---|
| `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -m "not ebay_live" --cov=buscai --cov=userintent --cov-config=pyproject.toml --cov-report=term-missing -q` | **423 passed, 1 skipped, 2 deselected**, 18 warnings em `12.26s` | **+12 novos testes aprovados** (de 411 para 423), 0 regressões |
| `.\.venv\Scripts\pytest.exe -m "not ebay_live" -q` | **423 passed, 1 skipped, 2 deselected**, 18 warnings em `10.71s` | Consistente com `python -m pytest` |

- `1 skipped`: [`tests/integration/test_gemini_live.py`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/tests/integration/test_gemini_live.py) (execução real desabilitada conforme regra offline).
- `2 deselected`: testes marcados com `ebay_live` (chamadas reais à eBay desabilitadas conforme regra offline).

---

## 10. Cobertura de Código

- **Cobertura global (`buscai` + `userintent`):** **92%** (`6780` declarações, `547` ausentes).
- Módulos diretamente alterados:
  - `src/userintent/evaluator.py`: **97%** (`148` stmts, `4` miss)
  - `src/userintent/schema.py`: **96%** (`103` stmts, `4` miss)
  - `src/userintent/llm.py`: **92%** (`200` stmts, `17` miss)

---

## 11. Qualidade Estática (`ruff`, `mypy`, `pip check`, AST/Imports)

Todos os comandos abaixo executados após a última edição de código:

| Verificação | Comando | Resultado Observado |
|---|---|---|
| Lint (`ruff`) | `.\.venv\Scripts\ruff.exe check --no-cache src/ tests/ scripts/ --exclude src/buscai/app` | `All checks passed!` (0 erros) |
| Tipagem (`mypy`) | `.\.venv\Scripts\mypy.exe --cache-dir "$env:TEMP\buscai_mypy_cache" src/buscai src/userintent --exclude "src/buscai/app"` | `Success: no issues found in 68 source files` (0 erros) |
| Dependências (`pip check`) | `.\.venv\Scripts\python.exe -m pip check` | `No broken requirements found.` |
| AST + Importação | Parse AST e `importlib.import_module` em todos os arquivos de `src/` (excl. `src/buscai/app`) | `AST_FILES: 68 IMPORTED_MODULES: 54` (0 falhas) |
| Arquivos protegidos | `git diff --name-only -- src/buscai/app/ AUDITORIA.md .env` | Saída vazia (0 modificações) |

---

## 12. Auditoria de Segurança, Privacidade e Ausência de Chamadas Externas

- **Rede:** fixture `autouse` em [`tests/test_stage11_2_2_corrections.py`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/tests/test_stage11_2_2_corrections.py) e [`tests/test_stage11_1_preparation.py`](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/tests/test_stage11_1_preparation.py) utilizam `sys.addaudithook` bloqueando `urllib.Request` e qualquer `socket.connect` fora de loopback (`127.0.0.1` / `::1`). Nenhuma tentativa de rede externa foi registrada.
- **Segredos e cabeçalhos:** testes injetaram chaves fictícias e cabeçalhos `Authorization`/`X-Goog-Api-Key` em respostas HTTP simuladas com `429`/`503` e erros com segredos embutidos; comprovado que nenhum segredo, cabeçalho sensível, prompt de usuário ou resposta bruta do LLM aparece nos logs, `progress_stream` ou JSON `--output`.
- **`.env`:** não lido nem modificado; credenciais não expostas.

---

## 13. Riscos Remanescentes

1. **Comportamento real do Gemini sob cota ativa:** como nenhuma chamada real foi feita nesta etapa, a taxa real de sucesso do modelo `gemini-3.5-flash-lite` após as correções P0/P1 e a efetividade prática do pacing de `4.0` s contra limites RPM/RPD da conta só poderão ser medidas em uma nova validação controlada autorizada.
2. **Peso real do modelo de embeddings:** o modelo `intfloat/multilingual-e5-small` continua dependente de pesos locais indisponíveis neste ambiente offline, mantendo `DeterministicStubEmbeddingProvider` no bloco E2E.
3. **Rotação de chave histórica (pendência operacional já registrada na Etapa 11.1):** a chave presente no commit histórico `3b28fae` requer rotação manual pelo mantenedor fora do repositório.

---

## 14. Critérios de Prontidão Para Eventual Nova Validação Real

Uma nova execução controlada ao vivo está tecnicamente pronta e deve obedecer aos critérios abaixo (sem execução automática):

- **Pré-condição de cota:** executar somente após janela de reset RPM/TPM/RPD da chave Gemini e eventual rotação de credenciais históricas.
- **Execução recomendada com pacing:** usar `--sleep-between-calls 4.0` (ou superior) e teto explícito (`--max-gemini-calls 20` para Bloco A isolado ou `<= 30` para Bloco A+B), acompanhando parada automática caso ocorra HTTP `429`.
- **Critérios de aceitação ao vivo:** 0 vazamentos em logs/`--output`, `http_429_rate_limit` interrompendo imediatamente sem desperdício de chamadas, classificação completa em 4 estados nos 10+8 cenários e rastreabilidade completa em `attempt_trail` e `failed_fields`.

---

## 15. Próximo Passo Sugerido Para Aprovação do Usuário

Caso o usuário autorize explicitamente consumir cota externa da API Gemini após verificar a janela de cota da chave:

```powershell
$env:RUN_GEMINI_INTEGRATION_TESTS="1"
.\.venv\Scripts\python.exe -u scripts/run_stage11_live_validation.py --mode gemini --max-gemini-calls 20 --sleep-between-calls 4.0 --output .tmp_stage11_gemini_metrics_v2.json
```

*(Observação: manter `RUN_GEMINI_INTEGRATION_TESTS` não definido até autorização explícita; após a execução, analisar o arquivo JSON antes de disparar `--mode both`.)*

---

## 16. Conclusão Técnica Objetiva

Todas as correções **P0, P1 e P2** do diagnóstico Gemini foram implementadas, integradas de forma limpa sem violar fronteiras arquiteturais (`D1–D9`) nem arquivos protegidos (`src/buscai/app/`, `AUDITORIA.md`, `.env`), e comprovadas **100% offline** por meio dos 31 cenários exigidos, atingindo **423 testes aprovados**, **92% de cobertura**, **0 erros de `ruff`**, **0 erros de `mypy`** e **0 chamadas externas**. O sistema encontra-se em estado íntegro e pronto para aguardar autorização do usuário antes de qualquer nova validação real.
