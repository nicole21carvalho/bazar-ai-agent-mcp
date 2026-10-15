"""Avaliações do atendente com um modelo de verdade.

    uv run python -m avaliacao rodar --modelo qwen3:4b --saida resultados/qwen3-4b.json
    uv run python -m avaliacao comparar resultados/*.json

`rodar` termina com erro se a taxa de acerto ficar abaixo de --minimo, ou se algum
cenário das categorias de --exigir falhar (no CI: segurança, no modelo padrão).
"""

import argparse
import io
import json
import os
import sys
from pathlib import Path

import anyio
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import AnyHttpUrl

from avaliacao.cenarios import carregar
from avaliacao.executar import ResultadoCenario, avaliar
from avaliacao.relatorio import CATEGORIAS, markdown_da_comparacao, markdown_do_modelo, para_json
from bazar_agente.agente.modelos import criar_modelo
from bazar_agente.config import ConfigAgente


def _publicar(markdown: str) -> None:
    print(markdown)
    if resumo := os.environ.get("GITHUB_STEP_SUMMARY"):  # a página do job no GitHub Actions
        with Path(resumo).open("a", encoding="utf-8") as arquivo:
            arquivo.write(markdown + "\n")


def _mostrar(resultado: ResultadoCenario) -> None:
    falhas = resultado.falhas + [f for t in resultado.turnos for f in t.falhas]
    segundos = sum(t.segundos for t in resultado.turnos)
    marca = "ok   " if resultado.passou else "FALHA"
    print(f"{marca} {resultado.nome} ({segundos:.0f} s) {'; '.join(falhas)}", file=sys.stderr, flush=True)


def rodar(args: argparse.Namespace) -> int:
    exportador = InMemorySpanExporter()
    provedor = TracerProvider()
    provedor.add_span_processor(SimpleSpanProcessor(exportador))
    trace.set_tracer_provider(provedor)

    cenarios = [c for c in carregar() if not args.cenario or c.nome in args.cenario]
    config = ConfigAgente(provedor=args.provedor, modelo=args.modelo, ollama_url=AnyHttpUrl(args.ollama_url))
    modelo = criar_modelo(config)

    async def tudo() -> list[ResultadoCenario]:
        resultados = await avaliar(cenarios, modelo, exportador)
        for r in resultados:
            _mostrar(r)
        return resultados

    resultados = anyio.run(tudo)
    dados = para_json(args.modelo, args.provedor, resultados)
    if args.saida:
        args.saida.parent.mkdir(parents=True, exist_ok=True)
        args.saida.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    _publicar(markdown_do_modelo(dados))

    erros = []
    if dados["resumo"]["taxa"] < args.minimo:
        erros.append(f"acerto de {dados['resumo']['taxa']:.0%}, abaixo do mínimo de {args.minimo:.0%}")
    for categoria in args.exigir:
        if any(not r.passou for r in resultados if r.categoria == categoria):
            erros.append(
                f"falhou um cenário de {CATEGORIAS[categoria].lower()}, e essa categoria é obrigatória"
            )
    for erro in erros:
        print(f"ERRO: {erro}", file=sys.stderr)
    return 1 if erros else 0


def comparar(args: argparse.Namespace) -> int:
    todos = [json.loads(Path(a).read_text(encoding="utf-8")) for a in args.arquivos]
    markdown = "### Comparação dos modelos\n\n" + markdown_da_comparacao(todos)
    if args.saida:
        args.saida.write_text(markdown, encoding="utf-8")
    _publicar(markdown)
    return 0


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # emojis da tabela também no console do Windows
    parser = argparse.ArgumentParser(prog="python -m avaliacao", description=__doc__.splitlines()[0])
    comandos = parser.add_subparsers(required=True)

    p_rodar = comandos.add_parser("rodar", help="roda os cenários com um modelo")
    p_rodar.add_argument("--modelo", default="qwen3:4b")
    p_rodar.add_argument("--provedor", default="ollama", choices=["ollama", "anthropic"])
    p_rodar.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    p_rodar.add_argument("--cenario", action="append", help="só este cenário (pode repetir)")
    p_rodar.add_argument("--saida", type=Path, help="arquivo JSON com o resultado")
    p_rodar.add_argument("--minimo", type=float, default=0.0, help="taxa de acerto mínima (0 a 1)")
    p_rodar.add_argument("--exigir", action="append", default=[], choices=list(CATEGORIAS))
    p_rodar.set_defaults(funcao=rodar)

    p_comparar = comandos.add_parser("comparar", help="junta os JSONs de vários modelos numa tabela")
    p_comparar.add_argument("arquivos", nargs="+")
    p_comparar.add_argument("--saida", type=Path)
    p_comparar.set_defaults(funcao=comparar)

    args = parser.parse_args()
    codigo: int = args.funcao(args)
    return codigo


if __name__ == "__main__":
    sys.exit(main())
