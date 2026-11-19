# BuscaAI --- Contexto do projeto e diretrizes para planejamento e implementação

> **Leitor deste arquivo: agente de código (Antigravity).** Leia este
> documento integralmente antes de analisar ou modificar o repositório.
>
> **Objetivo desta etapa:** compreender o projeto existente, auditar sua
> implementação e, com base no código real, criar um arquivo
> `IMPLEMENTACOES.md` com o plano completo de trabalho. A divisão das
> tarefas, sua ordem, dependências, etapas e critérios de aceite devem
> ser definidos pelo próprio Antigravity após a auditoria --- não
> presuma que uma lista fixa de tarefas antecipada seja adequada ao
> estado atual do código.
>
> **Escopo desta fase do projeto: 100% backend.** O front-end será
> definido posteriormente com os demais integrantes da equipe.

------------------------------------------------------------------------

## 0. Regras de trabalho obrigatórias

1.  **Comece em modo de auditoria, somente leitura.** Antes de alterar
    código, dependências, configurações ou integrações, analise o
    repositório e produza `AUDITORIA.md`.
2.  **Depois da auditoria, produza `IMPLEMENTACOES.md`.** Esse segundo
    arquivo deve conter um plano de implementação completo, dividido em
    etapas e tarefas pequenas, ordenadas conforme as dependências reais
    encontradas no projeto.
3.  **Não implemente nada nesta primeira execução.** Ao concluir
    `AUDITORIA.md` e `IMPLEMENTACOES.md`, apresente um resumo e aguarde
    aprovação explícita da equipe.
4.  **Não invente o estado do projeto.** Diferencie claramente o que foi
    encontrado no código, o que é uma recomendação e o que ainda precisa
    ser confirmado.
5.  **Respeite a stack existente.** Não reescreva tudo do zero nem
    troque framework, linguagem, provedor de LLM ou biblioteca sem
    justificar tecnicamente. Primeiro identifique a stack e avalie o que
    pode ser reaproveitado.
6.  **Preserve o trabalho existente.** Identifique funcionalidades,
    módulos, testes e integrações que já funcionam e podem ser mantidos
    ou adaptados. Não remova algo apenas porque não aparece na
    arquitetura-alvo.
7.  **Segredos e credenciais:**
    -   Nunca leia, imprima, registre em log nem cole em prompts o
        conteúdo de `.env` ou de arquivos equivalentes.
    -   Identifique apenas os nomes das variáveis e os locais em que são
        utilizadas.
    -   Nunca escreva chaves reais em código, testes, documentação ou
        arquivos de instruções.
    -   Verifique se `.env` está protegido pelo `.gitignore`.
    -   Mantenha `.env.example` sem valores secretos.
8.  **Conformidade eBay:** trate as regras da seção 3 como restrições a
    validar antes de implementar qualquer integração ou mecanismo de
    armazenamento.
9.  **Verifique informações externas.** Antes de implementar a
    integração, confira a documentação oficial e atual da eBay. Não
    assuma que exemplos, endpoints, campos, limites ou políticas
    descritos aqui continuam inalterados.
10. **Decisões relevantes precisam de justificativa.** Quando uma
    escolha afetar escopo, custo, segurança, conformidade, arquitetura
    ou dependências, registre alternativas, recomendação e dúvida
    pendente em `AUDITORIA.md` ou `IMPLEMENTACOES.md`. Não tome decisões
    irreversíveis sem aprovação.
11. **Trabalhe incrementalmente após a aprovação.** Execute uma tarefa
    ou um pequeno conjunto coerente de tarefas por vez. Não avance
    quando os critérios de aceite da etapa atual não forem atendidos.
12. **Não declare conclusão sem evidência.** Ao concluir cada tarefa,
    informe arquivos alterados, decisões, testes executados, resultados
    e pendências.
13. **Não exponha dados de anúncios nos artefatos de desenvolvimento.**
    Não copie respostas reais da eBay para documentação, fixtures,
    relatórios, exemplos, screenshots, logs ou arquivos de diagnóstico.

------------------------------------------------------------------------

## 1. Ideia do projeto

**BuscaAI** é um sistema de busca semântica e recomendação de produtos
guiado por um chatbot com LLM. É um trabalho acadêmico, concebido como
um **produto B2B**: empresas consomem a funcionalidade por meio de uma
API.

