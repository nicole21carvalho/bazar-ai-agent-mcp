"""Chamadas à API do Bazar (pelo gateway, com o token do cliente já trocado).

Quem decide o que o cliente pode ver é o Bazar: pedido de outra pessoa responde 404
lá. Este cliente não tenta "filtrar" nada; ele só traduz as respostas.
"""

import asyncio
from uuid import UUID, uuid4

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


class PedidoIncerto(BazarIndisponivel):
    """A criação ficou sem resposta: o pedido pode ter sido criado ou não."""


PEDIDO_INCERTO = (
    "Não deu para confirmar se o pedido foi criado: o Bazar demorou demais para responder. "
    "Confira em “Meus pedidos” antes de pedir de novo."
)

# Criação de pedido: cada tentativa espera mais que uma leitura, e as novas tentativas
# levam a mesma chave de idempotência. Somadas (3 x 15 s + pausas), ficam abaixo dos
# 60 s que o agente espera por uma ferramenta.
TENTATIVAS_DE_ESCRITA = 3
TEMPO_DE_ESCRITA = httpx.Timeout(15.0, connect=3.0)

_produtos = TypeAdapter(list[Produto])
_pedidos = TypeAdapter(list[Pedido])


class ClienteBazar:
    def __init__(self, http: httpx.AsyncClient, pausa_entre_tentativas: float = 0.5) -> None:
        # O AsyncClient já vem com base_url (o gateway) e timeouts curtos.
        self._http = http
        self._pausa = pausa_entre_tentativas

    async def listar_produtos(self) -> list[Produto]:
        resposta = await self._chamar("GET", "/api/produtos")
        return _produtos.validate_python(resposta.json())

    async def listar_pedidos(self, token: str) -> list[Pedido]:
        resposta = await self._chamar("GET", "/api/pedidos", token)
        return _pedidos.validate_python(resposta.json())

    async def consultar_pedido(self, token: str, pedido_id: UUID) -> Pedido:
        resposta = await self._chamar("GET", f"/api/pedidos/{pedido_id}", token)
        return Pedido.model_validate(resposta.json())

    async def criar_pedido(self, token: str, itens: list[ItemPedido], chave: str | None = None) -> Pedido:
        # Uma chave por pedido. Se a resposta não chegar, as novas tentativas levam a mesma
        # chave, e o Bazar devolve o pedido que já criou em vez de criar outro.
        chave = chave or uuid4().hex
        corpo = {"itens": [i.model_dump(by_alias=True) for i in itens]}
        resposta = await self._chamar("POST", "/api/pedidos", token, corpo, idempotencia=chave)
        return Pedido.model_validate(resposta.json())

    async def _chamar(
        self,
        metodo: str,
        caminho: str,
        token: str | None = None,
        corpo: object | None = None,
        idempotencia: str | None = None,
    ) -> httpx.Response:
        cabecalhos = {"Authorization": f"Bearer {token}"} if token else {}
        escrita = metodo != "GET"
        if idempotencia:
            cabecalhos["Idempotency-Key"] = idempotencia
        # Leitura que falhou na rede (serviço acordando, por exemplo) ganha mais uma
        # tentativa. Escrita só se repete com chave de idempotência: sem ela, repetir
        # poderia criar dois pedidos.
        tentativas = (TENTATIVAS_DE_ESCRITA if idempotencia else 1) if escrita else 2
        tempo = TEMPO_DE_ESCRITA if escrita else httpx.USE_CLIENT_DEFAULT
        for tentativa in range(1, tentativas + 1):
            ultima = tentativa == tentativas
            try:
                resposta = await self._http.request(
                    metodo, caminho, headers=cabecalhos, json=corpo, timeout=tempo
                )
            except httpx.TransportError as erro:
                if ultima:
                    if escrita:
                        raise PedidoIncerto(PEDIDO_INCERTO) from erro
                    raise BazarIndisponivel("O Bazar não respondeu. Tente de novo em instantes.") from erro
            else:
                # O gateway desistiu de esperar o serviço de pedidos (ou ele está
                # reiniciando): com a chave, perguntar de novo é seguro.
                if not (idempotencia and resposta.status_code in (502, 503, 504)) or ultima:
                    break
            await asyncio.sleep(self._pausa * tentativa)

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
            case 502 | 503 | 504 if escrita:
                # A requisição pode ter chegado ao serviço de pedidos antes de o gateway desistir.
                raise PedidoIncerto(PEDIDO_INCERTO)
            case _:
                raise BazarIndisponivel("O Bazar está com problemas agora. Tente de novo em instantes.")
