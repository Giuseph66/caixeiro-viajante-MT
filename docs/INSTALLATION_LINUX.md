# Instalação nativa no Linux

Guia de instalação e dados D1/D2, voltado a Debian/Kali Linux x86_64. Não usa contêineres. Os comandos com sudo abaixo são instruções para a pessoa administradora executar quando necessário; não foram executados pelo agente.

## Requisitos

- Python 3.12 ou 3.13, venv e pip.
- Node.js 22.12+ e npm. O workspace foi preparado com Node 24.12.0.
- PostgreSQL para persistência. O grafo e as matrizes D3 são arquivos locais e não exigem PostGIS.

O Vite requer Node 20.19+ ou 22.12+; Node 24.12.0 atende ao requisito atual. Confira avisos de versão do npm ao instalar. Referências: [Vite](https://vite.dev/guide/) e [OR-Tools Python](https://developers.google.com/optimization/install/).

## Preparar Python

Na raiz do projeto:

    python3 --version
    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install --upgrade pip
    python -m pip install -r backend/requirements.lock
    python -m pip install --no-deps -e backend

O arquivo requirements.lock contém versões transitivas congeladas para este ambiente Linux/Python. Após editar dependências em backend/pyproject.toml, atualize e revise o lock antes de distribuir.

Para recriar o lock no futuro:

    cd backend
    python -m pip install -e ".[dev]"
    python -m pip freeze

Revise a saída, remova a entrada editable local e grave dependências com versões fixas em requirements.lock. Não use pip global.

## Preparar frontend

    cd frontend
    npm ci
    npm run dev

O comando npm run build cria o bundle estático em dist. Node está fixado para conveniência no arquivo .nvmrc; gerenciadores de versões como nvm são opcionais.

## Obter e importar os dados D2

Os arquivos brutos são locais e ficam fora do Git; os manifestos registram origem, data, tamanho, licença e hashes. O snapshot OSM é a versão estática `brazil-261007`, não o alias mutável `latest`.

Na raiz do projeto, baixe os três arquivos. O PBF nacional tem cerca de 2,1 GB:

    mkdir -p data/raw/ibge data/raw/osm
    curl --fail --location --compressed https://servicodados.ibge.gov.br/api/v1/localidades/estados/51/municipios --output data/raw/ibge/municipios-mt.json
    curl --fail --location https://geoftp.ibge.gov.br/organizacao_do_territorio/estrutura_territorial/localidades/Localidades_do_Brasil/2022/Localidades_UFs_shp.zip --output data/raw/ibge/Localidades_UFs_shp.zip
    curl --fail --location https://download.geofabrik.de/south-america/brazil-261007.osm.pbf --output data/raw/osm/brazil-261007.osm.pbf
    curl --fail --location https://download.geofabrik.de/south-america/brazil-261007.osm.pbf.md5 --output data/raw/osm/brazil-261007.osm.pbf.md5
    (cd data/raw/osm && md5sum --check brazil-261007.osm.pbf.md5)

O `curl --compressed` grava a resposta da API IBGE descomprimida como JSON. A checagem MD5 confirma a cópia PBF conforme o arquivo publicado pela Geofabrik; o importador também calcula SHA-256 local.

Com o ambiente Python instalado e ativo:

    cd backend
    ../.venv/bin/python -m app.geography.import_data

O processo valida códigos e sedes, grava `data/processed/municipalities-mt.geojson`, conta tags `highway` do PBF em streaming e atualiza os relatórios/manifests em `data/processed/` e `data/manifests/`. A leitura nacional não monta o grafo nem filtra acessos automotivos; a D3 implementa esse processamento em etapa separada.

Os arquivos exatos e resultados da execução estão em [DATA_QUALITY_D2.md](DATA_QUALITY_D2.md).

## Construir grafo e matrizes D3

A malha estadual IBGE usada para delimitar o recorte é um arquivo público de 305 KiB. O importador lê o PBF nacional uma vez, usa índice PyOsmium esparso temporário em disco e guarda no grafo em memória somente trechos dentro da caixa do limite estadual acrescida de 100 km.

Na raiz do projeto:

    curl --fail --location https://geoftp.ibge.gov.br/organizacao_do_territorio/malhas_territoriais/malhas_municipais/municipio_2022/UFs/MT/MT_UF_2022.zip --output data/raw/ibge/MT_UF_2022.zip

Com o ambiente local ativo, execute a partir de backend:

    ../.venv/bin/python -m app.graph.builder

O comando verifica o SHA-256 do snapshot OSM, constrói data/processed/road-network-mt.jsonl.gz e atualiza data/manifests/road-network-mt.json. O primeiro processamento ainda não foi medido; a D2 registrou 98,222 s e 436,72 MiB ao contar tags do mesmo PBF, sem construir rede.

Depois da revisão do grafo e do uso de memória, as matrizes podem ser calculadas:

    ../.venv/bin/python -m app.routing.matrices

Esse comando executa Dijkstra para 142 origens, em distância e tempo, grava caminhos por par e cria cache nomeado pelo hash. Para registrar tamanhos 5/10/20/50/142:

    ../.venv/bin/python -m app.routing.matrices --benchmark

O benchmark repete cálculos progressivos sobre o grafo regional; custo real ainda é desconhecido. Confirme recursos antes de iniciar; duração/RSS ainda não foram medidos. Custos de matriz começam no nó viário associado à sede e não incluem o conector fora da rede. O relatório registra snaps acima de 10 km e pares sem caminho dirigido.

As restrições via nó são aplicadas. Via-ways, acessos/velocidades condicionais, barreiras e balsas não entram neste perfil inicial e ficam anotados no manifesto.

## PostgreSQL e PostGIS

Inspecione primeiro as versões e clusters presentes:

    psql --version
    pg_lsclusters
    apt-cache search postgresql-postgis

O host desta execução tinha cliente PostgreSQL 18.3 e servidores 17.6/18.3 instalados, porém nenhum cluster respondeu. Não instalar ou iniciar serviço automaticamente. Se o administrador precisar instalar PostgreSQL/PostGIS num sistema Debian/Kali, escolha o pacote PostGIS que corresponde à versão principal do servidor. A documentação oficial PostGIS para Debian indica o padrão postgresql-<versão>-postgis-3; consulte [instalação Debian/Ubuntu](https://postgis.net/documentation/getting_started/install_ubuntu/) e [PostgreSQL para Debian](https://www.postgresql.org/download/linux/debian/).

Exemplo para PostgreSQL 18, caso essa versão seja a escolhida e os repositórios do sistema forneçam o pacote:

    sudo apt install postgresql-18 postgresql-18-postgis-3

Depois de instalar, o administrador pode iniciar o cluster pelo mecanismo de serviço da distribuição e criar usuário/banco locais:

    sudo -u postgres createuser --pwprompt mt_routing
    sudo -u postgres createdb --owner=mt_routing mt_routing

Para habilitar PostGIS no banco de aplicação quando começar a ingestão geográfica:

    psql -U mt_routing -d mt_routing -c "CREATE EXTENSION postgis;"
    psql -U mt_routing -d mt_routing -c "SELECT postgis_full_version();"

Não habilite PostGIS no banco de manutenção postgres. A migração inicial deste projeto não cria colunas espaciais nem requer a extensão.

## Configurar aplicação e banco

Na raiz:

    cp .env.example .env

Edite DATABASE_URL em .env com usuário, senha, host e banco locais. O arquivo .env é ignorado pelo Git. Não salve credenciais reais em .env.example.

Com o ambiente Python ativo:

    cd backend
    alembic upgrade head

Para inspecionar o SQL PostgreSQL sem conectar a um servidor:

    alembic upgrade head --sql

A migração inicial cria apenas metadados de execuções. PostgreSQL precisa estar ativo para alembic upgrade head; a geração --sql não altera banco.

## Executar API e interface

Terminal 1:

    source .venv/bin/activate
    cd backend
    uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

Terminal 2:

    cd frontend
    npm run dev

Validação manual inicial:

    curl http://127.0.0.1:8000/api/health

Resposta esperada:

    {"status":"ok","service":"Rotas Multiagente MT"}

## Verificações de desenvolvimento

    cd backend
    python -m pytest
    ruff check app tests alembic
    npm --prefix ../frontend run build

Os resultados dependem do estado do ambiente e devem ser registrados por entrega. Nenhum serviço precisa de contêiner.
