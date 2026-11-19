# Auditoria Técnica Independente — BUSCAAI (Etapas 0–4)

> **Data:** 19/11/2026 · **Escopo:** código-fonte em `src/`, `tests/`, `scripts/`, `data/`, configuração e documentação.
> **Método:** leitura direta do código + sondagens somente leitura (scripts em pasta scratch fora do repositório; nenhum arquivo do projeto foi alterado, nenhum pacote instalado, nenhuma chamada à eBay/Gemini, nenhum download de pesos, `.env` não lido).
> **Convenção:** *[PROVADO]* = reproduzido por mim nesta auditoria; *[CÓDIGO]* = evidente na leitura, não executado; *[NÃO VALIDADO]* = depende do modelo/ambiente real.

---

## 1. Resumo executivo

O backend tem uma base sólida nas camadas de integração (OAuth em memória, QuotaGuard, retries, logging por *allowlist*, contratos imutáveis com `Decimal`) e **não encontrei violação direta da política de não persistência** nem vazamento de credenciais no código auditado.

Os problemas reais estão na **camada semântica (Etapa 4)**, que foi declarada "validada" mas está validada apenas contra um *stub* escrito pelo próprio agente:

1. **A deduplicação funde variantes distintas de produto em casos muito próximos dos exemplos pedidos** — iPhone 13 vs 13 *mini*, Galaxy S24 vs S24+, novo vs usado vs recondicionado, "Sony Headphones" genérico vs "Sony WH-1000XM5". Como os candidatos descartados não são devolvidos, a perda é irreversível antes da Etapa 5. Isto contradiz o requisito "na dúvida, preservar".
2. **Nenhum modelo real foi executado.** A "comparação" E5 vs MiniLM no benchmark compara dois vetores idênticos (o stub ignora `model_name`); limiares (0.68/0.75/0.86) não têm base empírica; o léxico do stub foi alinhado ao dataset sintético (n=10).
3. **O `QueryBuilder` (Etapa 3) deixa termos em português passarem para o `q` da eBay**, o que tende a zerar o recall da consulta principal.
4. **Vários testes "de conformidade" provam menos do que afirmam** (um é vacuamente verdadeiro).
5. `pytest` "puro" **falha na coleta** (4 arquivos); só `python -m pytest` da raiz funciona.

Riscos residuais de segurança: `.env` aparece no histórico Git (commits em branches remotos) — recomendável rotacionar chaves; todo o trabalho das Etapas 0–4 está **sem commit**.

**Veredito:** pronto para a Etapa 5 **somente após corrigir C1 e A1–A3** e definir o gate de validação com o modelo real (seção 9).

---

## 2. Avaliação geral da arquitetura

**Pontos positivos de organização.** Camadas claras: `config` → `observability` → `catalog.errors` → `search` (modelos, quota, OAuth, backend, mapper, query builder) → `pricing` → `semantic` → `userintent`. `SearchBackend` como `Protocol`; `EmbeddingProvider` como `Protocol`; carregamento de modelo, deduplicação e HTTP em classes separadas, como pedido. Não encontrei import circular a nível de módulo.

**Acoplamentos e duplicações reais:**

- **Imports eager transitivos [PROVADO].** `import buscai.semantic` carrega `httpx` e `google.genai` (`httpx: True | google.genai: True`). Causa: `buscai/search/__init__.py` importa backend/query_builder, e `userintent/__init__.py` (alterado na Etapa 3) importa `.llm` (SDK Gemini) sempre que qualquer um usa `userintent.schema`. Consequência: custo de import, `DeprecationWarning` do SDK em tudo e o teste AST de "sem cliente HTTP em `semantic/`" (ver §7) vale só para imports diretos.
- **Conhecimento de categoria triplicado.** `representation._CATEGORY_KEYWORDS`/`_KNOWN_BRANDS`, `query_builder.DEFAULT_MVP_CATEGORIES` e `userintent/evaluator._RAW_ALIASES`. A Etapa 5 planeja um quarto registro. Sem fonte única, as listas divergirão.
- **Constante privada exportada entre módulos:** `_VARIANT_TIER_TOKENS` listada em `__all__` de `representation.py` e importada por `dedup.py`.
- **Contrato indefinido para a consulta semântica.** `SemanticSearchProcessor.process(query_text, …)` não diz qual texto receber; os testes passam o texto bruto PT-BR do usuário (inclui "até R$ 5.000"), embora `UserIntent.query`/`SearchPlan` existam.

