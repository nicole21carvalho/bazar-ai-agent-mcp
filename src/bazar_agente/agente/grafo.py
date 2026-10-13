"""O agente como grafo do LangGraph: modelo → ferramentas → modelo, até a resposta.

    START → modelo ─(pediu ferramentas)→ ferramentas → modelo → ... → END
                   └(respondeu)──────────────────────────────────→ END

Toda ferramenta que muda algo (no Bazar: criar_pedido) para o grafo e espera o
cliente confirmar ("human in the loop"). O estado de cada conversa fica no
checkpointer: a conversa continua de onde parou, inclusive depois da pausa.
"""

from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from jsonschema import Draft202012Validator
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from bazar_agente.agente.ferramentas import DefinicaoFerramenta, Ferramentas
from bazar_agente.agente.protecoes import INSTRUCOES, proteger_dados

# Rodadas de ferramenta por mensagem do cliente: depois disso, o modelo responde
# sem ferramentas (nada de laço infinito gastando chamadas).
MAX_RODADAS = 5
# Mensagens recentes enviadas ao modelo (o histórico inteiro continua salvo).
JANELA = 30


class Estado(TypedDict):
    # Nos nós, o parâmetro se chama "state": é o nome que o protocolo do LangGraph tipa.
    mensagens: Annotated[list[AnyMessage], add_messages]
    rodadas: int


@dataclass(frozen=True)
class Contexto:
    """Dependências de uma execução: não vão para o checkpoint."""

    modelo: BaseChatModel
    ferramentas: Ferramentas
    definicoes: list[DefinicaoFerramenta]


async def chamar_modelo(state: Estado, *, runtime: Runtime[Contexto]) -> dict[str, Any]:
    ctx = runtime.context
    modelo: Any = ctx.modelo
    if state["rodadas"] < MAX_RODADAS:
        modelo = modelo.bind_tools([d.para_modelo() for d in ctx.definicoes])
    resposta = await modelo.ainvoke([SystemMessage(INSTRUCOES), *recortar(state["mensagens"], JANELA)])
    return {"mensagens": [resposta]}


def rotear(estado: Estado) -> Literal["ferramentas", "__end__"]:
    ultima = estado["mensagens"][-1]
    return "ferramentas" if isinstance(ultima, AIMessage) and ultima.tool_calls else "__end__"


async def executar_ferramentas(state: Estado, *, runtime: Runtime[Contexto]) -> dict[str, Any]:
    ctx = runtime.context
    ultima = state["mensagens"][-1]
    if not isinstance(ultima, AIMessage):  # o roteamento garante; aqui, só por segurança
        return {}
    por_nome = {d.nome: d for d in ctx.definicoes}

    # 1) Argumentos conferidos contra o esquema da própria ferramenta. Errado volta na
    # hora para o modelo corrigir: o cliente nunca confirma um pedido vazio ou inválido.
    problemas = {
        str(c["id"]): problema(por_nome.get(c["name"]), c["name"], c["args"]) for c in ultima.tool_calls
    }

    # 2) As confirmações vêm antes de qualquer execução. Ao retomar depois da pausa,
    # o LangGraph roda este nó de novo do início: nada pode ter sido executado antes.
    confirmadas: dict[str, bool] = {}
    for chamada in ultima.tool_calls:
        definicao = por_nome.get(chamada["name"])
        valida = problemas[str(chamada["id"])] is None
        if definicao is not None and not definicao.somente_leitura and valida:
            resposta = interrupt(
                {"ferramenta": chamada["name"], "argumentos": chamada["args"], "chamada_id": chamada["id"]}
            )
            confirmadas[str(chamada["id"])] = isinstance(resposta, dict) and resposta.get("aprovado") is True

    # 3) Executa (ou não) cada chamada e devolve o resultado como dado.
    mensagens: list[ToolMessage] = []
    for chamada in ultima.tool_calls:
        definicao = por_nome.get(chamada["name"])
        if (motivo := problemas[str(chamada["id"])]) is not None or definicao is None:
            conteudo, erro = motivo or "", True
        elif not definicao.somente_leitura and not confirmadas.get(str(chamada["id"])):
            conteudo = "O cliente NÃO confirmou: nada foi feito. Pergunte o que ele quer mudar."
            erro = False
        else:
            resultado = await ctx.ferramentas.chamar(chamada["name"], chamada["args"])
            conteudo, erro = proteger_dados(resultado.texto), resultado.erro
        mensagens.append(
            ToolMessage(
                content=conteudo,
                tool_call_id=str(chamada["id"]),
                name=chamada["name"],
                status="error" if erro else "success",
            )
        )
    return {"mensagens": mensagens, "rodadas": state["rodadas"] + 1}


def problema(definicao: DefinicaoFerramenta | None, nome: str, argumentos: dict[str, Any]) -> str | None:
    """Por que a chamada não pode ser executada, ou None se ela está certa."""
    if definicao is None:
        return f"A ferramenta {nome} não existe. Use só as ferramentas disponíveis."
    validador = Draft202012Validator(definicao.parametros, format_checker=Draft202012Validator.FORMAT_CHECKER)
    erros = sorted(validador.iter_errors(argumentos), key=lambda e: [str(p) for p in e.path])
    if not erros:
        return None
    detalhes = "; ".join(f"{'/'.join(map(str, e.path)) or 'argumentos'}: {e.message}" for e in erros[:3])
    return (
        f"Argumentos inválidos para {nome} ({detalhes}). Nada foi executado: corrija ou pergunte ao cliente."
    )


def recortar(mensagens: list[AnyMessage], limite: int) -> list[AnyMessage]:
    """As últimas mensagens, sem começar por um resultado de ferramenta órfão."""
    recentes = mensagens[-limite:]
    while recentes and isinstance(recentes[0], ToolMessage):
        recentes = recentes[1:]
    return recentes


def construir_grafo(checkpointer: BaseCheckpointSaver[Any]) -> CompiledStateGraph[Any, Contexto, Any, Any]:
    grafo = StateGraph(Estado, context_schema=Contexto)
    grafo.add_node("modelo", chamar_modelo)
    grafo.add_node("ferramentas", executar_ferramentas)
    grafo.add_edge(START, "modelo")
    grafo.add_conditional_edges("modelo", rotear, ["ferramentas", END])
    grafo.add_edge("ferramentas", "modelo")
    return grafo.compile(checkpointer=checkpointer, name="atendente-bazar")
