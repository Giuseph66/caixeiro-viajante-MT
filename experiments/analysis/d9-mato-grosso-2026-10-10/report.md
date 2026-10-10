# Relatório experimental D9 — Mato Grosso

Estado do lote: **complete**; repetições concluídas: **10/10**.

Os resultados usam os hashes congelados no manifesto do lote. D4 é best-known em 142 sedes. P1/P2 são exatos somente no conjunto parcial de rotas candidatas; P3 `not_found` não prova inviabilidade da rede.

Código do cálculo: `39f20556b8bd956932c63f77220085f66d1b721a`; análise: `cb5a8d75937c50857942bfb6148cb3a54d3958ef`. HEAD registrado pelo runner: `dfb1d49fd0ae6389bb2a6a1633aca7f9719143ab`.

## Estatísticas por configuração

O tempo mediano usa `runtime_s` do D4 para k=1 e `candidate_generation_wall_time_s` do D6 para k=2/3. O tempo de solver/parede do D5 aparece na seção própria; o tempo total do lote D6 permanece em `d6_batch_elapsed_s` nos CSVs.

| ID | k | Origem | Política | Objetivo | n viável | Status | Tempo mediano reportado (s) | D total mediana (km) | T máximo mediano (h) | Sobreposição mediana (km) | Gap mediano vs. melhor observado |
|---|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| E01 | 1 | fixed | P1 | distance | 10 | {"feasible": 10} | 302.5 | 10386.5 | 152.10 | 0.0 | 0.00% |
| E02 | 1 | fixed | P1 | time | 10 | {"feasible": 10} | 302.6 | 10483.3 | 151.46 | 0.0 | 0.00% |
| E03 | 1 | optimized | P1 | distance | 10 | {"feasible": 10} | 302.8 | 10023.9 | 147.55 | 0.0 | 0.00% |
| E04 | 1 | optimized | P1 | time | 10 | {"feasible": 10} | 302.8 | 10417.4 | 149.80 | 0.0 | 0.54% |
| E05 | 2 | fixed | P1 | distance | 10 | {"feasible": 10} | 420.4 | 10020.7 | 87.40 | 0.1 | 0.00% |
| E06 | 2 | fixed | P1 | time | 10 | {"feasible": 10} | 420.4 | 13324.4 | 92.94 | 694.2 | 0.00% |
| E07 | 2 | fixed | P2 | distance | 10 | {"feasible": 10} | 420.4 | 10020.7 | 87.40 | 0.1 | 0.00% |
| E08 | 2 | fixed | P2 | time | 10 | {"feasible": 10} | 420.4 | 13324.4 | 92.94 | 694.2 | 0.00% |
| E09 | 2 | fixed | P3 | distance | 10 | {"feasible": 10} | 420.4 | 10161.0 | 94.24 | 0.0 | 0.00% |
| E10 | 2 | fixed | P3 | time | 0 | {"not_found": 10} | — | — | — | — | — |
| E11 | 2 | optimized | P1 | distance | 10 | {"feasible": 10} | 447.8 | 10266.4 | 131.02 | 0.0 | 0.00% |
| E12 | 2 | optimized | P1 | time | 10 | {"feasible": 10} | 455.8 | 12882.9 | 88.98 | 186.0 | 0.19% |
| E13 | 2 | optimized | P2 | distance | 10 | {"feasible": 10} | 447.8 | 10266.4 | 131.02 | 0.0 | 0.00% |
| E14 | 2 | optimized | P2 | time | 10 | {"feasible": 10} | 455.8 | 13022.5 | 89.22 | 164.1 | 0.05% |
| E15 | 2 | optimized | P3 | distance | 10 | {"feasible": 10} | 447.8 | 10266.4 | 131.02 | 0.0 | 0.00% |
| E16 | 2 | optimized | P3 | time | 0 | {"not_found": 10} | — | — | — | — | — |
| E17 | 3 | fixed | P1 | distance | 10 | {"feasible": 10} | 420.3 | 9938.1 | 87.74 | 0.1 | 0.00% |
| E18 | 3 | fixed | P1 | time | 10 | {"feasible": 10} | 420.4 | 12122.2 | 56.13 | 133.3 | 0.00% |
| E19 | 3 | fixed | P2 | distance | 10 | {"feasible": 10} | 420.3 | 9938.1 | 87.74 | 0.1 | 0.00% |
| E20 | 3 | fixed | P2 | time | 10 | {"feasible": 10} | 420.4 | 12122.2 | 56.13 | 133.3 | 0.00% |
| E21 | 3 | fixed | P3 | distance | 0 | {"not_found": 10} | — | — | — | — | — |
| E22 | 3 | fixed | P3 | time | 0 | {"not_found": 10} | — | — | — | — | — |
| E23 | 3 | optimized | P1 | distance | 10 | {"feasible": 10} | 447.6 | 10122.6 | 108.30 | 11.2 | 0.00% |
| E24 | 3 | optimized | P1 | time | 10 | {"feasible": 10} | 455.3 | 13353.3 | 61.33 | 897.4 | 0.18% |
| E25 | 3 | optimized | P2 | distance | 10 | {"feasible": 10} | 447.6 | 10122.6 | 108.30 | 11.2 | 0.00% |
| E26 | 3 | optimized | P2 | time | 10 | {"feasible": 10} | 455.3 | 13336.0 | 61.79 | 857.0 | 17.12% |
| E27 | 3 | optimized | P3 | distance | 0 | {"not_found": 10} | — | — | — | — | — |
| E28 | 3 | optimized | P3 | time | 0 | {"not_found": 10} | — | — | — | — | — |

## Medições do D5

D5 é resumido separadamente antes da seleção de políticas D6; os tempos abaixo não são filtrados pelo status da rota escolhida posteriormente.

