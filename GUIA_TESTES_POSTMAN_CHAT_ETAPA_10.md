# Guia de Testes Postman — Etapa 10 (`POST /v1/chat`)

Este guia descreve como validar o endpoint conversacional stateless **`POST /v1/chat`** utilizando a coleção Postman oficial em [`postman/BUSCAAI_Etapa_8.postman_collection.json`](postman/BUSCAAI_Etapa_8.postman_collection.json).

---

## 1. Arquitetura Stateless e Formato de `conversation_history`

O servidor **não armazena sessões conversacionais, histórico de buscas nem anúncios da eBay** entre requisições. A continuidade multi-turn ocorre exclusivamente por meio do campo `conversation_history` enviado pelo cliente em cada requisição `POST /v1/chat`:

```json
{
  "message": "Prefiro Lenovo e pelo menos 16 GB de RAM.",
  "conversation_history": [
    {
      "role": "user",
      "content": "Quero um notebook para estudar até R$ 4.000."
    },
    {
      "role": "assistant",
      "content": "Busca concluída na categoria notebook com perfil study. Você pode ajustar orçamento, marca, perfil ou especificações."
    }
  ],
  "budget_brl": 4000,
  "profile_id": "study",
  "limit": 5
}
```

### Regras de validação e privacidade do histórico

1. **`role` permitido:** apenas `"user"` ou `"assistant"` (papéis como `"system"` ou `"tool"` são rejeitados com HTTP `422`).
2. **Sem duplicação:** não inclua `message` repetida manualmente ao final de `conversation_history`; o servidor já processa a mensagem atual separadamente e descarta eventual duplicata acidental no último turno `user`.
3. **Limites configuráveis (padrões em `Settings`):**
   - `BUSCAAI_CHAT_MAX_HISTORY_MESSAGES=12` (máximo de turnos em `conversation_history`)
   - `BUSCAAI_CHAT_MAX_MESSAGE_CHARS=500` (tamanho máximo de `message` e de cada turno)
   - `BUSCAAI_CHAT_MAX_HISTORY_TOTAL_CHARS=4000` (soma máxima de caracteres de todo `conversation_history`)
4. **Política de não persistência e não reenvio de anúncios da eBay:**
   - Utilize `response.conversation_context.assistant_turn_summary` (em vez de copiar cards, preços, URLs ou IDs de ofertas) ao montar o próximo turno `"assistant"` no cliente.
   - Mesmo que um cliente envie acidentalmente títulos, preços, URLs ou `item_id` em um turno `"assistant"`, a API sanitiza esse conteúdo antes de acionar o extrator de intenção (Gemini).

---

## 2. Pré-requisitos de Execução

Consulte [`GUIA_TESTES_POSTMAN_ETAPA_8.md`](GUIA_TESTES_POSTMAN_ETAPA_8.md) para iniciar o servidor local com Uvicorn e configurar as variáveis `base_url` (`http://127.0.0.1:8000`) e `api_key` no Postman.

> **Segurança:** o arquivo JSON da coleção mantém `api_key` vazia (`""`). Nunca salve segredos reais na coleção nem em commits.

---

## 3. Roteiro das 8 Requisições da Pasta `Chat Conversacional (Etapa 10)`

| # | Requisição na pasta `Chat Conversacional (Etapa 10)` | Resultado esperado | O que valida |
| :-: | :--- | :--- | :--- |
| **11** | `POST /v1/chat — Primeira mensagem` | `200`, `status` válido, `assistant_message`, `conversation_context.assistant_turn_summary` | Primeira interação sem histórico prévio (`conversation_history: []`) |
| **12** | `POST /v1/chat — Mensagem complementar (multi-turn)` | `200`, preservação da categoria/orçamento + novas preferências | Continuidade com `conversation_history` válido (`user` + `assistant`) |
| **13** | `POST /v1/chat — Necessidade de esclarecimento` | `200`, `clarification_needed: true`, `search_results: null` | Solicitação de esclarecimento para intenção vaga sem chamar a eBay |
| **14** | `POST /v1/chat — Busca concluída com refinamento` | `200`, `status: "ok"`, `search_results` preenchido | Turno de resposta ao esclarecimento executando a busca completa |
| **15** | `POST /v1/chat — Busca sem resultados` | `200`, `status: "no_results"` ou `"all_rejected"` | Resposta PT-BR determinística sem inventar resultados ou relaxar hard constraints |
| **16** | `POST /v1/chat — Histórico inválido (role system -> 422)` | `422` (`validation_error`) com `request_id` | Bloqueio de papéis arbitrários (`system`, `tool`) no histórico |
| **17** | `POST /v1/chat — Histórico excessivamente grande (422)` | `422` (`validation_error`) com `request_id` | Proteção contra abuso de tamanho/quantidade de mensagens no histórico |
| **18** | `POST /v1/chat — Autenticação inválida (401)` | `401` (`invalid_api_key`) | Exigência de `X-API-Key` válida também no endpoint conversacional |