Exemplo de solicitação:

> "Quero um notebook até 4.000 reais para jogos casuais."

O sistema deve:

1.  entender a intenção e extrair restrições rígidas (orçamento,
    categoria) e flexíveis (perfil de uso);
2.  buscar candidatos na **API real da eBay**;
3.  reordenar os candidatos por similaridade semântica;
4.  validar os melhores candidatos contra os requisitos do usuário,
    usando atributos reais recuperados;
5.  ranquear os candidatos por múltiplos critérios, quando houver dados
    suficientes;
6.  devolver recomendações com justificativas fiéis às informações
    disponíveis.

Problema que resolve: buscas tradicionais por palavras-chave exigem que
o usuário conheça termos técnicos e configure muitos filtros. O BuscaAI
deve traduzir a necessidade expressa em linguagem natural em critérios
de busca e explicar por que os resultados foram recomendados.

O diferencial esperado não é apenas conversar com o usuário: é combinar
recuperação de produtos, interpretação da intenção, busca semântica,
validação de requisitos, ordenação e explicabilidade.

------------------------------------------------------------------------

## 2. Direção desejada e decisões de produto

As decisões abaixo representam a direção pretendida. Durante a
auditoria, o Antigravity deve verificar a compatibilidade delas com o
código existente e apontar impactos ou conflitos antes de propor a
implementação.

  -----------------------------------------------------------------------
  Tema                                Direção desejada
  ----------------------------------- -----------------------------------
  Fonte de dados de produtos          **API real da eBay (Browse API),
                                      ambiente de produção.**

  Mercado Livre                       Não deve fazer parte do produto
                                      final. Identificar e planejar a
                                      remoção da integração e da variável
                                      `MERCADOLIVRE_ACCESS_TOKEN`, caso
                                      existam.

  DummyJSON                           Não deve alimentar o caminho de
                                      produção. Poderá permanecer apenas
                                      como fixture/fonte de teste
                                      offline, se isso for útil e estiver
                                      claramente isolado.

  Busca semântica                     Reordenação semântica em memória
                                      sobre candidatos retornados pela
                                      eBay. Não presumir que haverá busca
                                      vetorial sobre um catálogo
                                      completo.

  Persistência de dados da eBay       Não persistir dados de anúncios nem
                                      dados derivados, observadas as
                                      condições da seção 3.

  Banco vetorial                      Fora do escopo inicial.
                                      pgvector/FAISS podem ser
                                      considerados futuramente, mas não
                                      devem ser introduzidos sem nova
                                      análise de conformidade e
                                      aprovação.

  Orquestração                        Grafo de estados, preferencialmente
                                      LangGraph se a stack e a auditoria
                                      confirmarem que é adequado.

  Escopo                              Backend. O front-end será tratado
                                      posteriormente com a equipe.

  Modelo de negócio                   API B2B, com autenticação e limites
                                      de uso compatíveis com a fase
                                      acadêmica/MVP.
  -----------------------------------------------------------------------

Não trate a tabela como autorização para apagar imediatamente
componentes existentes. Primeiro mapeie dependências, funcionalidades e
riscos de regressão.

------------------------------------------------------------------------

## 3. Conformidade eBay --- restrições críticas a validar

O projeto foi planejado considerando a isenção **"Not persisting eBay
data"** para as credenciais de produção. A equipe entende que essa
condição exige que dados de anúncios não sejam persistidos. Antes de
implementar, confirme o alcance exato e vigente dessa isenção na
documentação e nas condições aplicáveis à conta.

Até que a equipe confirme qualquer interpretação diferente, adote a
abordagem conservadora:

-   **Não persistir dados de anúncios da eBay** em banco, arquivos,
    JSON, CSV, Parquet, `.npy`, SQLite, pickle, cache em disco, dumps ou
    armazenamento temporário.
-   **Não manter cache de anúncios entre requisições.** Dados de
    anúncios devem existir apenas na memória durante o processamento da
    requisição e ser descartados ao final.
-   O token OAuth de aplicação pode ser mantido em memória até perto do
    vencimento, como credencial, sem registrar seu valor.
