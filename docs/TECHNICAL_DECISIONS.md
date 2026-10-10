# Decisões técnicas

**Data:** 2026-10-09. **Estado:** decisões D0–D3; D3 possui código, mas falta executar/validar a importação de dados reais.

| ID | Decisão | Estado | Justificativa / condição |
|---|---|---|---|
| TD-01 | Execução Linux nativa; sem Docker, Compose, Podman | Determinada pelo escopo | Mantém ambiente pedido |
| TD-02 | Módulos de domínio separados de API/UI | Proposta | Pasta inicial vazia; sem migração |
| TD-03 | Python backend; React/TypeScript/Vite frontend | Proposta | Segue stack solicitada; versões ainda serão fixadas |
| TD-04 | IBGE API e código IBGE como chave municipal | Confirmada no snapshot | Em 2026-10-08, UF 51 retornou 142 IDs únicos; hash SHA-256 do JSON descompactado: 6a5506b41f96b2aa1dbab29a85c235d8550dfef95a484d37605db23a9645a9e4 |
| TD-05 | Coordenadas de sede de Localidades do Brasil, categoria/subcategoria de sede | Confirmada na D2 | 142 pontos `Cidade / Sede Municipal`, chaveados por `CD_MUN`; origem EPSG:4674, GeoJSON EPSG:4326; nunca usar centroide municipal |
| TD-06 | Snapshot OSM nacional PBF com hash/licença | Confirmada na D2 | `brazil-261007.osm.pbf`, 2.091.690.432 bytes; ODbL 1.0; checksum e relatório em `data/manifests/osm-brazil-261007.json` |
| TD-07 | OR-Tools para open TSP/mTSP; fim artificial sem custo físico | Implementada e medida em D4/D5 | API aceita starts/ends próprios; sem retorno à origem; resultados reais de 142 cidades são viáveis, sem prova de ótimo |
| TD-08 | Dijkstra referência, A* com heurística admissível; matrizes separadas | Implementada no código D3; sem validação executada | A* usa distância geodésica e velocidade máxima modelada como limites inferiores |
| TD-09 | Guardar ID físico e trajetória completa | Proposta crítica | P2/P3 requerem validação por segmento; distinguir sentidos e pistas |
| TD-10 | P1 sobre custos base; P2/P3 com caminho e visitação conjuntos | D6 implementada no conjunto de rotas candidatas | Compartilhamento é avaliado por ID físico; rotas reais usam caminhos D3 e não cobrem alternativas rodoviárias globais |
| TD-11 | Heurística não prova inviabilidade/ótimo | Determinada pelo protocolo | Ausência de solução heurística não é certificado |
| TD-12 | PostgreSQL/PostGIS para metadados; arquivos locais para PBF/grafo/matrizes | Proposta | Hashes e manifestos mantêm ativos grandes reproduzíveis |
| TD-13 | P2 lambda principal 1; sensibilidade 0, 0.25, 0.5, 2; v_ref=60 km/h | Provisória | Fórmula normalizada em MATHEMATICAL_MODEL.md; congelar antes da D9 |
| TD-14 | Cuiabá (5103403) como origem fixa; O2 percorre 142 sedes | Provisória | Baseline reproduzível; código confirmado no snapshot IBGE |
| TD-15 | Serviço municipal zero inicialmente | Provisória | Isola deslocamento; manter parâmetro futuro |
| TD-16 | Venv Python 3.13, lock pip e package-lock npm; sem instalação global | D1 implementada | Python 3.13; Node 24.12/npm 11.6; dependências geoespaciais D2 no lock local |
| TD-17 | Orçamento antes de experimentos reais | Determinada pelo protocolo | O2 pode testar 142 origens; são 28 configurações estruturais |
| TD-18 | PyOsmium para leitura streaming de PBF; PyShp para shapefile IBGE; PyProj para reprojeção | Confirmada na D2 | Versões Python 3.13 fixadas em `backend/requirements.lock`; leitura PBF sem formar grafo |
| TD-19 | Encaixe e conectividade das sedes fazem parte da construção do grafo | Implementada no código D3; dados pendentes | Snap registra nó mais próximo e distância; custos excluem conectores fora da rede |
| TD-20 | Recorte do grafo: envelope da UF oficial + 100 km em EPSG:5880 | Provisória até medir conectividade | Mantém buffer de estados vizinhos e limita memória; estradas terminam no limite do envelope |
| TD-21 | Estrutura de grafo própria, adjacency por nó | Implementada no código D3 | Preserva paralelas/IDs físicos e dá controle à busca com estado de conversão; não adiciona NetworkX nem replica grafo em memória |
| TD-22 | Conversões automotivas via nó suportadas; via-ways/condicionais registradas como não suportadas | Implementada no código D3; sem validação executada | Evita apresentar cobertura parcial de restrições como cobertura total |
| TD-23 | Tempo: maxspeed numérico; sem isso, velocidade por classe configurada | Provisória | Estimativa modelada; limitações ficam no manifesto; não representa tráfego observado |
| TD-24 | Índice temporário sparse_file_array do PyOsmium durante o parse nacional | Implementada no código D3; sem medição | Mantém localizações fora do grafo em disco e limita cópia de todos os nós na RAM; custo de I/O será medido |
| TD-25 | Teto rígido de 25 GB e processos `spawn` nos experimentos paralelos | Implementada e medida em D4/D5 | Runner aplica cgroup `MemoryMax`; workers recebem matrizes de custos sem trajetórias completas; pico D5 de 15.284.240.384 bytes |
| TD-26 | P2/P3 por enumeração de rotas candidatas com validação física final | Implementada e medida em D6 | Busca exaustiva é ótima apenas no conjunto enumerado; `not_found` em rede real não comprova inviabilidade global; pico D6 de 15.259.635.712 bytes |

