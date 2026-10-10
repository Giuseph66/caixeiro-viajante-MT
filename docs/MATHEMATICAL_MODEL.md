# Modelo matemático

**Estado:** D6 valida P1/P2/P3 por IDs físicos e busca exaustiva em conjuntos finitos de rotas; P2/P3 ainda não cobrem todos os caminhos alternativos da rede integral.
**Unidades:** distância em metros, tempo em segundos, municípios por código IBGE.

## Conjuntos e custos

- M: sedes municipais oficiais de MT.
- k ∈ {1, 2, 3}: agentes homogêneos, simultâneos e ativos.
- s: origem comum associada a ponto rodoviário verificado.
- G=(V,E): grafo dirigido para automóveis.
- Cada arco e tem comprimento l_e ≥ 0, tempo estimado t_e ≥ 0, geometria e physical_segment_id p(e).
- E_phys: segmentos físicos após agrupar arcos/sentidos da mesma pista.
- d_ij: menor distância rodoviária dirigida entre sedes i,j.
- τ_ij: menor tempo rodoviário dirigido entre sedes i,j.
- q_i ≥ 0: atendimento opcional por sede; inicialmente zero.

d_ij e τ_ij são calculados separadamente. O caminho mais curto pode diferir do caminho mais rápido. Ambos vêm da rede, nunca de linhas retas entre municípios.

## Atendimento e rotas abertas

M_s = M sem s se a origem coincidir com uma sede; caso contrário M_s=M. A sede de origem é atendida uma única vez no início compartilhado, sem atribuição duplicada. Origem fora de M mantém as 142 sedes como clientes.

Rota de cada agente a: R_a=(s, i_a1, i_a2, ..., i_aq), com i_aj ∈ M_s. O fim físico é a última sede da sequência. Restrições:

1. Cada i ∈ M_s ocorre em exatamente uma sequência.
2. Agentes distintos não recebem atendimento duplicado.
3. Todas as rotas começam em s e não precisam voltar.
4. Para k>1, cada agente atende ao menos uma sede distinta de s.
5. Transitar por sede atribuída a outro agente não transfere seu atendimento.
6. Cada perna deve corresponder a caminho dirigido válido em G.

No solver, um terminal artificial sem custo de entrada pode encerrar cada veículo. Não representa ponto geográfico nem produz distância, tempo, geometria ou sobreposição.

## Indicadores

Seja P_a a concatenação das pernas rodoviárias e S_a o conjunto atendido:

- D_a = soma de length_e por toda travessia em P_a; repetir trecho pelo mesmo agente conta novamente.
- T_a = soma de travel_time_e em P_a mais soma de q_i para sedes atendidas.
- D_total = soma_a D_a.
- T_max = max_a T_a, pois partidas são simultâneas.

Serviço da sede de origem tem custo zero nesta versão, para não cobrar o mesmo atendimento a todos os agentes. Serviço não nulo na origem exige regra de execução própria.

## Objetivos

**A — distância total:** em P1, minimizar D_total.

**B — tempo de conclusão:** em P1, minimizar T_max, não a soma dos tempos. A documentação OR-Tools explica span global como maior rota; somar coeficiente de span ao custo de arcos resulta em objetivo composto, não automaticamente em minimização pura de T_max. Representar o máximo explicitamente ou testar limite de tempo. Sem prova, classificar como viável/best-known ou não encontrada no limite.

Tempo é estimativa estática. Velocidades ausentes precisam de imputação versionada. A* temporal usa distância geodésica dividida por limite superior válido de velocidade; sem esse limite, usar Dijkstra como referência.

## Sobreposição física

u_ae=1 se agente a percorre segmento físico e ao menos uma vez. L_e é seu comprimento. Definir distância redundante:

O = soma_e L_e × max(0, soma_a u_ae - 1).

Conta uma vez por agente adicional que compartilha o trecho; repetição do mesmo agente não aumenta O, mas entra em D_a. Sentidos opostos compartilham quando têm o mesmo ID físico. Pistas independentes devem ter IDs distintos. Compartilhar somente nó não conta trecho.

**P1 — livre:** sem penalidade/restrição; relatar O.

**P2 — penalizada:** lambda principal provisório 1; sensibilidade {0, 0.25, 0.5, 2} para k=2 e 3. lambda zero reproduz baseline.

- Distância: J_D = D_total + lambda × O, em metros equivalentes.
- Tempo: J_T = T_max + lambda × O / v_ref, em segundos, com v_ref=60 km/h convertido em m/s.

O termo temporal converte distância redundante em tempo equivalente. Relatar separadamente T_max, O, penalidade e J_T. lambda e v_ref são parâmetros, não observações.

P2 é acoplado: mudar caminho de um agente muda a penalidade de outro. Matriz estática não representa O sozinha. Enquanto houver candidatos, reponderação ou busca local sem prova, resultado é heurístico, não ótimo global.

**P3 — proibida:** para todo segmento e, soma_a u_ae ≤ 1. Inclui sentidos opostos; um agente pode reutilizar seu trecho sem violação. Validar sobre sequências físicas finais. Sem solução heurística significa “nenhuma solução viável encontrada”; “inviável comprovada” exige certificado que cubra a instância integral.

Na execução D6 real, a busca combina sequências de visita candidatas e mantém o caminho D3 mais curto de cada perna. A enumeração é exaustiva apenas sobre as rotas fornecidas; `not_found` em P3 não prova inviabilidade da rede rodoviária.

## Origem

- Fixa: início comum s, sede/ponto rodoviário validado.
- Otimizada: testar até 142 sedes verificadas; quando s também é destino, atender uma vez no início.
- O2 pode exigir até 142 subproblemas por configuração. Registrar candidatos, limites e estado best-known. Só afirmar origem ótima se candidatos/subproblemas requeridos forem resolvidos com prova.
- O3, origens independentes, fora da primeira versão.

## Dijkstra, A* e status

Dijkstra é referência para custos não negativos. Distância geodésica só é limite inferior de distância após verificar geometria/comprimentos; para tempo usar geodésica/v_max com v_max que limite todas as arestas permitidas. Caso contrário, heurística zero.

Guardar caminhos separados para menor distância e menor tempo. Comparar A*/Dijkstra em grafos dirigidos, assimétricos, paralelos e sem caminho. P2/P3 mantêm alternativas ou recomputam trajetórias sob custos/restrições conjuntos.

Detectar sedes sem acesso, desconexão/sentido impossível, P3 bloqueada por gargalo, origem sem saídas alternativas, recorte insuficiente, ID físico ambíguo, limite de tempo/memória e falta de destinos para agentes ativos. Falha técnica não é inviabilidade matemática.

Separar status do job (queued/running/succeeded/failed/interrupted) do status da solução (feasible/not_found/infeasible_proven/optimal_proven com certificado).
