"""Configura o Keycloak do Bazar para o agente: `python -m bazar_agente.keycloak_config`.

Roda uma vez na subida (e pode rodar de novo à vontade: só cria o que falta e
acerta o que mudou). O realm do Bazar continua funcionando sozinho, sem o agente.

    Painel ──login──> token (aud: bazar-api, bazar-agente)
    Agente   troca ──> token (aud: bazar-mcp)    [cliente bazar-agente]
    MCP      troca ──> token (aud: bazar-api)    [cliente bazar-mcp]

Na troca de token padrão do Keycloak (RFC 8693), quem pede é um cliente
confidencial, o token recebido precisa ter esse cliente na audiência e o alvo
precisa ser um cliente: por isso existe o "bazar-api", que só representa a API.
"""

import asyncio
import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

ESCOPOS_PADRAO = ["basic", "email", "profile"]  # email e email_verified: o Bazar exige os dois


def _audiencia(cliente: str) -> dict[str, Any]:
    return {
        "name": f"audiencia-{cliente}",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.client.audience": cliente,
            "access.token.claim": "true",
            "id.token.claim": "false",
            "introspection.token.claim": "true",
        },
    }


def _sem_fluxos(client_id: str, nome: str) -> dict[str, Any]:
    # Nenhum login direto por este cliente: nem navegador, nem senha, nem conta de serviço.
    return {
        "clientId": client_id,
        "name": nome,
        "enabled": True,
        "publicClient": False,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
    }


def clientes_desejados(segredo_agente: str, segredo_mcp: str) -> list[tuple[dict[str, Any], list[str]]]:
    """Cada cliente com as audiências que ele pode pedir numa troca."""

    def trocador(client_id: str, nome: str, segredo: str) -> dict[str, Any]:
        return {
            **_sem_fluxos(client_id, nome),
            "clientAuthenticatorType": "client-secret",
            "secret": segredo,
            # Tokens trocados valem 15 min (o do painel, 5): um turno do agente com um
            # modelo lento pode levar minutos entre a troca e a chamada da ferramenta.
            # Continuam presos a uma audiência e só circulam na rede interna.
            "attributes": {"standard.token.exchange.enabled": "true", "access.token.lifespan": "900"},
        }

    return [
        (_sem_fluxos("bazar-api", "API do Bazar (alvo das trocas de token)"), []),
        (trocador("bazar-mcp", "Servidor MCP do Bazar", segredo_mcp), ["bazar-api"]),
        (trocador("bazar-agente", "Agente de atendimento do Bazar", segredo_agente), ["bazar-mcp"]),
    ]


