# Guia de Testes Postman — Etapa 8 (API FastAPI BUSCAAI)

Este guia descreve como subir a API HTTP do **BUSCAAI**, importar a coleção Postman oficial e validar todos os cenários da **Etapa 8** sem expor credenciais.

---

## 1. Coleção Importável

- Arquivo: [`postman/BUSCAAI_Etapa_8.postman_collection.json`](postman/BUSCAAI_Etapa_8.postman_collection.json)
- Variáveis configuradas na coleção:
  - `base_url`: `http://127.0.0.1:8000`
  - `api_key`: `""` *(vazia por segurança — preencha apenas localmente no Postman)*

> **Regra de segurança:** nunca salve credenciais reais dentro do arquivo JSON da coleção nem em commits do Git.

---

## 2. Como Subir a API Localmente

### 2.1. Configurar variáveis de ambiente no shell (sem versionar segredos)

No Windows PowerShell, ative o `.venv` e configure localmente as variáveis necessárias para o processo do servidor (ou utilize seu `.env` local não versionado):

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "src"
$env:BUSCAAI_API_KEYS = "sua_chave_local_forte"
$env:BUSCAAI_RATE_LIMIT_PER_MINUTE = "30"
$env:BUSCAAI_EMBEDDING_PRELOAD = "true"
```

> Para validar o comportamento de `not_ready` em `/v1/health`, inicie com `BUSCAAI_EMBEDDING_PRELOAD="false"` ou sem `GEMINI_API_KEY` / `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET`.

### 2.2. Iniciar o Uvicorn

```powershell
.\.venv\Scripts\uvicorn.exe buscai.api.app:app --host 127.0.0.1 --port 8000
```

A documentação OpenAPI/Swagger fica disponível em:

- Swagger UI: `http://127.0.0.1:8000/docs`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`

---

## 3. Como Importar e Configurar no Postman

1. Abra o Postman e clique em **Import**.
2. Selecione [`postman/BUSCAAI_Etapa_8.postman_collection.json`](postman/BUSCAAI_Etapa_8.postman_collection.json).
3. Abra a coleção **BUSCAAI - Etapa 8 (FastAPI)** -> aba **Variables**.
4. Confirme `base_url` (`http://127.0.0.1:8000`) e preencha o campo **Current Value** de `api_key` com a chave configurada em `BUSCAAI_API_KEYS`.

---

## 4. Roteiro das 10 Requisições

| # | Requisição | Endpoint | Resultado esperado | O que valida |
| :-: | :--- | :--- | :--- | :--- |
| **1** | Health | `GET /v1/health` | `200` (`readiness="ready"`) ou `503` (`readiness="not_ready"`), sempre com `liveness="alive"` | Estado operacional sem segredos, diferenciação obrigatório vs opcional |
| **2** | Busca autenticada válida | `POST /v1/search` | `200`, `status` válido, `products`, `offers`, `score_breakdown`, `validation`, `explanation`, `exchange_rate_info`, `request_id` | Orquestração completa via Etapa 7 + conversão BRL estimada + headers `X-Request-ID` e `X-RateLimit-*` |
| **3** | Ausência de API Key | `POST /v1/search` | `401` (`missing_api_key`) | Bloqueio de acesso sem `X-API-Key` |
| **4** | API Key inválida | `POST /v1/search` | `401` (`invalid_api_key`) | Rejeição segura sem ecoar a chave |
| **5** | Query vazia | `POST /v1/search` | `422` (`validation_error`) | Rejeição de `query` vazia/em branco |
| **6** | Query excessivamente longa | `POST /v1/search` | `422` (`validation_error`) | Limite máximo de 500 caracteres |
| **7** | Orçamento inválido | `POST /v1/search` | `422` (`validation_error`) | Rejeição de `budget_brl <= 0` |
| **8** | Perfil inválido | `POST /v1/search` | `422` (`validation_error`) | Rejeição de `profile_id` inexistente |
| **9** | Limite de requisições excedido | `POST /v1/search` | `429` (`rate_limit_exceeded`) + `Retry-After` | Rate limiter em memória por cliente autenticado |
| **10** | Erro seguro (campo extra) | `POST /v1/search` | `422` (`validation_error`) com `request_id` e sem `Traceback` | `ConfigDict(extra="forbid")` na fronteira HTTP e handler sanitizado |

### Testando o cenário 9 (Rate limit 429) no Collection Runner

1. Inicie o servidor com `$env:BUSCAAI_RATE_LIMIT_PER_MINUTE = "2"`.
2. No Postman, execute apenas a requisição **9. Limite de requisições excedido (429)** no **Collection Runner** com **Iterations = 3**.
3. As 2 primeiras chamadas retornam `200` (ou erro controlado de provedor caso sem credenciais externas) consumindo a janela; a 3ª chamada retorna **HTTP `429`** com `Retry-After` e `X-RateLimit-Remaining: 0`.

> **Limitação multi-instância:** o rate limiting é mantido em memória local ao processo. Em múltiplas instâncias/workers (`--workers > 1`), cada processo mantém seus próprios contadores.

---

## 5. Testes Conversacionais da Etapa 10 (`POST /v1/chat`)

A mesma coleção Postman inclui agora a pasta **Chat Conversacional (Etapa 10)** (requisições **11** a **18**). Para o roteiro detalhado, formato de `conversation_history` e limites configuráveis do endpoint stateless `/v1/chat`, consulte [`GUIA_TESTES_POSTMAN_CHAT_ETAPA_10.md`](GUIA_TESTES_POSTMAN_CHAT_ETAPA_10.md).
