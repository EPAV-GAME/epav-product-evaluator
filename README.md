# EPAV — API de avaliação de produtos

O Worker privado de Redis usa TCP nativo do Cloudflare, com valores enviados em blocos de 16 KiB e leituras transacionais de valor/expiração. Isso permite armazenar o catálogo compactado completo sem a falha de compatibilidade do antigo driver Node.

A avaliação usa [JSON mode da Groq](https://console.groq.com/docs/structured-outputs), com campos e referências permitidas explícitos no prompt. Toda resposta passa pelo modelo Pydantic, referências do diálogo e limites da rubrica. A validação local continua rígida; falhas não recebem nota fictícia. Chamadas legadas com schema podem repetir uma vez em JSON mode após `json_validate_failed`.

## Duas versões do jogo

Esta API conserva a avaliação **com IA** e dez opções por categoria. A [API sem IA](https://github.com/EPAV-GAME/epav-rule-evaluator) calcula sua própria rubrica lógica e oferece cinco opções. Ela reutiliza somente catálogo/contexto/ranking, por service binding, sem chamar a Groq.

`POST /v1/contexto` exige o mesmo token e limites de requisição. Reconstrói o diálogo e as fichas públicas dos IDs atual/anteriores, validando as etapas antes de consultar o catálogo. Não expõe dados comerciais, identidade do jogador ou credenciais. As avaliações de cardápio, nas duas versões, aproveitam as fichas presentes no catálogo compactado de 15 minutos em vez de consultar cada documento novamente. Login e permissões permanecem conferidos a cada pedido; não são cacheados.

Os dois jogos usam a mesma geração Redis e invalidação existente do admin/bot. Cache compartilhado limita atualizações ao TTL ou à invalidação; não confunde sorteios com resultados pessoais. O contrato legado sem categoria mantém sua ficha individual com TTL de 60 segundos.

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

Classificações: excelente ≥850, boa ≥650, parcial ≥400, inadequada <400. A rubrica `epav-produto-v2` exige evidências do cliente e do produto para notas acima de 75. Respostas sem evidência ficam limitadas a 50. A ausência de dados representa uma confirmação pendente, sem atribuir um erro comprovado ao jogador.

- Restrições alimentares sem detalhes/composição recebem 50; sem restrição alimentar declarada, esse critério recebe 100.
- Preparo prioritário sem método/tempo confirmado limita praticidade a 75. Prioridades centrais de preparo, restrição ou orçamento ainda não confirmadas limitam necessidade a 75.
- Quantidade ausente recebe 50. Unidades vezes peso explícito da embalagem são conferidos, com tolerância de 2% ou 10 g; divergência limita quantidade a 25. Peso consistente não comprova porções: refeições e acompanhamentos pendentes limitam esse critério a 75.
- Tipo e ocasião incompatíveis com os parâmetros do atendimento limitam seus critérios a 25; incompatibilidade em ambos limita o total a 200. Esses parâmetros só são usados após a etapa de recomendação ter sido revelada. Categorias ausentes não comprovam incompatibilidade.
- A indicação de contradição da IA, isoladamente, não comprova um conflito alimentar. O catálogo público atual não contém preço, composição, alérgenos nem tempo de preparo; esses dados não são inferidos da descrição ou de alegações do jogador.

A geração usa JSON Schema com referências limitadas às falas realmente presentes, e a API verifica as referências novamente. O cálculo continua no servidor. A avaliação usa uma única chamada Groq, respostas curtas e `reasoning_effort=low` para GPT OSS, mantendo os caches existentes. `Server-Timing` permite distinguir autenticação, consulta do produto e avaliação. A duração depende também da rede e do provedor. Documentação: [raciocínio na Groq](https://console.groq.com/docs/reasoning) e [respostas estruturadas](https://console.groq.com/docs/structured-outputs).

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

`no_atual` é a fala atual já vista. `historico` contém as decisões anteriores. Quantidade e observação são opcionais. O jogo integra cinco etapas de refeição, com dez opções por categoria e avaliação da escolha.

### Refeição construída durante o diálogo

`POST /v1/recomendacoes` com `categoria` (`entrada`, `principal`, `acompanhamento`, `bebida` ou `sobremesa`) retorna até **10 produtos distintos com foto** naquela categoria. A ordem ocupa as últimas cinco falas do roteiro de cada cliente, intercalada com suas respostas originais:

| Cliente | Entrada | Principal | Acompanhamento | Bebidas | Sobremesa |
|---|---|---|---|---|---|
| Lucas | d2 | d3 | d4 | d5 | d6 |
| Marina | d3 | d4 | d5 | d6 | d7 |
| Rafael | d4 | d5 | d6 | d7 | d8 |
| Camila | d5 | d6 | d7 | d8 | d9 |
| André | d6 | d7 | d8 | d9 | d10 |

O papel na refeição é classificado por regras explícitas sobre tipo e nome do alimento, em `src/menu.py`; por exemplo, linguiça **com** queijo coalho continua sendo prato principal. A ocasião do cliente é priorizada, e o sorteio é completado apenas com alimentos reais do mesmo papel. O principal respeita os tipos do perfil. A seleção não chama a IA. Se o acervo tiver menos de dez candidatos, retorna os existentes com `total_disponiveis` e `quantidade_solicitada=10`; categoria vazia retorna `produtos=[]`, sem substituir por outro tipo de alimento.

O catálogo completo é lido em páginas de 500 registros, com limite de 12 páginas. O conjunto de candidatos é compactado e compartilhado no Redis por até 15 minutos, usando uma única entrada e uma única trava para evitar excesso de chamadas por requisição no Cloudflare. As cinco categorias reutilizam esse catálogo e sorteiam suas opções separadamente. Só entram campos públicos e fotos do bucket validado; preços, margens e fornecedores não são incluídos.

Na avaliação, envie a mesma `categoria` e `escolhas_anteriores: [{categoria, produto_id, quantidade}]`. O servidor valida a etapa, a ordem e a ausência de categorias repetidas/futuras, e busca as fichas anteriores no Firebase em paralelo. O modelo considera essas escolhas junto ao histórico original, sem cobrar novamente pontos das respostas do diálogo e sem julgar sobremesa ou bebida como se precisassem ser carne. IDs anteriores continuam sendo declarações do jogador, sem sessão de partida verificada no servidor. A avaliação ainda usa uma única chamada Groq por produto.

Clientes antigos que omitem `categoria` continuam usando o contrato de três opções abaixo.

## Chaves Groq

Segredo Cloudflare `GROQ_API_KEYS`:

```text
gsk_CHAVE1|gsk_CHAVE2|gsk_CHAVE3
```

Aceita até 16 chaves, elimina duplicadas e começa pela primeira disponível. Em 429/cota, credencial recusada ou indisponibilidade do provedor, tenta a seguinte. Usa `Retry-After` para suspender a chave temporariamente dentro da instância do Worker; outras instâncias mantêm seu próprio estado. Erro no modelo ou no formato da requisição não consome todas as chaves. A Groq pode compartilhar cotas entre chaves da mesma organização.

Todas indisponíveis: HTTP 503 com `Retry-After`, sem pontuação inventada. Resposta inválida da IA: HTTP 502. Modelo padrão: `openai/gpt-oss-20b`, resposta JSON Schema estrita; configurável em `GROQ_MODEL` para outro modelo Groq com suporte ao mesmo formato.

## Segurança e limites

### Três produtos para a etapa de recomendação

`POST /v1/recomendacoes` aceita `{cliente_id, no_atual, historico}` e exige o mesmo token Firebase da avaliação. Etapas válidas: cliente1/d3, cliente2/d5, cliente3/d7, cliente4/d6 e cliente5/d7. O servidor valida a sequência oficial e retorna exatamente três alimentos diferentes disponíveis no jogo e com URL válida de foto no bucket Swift, a ficha de escuta e fichas públicas com nome, código, tipos, ocasiões, marca, formato, unidade e peso. Se houver menos de três alimentos compatíveis com foto, retorna `INSUFFICIENT_PRODUCTS`, sem completar com itens sem imagem.

O catálogo Redis guarda somente candidatos com foto por 15 minutos, com chave própria de versão. A consulta lê páginas de 150 registros, continuando se os primeiros itens não tiverem foto; para quando há pelo menos 12 nomes distintos por perfil dessa ocasião, ao terminar a consulta ou após 10 páginas. Não exige novos índices. O cache local reutiliza esses dados por até 30 segundos, limitado pelo TTL restante, e continua conferindo invalidação a cada 5 segundos. A resposta inclui `Server-Timing` com tempos de autenticação e catálogo, sem credenciais. A preparação feita pelo jogo enquanto exibe a fala não usa Groq.

`peso_embalagem_kg` é extraído apenas de uma indicação explícita em gramas ou quilos na descrição (ex.: `700G`, `1KG`). Fica nulo quando o peso não está claro. O campo comercial `Volume (KG)` representa volume agregado e não é apresentado ao jogador nem enviado à IA.

A recuperação usa uma consulta limitada de até 150 registros da ocasião configurada para o cliente, com cache Redis compartilhado por 15 minutos, filtrando `disponivelNoJogo=true` e os indicadores SIM/NÃO sincronizados pelo painel admin. Consultas de igualdade aproveitam índices existentes. A seleção filtra os tipos e a ocasião definidos para cada cliente, remove duplicações por código/nome e sorteia três alimentos sem repetição. Não consome tokens Groq, não oferece uma nota antecipada nem expõe os dados comerciais. A avaliação final continua em `/v1/avaliacoes`, usando uma ficha pública com cache de até 60 segundos para conferir disponibilidade e avaliar a quantidade escolhida.


#### Parâmetros de seleção, sem IA

Os parâmetros estão em `PRODUCT_PARAMETERS`, no arquivo `src/recommendations.py`, e correspondem às necessidades dos roteiros oficiais. São regras pré-definidas; não representam estatísticas coletadas dos jogadores.

| Cliente | Ocasião obrigatória | Tipos permitidos |
|---|---|---|
| Lucas | Churrasco | Carnes, Aves |
| Marina | Praticidade | Carnes, Aves, Pescados, Acompanhamentos |
| Rafael | Dia a dia | Carnes |
| Camila | Praticidade | Carnes, Aves, Pescados, Acompanhamentos |
| André | Dia a dia | Carnes, Aves, Pescados, Acompanhamentos |

O servidor consulta uma única ocasião e filtra os tipos permitidos entre os produtos disponíveis com foto válida. Depois elimina nomes/códigos duplicados e usa `SystemRandom.sample` para sortear três alimentos distintos, com a mesma chance para cada alimento elegível.

O Redis guarda o catálogo de candidatos, e não o resultado do sorteio: novas consultas podem apresentar trios diferentes, inclusive com o cache aquecido. Um sorteio pode repetir um trio por acaso; se houver exatamente três produtos elegíveis, o conjunto será o mesmo. Se houver menos de três, o endpoint retorna `INSUFFICIENT_PRODUCTS`, sem completar as opções com alimentos fora dos parâmetros.

As categorias não confirmam preço, composição ou alergênicos. Restrições não especificadas no roteiro continuam sendo pontos para confirmar, e a quantidade fica a cargo do jogador. A Groq é chamada somente em `/v1/avaliacoes`, após a escolha. `/v1/recomendacoes` funciona sem configurar chaves Groq e continua validando o histórico contra o roteiro oficial.

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

## Cache Redis compartilhado

O Worker privado `epav-redis-cache`, neste mesmo repositório, conecta a API FastAPI ao Redis Cloud pelo cliente oficial Node Redis. A API usa um service binding Cloudflare; o cache não tem URL pública, rotas públicas nem CORS. `REDIS_PASSWORD` fica exclusivamente nos segredos desse Worker. O endpoint fornecido usa TCP sem TLS; `REDIS_TLS=true` permite usar um endpoint Redis com TLS quando habilitado pelo proprietário.

| Dados | Validade máxima |
|---|---:|
| Categorias e ocasiões do catálogo | 15 minutos |
| Ficha do produto para avaliação | 60 segundos |
| Top 20 do ranking público | 30 segundos |

Somente campos públicos necessários ao jogo são armazenados. Autenticação, tokens Firebase, chaves Groq, e-mails, margens, fornecedores, auditoria e transações de edição/remoção ficam fora do cache. O ranking compartilha apenas as colunas já públicas, sem UID ou e-mail. Resultados e falhas da IA não são armazenados.

Um lock Redis `SET NX EX` reúne consultas simultâneas da mesma chave entre instâncias. Cada lock tem um proprietário e é liberado por comparação atômica. As chaves têm namespace e versão próprios: nenhuma operação limpa outras aplicações do Redis. A expiração é automática; não há dependência de um job para remover dados antigos.

Falha do Redis aciona um circuito de 30 segundos e uma consulta direta ao Firebase com cache local curto. Erros não viram produtos ou pontuações fictícias. Um Redis vazio ainda precisa de uma primeira leitura bem-sucedida do Firebase: ele não recupera uma cota já esgotada. Alterações externas que não invalidem o cache aparecem após o TTL.

### Invalidação e integração

- `GET /v1/ranking` retorna `{resultados, cache_segundos}` sem login, como as regras públicas existentes do ranking.
- `POST /v1/cache/invalidate`: exige token Firebase de administrador, com claim conferida novamente, ou o segredo `CACHE_INVALIDATION_TOKEN` do bot. Troca a geração do cache; outras instâncias atualizam em até 5 segundos.
- `GET /v1/cache/status`: exige a mesma autorização e testa a conexão, sem exibir credenciais.
- O painel invalida depois de salvar um produto. O bot invalida ao terminar uma execução que mudou fotos, removeu ou restaurou registros, inclusive quando houve falha parcial.
- Se a invalidação falhar, a alteração continua salva e o TTL limita a defasagem. O ranking atualizado aparece em até 30 segundos após uma publicação.

Implantar o cache antes da API, na conta Cloudflare autorizada:

```sh
npm ci
npx wrangler deploy --config cache-worker/wrangler.jsonc --profile epav
npx wrangler secret put REDIS_PASSWORD --config cache-worker/wrangler.jsonc --profile epav
uv run pywrangler secret put CACHE_INVALIDATION_TOKEN --profile epav
uv run pywrangler deploy --profile epav
```

A chave de invalidação gerada deve ser configurada como segredo no GitHub Actions de `EPAV-GAME/epav-swift-images`. Nunca colocar a senha Redis ou essa chave no frontend, exemplos, arquivos versionados ou logs.

Testes adicionais: `node --test cache-worker/cache.test.mjs`. Os testes Python cobrem cache entre instâncias, consultas simultâneas, expiração, invalidação, falha do Redis, disponibilidade de produtos e exclusão de dados privados.
