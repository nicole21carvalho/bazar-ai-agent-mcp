"""Servidor MCP do Bazar visto por um cliente MCP de verdade."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from mcp import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from pydantic import AnyHttpUrl

from bazar_agente.bazar.cliente import ClienteBazar
from bazar_agente.mcp_bazar.servidor import criar_servidor
from bazar_agente.seguranca.tokens import FalhaDeAutorizacao, VerificadorJWT
from tests.conftest import API_URL, CHAVES_URL, EMISSOR, URL_MCP

pytestmark = pytest.mark.anyio

PEDIDO = {
    "id": "8f7e3474-0000-4000-8000-000000000001",
    "status": "CONFIRMADO",
    "total": 375.60,
    "motivo": None,
    "itens": [{"produtoId": "cafe-especial", "quantidade": 2}],
    "criadoEm": "2026-10-13T12:00:00Z",
    "atualizadoEm": "2026-10-13T12:00:05Z",
}
PRODUTOS = [{"id": "cafe-especial", "nome": "Café especial 250 g", "preco": 42.90, "disponivel": 112}]


class TrocadorFalso:
    """Registra as trocas pedidas e devolve um token "da API" previsível."""

    def __init__(self, falhar: bool = False) -> None:
        self.pedidos: list[tuple[str, str]] = []
        self.falhar = falhar

    async def trocar(self, token: str, audiencia: str) -> str:
        self.pedidos.append((token, audiencia))
        if self.falhar:
            raise FalhaDeAutorizacao
        return f"api({token})"


def servidor_de_teste(trocador: TrocadorFalso) -> Any:
    return criar_servidor(
        cliente=ClienteBazar(httpx.AsyncClient(base_url=API_URL)),
        trocador=trocador,
        verificador=VerificadorJWT(httpx.AsyncClient(), CHAVES_URL, EMISSOR, "bazar-mcp", URL_MCP),
        emissor=AnyHttpUrl(EMISSOR),
        url_publica=AnyHttpUrl(URL_MCP),
        audiencia_api="bazar-api",
    )


@pytest.fixture
def trocador() -> TrocadorFalso:
    return TrocadorFalso()


@pytest.fixture
async def cliente_mcp(trocador: TrocadorFalso) -> AsyncIterator[Client]:
    # O middleware HTTP poria o usuário no contexto; no transporte em memória, pomos aqui.
    acesso = AccessToken(token="token-do-mcp", client_id="bazar-agente", scopes=[], subject="cliente-1")
    marca = auth_context_var.set(AuthenticatedUser(acesso))
    try:
        async with Client(servidor_de_teste(trocador)) as cliente:
            yield cliente
    finally:
        auth_context_var.reset(marca)


async def test_lista_as_quatro_ferramentas_e_marca_quais_so_leem(cliente_mcp: Client) -> None:
    ferramentas = {f.name: f for f in (await cliente_mcp.list_tools()).tools}
    assert set(ferramentas) == {"listar_produtos", "listar_meus_pedidos", "consultar_pedido", "criar_pedido"}
    for nome in ("listar_produtos", "listar_meus_pedidos", "consultar_pedido"):
        anotacoes = ferramentas[nome].annotations
        assert anotacoes is not None
        assert anotacoes.read_only_hint is True
    criar = ferramentas["criar_pedido"].annotations
    assert criar is not None
    assert criar.read_only_hint is False
    assert criar.idempotent_hint is False


async def test_catalogo_vem_estruturado(cliente_mcp: Client) -> None:
    with respx.mock(base_url=API_URL) as bazar:
        bazar.get("/api/produtos").respond(json=PRODUTOS)
        resultado = await cliente_mcp.call_tool("listar_produtos", {})
    assert not resultado.is_error
    assert resultado.structured_content is not None
    produto = resultado.structured_content["result"][0]
    assert produto["id"] == "cafe-especial"
    assert produto["disponivel"] == 112


async def test_consulta_troca_o_token_e_chama_o_bazar_em_nome_do_cliente(
    cliente_mcp: Client, trocador: TrocadorFalso
) -> None:
    with respx.mock(base_url=API_URL) as bazar:
        rota = bazar.get(f"/api/pedidos/{PEDIDO['id']}").respond(json=PEDIDO)
        resultado = await cliente_mcp.call_tool("consultar_pedido", {"pedido_id": PEDIDO["id"]})

    assert not resultado.is_error
    assert resultado.structured_content is not None
    assert resultado.structured_content["status"] == "CONFIRMADO"
    # Nunca o token recebido vai para o Bazar: só o trocado, com aud = bazar-api.
    assert trocador.pedidos == [("token-do-mcp", "bazar-api")]
    assert rota.calls.last.request.headers["Authorization"] == "Bearer api(token-do-mcp)"


async def test_pedido_de_outro_cliente_responde_que_nao_encontrou(cliente_mcp: Client) -> None:
    with respx.mock(base_url=API_URL) as bazar:
        bazar.get(url__regex=r"/api/pedidos/.+").respond(404)
        resultado = await cliente_mcp.call_tool(
            "consultar_pedido", {"pedido_id": "00000000-0000-4000-8000-000000000000"}
        )
    assert resultado.is_error
    assert "Não encontrei esse pedido" in str(resultado.content)


async def test_criar_pedido_envia_os_itens_no_formato_do_bazar(cliente_mcp: Client) -> None:
    with respx.mock(base_url=API_URL) as bazar:
        rota = bazar.post("/api/pedidos").respond(202, json={**PEDIDO, "status": "PENDENTE", "total": None})
        resultado = await cliente_mcp.call_tool(
            "criar_pedido", {"itens": [{"produto_id": "cafe-especial", "quantidade": 2}]}
        )
    assert not resultado.is_error
    assert rota.calls.last.request.read() == b'{"itens":[{"produtoId":"cafe-especial","quantidade":2}]}'


@pytest.mark.parametrize(
    "itens",
    [
        [],
        [{"produto_id": "cafe-especial", "quantidade": 0}],
        [{"produto_id": "cafe-especial", "quantidade": 100}],
        [{"produto_id": "../../admin", "quantidade": 1}],
        [{"produto_id": "cafe", "quantidade": 1}] * 21,
    ],
)
async def test_criar_pedido_invalido_e_barrado_antes_de_chegar_ao_bazar(
    cliente_mcp: Client, itens: list[dict[str, Any]]
) -> None:
    with respx.mock(base_url=API_URL, assert_all_called=False) as bazar:
        rota = bazar.post("/api/pedidos")
        resultado = await cliente_mcp.call_tool("criar_pedido", {"itens": itens})
    assert resultado.is_error
    assert not rota.called


async def test_limite_do_bazar_chega_como_mensagem_clara(cliente_mcp: Client) -> None:
    with respx.mock(base_url=API_URL) as bazar:
        bazar.get("/api/pedidos").respond(429, headers={"Retry-After": "30"})
        resultado = await cliente_mcp.call_tool("listar_meus_pedidos", {})
    assert resultado.is_error
    assert "Espere um pouco" in str(resultado.content)


async def test_troca_recusada_nao_chama_o_bazar() -> None:
    trocador = TrocadorFalso(falhar=True)
    acesso = AccessToken(token="token-do-mcp", client_id="bazar-agente", scopes=[], subject="cliente-1")
    marca = auth_context_var.set(AuthenticatedUser(acesso))
    try:
        with respx.mock(base_url=API_URL, assert_all_called=False) as bazar:
            rota = bazar.get("/api/pedidos")
            async with Client(servidor_de_teste(trocador)) as cliente:
                resultado = await cliente.call_tool("listar_meus_pedidos", {})
    finally:
        auth_context_var.reset(marca)
    assert resultado.is_error
    assert "Entre de novo" in str(resultado.content)
    assert not rota.called


async def test_sem_usuario_autenticado_nada_funciona() -> None:
    async with Client(servidor_de_teste(TrocadorFalso())) as cliente:
        resultado = await cliente.call_tool("listar_produtos", {})
    assert resultado.is_error
    assert "não autenticado" in str(resultado.content)


async def test_leitura_que_falha_na_rede_tenta_de_novo_uma_vez(cliente_mcp: Client) -> None:
    with respx.mock(base_url=API_URL) as bazar:
        bazar.get("/api/pedidos").mock(
            side_effect=[httpx.ReadTimeout("acordando"), httpx.Response(200, json=[])]
        )
        resultado = await cliente_mcp.call_tool("listar_meus_pedidos", {})
    assert not resultado.is_error


async def test_criacao_que_falha_na_rede_nao_repete_para_nao_duplicar_o_pedido(cliente_mcp: Client) -> None:
    with respx.mock(base_url=API_URL) as bazar:
        rota = bazar.post("/api/pedidos").mock(side_effect=httpx.ReadTimeout("lento"))
        resultado = await cliente_mcp.call_tool(
            "criar_pedido", {"itens": [{"produto_id": "cafe-especial", "quantidade": 1}]}
        )
    assert resultado.is_error
    assert rota.call_count == 1
