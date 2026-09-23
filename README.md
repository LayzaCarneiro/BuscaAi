# UserIntent — Etapa 1

Projeto inicial para transformar consultas de compras em um `UserIntent` estruturado usando Gemini + Pydantic, e medir a qualidade da extração em 50 casos.

## 1. Ambiente

```bash
cd /Users/SEU_USUARIO/Downloads/userintent_project
python3 -m venv path/to/venv
source source path/to/venv/bin/activate
python3 -m pip install -U pip
python3 -m pip install -r requirements.txt
```

Crie `.env` a partir de `.env.example`:

```bash
cp .env.example .env
```

Preencha `GEMINI_API_KEY`. O modelo pode ser alterado em `GEMINI_MODEL`.

## 2. Teste de um caso

```bash
PYTHONPATH=src python scripts/run_single_intent.py
```

A saída esperada é um JSON compatível com `UserIntent`.

## 3. Avaliação dos 50 casos

```bash
PYTHONPATH=src python scripts/evaluate_intent.py
```

Isso chama o Gemini uma vez por caso, salva as previsões em `data/predictions_gemini.jsonl` e imprime as métricas.

## 4. Reavaliar sem chamar a API

Depois de gerar as predições:

```bash
PYTHONPATH=src python scripts/evaluate_intent.py \
  --predictions data/predictions_gemini.jsonl
```

## 5. Testes unitários

```bash
PYTHONPATH=src pytest -q
```

## Estrutura

```text
userintent_project/
├── src/userintent/
│   ├── schema.py        # contrato UserIntent
│   ├── prompt.py        # regras de extração
│   ├── llm.py           # Gemini Structured Outputs
│   └── evaluator.py     # métricas
├── data/
│   └── intent_eval_50.jsonl
├── scripts/
│   ├── test_single_intent.py
│   ├── extract_intent.py
│   └── evaluate_intent.py
└── tests/
```
