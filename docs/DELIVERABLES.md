# Entregas e tarefas

Cada tarefa produz evidência revisável. Parar ao fim de cada entrega e aguardar aprovação antes de iniciar a seguinte. Caminhos são propostos e podem mudar com decisão arquitetural registrada.

| ID | Tarefa / artefatos | Verificação e critério de conclusão | Depende |
|---|---|---|---|
| D0 | Planejamento nos seis arquivos de docs | Ambiente, fonte IBGE, modelo, riscos, fases e tarefas revisáveis | Concluída |
| D1.1 | Estrutura backend/frontend, .gitignore, .env.example, README | Instalação nativa descrita; sem contêineres | Aprovação D0 |
| D1.2 | backend/pyproject.toml, lockfile, frontend/package.json/lockfile | Resolver versões em venv; verificar wheels/Python/Linux | D1.1 |
| D1.3 | Configuração, logs e schemas em backend/app/core e schemas | Configuração inválida rejeitada; sem segredos versionados | D1.1–D1.2 |
| D1.4 | PostgreSQL/PostGIS, SQLAlchemy e Alembic | Migração local reversível/reproduzível; instalação só após aprovação de D1 | D1.2–D1.3 |
| D1.5 | Testes mínimos, Ruff e documentos de instalação | Testes básicos passam; ferramentas não alteram sistema global | D1.2–D1.4 |
| D2.1 | Validação do catálogo IBGE em backend/app/geography/municipalities.py; manifesto em data/manifests | Snapshot SHA-256 repetido; 142 códigos únicos | Concluída |
| D2.2 | Importar Localidades do Brasil Censo 2022 | Uma sede oficial por código IBGE; EPSG:4674→4326; sem centroide | Concluída |
| D2.3 | Leitura PBF com PyOsmium em backend/app/geography/osm_importer.py | Snapshot nacional, checksum/licença, vias highway, tempo e RSS documentados | Concluída |
| D2.4 | Relatório de qualidade em docs/DATA_QUALITY_D2.md e data/manifests | Catálogo, sedes e origem PBF auditados; encaixe e conectividade dirigidos passam para D3.1, pois requerem o grafo | Concluída com fronteira de escopo registrada |
| D3.1 | Nós/arcos e IDs físicos em backend/app/graph | Testar sentidos, paralelas, duplicadas, reversos e conversões | D2 |
| D3.1a | Encaixe sede–rede e conectividade do perfil automotivo | Distância de encaixe, acesso, mão única e municípios alcançáveis medidos no grafo construído | D3.1 |
| D3.2 | Dijkstra/A* em backend/app/shortest_path | Custos iguais nos casos pequenos; caminhos ausentes/asimétricos e arestas recuperáveis | D3.1 |
| D3.3 | Matrizes em backend/app/routing/matrices | Matrizes dirigidas de distância e tempo independentes; hash, unidade e caminhos conferidos | D3.2 |
| D3.4 | Cache e benchmarks de 5/10/20/50/142 cidades | Chaves imutáveis; registrar nós/arestas, RAM e duração | D3.3 |
| D4.1 | Open TSP/P1 em backend/app/optimization/visitation/tsp | Origem fixa, fim aberto, cobertura e métricas das arestas corretas | D3 |
| D4.2 | O1/O2 e baseline heurístico | Testar origem fixa e candidatas O2; origem municipal atendida uma vez; limites registrados | D4.1 |
| D4.3 | Validação exata de instâncias pequenas | Enumerar pequenas rotas e classificar prova/viável corretamente | D4.1–D4.2 |
| D5.1 | Open mTSP k=2/3 em backend/app/optimization/visitation/mtsp | Términos artificiais; cobertura única; todos os agentes ativos | D4 |
| D5.2 | Objetivos A/B e métricas individuais | Validar soma de distância versus máximo de tempo; service time 0 e não nulo | D5.1 |
| D5.3 | Balanceamento, seeds e limites | Registrar carga, início simultâneo, reprodutibilidade e timeout | D5.1–D5.2 |
| D6.1 | Sobreposição em backend/app/optimization/overlap | Métrica por IDs físicos; testar sentidos opostos e repetição pelo mesmo agente | D5 |
| D6.2 | P1/P2 | P1 permite compartilhamento; P2 aplica lambda/caminhos; lambda zero compara baseline | D6.1 |
| D6.3 | P3 exata em grafos pequenos | Casos viável/inviável; compartilhamento entre agentes validado nas arestas | D6.1 |
| D6.4 | P3 em rede real e relatório de limites | Distinguir certificado de “nenhuma solução encontrada”; medir gargalos | D6.3 |
| D7.1 | Repositórios, migrações e manifestos | Persistir dados/configuração/hash/seed/ambiente sem blobs geográficos grandes | D1, D6 |
| D7.2 | Worker local | Jobs não bloqueiam HTTP; estados, falhas/interrupções recuperáveis | D7.1 |
| D7.3 | API FastAPI e OpenAPI em backend/app/api | Contratos para saúde, municípios, datasets, configurações, jobs, métricas e GeoJSON | D2, D6, D7.1–D7.2 |
| D8.1 | Formulários React/TypeScript/Vite | Validar número de agentes, origem, objetivo e política na API | D7.3 |
| D8.2 | MapLibre/GeoJSON | Rotas rodoviárias reais, legenda por agente, início/fim, compartilhadas e crédito OSM | D2, D7.3–D8.1 |
| D8.3 | Comparação e exportação | Comparar métricas/status; nenhuma simulação apresentada como dado real | D7.3–D8.2 |
| D9.1 | Orçamento/configurações em experiments/configurations | Estimar CPU/RAM/disco/tempo antes dos 28 cenários; congelar dados/matrizes | D3–D6 |
| D9.2 | Runner em experiments/runners | Dez repetições heurísticas configuráveis; IDs, seeds e interrupções persistidos | D9.1 |
| D9.3 | Resultados/análise em experiments/results e analysis | Métricas das arestas; estatísticas reproduzíveis; classificação científica correta | D9.2 |
| D9.4 | Sensibilidade P2 | Lambdas acordados para k=2/3; comparar qualidade e sobreposição | D9.2 |
| D10.1 | README e instalação nativa | Reproduzir venv, banco, API, frontend e testes, sem Docker/sudo implícito | D1–D9 |
| D10.2 | Metodologia, decisões e relatório final | Dados/licenças, formulação, versões, limitações, resultados e reprodução | D0–D9 |

