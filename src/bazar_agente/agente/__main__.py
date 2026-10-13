"""Sobe a API do agente: `python -m bazar_agente.agente`."""

import logging
from functools import partial

import httpx
import uvicorn
from fastapi import FastAPI

from bazar_agente.agente.api import criar_app
from bazar_agente.agente.ferramentas import sessao_mcp
from bazar_agente.agente.modelos import criar_modelo
from bazar_agente.config import ConfigAgente
from bazar_agente.seguranca.tokens import TrocaDeToken, VerificadorJWT


def montar(config: ConfigAgente) -> FastAPI:
    http_keycloak = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))
    return criar_app(
        config,
        modelo=criar_modelo(config),
        verificador=VerificadorJWT(
            http_keycloak,
            chaves_url=config.chaves_url,
            emissor=str(config.emissor).rstrip("/"),
            audiencia=config.audiencia,
            recurso=config.audiencia,
        ),
        trocador=TrocaDeToken(http_keycloak, config.token_url, config.cliente_id, config.cliente_segredo),
        abrir_ferramentas=partial(sessao_mcp, str(config.mcp_url)),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = ConfigAgente()
    if not config.cliente_segredo.get_secret_value():
        raise SystemExit("Defina BAZAR_AGENTE_CLIENTE_SEGREDO (segredo do cliente bazar-agente no Keycloak).")
    uvicorn.run(montar(config), host="0.0.0.0", port=8000, proxy_headers=True)  # noqa: S104 (container)


if __name__ == "__main__":
    main()
