Plataforma de pesquisa para planejar rotas rodoviárias abertas entre as sedes dos municípios de Mato Grosso, com um a três agentes. D2 contém 142 municípios, sedes oficiais do IBGE e snapshot nacional OSM. D3 gera o grafo direcionado e matrizes a partir dos dados locais fixados por manifesto.


## Ambiente local

- Linux nativo, Python 3.12 ou 3.13, Node.js 22.12 ou superior.
- PostgreSQL 17 em container Docker Compose para persistência; o processamento local do grafo D3 não depende do banco.
- O banco fica acessível somente em `127.0.0.1:5433`, usa volume persistente e tem limite de memória de 1 GiB.

Veja [instalação nativa no Linux](docs/INSTALLATION_LINUX.md) para criar o ambiente Python, instalar pacotes locais, configurar o banco e executar os serviços.

Consulte o [relatório de qualidade D2](docs/DATA_QUALITY_D2.md) para fontes, hashes, validações e limites dos dados atuais.

## Processamento D3

Com o grafo D3.1 gerado, calcule as matrizes direcionadas de distância e tempo:

    cd backend
    ../.venv/bin/python -m app.routing.matrices --workers 8

O cálculo usa acelerador C++ compilado localmente (`g++`), imprime progresso por origem e grava checkpoints que são reutilizados se a execução for interrompida. Sem `g++`, usa a implementação Python de referência. Para executar os benchmarks de 5, 10, 20, 50 e 142 sedes, acrescente `--benchmark`.

Importação OSM, construção do grafo, matrizes e runners D4–D6 abrem uma unidade `systemd-run --user` própria com limite rígido de 25.000.000.000 bytes antes de carregar dados grandes. Workers ficam no mesmo cgroup; se o limite não puder ser aplicado, o cálculo para antes de iniciar.

## Geometria das rotas e rodovias do IBGE

Resultados antigos (D4/D5 sem `legs`) aparecem no mapa como linhas tracejadas entre sedes. Para recuperar as vias OSM exatas, reconstrua os trechos a partir da matriz em cache:

    cd backend
    ../.venv/bin/python -m app.services.route_legs

O comando grava `data/processed/jobs/<run>.legs.json.gz` ao lado de cada resultado, sem alterar o JSON original, e confere a soma dos custos da matriz com o objetivo registrado. Resultados novos já gravam as vias.

O mapa exibe sempre uma camada de fundo com os trechos rodoviários da Base Cartográfica Contínua 1:250.000 (BC250, versão 2025). Ela é só visual: as rotas continuam calculadas sobre o grafo OSM. Para gerar o arquivo `frontend/public/ibge-rodovias-mt.geojson`:

    cd backend
    ../.venv/bin/python -m app.geography.ibge_roads
    ../.venv/bin/python -m app.geography.ibge_rivers

O segundo comando gera `frontend/public/ibge-rios-mt.geojson` com os cursos d'água nomeados (reaproveita o GeoPackage extraído pelo primeiro). Ao passar o mouse sobre uma rodovia ou rio, o mapa mostra o nome: sigla e tipo da via (por exemplo `BR-163 · Rodovia federal · pavimentada`) ou o nome do rio. Trechos sem sigla na BC250 aparecem só pelo tipo.

## Repetição na própria rota

A configuração `repetition_policy` vale para cada agente, com 1, 2 ou 3 equipes, e é independente de P1/P2/P3 (compartilhamento entre equipes). O mesmo trecho físico no sentido contrário também conta como repetição. Cada passagem depois da primeira acrescenta seu comprimento à medida **Repetição própria**.

- `allow`: permite repetição, sem custo extra; padrão da API para compatibilidade com execuções anteriores.
- `reduce`: soma ao objetivo um metro por metro repetido; no objetivo tempo, converte a penalidade usando 60 km/h. A distância e o tempo reais continuam disponíveis separadamente.
- `forbid`: só publica rotas com repetição zero para cada agente. Se a busca não encontrar uma, retorna `not_found`, sem entregar uma rota que viole a regra.

A busca ajusta a ordem e a distribuição das cidades usando os caminhos direcionados já gravados na matriz D3, preservando origem e destino fixos. Reserva 25% do tempo configurado para esse ajuste, sem criar processos extras. `not_found` não prova impossibilidade na rede rodoviária inteira: caminhos alternativos fora da matriz não são enumerados. Cidades com acesso único podem exigir ida e volta pelo mesmo trecho.

## Reproduzir a D9

Com os manifestos D3–D6 presentes, registre primeiro o orçamento e então execute as dez repetições configuradas:

    .venv/bin/python experiments/runners/d9_repeated_experiments.py
    .venv/bin/python experiments/runners/d9_repeated_experiments.py --run
    .venv/bin/python experiments/analysis/d9_analysis.py

O primeiro comando apenas estima CPU, RAM, duração, disco e hash da configuração. O runner aplica teto de RAM de 25 GB e grava checkpoints e relatórios por repetição em `experiments/results/`; a análise grava métricas e estatísticas em `experiments/analysis/`. Consulte o [protocolo experimental](docs/EXPERIMENTAL_PROTOCOL.md) e o [orçamento D9](experiments/analysis/d9-budget-mt-2026-10-10.md).

O lote D9 de 2026-10-10 terminou com 10/10 repetições: 280 observações principais (220 `feasible`, 60 `not_found`), 80/80 resultados D5 viáveis e 400 observações de sensibilidade P2. Os `not_found` são todos P3 sem solução no conjunto de candidatas e não provam inviabilidade da rede. O [relatório D9](experiments/analysis/d9-mato-grosso-2026-10-10/report.md) e seus CSVs guardam os resultados. A unidade de cálculo teve teto rígido de 25.000.000.000 bytes, pico de 15,6 GiB e nenhum evento OOM.

## Iniciar

Configure `.env` a partir de `.env.example`, defina uma senha local em `POSTGRES_PASSWORD` e use a mesma senha em `DATABASE_URL`. Depois inicie o banco pela raiz do projeto:

    docker compose up -d db

Backend:

    cd backend
    source ../.venv/bin/activate
    python run_api.py --reload

O lançador mede a memória disponível antes de iniciar a API e aplica um teto de 80% dela, limitado a 25 GB. Cada cálculo faz a mesma validação antes de começar.

Frontend, em outro terminal:

    cd frontend
    npm ci
    npm run dev

API inicial: http://127.0.0.1:8000/api/health. A documentação OpenAPI fica em http://127.0.0.1:8000/docs.
Frontend: http://127.0.0.1:5173. A tela permite iniciar e acompanhar jobs, cancelar, visualizar rotas rodoviárias reais, comparar execuções e exportar resultados em JSON.

O endpoint de saúde confirma somente que a API está em execução; o banco não é verificado por ele. Para a primeira migração, configure DATABASE_URL e execute o comando na documentação de instalação.

## Planejamento

- [Plano](docs/PROJECT_PLAN.md)
- [Arquitetura](docs/ARCHITECTURE.md)
- [Modelo matemático](docs/MATHEMATICAL_MODEL.md)
- [Entregas](docs/DELIVERABLES.md)
- [Protocolo experimental](docs/EXPERIMENTAL_PROTOCOL.md)
- [Decisões técnicas](docs/TECHNICAL_DECISIONS.md)

Dados rodoviários, matrizes e experimentos usam manifestos com fonte, versão, licença e hashes. Consulte [entregas](docs/DELIVERABLES.md) para o estado e os limites de cada resultado.
# caixeiro-viajante-MT