## Estado da D3 em 2026-10-09

D3.1–D3.4 concluídas. Testes focados: 16 passaram. Cache e manifesto: `data/manifests/travel-matrix-mt.json`, hash `043328bf1f67cf5d7b4cd9a4d2304169650f9e5acee5a057c1f4609e9b19a7a3`. Foram processadas 142 sedes; todas encaixaram até 108,92 m (média 40,12 m), e não houve pares dirigidos inalcançáveis em distância ou tempo. O cache gzip tem 316.512.806 bytes. A execução completa durou 1.538,863 s, pico de 25.288,43 MiB e 8 workers. Tempos do benchmark: 5 cidades 3,619 s; 10 5,028 s; 20 15,335 s; 50 74,481 s; 142 592,301 s. Binário/relatórios grandes permanecem locais; manifesto e código estão versionados.

## Estado da D4 em 2026-10-10

D4.1–D4.3 concluídas em `backend/app/optimization/visitation/tsp.py`. Open TSP usa arcos dirigidos, origem fixa ou variável, destino artificial sem custo de retorno e baseline vizinho mais próximo. Até 9 sedes, enumera todas as ordens e distingue `optimal_proven` de `infeasible_proven`; em 142 sedes, reporta apenas `feasible`.

Cinco testes sintéticos passaram. Quatro cenários reais foram calculados e auditados contra o cache D3: cada rota cobre as 142 sedes uma vez, tem 141 pernas abertas, e custos/caminhos dirigidos conferem com a matriz. Cuiabá fixa (O1): distância 10.386.464,340 m, 21,8% abaixo do baseline; tempo 545.267,081 s, 21,1% abaixo. Origem variável (O2): código IBGE 5107578 nos dois objetivos; distância 10.023.852,615 m, 20,5% abaixo do baseline; tempo 539.273,514 s, 17,0% abaixo. Nenhum resultado de 142 cidades tem prova de ótimo global. Detalhes, sequências e auditoria: `data/manifests/tsp-benchmarks-mt.json`.

Runner reproduzível: `experiments/runners/d4_open_tsp.py`. Os quatro cenários executaram em sequência, 300 s de busca por cenário, dentro de cgroup limitado a 25.000.000.000 bytes; pico cgroup observado 16.809.508.864 bytes. Revalidação do relatório também exige o mesmo teto. O processamento paralelo de matrizes D3 continua em 8 workers; TSP usou um solver por vez para manter o limite de RAM.

## Estado da D5 em 2026-10-10

