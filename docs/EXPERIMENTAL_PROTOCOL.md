# Protocolo experimental

**Estado:** configuração D9 congelada; orçamento registrado antes do lote; dez repetições iniciadas em 2026-10-10; análise pendente.
**Regra:** conferir orçamento, RAM disponível e disco livre imediatamente antes do lote.

## Perguntas

1. Como número de agentes altera distância total e tempo máximo?
2. Como origem fixa versus otimizada altera custos?
3. Qual o custo da sobreposição penalizada ou proibida de segmentos físicos?
4. Quanto variam os resultados e duração entre execuções heurísticas?

Resultados descrevem snapshot congelado, não tráfego observado em tempo real.

## Matriz de cenários

Há 36 combinações nominais de agentes, origem, objetivo e política. Para um agente, as três políticas de sobreposição interagente são equivalentes; restam 28 configurações estruturais:

| IDs | Agentes | Origem | Política | Configurações |
|---|---:|---|---|---|
| E01–E04 | 1 | fixa/otimizada | P1 equivalente | A/B por origem |
| E05–E10 | 2 | fixa | P1/P2/P3 | A/B por política |
| E11–E16 | 2 | otimizada | P1/P2/P3 | A/B por política |
| E17–E22 | 3 | fixa | P1/P2/P3 | A/B por política |
| E23–E28 | 3 | otimizada | P1/P2/P3 | A/B por política |

E01: 1 agente, fixa, distância; E02: fixa, tempo máximo; E03: otimizada, distância; E04: otimizada, tempo máximo. Em cada bloco de seis a seguir, ordem: P1/distância, P1/tempo, P2/distância, P2/tempo, P3/distância, P3/tempo.

Configuração de referência proposta:

- Origem fixa: Cuiabá, IBGE 5103403, conferida na API UF 51 em 2026-10-08.
- Origem otimizada: até 142 sedes verificadas; origem também conta como atendida uma vez.
- P2: lambda principal 1; sensibilidade 0, 0.25, 0.5 e 2 para k=2 e k=3.
- Conversão temporal de sobreposição: v_ref=60 km/h, constante convencional do modelo, não medição.
- Serviço por sede: zero segundos neste estudo; manter parâmetro para o futuro.
- Veículos equivalentes, partida simultânea, sem janelas de tempo.

Com lambda zero, P2 deve reproduzir baseline dentro das tolerâncias do solver; desvio sem justificativa invalida a comparação.

## Repetições e orçamento

O protocolo executável está em `experiments/configurations/d9-mt.json`: dez repetições, ajustáveis por `--repetitions`; seed inicial 20261008, stride 10 e offsets 0/1/2 para D4/D5/D6. Cada runner grava seus limites, seeds, versões, hashes, status e artefatos no índice da repetição. Os cenários são E01–E28.

Limites congelados: 300 s por cenário D4/D5; 60 s por variante D6; oito processos no máximo; `MemoryMax=25.000.000.000` bytes para todo o processo de cálculo e seus workers. O2 usa os resultados medidos de 142 origens. P2 testa λ=0, 0,25, 0,5, 1 e 2 sobre o mesmo conjunto de candidatas; λ=1 é o principal. λ=0 é conferido contra P1 sem busca adicional.

A estimativa reproduzível fica em `experiments/analysis/d9-budget-mt-2026-10-10.md`. Ela usa os tempos medidos D5 como referência, limites D4/D6 como teto, reserva 20% de duração e exige 2 GB livres para relatórios. Execução parcial permanece best-known, não completa/ótima.

## Dados e reprodutibilidade

Congelar para cada estudo:

- snapshot municipal IBGE, endpoint/arquivo, data e hash;
- Localidades do Brasil, edição, categoria, data, hash e junção por código;
- PBF OSM, URL, data do dado, hash, licença, recorte e parser;
- hash/versão do grafo, perfil do veículo e regras de velocidade/imputação;
- hash das matrizes e caminhos;
- commit quando houver Git, versões de Python/OR-Tools/NetworkX/OSMnx;
- configuração serializada, seed, limites, CPU/RAM/SO e timestamps.

Cada repetição tem experiment_id, cenário, parâmetros, versão de dados/software, seed, duração, status, métricas e caminhos dos artefatos. Não sobrescrever resultados. OSM exige atribuição e registro da licença ODbL.

## Métricas

Calcular a partir de rotas reconstruídas e validadas:

| Métrica | Definição |
|---|---|
| Distância total | Soma de cada travessia por todos os agentes; km (valor-base em m) |
| Distância individual | Travessias de cada rota; km |
| Maior distância | Máximo individual; km |
| Tempo individual | Arestas + serviço; horas (valor-base em segundos) |
| Tempo máximo | Máximo dos tempos individuais |
| Cobertura | 142 municípios distintos atendidos |
| Atribuição | Códigos IBGE/contagem por agente; origem contada uma vez |
| Distância redundante O | Comprimento de trecho usado por mais de um agente, por agente adicional |
| Percentual de sobreposição | 100 × O / D_total |
| Tempo computacional | Parede, CPU e pico de RAM quando disponível |
| Viabilidade | P1/P2/P3 verificada nos segmentos físicos |
| Qualidade | Gap/limite inferior quando disponível; senão, best-known |
| Objetivo do solver | Valor de A/B/P2, separado das métricas operacionais |
| Carga | Cidades, distância e tempo por agente; média/dispersão |
| Origem/términos | Código inicial, modo e última sede real de cada agente |

P2 reporta também penalidade e função composta; não comparar só pelo custo interno do solver.

## Validade e análise

1. Rejeitar sede ausente/duplicada, atribuição inválida, percurso descontínuo, sentido proibido ou custo incompatível com arestas.
2. Rejeitar P3 se algum physical_segment_id for usado por agentes diferentes. Nó compartilhado é permitido; começo comum não implica compartilhar trecho.
3. Separar ótimo comprovado, viável/best-known, não encontrada no limite, inviável comprovada e falha técnica.
4. Em O2 parcial, registrar candidatos avaliados; não declarar origem globalmente ótima.
5. Reportar as execuções e estatísticas das dez repetições: mediana, dispersão, extremos, status e limite.
6. Comparar algoritmos com mesmos dados, seeds, hardware e limites sempre que aplicável.
7. Não extrapolar tempo estático para trânsito/condição da estrada sem dados externos independentes.

## Arquivos futuros

- Configurações: experiments/configurations/
- Runner: experiments/runners/
- Resultados brutos: experiments/results/
- Análises: experiments/analysis/
- Manifestos: data/manifests/
- Relatório: docs/experiments/
