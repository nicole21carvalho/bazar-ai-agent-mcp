"""Chamadas à API do Bazar (pelo gateway, com o token do cliente já trocado).

Quem decide o que o cliente pode ver é o Bazar: pedido de outra pessoa responde 404
lá. Este cliente não tenta "filtrar" nada; ele só traduz as respostas.
"""

from uuid import UUID

import httpx
from pydantic import TypeAdapter

from bazar_agente.bazar.modelos import ItemPedido, Pedido, Produto


class ErroBazar(Exception):
    """Falha de uma chamada ao Bazar, com uma mensagem que pode ir para o cliente."""

    def __init__(self, mensagem: str) -> None:
        super().__init__(mensagem)
        self.mensagem = mensagem


class NaoEncontrado(ErroBazar):
    pass


class AcessoNegado(ErroBazar):
    pass


class LimiteExcedido(ErroBazar):
    def __init__(self, mensagem: str, tente_em_segundos: int | None) -> None:
        super().__init__(mensagem)
        self.tente_em_segundos = tente_em_segundos


class EntradaInvalida(ErroBazar):
    pass


class BazarIndisponivel(ErroBazar):
    pass


_produtos = TypeAdapter(list[Produto])
_pedidos = TypeAdapter(list[Pedido])


class ClienteBazar:
    def __init__(self, http: httpx.AsyncClient) -> None:
        # O AsyncClient já vem com base_url (o gateway) e timeouts curtos.
        self._http = http

    async def listar_produtos(self) -> list[Produto]:
        resposta = await self._chamar("GET", "/api/produtos")
        return _produtos.validate_python(resposta.json())

    async def listar_pedidos(self, token: str) -> list[Pedido]:
        resposta = await self._chamar("GET", "/api/pedidos", token)
        return _pedidos.validate_python(resposta.json())

    async def consultar_pedido(self, token: str, pedido_id: UUID) -> Pedido:
        resposta = await self._chamar("GET", f"/api/pedidos/{pedido_id}", token)
        return Pedido.model_validate(resposta.json())

    async def criar_pedido(self, token: str, itens: list[ItemPedido]) -> Pedido:
        corpo = {"itens": [i.model_dump(by_alias=True) for i in itens]}
        resposta = await self._chamar("POST", "/api/pedidos", token, corpo)
        return Pedido.model_validate(resposta.json())

    async def _chamar(
        self, metodo: str, caminho: str, token: str | None = None, corpo: object | None = None
    ) -> httpx.Response:
        cabecalhos = {"Authorization": f"Bearer {token}"} if token else {}
        # Leitura que falhou na rede (serviço acordando, por exemplo) ganha mais uma
        # tentativa. Criação de pedido não: repetir poderia criar dois pedidos.
        tentativas = 2 if metodo == "GET" else 1
        for tentativa in range(1, tentativas + 1):
            try:
                resposta = await self._http.request(metodo, caminho, headers=cabecalhos, json=corpo)
                break
            except httpx.TransportError as erro:
                if tentativa == tentativas:
                    raise BazarIndisponivel("O Bazar não respondeu. Tente de novo em instantes.") from erro

        match resposta.status_code:
            case code if code < 300:
                return resposta
            case 404:
                raise NaoEncontrado("Não encontrei esse pedido entre os seus.")
            case 401 | 403:
                raise AcessoNegado("Sua sessão não permite essa operação. Entre de novo no Bazar.")
            case 429:
                espera = resposta.headers.get("Retry-After")
                raise LimiteExcedido(
                    "Muitas operações seguidas. Espere um pouco e tente de novo.",
                    int(espera) if espera and espera.isdigit() else None,
                )
            case 400 | 422:
                raise EntradaInvalida("O Bazar recusou os dados do pedido. Confira produtos e quantidades.")
            case _:
                raise BazarIndisponivel("O Bazar está com problemas agora. Tente de novo em instantes.")
