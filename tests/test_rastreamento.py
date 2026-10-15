"""Rastreamento (OpenTelemetry): o que vira span, o trace atravessando o MCP e o que nunca vai."""

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import pytest
import respx
from langchain_core.messages import AIMessage, HumanMessage
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from mcp import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from bazar_agente.agente.ferramentas import FerramentasMCP
from bazar_agente.agente.grafo import Contexto, construir_grafo
from bazar_agente.observabilidade import atributos_do_modelo
from tests.conftest import API_URL, CHAVES_URL, EmitirToken
from tests.falsos import DEFINICOES, FerramentasFalsas, ModeloRoteirizado, chamada
from tests.test_api import Ambiente, eventos
from tests.test_servidor_mcp import PEDIDO, TrocadorFalso, servidor_de_teste

pytestmark = pytest.mark.anyio


def conteudo_dos_spans(spans: Sequence[ReadableSpan]) -> str:
    """Tudo o que um span leva para fora: nome, atributos e eventos."""
    partes: list[str] = []
    for span in spans:
        partes.append(span.name)
        partes += [f"{k}={v!s}" for k, v in (span.attributes or {}).items()]
        for evento in span.events:
            partes += [evento.name, *(f"{k}={v!s}" for k, v in (evento.attributes or {}).items())]
    return "\n".join(partes).lower()


def por_operacao(spans: Sequence[ReadableSpan], operacao: str) -> list[ReadableSpan]:
    return [s for s in spans if (s.attributes or {}).get("gen_ai.operation.name") == operacao]


def ancestrais(span: ReadableSpan, spans: Sequence[ReadableSpan]) -> list[ReadableSpan]:
    """Pai, avô, ... até a raiz (entre os spans gravados)."""
    por_id = {s.context.span_id: s for s in spans if s.context is not None}
    linha: list[ReadableSpan] = []
    while span.parent is not None and span.parent.span_id in por_id:
        span = por_id[span.parent.span_id]
        linha.append(span)
    return linha


async def test_modelo_e_ferramenta_viram_spans_sem_o_conteudo_da_conversa(
    spans: InMemorySpanExporter,
) -> None:
    pensou = AIMessage(
        "",
        tool_calls=[
            {"name": "consultar_pedido", "args": {"pedido_id": PEDIDO["id"]}, "id": "c1", "type": "tool_call"}
        ],
        usage_metadata={"input_tokens": 812, "output_tokens": 23, "total_tokens": 835},
        response_metadata={"done_reason": "stop"},
    )
    modelo = ModeloRoteirizado(roteiro=[pensou, AIMessage("Seu pedido está confirmado, Ana.")])
    ferramentas = FerramentasFalsas({"consultar_pedido": {"status": "CONFIRMADO", "cliente": "Ana Souza"}})
    await construir_grafo(InMemorySaver()).ainvoke(
        {
            "mensagens": [HumanMessage(f"Meu CPF é 123.456.789-00. Cadê o pedido {PEDIDO['id']}?")],
            "rodadas": 0,
        },
        {"configurable": {"thread_id": "t1"}},
        context=Contexto(modelo=modelo, ferramentas=ferramentas, definicoes=DEFINICOES),
    )

    lista = spans.get_finished_spans()
    chats = por_operacao(lista, "chat")
    assert len(chats) == 2
    primeiro = chats[0].attributes or {}
    assert primeiro["gen_ai.usage.input_tokens"] == 812
    assert primeiro["gen_ai.usage.output_tokens"] == 23
    assert primeiro["gen_ai.response.finish_reasons"] == ("stop",)
    assert primeiro["bazar.ferramentas_pedidas"] == 1

    (ferramenta,) = por_operacao(lista, "execute_tool")
    assert ferramenta.name == "execute_tool consultar_pedido"
    assert (ferramenta.attributes or {})["bazar.ferramenta.desfecho"] == "executada"
    assert (ferramenta.attributes or {})["bazar.ferramenta.somente_leitura"] is True

    # Nem a mensagem do cliente, nem os argumentos, nem o resultado, nem a resposta.
    tudo = conteudo_dos_spans(lista)
    for dado in ("123.456.789-00", str(PEDIDO["id"]), "ana souza", "confirmado", "cadê"):
        assert dado not in tudo


async def test_argumento_invalido_e_compra_recusada_ficam_marcados_no_trace(
    spans: InMemorySpanExporter,
) -> None:
    modelo = ModeloRoteirizado(
        roteiro=[
            chamada("consultar_pedido", {"pedido_id": "nao-e-uuid"}, id_="c1"),
            chamada("criar_pedido", {"itens": [{"produto_id": "cafe-especial", "quantidade": 1}]}, id_="c2"),
            AIMessage("Tudo bem, não comprei."),
        ]
    )
    ctx = Contexto(modelo=modelo, ferramentas=FerramentasFalsas(), definicoes=DEFINICOES)
    grafo = construir_grafo(InMemorySaver())
    cfg: Any = {"configurable": {"thread_id": "t2"}}
    await grafo.ainvoke({"mensagens": [HumanMessage("Quero café")], "rodadas": 0}, cfg, context=ctx)
    await grafo.ainvoke(Command(resume={"aprovado": False}), cfg, context=ctx)

    ferramentas = {s.name: s for s in por_operacao(spans.get_finished_spans(), "execute_tool")}
    invalida = ferramentas["execute_tool consultar_pedido"]
    assert invalida.status.status_code == StatusCode.ERROR
    assert (invalida.attributes or {})["error.type"] == "argumentos_invalidos"
    recusada = ferramentas["execute_tool criar_pedido"]
    assert (recusada.attributes or {})["bazar.ferramenta.desfecho"] == "nao_confirmada"
    assert recusada.status.status_code != StatusCode.ERROR  # o cliente dizer "não" não é erro


