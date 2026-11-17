# EPAV Product Evaluator

API FastAPI no Cloudflare para os dois jogos EPAV. Catálogo, ranking, painel e sincronização usam PostgreSQL no Neon. Firebase é usado somente para autenticação e verificação da permissão administrativa. Fotos permanecem no Cloudflare R2 e os dados públicos frequentes continuam em Redis.

- `POST /v1/recomendacoes`: seleção por parâmetros do cliente e sorteio, sem IA.
- `POST /v1/avaliacoes`: avaliação Groq de 0 a 1000, com diálogo validado no servidor e troca de chave em caso de cota.
- `POST /v1/catalogo/itens`: fichas públicas por SKU para a API sem IA.
- `GET /v1/ranking`: top 20 público, sem identificadores de conta.
- `POST /v1/ranking`: gravação com token Firebase; o servidor determina o usuário e a data.
- `GET /v1/admin/dados/{colecao}`: dados paginados, somente com a permissão `admin: true` verificada no Firebase.
- `POST /v1/admin/produtos/{id}`: edição validada, revisão obrigatória e histórico na mesma transação.
- `/v1/sync/*`: operações do robô com segredo exclusivo; não acessíveis com tokens de jogadores.

As tabelas de negócio e a view pública ficam em `epav_data`. O suporte tipado necessário à API fica em `epav_internal`, fora do explorador. As cópias antigas da migração foram retiradas da produção e preservadas em uma branch de backup do Neon. Nenhuma tabela ou view possui colunas TEXT, JSON ou JSONB. JSON é utilizado somente nas mensagens HTTP e nos parâmetros de consultas. SQL e scripts estão em [epav-modelagem](https://github.com/EPAV-GAME/epav-modelagem).

## Configuração

Segredos do Worker: `DATABASE_URL` (conexão pooled da conta limitada ao esquema operacional), `DATA_SYNC_TOKEN` (robô), `GROQ_API_KEYS` e `CACHE_INVALIDATION_TOKEN`. A API não usa chave privada de serviço Firebase nem consulta Firestore. `FIREBASE_WEB_API_KEY` é configuração pública de autenticação.

O transporte Python usa o protocolo HTTPS do driver Neon serverless: consultas parametrizadas e lotes em transação serializável. Erros não expõem SQL, credenciais ou respostas do banco. O cache Redis é compartilhado pelo binding `REDIS_CACHE`, com catálogo de fotos comprimido por 15 minutos, ranking por 30 segundos e invalidação após gravações. Falhas de Redis recorrem ao Postgres.

## Verificação e publicação

`PYTHONPATH=src uv run python -m unittest discover -s tests -v`

`uv run pywrangler deploy --dry-run`

`uv run pywrangler deploy --profile epav`

A API sem IA continua usando o binding existente deste catálogo. Margens, fornecedores e identidade do jogador não são enviados à Groq nem às fichas públicas.