## P2

Para distância: minimizar D_total + lambda O. Para tempo: minimizar T_max + lambda O/v_ref, com v_ref=60 km/h. Os componentes e o valor composto sempre são reportados. P2 exige busca conjunta. Se iterativa/heurística, será identificada como tal.

## Risco P3

OR-Tools RoutingModel documenta visitação com custos/dimensões por rota; não foi identificada na documentação consultada uma restrição global genérica de uso exclusivo de cada segmento físico entre veículos. A camada conjunta é responsabilidade do projeto:

1. Formulação exata pequena para testar P3 em grafos controlados.
2. Em rede real, explorar caminhos candidatos e método conjunto com limites explícitos.
3. Validar solução diretamente nos IDs físicos.
4. Limite alcançado sem solução: “nenhuma solução viável encontrada”; “inviável comprovada” só com certificado da instância coberta.

Se P3 não for praticável na rede integral, registrar a limitação; não relaxar silenciosamente.

## Compatibilidade

O ambiente Python 3.13 já contém PyOsmium, PyShp e PyProj em .venv; D3 não adicionou dependências. Importações/sintaxe foram verificadas. A construção do grafo e as matrizes reais não foram executadas; leitura de tags D2 não comprova memória/tempo da rede filtrada.

## Referências oficiais consultadas

Consultadas em 2026-10-08:

- [OR-Tools: rotas com starts/ends](https://developers.google.com/optimization/routing/routing_tasks), [span global](https://developers.google.com/optimization/routing/vrp), [dimensões](https://developers.google.com/optimization/routing/dimensions), [instalação Python](https://developers.google.com/optimization/install/).
- [NetworkX A*: admissibilidade](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.shortest_paths.astar.astar_path.html).
- [OSMnx: grafos/caminhos](https://osmnx.readthedocs.io/en/stable/user-reference.html).
- [osmium: PBF, recortes e memória](https://docs.osmcode.org/osmium/latest/osmium-extract.html).
- [IBGE API](https://servicodados.ibge.gov.br/api/docs/localidades), [Localidades do Brasil](https://www.ibge.gov.br/estatisticas/sociais/saude/27385-localidades.html).
- [Geofabrik Centro-Oeste](https://download.geofabrik.de/south-america/brazil/centro-oeste.html), [Brasil](https://download.geofabrik.de/south-america/brazil.html).
- [GeoPandas](https://geopandas.org/en/stable/docs/user_guide/io.html), [PostGIS](https://postgis.net/documentation/getting_started/), [OSM ODbL/atribuição](https://www.openstreetmap.org/copyright).
