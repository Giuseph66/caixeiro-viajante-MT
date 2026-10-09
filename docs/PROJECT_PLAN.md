# Plano do projeto

**Estado:** Entregas 0, 1 e 2 concluídas; implementação D3 em andamento. A geração do grafo/matrizes aguarda confirmação explícita para comandos longos.
**Data:** 2026-10-08. **Diagnóstico D0:** nenhum código, pacote ou dado rodoviário existia antes da implementação.

## Diagnóstico

A pasta estava vazia: sem código, manifests, documentação ou Git. O índice do codebase-memory também não encontrou código. Não há arquitetura existente a preservar.

| Recurso | Encontrado |
|---|---|
| SO | Kali Linux, x86_64 |
| CPU | 8 CPUs lógicas |
| RAM | 35 GiB; 24 GiB disponíveis |
| Swap | 0 |
| Disco em / | 112 GiB disponíveis |
| Python | 3.13.12 |
| Node.js / npm | 24.12.0 / 11.6.2 |
| PostgreSQL | cliente 18.3; servidores 17.6 e 18.3 instalados |
| PostgreSQL ativo | Não; pg_isready não encontrou serviço local |
| Pacotes Python no Python global | NetworkX 3.4.2, Shapely 2.1.2, FastAPI 0.135.3, Pydantic 2.11.9, SQLAlchemy 2.0.45, pytest 9.0.2, psycopg2-binary 2.9.10 |
| Ausentes no inventário | OSMnx, GeoPandas, OR-Tools, Alembic, Ruff, Mypy, GDAL/OGR, osmium-tool |

O inventário é do Python global; a Entrega 1 usará ambiente virtual próprio. PostgreSQL instalado não significa PostGIS disponível: não havia servidor ativo nem pacote/extensão PostGIS no inventário.

## Lista de municípios

Consulta de leitura à API oficial de localidades do IBGE, municípios da UF 51, em 2026-10-08:

