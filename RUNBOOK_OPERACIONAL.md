# Runbook Operacional — BUSCAAI Backend API

Guia prático de operação em produção para o serviço HTTP **BUSCAAI** (`buscai.api.app:app`).
**Princípio fundamental:** o BUSCAAI é stateless, não persiste anúncios da eBay nem embeddings em disco/banco e nunca deve ter segredos gravados em arquivos versionados ou imagens.

---

## 1. Inicialização e Encerramento

### 1.1 Inicialização padrão (Contêiner)
- Comando de entrada definido no [`Dockerfile`](Dockerfile):
  ```bash
  uvicorn buscai.api.app:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers
  ```
- **Pré-requisitos no boot:**
  - `BUSCAAI_EMBEDDING_PRELOAD=true`
  - `BUSCAAI_EMBEDDING_LOCAL_FILES_ONLY=true`
  - `BUSCAAI_EMBEDDING_CACHE_DIR=/opt/buscai/model_cache` (pré-populado no build da imagem)
  - Credenciais injetadas pelo gestor de segredos: `GEMINI_API_KEY`, `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `BUSCAAI_API_KEYS`.
- **Probes recomendadas:**
  - `startupProbe`: HTTP `GET /v1/health` tolerando `>= 45–60 s` (cold start do E5 + PyTorch em CPU observado em `~9.7 s` em cache limpo e até `~22–44 s` em inicialização fria de runtime).
  - `readinessProbe`: HTTP `GET /v1/health` (aceitar apenas HTTP `200`).

### 1.2 Encerramento gracioso (`SIGTERM`)
- Ao receber `SIGTERM` / `SIGINT`, o lifespan FastAPI interrompe novas conexões e executa `AppContainer.close()`, fechando clientes `httpx.Client` (`EbayOAuthClient`, `EbayLiveBackend`) e liberando os pesos do modelo de embeddings via `unload()`.
- Configure `terminationGracePeriodSeconds >= 30s` para permitir conclusão de buscas em andamento antes do encerramento forçado.

---

## 2. Verificação de Health e Readiness

Execute periodicamente ou durante incidentes:

```bash
curl -sS -w "\nHTTP %{http_code}\n" http://127.0.0.1:8000/v1/health
```

- **HTTP `200` (`readiness: "ready"`):** todos os componentes obrigatórios (`api_auth`, `search_orchestrator`, `embeddings`, `tracing_policy`) estão prontos.
- **HTTP `503` (`readiness: "not_ready"`):** verifique no array `components` qual componente falhou:
  - `api_auth = not_ready`: `BUSCAAI_API_KEYS` ausente, vazia ou contendo apenas placeholders.
  - `search_orchestrator = not_ready`: `GEMINI_API_KEY`, `EBAY_CLIENT_ID` ou `EBAY_CLIENT_SECRET` ausentes, inválidos (placeholders) ou erro na construção do cliente.
  - `embeddings = not_ready`: modelo E5 não carregado em memória (`BUSCAAI_EMBEDDING_PRELOAD=false` ou falha ao ler `BUSCAAI_EMBEDDING_CACHE_DIR`).
  - `tracing_policy = not_ready`: variável proibida de tracing ativa no ambiente (`LANGSMITH_TRACING`, `LANGCHAIN_TRACING_V2`, etc.); remova imediatamente do ambiente e reinicie.

---

## 3. Rotação de `BUSCAAI_API_KEYS`

Como a autenticação aceita múltiplas chaves separadas por vírgula em `BUSCAAI_API_KEYS`:

1. **Janela de transição (zero downtime):** adicione a nova chave mantendo a antiga na variável:
   `BUSCAAI_API_KEYS=<chave_atual>,<chave_nova>`
2. Realize o rollout controlado das réplicas (uma por vez, aguardando `GET /v1/health` retornar `200`).
3. Atualize os clientes B2B consumidores para enviar a `<chave_nova>` no header `X-API-Key`.
4. Após confirmar nos logs/acessos dos clientes que a `<chave_atual>` não está mais em uso, atualize `BUSCAAI_API_KEYS=<chave_nova>` no gestor de segredos e faça novo rollout.
5. **Em caso de vazamento/comprometimento de chave:** remova imediatamente a chave comprometida de `BUSCAAI_API_KEYS` e reinicie todas as réplicas; a invalidação é imediata após o restart.

---

## 4. Atualização da Taxa `USD/BRL`

O BUSCAAI usa taxa configurável por ambiente (`USD_BRL_RATE`), sempre sinalizada como estimativa na API (`is_estimate=true`).

1. Atualize no gestor de configuração/ambiente:
   - `USD_BRL_RATE=<nova_taxa_decimal>` (ex.: `5.65`, sempre `> 0`)
   - `USD_BRL_RATE_UPDATED_AT=<YYYY-MM-DD>`
   - `USD_BRL_RATE_SOURCE=env_fixed_rate` (ou identificador da fonte autorizada)
2. Reinicie as réplicas em rolling update e valide em uma busca ou no campo `exchange_rate` do `GET /v1/health` / `exchange_rate_info` de `POST /v1/search`.
3. **Nunca** configure `USD_BRL_RATE <= 0` ou valores não numéricos (`NaN`/`Inf`), pois `load_settings()` falhará no boot por validação estrita.

---

## 5. Tratamento de Falhas Gemini

- **Sintomas HTTP para o cliente:**
  - `503 llm_unavailable`: timeout (`> 15 s` após retries) ou indisponibilidade transitória (HTTP `429`/`503`/`504`) do Google Gemini.
  - `502 llm_error`: resposta vazia/malformada ou falha de autenticação/permissão (`401`/`403`) no Gemini.
- **Ações de diagnóstico:**
  1. Filtre logs por `stage == "intent_extraction"` e `error_type`.
  2. Se ocorrer `IntentExtractionError` imediatamente após deploy/rotação, valide `GEMINI_API_KEY` e `GEMINI_MODEL` no ambiente (sem imprimir o valor da chave).
  3. Se ocorrer pico de `LLMUnavailableError` com `status/429`, verifique cota/limites do projeto no Google AI Studio / Vertex e ajuste temporariamente `BUSCAAI_RATE_LIMIT_PER_MINUTE` se houver abuso de clientes.

---

## 6. Tratamento de Falhas eBay

- **Sintomas HTTP para o cliente:**
  - `503 upstream_rate_limit` (com header `Retry-After` quando fornecido): HTTP `429` da eBay ou exaustão do `QuotaGuard` diário (`EBAY_DAILY_QUOTA_LIMIT`).
  - `503 upstream_timeout` / `upstream_unavailable`: timeout (`5s connect / 12s read`) ou HTTP `5xx` na eBay após até `2` retries.
  - `502 upstream_auth_error`: falha persistente de OAuth (`401` após renovação automática única ou `403`).
- **Ações de diagnóstico:**
  1. Inspecione logs com `stage` em `{"ebay_oauth", "ebay_search", "ebay_get_item"}`.
  2. Se houver `ebay_oauth_auth_rejected` / `ProviderAuthError`, verifique validade e escopo de `EBAY_CLIENT_ID` e `EBAY_CLIENT_SECRET` para produção (`api.ebay.com`).
  3. Se houver `QuotaGuard` esgotado, confirme `EBAY_DAILY_QUOTA_LIMIT` dividido corretamente entre réplicas (ver Seção 9).

---

## 7. Verificação do Modelo E5 (`intfloat/multilingual-e5-small`)

- **No contêiner / host offline:**
  1. Verifique se `BUSCAAI_EMBEDDING_PRELOAD=true` e `BUSCAAI_EMBEDDING_LOCAL_FILES_ONLY=true`.
  2. Confirme a existência do cache no diretório configurado (`BUSCAAI_EMBEDDING_CACHE_DIR`, ex.: `/opt/buscai/model_cache`).
  3. Execute teste pontual de carga e inferência local sem rede:
     ```bash
     BUSCAAI_EMBEDDING_LOCAL_FILES_ONLY=true python scripts/preload_embedding_model.py
     ```
  4. Resultado esperado: saída JSON com `"event": "embedding_preload_verified"`, `"dimension": 384`, `"local_files_only": true`.

---

## 8. Diagnóstico de Consumo Elevado de Memória

- **Linha de base esperada:** `~1.3 GB` logo após cold start e `~1.5–1.6 GB` RSS em regime normal para **1 worker**.
- **Causas comuns de consumo acima de `3 GB` por contêiner:**
  1. **Múltiplos workers no mesmo contêiner:** verificar se o comando de inicialização foi alterado de `--workers 1` para `--workers N` (multiplica o modelo E5 `N` vezes na RAM).
  2. **Limite de corpo HTTP elevado:** conferir `BUSCAAI_MAX_REQUEST_BODY_BYTES` (padrão seguro `65536`).
  3. **Múltiplos clientes distintos no rate limiter:** o `InMemoryRateLimiter` limita a `10.000` buckets em memória, consumo desprezível frente ao PyTorch, mas verificar se há picos anômalos de payload.
- **Ação:** garanta `uvicorn --workers 1`, limite mínimo de `4 GB RAM` por réplica e acione restart gracioso da réplica se RSS aproximar-se do limite do contêiner.

---

## 9. Rate Limiting e Cota Diária eBay

- **Escopo por processo:** tanto `InMemoryRateLimiter` (`BUSCAAI_RATE_LIMIT_PER_MINUTE`) quanto `QuotaGuard` (`EBAY_DAILY_QUOTA_LIMIT`) mantêm contadores na memória local de cada processo (sem Redis/DB).
- **Regra obrigatória para múltiplas réplicas (`R` réplicas):**
  - Divida a cota diária global da conta eBay pelo número de réplicas:
    `EBAY_DAILY_QUOTA_LIMIT_POR_REPLICA = floor(5000 / R)` (ex.: com `2` réplicas, configure `EBAY_DAILY_QUOTA_LIMIT=2500`; com `4` réplicas, `1250`).
  - Lembre-se de que o limite por `X-API-Key` efetivo máximo em balanceamento uniforme será aproximadamente `R × BUSCAAI_RATE_LIMIT_PER_MINUTE`.

---

## 10. Recuperação Após Falha

| Cenário | Recuperação Padrão |
|---|---|
| **Crash / OOM Kill do worker** | O orquestrador reinicia a réplica; `readinessProbe` (`/v1/health`) mantém tráfego fora da réplica por ~`10–45 s` até o E5 finalizar o preload local (`BUSCAAI_EMBEDDING_PRELOAD=true`). |
| **Token OAuth expirado/revogado** | Automático: `EbayLiveBackend` invalida o token em memória ao receber HTTP `401` e `EbayOAuthClient` emite novo token na próxima tentativa. |
| **Falha no startup por placeholder/credencial** | Corrija a variável no gestor de segredos e reinicie a réplica; nenhuma alteração de estado em disco precisa ser limpa (serviço stateless). |
| **Variável de tracing externo ativada acidentalmente** | Remova variáveis `LANGSMITH_*` / `LANGCHAIN_TRACING*` do ambiente e reinicie imediatamente; `/v1/health` voltará automaticamente a `200 ready`. |
| **Degradação severa de latência CPU sob pico** | Verifique `OMP_NUM_THREADS` / `TORCH_NUM_THREADS` (iniciar com `4`), escale horizontalmente adicionando réplicas (`--workers 1`) respeitando o rateio de `EBAY_DAILY_QUOTA_LIMIT`, e ajuste `BUSCAAI_RATE_LIMIT_PER_MINUTE` se necessário. |
