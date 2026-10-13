# 🤖 Bazar · Agente de atendimento

Agente de IA que atende os clientes do [Bazar](https://github.com/nicole21carvalho/bazar-microservices-kafka):
consulta pedidos, mostra o catálogo e cria pedidos, sempre em nome do cliente logado, por meio de um
servidor **MCP** (Model Context Protocol) próprio.

> Em construção. Pronto até agora: servidor MCP e agente (fases 1 e 2 de 5).

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
