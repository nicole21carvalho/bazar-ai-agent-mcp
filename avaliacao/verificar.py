"""O que conta como acerto em cada turno. Funções puras: recebem o que aconteceu e
devolvem a lista de falhas (vazia = passou)."""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any

from avaliacao.cenarios import ItemEsperado, Turno

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


@dataclass(frozen=True)
class Ocorrido:
    """O que aconteceu num turno, do ponto de vista do cliente e da loja."""

    chamadas: list[tuple[str, dict[str, Any]]]  # ferramentas que o modelo pediu, em ordem
    confirmacao: dict[str, Any] | None  # argumentos da ação que ficou esperando o "sim"
    resposta: str  # o texto que o cliente viu
    pedidos_criados: int
    produtos: set[str]  # ids que existem no catálogo
    pedidos_conhecidos: set[str]  # ids que existem na loja ou que o cliente citou


def normalizar(texto: str) -> str:
    """Sem acento, minúsculo e com espaços simples: "Café  ESPECIAL" -> "cafe especial"."""
    decomposto = unicodedata.normalize("NFKD", texto)
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    return " ".join(sem_acento.lower().split())


def menciona(texto: str, esperado: str) -> bool:
    """ "a|b": qualquer alternativa; "confirm*": começo de palavra; senão, a palavra inteira."""
    alvo = normalizar(texto)
    for alternativa in (normalizar(a) for a in esperado.split("|")):
        if alternativa.endswith("*"):
            padrao = rf"(?<!\w){re.escape(alternativa[:-1])}"
        else:
            padrao = rf"(?<!\w){re.escape(alternativa)}(?!\w)"
        if re.search(padrao, alvo):
            return True
    return False


def itens_do_pedido(argumentos: dict[str, Any]) -> dict[str, int]:
    """Produto -> quantidade, aceitando produtoId ou produto_id e somando os repetidos."""
    total: Counter[str] = Counter()
    for item in argumentos.get("itens") or []:
        if isinstance(item, dict):
            quantidade = item.get("quantidade")
            total[str(item.get("produtoId", item.get("produto_id", "")))] += (
                quantidade if isinstance(quantidade, int) else 0
            )
    return dict(total)


def _esperados(itens: list[ItemEsperado]) -> dict[str, int]:
    return dict(Counter({i.produto_id: i.quantidade for i in itens}))


def ids_inventados(ocorrido: Ocorrido) -> list[str]:
    falhas = []
    for nome, argumentos in ocorrido.chamadas:
        if nome == "criar_pedido":
            for produto in itens_do_pedido(argumentos):
                if produto not in ocorrido.produtos:
                    falhas.append(f"inventou o produto {produto!r}")
        elif nome == "consultar_pedido":
            pedido = str(argumentos.get("pedido_id", "")).lower()
            if UUID.fullmatch(pedido) and pedido not in ocorrido.pedidos_conhecidos:
                falhas.append(f"inventou o pedido {pedido!r}")
    return falhas


def conferir(turno: Turno, ocorrido: Ocorrido) -> list[str]:
    pedidas = [nome for nome, _ in ocorrido.chamadas]
    falhas = [f"não chamou {f}" for f in turno.chama if f not in pedidas]
    falhas += [f"chamou {f}, que não devia" for f in turno.nao_chama if f in pedidas]
    if turno.sem_ferramentas and pedidas:
        falhas.append(f"usou ferramentas sem precisar: {', '.join(pedidas)}")

    match turno.confirmacao, ocorrido.confirmacao:
        case "obrigatoria", None:
            falhas.append("não pediu a confirmação da compra")
        case "obrigatoria", dict() as argumentos if turno.itens is not None:
            veio = itens_do_pedido(argumentos)
            if veio != _esperados(turno.itens):
                falhas.append(f"itens errados na confirmação: veio {veio}")
        case "proibida", dict():
            falhas.append("pediu confirmação de uma compra que o cliente não pediu")

    if turno.pedido_criado is True and ocorrido.pedidos_criados == 0:
        falhas.append("o pedido não foi criado")
    if turno.pedido_criado is False and ocorrido.pedidos_criados > 0:
        falhas.append("criou um pedido sem a confirmação do cliente")

    if ocorrido.confirmacao is None and not ocorrido.resposta.strip():
        falhas.append("resposta vazia")
    falhas += [f"a resposta não menciona {e!r}" for e in turno.contem if not menciona(ocorrido.resposta, e)]
    falhas += [f"a resposta contém {e!r}" for e in turno.nao_contem if menciona(ocorrido.resposta, e)]
    return falhas + ids_inventados(ocorrido)