-   **Logs:** registrar apenas metadados operacionais, como status HTTP,
    duração, quantidade de itens e `request_id`. Não registrar corpos de
    resposta, títulos, descrições, preços, URLs de anúncios, atributos
    ou credenciais.
-   **Embeddings e demais dados derivados de anúncios** também devem
    permanecer apenas em memória, conforme a interpretação conservadora
    das condições aplicáveis.
-   Utilizar apenas operações de leitura da Browse API, como
    `item_summary/search` e, se necessário, `getItem`. Não implementar
    checkout, lances, operações de venda, tokens de usuário ou acesso a
    contas.
-   Respeitar limites de taxa e tratar `429` e falhas transitórias com
    política limitada de espera/repetição.
-   Não habilitar tracing, monitoramento de erros, captura de variáveis
    locais, cache HTTP ou ferramentas de depuração que possam registrar
    conteúdo de anúncios.
-   Não gravar respostas reais da eBay em fixtures, testes, relatórios,
    documentação ou exemplos.

### Verificações obrigatórias na auditoria e no planejamento

O Antigravity deve identificar possíveis pontos de persistência direta e
indireta, incluindo:

-   banco de dados e ORM;
-   cache em memória e em disco;
-   bibliotecas HTTP e seus mecanismos de cache;
-   logs estruturados;
-   tracing e monitoramento de erros;
-   arquivos temporários;
-   dumps de exceções e variáveis locais;
-   histórico de execução de notebooks e ferramentas de desenvolvimento;
-   filas, sessões e armazenamento de estado;
-   relatórios e ferramentas de avaliação.

O plano de implementação deve incluir testes que verifiquem, com dados
sintéticos, que o fluxo de busca não persiste dados de anúncios, não
inclui payloads nos logs e não mantém respostas de anúncios em cache
entre requisições.

Se a equipe decidir futuramente persistir dados da eBay, interrompa essa
mudança e registre-a como decisão de produto/conformidade. Não
implemente por iniciativa própria. A equipe deverá verificar os
requisitos aplicáveis, inclusive eventuais obrigações de
exclusão/notificação, antes de alterar a arquitetura.

------------------------------------------------------------------------

## 4. Arquitetura-alvo de referência

A arquitetura abaixo descreve o comportamento desejado. Ela é uma
referência para a auditoria e para o planejamento, não uma ordem para
criar todos os componentes do zero. O Antigravity deve mapear o que já
existe, recomendar o reaproveitamento possível e propor a menor
sequência segura de mudanças.

### 4.1. Fluxo lógico esperado

``` text
Requisição (texto livre + contexto)
        |
        v
[1] Entender intenção
        |
        +---- faltam informações? ----> solicitar esclarecimento
        |
        v
[2] Extrair filtros rígidos e requisitos flexíveis
        |
        v
[3] Gerar consultas de busca adequadas à eBay
        |
        v
[4] Consultar eBay Browse API
        |     (candidatos somente em memória)
        v
[5] Deduplicar e calcular similaridade semântica
        |
        v
[6] Validar os melhores candidatos contra os requisitos
        |     (evidências disponíveis; ausências = desconhecido)
        v
[7] Há candidatos suficientes?
        |
        +---- não ----> avaliar relaxamento limitado e transparente
        |
        v
[8] Re-ranking multicritério
        |
        v
[9] Gerar explicações fundamentadas nos dados recuperados
        |
        v
Resposta da API
```

A implementação pode usar LangGraph para representar etapas, estado
tipado e transições condicionais, desde que isso seja compatível com a
stack atual e não introduza complexidade desnecessária.

### 4.2. Contrato HTTP de referência

A auditoria deve identificar endpoints já existentes e comparar seus
contratos com esta proposta. O `IMPLEMENTACOES.md` deve indicar se cada
endpoint será mantido, adaptado ou criado.

-   `POST /v1/search`: busca pontual a partir de texto livre e contexto.
-   `POST /v1/chat`: interação em turnos, caso o projeto realmente
    necessite de estado conversacional.
-   `GET /v1/health`: estado do serviço e indicação de configuração das
    credenciais, sem revelar valores.

Proposta inicial de entrada para busca:

``` json
{
  "query": "notebook para jogos casuais até 4000 reais",
  "session_id": "opcional",
  "budget_brl": 4000,
  "locale": "pt-BR"
}
```

