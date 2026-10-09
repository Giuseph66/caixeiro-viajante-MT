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

O código D3.1–D3.4 e testes sintéticos foram adicionados. Ainda não houve execução dos testes D3 nem leitura do PBF para construir a rede. Matriz real, conectividade das 142 sedes, hashes de saída e benchmarks seguem pendentes. O projeto permanece na D3; D4 não começou.

## Portões de aceite

- D1: ambiente reproduzível e dependências compatíveis antes da ingestão.
- D2: 142 municípios comparados com fonte oficial; sedes e rede documentadas.
- D3: caminhos e custos consistentes antes de TSP.
- D4/D5: cobertura única, rota aberta e agentes ativos antes da sobreposição.
- D6: política física e status honestos antes de API/frontend.
- D9: orçamento apresentado antes de experimentos caros.
- D10: separar medição, hipótese, heurística e prova.

Os testes das entregas futuras continuam planejados. Na D1/D2 foram executados os testes básicos registrados nos respectivos relatórios.
