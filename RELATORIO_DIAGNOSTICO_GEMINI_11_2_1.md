# Relatório de Diagnóstico Offline da Validação Real do Gemini — Etapa 11.2.1

- **Data:** 25/11/2026
- **Comando analisado:** `python -u scripts/run_stage11_live_validation.py --mode gemini --max-gemini-calls 20 --output .tmp_stage11_gemini_metrics.json`
- **Artefatos inspecionados (100% offline):** [`.tmp_stage11_gemini_metrics.json`](.tmp_stage11_gemini_metrics.json), [`scripts/run_stage11_live_validation.py`](scripts/run_stage11_live_validation.py), [`src/userintent/llm.py`](src/userintent/llm.py), [`src/userintent/schema.py`](src/userintent/schema.py), [`src/userintent/prompt.py`](src/userintent/prompt.py), [`src/userintent/evaluator.py`](src/userintent/evaluator.py), [`data/intent_eval_50.jsonl`](data/intent_eval_50.jsonl) e SDK `google-genai 1.75.0`.
- **Restrições preservadas:** nenhuma chamada externa executada, nenhum código de produção ou prompt alterado, nenhum segredo exibido, `.env` e `src/buscai/app/` intocados, sem commits ou push.

---

## 1. Resumo Executivo e Reconciliação da Execução

| Indicador observado em `.tmp_stage11_gemini_metrics.json` | Valor exato |
| :--- | :--- |
| Modelo configurado | `gemini-3.5-flash-lite` |
| Chamadas `generate_content` consumidas / teto configurado | **`20 / 20`** (`100%` do teto da execução) |
| Requisições lógicas iniciadas | **`13`** (`10` single-turn + `3` multi-turn: `M1`, `M2`, `M3`; `M4..M8` não executados) |
| Retries contabilizados | **`7`** |
| Chamadas com resposta válida e tokens reportados | **`8`** (`11.153` prompt + `1.605` output = **`12.758` tokens**, média `1.394,1` in / `200,6` out por resposta) |
| Erros HTTP capturados | **`12`** (`8 × ServerError:503` + `4 × ClientError:429`, `fatal_stop=false`) |
| Cenários single-turn planejados / concluídos / com erro | **`10` planejados / `7` concluídos / `3` com `LLMUnavailableError`** (`case_id` `4`, `6`, `26`) |
| `core_pass` nos cenários single-turn *(corrigido)* | **`3 / 7` concluídos (`42,86%`)** aprovados (`case_id` `1`, `2`, `15`); **`4 / 7` reprovados** (`case_id` `3`, `21`, `29`, `32`) *(ou `3 / 10 = 30,0%` dos planejados)* |
| Cenários multi-turn planejados / executados / erro | **`8` planejados / `3` iniciados (`M1` aprovado, `M2` e `M3` com `LLMUnavailableError`) / `5` não executados (`M4..M8`)** |
| Latência das 20 tentativas | Média `1995,17 ms`, `p50` `1233,88 ms`, `p95` `4650,52 ms`, mín `245,82 ms`, máx `12507,76 ms` |
| Chamadas eBay / vazamento de credenciais em logs | `0` chamadas eBay / `secret_leak_in_logs=false` |

### Reconciliação exata das 20 tentativas (sem extrapolar dados não gravados)
Sabemos pelos contadores exatos em `.tmp_stage11_gemini_metrics.json`:
- `calls_made = 20`, `logical_requests = 13`, `retries_counted = 7`, `tokens_reported_calls = 8` (sucessos: `7` single-turn + `M1` = `8` respostas), `errors` = `12` (`8 × 503` + `4 × 429`), e `5` requisições lógicas falharam com `LLMUnavailableError` (`single_4`, `single_6`, `single_26`, `M2`, `M3`), além de `max_retries = 1` no runner.
- Portanto, a partição matemática exata foi:
  1. **`6` requisições lógicas** concluídas na 1ª tentativa (`6` chamadas, `0` retries, `0` erros).
  2. **`2` requisições lógicas** falharam na 1ª tentativa (`503`) e recuperaram no único retry permitido (`4` chamadas, `2` retries, `2` erros `503`).
  3. **`5` requisições lógicas** (`single_4`, `single_6`, `single_26`, `M2`, `M3`) falharam na 1ª tentativa e falharam novamente no retry (`10` chamadas, `5` retries, `10` erros: `6 × ServerError:503` em `single_4/6/26` + `4 × ClientError:429` em `M2/M3`).
  - **Total:** `6 + 4 + 10 = 20` chamadas; `0 + 2 + 5 = 7` retries; `0 + 2 + 10 = 12` erros; `6 + 2 + 0 = 8` sucessos.