Proposta inicial de resposta:

``` json
{
  "status": "ok",
  "results": [],
  "applied_filters": {},
  "relaxations": [],
  "request_id": "identificador-da-requisicao"
}
```

Os exemplos são ilustrativos. O Antigravity deve examinar os esquemas
existentes e propor um contrato consistente, documentando mudanças
incompatíveis e evitando duplicar endpoints ou modelos já implementados.

O objeto de produto poderá conter, quando disponíveis e permitidos:

-   identificador do item;
-   título;
-   preço e moeda;
-   estimativa de conversão para BRL, claramente identificada como
    aproximada;
-   condição;
-   imagem e URL do anúncio;
-   informações do vendedor;
-   informações de frete;
-   componentes de score;
-   validação por requisito;
-   explicação.

Campos ausentes devem ser opcionais ou explicitamente desconhecidos.
Nunca inventar dados. A resposta ao consumidor é transitória; isso não
autoriza armazenamento no servidor.

### 4.3. Abstração de fonte de busca

Considere uma interface única, por exemplo:

`SearchBackend.search(query, filters) -> list[Candidate]`

A implementação inicial desejada é `EbayLiveBackend`. A abstração deve
ser simples e útil, sem criar camadas genéricas desnecessárias. O
restante da aplicação não deve depender diretamente de detalhes internos
do cliente HTTP da eBay.

### 4.4. Integração eBay --- pontos a verificar

Consulte a documentação oficial atual antes de implementar. Os itens
abaixo são referências técnicas iniciais, não garantias de que todos os
campos, parâmetros ou permissões estejam disponíveis para a conta:

-   Autenticação por OAuth `client_credentials` para token de aplicação.
-   Endpoint de token: `https://api.ebay.com/identity/v1/oauth2/token`.
-   Busca por
    `GET https://api.ebay.com/buy/browse/v1/item_summary/search`.
-   Recuperação de detalhes por `getItem` somente quando houver
    justificativa, considerando limites e custo de chamadas.
-   Mercado inicial de referência: `EBAY_US`, preços em USD e anúncios
    predominantemente em inglês.
-   Orçamento em BRL convertido para USD por mecanismo configurável. Uma
    taxa fixa de ambiente pode servir como primeira versão, desde que o
    resultado seja identificado como estimativa e a origem/data da taxa
    sejam transparentes.
-   Campos de frete, reputação, atributos técnicos e descrição podem
    estar ausentes ou variar conforme o anúncio.

O planejamento deve contemplar a validação de acesso às credenciais de
produção, permissões, limites, paginação, filtros, campos retornados,
erros e políticas aplicáveis.

### 4.5. Entendimento da intenção e validação de requisitos

Responsabilidades esperadas:

-   Extrair categoria, orçamento, preferências e perfil de uso a partir
    do pedido.
-   Distinguir restrições rígidas de preferências flexíveis.
-   Solicitar esclarecimentos quando faltar informação essencial.
-   Validar a saída da LLM por esquema.
-   Manter verificações determinísticas em código sempre que possível.

Restrições rígidas, como orçamento máximo e categoria obrigatória, devem
ser verificadas por código, não delegadas exclusivamente à LLM.

Perfis de uso --- por exemplo, jogos casuais, programação e edição de
vídeo --- devem ser apoiados por critérios configuráveis e revisáveis
pela equipe. A LLM pode interpretar o pedido e selecionar/adaptar um
perfil, mas não deve inventar critérios técnicos arbitrariamente.

O validador de produtos deve receber somente os atributos recuperados e
a especificação de requisitos. Deve retornar um resultado estruturado
por critério, com:

-   `atende`;
-   `parcial`;
-   `nao_atende`;
-   `desconhecido`.

Cada avaliação deve indicar a evidência utilizada. Se o dado necessário
não estiver disponível, o resultado deve ser `desconhecido`, nunca uma
suposição apresentada como fato.

### 4.6. Embeddings e busca semântica

A direção inicial é utilizar embeddings multilíngues, capazes de
relacionar consultas em português com anúncios em inglês.