D5.1–D5.3 concluídas em `backend/app/optimization/visitation/mtsp.py`: mTSP aberto com k=2/3, origem O1 fixa ou O2 variável, objetivos de distância total ou tempo máximo, baseline balanceada e service time configurável. Os 7 testes focados passaram; suíte backend: 33 passaram. Os 8 cenários reais atenderam cada uma das 142 sedes exatamente uma vez, ativaram todos os agentes e tiveram caminhos abertos validados contra 186.439 IDs de aresta física do grafo.

| Cenário | Origem IBGE | Distância total | Tempo máximo |
|---|---:|---:|---:|
| O1 k=2 distância | 5103403 | 10.020,7 km | 87,40 h |
| O1 k=2 tempo | 5103403 | 13.324,4 km | 92,94 h |
| O1 k=3 distância | 5103403 | 9.938,1 km | 87,74 h |
| O1 k=3 tempo | 5103403 | 12.122,2 km | 56,13 h |
| O2 k=2 distância | 5103437 | 10.266,4 km | 131,02 h |
| O2 k=2 tempo | 5105150 | 12.759,5 km | 88,82 h |
| O2 k=3 distância | 5107198 | 10.122,6 km | 108,30 h |
| O2 k=3 tempo | 5107305 | 13.353,3 km | 61,33 h |

Todos os resultados reais são `feasible`, sem prova de ótimo. O2 avaliou as 142 origens por cenário dentro de um limite compartilhado de 300 s. Execução reproduzível: `.venv/bin/python experiments/runners/d5_open_mtsp.py --time-limit-s 300 --workers 8 --output data/processed/mtsp-d5-reproduction.json`. Com 8 processos em 8 CPUs, levou 430,778 s; o cgroup limitou memória a 24.999.997.440 bytes e observou pico de 15.284.240.384 bytes. Manifesto completo, rotas, IDs e métricas: `data/manifests/mtsp-benchmarks-mt.json`.

## Estado da D6 em 2026-10-10

D6.1–D6.4 implementadas em `backend/app/optimization/overlap.py`. A sobreposição é calculada pelos IDs físicos das arestas: sentidos opostos contam como o mesmo segmento, e repetição pelo mesmo agente só conta uma vez. A busca exaustiva seleciona rotas ativas que cobrem cada sede uma vez, pontua P1/P2 e rejeita interseções entre agentes em P3. Os 5 testes focados passaram; suíte backend: 38 passaram.

No relatório real, P1 mediu sobreposição entre 0 e 897,36 km nos oito cenários D5. Com λ=1, a busca de candidatas reduziu O2/k=2/tempo de 206,45 para 164,10 km, mudando a origem de 5105150 para 5107925; o tempo máximo subiu de 319.742 para 331.049 s. Em O2/k=3/tempo, reduziu de 897,36 para 857,02 km; o tempo máximo subiu de 220.778 para 273.868 s. λ=0 reproduz P1.

P3 encontrou combinação sem trechos compartilhados em 2 dos 8 cenários: O1/k=2/distância e O2/k=2/distância. Nos outros seis, retornou `not_found`; isso não prova inviabilidade da rede. Nos grafos sintéticos, a enumeração exaustiva distingue conjunto viável de conjunto inviável de candidatas.

Limite medido: a busca real combina rotas geradas pelo OR-Tools com até 60 s por variante; cada perna mantém o caminho D3 mais curto na métrica escolhida. A enumeração é exata dentro desse conjunto finito, mas não inclui todos os caminhos rodoviários alternativos e não prova ótimo global de P2 ou inviabilidade global de P3. As 56 variantes adicionais usaram `spawn`, 8 workers/CPUs e cgroup limitado a 25.000.000.000 bytes; pico observado 15.259.635.712 bytes. Reproduzir: `.venv/bin/python experiments/runners/d6_overlap.py --variants 8 --workers 8 --time-limit-s 60 --output data/processed/overlap-d6-reproduction.json`. Relatório com métricas, IDs físicos, seeds e origens: `data/manifests/overlap-benchmarks-mt.json`.

## Estado da D7 em 2026-10-10

D7.1–D7.3 implementadas: migração Alembic para progresso/resultados, repositório SQLAlchemy e worker local serial que executa D4–D6 e persiste estado, logs, falhas, cancelamento e recuperação. A API expõe saúde, municípios, datasets, configurações, algoritmos, jobs e endpoints por execução para métricas, rotas, GeoJSON e exportação JSON; `/api/jobs` também está disponível como alias de `/api/experiments`.

Verificação: suíte backend com 44 testes aprovados (um aviso de depreciação do Starlette/AnyIO); migração SQLite aplicada e revertida com sucesso. Os testes da API usam banco temporário e grafo sintético, sem carregar o grafo rodoviário de 300 MB.

## Estado da D8 em 2026-10-10