def test_modelo_do_ollama_se_descreve_nas_convencoes_de_genai() -> None:
    assert atributos_do_modelo(ChatOllama(model="qwen3:4b")) == {
        "gen_ai.provider.name": "ollama",
        "gen_ai.request.model": "qwen3:4b",
    }
    assert atributos_do_modelo(object())["gen_ai.provider.name"] == "desconhecido"


async def test_um_trace_so_do_agente_ate_o_bazar_passando_pelo_mcp(spans: InMemorySpanExporter) -> None:
    acesso = AccessToken(token="token-do-mcp", client_id="bazar-agente", scopes=[], subject="cliente-1")
    marca = auth_context_var.set(AuthenticatedUser(acesso))
    instrumentador = HTTPXClientInstrumentor()
    instrumentador.instrument()
    try:
        async with Client(servidor_de_teste(TrocadorFalso())) as cliente:
            ferramentas = FerramentasMCP(cliente)
            modelo = ModeloRoteirizado(
                roteiro=[chamada("consultar_pedido", {"pedido_id": PEDIDO["id"]}), AIMessage("Confirmado!")]
            )
            ctx = Contexto(modelo=modelo, ferramentas=ferramentas, definicoes=await ferramentas.definicoes())
            with respx.mock(base_url=API_URL) as bazar:
                rota = bazar.get(f"/api/pedidos/{PEDIDO['id']}").respond(json=PEDIDO)
                await construir_grafo(InMemorySaver()).ainvoke(
                    {"mensagens": [HumanMessage("Cadê meu pedido?")], "rodadas": 0},
                    {"configurable": {"thread_id": "t3"}},
                    context=ctx,
                )
    finally:
        instrumentador.uninstrument()
        auth_context_var.reset(marca)

    lista = spans.get_finished_spans()
    (agente,) = [s for s in lista if s.name == "execute_tool consultar_pedido"]
    (servidor,) = [s for s in lista if s.name == "tools/call consultar_pedido"]
    (bazar_http,) = [s for s in lista if s.name == "GET"]
    # O servidor MCP roda em outra tarefa: o contexto chegou dentro da mensagem MCP (_meta).
    assert agente.context is not None
    assert ancestrais(servidor, lista)[-1] is agente
    assert ancestrais(bazar_http, lista)[:1] == [servidor]
    # E o servidor MCP passa o trace ao gateway do Bazar pelo cabeçalho W3C.
    traceparent = rota.calls.last.request.headers["traceparent"]
    assert traceparent.split("-")[1] == f"{agente.context.trace_id:032x}"


@respx.mock
async def test_api_abre_um_span_por_mensagem_sem_identificar_o_cliente(
    spans: InMemorySpanExporter, emitir_token: EmitirToken, jwks: dict[str, Any]
) -> None:
    respx.get(CHAVES_URL).respond(json=jwks)
    token = emitir_token(aud="bazar-agente", sub="sub-da-ana-4242")
    amb = Ambiente([chamada("criar_pedido", {"itens": [{"produto_id": "cafe-especial", "quantidade": 2}]})])
    conversa = uuid4()
    async with amb.http(token) as cliente:
        resposta = await cliente.post(f"/api/conversas/{conversa}/mensagens", json={"texto": "Quero 2 cafés"})
    assert any(nome == "confirmacao" for nome, _ in eventos(resposta.text))

    lista = spans.get_finished_spans()
    (agente,) = por_operacao(lista, "invoke_agent")
    atributos = agente.attributes or {}
    assert atributos["gen_ai.agent.name"] == "atendente-bazar"
    assert atributos["gen_ai.conversation.id"] == str(conversa)
    assert atributos["bazar.aguardando_confirmacao"] is True
    assert agente.context is not None
    # O modelo roda dentro do span da mensagem.
    (chat,) = por_operacao(lista, "chat")
    assert chat.parent is not None
    assert chat.parent.span_id == agente.context.span_id
    assert "sub-da-ana-4242" not in conteudo_dos_spans(lista)
    assert token[-20:].lower() not in conteudo_dos_spans(lista)


async def test_sem_endpoint_configurado_o_rastreamento_fica_desligado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bazar_agente.observabilidade import configurar_rastreamento

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert configurar_rastreamento("bazar-agente") is None