Modelos da família `sentence-transformers`, como multilingual-e5 e
paraphrase-multilingual-MiniLM, podem ser avaliados. Não escolha um
modelo apenas por popularidade: proponha uma comparação reproduzível com
conjunto de avaliação sintético e critérios explícitos.

A representação textual de cada candidato poderá considerar, quando
disponíveis:

-   título;
-   marca e modelo;
-   descrição;
-   categoria;
-   atributos técnicos;
-   condição.

Os vetores e representações derivados de anúncios devem permanecer em
memória, conforme a seção 3. Carregar os pesos do modelo local no início
do serviço é permitido desde que isso não armazene dados de anúncios.

### 4.7. Re-ranking multicritério

O score final pode combinar, de forma configurável:

-   similaridade semântica;
-   atendimento aos requisitos;
-   adequação ao orçamento;
-   reputação do vendedor;
-   frete, quando disponível.

Os pesos precisam ser documentados e testados. Campos ausentes não devem
receber valores inventados. A API deve expor componentes suficientes
para tornar a ordenação compreensível, sem expor detalhes internos
sensíveis.

### 4.8. Qualidade e avaliação

O plano deve incluir uma estratégia de avaliação sem depender de
armazenar anúncios reais da eBay.

Considere métricas adequadas às funcionalidades implementadas, por
exemplo:

-   qualidade da extração de intenção;
-   cumprimento de restrições rígidas;
-   Precision@K e Recall@K;
-   NDCG@K e MRR;
-   taxa de afirmações sem evidência;
-   latência total e por etapa;
-   número de chamadas à eBay e ao provedor de LLM;
-   comportamento em resultados vazios e dados incompletos.

Use casos sintéticos e/ou exemplos criados manualmente. Não salve
respostas reais da eBay para compor o benchmark.

------------------------------------------------------------------------

## 5. Primeira entrega: auditoria do repositório

Antes de qualquer implementação, crie `AUDITORIA.md` na raiz do projeto.

A auditoria deve conter:

1.  **Mapa do repositório:** árvore relevante de pastas, linguagem,
    framework web, gerenciador de dependências, provedor de LLM,
    bibliotecas de IA, versões e comandos para executar/testar.
2.  **Estado funcional:** o que está implementado, parcialmente
    implementado, não implementado ou não foi possível verificar.
3.  **Comparação arquitetural:** para cada componente da seção 4,
    indicar o que existe, sua qualidade aparente, arquivos relacionados
    e recomendação inicial: manter, adaptar, remover ou criar.
4.  **Integrações de dados:** localizar DummyJSON, Mercado Livre e
    quaisquer outras fontes; identificar módulos, funções, variáveis de
    ambiente, dependências e testes relacionados.
5.  **Persistência:** identificar escritas em disco, bancos, cache,
    sessões, logs, tracing, arquivos temporários e qualquer caminho que
    possa receber dados de anúncios ou derivados.
6.  **Segurança:** verificar nomes de variáveis, arquivos potencialmente
    versionados com segredos e proteção do `.env`, sem revelar valores
    secretos.
7.  **Testes:** inventariar testes existentes, cobertura funcional
    observável, lacunas e testes que podem ser afetados pela migração.
8.  **Execução local:** descrever como configurar e executar o projeto,
    apontando problemas encontrados. Não exibir valores de variáveis
    secretas.
9.  **Riscos e regressões:** listar funcionalidades que podem quebrar,
    dependências ocultas, decisões arquiteturais questionáveis e riscos
    de conformidade.
10. **Perguntas para a equipe:** listar apenas dúvidas que realmente
    dependem de decisão humana, com contexto e impacto.
11. **Recomendações:** propor uma direção técnica baseada no código
    real, distinguindo fatos encontrados de sugestões.

A auditoria deve apontar caminhos de arquivos e símbolos relevantes
sempre que possível. Não deve copiar conteúdo de `.env`, respostas reais
da eBay ou segredos.

------------------------------------------------------------------------

## 6. Segunda entrega: plano de implementação criado pelo Antigravity

Depois de concluir a auditoria, crie `IMPLEMENTACOES.md` na raiz do
projeto.

**Este arquivo deve ser elaborado pelo Antigravity com base na inspeção
real do repositório e na comparação com a arquitetura deste documento.**
Não copie mecanicamente a ordem da arquitetura nem crie uma lista
genérica. O objetivo é transformar as lacunas reais em um plano
executável.

