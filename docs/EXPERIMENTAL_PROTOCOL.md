# Protocolo experimental

**Estado:** plano pré-registrável; nenhum experimento executado.  
**Regra:** não executar os 28 cenários antes de estimar CPU, RAM, disco e duração.

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

Planejar dez repetições por configuração heurística. Semente mestra 20261008; seed de repetição = 20261008 + índice. Permitir ajustar repetições. Registrar limite de tempo, threads, parâmetros, versão do solver e estado de interrupção.

O2 pode exigir até 142 origens por configuração. Antes da D9, medir instâncias de 5, 10, 20, 50 e 142 cidades, junto de nós/arestas da rede, RAM, duração e efeito do cache. Estimar orçamento dos cenários/repetições e então propor limites. Execução parcial permanece best-known, não completa/ótima.

Nenhuma rota, matriz rodoviária, medição de desempenho ou orçamento foi gerado nesta Entrega 0.

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
