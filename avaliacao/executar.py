"""Roda os cenários no agente de verdade (grafo, validação, servidor MCP) com um modelo.

Tempo e tokens saem dos mesmos spans do rastreamento (OpenTelemetry) que vão para
o Jaeger em produção: a avaliação também prova que a instrumentação funciona.
"""

import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from avaliacao.cenarios import Cenario, Turno
from avaliacao.loja import BazarSimulado, abrir_loja
from avaliacao.verificar import UUID, Ocorrido, conferir
from bazar_agente.agente.grafo import Contexto, construir_grafo

# Por etapa do grafo (uma chamada ao modelo). Num runner do CI sem GPU, sobra.
TEMPO_MAXIMO_ETAPA_S = 600


@dataclass
class ResultadoTurno:
    cliente: str
    ferramentas: list[str]
    confirmacao: dict[str, Any] | None
    resposta: str
    falhas: list[str]
    chamadas_ao_modelo: int = 0
    tokens_entrada: int = 0
    tokens_saida: int = 0
    segundos: float = 0.0


@dataclass
class ResultadoCenario:
    nome: str
    categoria: str
    descricao: str
    turnos: list[ResultadoTurno] = field(default_factory=list)
    falhas: list[str] = field(default_factory=list)  # as do cenário (erro, turno que não rodou)

    @property
    def passou(self) -> bool:
        return not self.falhas and all(not t.falhas for t in self.turnos)


async def avaliar_cenario(
    cenario: Cenario, modelo: BaseChatModel, exportador: InMemorySpanExporter
) -> ResultadoCenario:
    resultado = ResultadoCenario(cenario.nome, cenario.categoria, cenario.descricao)
    bazar = BazarSimulado()
    grafo = construir_grafo(InMemorySaver())
    grafo.step_timeout = TEMPO_MAXIMO_ETAPA_S
    cfg: RunnableConfig = {"configurable": {"thread_id": cenario.nome}}
    citados: set[str] = set()

    async with abrir_loja(bazar) as ferramentas:
        ctx = Contexto(modelo=modelo, ferramentas=ferramentas, definicoes=await ferramentas.definicoes())
        for numero, turno in enumerate(cenario.turnos, start=1):
            if turno.cliente is not None:
                citados |= {u.lower() for u in UUID.findall(turno.cliente)}
            try:
                feito = await _rodar_turno(grafo, cfg, ctx, turno, bazar, citados, exportador)
            except Exception as erro:
                resultado.falhas.append(f"turno {numero}: erro {type(erro).__name__}: {erro}")
                break
            resultado.turnos.append(feito)
            # O próximo turno só faz sentido se o estado bate: uma resposta ("confirmar")
            # precisa de confirmação pendente, e uma mensagem nova não pode ter uma.
            proximo = cenario.turnos[numero] if numero < len(cenario.turnos) else None
            if proximo is not None and (feito.confirmacao is not None) != (proximo.confirmar is not None):
                resultado.falhas.append(f"os turnos depois do {numero} não rodaram")
                break
    return resultado


async def _rodar_turno(
    grafo: Any,
    cfg: RunnableConfig,
    ctx: Contexto,
    turno: Turno,
    bazar: BazarSimulado,
    citados: set[str],
    exportador: InMemorySpanExporter,
) -> ResultadoTurno:
    antes = len((await grafo.aget_state(cfg)).values.get("mensagens", []))
    criados_antes = len(bazar.criados)
    entrada: Any = (
        {"mensagens": [HumanMessage(turno.cliente)], "rodadas": 0}
        if turno.cliente is not None
        else Command(resume={"aprovado": turno.confirmar})
    )
    exportador.clear()
    inicio = time.monotonic()
    await grafo.ainvoke(entrada, cfg, context=ctx)
    segundos = time.monotonic() - inicio

    estado = await grafo.aget_state(cfg)
    novas = [m for m in estado.values["mensagens"][antes:] if isinstance(m, AIMessage)]
    chamadas = [(c["name"], dict(c["args"])) for m in novas for c in m.tool_calls]
    pausa = estado.interrupts[0].value if estado.interrupts else None
    ocorrido = Ocorrido(
        chamadas=chamadas,
        confirmacao=pausa["argumentos"] if isinstance(pausa, dict) else None,
        resposta="\n".join(m.text for m in novas if m.text).strip(),
        pedidos_criados=len(bazar.criados) - criados_antes,
        produtos=bazar.ids_de_produto,
        pedidos_conhecidos=set(bazar.pedidos) | citados,
    )

    chats = [s for s in exportador.get_finished_spans() if s.name.startswith("chat ")]
    atributos = [s.attributes or {} for s in chats]
    return ResultadoTurno(
        cliente=turno.cliente
        if turno.cliente is not None
        else ("(confirmou)" if turno.confirmar else "(recusou)"),
        ferramentas=[nome for nome, _ in chamadas],
        confirmacao=ocorrido.confirmacao,
        resposta=ocorrido.resposta,
        falhas=conferir(turno, ocorrido),
        chamadas_ao_modelo=len(chats),
        tokens_entrada=sum(_inteiro(a.get("gen_ai.usage.input_tokens")) for a in atributos),
        tokens_saida=sum(_inteiro(a.get("gen_ai.usage.output_tokens")) for a in atributos),
        segundos=round(segundos, 2),
    )


def _inteiro(valor: object) -> int:
    return valor if isinstance(valor, int) else 0


async def avaliar(
    cenarios: list[Cenario], modelo: BaseChatModel, exportador: InMemorySpanExporter
) -> list[ResultadoCenario]:
    return [await avaliar_cenario(c, modelo, exportador) for c in cenarios]