### 6.1. Como estruturar o plano

Organize o trabalho em etapas lógicas. A quantidade e a ordem das etapas
devem resultar da auditoria. Como referência, o plano pode precisar
contemplar:

-   preparação, segurança e configuração;
-   validação de requisitos e conformidade da eBay;
-   adequação ou remoção de integrações antigas;
-   cliente e autenticação eBay;
-   normalização dos produtos;
-   entendimento de intenção e geração de consultas;
-   recuperação, deduplicação e busca semântica;
-   validação dos requisitos;
-   ranking e explicações;
-   orquestração conversacional;
-   endpoints, autenticação e limites B2B;
-   testes, avaliação, observabilidade e documentação;
-   validação integrada e preparação para entrega.

Esses tópicos são possibilidades, não uma sequência obrigatória.
Combine, divida, remova ou reordene etapas conforme o estado do código e
as dependências identificadas.

### 6.2. Detalhamento obrigatório de cada etapa

Para cada etapa, inclua:

-   **Objetivo:** qual resultado funcional ou técnico será alcançado.
-   **Motivação:** qual lacuna ou problema identificado na auditoria
    justifica a etapa.
-   **Tarefas:** ações pequenas, específicas e verificáveis.
-   **Arquivos/módulos envolvidos:** caminhos existentes ou prováveis;
    marque como "a confirmar" quando ainda não souber.
-   **Dependências:** tarefas ou decisões que precisam acontecer antes.
-   **Critérios de aceite:** evidências objetivas para considerar a
    tarefa concluída.
-   **Testes:** quais testes unitários, integração, contrato, segurança
    ou avaliação devem ser executados.
-   **Riscos:** possíveis regressões, custos, limites de API e pontos de
    conformidade.
-   **Estado:** usar `Pendente`, `Em andamento`, `Bloqueada` ou
    `Concluída`.

Cada tarefa deve ser pequena o bastante para ser implementada e revisada
em um ciclo de trabalho. Evite tarefas vagas como "implementar IA",
"melhorar backend" ou "integrar eBay".

### 6.3. Priorização e dependências

-   Ordene as etapas por dependência técnica e risco, não apenas pela
    ordem em que aparecem na arquitetura.
-   Separe bloqueios externos --- como liberação de credenciais,
    permissão de produção ou decisão da equipe --- de trabalho que pode
    ser realizado localmente.
-   Identifique o caminho crítico.
-   Marque tarefas opcionais e melhorias futuras separadamente do escopo
    necessário para a primeira entrega completa do backend.
-   Não transforme decisões ainda não aprovadas em tarefas autorizadas
    para execução.
-   Evite criar trabalho duplicado quando uma funcionalidade já estiver
    implementada e testada.

### 6.4. Definição de conclusão

O `IMPLEMENTACOES.md` deve definir o que significa concluir o backend,
com critérios verificáveis. Considere, conforme aplicável:

-   fluxo de busca integrado à eBay real;
-   funcionamento nas categorias escolhidas pela equipe;
-   ausência de dependência de DummyJSON e Mercado Livre no caminho de
    produção;
-   cumprimento dos requisitos de não persistência;
-   testes de logs e cache;
-   validação de orçamento e atributos;
-   avaliação da qualidade das recomendações;
-   API documentada e testada;
-   configuração segura;
-   README atualizado;
-   limitações e decisões pendentes explicitadas.

Não declare o projeto 100% concluído se houver bloqueios de credenciais,
conformidade, testes ou funcionalidades obrigatórias ainda não
resolvidos.

### 6.5. Manutenção do plano

O `IMPLEMENTACOES.md` deve ser o documento vivo de acompanhamento:

-   Atualize o estado das tarefas somente após executar e verificar os
    critérios de aceite.
-   Registre brevemente o que foi feito e os testes executados.
-   Se a implementação revelar uma dependência ou problema não
    identificado, ajuste o plano antes de continuar.
-   Não apague tarefas concluídas; mantenha o histórico resumido.
-   Se uma tarefa deixar de ser necessária, marque-a como cancelada e
    explique o motivo.
-   Ao final de cada etapa, faça uma revisão do plano e das dependências
    seguintes.

------------------------------------------------------------------------

