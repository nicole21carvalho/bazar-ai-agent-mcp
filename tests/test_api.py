"""API do agente pelo HTTP: autenticação, eventos SSE, confirmação e limites."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI
from langchain_core.messages import AIMessage

from bazar_agente.agente.api import criar_app
from bazar_agente.agente.ferramentas import Ferramentas
from bazar_agente.config import ConfigAgente
from bazar_agente.seguranca.tokens import FalhaDeAutorizacao, VerificadorJWT
from tests.conftest import CHAVES_URL, EMISSOR, EmitirToken
from tests.falsos import FerramentasFalsas, ModeloRoteirizado, chamada

pytestmark = pytest.mark.anyio


class Trocador:
    def __init__(self, falhar: bool = False) -> None:
        self.falhar = falhar
        self.tokens: list[str] = []

    async def trocar(self, token: str, audiencia: str) -> str:
        if self.falhar:
            raise FalhaDeAutorizacao
        self.tokens.append(audiencia)
        return f"mcp({token[-6:]})"


class Ambiente:
    def __init__(self, roteiro: list[AIMessage], *, por_minuto: int = 10, falhar_troca: bool = False) -> None:
        self.modelo = ModeloRoteirizado(roteiro=roteiro)
        self.ferramentas = FerramentasFalsas({"consultar_pedido": {"status": "CONFIRMADO"}})
        self.trocador = Trocador(falhar_troca)
        self.tokens_mcp: list[str] = []

        @asynccontextmanager
        async def abrir(token: str) -> AsyncIterator[Ferramentas]:
            self.tokens_mcp.append(token)
            yield self.ferramentas

        self.app: FastAPI = criar_app(
            ConfigAgente(emissor=EMISSOR, mensagens_por_minuto=por_minuto),
            modelo=self.modelo,
            verificador=VerificadorJWT(
                httpx.AsyncClient(), CHAVES_URL, EMISSOR, "bazar-agente", "bazar-agente"
            ),
            trocador=self.trocador,
            abrir_ferramentas=abrir,
        )

    def http(self, token: str | None) -> httpx.AsyncClient:
        cabecalhos = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://agente.teste", headers=cabecalhos
        )


def eventos(corpo: str) -> list[tuple[str, Any]]:
    """Lê o formato SSE: blocos "event: x / data: {...}" separados por linha em branco."""
    lidos = []
    for bloco in corpo.strip().split("\n\n"):
        campos = dict(linha.split(": ", 1) for linha in bloco.splitlines() if ": " in linha)
        if "event" in campos:
            lidos.append((campos["event"], json.loads(campos.get("data", "null"))))
    return lidos


@pytest.fixture
def token(emitir_token: EmitirToken, jwks: dict[str, Any]) -> str:
    respx.get(CHAVES_URL).respond(json=jwks)
    return emitir_token(aud="bazar-agente")


@respx.mock
async def test_sem_login_nao_conversa() -> None:
    amb = Ambiente([AIMessage("oi")])
    async with amb.http(None) as cliente:
        resposta = await cliente.post(f"/api/conversas/{uuid4()}/mensagens", json={"texto": "oi"})
    assert resposta.status_code == 401
    assert resposta.headers["WWW-Authenticate"] == "Bearer"


@respx.mock
async def test_token_do_painel_para_outro_servico_e_recusado(
    emitir_token: EmitirToken, jwks: dict[str, Any]
) -> None:
    respx.get(CHAVES_URL).respond(json=jwks)
    amb = Ambiente([AIMessage("oi")])
    async with amb.http(emitir_token(aud="bazar-api")) as cliente:
        resposta = await cliente.post(f"/api/conversas/{uuid4()}/mensagens", json={"texto": "oi"})
    assert resposta.status_code == 401


@respx.mock
async def test_conversa_em_tempo_real_com_ferramenta(token: str) -> None:
    amb = Ambiente(
        [chamada("consultar_pedido", {"pedido_id": "8f7e"}), AIMessage("Seu pedido está confirmado.")]
    )
    async with amb.http(token) as cliente:
        resposta = await cliente.post(
            f"/api/conversas/{uuid4()}/mensagens", json={"texto": "Cadê meu pedido 8f7e?"}
        )
    assert resposta.status_code == 200
    assert resposta.headers["content-type"].startswith("text/event-stream")
    lidos = eventos(resposta.text)
    assert ("ferramenta", {"nome": "consultar_pedido"}) in lidos
    assert "".join(d["texto"] for e, d in lidos if e == "texto") == "Seu pedido está confirmado."
    assert lidos[-1][0] == "fim"
    # O agente trocou o token do cliente por um só do servidor MCP antes de abrir a sessão.
    assert amb.trocador.tokens == ["bazar-mcp"]
    assert amb.tokens_mcp[0].startswith("mcp(")


@respx.mock
async def test_compra_pede_confirmacao_e_so_cria_depois_do_sim(token: str) -> None:
    itens = [{"produto_id": "cafe-especial", "quantidade": 2}]
    amb = Ambiente([chamada("criar_pedido", {"itens": itens}), AIMessage("Pedido criado!")])
    conversa = uuid4()
    async with amb.http(token) as cliente:
        primeira = await cliente.post(f"/api/conversas/{conversa}/mensagens", json={"texto": "2 cafés"})
        pausa = dict(eventos(primeira.text))["confirmacao"]
        assert pausa["ferramenta"] == "criar_pedido"
        assert amb.ferramentas.chamadas == []

        # Enquanto a confirmação espera, uma nova mensagem não passa por cima dela.
        outra = await cliente.post(f"/api/conversas/{conversa}/mensagens", json={"texto": "e aí?"})
        assert outra.status_code == 409

        segunda = await cliente.post(f"/api/conversas/{conversa}/confirmacao", json={"aprovado": True})
    assert amb.ferramentas.chamadas == [("criar_pedido", {"itens": itens})]
    assert "Pedido criado!" in segunda.text


@respx.mock
async def test_confirmar_sem_nada_pendente_e_recusado(token: str) -> None:
    amb = Ambiente([AIMessage("oi")])
    async with amb.http(token) as cliente:
        resposta = await cliente.post(f"/api/conversas/{uuid4()}/confirmacao", json={"aprovado": True})
    assert resposta.status_code == 409


@respx.mock
async def test_ninguem_confirma_a_compra_de_outro_cliente(
    emitir_token: EmitirToken, jwks: dict[str, Any]
) -> None:
    respx.get(CHAVES_URL).respond(json=jwks)
    amb = Ambiente([chamada("criar_pedido", {"itens": []}), AIMessage("ok")])
    conversa = uuid4()
    async with amb.http(emitir_token(aud="bazar-agente", sub="ana")) as ana:
        await ana.post(f"/api/conversas/{conversa}/mensagens", json={"texto": "compra"})
    async with amb.http(emitir_token(aud="bazar-agente", sub="intruso")) as intruso:
        resposta = await intruso.post(f"/api/conversas/{conversa}/confirmacao", json={"aprovado": True})
    assert resposta.status_code == 409  # para o intruso, essa conversa nem existe
    assert amb.ferramentas.chamadas == []


@respx.mock
async def test_mensagem_vazia_ou_gigante_e_recusada(token: str) -> None:
    amb = Ambiente([AIMessage("oi")])
    async with amb.http(token) as cliente:
        vazia = await cliente.post(f"/api/conversas/{uuid4()}/mensagens", json={"texto": " ​ "})
        gigante = await cliente.post(f"/api/conversas/{uuid4()}/mensagens", json={"texto": "a" * 2001})
    assert vazia.status_code == 422
    assert gigante.status_code == 422


@respx.mock
async def test_limite_de_mensagens_por_minuto(token: str) -> None:
    amb = Ambiente([AIMessage(str(i)) for i in range(5)], por_minuto=3)
    async with amb.http(token) as cliente:
        codigos = [
            (await cliente.post(f"/api/conversas/{uuid4()}/mensagens", json={"texto": "oi"})).status_code
            for _ in range(5)
        ]
        bloqueada = await cliente.post(f"/api/conversas/{uuid4()}/mensagens", json={"texto": "oi"})
    assert codigos == [200, 200, 200, 429, 429]
    assert int(bloqueada.headers["Retry-After"]) > 0


@respx.mock
async def test_sessao_expirada_vira_evento_de_erro_e_nao_trava_a_conversa(
    emitir_token: EmitirToken, jwks: dict[str, Any]
) -> None:
    respx.get(CHAVES_URL).respond(json=jwks)
    amb = Ambiente([AIMessage("oi")], falhar_troca=True)
    conversa = uuid4()
    async with amb.http(emitir_token(aud="bazar-agente")) as cliente:
        primeira = await cliente.post(f"/api/conversas/{conversa}/mensagens", json={"texto": "oi"})
        segunda = await cliente.post(f"/api/conversas/{conversa}/mensagens", json={"texto": "oi"})
    assert ("erro", {"mensagem": "Sua sessão expirou. Entre de novo no Bazar."}) in eventos(primeira.text)
    assert segunda.status_code == 200  # a conversa não ficou presa como "em andamento"


@respx.mock
async def test_etapa_que_passa_do_tempo_vira_aviso_e_nao_trava_a_conversa(token: str) -> None:
    import anyio
    from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
    from langchain_core.outputs import ChatResult

    class ModeloLento(ModeloRoteirizado):
        async def _agenerate(
            self,
            messages: Any,
            stop: Any = None,
            run_manager: AsyncCallbackManagerForLLMRun | None = None,
            **kw: Any,
        ) -> ChatResult:
            await anyio.sleep(5)
            return self._generate(messages)

    amb = Ambiente([AIMessage("tarde demais")])
    amb.app = criar_app(
        ConfigAgente(emissor=EMISSOR, tempo_maximo_etapa_s=0.2),
        modelo=ModeloLento(roteiro=[AIMessage("tarde demais"), AIMessage("agora sim")]),
        verificador=VerificadorJWT(httpx.AsyncClient(), CHAVES_URL, EMISSOR, "bazar-agente", "bazar-agente"),
        trocador=amb.trocador,
        abrir_ferramentas=lambda token: _sessao_vazia(amb),
    )
    conversa = uuid4()
    async with amb.http(token) as cliente:
        resposta = await cliente.post(f"/api/conversas/{conversa}/mensagens", json={"texto": "oi"})
    assert ("erro", {"mensagem": "Demorei demais para responder. Tente de novo."}) in eventos(resposta.text)


@asynccontextmanager
async def _sessao_vazia(amb: Ambiente) -> AsyncIterator[Ferramentas]:
    yield amb.ferramentas
