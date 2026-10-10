# Arquitetura do sistema

**Estado:** D1–D8 implementadas; lote e análise D9 em execução. Grafo, caminhos e matrizes usam `backend/app/graph/`, `backend/app/shortest_path/` e `backend/app/routing/matrices.py`; a rede real e as matrizes têm manifestos congelados.

## Componentes

| Camada | Responsabilidade | Caminho principal |
|---|---|---|
| Catálogo territorial | Códigos IBGE, nomes, sedes e coordenadas | backend/app/geography/municipalities.py |
| Aquisição/ingestão | Validação do catálogo, leitura PyOsmium e manifestos | backend/app/geography/import_data.py, municipalities.py, osm_importer.py |
| Grafo rodoviário | Nós/arcos dirigidos, restrições, geometria e IDs físicos | backend/app/graph/ |
| Caminhos mínimos | Dijkstra, A*, heurísticas e recuperação de arcos | backend/app/shortest_path/ |
| Matrizes/cache | Custos e caminhos por par, chaveados por snapshot | backend/app/routing/matrices.py |
| Visitação | Open TSP/mTSP, sequência e atribuição | backend/app/optimization/visitation/ |
| Caminhos conjuntos | Alternativas, P1/P2/P3 e estado de prova | backend/app/optimization/overlap/ |
| Validação/métricas | Cobertura, continuidade, direção, custo e sobreposição | backend/app/validation/, backend/app/metrics/ |
| Experimentos | Configuração, seed, limites, logs e artefatos | backend/app/experiments/ |
| API/persistência | Contratos HTTP, jobs, registros e migrações | backend/app/api/, backend/app/persistence/, backend/alembic/ |
| Interface | MapLibre, formulários e comparação | frontend/src/ |
| Dados locais | Fontes, derivados, matrizes e manifestos | data/raw/, data/processed/, data/matrices/, data/manifests/ |
| Protocolo | Configuração, execução e análise | experiments/ |

## Fluxo

1. Obter a lista oficial do IBGE e usar código IBGE como chave. A D2 persistiu 142 códigos únicos.
2. Selecionar Cidade / Sede Municipal; validar uma por município e transformar os pontos de EPSG:4674 para EPSG:4326. O código D3 encaixa cada sede no nó viário mais próximo e registra distância.
3. A D2 guardou snapshot nacional PBF, hash, licença e relatório de tags highway. O importador D3 filtra perfil de automóvel numa caixa oficial IBGE + 100 km e aplica o teto de RAM antes da leitura.
4. Dijkstra e A* calculam distância/tempo separadamente; as matrizes dirigidas guardam custos e caminhos por par, congelados por hash.
5. D4–D6 calculam sequência, distribuição de sedes e políticas de sobreposição. P2/P3 selecionam rotas candidatas considerando o uso físico entre agentes.
6. Recalcular métricas e validar a solução. A API persiste configuração, seeds, software, dados, status, progresso e referências aos artefatos.
7. API e frontend apresentam rotas rodoviárias e métricas validadas; o worker local serial executa jobs e publica progresso e cancelamento.

## Contratos mínimos

| Contrato | Campos | Invariante |
|---|---|---|
| Municipality | ibge_code, name, seat_lat/lon, locality_code, snapshot | Código único; coordenada de sede, não centroide municipal |
| RoadSegment | u/v/key, physical_segment_id, length_m, travel_time_s, geometry, access, source_ids | Arco dirigido; custos não negativos; ID une sentidos da mesma pista |
| RoadNetworkSnapshot | snapshot_id, fonte/data/hash/licença, perfil, recorte, parser | Imutável depois de gerar matriz |
| TravelMatrix | mode, unit, municípios, custo dirigido, caminho por par, graph hash | Matrizes independentes; sem caminho explícito |
| ExperimentConfig | agentes, origem, objetivo, política, lambda, service time, seed, limite | Serializável e validada antes de executar |
| RouteSolution | início, atribuições, sedes ordenadas, trechos, fim, métricas, status | Aberta; terminal artificial não gera deslocamento; status distingue prova |