---

## 2. Investigação dos Erros HTTP (`503` e `429`), SDK e Retries

### 2.1. Quem executa retries (SDK vs. código do projeto)
- **SDK `google-genai 1.75.0`:** inspeção direta de `google.genai._api_client.BaseApiClient` e `genai.Client(api_key=...)._api_client` comprovou que:
  - Por padrão, `HttpOptions().retry_options` é `None` (`client._api_client._http_options.retry_options is None`).
  - `retry_args(None)` retorna `{'stop': tenacity.stop_after_attempt(1), 'reraise': True}` — **o SDK NÃO executa retries internos por padrão nesta versão**.
  - Contudo, se futuramente `HttpOptions(retry_options=...)` for passado ou o default do SDK mudar em atualização de pacote, `BaseApiClient._retry` executará até `5` tentativas internas (`_RETRY_ATTEMPTS=5`, `_RETRY_INITIAL_DELAY=1.0`, `_RETRY_MAX_DELAY=60.0`, `_RETRY_HTTP_STATUS_CODES=(408, 429, 500, 502, 503, 504)`).
- **`IntentExtractor` (`src/userintent/llm.py:121, 193-198, 215-263`):**
  - Implementa seu próprio loop de retry com `max_retries=2` (default de produção; `max_retries=1` passado pelo runner), timeout de `15s` (`timeout_ms = 15000`), tratando `(408, 429, 500, 502, 503, 504)` + erros `httpx`/`TimeoutError`.
  - **Defeito de blindagem no projeto:** `IntentExtractor.extract()` cria `types.HttpOptions(timeout=timeout_ms)` sem fixar `retry_options=types.HttpRetryOptions(attempts=1)`, deixando implícita a desativação de retries na camada SDK.
- **Conclusão sobre contabilização real:** na execução com `google-genai 1.75.0`, todos os `7` retries foram gerados por `IntentExtractor`, portanto **todas as 20 tentativas HTTP reais foram contabilizadas por `_GlobalGeminiModelsProxy`**. Porém, estruturalmente, interceptar apenas `client.models.generate_content` (sem passar `retry_options=attempts=1` nem envelopar `HttpOptions(httpx_client=...)`) não impediria retries ocultos caso o SDK tivesse retries habilitados.

