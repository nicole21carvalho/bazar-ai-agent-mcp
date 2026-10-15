"""Os cenários de cenarios.toml, validados: campo com nome errado falha na hora."""

import tomllib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ARQUIVO = Path(__file__).with_name("cenarios.toml")

Categoria = Literal["consulta", "compra", "seguranca", "escopo"]


class _Estrito(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ItemEsperado(_Estrito):
    produto_id: str
    quantidade: int


class Turno(_Estrito):
    cliente: str | None = None
    confirmar: bool | None = None
    chama: list[str] = []
    nao_chama: list[str] = []
    sem_ferramentas: bool = False
    confirmacao: Literal["proibida", "obrigatoria", "opcional"] = "proibida"
    itens: list[ItemEsperado] | None = None
    pedido_criado: bool | None = None
    contem: list[str] = []
    nao_contem: list[str] = []

    @model_validator(mode="after")
    def _coerente(self) -> Self:
        if (self.cliente is None) == (self.confirmar is None):
            raise ValueError("cada turno tem 'cliente' (mensagem) ou 'confirmar' (resposta), e só um")
        if self.itens is not None and self.confirmacao != "obrigatoria":
            raise ValueError("'itens' só faz sentido com confirmacao = \"obrigatoria\"")
        return self


class Cenario(_Estrito):
    nome: str
    categoria: Categoria
    descricao: str
    turnos: list[Turno] = Field(alias="turno", min_length=1)

    @model_validator(mode="after")
    def _ordem(self) -> Self:
        for anterior, turno in zip([None, *self.turnos], self.turnos, strict=False):
            espera = anterior is not None and anterior.confirmacao == "obrigatoria"
            if (turno.confirmar is not None) and not espera:
                raise ValueError(
                    f"{self.nome}: 'confirmar' precisa vir depois de uma confirmação obrigatória"
                )
        return self


def carregar(arquivo: Path = ARQUIVO) -> list[Cenario]:
    dados = tomllib.loads(arquivo.read_text(encoding="utf-8"))
    cenarios = [Cenario.model_validate(c) for c in dados["cenario"]]
    nomes = [c.nome for c in cenarios]
    if len(nomes) != len(set(nomes)):
        raise ValueError("há cenários com o mesmo nome")
    return cenarios
