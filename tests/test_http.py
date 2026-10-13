"""O servidor MCP pela camada HTTP real (Streamable HTTP), como o agente vai usar."""

from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
import respx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import AnyHttpUrl, SecretStr
from starlette.applications import Starlette

from bazar_agente.config import ConfigMCP
from bazar_agente.mcp_bazar.__main__ import criar_app
from tests.conftest import API_URL, CHAVES_URL, EMISSOR, TOKEN_URL, URL_MCP, EmitirToken

pytestmark = pytest.mark.anyio

PRODUTOS = [{"id": "cafe-especial", "nome": "Café especial 250 g", "preco": 42.90, "disponivel": 112}]


@pytest.fixture
async def app() -> AsyncIterator[Starlette]:
    config = ConfigMCP(
        api_url=AnyHttpUrl(API_URL),
        url_publica=AnyHttpUrl(URL_MCP),
        emissor=AnyHttpUrl(EMISSOR),
        cliente_segredo=SecretStr("segredo"),
        hosts_permitidos=["mcp.teste"],
    )
    aplicacao = criar_app(config)
    async with aplicacao.router.lifespan_context(aplicacao):
        yield aplicacao


def http(app: Starlette, **cabecalhos: str) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://mcp.teste", headers=cabecalhos
    )


INICIAR = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


async def test_sem_token_responde_401_apontando_onde_pedir_o_token(app: Starlette) -> None:
    async with http(app) as cliente:
        resposta = await cliente.post("/mcp", json=INICIAR, headers={"Accept": "application/json"})
    assert resposta.status_code == 401
    desafio = resposta.headers["WWW-Authenticate"]
    assert desafio.startswith("Bearer")
    assert "resource_metadata=" in desafio


async def test_metadados_oauth_dizem_qual_keycloak_emite_os_tokens(app: Starlette) -> None:
    async with http(app) as cliente:
        resposta = await cliente.get("/.well-known/oauth-protected-resource/mcp")
    assert resposta.status_code == 200
    metadados = resposta.json()
    assert metadados["resource"] == URL_MCP
    assert [s.rstrip("/") for s in metadados["authorization_servers"]] == [EMISSOR]


@respx.mock
async def test_token_de_outro_servico_e_recusado(
    app: Starlette, emitir_token: EmitirToken, jwks: dict[str, Any]
) -> None:
    respx.get(CHAVES_URL).respond(json=jwks)
    async with http(app, Authorization=f"Bearer {emitir_token(aud='bazar-api')}") as cliente:
        resposta = await cliente.post("/mcp", json=INICIAR, headers={"Accept": "application/json"})
    assert resposta.status_code == 401


@respx.mock
async def test_host_estranho_e_barrado_mesmo_com_token_valido(
    app: Starlette, emitir_token: EmitirToken, jwks: dict[str, Any]
) -> None:
    # DNS rebinding: um site malicioso faz o navegador da vítima chamar este
    # servidor por outro nome. Sem token, já cai no 401; com token, cai aqui.
    respx.get(CHAVES_URL).respond(json=jwks)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://atacante.teste",
        headers={"Authorization": f"Bearer {emitir_token()}"},
    ) as cliente:
        resposta = await cliente.post(
            "/mcp", json=INICIAR, headers={"Accept": "application/json, text/event-stream"}
        )
    assert resposta.status_code in (400, 403, 421)


@respx.mock
async def test_fluxo_completo_com_token_valido(
    app: Starlette, emitir_token: EmitirToken, jwks: dict[str, Any]
) -> None:
    respx.get(CHAVES_URL).respond(json=jwks)
    troca = respx.post(TOKEN_URL).respond(json={"access_token": "token-da-api", "expires_in": 300})
    bazar = respx.get(f"{API_URL}/api/produtos").respond(json=PRODUTOS)

    cabecalhos = {"Authorization": f"Bearer {emitir_token()}"}
    async with (
        http(app, **cabecalhos) as cliente_http,
        Client(streamable_http_client(f"{URL_MCP}", http_client=cliente_http)) as cliente,
    ):
        nomes = {f.name for f in (await cliente.list_tools()).tools}
        resultado = await cliente.call_tool("listar_produtos", {})

    assert "consultar_pedido" in nomes
    assert not resultado.is_error
    assert bazar.called
    assert not troca.called  # o catálogo é público: nenhuma troca de token à toa