Interfaces internas independem de framework. Pydantic valida fronteira da API; algoritmos não dependem de FastAPI. Arquivos geográficos grandes não trafegam no schema de experimento.

## Grafo e caminhos

Preservar vias paralelas, acesso de automóveis, mão única, geometria e procedência. Cada arco guarda comprimento geodésico em metros, tempo modelado em segundos, classe/tags, IDs de nós/ways originais, sentido e IDs de segmento. Arcos reversos da mesma sequência OSM compartilham physical_segment_id; ways sobrepostos distintos não são deduplicados. Restrições de conversão via nó são aplicadas; via-ways e regras condicionais são limitação registrada. Velocidades por classe são estimativas, não observações de tráfego.

Conversões proibidas podem depender da sequência de dois trechos. Se o grafo não representar relações OSM de restrição, usar estado de busca por segmento dirigido ou registrar a limitação; uma aresta permitida isoladamente não valida toda conversão.

PBF local reduz dependência de Overpass. A leitura usa PyOsmium/libosmium com índice temporário esparso em disco; o grafo regional preserva segmentos dirigidos, restrições via nó e IDs físicos. Os limites para via-ways e tags condicionais estão registrados no manifesto D3.

- Matriz de distância minimiza soma de length_m; matriz de tempo minimiza travel_time_s.
- Matrizes são dirigidas e podem ser assimétricas. Sem caminho não vira zero nem linha reta.
- Dijkstra é referência e opção para varredura por origem; A* atende pares e comparação, com heurística admissível validada.
- Guardar a trajetória por segmentos, não somente os custos. D6 seleciona entre rotas candidatas geradas pelos solvers e registra que o conjunto não contém todos os caminhos possíveis.
- OR-Tools recebe visitação e fins artificiais sem custo físico. Cada agente ativo precisa de atendimento distinto além da origem.
- Objetivo B é max(T_a); custo de span somado aos arcos não prova, por si, minimização pura desse máximo.
- P2/P3 ficam em camada própria; D6 identifica resultado heurístico e o escopo da prova.

## Rota aberta

O fim físico é a última sede atendida. Um término artificial, distinto por veículo, só encerra a sequência; não tem coordenada, geometria, distância ou tempo. Nenhum retorno à origem é adicionado.

Se a origem for uma sede obrigatória, ela é contada como atendida uma vez no início compartilhado. A configuração fixa de referência proposta é Cuiabá, IBGE 5103403; O2 escolhe entre sedes verificadas de MT. Origem externa só é aceita com localização e associação viária verificadas.

## Persistência, API e interface

- PostgreSQL/SQLAlchemy: configurações, estados, progresso, métricas, validações e referências a artefatos. O esquema atual não requer PostGIS.
- Sistema de arquivos: PBF, manifestos, grafo, matrizes e sequências de segmentos. Banco guarda hash, formato e caminho relativo.
- Worker local independente para jobs longos. Estados do job: queued, running, succeeded, failed, interrupted. Status da solução é campo separado.
- API FastAPI: saúde, municípios, estado dos dados, algoritmos, configs, criar/listar/consultar job, métricas, GeoJSON e exportação.
- React/TypeScript/Vite e MapLibre GL JS; GeoJSON deriva de caminhos rodoviários reais, com atribuição OSM visível.

Sem fila distribuída na primeira versão. Sem banco em contêiner. Configuração local em .env e exemplo sem segredos.

## Dependências implementadas

Python, PyOsmium, PyShp, PyProj, FastAPI, Pydantic, SQLAlchemy, Alembic, OR-Tools, pytest e Ruff; PostgreSQL para persistência; React, TypeScript, Vite e MapLibre GL JS. D3 usa adjacency própria e não adiciona NetworkX/OSMnx.
