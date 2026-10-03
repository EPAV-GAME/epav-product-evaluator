# EPAV — API de avaliação de produtos

API independente em **FastAPI**, hospedada em **Cloudflare Python Workers**, usando **Groq** para avaliar a recomendação de um alimento ao cliente do jogo. Não treina um modelo próprio: aplica uma rubrica pedagógica a um modelo hospedado pela Groq.

## Resultado: 0 a 1000

| Critério | Pontos máximos |
|---|---:|
| Adequação à necessidade | 350 |
| Adequação à ocasião | 250 |
| Praticidade | 150 |
| Respeito às restrições declaradas | 150 |
| Quantidade e desperdício | 100 |

A IA dá notas de 0 a 100 por critério e cita evidências. A API valida as notas e calcula a soma ponderada. O resultado contém `score`, `percentual_adequacao`, classificação, justificativas, sugestão, informações faltantes, versão da rubrica e hash do contexto. O percentual representa adequação pedagógica; não é uma probabilidade estatística de acerto.

Classificações: excelente ≥850, boa ≥650, parcial ≥400, inadequada <400. Contradição explícita entre restrição do cliente e fato confirmado do produto limita a nota a 200. Quantidade ausente recebe nota 50 nesse critério. Informações inexistentes não devem virar afirmações sobre preço, composição, alérgenos ou preparo.

## Contexto confiável

- Usa os cinco cenários oficiais exportados de `epav-game/js/clientsData.js`.
- Recebe IDs das decisões e reconstrói as falas e a ficha de escuta até a etapa atual.
- Rejeita opções inválidas, etapas puladas e dados extras, como uma nota enviada pelo jogador.
- Lê o produto diretamente de `produtos_swift` no Firebase e exige `disponivelNoJogo=true`.
- Envia à Groq somente o contexto fictício do atendimento e campos selecionados do produto. Não envia identidade do jogador, margens, fornecedores ou dados de vendas.

O histórico recebido ainda é uma declaração do jogador, validada contra o roteiro. Esta API oferece feedback de aprendizagem; não publica nem altera o ranking. Para notas oficiais de competição, as ações da partida devem ser registradas e verificadas no servidor.

## Endpoints

- `GET /health`: saúde e configuração, sem expor segredos.
- `GET /docs`: documentação interativa FastAPI.
- `GET /openapi.json`: contrato da API.
- `POST /v1/avaliacoes`: avaliação; exige `Authorization: Bearer <ID_TOKEN_FIREBASE>` de jogador autenticado em `epav-game`.

API publicada: https://epav-product-evaluator.kevinernandes2012.workers.dev

Na documentação `/docs`, usar **Authorize** para informar o ID token do Firebase e testar o endpoint.

Exemplo de corpo:

```json
{
  "cliente_id": "cliente1",
  "no_atual": "d3",
  "historico": [
    {"no_id": "d1", "opcao_id": "d1-o1"},
    {"no_id": "d2", "opcao_id": "d2-o1"}
  ],
  "produto_id": "ID_REAL_DO_DOCUMENTO_FIREBASE",
  "quantidade": {"unidades": 3, "peso_total_kg": 2.1},
  "observacao_jogador": "Escolhi esta opção pensando na praticidade do churrasco."
}
```

`no_atual` é a fala atual já vista. `historico` contém as decisões anteriores. Quantidade e observação são opcionais. A API fornece o serviço para o futuro seletor de produtos do jogo; esta versão não adiciona a tela de seleção ao jogo.

## Chaves Groq

Segredo Cloudflare `GROQ_API_KEYS`:

```text
gsk_CHAVE1|gsk_CHAVE2|gsk_CHAVE3
```

Aceita até 16 chaves, elimina duplicadas e começa pela primeira disponível. Em 429/cota, credencial recusada ou indisponibilidade do provedor, tenta a seguinte. Usa `Retry-After` para suspender a chave temporariamente dentro da instância do Worker; outras instâncias mantêm seu próprio estado. Erro no modelo ou no formato da requisição não consome todas as chaves. A Groq pode compartilhar cotas entre chaves da mesma organização.

Todas indisponíveis: HTTP 503 com `Retry-After`, sem pontuação inventada. Resposta inválida da IA: HTTP 502. Modelo padrão: `openai/gpt-oss-20b`, resposta JSON Schema estrita; configurável em `GROQ_MODEL` para outro modelo Groq com suporte ao mesmo formato.

## Segurança e limites

### Três produtos para a etapa de recomendação

`POST /v1/recomendacoes` aceita `{cliente_id, no_atual, historico}` e exige o mesmo token Firebase da avaliação. Etapas válidas: cliente1/d3, cliente2/d5, cliente3/d7, cliente4/d6 e cliente5/d7. O servidor valida a sequência oficial e retorna exatamente três alimentos diferentes disponíveis no jogo, a ficha de escuta e fichas públicas com nome, código, tipos, ocasiões, marca, formato, unidade, peso e URL de foto quando disponível.

A recuperação usa consultas limitadas de até 150 registros por categoria/ocasião, com cache por uma hora, filtrando `disponivelNoJogo=true` e os indicadores SIM/NÃO sincronizados pelo painel admin. Consultas de igualdade aproveitam índices existentes. A seleção considera o perfil e as falas reveladas, remove duplicações por código/nome e varia a ordem dos cartões. Não consome tokens Groq, não oferece uma nota antecipada nem expõe os dados comerciais. A avaliação final continua em `/v1/avaliacoes`, lendo novamente o produto no Firebase para conferir disponibilidade e avaliar a quantidade escolhida.

O Worker Python usa as APIs oficiais Google com a conta de serviço guardada em segredo. Não é necessário instalar bibliotecas gRPC do Firebase Admin SDK no runtime do Cloudflare, nem liberar leitura pública da coleção.

Chaves ficam apenas no Cloudflare, fora do GitHub e do navegador. A credencial Firebase é usada para leitura. O cliente precisa estar autenticado; há limites Cloudflare por jogador (15/min), IP (30/min) e serviço (120/min). Os limites são aproximados por localização Cloudflare. Requisições têm até 32 KiB e até 40 decisões; avaliação tem prazo total de 40 segundos. CORS permite o domínio do jogo. A API não registra tokens, chaves ou respostas completas do provedor.

## Implantação

```sh
uv sync --locked
uv run pywrangler deploy --profile epav
uv run pywrangler secret put GROQ_API_KEYS --profile epav
uv run pywrangler secret put FIREBASE_SERVICE_ACCOUNT_JSON --profile epav
```

O arquivo de conta de serviço deve ser do projeto `epav-game`. `FIREBASE_WEB_API_KEY` é a configuração pública de autenticação do Firebase. Não colocar chaves Groq no frontend. Para mudar as chaves, atualizar somente o segredo `GROQ_API_KEYS`.

Testes:

```sh
PYTHONPATH=src uv run python -m unittest discover -s tests -v
uv run pywrangler deploy --dry-run
```

Desenvolvimento nativo: configurar variáveis de ambiente e usar `uv run uvicorn main:app --app-dir src`. Limites vinculados ao Cloudflare são aplicados no Worker; desenvolvimento nativo não simula esses bindings.

Para atualizar os roteiros:

```sh
node scripts/export_scenarios.mjs /caminho/epav-game/js/clientsData.js
```

O GitHub Actions executa testes e verifica o bundle em cada push e PR. Implantação inicial usa o perfil Cloudflare autorizado localmente; CI não contém credenciais de implantação.
