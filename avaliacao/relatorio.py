"""Resultados em JSON (para guardar) e em Markdown (para o resumo do CI e o README)."""

from dataclasses import asdict
from datetime import UTC, datetime
from statistics import mean
from typing import Any

from avaliacao.executar import ResultadoCenario

CATEGORIAS = {"consulta": "Consultas", "compra": "Compras", "seguranca": "Segurança", "escopo": "Escopo"}


def resumo(resultados: list[ResultadoCenario]) -> dict[str, Any]:
    turnos = [t for r in resultados for t in r.turnos]
    por_categoria = {
        categoria: {
            "passaram": sum(r.passou for r in resultados if r.categoria == categoria),
            "total": sum(r.categoria == categoria for r in resultados),
        }
        for categoria in CATEGORIAS
    }
    passaram = sum(r.passou for r in resultados)
    return {
        "cenarios": len(resultados),
        "passaram": passaram,
        "taxa": round(passaram / len(resultados), 3) if resultados else 0.0,
        "por_categoria": por_categoria,
        # Quantas vezes o modelo pediu confirmação por texto antes de agir (um turno a mais para o cliente).
        "perguntou_antes": sum(t.perguntou_antes for t in turnos),
        "segundos_por_turno": round(mean(t.segundos for t in turnos), 1) if turnos else 0.0,
        "tokens_por_turno": round(mean(t.tokens_entrada + t.tokens_saida for t in turnos)) if turnos else 0,
    }


def para_json(modelo: str, provedor: str, resultados: list[ResultadoCenario]) -> dict[str, Any]:
    return {
        "modelo": modelo,
        "provedor": provedor,
        "gerado_em": datetime.now(UTC).isoformat(timespec="seconds"),
        "resumo": resumo(resultados),
        "cenarios": [{**asdict(r), "passou": r.passou} for r in resultados],
    }


def _fracao(categoria: dict[str, int]) -> str:
    return f"{categoria['passaram']}/{categoria['total']}" if categoria["total"] else "-"


def markdown_do_modelo(dados: dict[str, Any]) -> str:
    r = dados["resumo"]
    linhas = [
        f"### {dados['modelo']} ({dados['provedor']}): {r['passaram']}/{r['cenarios']} cenários",
        "",
        "| Cenário | Categoria | Resultado | Ferramentas pedidas | O que falhou |",
        "|---|---|---|---|---|",
    ]
    for c in dados["cenarios"]:
        ferramentas = " → ".join(f for t in c["turnos"] for f in t["ferramentas"]) or "nenhuma"
        falhas = c["falhas"] + [f for t in c["turnos"] for f in t["falhas"]]
        linhas.append(
            f"| `{c['nome']}` | {CATEGORIAS[c['categoria']]} | {'✅' if c['passou'] else '❌'} "
            f"| {ferramentas} | {'; '.join(falhas).replace('|', '/') or ''} |"
        )
    linhas += [
        "",
        f"Tempo médio por turno: {r['segundos_por_turno']} s · tokens por turno: {r['tokens_por_turno']}"
        f" · perguntou antes de agir: {r.get('perguntou_antes', 0)} vez(es)",
    ]
    return "\n".join(linhas) + "\n"


def markdown_da_comparacao(todos: list[dict[str, Any]]) -> str:
    cabecalho = [
        "Modelo",
        "Acerto",
        *CATEGORIAS.values(),
        "Perguntou antes",
        "Tempo por turno",
        "Tokens por turno",
    ]
    linhas = ["| " + " | ".join(cabecalho) + " |", "|" + "---|" * len(cabecalho)]
    for dados in sorted(todos, key=lambda d: (-d["resumo"]["taxa"], d["resumo"]["segundos_por_turno"])):
        r = dados["resumo"]
        linhas.append(
            "| "
            + " | ".join(
                [
                    f"`{dados['modelo']}`",
                    f"**{r['taxa']:.0%}** ({r['passaram']}/{r['cenarios']})",
                    *(_fracao(r["por_categoria"][c]) for c in CATEGORIAS),
                    str(r.get("perguntou_antes", 0)),
                    f"{r['segundos_por_turno']} s",
                    str(r["tokens_por_turno"]),
                ]
            )
            + " |"
        )
    return "\n".join(linhas) + "\n"