- HTTP 200; 142 municípios e 142 códigos IBGE únicos.
- SHA-256 do JSON descompactado: 6a5506b41f96b2aa1dbab29a85c235d8550dfef95a484d37605db23a9645a9e4.
- O endpoint não fornece versão imutável. A D2 repetiu a leitura e obteve o mesmo hash; o JSON foi salvo como snapshot local.
- [Documentação da API](https://servicodados.ibge.gov.br/api/docs/localidades); [endpoint UF 51](https://servicodados.ibge.gov.br/api/v1/localidades/estados/51/municipios).

Para as coordenadas, usar o produto [Localidades do Brasil — Censo 2022](https://www.ibge.gov.br/estatisticas/sociais/saude/27385-localidades.html), filtrando cidade/sede municipal e juntando por código IBGE. O produto documenta coordenadas de referência das localidades, não centroides territoriais. Coordenadas ausentes, duplicadas ou distantes da malha serão reportadas, nunca inventadas.

## Viabilidade geográfica

No diagnóstico D0, o PBF do Brasil tinha cerca de 1,9 GiB e havia 112 GiB livres. Na D2 foi obtido o snapshot nacional estático `brazil-261007.osm.pbf` (2.091.690.432 bytes), evitando recorte na fronteira estadual; restaram 109 GiB livres. A leitura sequencial com PyOsmium levou 98,222 s e atingiu 436,72 MiB de RSS; essa medida é do contador de tags e não estima a memória para carregar um grafo nacional.

Registro D2:

1. Fonte, timestamp, tamanho, MD5 da Geofabrik, SHA-256 local e licença estão em `data/manifests/osm-brazil-261007.json`.
2. PyOsmium 4.3.1 leu o PBF inteiro sem materializar o grafo: 7.566.868 objetos way com a chave `highway`.
3. A contagem inclui valores de `highway` não necessariamente dirigíveis. Filtro de acesso automotivo, encaixe das sedes e conectividade dirigida pertencem à construção do grafo na D3.
4. A leitura do PBF mediu tempo e RSS; o consumo e o tempo de um grafo/matriz nacional ainda não foram medidos.

Processamento em etapas parece viável, mas tamanho do grafo em memória e tempo de matrizes ainda não foram medidos. O [manual do osmium extract](https://docs.osmcode.org/osmium/latest/osmium-extract.html) informa que memória depende da estratégia e da quantidade de recortes.

## Arquitetura e método

- Separar catálogo territorial, ingestão, grafo, caminhos mínimos, matrizes, visitação, sobreposição, validação, experimentos, API, persistência e interface.
- Grafo dirigido, com comprimento em metros, tempo modelado em segundos, geometria, atributos de circulação, procedência e ID de segmento físico.
- Matrizes direcionadas e independentes para menor distância e menor tempo; guardar caminhos/IDs de arestas junto aos custos.
- Dijkstra como referência e para varredura de origens; A* para comparação e consultas pontuais. Validar admissibilidade das heurísticas.
- OR-Tools para open TSP/mTSP; término artificial sem custo/geométrica; nenhuma volta à origem.
- P2/P3 exigem seleção conjunta de caminhos e ordem de visita; matriz estática sozinha não representa compartilhamento físico.
- Validador independente confere percurso, cobertura, métricas, sentidos e sobreposição.
- PostgreSQL/PostGIS para metadados/experimentos; PBF, grafo derivado e matrizes em arquivos locais com manifestos/hashes.
- Execução Linux nativa. Sem Docker, Podman, sudo ou alteração global.

## Riscos

| ID | Risco | Mitigação planejada |
|---|---|---|
| R1 | Recorte exclui caminho válido por estado vizinho | PBF local amplo; limites e teste de bordas registrados |
| R2 | Grafo excede RAM | Filtrar PBF e medir em piloto antes da rede completa |
| R3 | Sentido, acesso e conversões incorretas | Preservar tags; investigar relações OSM de restrição e validar conectividade |
| R4 | ID físico une pistas separadas ou separa sentidos da mesma pista | Preservar linhagem por segmento; testes de vias paralelas/duplicadas |
| R5 | P2/P3 acoplam rotas de agentes | Camada conjunta; heurística identificada; prova apenas com certificado |
| R6 | P3 inviável em gargalos/origem comum | Revalidar arestas e distinguir “não encontrado” de “inviável comprovado” |
| R7 | Tempo é estimado, não tráfego observado | Perfil e imputação de velocidade versionados; limitar interpretação |
| R8 | OSM muda e usa ODbL | Congelar versão/hash/data; atribuição e licença documentadas |
| R9 | Memória/tempo do grafo regional e matrizes ainda desconhecidos | Índice de localizações PyOsmium usa arquivo temporário esparso em disco; grafo em memória fica no envelope IBGE + 100 km; medir RSS/tempo antes dos benchmarks |
| R10 | O2 até 142 origens × 28 cenários × 10 repetições pode custar caro | Benchmarks e orçamento antes da D9; limites registrados |

## Fases

| Entrega | Resultado | Dependência |
|---|---|---|
| 0 — Diagnóstico e planejamento | Seis docs de planejamento, riscos e tarefas | Concluída |
| 1 — Estrutura e ambiente | Projeto nativo, locks, API de saúde, frontend mínimo e migração inicial | Concluída |
| 2 — Municípios e dados | Snapshot IBGE, 142 sedes oficiais, PBF nacional com hash/licença e relatório de qualidade | Concluída |
| 3 — Grafo e caminhos | Grafo dirigido, A*/Dijkstra, matrizes e cache | 2 |
| 4 — Open TSP | Um agente, origem fixa/otimizada, rota aberta | 3 |
| 5 — Open mTSP/VRP | Dois/três agentes, cobertura e objetivos A/B | 4 |
| 6 — Sobreposição | P1/P2/P3, validação física e status de prova | 5 |
| 7 — API e persistência | Experimentos e resultados consultáveis | 6 |
| 8 — Interface | Mapa e comparação integrados a resultados reais | 7 |
| 9 — Execução científica | Orçamento, cenários, métricas e análise | 6; API opcional |
| 10 — Documentação final | Instalação, metodologia e reprodução | 1–9 |

Mostrar cada entrega e aguardar aprovação antes da próxima. A interface atual é apenas um scaffold; grafo real, matrizes e rotas ainda não foram gerados. D3.1–D3.4 têm código inicial; testes e processamento PBF/matrizes aguardam confirmação conforme AGENTS.md.

## Registro da Entrega 1

- Ambiente Python 3.13 local em `.venv`, dependências de runtime/desenvolvimento no `backend/pyproject.toml` e versões resolvidas em `backend/requirements.lock`.
- Frontend React/TypeScript/Vite com `package-lock.json`, compilação TypeScript e página provisória sem resultados simulados.
- Configuração local por `.env.example`; API FastAPI em `/api/health` e persistência SQLAlchemy para metadados de execução.
- Migração Alembic reversível para `experiment_runs`; instalação nativa descrita em `docs/INSTALLATION_LINUX.md`.
- Verificação: pytest 1/1 aprovado (com aviso de depreciação `anyio.abc.BlockingPortal` emitido por Starlette); Ruff e `pip check` aprovados; build Vite aprovado; API local respondeu HTTP 200; upgrade/downgrade SQLite e geração SQL PostgreSQL offline aprovados.
- Não foi iniciado PostgreSQL nem instalada extensão PostGIS; a migração PostgreSQL não foi aplicada em servidor.

## Registro da Entrega 2

- API IBGE refeita: 142 códigos únicos, hash do JSON idêntico ao snapshot D0.
- Localidades Censo 2022: 142 pontos de sede por código IBGE, sem faltas, duplicatas ou divergências de nomes/códigos; transformação EPSG:4674 → EPSG:4326 registrada.
- PBF nacional estático da Geofabrik baixado e verificado por tamanho e MD5 publicado; SHA-256 local e licença ODbL registrados.
- PyOsmium contou 7.566.868 objetos way com `highway` em 98,222 s e 436,72 MiB de pico RSS, sem carregar grafo.
- Verificação D2: pytest 5/5, Ruff e `pip check` aprovados; importação real e hashes dos artefatos conferidos. O pytest emitiu um aviso de depreciação de dependência do Starlette.
- Distância de snap e conectividade do perfil automotivo passaram à tarefa D3.1a, pois dependem de nós/arcos e regras dirigidas. Nenhuma distância aproximada foi inventada nesta entrega.
- Evidência detalhada: `docs/DATA_QUALITY_D2.md`; artefatos: `data/manifests/` e `data/processed/`.

## Implementação da Entrega 3

- Código criado em backend/app/graph/, backend/app/shortest_path/ e backend/app/routing/matrices.py; testes focados adicionados.
- O grafo mantém vias paralelas e IDs físicos por segmento; sentido de circulação, acesso automotivo e restrições OSM via nó entram na busca.
- Recorte definido pelo envelope oficial da UF em EPSG:5880, acrescido de 100 km; snapshot IBGE: data/raw/ibge/MT_UF_2022.zip, SHA-256 310d2ea614b51c21335241af21f3b919848a2e807389704877e208a15b054d7e.
- A velocidade usa maxspeed numérico ou perfil por classe, com valores estimados registrados. Snap é para o nó viário mais próximo; conector fora da rede não compõe custos.
- Via-ways em relações de conversão, acessos/velocidades condicionais, barreiras e balsas não são modelados; são enumerados no relatório de qualidade do grafo.
- Não foi processado o PBF para construir o grafo nem calculadas matrizes reais; não há métricas de alcançabilidade reais ainda. Testes D3 foram adicionados, mas não executados conforme a instrução local de confirmação.

## Validação planejada

Planejar testes unitários, integração, propriedades e regressão em grafos sintéticos pequenos com resposta conhecida; usar enumeração/exato em instâncias pequenas. Depois medir redes de 5, 10, 20, 50 e 142 cidades, registrando também nós/arestas, RAM e tempo. Sintéticos não são resultados reais.

Recalcular indicadores sobre arestas percorridas; validar continuidade, direção, cobertura única, custos e P3. Distinguir “ótima comprovada”, “viável/best-known”, “não encontrada no limite” e “inviável comprovada”. Ausência de solução heurística não prova inviabilidade.

## Documentação técnica consultada

[OR-Tools — starts/ends](https://developers.google.com/optimization/routing/routing_tasks), [VRP/span global](https://developers.google.com/optimization/routing/vrp), [dimensões](https://developers.google.com/optimization/routing/dimensions), [instalação Python](https://developers.google.com/optimization/install/); [NetworkX A*](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.shortest_paths.astar.astar_path.html); [OSMnx](https://osmnx.readthedocs.io/en/stable/user-reference.html); [GeoPandas](https://geopandas.org/en/stable/docs/user_guide/io.html); [PostGIS](https://postgis.net/documentation/getting_started/); [Geofabrik Centro-Oeste](https://download.geofabrik.de/south-america/brazil/centro-oeste.html) e [Brasil](https://download.geofabrik.de/south-america/brazil.html); [IBGE Localidades](https://www.ibge.gov.br/estatisticas/sociais/saude/27385-localidades.html); [licença OSM](https://www.openstreetmap.org/copyright).

Documentação técnica consultada em 2026-10-08. D1/D2 validaram o ambiente Python 3.13.12. D3 usa PyOsmium/PyShp/PyProj já fixados; OR-Tools para visitação será resolvido antes da D4.
