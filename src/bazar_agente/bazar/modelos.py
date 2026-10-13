"""Modelos da API do Bazar.

As restrições espelham as do próprio Bazar (PedidoController): um pedido inválido é
barrado aqui, antes de qualquer chamada, e o modelo de linguagem recebe o motivo
exato para corrigir os argumentos.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

StatusPedido = Literal["PENDENTE", "AGUARDANDO_PAGAMENTO", "CONFIRMADO", "CANCELADO"]

IdProduto = Annotated[
    str, Field(pattern=r"^[a-z0-9-]{1,60}$", description="Id do produto, como aparece no catálogo")
]


class _ModeloBazar(BaseModel):
    # A API do Bazar usa camelCase (produtoId); aqui, snake_case.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class Produto(_ModeloBazar):
    id: str
    nome: str
    preco: Decimal
    disponivel: int


class ItemPedido(_ModeloBazar):
    produto_id: IdProduto
    quantidade: Annotated[int, Field(ge=1, le=99, description="De 1 a 99 unidades")]


class Pedido(_ModeloBazar):
    id: UUID
    status: StatusPedido
    total: Decimal | None = Field(description="Calculado pelo estoque; vazio enquanto o pedido é novo")
    motivo: str | None = Field(description="Por que o pedido foi cancelado, quando foi")
    itens: list[ItemPedido]
    criado_em: datetime
    atualizado_em: datetime


ItensDoPedido = Annotated[list[ItemPedido], Field(min_length=1, max_length=20)]
