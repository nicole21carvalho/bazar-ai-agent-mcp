"""API do agente: o painel do Bazar conversa com o atendente em tempo real (SSE).

    POST /api/conversas/{id}/mensagens    {"texto": "..."}     → eventos
    POST /api/conversas/{id}/confirmacao  {"aprovado": true}    → eventos

Eventos: "ferramenta" (o agente está consultando algo), "texto" (pedaços da
resposta), "confirmacao" (uma ação espera o "sim" do cliente), "erro" e "fim".

O cliente se identifica com o token do Keycloak (o mesmo login do painel). A
conversa fica presa a ele: o id da thread inclui o "sub" do token, então ninguém
continua a conversa de outra pessoa, nem sabendo o id.
"""

import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.sse import EventSourceResponse, ServerSentEvent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from mcp.server.auth.provider import AccessToken, TokenVerifier
from pydantic import BaseModel, Field

from bazar_agente.agente.ferramentas import Ferramentas
from bazar_agente.agente.grafo import Contexto, construir_grafo
from bazar_agente.agente.protecoes import limpar_mensagem
from bazar_agente.config import ConfigAgente
from bazar_agente.mcp_bazar.servidor import Trocador
from bazar_agente.seguranca.tokens import FalhaDeAutorizacao

log = logging.getLogger(__name__)

AbrirFerramentas = Callable[[str], AbstractAsyncContextManager[Ferramentas]]


class NovaMensagem(BaseModel):
    texto: str = Field(min_length=1, max_length=10_000)


class Confirmacao(BaseModel):
    aprovado: bool


class LimiteDeUso:
    """Janela deslizante de um minuto por cliente (em memória, por réplica)."""

    def __init__(self, por_minuto: int) -> None:
        self._por_minuto = por_minuto
        self._uso: dict[str, deque[float]] = {}

    def consumir(self, cliente: str) -> int | None:
        """Registra um uso; se passou do limite, devolve quantos segundos esperar."""
        agora = time.monotonic()
        janela = self._uso.setdefault(cliente, deque())
        while janela and agora - janela[0] >= 60:
            janela.popleft()
        if len(janela) >= self._por_minuto:
            return int(60 - (agora - janela[0])) + 1
        janela.append(agora)
        return None


