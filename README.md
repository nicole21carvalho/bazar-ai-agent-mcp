# 🤖 Bazar · Agente de atendimento

Agente de IA que atende os clientes do [Bazar](https://github.com/nicole21carvalho/bazar-microservices-kafka):
consulta pedidos, mostra o catálogo e cria pedidos, sempre em nome do cliente logado, por meio de um
servidor **MCP** (Model Context Protocol) próprio.

> Em construção. Pronto até agora: servidor MCP, agente e integração com o Bazar (fases 1 a 3 de 5).

## Como as peças se encaixam

```
Painel do Bazar ──login PKCE──> Keycloak        token: aud = bazar-api, bazar-agente
   │ chat (SSE)
   ▼
Agente (LangGraph) ──troca de token──> Keycloak  token: aud = bazar-mcp
   │ MCP (Streamable HTTP)
   ▼
Servidor MCP do Bazar ──troca de token──> Keycloak  token: aud = bazar-api
   │ REST
   ▼
API do Bazar (gateway → pedidos, estoque)
```

Cada elo só aceita o token emitido para ele, e cada troca gera um token novo, válido só para o próximo
serviço, sempre em nome do mesmo cliente (`sub`). O job `keycloak-config` monta essa cadeia no Keycloak do
Bazar na subida, sem mexer no arquivo de realm dele (e pode rodar de novo: só cria o que falta).

## Rodando junto com o Bazar

```bash
# 1. No repositório do Bazar, com AGENTE_URL=http://localhost:8000 no .env dele:
docker compose up -d --build

# 2. Aqui (gere os dois segredos com: openssl rand -hex 24):
cp .env.example .env
docker compose up -d --build
```

Abra o painel em http://localhost:5173, entre e clique em **💬 Atendimento**.

## Agente (LangGraph)

```
START → modelo ─(pediu ferramentas)→ ferramentas → modelo → ... → END
               └(respondeu)──────────────────────────────────→ END
```

- **Confirmação humana**: toda ferramenta que muda algo (no Bazar, `criar_pedido`) pausa o grafo e espera o
  "sim" do cliente. Resposta ambígua conta como "não"; ferramenta sem a anotação "só leitura" é tratada como
  ação. Ao retomar, nenhuma leitura se repete.
- **Proteção contra prompt injection**: o que vem das ferramentas entra marcado como dado
  (`<dados_da_ferramenta>`), frases com cara de ordem são neutralizadas e um dado não consegue fechar o bloco
  antes da hora. Mas a proteção principal é a arquitetura: as ferramentas agem em nome do cliente, e o Bazar só
  devolve o que é dele.
- **Memória por conversa**, presa ao cliente do token: ninguém continua (nem confirma) a conversa de outra pessoa.
- **API em tempo real** (FastAPI, SSE nativo): eventos `ferramenta`, `texto`, `confirmacao`, `erro` e `fim`.
  Limite de mensagens por minuto, tamanho máximo e Unicode invisível removido.
- **Modelo de linguagem trocável**: Ollama (local, gratuito, padrão) ou Claude (`uv sync --extra anthropic`).

## Servidor MCP

- Quatro ferramentas: `listar_produtos`, `listar_meus_pedidos`, `consultar_pedido` e `criar_pedido`,
  com anotações de quais só leem.
- **Resource server OAuth** (especificação de autorização do MCP): só aceita tokens do Keycloak emitidos
  para ele, publica os metadados RFC 9728 e recusa tokens de outros serviços.
- **Sem repassar o token do cliente**: antes de chamar o Bazar, troca o token por outro, válido só para a
  API (OAuth Token Exchange, RFC 8693). O agente faz o mesmo para falar com o servidor MCP.
- Entradas validadas antes de qualquer chamada, com as mesmas regras do Bazar.

```bash
uv sync --all-extras
uv run pytest          # 53 testes, com um modelo "roteirizado" (sem IA de verdade)
uv run ruff check . && uv run mypy src tests
```