---

## 3. Problemas críticos

### C1 — Deduplicação funde variantes distintas e descarta candidatos de forma irreversível
**Gravidade: crítica (funcional)** — não é falha de conformidade, mas é perda silenciosa de dados antes de qualquer validação.

**Evidência [PROVADO]** (`classify_pair` com similaridade simulada 0.90, plausível para E5):

| Par | Resultado | Causa |
| :--- | :--- | :--- |
| iPhone 13 vs iPhone 13 **mini** | `probable_duplicate` | `"mini"` está em `_NOISE_MODEL_WORDS` ([representation.py:218](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/semantic/representation.py#L218)) e é removido dos `model_tokens`, mas também consta em `_VARIANT_TIER_TOKENS` (linha 269), que nunca verá o token |
| Galaxy S24 vs Galaxy S24**+** | `probable_duplicate` | a regex de tokens ([representation.py:690](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/semantic/representation.py#L690)) `[a-z0-9]+(?:-[a-z0-9]+)*` descarta o `+`; tokens idênticos `('galaxy','s24')` |
| Mesmo modelo **New** vs **Used** (iPhone 15; Dell XPS) | `probable_duplicate` | único veto de condição é `parts` ([dedup.py:275](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/semantic/dedup.py#L275)); `new/used/refurbished/open_box` não vetam |
| "Sony Wireless Headphones" vs "Sony WH-1000XM5 Wireless Headphones" | `probable_duplicate` (`consistent_brand_and_specs`) | `_check_model_compatibility` retorna "sem conflito" quando um lado tem `model_tokens` vazio ([dedup.py:136](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/semantic/dedup.py#L136)); o ramo da [linha 344](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/semantic/dedup.py#L344) aceita só marca igual + cosseno ≥ 0.86 |

O teste `test_dedup_never_merges_variants_or_distinct_models_even_with_extreme_cosine` ([test_semantic_dedup.py:72](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/tests/test_semantic_dedup.py#L72)) afirma exatamente isso, mas só cobre os 4 pares dos exemplos do enunciado (não os irmãos acima). O nome do teste superdeclara.

**Consequência:** o cliente B2B pode nunca ver um iPhone *usado* barato porque um *novo* "equivalente" foi mantido; ou o S24+ some. `SemanticProcessingResult.candidates` só devolve canônicos ([pipeline.py:149](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/semantic/pipeline.py#L149)).

**Correção recomendada:**
1. Tratar `condition_bucket` como veto quando ambos conhecidos e diferentes (e preferir `similar_product` se um é `None`).
2. Preservar `+` e sufixos de variante (`plus`, `+`, `mini`, `pro`, `max`, `ultra`, `se`, `fe`) como tokens obrigatórios; remover `"mini"` de `_NOISE_MODEL_WORDS`.
3. Quando só um lado tem `model_tokens`, **não** classificar como duplicata (tratar como incerteza → `similar_product`).
4. Adicionar testes parametrizados com todos os pares acima e usar *property test* "nenhuma variante de `{mini,+,Pro,Max,Ultra,SE,condição}` é fundida".

---

## 4. Problemas importantes

### A1 — Dedup colapsa ofertas e escolhe o "canônico" sem considerar preço/vendedor/frete
**Gravidade: alta.** `_candidate_completeness_score` ([dedup.py:172](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/semantic/dedup.py#L172)) ordena por `details_loaded`, nº de aspects, campos preenchidos e **`len(candidate.title)`** (linha 181). Efeitos: premia títulos com *keyword stuffing*; ignora preço, reputação do vendedor e frete; `DuplicateGroup` guarda só `duplicate_item_ids`, não as ofertas. Em um recomendador, mesmo produto de vendedores diferentes são ofertas distintas; a melhor pode ser descartada.
**Observação:** o teste de transitividade foi afrouxado para `("tr-1" in kept) ^ ("tr-2" in kept)` ([test_semantic_dedup.py:145](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/tests/test_semantic_dedup.py#L145)) após o desempate por tamanho de título escolher `tr-2`; isso mascara a arbitrariedade em vez de decidir a regra.
**Correção:** decidir explicitamente o contrato — (a) agrupar sem descartar (devolver grupo com ofertas para a Etapa 6 escolher), ou (b) canônico = menor preço total com vendedor aceitável; remover tamanho de título do desempate.

### A2 — Escolha do modelo e limiares sem evidência empírica
**Gravidade: alta (risco de decisão), documentação enganosa.**
- **[PROVADO]** O stub produz vetores idênticos para `intfloat/multilingual-e5-small` e `paraphrase-multilingual-MiniLM-L12-v2` (`vetores identicos: True`). O benchmark imprime duas linhas com métricas iguais e apenas latência distinta (ruído) — não é comparação de modelos. O `RELATORIO_TESTES_ETAPA_4.md` §5.1 apresenta a tabela sem esta ressalva clara.
- O léxico do stub (`_BILINGUAL_CONCEPT_ALIASES`, [embeddings.py ~L388](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/semantic/embeddings.py)) inclui conceitos como `16gb`, `512gb`, `work`, `study`, `anc` que coincidem com o dataset sintético → métricas circulares. Dataset: 4 casos de recuperação + 6 pares (n=10); `precision_at_3=0.667` e `dedup_accuracy=0.833` são ruído estatístico.
- A especificação do modelo veio de resumos de busca web (inclusive fontes secundárias). A afirmação de que `fastembed` suporta `intfloat/multilingual-e5-small` vinha de página de *crate* Rust; **[NÃO VALIDADO]** que o `fastembed` Python liste esse modelo. Compatibilidade de `torch`/`onnxruntime` em Python 3.14.7 Windows também não foi verificada.
- E5 é conhecido (model card) por distribuir cossenos em faixa alta (~0.7–1.0): com `similar_product ≥ 0.68` praticamente tudo vira "semelhante", e `rephrased 0.75`/`probable 0.86` podem ser frouxos — o que agrava C1.
- `MiniLM` em `SUPPORTED_MODEL_SPECS` tem `max_tokens=256` ([embeddings.py:73](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/semantic/embeddings.py#L73)); pelo que conheço do model card é 128 — **verificar**. O campo não é usado para truncar.
**Correção:** gate opt-in com modelo real (≥100 pares rotulados + ≥30 consultas PT-BR), calibrar limiares com curva precisão/recall priorizando **falso-positivo de fusão = 0**; ajustar a documentação para "stub — não comparativo".

### A3 — `QueryBuilder` vaza português para a consulta eBay
**Gravidade: alta (recall).** `_translate_feature_en` ([query_builder.py:499](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/search/query_builder.py#L499)) devolve qualquer texto ASCII não reconhecido **como está**.
**[PROVADO]** Em 30 intenções (24 do dataset de 50 + 6 sintéticas), 7 geraram `q` com PT-BR: `'resistente a poeira smartphone'`, `'bateria de longa duracao smartphone'`, `'tela dobravel smartphone'`, `'teclado numerico laptop'`, `'tela curva monitor'`, `'resistente ao suor headphones'` (sondas minhas, hipotéticas porém realistas) e **um caso real do dataset** (`'compatibilidade com Linux laptop'`; a variante acentuada "fácil instalação de Linux" é descartada). A eBay trata os termos como AND → resultado vazio. A consulta alternativa só ajuda quando `max_queries=2` e existe marca/modelo/categoria.
**Consequência adicional:** `preferred_features` entram no `q` da consulta **principal**; o design pede que preferências sejam relaxáveis.
**Correção:** lista branca — traduzir via dicionário ou **omitir** (registrar em `ignored_preferences`), nunca repassar texto livre; limitar tamanho de `q`; mover `preferred_features` apenas para consultas alternativas/validação. Teste: asserir que `q` só contém tokens ASCII do vocabulário EN.

### A4 — `merge_explicit_budget` pode afrouxar um teto rígido
**Gravidade: média.** Quando o texto traz moeda diferente (ex.: USD) e o `budget_brl` explícito existe, o ramo de [budget.py:77](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/userintent/budget.py#L77) substitui o teto sem comparar. **[PROVADO]:** `max_price=300 USD hard` + `budget_brl=5000` → `5000.0 BRL`. Viola a regra "mais restritivo vence" documentada.
**Correção:** converter via `CurrencyConverter` e aplicar o mínimo, ou levantar `ValueError` em moedas divergentes.

### A5 — Testes de conformidade provam menos do que afirmam
**Gravidade: média-alta.** Detalhes na §7. Resumo: `test_semantic_errors_never_leak_listing_strings_in_logs` ([test_semantic_compliance.py:192](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/tests/test_semantic_compliance.py#L192)) é **vacuamente verdadeiro** (o serviço não registra nada na falha); verificação de disco só olha `tmp_path`; AST não proíbe escrita.

### A6 — Mensagens de exceção podem carregar texto de anúncios
**Gravidade: média [CÓDIGO].** Os provedores reais fazem `safe_msg = sanitize_text(str(exc))` e embutem em `EmbeddingInferenceError` ([embeddings.py:283, 373](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/semantic/embeddings.py#L283)). `sanitize_text` só redige segredos; erros de tokenizer/ONNX frequentemente ecoam a entrada. Na Etapa 8, handlers que devolvam `str(exc)` vazariam títulos. Além disso o `EmbeddingService` não normaliza exceções de provedores customizados (o teste usa `RuntimeError` cru).
**Correção:** mensagens de erro fixas + `error_type`; manter a causa só em `__cause__` e nunca registrar/serializar.

### A7 — Carregamento *lazy* de ~470 MB no caminho da requisição
**Gravidade: média [CÓDIGO].** `ensure_loaded()` roda na primeira `encode`; nada pré-carrega no startup nem usa `is_ready()`. A primeira requisição fará download (HF Hub, com cache em disco do usuário) e pode estourar timeouts. O backend padrão (`sentence_transformers`) não está nas dependências principais; com a configuração padrão a primeira busca falha com `EmbeddingModelLoadError`. A política de não persistência cobre anúncios, mas o teste de disco não monitora o cache do HF.
**Correção:** *preload* no `lifespan` (Etapa 8), `HF_HUB_OFFLINE=1` em produção, health `ready=false` até carregar.

### A8 — `pytest` puro falha na coleta
**Gravidade: média [PROVADO].** `pytest --collect-only` → 4 erros `No module named 'scripts'` (`test_ebay_backend`, `test_product_search`, `test_search_flow`, `test_semantic_integration`). `pyproject.toml` só tem `pythonpath = ["src"]`; os testes importam `scripts.*` (pacote-namespace sem `__init__`), o que só funciona com `python -m pytest` da raiz. CI/IDE que chamem `pytest` quebram.
**Correção:** `pythonpath = ["src", "."]` ou mover a lógica dos scripts para `src/`.

### A9 — Corrida no OAuth sob 401 concorrente
**Gravidade: média-baixa [CÓDIGO].** Em `enrich_candidates` (threads, [ebay_backend.py:267](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/search/ebay_backend.py#L267)) vários workers recebendo 401 chamam `invalidate()` ([L389](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/search/ebay_backend.py#L389)) e depois `get_access_token(force_refresh=True)`; cada um força nova emissão, e um `invalidate()` pode descartar o token recém-renovado por outro. O limite de emissão (1.000/dia) não é guardado. Impacto prático baixo com `max_concurrency=4`, mas é *thundering herd* evitável.
**Correção:** `force_refresh` condicionado a "token ainda é o que falhou" (comparar versão/valor).

### A10 — Custo O(n²) e resultado inchado
**Gravidade: média-baixa.** **[PROVADO]** `deduplicate` (stub, sem custo de inferência): n=200 → 239 ms; n=400 → 784 ms; 79.800 `PairEvaluation` Pydantic mantidos em `DeduplicationResult.pair_evaluations` ([dedup.py:513](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/semantic/dedup.py#L513)). n=400 é o teto atual (2 consultas × `limit` 200). Em FastAPI com *threadpool* o GIL será disputado.
**Correção:** *blocking* por (categoria, marca), não reter `pair_evaluations` por padrão, limitar nº de candidatos.

### A11 — Intent/Prompt da Etapa 3 nunca avaliado contra o Gemini
**Gravidade: média [NÃO VALIDADO].** O prompt e o schema mudaram (novos `models/condition/currency`); `evaluator` não pontua esses campos; os 6 cenários "integrados" usam payloads LLM escritos à mão, logo validam o `QueryBuilder`, **não** a interpretação. Regressão de extração é possível e não medida.
**Correção:** rodar `scripts/evaluate_intent.py` (opt-in) antes/depois e estender `evaluator`.

### A12 — Segredos no histórico Git e trabalho sem commit
**Gravidade: alta (aceita pelo proprietário via D2) / média.**
- **[PROVADO]** `git log --all -- .env` lista os commits `90663f3` e `3b28fae` (inclusive em branches remotos). Não li o conteúdo. Se havia chaves reais, permanecem válidas até rotação. Recomendo rotacionar Gemini e eBay Cert ID antes de qualquer publicação.
- **[PROVADO]** Etapas 0–4 estão integralmente não versionadas (~60 arquivos, deleções não encenadas); não há ponto de retorno. (Respeitando a instrução de não commitar nesta auditoria.)

### A13 — Texto de embedding pode ser truncado antes das specs
**Gravidade: média [NÃO VALIDADO com modelo real].** `build_candidate_embedding_text` ordena `aspects` alfabeticamente; E5 trunca em 512 tokens. **[PROVADO]** com 40 aspects sintéticos, "RAM Size" ficou na posição 2836/2878 caracteres. Anúncios reais da eBay frequentemente têm muitos aspects.
**Correção:** priorizar aspects técnicos por categoria e limitar o comprimento.

### A14 — Query semântica e prefixos
**Gravidade: média (depende do modelo real).** Dedup compara documento×documento com `passage:` nos dois lados; a documentação do E5 recomenda `query:` para ambos em tarefas de similaridade simétrica. Os limiares serão calibrados em uma regime diferente do recomendado. E o texto bruto do usuário (com orçamento) como consulta introduz ruído (ver §2).

---

## 5. Melhorias recomendadas (menor gravidade)

| ID | Arquivo / local | Evidência | Gravidade | Correção |
| :-- | :-- | :-- | :-: | :-- |
| M1 | [llm.py:22,69](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/userintent/llm.py#L22) | Marcadores `"500"`, `"429"` casam por substring em qualquer mensagem. **[PROVADO]** `ValueError("400 … max_output 5000 …")` → transitório | baixa | Classificar por `APIError.code`/tipo; remover heurística textual |
| M2 | [llm.py:147,153](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/userintent/llm.py#L147) | `assert` em código de produção (removido com `-O`) | baixa | Levantar exceção explícita |
| M3 | `llm.py` `_format_contents` | Sem limite de tamanho do texto/histórico | baixa-média | Limitar (Etapa 8 prevê 500 caracteres no schema HTTP) |
| M4 | `query_builder.py`/`SearchQuery` | `query_text` sem teto de tamanho | baixa | Teto + truncamento seguro |
| M5 | [.env.example:2,6,7,17](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/.env.example) | `BUSCAAI_API_KEYS=chave_cliente_1,chave_cliente_2` seria chave **válida** se copiada; `is_*_configured` retorna `True` para `coloque_…` | baixa-média | Placeholders vazios/comentados; rejeitar placeholders na inicialização |
| M6 | [logging.py:35](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/observability/logging.py#L35) | `FORBIDDEN_KEY_FRAGMENTS` exportado e **não usado** (filtro real é a allowlist, mais forte) | baixa | Remover ou documentar como legado |
| M7 | `quota.py` | Janela deslizante local de 24 h ≠ reset real da eBay; sem controle do limite OAuth; por processo | baixa (documentado) | Consultar API de limites da eBay; registrar divergência |
| M8 | `embeddings.py` stub | `_loaded` sem lock | baixa | Irrelevante em produção; manter |

---

## 6. Pontos bem implementados

- **OAuth** ([ebay_auth.py](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/search/ebay_auth.py)): token só em memória, margem de renovação de 120 s, `threading.Lock`, `SecretStr`, `__repr__` sem segredo, mapeamento tipado de 400/401/403/429/5xx/timeout.
- **QuotaGuard** ([quota.py](file:///C:/Users/dedey/.vscode/Projetos/BuscaAi/src/buscai/search/quota.py)): consumo e orçamento por requisição atômicos sob um único lock; recusa antes de gastar cota; retries contam na cota diária.
- **Backend eBay**: retries limitados com *backoff*, honra `Retry-After`, `item_id` URL-encoded, parsing defensivo com `Decimal`, campos ausentes como `None`, sem cache de anúncios (provado por teste de duas buscas idênticas).
- **Logging**: *allowlist* de campos + sanitização de padrões de segredo; JSON em linha única; sem *stack traces*.
- **Contratos**: `Candidate`/`SearchQuery`/`SearchResponse` imutáveis, validação de moeda/URL/preço; `UserIntent` com `extra="ignore"` e schema compatível com Gemini (testado sem `additionalProperties`).
- **Dedup**: vetos estruturais corretos para armazenamento, RAM, resolução, tela, painel, Hz, categoria, marca, MPN; agrupamento por clique completa evita transitividade indevida; política conservadora por incerteza em armazenamento/RAM/resolução.
- **Semântico**: sem imports de banco/arquivo/cache; vetores locais com `del` após uso; texto de embedding exclui preço, URLs, vendedor, frete e `item_id` (verificado por teste).
- **Transparência**: relatórios marcam `BLOQUEADO` para ruff/mypy/pytest-cov e declaram uso de stub.
- **Streamlit** (`src/buscai/app/`) sem alterações (`git diff` vazio); imports passivos continuam resolvendo.

**Áreas sem achados:** quota/concorrência do `QuotaGuard`; tratamento de HTTP 429 do backend; ausência de persistência em disco no código de `semantic/` (`Select-String` por `open(`, `np.save`, `write_text`, `memmap` não retornou nada).

---

## 7. Lacunas nos testes

Quantidade (188) não é evidência de qualidade; a análise abaixo é por conteúdo.

1. **Vacuoso:** `test_semantic_errors_never_leak_listing_strings_in_logs` — o `EmbeddingService`/`SemanticSearchProcessor` não registram nada ao falhar; a asserção "sentinela ∉ logs" é sempre verdadeira. O que deveria ser testado (mensagens de exceção e handlers) não é.
2. **Disco:** `list(tmp_path.rglob("*")) == []` só observa o diretório de trabalho temporário; não observa `tempfile.gettempdir()` nem o cache do HF. Alternativa robusta: `sys.addaudithook` em eventos `open` com modo de escrita durante `process()`.
3. **AST de imports:** proíbe `pickle/sqlite3/...`, mas não `open()`, `np.save`, `Path.write_*`; e, por import transitivo, `httpx`/`google.genai` **estão carregados** (provado), então a frase "sem clientes HTTP" é verdadeira só para imports diretos.
4. **Ciclo de vida:** `weakref` só rastreia arrays devolvidos pelo provedor-espião; arrays criados em `l2_normalize`/`cosine_similarity_matrix`/fatiamento não são rastreados. `_walk_object_graph` só inspeciona objetos de `buscai.semantic`.
5. **Dedup:** 3 testes "mega" com ~10 cenários cada; só os exemplos do enunciado; **faltam** `mini`, `+`, condição, genérico×específico, ordem de entrada (clustering guloso é dependente de ordem), incerteza só em um lado, `details_loaded` pós-`getItem`.
6. **Benchmark:** `test_benchmark_embeddings_script…` assere `mrr == 1.0` — testa o stub, não o sistema, e quebrará se o stub mudar.
7. **Integração Etapa 3:** o dataset de 50 casos é percorrido apenas verificando que `q` é não vazio; nada checa idioma do `q` (A3). Os 6 cenários dependem de payload LLM escrito à mão.
8. **Intent LLM:** exercitado só pelo caminho `text` (JSON); o caminho `parsed` com objeto real do SDK, `HttpOptions` efetivo e 429/503 reais não são cobertos.
9. **Concorrência:** não há teste de `enrich_candidates` com 401 simultâneo, nem de `EmbeddingService` com múltiplas threads.
10. **Cobertura 89,8% (`trace`)** conta linhas executadas, não ramos; as linhas dos provedores reais (`sentence_transformers`/`fastembed`) só são executadas com *loaders* simulados.
11. **Execução:** `pytest` puro quebra (A8).

---

## 8. Riscos ainda não validados em ambiente real

| Risco | Estado |
| :-- | :-- |
| Qualidade PT-BR↔EN-US do `multilingual-e5-small` em títulos de anúncios | **Não medida** |
| Limiares 0.68 / 0.75 / 0.86 | **Sem calibração**; E5 comprime cossenos |
| Taxa de falso-positivo/negativo de fusão com anúncios reais (ruído, caixa alta, abreviações, "lot of 2", acessórios) | **Desconhecida** |
| `fastembed` suportar o modelo escolhido; `torch`/`onnxruntime` em Python 3.14.7 Windows | **Não verificado** |
| Latência/RAM reais (400 docs × 512 tokens) e concorrência de inferência | **Não medidas** |
| Truncamento a 512 tokens e prefixos `query:`/`passage:` | **Corretos no código; efeito não medido** |
| Formato real de `shippingOptions`/`localizedAspects`/`brand` da eBay; cobertura de `getItem` | Parcialmente exercitado (teste opt-in manual); sem dados agregados |
| Comportamento do prompt Etapa 3 no Gemini | **Não avaliado** |
| Reset real da cota eBay vs janela local | **Não verificado** |

**Provado por testes offline:** contratos e validações; parsing defensivo; retries/erros HTTP com `MockTransport`; ausência de cache/escrita no cwd; ausência de campos proibidos no texto de embedding; vetos estruturais para os pares do enunciado; unicidade de carregamento com *loader* simulado.

---

## 9. Recomendações para as Etapas 5–10

**Antes da Etapa 5 (bloqueantes):** corrigir C1; decidir A1 (agrupar vs. descartar); corrigir A3 e A4; corrigir A8; criar um *gate* opt-in `ebay_live`+modelo real: ≥100 pares rotulados (incluindo os irmãos de C1), calibrar limiares com meta de **0% de falso-positivo de fusão**, medir latência/RAM.

- **Etapa 5:** o preço "rígido" hoje é só filtro eBay em USD (exclui frete, usa câmbio estimado, ignora `max_inclusive=False`) — revalidar por candidato com `Decimal` e inclusividade. Usar um **único registro** de categorias/perfis para `QueryBuilder` + `representation` + validador. Executar `getItem` antes da dedup para pares ambíguos. Tratar `condition` como atributo de primeira classe.
- **Etapa 6:** o ranking precisa das ofertas (preço, vendedor, frete) — depende da decisão A1. Calibrar `semantic_score` (cossenos E5 são comprimidos).
- **Etapa 7 (LangGraph):** estado contém candidatos; testar com `LANGSMITH_TRACING=true`/`LANGCHAIN_TRACING_V2=true` no ambiente, garantir `compile()` sem checkpointer e que o import não ative *tracing*.
- **Etapa 8:** *preload* do modelo no `lifespan`, semáforo de inferência, limites (`query` 500, nº de candidatos), *handlers* que **não** devolvem `str(exc)`, validação/limitação de `X-Request-ID`, rejeitar chaves de exemplo, `HF_HUB_OFFLINE=1`.
- **Etapa 9:** conformidade com `sys.addaudithook` (escritas), snapshot de `tempfile.gettempdir()` e do cache HF, métricas de memória; testes de concorrência; teste de idioma do `q`; avaliação do Gemini nos 50 casos.
- **Etapa 10:** sessão sem candidatos/IDs (já previsto); testar com sentinelas.

---

## 10. Avaliação final

| Dimensão | Nota | Justificativa e limites |
| :-- | :-: | :-- |
| Arquitetura | **7,0** | Camadas e *Protocols* coerentes; penalizada por imports eager transitórios, conhecimento de categoria triplicado e contrato indefinido da query semântica. |
| Qualidade do código | **6,5** | Legível, tipado e defensivo; mas arquivos grandes com listas fixas (`representation.py`), regex frágil (`+`), heurísticas textuais, `assert` em produção. |
| Segurança | **7,0** | Sem vazamento ou persistência encontrados; ótimo desenho de logs. Descontos: `.env` no histórico Git, mensagens de erro potencialmente com texto de anúncios, placeholders válidos. Não auditei dependências com ferramentas (não instaladas). |
| Testabilidade | **6,0** | Injeção de dependências excelente; porém testes de conformidade parcialmente vacuosos, `pytest` puro quebra, testes "mega" e pouca cobertura de concorrência/ramos. |
| Implementação semântica | **4,5** | Arquitetura correta e vetos úteis, mas fusão indevida de variantes, nenhum modelo real executado, limiares sem base, benchmark circular. |
| Maturidade geral do backend | **5,5** | Integração eBay/infra madura para o escopo atual; camada semântica e consulta ainda são protótipos validados só offline. |

**Limitações desta avaliação:** não executei modelos reais, não chamei eBay/Gemini, não li `.env`, não rodei lint/tipos/auditoria de dependências (ferramentas ausentes) e as sondagens com similaridade 0.90 *simulam* o comportamento do E5; a taxa real de erro só será conhecida com o modelo real.
