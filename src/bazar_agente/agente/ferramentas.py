"""Ponte entre o servidor MCP e o modelo de linguagem.

O adaptador pronto do LangChain para MCP ainda não suporta o SDK 2.x; esta ponte é
pequena de propósito: lista as ferramentas do servidor (nome, descrição, esquema dos
argumentos e se só leem) e as executa pela sessão MCP do cliente logado.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent, Tool


@dataclass(frozen=True)
class DefinicaoFerramenta:
    nome: str
    descricao: str
    parametros: dict[str, Any]
    # Sem a anotação "só leitura", a ferramenta é tratada como uma ação: pede
    # confirmação do cliente. Na dúvida, o padrão é o seguro.
    somente_leitura: bool

    @classmethod
    def de_mcp(cls, ferramenta: Tool) -> DefinicaoFerramenta:
        anotacoes = ferramenta.annotations
        return cls(
            nome=ferramenta.name,
            descricao=ferramenta.description or ferramenta.title or ferramenta.name,
            parametros=ferramenta.input_schema,
            somente_leitura=bool(anotacoes and anotacoes.read_only_hint),
        )

    def para_modelo(self) -> dict[str, Any]:
        """Formato de "function calling" aceito pelos provedores do LangChain."""
        return {
            "type": "function",
            "function": {"name": self.nome, "description": self.descricao, "parameters": self.parametros},
        }


@dataclass(frozen=True)
class Resultado:
    texto: str
    erro: bool


class Ferramentas(Protocol):
    async def definicoes(self) -> list[DefinicaoFerramenta]: ...

    async def chamar(self, nome: str, argumentos: dict[str, Any]) -> Resultado: ...


class FerramentasMCP:
    """Ferramentas de um servidor MCP, por uma sessão já autenticada."""

    def __init__(self, cliente: Client) -> None:
        self._cliente = cliente
        self._definicoes: list[DefinicaoFerramenta] | None = None

    async def definicoes(self) -> list[DefinicaoFerramenta]:
        if self._definicoes is None:
            lista = await self._cliente.list_tools()
            self._definicoes = [DefinicaoFerramenta.de_mcp(f) for f in lista.tools]
        return self._definicoes

    async def chamar(self, nome: str, argumentos: dict[str, Any]) -> Resultado:
        resultado = await self._cliente.call_tool(nome, argumentos)
        return Resultado(texto=_texto(resultado), erro=bool(resultado.is_error))


def _texto(resultado: CallToolResult) -> str:
    if resultado.structured_content is not None and not resultado.is_error:
        return json.dumps(resultado.structured_content, ensure_ascii=False, default=str)
    partes = [c.text for c in resultado.content if isinstance(c, TextContent)]
    return "\n".join(partes) or ("Erro sem detalhes." if resultado.is_error else "")


@asynccontextmanager
async def sessao_mcp(url: str, token: str) -> AsyncIterator[FerramentasMCP]:
    """Abre uma sessão MCP por Streamable HTTP com o token (já trocado) do cliente."""
    cabecalhos = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=cabecalhos, timeout=httpx2.Timeout(30.0, connect=5.0)) as http,
        Client(streamable_http_client(url, http_client=http)) as cliente,
    ):
        yield FerramentasMCP(cliente)
