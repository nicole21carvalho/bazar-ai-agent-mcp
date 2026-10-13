"""Agente + servidor MCP de verdade (em memória) + Bazar simulado: as peças juntas."""

from collections.abc import AsyncIterator

import pytest
import respx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from mcp import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from bazar_agente.agente.ferramentas import FerramentasMCP
from bazar_agente.agente.grafo import Contexto, construir_grafo
from tests.conftest import API_URL
from tests.falsos import ModeloRoteirizado, chamada
from tests.test_servidor_mcp import PEDIDO, PRODUTOS, TrocadorFalso, servidor_de_teste

pytestmark = pytest.mark.anyio


@pytest.fixture
async def ferramentas() -> AsyncIterator[FerramentasMCP]:
    acesso = AccessToken(token="token-do-mcp", client_id="bazar-agente", scopes=[], subject="cliente-1")
    marca = auth_context_var.set(AuthenticatedUser(acesso))
    try:
        async with Client(servidor_de_teste(TrocadorFalso())) as cliente:
            yield FerramentasMCP(cliente)
    finally:
        auth_context_var.reset(marca)


async def test_as_definicoes_vem_do_servidor_com_o_que_so_le(ferramentas: FerramentasMCP) -> None:
    por_nome = {d.nome: d for d in await ferramentas.definicoes()}
    assert por_nome["consultar_pedido"].somente_leitura
    assert not por_nome["criar_pedido"].somente_leitura
    esquema = por_nome["consultar_pedido"].parametros
    assert esquema["properties"]["pedido_id"]["format"] == "uuid"
    # O esquema vai para o modelo no formato de "function calling".
    assert por_nome["criar_pedido"].para_modelo()["function"]["name"] == "criar_pedido"


async def test_pergunta_vira_chamada_mcp_que_vira_resposta(ferramentas: FerramentasMCP) -> None:
    modelo = ModeloRoteirizado(
        roteiro=[chamada("consultar_pedido", {"pedido_id": PEDIDO["id"]}), AIMessage("Confirmado!")]
    )
    ctx = Contexto(modelo=modelo, ferramentas=ferramentas, definicoes=await ferramentas.definicoes())
    grafo = construir_grafo(InMemorySaver())
    with respx.mock(base_url=API_URL) as bazar:
        bazar.get(f"/api/pedidos/{PEDIDO['id']}").respond(json=PEDIDO)
        final = await grafo.ainvoke(
            {"mensagens": [HumanMessage("Cadê meu pedido?")], "rodadas": 0},
            {"configurable": {"thread_id": "cliente-1:a"}},
            context=ctx,
        )
    resultado = next(m for m in final["mensagens"] if isinstance(m, ToolMessage))
    assert resultado.status == "success"
    assert '"CONFIRMADO"' in str(resultado.content)
    assert final["mensagens"][-1].content == "Confirmado!"


async def test_argumento_errado_do_modelo_volta_como_erro_e_o_agente_segue(
    ferramentas: FerramentasMCP,
) -> None:
    modelo = ModeloRoteirizado(
        roteiro=[
            chamada("criar_pedido", {"itens": [{"produto_id": "cafe-especial", "quantidade": 0}]}),
            AIMessage("A quantidade precisa ser de pelo menos 1."),
        ]
    )
    ctx = Contexto(modelo=modelo, ferramentas=ferramentas, definicoes=await ferramentas.definicoes())
    grafo = construir_grafo(InMemorySaver())
    cfg: RunnableConfig = {"configurable": {"thread_id": "cliente-1:b"}}
    with respx.mock(base_url=API_URL, assert_all_called=False) as bazar:
        rota = bazar.post("/api/pedidos")
        final = await grafo.ainvoke(
            {"mensagens": [HumanMessage("Quero café")], "rodadas": 0}, cfg, context=ctx
        )
    # O esquema real do servidor MCP (quantidade >= 1) barra antes de pedir confirmação.
    assert "__interrupt__" not in final
    erro = next(m for m in final["mensagens"] if isinstance(m, ToolMessage))
    assert erro.status == "error"
    assert not rota.called
    assert final["mensagens"][-1].content == "A quantidade precisa ser de pelo menos 1."


async def test_catalogo_real_passa_pela_protecao_de_dados(ferramentas: FerramentasMCP) -> None:
    modelo = ModeloRoteirizado(roteiro=[chamada("listar_produtos", {}), AIMessage("Temos café.")])
    ctx = Contexto(modelo=modelo, ferramentas=ferramentas, definicoes=await ferramentas.definicoes())
    with respx.mock(base_url=API_URL) as bazar:
        bazar.get("/api/produtos").respond(json=PRODUTOS)
        await construir_grafo(InMemorySaver()).ainvoke(
            {"mensagens": [HumanMessage("O que tem?")], "rodadas": 0},
            {"configurable": {"thread_id": "cliente-1:c"}},
            context=ctx,
        )
    dado = str(next(m for m in modelo.recebidas[1] if isinstance(m, ToolMessage)).content)
    assert dado.startswith("<dados_da_ferramenta>")
    assert "Café especial 250 g" in dado


async def test_sessao_mcp_que_cai_vira_erro_para_o_modelo_e_nao_derruba_a_conversa() -> None:
    from mcp import MCPError

    class ClienteQueCai:
        async def call_tool(self, nome: str, argumentos: dict[str, object]) -> None:
            raise MCPError(code=-32001, message="Unauthorized")

    resultado = await FerramentasMCP(ClienteQueCai()).chamar("listar_meus_pedidos", {})  # type: ignore[arg-type]
    assert resultado.erro
    assert "tentar de novo" in resultado.texto