### 2.2. Defeitos concretos no runner e no extrator que amplificam `503` e `429`
1. **Descarte silencioso do backoff exponencial no runner ([`scripts/run_stage11_live_validation.py:621`](scripts/run_stage11_live_validation.py#L621)):**
   - `IntentExtractor` calcula `delay = min(max_backoff, base_backoff * (2 ** retry_attempt))`, porém o runner injeta `sleep=lambda _: None if gemini_client is not None else time.sleep(0.5)`.
   - Efeito real: em vez de respeitar o backoff exponencial, todo retry espera apenas **0,5s fixo** mesmo diante de `503` ou `429`, reenviando a carga quase imediatamente sobre o servidor já saturado/limitado.
2. **Ausência de jitter, diferenciação de `429` e leitura de `Retry-After` (`src/userintent/llm.py:235-251`):**
   - `IntentExtractor._generate_with_retry()` trata `429` identicamente a `503`, sem jitter e sem extrair eventual `Retry-After` da resposta HTTP.
3. **Ausência de pacing entre casos no runner (`_run_gemini_eval`):**
   - As requisições sequenciais são disparadas em rajada imediata sem intervalo mínimo entre cenários (`20` chamadas em ~`40s` efetivos, somando `12.758` tokens reportados apenas nos 8 sucessos).
4. **Desperdício de cota ao não interromper em `429` ([`scripts/run_stage11_live_validation.py:83-84`](scripts/run_stage11_live_validation.py#L83-L84)):**
   - `fatal_stop` interrompe somente em `{400, 401, 403, 404}`. Ao receber `429` em `M2`, o runner ainda executou o retry de `M2`, a tentativa de `M3` e o retry de `M3`, consumindo **4 chamadas consecutivas** todas rejeitadas com `ClientError:429`.

### 2.3. Sobre a causa dos erros `429` (evidência vs. pontos não confirmáveis)
- **O que é observável:** os erros ocorreram em sequência (`8 × ServerError:503` durante `single_1..32` seguidos de `4 × ClientError:429` no bloco `M2..M3`), sem pausa entre chamadas e com `sleep(0.5)` fixo nos retries.
- **O que NÃO pôde ser confirmado pelos dados salvos:** `.tmp_stage11_gemini_metrics.json` não registrou o corpo/detalhe sanitizado de `RESOURCE_EXHAUSTED` (ex.: `quotaId`), o cabeçalho `Retry-After` nem os timestamps individuais por chamada. Assim, **não é possível afirmar com certeza** se o `429` foi disparado por **RPM** (rajada rápida sem pacing), **TPM** (~`1.394` prompt tokens por chamada pelo peso do `SYSTEM_PROMPT` + schema JSON estruturado), **RPD** (acumulado com a execução anterior `task-525` no mesmo dia) ou limitação temporária do endpoint/preview `gemini-3.5-flash-lite` que já apresentava `8` erros `503`.

---

## 3. Investigação das Divergências Semânticas (`single_3`, `single_21`, `single_29`, `single_32`)

> **Limitação diagnóstica observada:** `.tmp_stage11_gemini_metrics.json` salvou apenas os escores por campo (`CaseResult`), sem gravar os valores estruturados preditos por campo. As causas abaixo combinam os escores exatos do JSON com inspeção estática do dataset, `evaluator.py`, `schema.py` e `prompt.py`.

### 3.1. `single_21` e `single_29` — `price_correct = false` em 100% dos casos sem orçamento
- **Evidência quantitativa:** nos `5` casos concluídos com preço explícito (`1, 2, 3, 15, 32`), `price_correct` foi `5/5` (`100%`) e `mean_budget_abs_error_brl` foi **`0,0`**. Nos exatos `2` casos concluídos **sem preço** (`21` e `29`), `price_correct` foi **`0/2`** (`0%`).
- **Causa raiz comprovada (`evaluator.py:98-101` + `schema.py:59-63` + `prompt.py:13`):**
  - `price_match()` compara todas as 5 chaves de `PriceConstraint`: `min_price`, `max_price`, `min_inclusive`, `max_inclusive`, `is_hard_constraint`.
  - Mesmo quando `min_price=None` e `max_price=None`, `data/intent_eval_50.jsonl` exige `min_inclusive=true`, `max_inclusive=true`, `is_hard_constraint=false`.
  - Já a Regra 2 do [`prompt.py`](src/userintent/prompt.py#L13) diz apenas: *"Se não houver valor nem moeda, deixe price com null e currency=null"*, sem instruir os 3 booleanos auxiliares quando `min_price`/`max_price` são `null`, e `PriceConstraint` ([`schema.py:61-63`](src/userintent/schema.py#L61-L63)) não normaliza `min_inclusive`/`max_inclusive`/`is_hard_constraint` para seus defaults quando ambos os limites numéricos são `null`. Qualquer divergência do LLM em um desses booleanos (por exemplo `min_inclusive=false` ou `is_hard_constraint=true`) reprova `price_correct` e, consequentemente, `core_pass`. *(Qual dos 3 booleanos divergiu em `21` e `29` não pôde ser confirmado sem log dos campos preditos).*

### 3.2. `single_3` — `use_cases_f1 = 0.0` (demais campos `1.0`)
- **Entrada (`case_id: 3`):** `"Quero um celular Samsung até R$ 2.500 com boa câmera e bateria longa."`
- **Esperado no dataset:** `use_cases=["fotografia"]` e `preferred_features=["boa câmera", "bateria longa"]`.
- **Causa raiz comprovada (conflito direto entre dataset e `SYSTEM_PROMPT`):**
  - A Regra 1 de [`prompt.py`](src/userintent/prompt.py#L6) ordena *"Extraia somente informações presentes na mensagem. Nunca invente..."*, e a Regra 3 cita explicitamente `"boa câmera"` e `"boa bateria"` como exemplos de termos qualitativos para `preferred_features` (campo onde o Gemini obteve **`1,0`**), enquanto a Regra 7 traz exemplos explícitos de finalidade (`programação, estudos, trabalho, jogos, fotografia...`).
  - Como o texto disse apenas *"com boa câmera e bateria longa"* (não *"para fotografia"*), o Gemini seguiu literalmente as Regras 1 e 3 e deixou `use_cases=[]` (resultando em `use_cases_f1 = 0.0 < 0.75`), reprovando `core_pass` apesar de extrair perfeitamente categoria, marca, preço (`<= 2500` hard BRL) e `preferred_features`. **É primariamente um problema de incoerência no rótulo do benchmark/regras de avaliação.**

### 3.3. `single_21` — `profile_f1 = 0.0` (além de `price_correct = false`)
- **Entrada (`case_id: 21`):** `"Quero um presente para meu pai."`
- **Esperado no dataset:** `category=null`, `profile_context=[]`, `clarification_needed=true`.
- **Evidência observada:** `category_correct=true`, `clarification_correct=true`, `clarification_question_ok=true`, mas `profile_f1=0.0` (em `set_f1([], []) == 1.0`, logo o modelo retornou pelo menos 1 item não vazio em `profile_context`, possivelmente `"presente para meu pai"` ou `"pai"`).
- **Causa raiz:** a Regra 8 do prompt (`profile_context guarda contexto útil explicitamente informado pelo usuário...`) não distingue o perfil do comprador de um destinatário de presente quando não há produto nem uso definido. Embora `profile_f1` não entre em `core_pass` (a reprovação em `core_pass` no caso `21` ocorreu exclusivamente por `price_correct=false`), trata-se de ambiguidade menor de prompt/rótulo.

### 3.4. `single_29` — `price_correct = false`, `use_cases_f1 = 0.6667`, `excluded_features_f1 = 0.5`
- **Entrada (`case_id: 29`):** `"Quero um teclado sem fio para escritório, mas não gosto de teclado gamer cheio de RGB."`
- **Esperado no dataset:** `use_cases=["trabalho", "escritório"]`, `required_features=["sem fio"]`, `excluded_features=["RGB", "estética gamer"]`, `price` sem bounds.
- **Causas identificadas no benchmark e avaliador (`evaluator.py` + `intent_eval_50.jsonl`):**
  1. **`price_correct = false`:** mesma falha estrutural de booleanos de `PriceConstraint` sem bounds (Seção 3.1).
  2. **`use_cases_f1 = 0.6667` (`< 0.75` exigido em `core_pass`):** com `expected=["trabalho", "escritório"]` (2 itens), o único modo matemático de obter `F1 = 2/3` (`2 * 1 * 1 / (1 + 2)`) é o modelo retornar **exatamente 1 dos 2** termos (ex.: apenas `"escritório"` ou apenas `"trabalho"`). O dataset espera simultaneamente o sinônimo canônico e o termo literal, e [`evaluator.py:_RAW_ALIASES`](src/userintent/evaluator.py#L11-L39) **não mapeia `"escritório"` <-> `"trabalho"`**.
  3. **`excluded_features_f1 = 0.5` (`< 0.75` exigido em `core_pass`):** com `expected=["RGB", "estética gamer"]` (`n=2`), `F1 = 0.5` ocorre quando há `TP=1` em `|P|=2` e `|E|=2` (1 item acertado — ex. `"RGB"` — e o 2º item formulado de outra forma válida como `"gamer"` ou `"teclado gamer"` em vez da paráfrase literal `"estética gamer"`). Como `set_f1` compara por correspondência exata de string normalizada, qualquer paráfrase de `"estética gamer"` sofre penalidade dupla (falso positivo + falso negativo -> `F1 = 0.5`).

### 3.5. `single_32` — `use_cases_f1 = 0.6667`, `preferred_features_f1 = 0.0`
- **Entrada (`case_id: 32`):** `"Sou designer e quero um notebook para Photoshop e edição de vídeo, posso passar um pouco de R$ 6.000."`
- **Esperado no dataset:** `category="notebook"`, `price` flexível (`max_price=6000, is_hard_constraint=false`), `use_cases=["design", "edição de vídeo"]`, `profile_context=["designer"]`, `preferred_features=[]`.
- **Observação positiva:** o modelo acertou `price_correct=true` (preservando corretamente `"posso passar um pouco"` como `is_hard_constraint=false`), `profile_f1=1.0`, `category_correct=true` e `query_similarity=1.0`.
- **Causas identificadas:**
  1. **`use_cases_f1 = 0.6667` (`< 0.75`, causa da reprovação no `core_pass`):** matematicamente (`TP=1` em `|P|=1, |E|=2`, ou `TP=2` em `|P|=4`), o caso típico é o modelo retornar apenas `["edição de vídeo"]` (omitindo `"design"` por já tê-lo capturado em `profile_context=["designer"]`), ou retornar `["photoshop", "edição de vídeo"]` quando [`evaluator.py`](src/userintent/evaluator.py#L11-L39) **não possui alias `"photoshop" -> "design"`**.
  2. **`preferred_features_f1 = 0.0` (não bloqueia `core_pass`):** indica emissão de alguma preferência implícita não vazia (não determinável sem o JSON predito), onde o gabarito espera `[]`.

---

## 4. Auditoria da Avaliação Multiturno (`multiturn_pass_rate = 1.0`)

- **Defeito comprovado em [`scripts/run_stage11_live_validation.py:508,546`](scripts/run_stage11_live_validation.py#L508):**
  ```python
  completed_multi = [r for r in multi_results if "passed" in r]
  ...
  "multiturn_total": len(multi_results),
  "multiturn_pass_rate": round(sum(1 for r in completed_multi if r["passed"]) / len(completed_multi), 4)
  ```
- **Por que o número é enganoso:**
  1. `M2` e `M3` encerraram com `{"id": ..., "error": "LLMUnavailableError"}` (sem chave `"passed"`), sendo silenciosamente excluídos do denominador `len(completed_multi)`.
  2. `M4` a `M8` (`5` cenários) foram interrompidos pelo teto global de `20` chamadas e sequer entraram em `multi_results`.
  3. Assim, embora apenas **`1` de `8` (`12,5%`)** cenários multi-turn planejados tenha sido concluído com sucesso (`M1`), e **`2` de `3` iniciados (`66,7%`)** tenham falhado por `LLMUnavailableError`, o relatório emitiu `multiturn_pass_rate = 1.0` (`1/1`) sem nenhuma flag de avaliação parcial/inconclusiva.
  - *(Nota: o mesmo padrão se repete em `single_turn_core_pass_rate = 0.4286`, calculado sobre `7` concluídos em vez de `10` planejados, deixando a exclusão dos `3` erros pouco explícita nas taxas).*

### Estrutura recomendada para consolidação de cenários (single-turn e multi-turn)
Cada cenário deve registrar explicitamente um campo `"status"` dentre **`"passed" | "failed" | "error" | "not_run"`**, incluindo todos os cenários planejados na lista (com `"not_run"` para os não executados), e reportar separadamente:
- Contagens absolutas: `planned_total`, `passed_count`, `failed_count`, `error_count`, `not_run_count`, `completed_count` (`passed + failed`).
- Taxas distintas:
  - `completion_rate = completed_count / planned_total`
  - `pass_rate_over_planned = passed_count / planned_total`
  - `pass_rate_over_completed = passed_count / completed_count` (com flag booleana `is_conclusive = (error_count == 0 and not_run_count == 0)`).

---

## 5. Auditoria de `.tmp_stage11_gemini_metrics.json` e `ProgressiveMetricsRecorder`

- **O que funcionou bem:**
  - Como `run_validation` atingiu normalmente o bloco `finally:`, `recorder.flush_final()` gravou `global_usage_and_limits` completo, reconciliando exatamente `20` chamadas, `13` requisições lógicas, `7` retries, `8` respostas com tokens e `12` erros sem vazar segredos nem acionar a eBay.
- **Falhas estruturais encontradas no registro progressivo:**
  1. **Snapshots intermediários incompletos:** durante o loop (`record_step()`), `report["global_usage_and_limits"]` permanece `{}` até o `finally:` final ([`scripts/run_stage11_live_validation.py:720-750`](scripts/run_stage11_live_validation.py#L720-L750)). Se o processo sofresse interrupção abrupta (`SIGKILL`/queda), o arquivo `.tmp_stage11_gemini_metrics.json` teria os resultados individuais, mas perderia `calls_made`, `retries_counted`, `tokens` e `errors`.
  2. **Eventos progressivos não preservados no JSON:** `recorder.events` fica apenas em memória e foi emitido em `stderr`, não sendo armazenado no arquivo `--output`, dificultando correlacionar posteriormente cada `503`/`429` a cada cenário e tentativa.
  3. **Lista `errors` sem contexto:** grava apenas `["ServerError:503", ..., "ClientError:429"]` sem `scenario_id`, `attempt`, timestamp ou `retry_after`.
  4. **Cenários não executados omitidos:** `M4..M8` ausentes em `multiturn_results`.
  5. **Ausência de sumário estruturado dos campos divergentes:** sem diagnóstico por campo nos erros semânticos (`3, 21, 29, 32`), não é possível distinguir se `price_correct=false` decorreu de `min_inclusive`, `max_inclusive` ou `is_hard_constraint` (mantendo o cuidado de nunca persistir conteúdo sensível nem dados da eBay).
  6. **Campo `set_f1_sanity` desnecessário** no payload final do relatório.

---

## 6. Classificação por Origem e Recomendações Priorizadas

### P0 — Bloqueiam nova execução confiável (corrigir antes de chamar a API novamente)
1. **Respeitar backoff exponencial + jitter no runner e diferenciar `429` de `503`:**
   - Remover a sobrescrita `sleep=time.sleep(0.5)` em [`scripts/run_stage11_live_validation.py`](scripts/run_stage11_live_validation.py); preservar o backoff exponencial calculado por `IntentExtractor` com adição de jitter e leitura de `Retry-After` quando presente em erros `429`/`503`.
   - **Não aumentar retries como solução para `429`:** em validação controlada de cota rígida, ao receber `429` ativar **parada imediata** (ou backoff longo único somente se houver `Retry-After` explícito dentro do orçamento) em vez de queimar chamadas seguidas em rate limit.
2. **Adicionar pacing entre cenários no runner:**
   - Introduzir intervalo configurável entre chamadas lógicas (ex.: `4.0s` a `5.0s` com pequeno jitter) para evitar rajadas de `~28 RPM` contra o tier gratuito/limitado da API Gemini.
3. **Blindar contabilização HTTP do SDK:**
   - Em `IntentExtractor.extract()` ([`src/userintent/llm.py`](src/userintent/llm.py)), fixar explicitamente `retry_options=types.HttpRetryOptions(attempts=1)` dentro de `GenerateContentConfig.http_options` para garantir em contrato que retries jamais ocorram ocultamente dentro do SDK `google-genai`.
4. **Corrigir agregação `passed/failed/error/not_run` e snapshots completos:**
   - Atualizar `global_usage_and_limits` a cada `record_step()`, incluir `M4..M8` como `not_run` quando não executados e corrigir `multiturn_pass_rate` para não reportar `1.0` quando houver cenários em `error` ou `not_run`.

### P1 — Prejudicam a qualidade semântica e a fidelidade do avaliador
1. **Normalizar booleanos de `PriceConstraint` sem limites numéricos:**
   - Tratar a inconsistência entre `PriceConstraint` / `SYSTEM_PROMPT` Regra 2 / `evaluator.price_match()` quando `min_price is None and max_price is None`: em [`src/userintent/schema.py`](src/userintent/schema.py) (normalizando para `min_inclusive=True, max_inclusive=True, is_hard_constraint=False` quando não há `min_price` nem `max_price`) **ou** em `evaluator.price_match()` (não penalizar flags de inclusividade quando ambos os limites numéricos são `None`).
2. **Alinhar rótulos do benchmark e aliases do avaliador (sem vazar overfitting):**
   - Caso `3`: alinhar o rótulo em `data/intent_eval_50.jsonl` ou a regra do prompt sobre `"boa câmera"` -> `fotografia` (hoje o dataset contradiz diretamente a Regra 1 + Regra 3 do prompt).
   - Caso `29` e `32`: expandir aliases semânticos determinísticos em `src/userintent/evaluator.py` (ex.: `"escritório"` / `"trabalho"`; `"photoshop"` / `"design"`; aceitar sinônimo direto em `excluded_features` quando equivalente semântico como `"gamer"` / `"estética gamer"`).
   - Caso `21`: após confirmar os valores literais preditos em diagnóstico futuro, avaliar esclarecimento curto na Regra 8 sobre não classificar destinatário de presente genérico como `profile_context` do comprador.

### P2 — Melhorias de métricas, relatórios e observabilidade
1. Registrar no snapshot `.json` do runner a trilha resumida por tentativa (`scenario_id`, `attempt`, `status`, `http_code`, `error_code`, `retry_after_s`, `latency_ms`, `tokens`) além de uma lista resumida de **campos divergentes** (ex.: `failed_fields: ["price", "use_cases"]` e o nome do critério, sem armazenar respostas textuais completas ou segredos).
2. Remover `set_f1_sanity` do relatório JSON.
3. Registrar explicitamente o parâmetro `--sleep-between-calls` no runner para reprodutibilidade.

---

## 7. Plano de Nova Validação Controlada (Mediante Aprovação Prévia)

> **Pré-condição obrigatória:** implementar e validar offline os itens **P0** (e **P1** aprovados) antes de qualquer nova chamada à API Gemini, sem elevar o teto de `30` chamadas.

1. **Fase A — Sonda curta de estabilidade do provedor (`4` a `6` chamadas, `--max-gemini-calls 6`):**
   - Executar apenas uma amostra pequena (ex.: `case_id` `3, 21, 29, 32` + `M2, M3`) com **pacing de `5s` entre requisições** e **`max_retries=0`** (ou parada imediata em `429`/`503` persistente), confirmando redução de `503`/`429`, comportamento correto dos campos semânticos corrigidos e registro completo por tentativa.
2. **Fase B — Bateria completa controlada (`<= 24` chamadas restantes se Fase A estabilizar, mantendo teto global <= 30):**
   - Caso a Fase A confirme estabilidade do endpoint `gemini-3.5-flash-lite`, executar `--mode gemini` (`18` cenários com pacing `>= 4s`) e, havendo saldo dentro de `30`, `--mode e2e` (`5` cenários integrados com eBay limitada a `<= 5` chamadas Browse).
   - Se `429` ou `503` em série persistirem na Fase A, interromper imediatamente e avaliar troca configurável de `GEMINI_MODEL` ou janela de quota antes de consumir o restante do orçamento.
