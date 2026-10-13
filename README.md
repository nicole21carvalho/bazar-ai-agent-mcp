# 🤖 Bazar · Agente de atendimento

Agente de IA que atende os clientes do [Bazar](https://github.com/nicole21carvalho/bazar-microservices-kafka):
consulta pedidos, mostra o catálogo e cria pedidos, sempre em nome do cliente logado, por meio de um
servidor **MCP** (Model Context Protocol) próprio.

> Em construção. Pronto até agora: o servidor MCP do Bazar (fase 1 de 5).

## Servidor MCP

- Quatro ferramentas: `listar_produtos`, `listar_meus_pedidos`, `consultar_pedido` e `criar_pedido`,
  com anotações de quais só leem.
- **Resource server OAuth** (especificação de autorização do MCP): só aceita tokens do Keycloak emitidos
  para ele, publica os metadados RFC 9728 e recusa tokens de outros serviços.
- **Sem repassar o token do cliente**: antes de chamar o Bazar, troca o token por outro, válido só para a
  API (OAuth Token Exchange, RFC 8693). Quem decide o que o cliente pode ver é o próprio Bazar.
- Entradas validadas antes de qualquer chamada, com as mesmas regras do Bazar.

```bash
uv sync
uv run pytest          # 29 testes
uv run ruff check . && uv run mypy src tests
```