| Cenário | n viável | Status | Solver mediano (s) | Parede mediana (s) | D total mediana (km) | T máximo mediano (h) |
|---|---:|---|---:|---:|---:|---:|
| O1-fixed-cuiaba-k2-distance | 10 | {"feasible": 10} | 300.1 | 300.1 | 10020.7 | 87.40 |
| O1-fixed-cuiaba-k2-time | 10 | {"feasible": 10} | 300.1 | 300.1 | 13324.4 | 92.94 |
| O1-fixed-cuiaba-k3-distance | 10 | {"feasible": 10} | 300.1 | 300.1 | 9938.1 | 87.74 |
| O1-fixed-cuiaba-k3-time | 10 | {"feasible": 10} | 300.1 | 300.1 | 12122.2 | 56.13 |
| O2-variable-origin-k2-distance | 10 | {"feasible": 10} | 303.9 | 303.9 | 10266.4 | 131.02 |
| O2-variable-origin-k2-time | 10 | {"feasible": 10} | 304.9 | 304.9 | 12882.9 | 88.98 |
| O2-variable-origin-k3-distance | 10 | {"feasible": 10} | 303.9 | 303.9 | 10122.6 | 108.30 |
| O2-variable-origin-k3-time | 10 | {"feasible": 10} | 304.9 | 304.9 | 13353.3 | 61.33 |

## Sensibilidade de P2

Os valores λ=0, 0,25, 0,5, 1 e 2 foram selecionados sobre os mesmos candidatos de cada repetição. λ=0 é conferido contra P1 pelo runner D6.

| ID | λ | n viável | Objetivo mediano | Sobreposição mediana (km) | T máximo mediano (h) |
|---|---:|---:|---:|---:|---:|
| E07 | 0.0 | 10 | 10020681.835 | 0.1 | 87.40 |
| E07 | 0.25 | 10 | 10020714.642 | 0.1 | 87.40 |
| E07 | 0.5 | 10 | 10020747.449 | 0.1 | 87.40 |
| E07 | 1.0 | 10 | 10020813.062 | 0.1 | 87.40 |
| E07 | 2.0 | 10 | 10020944.289 | 0.1 | 87.40 |
| E08 | 0.0 | 10 | 334583.214 | 694.2 | 92.94 |
| E08 | 0.25 | 10 | 344995.921 | 694.2 | 92.94 |
| E08 | 0.5 | 10 | 355408.628 | 694.2 | 92.94 |
| E08 | 1.0 | 10 | 376234.042 | 694.2 | 92.94 |
| E08 | 2.0 | 10 | 417884.871 | 694.2 | 92.94 |
| E13 | 0.0 | 10 | 10266359.653 | 0.0 | 131.02 |
| E13 | 0.25 | 10 | 10266359.653 | 0.0 | 131.02 |
| E13 | 0.5 | 10 | 10266359.653 | 0.0 | 131.02 |
| E13 | 1.0 | 10 | 10266359.653 | 0.0 | 131.02 |
| E13 | 2.0 | 10 | 10266359.653 | 0.0 | 131.02 |
| E14 | 0.0 | 10 | 320341.801 | 186.0 | 88.98 |
| E14 | 0.25 | 10 | 323131.992 | 186.0 | 88.98 |
| E14 | 0.5 | 10 | 325935.947 | 186.0 | 88.98 |
| E14 | 1.0 | 10 | 331038.601 | 164.1 | 89.22 |
| E14 | 2.0 | 10 | 340884.310 | 164.1 | 89.22 |
| E19 | 0.0 | 10 | 9938084.661 | 0.1 | 87.74 |
| E19 | 0.25 | 10 | 9938117.468 | 0.1 | 87.74 |
| E19 | 0.5 | 10 | 9938150.274 | 0.1 | 87.74 |
| E19 | 1.0 | 10 | 9938215.888 | 0.1 | 87.74 |
| E19 | 2.0 | 10 | 9938347.115 | 0.1 | 87.74 |
| E20 | 0.0 | 10 | 202085.798 | 133.3 | 56.13 |
| E20 | 0.25 | 10 | 204085.920 | 133.3 | 56.13 |
| E20 | 0.5 | 10 | 206086.043 | 133.3 | 56.13 |
| E20 | 1.0 | 10 | 210086.288 | 133.3 | 56.13 |
| E20 | 2.0 | 10 | 218086.779 | 133.3 | 56.13 |
| E25 | 0.0 | 10 | 10122574.033 | 11.2 | 108.30 |
| E25 | 0.25 | 10 | 10125371.712 | 11.2 | 108.30 |
| E25 | 0.5 | 10 | 10128169.392 | 11.2 | 108.30 |
| E25 | 1.0 | 10 | 10133764.750 | 11.2 | 108.30 |
| E25 | 2.0 | 10 | 10144955.468 | 11.2 | 108.30 |
| E26 | 0.0 | 10 | 220777.702 | 897.4 | 61.33 |
| E26 | 0.25 | 10 | 234238.048 | 897.4 | 61.33 |
| E26 | 0.5 | 10 | 247698.394 | 897.4 | 61.33 |
| E26 | 1.0 | 10 | 273867.587 | 857.0 | 61.79 |
| E26 | 2.0 | 10 | 325288.926 | 857.0 | 61.79 |

## Arquivos reproduzíveis

- `observations.csv`: métricas por repetição.
- `d5-observations.csv`: métricas D5 por cenário e repetição.
- `lambda-sensitivity.csv`: resultados de P2 por λ e repetição.
- `summary.json`: estatísticas descritivas e metadados.
- `experiments/results/.../index.json`: seeds, hashes, estado e artefatos de cada repetição.