D8.1–D8.3 implementadas no frontend React/TypeScript/Vite: formulário de experimento, histórico e logs, progresso, cancelamento, seleção de cenários, comparação entre execuções e exportação JSON. O mapa MapLibre consome GeoJSON das arestas rodoviárias reais e colore agentes; as rotas comparadas aparecem em camada tracejada. Não há linha reta aproximada nem tiles externos. A interface informa o limite de RAM e o escopo de `not_found` em P3.

Build TypeScript/Vite aprovado. A tela principal carrega o mapa sob demanda, mantendo o bundle inicial em aproximadamente 75 KB gzip.

## Estado da D9 em 2026-10-10

Configuração D9 congelada em `experiments/configurations/d9-mt.json`: 28 cenários estruturais × 10 repetições, seeds próprias por estágio, limites de 300 s por cenário D4/D5 e 60 s por variante D6. O orçamento foi registrado antes da execução em `experiments/analysis/d9-budget-mt-2026-10-10.md`: até 8 CPUs, estimativa de 6,84 h com margem de 20%, reserva de 2 GB e `MemoryMax=25.000.000.000` bytes. Os hashes do grafo e das matrizes conferem com o snapshot configurado.

O lote foi executado em `d9-mt-20261010.service`, com até oito CPUs e teto de RAM aplicado ao processo e a todos os workers. As dez repetições terminaram em 6 h 24 min (19 h 37 min de CPU). O cgroup registrou pico de 15,6 GiB sob `MemoryMax=25.000.000.000` bytes; `oom_kill=0` e `NRestarts=0`.

As dez repetições geraram 280 observações principais: 220 `feasible` e 60 `not_found`. As 60 ocorrências são seis configurações P3 sem combinação no conjunto de candidatas; não provam inviabilidade da rede. D4 teve 40/40 observações viáveis. D5 teve 80/80 viáveis, cobrindo os oito cenários em dez repetições. A sensibilidade P2 contém 400 observações (oito cenários × cinco valores de λ × dez repetições).

Em P3, duas das oito configurações de distribuição tiveram combinação sem arestas físicas compartilhadas; nas outras seis, a busca não encontrou combinação dentro das candidatas e do limite de tempo. Em P2, λ=1 reduziu a sobreposição mediana de 186,0 para 164,1 km em O2/k=2/tempo, com aumento do objetivo composto de 320.342 para 331.039 s; em O2/k=3/tempo, caiu de 897,4 para 857,0 km, enquanto o objetivo composto subiu de 220.778 para 273.868 s. Isso descreve o conjunto avaliado e não prova ótimo global.

O relatório completo, `summary.json` e CSVs estão em `experiments/analysis/d9-mato-grosso-2026-10-10/`. O índice e o snapshot registram seeds, hashes dos artefatos e procedência dos commits: cálculo `39f20556b8bd956932c63f77220085f66d1b721a`, análise `cb5a8d75937c50857942bfb6148cb3a54d3958ef`, HEAD inicial `dfb1d49fd0ae6389bb2a6a1633aca7f9719143ab`. O snapshot também registra que o worktree estava marcado como dirty no início; a nota identifica o commit de cálculo como fonte do código D4–D6/configuração. Os arquivos gzip detalhados por repetição ficam no diretório local ignorado `experiments/results/`; podem ser regenerados pelos comandos em [Reproduzir a D9](../README.md#reproduzir-a-d9).

## Estado final da D10 em 2026-10-10

D10.1–D10.2 concluídas: README e instalação nativa descrevem ambiente, execução, limite de RAM e reprodução; arquitetura, modelo, decisões e protocolo registram dados, formulação, algoritmos e limitações. O relatório D9 contém estatísticas por cenário, D5 separado, sensibilidade P2, hashes e status. A velocidade é modelada, não tráfego observado; resultados de 142 sedes são heurísticos; `not_found` e busca exaustiva no pool não equivalem a prova sobre toda a rede.

## Portões de aceite

- D1: ambiente reproduzível e dependências compatíveis antes da ingestão.
- D2: 142 municípios comparados com fonte oficial; sedes e rede documentadas.
- D3: caminhos e custos consistentes antes de TSP.
- D4/D5: cobertura única, rota aberta e agentes ativos antes da sobreposição.
- D6: política física e status honestos antes de API/frontend.
- D9: orçamento apresentado antes de experimentos caros.
- D10: separar medição, hipótese, heurística e prova.

Os testes das entregas D1–D8 foram executados e registrados nas respectivas seções. D9 terminou com 10/10 repetições e D10 registra método, resultados, procedência e limites científicos.
