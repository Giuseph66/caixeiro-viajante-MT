Plataforma de pesquisa para planejar rotas rodoviárias abertas entre as sedes dos municípios de Mato Grosso, com um a três agentes. D2 contém 142 municípios, sedes oficiais do IBGE e snapshot nacional OSM. Código D3 para grafo, caminhos e matrizes existe; processamento real aguarda confirmação para comandos longos.


## Ambiente local

- Linux nativo, Python 3.12 ou 3.13, Node.js 22.12 ou superior.
- PostgreSQL local para persistência; o processamento local do grafo D3 não depende do banco.
- Não usa Docker, Compose ou Podman.

Veja [instalação nativa no Linux](docs/INSTALLATION_LINUX.md) para criar o ambiente Python, instalar pacotes locais, configurar o banco e executar os serviços.

Consulte o [relatório de qualidade D2](docs/DATA_QUALITY_D2.md) para fontes, hashes, validações e limites dos dados atuais.

## Iniciar

Backend:

    cd backend
    source ../.venv/bin/activate
    uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

Frontend, em outro terminal:

    cd frontend
    npm run dev

API inicial: http://127.0.0.1:8000/api/health. A documentação OpenAPI fica em http://127.0.0.1:8000/docs.

O endpoint de saúde confirma somente que a API está em execução; o banco não é verificado por ele. Para a primeira migração, configure DATABASE_URL e execute o comando na documentação de instalação.

## Planejamento

- [Plano](docs/PROJECT_PLAN.md)
- [Arquitetura](docs/ARCHITECTURE.md)
- [Modelo matemático](docs/MATHEMATICAL_MODEL.md)
- [Entregas](docs/DELIVERABLES.md)
- [Protocolo experimental](docs/EXPERIMENTAL_PROTOCOL.md)
- [Decisões técnicas](docs/TECHNICAL_DECISIONS.md)

Dados rodoviários, matrizes e experimentos só serão adicionados nas entregas correspondentes, com fonte, versão, licença e hash documentados.
# caixeiro-viajante-MT
