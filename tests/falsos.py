"""Dublês dos testes: um modelo que segue roteiro e ferramentas em memória."""

import json
from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from bazar_agente.agente.ferramentas import DefinicaoFerramenta, Resultado


class ModeloRoteirizado(BaseChatModel):
    """Devolve as respostas do roteiro, em ordem, e guarda o que recebeu a cada chamada."""

    roteiro: list[AIMessage]
    _recebidas: list[list[BaseMessage]] = PrivateAttr(default_factory=list)
    _com_ferramentas: list[bool] = PrivateAttr(default_factory=list)
    _vinculadas: bool = PrivateAttr(default=False)

    @property
    def _llm_type(self) -> str:
        return "roteirizado"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ModeloRoteirizado:
        vinculado = self.model_copy()
        vinculado._recebidas = self._recebidas
        vinculado._com_ferramentas = self._com_ferramentas
        vinculado._vinculadas = True
        vinculado.__dict__["roteiro"] = self.roteiro  # mesma lista: o roteiro anda para os dois
        return vinculado

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._recebidas.append(list(messages))
        self._com_ferramentas.append(self._vinculadas)
        resposta = self.roteiro.pop(0) if self.roteiro else AIMessage("(fim do roteiro)")
        return ChatResult(generations=[ChatGeneration(message=resposta)])

    @property
    def recebidas(self) -> list[list[BaseMessage]]:
        return self._recebidas

    @property
    def com_ferramentas(self) -> list[bool]:
        return self._com_ferramentas


def chamada(nome: str, argumentos: dict[str, Any], id_: str = "c1", texto: str = "") -> AIMessage:
    return AIMessage(texto, tool_calls=[{"name": nome, "args": argumentos, "id": id_, "type": "tool_call"}])


DEFINICOES = [
    DefinicaoFerramenta("listar_produtos", "Catálogo", {"type": "object", "properties": {}}, True),
    DefinicaoFerramenta(
        "consultar_pedido",
        "Consulta um pedido",
        {
            "type": "object",
            "properties": {"pedido_id": {"type": "string", "format": "uuid"}},
            "required": ["pedido_id"],
        },
        True,
    ),
    DefinicaoFerramenta(
        "criar_pedido",
        "Cria um pedido",
        {"type": "object", "properties": {"itens": {"type": "array"}}, "required": ["itens"]},
        False,
    ),
]


class FerramentasFalsas:
    """Responde com dados fixos e registra cada chamada."""

    def __init__(self, respostas: dict[str, Any] | None = None) -> None:
        self.respostas = respostas or {}
        self.chamadas: list[tuple[str, dict[str, Any]]] = []

    async def definicoes(self) -> list[DefinicaoFerramenta]:
        return DEFINICOES

    async def chamar(self, nome: str, argumentos: dict[str, Any]) -> Resultado:
        self.chamadas.append((nome, argumentos))
        resposta = self.respostas.get(nome, {"ok": True})
        if isinstance(resposta, Resultado):
            return resposta
        return Resultado(texto=json.dumps(resposta, ensure_ascii=False), erro=False)
