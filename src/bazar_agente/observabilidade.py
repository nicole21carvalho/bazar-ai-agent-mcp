"""Rastreamento distribuído (OpenTelemetry): um trace por mensagem do cliente.

    API do agente ─ invoke_agent ─┬─ chat qwen3:4b-instruct                (o modelo pensou)
                                  ├─ execute_tool consultar_pedido
                                  │    └─ MCP → servidor MCP ─ tools/call → gateway do Bazar → serviços
                                  └─ chat qwen3:4b-instruct                (o modelo respondeu)

O SDK do MCP já cria os spans das chamadas MCP e leva o contexto do trace dentro da
própria mensagem (`_meta`); o servidor MCP passa o contexto ao Bazar pelo cabeçalho
`traceparent`, que o gateway (Spring) entende. Assim, o Jaeger do Bazar mostra a
conversa inteira, do modelo de linguagem até a saga no Kafka.

Liga só quando `OTEL_EXPORTER_OTLP_ENDPOINT` está definido. O conteúdo da conversa
nunca vai para o trace: só nomes, contagens, tempos e erros. Texto do cliente,
respostas do modelo, argumentos de ferramenta e tokens de acesso ficam de fora.
"""

import os
from typing import Any

from langchain_core.messages import AIMessage
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span

rastreador = trace.get_tracer("bazar_agente")


def configurar_rastreamento(servico: str) -> TracerProvider | None:
    """Exporta os spans por OTLP/HTTP, se houver para onde. Devolve o provedor criado."""
    if not (
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    ):
        return None
    recurso = Resource.create(
        {"service.name": os.environ.get("OTEL_SERVICE_NAME", servico), "service.namespace": "bazar"}
    )
    provedor = TracerProvider(resource=recurso)
    provedor.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provedor)
    # Chamadas HTTP (Keycloak, Bazar, Ollama) viram spans e levam o "traceparent".
    HTTPXClientInstrumentor().instrument()
    return provedor


def atributos_do_modelo(modelo: Any) -> dict[str, str]:
    """Provedor e nome do modelo, nos nomes das convenções de GenAI do OpenTelemetry."""
    # Os modelos do LangChain descrevem a si mesmos (é o que o LangSmith usa).
    parametros = modelo._get_ls_params() if hasattr(modelo, "_get_ls_params") else {}
    return {
        "gen_ai.provider.name": str(parametros.get("ls_provider") or "desconhecido"),
        "gen_ai.request.model": str(
            parametros.get("ls_model_name") or getattr(modelo, "model", "desconhecido")
        ),
    }


def anotar_resposta(span: Span, resposta: AIMessage) -> None:
    """Tokens gastos, motivo de parada e quantas ferramentas o modelo pediu. Nada do texto."""
    uso = resposta.usage_metadata
    if uso:
        span.set_attribute("gen_ai.usage.input_tokens", uso["input_tokens"])
        span.set_attribute("gen_ai.usage.output_tokens", uso["output_tokens"])
    meta = resposta.response_metadata
    motivo = meta.get("done_reason") or meta.get("stop_reason") or meta.get("finish_reason")
    if motivo:
        span.set_attribute("gen_ai.response.finish_reasons", [str(motivo)])
    span.set_attribute("bazar.ferramentas_pedidas", len(resposta.tool_calls))