def criar_app(
    config: ConfigAgente,
    *,
    modelo: BaseChatModel,
    verificador: TokenVerifier,
    trocador: Trocador,
    abrir_ferramentas: AbrirFerramentas,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> FastAPI:
    grafo = construir_grafo(checkpointer or InMemorySaver())
    limite = LimiteDeUso(config.mensagens_por_minuto)
    em_andamento: set[str] = set()

    app = FastAPI(title="Bazar · Agente de atendimento", version="0.1.0", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.origens_cors,
        allow_methods=["POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    async def cliente_atual(authorization: Annotated[str | None, Header()] = None) -> AccessToken:
        token = (authorization or "").removeprefix("Bearer ").strip()
        acesso = await verificador.verify_token(token) if token else None
        if acesso is None or not acesso.subject:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Entre no Bazar para conversar.", {"WWW-Authenticate": "Bearer"}
            )
        return acesso

    Cliente = Annotated[AccessToken, Depends(cliente_atual)]

    async def checar(acesso: AccessToken, conversa_id: UUID, *, nova_mensagem: bool) -> RunnableConfig:
        """Checagens antes de abrir o stream: depois que ele começa, o status 200 já foi."""
        cfg: RunnableConfig = {"configurable": {"thread_id": f"{acesso.subject}:{conversa_id}"}}
        if cfg["configurable"]["thread_id"] in em_andamento:
            raise HTTPException(status.HTTP_409_CONFLICT, "Espere a resposta anterior terminar.")
        pendente = bool((await grafo.aget_state(cfg)).interrupts)
        if nova_mensagem and pendente:
            raise HTTPException(status.HTTP_409_CONFLICT, "Responda primeiro à confirmação pendente.")
        if not nova_mensagem and not pendente:
            raise HTTPException(status.HTTP_409_CONFLICT, "Não há nada esperando confirmação.")
        espera = limite.consumir(str(acesso.subject))
        if espera is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, "Muitas mensagens seguidas.", {"Retry-After": str(espera)}
            )
        return cfg

    async def para_mensagem(conversa_id: UUID, acesso: Cliente) -> RunnableConfig:
        return await checar(acesso, conversa_id, nova_mensagem=True)

    async def para_confirmacao(conversa_id: UUID, acesso: Cliente) -> RunnableConfig:
        return await checar(acesso, conversa_id, nova_mensagem=False)

    def texto_valido(corpo: NovaMensagem) -> str:
        texto = limpar_mensagem(corpo.texto)
        if not texto or len(texto) > config.tamanho_maximo_mensagem:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"A mensagem precisa ter de 1 a {config.tamanho_maximo_mensagem} caracteres.",
            )
        return texto

    async def transmitir(
        entrada: dict[str, Any] | Command[Any], cfg: RunnableConfig, acesso: AccessToken
    ) -> AsyncIterator[ServerSentEvent]:
        chave = str(cfg["configurable"]["thread_id"])
        # A reserva fica aqui dentro (e não na checagem) para nunca "vazar": se a
        # requisição falhar antes do stream, a conversa não fica travada.
        if chave in em_andamento:
            yield ServerSentEvent(event="erro", data={"mensagem": "Espere a resposta anterior terminar."})
            return
        em_andamento.add(chave)
        try:
            token_mcp = await trocador.trocar(acesso.token, config.audiencia_mcp)
            async with abrir_ferramentas(token_mcp) as ferramentas:
                ctx = Contexto(
                    modelo=modelo, ferramentas=ferramentas, definicoes=await ferramentas.definicoes()
                )
                async for modo, dado in grafo.astream(
                    entrada, cfg, context=ctx, stream_mode=["messages", "updates"]
                ):
                    for evento in _eventos(modo, dado):
                        yield evento
        except FalhaDeAutorizacao:
            yield ServerSentEvent(
                event="erro", data={"mensagem": "Sua sessão expirou. Entre de novo no Bazar."}
            )
        except Exception as erro:
            # Nada do conteúdo da conversa no log: só o tipo do erro.
            log.exception("falha no atendimento: %s", type(erro).__name__)
            yield ServerSentEvent(event="erro", data={"mensagem": "O atendente está indisponível agora."})
        finally:
            em_andamento.discard(chave)
        yield ServerSentEvent(event="fim", data={})

    @app.post("/api/conversas/{conversa_id}/mensagens", response_class=EventSourceResponse)
    async def enviar_mensagem(
        texto: Annotated[str, Depends(texto_valido)],
        cfg: Annotated[RunnableConfig, Depends(para_mensagem)],
        acesso: Cliente,
    ) -> AsyncIterator[ServerSentEvent]:
        async for evento in transmitir({"mensagens": [HumanMessage(texto)], "rodadas": 0}, cfg, acesso):
            yield evento

    @app.post("/api/conversas/{conversa_id}/confirmacao", response_class=EventSourceResponse)
    async def confirmar(
        corpo: Confirmacao,
        cfg: Annotated[RunnableConfig, Depends(para_confirmacao)],
        acesso: Cliente,
    ) -> AsyncIterator[ServerSentEvent]:
        async for evento in transmitir(Command(resume={"aprovado": corpo.aprovado}), cfg, acesso):
            yield evento

    @app.get("/saude")
    async def saude() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _eventos(modo: str, dado: Any) -> list[ServerSentEvent]:
    if modo == "messages":
        pedaco, meta = dado
        texto = pedaco.text if isinstance(pedaco, AIMessageChunk | AIMessage) else ""
        if meta.get("langgraph_node") == "modelo" and texto:
            return [ServerSentEvent(event="texto", data={"texto": texto})]
        return []
    eventos: list[ServerSentEvent] = []
    for no, atualizacao in dado.items():
        if no == "__interrupt__":
            eventos += [ServerSentEvent(event="confirmacao", data=pausa.value) for pausa in atualizacao]
        elif no == "modelo":
            for mensagem in atualizacao.get("mensagens", []):
                for chamada in getattr(mensagem, "tool_calls", []):
                    eventos.append(ServerSentEvent(event="ferramenta", data={"nome": chamada["name"]}))
    return eventos
