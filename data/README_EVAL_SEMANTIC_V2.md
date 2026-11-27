# Documentação do Dataset Semântico Controlado v2

> **Natureza dos dados:** **100% sintético**, criado exclusivamente para avaliação estruturada offline e benchmarking futuro do modelo `intfloat/multilingual-e5-small`. **Não contém títulos, preços, URLs, vendedores ou payloads persistidos de anúncios reais da eBay.**

---

## 1. Estrutura e Separação Calibração vs. Avaliação Final

Os dados são divididos em dois conjuntos estritamente disjuntos para evitar que métricas finais sejam medidas no mesmo conjunto usado para ajuste de limiares:

| Arquivo | Split | Consultas PT-BR (`retrieval`) | Pares de deduplicação (`deduplication`) | Uso permitido |
| :--- | :---: | :---: | :---: | :--- |
| `data/eval_semantic_v2_calibration.jsonl` | `calibration` | **12** | **40** | Ajuste exploratório / calibração de limiares (`dedup_probable_threshold`, `dedup_similar_threshold`, pesos semânticos) |
| `data/eval_semantic_v2_evaluation.jsonl` | `evaluation` | **24** | **72** | Medição final cega pós-calibração (Precision@K, Recall@K, MRR, NDCG@K, Dedup Accuracy, False Dedup Rate, False Negative Rate) |
| **Total v2** | — | **36** *(meta >= 30)* | **112** *(meta >= 100)* | Avaliação completa nas 4 categorias MVP |

> O arquivo legado `data/eval_semantic_synthetic.jsonl` (`4` consultas, `6` pares) foi preservado intacto para manter compatibilidade com [`tests/test_semantic_integration.py`](../tests/test_semantic_integration.py).

---

## 2. Regras Objetivas de Rotulagem

### 2.1. Tarefa `retrieval` (`relevance`)
- **`2` (altamente relevante):** mesmo produto/categoria e atendimento claro às restrições objetivas da consulta (marca/linha, capacidade, resolução, conectividade ou perfil técnico).
- **`1` (parcialmente relevante):** mesma categoria MVP, porém diverge em marca pedida, variante, capacidade secundária ou uso principal.
- **`0` (irrelevante / falso positivo):** categoria incompatível (acessório como capa, cabo, mouse, suporte) ou outro produto distinto.

### 2.2. Tarefa `deduplication` (`expected_label`)
A rotulagem prioriza **evitar falsos positivos de fusão** (`false_dedup_rate = 0`):

1. **`probable_duplicate` (mesmo produto físico/SKU lógico):**
   - Mesma marca, mesmo modelo exato e mesma variante (ou ambos sem sufixo de variante).
   - Mesmas especificações técnicas críticas compatíveis (RAM, armazenamento, resolução/tamanho/taxa de atualização, conectividade ANC/wireless).
   - Mesma condição de item (`New` com `New`, etc.) ou ambos explicitamente equivalentes entre PT-BR e EN-US.
2. **`similar_product` (produtos parecidos ou variantes distintas — NÃO fundir):**
   - **Variantes distintas** do mesmo modelo base (`Mini`, `Plus`, `Pro`, `Max`, `Ultra`, `SE`, `FE`).
   - **Condições distintas** (`New` vs `Used`, `Used` vs `Refurbished`, `Open Box` vs `New`).
   - **RAM ou armazenamento divergentes** (`8GB` vs `16GB`, `16GB` vs `32GB`, `128GB` vs `256GB`, `256GB` vs `512GB`, `512GB` vs `1TB`).
   - **Resoluções ou especificações incompatíveis** (`FHD 1080p` vs `4K UHD`, `60Hz` vs `144Hz/165Hz`, `Wired` vs `Wireless ANC`, `24"` vs `27"`/`32"`, `IPS` vs `OLED`).
   - **Modelos diferentes da mesma marca** dentro da mesma categoria (ex.: `ThinkPad T14` vs `IdeaPad 3`).
   - **Informação incompleta:** quando `brand`, `model`, `condition` ou especificação crítica estiver ausente em um dos lados de forma que não comprove identidade física.
3. **`different_product` (produtos ou categorias distintas):**
   - Categorias distintas (ex.: `smartphone` vs `monitor`, `notebook` vs `cabo/capa`) ou produtos funcionalmente incompatíveis.
