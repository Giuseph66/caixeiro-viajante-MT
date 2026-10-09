# Relatório de qualidade dos dados — D2

**Coleta:** 2026-10-08, horário local de Cuiabá. **Código:** PyOsmium 4.3.1, PyShp 3.1.6 e PyProj 3.8.0 em Python 3.13.12. Os manifestos JSON guardam os mesmos identificadores e hashes.

## Fontes e snapshots

| Conjunto | Fonte e snapshot | Tamanho | SHA-256 | Licença/atribuição |
|---|---|---:|---|---|
| Municípios MT | [API de Localidades IBGE, UF 51](https://servicodados.ibge.gov.br/api/v1/localidades/estados/51/municipios); resposta descomprimida | 64.818 bytes | `6a5506b41f96b2aa1dbab29a85c235d8550dfef95a484d37605db23a9645a9e4` | API pública do IBGE; endpoint sem versão imutável |
| Localidades | [Localidades do Brasil, Censo 2022](https://geoftp.ibge.gov.br/organizacao_do_territorio/estrutura_territorial/localidades/Localidades_do_Brasil/2022/Localidades_UFs_shp.zip); arquivo atualizado em 2025-11-19 | 4.538.286 bytes | `f4af7774f1bf92636c8ff2bde61b515b8dd65b3d50eca66664f3ce77a0b7470c` | Arquivo público; o índice de download não informa licença aberta específica |
| Malha OSM | [Geofabrik Brasil](https://download.geofabrik.de/south-america/brazil-261007.osm.pbf), dados até 2026-10-07T20:20:35Z | 2.091.690.432 bytes | `8359bb2397f1654e8ff2be2e0f217cfd66a4f3098554612d2376f0fb2fe06e93` | ODbL 1.0; © OpenStreetMap contributors |

O PBF corresponde ao arquivo estático `brazil-261007.osm.pbf`. Seu MD5 publicado pela Geofabrik é `8a87b969d97fa4a8270991c06acbaceb`, verificado após o download. Foi escolhido o extrato nacional para não truncar estradas na divisa de Mato Grosso. Os arquivos brutos estão em `data/raw/` e são ignorados pelo Git; os manifestos estão em `data/manifests/`.

## Municípios e sedes

- A API devolveu 142 municípios e 142 códigos IBGE únicos. Seu SHA-256 coincide com a primeira consulta da D0.
- O shapefile de MT tem 1.560 localidades. O filtro oficial `CT_LOCALID=Cidade` e `SCT_LOCALI=Sede Municipal` encontrou exatamente 142 pontos.
- Todas as sedes foram associadas pelo código `CD_MUN`; não houve códigos ausentes/duplicados, divergência entre os nomes dos dois produtos, código de localidade incompatível ou coordenada inválida.
- A geometria-fonte é EPSG:4674 (SIRGAS 2000). Os pontos do GeoJSON foram transformados para EPSG:4326 com PyProj. O DBF também traz latitude/longitude arredondadas; a diferença máxima para a geometria foi `0,00005°`, e a geometria foi usada como coordenada de maior precisão.
- As coordenadas foram validadas por finitude, faixa geográfica e consistência dos códigos no produto-fonte. Não foi feito teste de contenção contra polígonos municipais.
- O arquivo derivado `data/processed/municipalities-mt.geojson` tem 142 feições. Seu hash está no manifesto `data/manifests/municipalities-mt.json`.

## Leitura do PBF

PyOsmium percorreu o PBF em streaming, sem construir nós/arcos de roteamento:

- 7.566.868 objetos `way` com a chave `highway`.
- Tempo medido: 98,222 s.
- Pico de RSS do processo: 436,72 MiB.
- O resumo completo das classes e dos valores de acesso/mão única está em `data/processed/osm-quality.json` e `data/manifests/osm-brazil-261007.json`.

A chave OSM `highway` também representa elementos sem função de estrada, como pontos de ônibus, serviços e cruzamentos; a contagem acima não significa “vias dirigíveis”. Foram encontrados valores de baixa frequência, como `AL-101`, `RUA PUBLICA`, `residencial` e `área rural`. Eles ficam destacados no relatório para revisão; não são removidos ou corrigidos automaticamente. [A documentação OSM](https://wiki.openstreetmap.org/wiki/Key:highway) descreve tanto classes viárias quanto outros usos dessa chave.

## Limites desta entrega

- Ainda não há filtro de acesso para automóveis, grafo dirigido, encaixe da sede à aresta mais próxima ou teste de alcançabilidade. Esses critérios dependem do grafo e serão validados na D3.
- O tempo e a memória medidos são apenas da leitura sequencial de tags. Não estimam a memória para manter o grafo completo, o cálculo de caminhos ou a matriz de custos.
- A API do IBGE não fixa versões; use o hash do manifesto para identificar o snapshot. O PBF OSM é uma fotografia temporal sob ODbL e deve permanecer ligado à atribuição indicada.
- Nenhuma cidade, coordenada, estrada, distância ou rota foi inventada.
