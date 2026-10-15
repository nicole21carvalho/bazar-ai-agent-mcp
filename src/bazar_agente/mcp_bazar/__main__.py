"""Sobe o servidor MCP do Bazar por Streamable HTTP: `python -m bazar_agente.mcp_bazar`."""

import contextlib
import logging
from collections.abc import AsyncIterator

import httpx
import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from bazar_agente.bazar.cliente import ClienteBazar
from bazar_agente.config import ConfigMCP
from bazar_agente.mcp_bazar.servidor import criar_servidor
from bazar_agente.observabilidade import configurar_rastreamento
from bazar_agente.seguranca.tokens import TrocaDeToken, VerificadorJWT

TIMEOUT = httpx.Timeout(10.0, connect=3.0)


def criar_app(config: ConfigMCP) -> Starlette:
    http_bazar = httpx.AsyncClient(base_url=str(config.api_url), timeout=TIMEOUT)
    http_keycloak = httpx.AsyncClient(timeout=TIMEOUT)
    servidor = criar_servidor(
        cliente=ClienteBazar(http_bazar),
        trocador=TrocaDeToken(http_keycloak, config.token_url, config.cliente_id, config.cliente_segredo),
        verificador=VerificadorJWT(
            http_keycloak,
            chaves_url=config.chaves_url,
            emissor=str(config.emissor).rstrip("/"),
            audiencia=config.audiencia,
            recurso=str(config.url_publica),
        ),
        emissor=config.emissor,
        url_publica=config.url_publica,
        audiencia_api=config.audiencia_api,
    )
    # Sem estado entre requisições: qualquer réplica atende qualquer chamada.
    app = servidor.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=config.hosts_permitidos
        ),
    )

    @contextlib.asynccontextmanager
    async def ciclo_de_vida(app: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as pilha:
            pilha.push_async_callback(http_bazar.aclose)
            pilha.push_async_callback(http_keycloak.aclose)
            async with app_original(app):
                yield

    app_original = app.router.lifespan_context
    app.router.lifespan_context = ciclo_de_vida
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = ConfigMCP()
    if not config.cliente_segredo.get_secret_value():
        raise SystemExit("Defina BAZAR_MCP_CLIENTE_SEGREDO (segredo do cliente bazar-mcp no Keycloak).")
    # O próprio SDK do MCP cria o span de cada chamada (continuando o trace do agente);
    # daqui, as chamadas ao Bazar levam o "traceparent" adiante.
    configurar_rastreamento("bazar-mcp")
    uvicorn.run(criar_app(config), host="0.0.0.0", port=8090, proxy_headers=True)  # noqa: S104 (container)


if __name__ == "__main__":
    main()
