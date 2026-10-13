"""Configurador do Keycloak contra uma API de administração simulada (com estado)."""

import json
import re
import uuid
from typing import Any

import httpx
import pytest

from bazar_agente.keycloak_config import Configurador

pytestmark = pytest.mark.anyio

ESCOPOS = {"basic": "e-basic", "email": "e-email", "profile": "e-profile", "roles": "e-roles"}


class KeycloakFalso:
    """O pedaço da API de administração que o configurador usa."""

    def __init__(self) -> None:
        painel = {"id": "id-web", "clientId": "bazar-web", "publicClient": True, "attributes": {}}
        self.clientes: dict[str, dict[str, Any]] = {"id-web": painel}
        self.mapeadores: dict[str, list[dict[str, Any]]] = {"id-web": [{"name": "audiencia-bazar-api"}]}
        self.escopos: dict[str, set[str]] = {"id-web": {"basic", "email"}}
        self.escritas: list[str] = []

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        caminho, metodo = pedido.url.path.removeprefix("/admin/realms/bazar"), pedido.method
        if metodo != "GET":
            self.escritas.append(f"{metodo} {caminho}")
        if caminho == "/clients" and metodo == "GET":
            alvo = pedido.url.params["clientId"]
            return httpx.Response(200, json=[c for c in self.clientes.values() if c["clientId"] == alvo])
        if caminho == "/clients" and metodo == "POST":
            corpo = json.loads(pedido.content)
            id_ = str(uuid.uuid4())
            self.clientes[id_] = {**corpo, "id": id_}
            self.mapeadores[id_] = list(corpo.get("protocolMappers", []))
            self.escopos[id_] = {"roles"}  # o Keycloak põe os escopos padrão do realm
            return httpx.Response(201)
        if m := re.fullmatch(r"/clients/([^/]+)", caminho):
            self.clientes[m[1]] = {k: v for k, v in json.loads(pedido.content).items() if k != "secret"}
            return httpx.Response(204)
        if m := re.fullmatch(r"/clients/([^/]+)/protocol-mappers/models", caminho):
            if metodo == "GET":
                return httpx.Response(200, json=self.mapeadores[m[1]])
            self.mapeadores[m[1]].append(json.loads(pedido.content))
            return httpx.Response(201)
        if m := re.fullmatch(r"/clients/([^/]+)/default-client-scopes", caminho):
            return httpx.Response(200, json=[{"name": n} for n in self.escopos[m[1]]])
        if m := re.fullmatch(r"/clients/([^/]+)/default-client-scopes/([^/]+)", caminho):
            nome = next(n for n, i in ESCOPOS.items() if i == m[2])
            self.escopos[m[1]].add(nome)
            return httpx.Response(204)
        if caminho == "/client-scopes":
            return httpx.Response(200, json=[{"name": n, "id": i} for n, i in ESCOPOS.items()])
        return httpx.Response(404)

    def cliente(self, client_id: str) -> tuple[str, dict[str, Any]]:
        return next((i, c) for i, c in self.clientes.items() if c["clientId"] == client_id)


async def aplicar(keycloak: KeycloakFalso) -> list[str]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(keycloak), base_url="http://kc") as http:
        return await Configurador(http, "bazar").aplicar("segredo-agente", "segredo-mcp")


async def test_primeira_execucao_monta_a_cadeia_de_troca_de_token() -> None:
    keycloak = KeycloakFalso()
    alteracoes = await aplicar(keycloak)
    assert "cliente bazar-agente criado" in alteracoes

    _, agente = keycloak.cliente("bazar-agente")
    assert agente["publicClient"] is False
    assert agente["attributes"]["standard.token.exchange.enabled"] == "true"
    assert not agente["standardFlowEnabled"]
    assert not agente["directAccessGrantsEnabled"]

    def audiencias(client_id: str) -> set[str]:
        id_, _ = keycloak.cliente(client_id)
        return {m["config"]["included.client.audience"] for m in keycloak.mapeadores[id_] if "config" in m}

    # Cada um só pode pedir o próximo elo da cadeia.
    assert audiencias("bazar-agente") == {"bazar-mcp"}
    assert audiencias("bazar-mcp") == {"bazar-api"}
    assert audiencias("bazar-web") == {"bazar-agente"}  # o login do painel passa a valer para o agente
    # O alvo final existe como cliente, sem nenhum login habilitado.
    _, api = keycloak.cliente("bazar-api")
    assert not any(
        api[f] for f in ("standardFlowEnabled", "directAccessGrantsEnabled", "serviceAccountsEnabled")
    )
    # E-mail no token trocado: o Bazar exige email e email_verified.
    id_mcp, _ = keycloak.cliente("bazar-mcp")
    assert {"basic", "email", "profile"} <= keycloak.escopos[id_mcp]


async def test_segunda_execucao_nao_muda_nada() -> None:
    keycloak = KeycloakFalso()
    await aplicar(keycloak)
    keycloak.escritas.clear()

    alteracoes = await aplicar(keycloak)

    assert alteracoes == []
    # Só o segredo é reaplicado (o GET do Keycloak nem sempre o devolve).
    assert all(e.startswith("PUT /clients/") and e.count("/") == 2 for e in keycloak.escritas)


async def test_configuracao_mexida_a_mao_volta_ao_certo() -> None:
    keycloak = KeycloakFalso()
    await aplicar(keycloak)
    id_, agente = keycloak.cliente("bazar-agente")
    agente["directAccessGrantsEnabled"] = True  # alguém ligou login por senha no console
    agente["attributes"]["standard.token.exchange.enabled"] = "false"

    alteracoes = await aplicar(keycloak)

    assert keycloak.clientes[id_]["directAccessGrantsEnabled"] is False
    assert keycloak.clientes[id_]["attributes"]["standard.token.exchange.enabled"] == "true"
    assert any("bazar-agente ajustado" in a for a in alteracoes)


async def test_sem_o_realm_do_bazar_avisa_em_vez_de_inventar() -> None:
    keycloak = KeycloakFalso()
    keycloak.clientes.pop("id-web")
    with pytest.raises(RuntimeError, match="bazar-web"):
        await aplicar(keycloak)
