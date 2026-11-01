"""Regenera los goldens del corpus: la salida **y** el IR.

Sólo hace falta cuando un cambio es intencionado. El diff que produce el
`git diff` es la revisión: si un golden cambia y no debía, el cambio no era
intencionado.

    python3 tools/update_corpus.py
    python3 tools/update_corpus.py --only 01_hola

Los dos ficheros se regeneran en la misma pasada, y a propósito. El IR de un
programa cambia mucho más a menudo que su salida --casi cualquier retoque del
backend lo mueve-- y tenerlos juntos significa que el diff enseña **qué
instrucción** cambió al lado de **qué línea imprime**, que es la pregunta que uno
se hace al revisar. Con los goldens separados, esa pregunta necesita dos
comparaciones y un razonamiento de por dónde empezar.

El IR canónico es también el juez del bootstrap (ADR 0016): Stage 0 y Stage 1 se
comparan sobre este texto y no sobre binarios, porque un binario no dice dónde
está la diferencia. Es el fichero más importante que produce este script.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compiler"))

from sourc import interp, sir  # noqa: E402
from sourc import astdump  # noqa: E402
from sourc.texto import visible  # noqa: E402
from sourc.lexer import tokenize  # noqa: E402
from sourc.diagnostics import CompileError, DiagnosticBag  # noqa: E402
from sourc.parser import parse  # noqa: E402
from sourc.types import check  # noqa: E402

CORPUS = ROOT / "tests" / "corpus"
EJEMPLOS = ROOT / "examples"
EXPECTED = ROOT / "tests" / "golden" / "out"
IR = ROOT / "tests" / "golden" / "ir"
TOKENS = ROOT / "tests" / "golden" / "lex"
AST = ROOT / "tests" / "golden" / "ast"
CASOS_AST = ROOT / "tests" / "casos_ast"
CASOS_PARSER = ROOT / "tests" / "casos_parser"
PARSER = ROOT / "tests" / "golden" / "parser"


def fuentes() -> list[Path]:
    """Corpus y ejemplos.

    Los dos, y no sólo el corpus, porque es la lista que usa `tools/parity.py` y
    la que compilará el bootstrap: un programa que nadie compara contra el
    intérprete no aporta nada como juez, y el IR de los ejemplos es exactamente
    el material con el que se prueba la rebanada 1 del compilador en Source.
    """
    return sorted(CORPUS.glob("*.src")) + sorted(EJEMPLOS.glob("*.src"))


def compile_one(path: Path):
    """Devuelve `(programa, modulo)` o `None`.

    Los dos y no sólo el módulo porque los tres artefactos --salida, IR y AST--
    salen de la **misma** pasada. Parsear otra vez para el AST daría un segundo
    `parse` que podría diferir sin que nadie lo notara, y entonces el golden del
    AST y el del IR serían el mismo error escrito dos veces.
    """
    source = path.read_text(encoding="utf-8")
    bag = DiagnosticBag()
    try:
        program = parse(source, bag)
    except CompileError as error:
        print(f"{path.name}: {error.diagnostic.message}")
        return None
    typing = check(program, bag)
    if bag.has_errors():
        print(f"{path.name}:")
        print(bag.render(str(path), source))
        return None
    return program, sir.build(program, typing, path.stem, "1.0")


def main() -> int:
    parser = argparse.ArgumentParser(prog="update_corpus")
    parser.add_argument("--only", help="regenera sólo un programa (sin extensión)")
    args = parser.parse_args()

    EXPECTED.mkdir(parents=True, exist_ok=True)
    IR.mkdir(parents=True, exist_ok=True)
    TOKENS.mkdir(parents=True, exist_ok=True)
    AST.mkdir(parents=True, exist_ok=True)
    PARSER.mkdir(parents=True, exist_ok=True)
    updated = 0
    failed = 0
    cambios_ir = []
    cambios_lex = []
    cambios_ast = []
    cambios_parser = []

    for path in fuentes():
        if args.only and path.stem != args.only:
            continue
        compilado = compile_one(path)
        if compilado is None:
            failed += 1
            continue
        program, module = compilado
        # La salida sólo se goldeniza para el corpus. `examples/laberinto.src`
        # tiene bucle de juego y sin límite de frames se come los pasos
        # infinitos; el IR no necesita ejecutarse, y por eso los dos artefactos
        # no se generan en el mismo sitio aunque salen del mismo módulo.
        if path.parent == CORPUS:
            result = interp.run(module, step_limit=5_000_000)
            target = EXPECTED / (path.stem + ".txt")
            previous = (target.read_text(encoding="utf-8")
                        if target.exists() else None)
            if previous != result.output:
                target.write_text(result.output, encoding="utf-8")
                state = "nuevo" if previous is None else "cambiado"
                print(f"{path.name}: salida {state} "
                      f"({result.allocations} alocaciones)")

        # El AST se vuelca desde el `program` que ya hay, no desde un segundo
        # `parse`: un segundo parse podría diferir sin que nadie lo notara y
        # entonces el golden y el compilador serían el mismo error dos veces.
        buffer = io.StringIO()
        astdump.volcar(program, buffer)
        texto_ast = buffer.getvalue()
        destino_ast = AST / (path.stem + ".ast")
        antes_ast = (destino_ast.read_text(encoding="utf-8")
                     if destino_ast.exists() else None)
        if antes_ast != texto_ast:
            destino_ast.write_text(texto_ast, encoding="utf-8")
            cambios_ast.append((path.stem,
                                "nuevo" if antes_ast is None else "cambiado"))

        texto_lex = _flujo_de_tokens(path)
        destino_lex = TOKENS / (path.stem + ".lex")
        antes_lex = (destino_lex.read_text(encoding="utf-8")
                     if destino_lex.exists() else None)
        if antes_lex != texto_lex:
            destino_lex.write_text(texto_lex, encoding="utf-8")
            cambios_lex.append((path.stem,
                                "nuevo" if antes_lex is None else "cambiado"))

        texto_ir = sir.serialize(module)
        destino_ir = IR / (path.stem + ".sir")
        antes_ir = (destino_ir.read_text(encoding="utf-8")
                    if destino_ir.exists() else None)
        if antes_ir != texto_ir:
            destino_ir.write_text(texto_ir, encoding="utf-8")
            cambios_ir.append((path.stem,
                               "nuevo" if antes_ir is None else "cambiado",
                               _que_cambio(antes_ir, texto_ir)))
        updated += 1

    # Los casos de `tests/casos_ast/` son **sólo de parseo**: hay nodos que
    # parsean y no compilan --`ARENA` es de la Fase 4, `GOTO` es E0043, el acceso
    # a campo es E0025-- y el AST de esos nodos necesita un golden igual que el
    # de los demás. Pasarlos por `compile_one` los rechazaría el typechecker y
    # quedarían sin judge, que es como un nodo se queda sin probar sin que nadie
    # lo decida.
    bag = DiagnosticBag()
    for path in sorted(CASOS_AST.glob("*.src")):
        fuente = path.read_text(encoding="utf-8")
        try:
            programa = parse(fuente, bag)
        except CompileError as error:
            print(f"{path.name}: no parsea: {error.diagnostic.message}")
            continue
        buffer = io.StringIO()
        astdump.volcar(programa, buffer)
        destino = AST / (path.stem + ".ast")
        antes = destino.read_text(encoding="utf-8") if destino.exists() else None
        if antes != buffer.getvalue():
            destino.write_text(buffer.getvalue(), encoding="utf-8")
            cambios_ast.append((path.stem,
                                "nuevo" if antes is None else "cambiado"))

    # Los casos del parser (rebanada 2a) van a `golden/parser/`, aparte de
    # `golden/ast/`. Dos razones y las dos son de alcance: el golden de `ast` es
    # el juez del parser de Python y no debe moverse mientras se construye el de
    # Source, y los casos de 2a son un subconjunto --sin operadores binarios--
    # que no es el programa entero.
    bag = DiagnosticBag()
    for path in sorted(CASOS_PARSER.glob("*.src")):
        fuente = path.read_text(encoding="utf-8")
        try:
            programa = parse(fuente, bag)
        except CompileError as error:
            print(f"{path.name}: no parsea: {error.diagnostic.message}")
            continue
        buffer = io.StringIO()
        astdump.volcar(programa, buffer)
        destino = PARSER / (path.stem + ".ast")
        antes = destino.read_text(encoding="utf-8") if destino.exists() else None
        if antes != buffer.getvalue():
            destino.write_text(buffer.getvalue(), encoding="utf-8")
            cambios_parser.append((path.stem,
                                   "nuevo" if antes is None else "cambiado"))

    for nombre, estado, detalle in cambios_ir:
        print(f"{nombre}.sir: IR {estado}{detalle}")
    for nombre, estado in cambios_lex:
        print(f"{nombre}.lex: tokens {estado}")
    for nombre, estado in cambios_ast:
        print(f"{nombre}.ast: AST {estado}")
    for nombre, estado in cambios_parser:
        print(f"{nombre}.ast (2a): {estado}")

    print(f"\n{updated} programa(s), {failed} con error")
    return 1 if failed else 0


def _flujo_de_tokens(path: Path) -> str:
    """Los tokens de un fuente en la forma canónica de `sourc lex`.

    Se relee el fichero en vez de看不出来 el módulo que ya se tiene, y es a
    propósito: el lexer es la *primera* fase, así que la única forma de que su
    golden signifique algo es generarlo desde el mismo punto de entrada que
    usará el compilador escrito en Source, que es el texto.
    """
    bolsa = DiagnosticBag()
    tokens = tokenize(path.read_text(encoding="utf-8"), bolsa)
    lineas = [f"{t.span.start:>4} {t.span.end:>5} {t.kind:<13} "
              f"{visible(t.value)} | {visible(t.text)}"
              for t in tokens]
    lineas.append(f"\n; {len(tokens)} token(s)")
    return "\n".join(lineas) + "\n"


def _que_cambio(antes: str | None, ahora: str) -> str:
    """Cuenta las líneas que cambian, para que el diff se pueda dimensionar.

    El número importa: "cambió" y "cambió en 300 de 400 líneas" son dos
    noticias distintas, y la segunda es la que dice si el cambio fue de una
    instrucción o de un refactor.
    """
    if antes is None:
        return ""
    viejas, nuevas = antes.splitlines(), ahora.splitlines()
    if len(viejas) != len(nuevas):
        return f" ({len(viejas)} -> {nuevas and len(nuevas)} lineas)"
    distintas = sum(1 for a, b in zip(viejas, nuevas) if a != b)
    return f" ({distintas} de {len(viejas)} lineas)"


if __name__ == "__main__":
    sys.exit(main())