class Configurador:
    def __init__(self, http: httpx.AsyncClient, realm: str) -> None:
        # O AsyncClient já vem com base_url do Keycloak e o token de admin.
        self._http = http
        self._realm = f"/admin/realms/{realm}"
        self.alteracoes: list[str] = []

    async def aplicar(self, segredo_agente: str, segredo_mcp: str) -> list[str]:
        for representacao, audiencias in clientes_desejados(segredo_agente, segredo_mcp):
            id_ = await self._garantir_cliente(representacao)
            for audiencia in audiencias:
                await self._garantir_mapeador(id_, representacao["clientId"], _audiencia(audiencia))
            if representacao.get("secret"):
                await self._garantir_escopos(id_, representacao["clientId"])
        # O token que o painel recebe no login passa a valer também para o agente.
        painel = await self._buscar("bazar-web")
        if painel is None:
            raise RuntimeError("Cliente bazar-web não encontrado: o realm do Bazar foi importado?")
        await self._garantir_mapeador(painel["id"], "bazar-web", _audiencia("bazar-agente"))
        return self.alteracoes

    async def _buscar(self, client_id: str) -> dict[str, Any] | None:
        resposta = await self._http.get(f"{self._realm}/clients", params={"clientId": client_id})
        resposta.raise_for_status()
        encontrados: list[dict[str, Any]] = resposta.json()
        return encontrados[0] if encontrados else None

    async def _garantir_cliente(self, desejado: dict[str, Any]) -> str:
        nome = desejado["clientId"]
        atual = await self._buscar(nome)
        if atual is None:
            (await self._http.post(f"{self._realm}/clients", json=desejado)).raise_for_status()
            self.alteracoes.append(f"cliente {nome} criado")
            criado = await self._buscar(nome)
            if criado is None:
                raise RuntimeError(f"O Keycloak não devolveu o cliente {nome} recém-criado.")
            return str(criado["id"])

        atributos_atuais = atual.get("attributes", {})
        divergente = sorted(
            [k for k, v in desejado.items() if k not in ("secret", "attributes") and atual.get(k) != v]
            + [k for k, v in desejado.get("attributes", {}).items() if atributos_atuais.get(k) != v]
        )
        # O segredo nem sempre volta no GET: ele é reaplicado a cada execução.
        if divergente or "secret" in desejado:
            corpo = {
                **atual,
                **desejado,
                "attributes": {**atributos_atuais, **desejado.get("attributes", {})},
            }
            (await self._http.put(f"{self._realm}/clients/{atual['id']}", json=corpo)).raise_for_status()
        if divergente:
            self.alteracoes.append(f"cliente {nome} ajustado ({', '.join(divergente)})")
        return str(atual["id"])

    async def _garantir_mapeador(self, id_: str, cliente: str, mapeador: dict[str, Any]) -> None:
        caminho = f"{self._realm}/clients/{id_}/protocol-mappers/models"
        resposta = await self._http.get(caminho)
        resposta.raise_for_status()
        if any(m.get("name") == mapeador["name"] for m in resposta.json()):
            return
        (await self._http.post(caminho, json=mapeador)).raise_for_status()
        self.alteracoes.append(f"{cliente}: {mapeador['name']}")

    async def _garantir_escopos(self, id_: str, cliente: str) -> None:
        resposta = await self._http.get(f"{self._realm}/clients/{id_}/default-client-scopes")
        resposta.raise_for_status()
        presentes = {e["name"] for e in resposta.json()}
        faltando = [e for e in ESCOPOS_PADRAO if e not in presentes]
        if not faltando:
            return
        todos = await self._http.get(f"{self._realm}/client-scopes")
        todos.raise_for_status()
        por_nome = {e["name"]: e["id"] for e in todos.json()}
        for escopo in faltando:
            caminho = f"{self._realm}/clients/{id_}/default-client-scopes/{por_nome[escopo]}"
            (await self._http.put(caminho)).raise_for_status()
            self.alteracoes.append(f"{cliente}: escopo {escopo}")


async def token_de_admin(http: httpx.AsyncClient, usuario: str, senha: str) -> str:
    resposta = await http.post(
        "/realms/master/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": "admin-cli", "username": usuario, "password": senha},
    )
    resposta.raise_for_status()
    return str(resposta.json()["access_token"])


async def configurar(
    url: str, realm: str, usuario: str, senha: str, segredo_agente: str, segredo_mcp: str
) -> None:
    async with httpx.AsyncClient(base_url=url, timeout=30.0) as http:
        for tentativa in range(60):  # o Keycloak demora para subir
            try:
                token = await token_de_admin(http, usuario, senha)
                break
            except httpx.HTTPError:
                if tentativa == 59:
                    raise
                await asyncio.sleep(5)
        http.headers["Authorization"] = f"Bearer {token}"
        alteracoes = await Configurador(http, realm).aplicar(segredo_agente, segredo_mcp)
    log.info("Keycloak configurado: %s", "; ".join(alteracoes) or "nada a mudar")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    obrigatorias = [
        "KEYCLOAK_URL",
        "KC_SENHA_ADMIN",
        "BAZAR_AGENTE_CLIENTE_SEGREDO",
        "BAZAR_MCP_CLIENTE_SEGREDO",
    ]
    if faltando := [v for v in obrigatorias if not os.environ.get(v)]:
        raise SystemExit(f"Defina: {', '.join(faltando)}")
    asyncio.run(
        configurar(
            os.environ["KEYCLOAK_URL"],
            os.environ.get("KEYCLOAK_REALM", "bazar"),
            os.environ.get("KC_ADMIN", "admin"),
            os.environ["KC_SENHA_ADMIN"],
            os.environ["BAZAR_AGENTE_CLIENTE_SEGREDO"],
            os.environ["BAZAR_MCP_CLIENTE_SEGREDO"],
        )
    )


if __name__ == "__main__":
    main()