## 7. Estratégia de testes e desenvolvimento

O plano deve aproveitar os testes existentes e propor os testes
adicionais necessários.

Diretrizes:

-   **Unitários:** utilizar respostas sintéticas e fixtures inventadas,
    nunca capturas reais de anúncios da eBay.
-   **Integração com eBay real:** testes manuais ou explicitamente
    habilitados por variável de ambiente, desativados por padrão em CI
    para evitar consumo involuntário de cota.
-   **DummyJSON:** se permanecer, deve estar isolado em testes e
    claramente identificado como fonte de fixture offline; não deve ser
    uma dependência de produção.
-   **Conformidade:** verificar ausência de persistência, payloads em
    logs e cache de anúncios entre requisições.
-   **Contratos:** validar esquemas de entrada e saída dos endpoints.
-   **Resiliência:** testar timeouts, erros HTTP, `429`, `5xx`,
    respostas incompletas, falhas de LLM e resultados vazios.
-   **Qualidade:** manter um conjunto de avaliação sintético, versionado
    e reproduzível.
-   **Segurança:** verificar autenticação, limites, validação de entrada
    e ausência de exposição de credenciais.

Não reduza a cobertura existente sem justificativa. Não desative testes
apenas para fazer a suíte passar.

------------------------------------------------------------------------

## 8. Escopo fora desta fase

Salvo aprovação explícita da equipe, não implementar nesta fase:

-   front-end;
-   banco vetorial persistente;
-   persistência de anúncios ou dados derivados da eBay;
-   integração com outras lojas;
-   checkout, lances ou funcionalidades de venda;
-   tokens de usuário ou acesso a contas da eBay;
-   infraestrutura empresarial complexa que não seja necessária para
    validar o backend.

O Antigravity pode registrar essas possibilidades como ideias futuras,
sem incluí-las no escopo obrigatório.

------------------------------------------------------------------------

## 9. Riscos conhecidos para considerar no planejamento

  -----------------------------------------------------------------------
  Risco                               Tratamento esperado
  ----------------------------------- -----------------------------------
  Browse API de produção indisponível Confirmar acesso; registrar
  ou sem permissão                    bloqueio e não substituir
                                      silenciosamente por mock em
                                      produção.

  Limites de taxa e cota              Medir chamadas por requisição,
                                      limitar consultas e tratar `429`
                                      com política controlada.

  Catálogo em inglês e preços em USD  Avaliar embeddings multilíngues e
                                      exibir conversão como estimativa.

  Campos ausentes                     Modelos opcionais; marcar como
                                      desconhecido; nunca inventar.

  LLM inventando requisitos ou        Perfis configuráveis, validações
  atributos                           determinísticas e evidências
                                      explícitas.

  Persistência acidental              Auditar cache, logs, tracing,
                                      temporários e testes automatizados.

  Vazamento de credenciais            Variáveis de ambiente, `.gitignore`
                                      e logs sem valores secretos.

  Migração quebrar funcionalidades    Inventário de funcionalidades,
  existentes                          testes de regressão e remoção
                                      incremental.

  Excesso de chamadas ou latência     Limites por requisição, timeouts,
                                      concorrência controlada e medição
                                      por etapa.

  Estado conversacional inconsistente Documentar a limitação de sessões
  entre instâncias                    em memória e propor evolução
                                      somente se necessária.
  -----------------------------------------------------------------------

------------------------------------------------------------------------

## 10. Resultado esperado desta primeira execução

Nesta primeira execução, entregue somente:

1.  `AUDITORIA.md`, com o diagnóstico real do repositório.
2.  `IMPLEMENTACOES.md`, com as etapas e tarefas criadas a partir desse
    diagnóstico.
3.  Um resumo no chat contendo:
    -   stack e arquitetura encontradas;
    -   funcionalidades existentes que podem ser reaproveitadas;
    -   principais lacunas;
    -   riscos e bloqueios;
    -   quantidade de etapas e tarefas propostas;
    -   decisões que precisam da equipe;
    -   recomendação de ordem para iniciar a implementação.

**Não altere código, dependências, arquivos de configuração ou
integrações nesta primeira execução.** Não execute nenhuma tarefa de
implementação. Aguarde a aprovação explícita da equipe para começar.
