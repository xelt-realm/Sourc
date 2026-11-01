"""CLI: `sourc` — la herramienta de línea de órdenes.

Subcomandos de la Fase 1:

    sourc check   hola.src              parseo + typecheck
    sourc run     hola.src [--keys w,a] ejecuta el IR en el intérprete
    sourc build   hola.src [--emit sir] baja a IR y a WASM
    sourc sir     hola.src              imprime el IR canónico
    sourc test    [--dir DIR]           ejecuta la suite

Sin dependencias externas. Arranque por debajo de 80 ms, que es un presupuesto
documentado: una herramienta que tarda medio segundo en arrancar no se usa.
"""

from __future__ import annotations

import argparse
import sys
import time
import os
from pathlib import Path

from . import (LANG_VERSION, VERSION, astdump, budget, diffir, interp, paquete,
               rutas, sir, subset)
from .texto import visible  # reexportado: `sourc lex` lo usa
from .wasm import backend
from .diagnostics import CompileError, DiagnosticBag
from .parser import parse
from .types import check

BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
CYAN = "\033[36m"
RESET = "\033[0m"


def use_color(stream) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


class Printer:
    """Escribe por dos canales: lo normal y los errores.

    Los errores van a `sys.stderr` **por su cuenta** y no por `stream`, que era
    un descuido con consecuencias: quien quisiera capturar la salida del
    compilador --un test, una herramienta-- tenía que redireccionar `stderr` a
    mano, y un `except` que se caía en el camino del error no se veía porque su
    salida se iba a un sitio que nadie miraba. Ahora los dos son capturables.
    """

    def __init__(self, stream=None, err_stream=None):
        self.stream = stream or sys.stdout
        self.err_stream = err_stream or sys.stderr
        self.color = use_color(self.stream)

    def paint(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.color else text

    def out(self, text: str = "") -> None:
        self.stream.write(text + "\n")

    def err(self, text: str = "") -> None:
        self.err_stream.write(text + "\n")

    def header(self, text: str) -> None:
        self.out(self.paint(text, BOLD))

    def info(self, text: str) -> None:
        self.out(self.paint(text, DIM))

    def good(self, text: str) -> None:
        self.out(self.paint(text, GREEN))

    def bad(self, text: str) -> None:
        self.err(self.paint(text, RED))

    def warn(self, text: str) -> None:
        self.err(self.paint(text, YELLOW))


def report(printer: Printer, path: Path, source: str,
           diagnostics: DiagnosticBag) -> bool:
    """Imprime los diagnósticos. Devuelve True si no hay errores."""
    printer.info(str(path))
    printed = False
    for diagnostic in diagnostics.sorted():
        printer.err(diagnostic.render(str(path), source))
        printed = True
    if not printed and not diagnostics.has_errors():
        return True
    counts = (f"{len(diagnostics.errors)} error(es), "
              f"{len(diagnostics.warnings)} aviso(s)")
    if diagnostics.has_errors():
        printer.bad(counts)
    else:
        printer.warn(counts)
    return not diagnostics.has_errors()


def compile_file(path: Path, printer: Printer, module_name: str | None = None,
                 contrato: "subset.Contrato | None" = None,
                 source: str | None = None):
    """Pipeline completo: parseo, subconjunto, typecheck, IR.

    El chequeo del subconjunto va **después del parseo y antes del typecheck**,
    y el orden no es arbitrario. Si un programa usa `ARENA` y además tiene un
    error de tipos, quien lo escribe quiere saber primero que está por encima de
    su nivel: "no está implementado" es la noticia de la linguagem, y "está
    por encima del nivel que te toca" es la suya.

    `contrato=None` no comprueba nada, que es lo que pasa por defecto: el corpus
    no cabe en el subconjunto y no tiene por qué caber (ADR 0015).

    `source` se puede pasar ya leído porque quien llama lo necesita después: para
    renderizar el diagnóstico del **backend**, que sale más tarde y en otro
    sitio, y volver a leer el fichero dentro del `except` sería leerlo dos veces
    para poder pintar una flecha.
    """
    if source is None:
        source = path.read_text(encoding="utf-8")
    diagnostics = DiagnosticBag()

    try:
        program = parse(source, diagnostics)
    except CompileError as error:
        printer.err(error.diagnostic.render(str(path), source))
        printer.bad("1 error")
        return None

    if contrato is not None:
        diagnostics.items.extend(subset.violaciones(program, contrato))

    typing = check(program, diagnostics)
    if diagnostics.has_errors():
        report(printer, path, source, diagnostics)
        return None

    if diagnostics.has_warnings():
        report(printer, path, source, diagnostics)

    try:
        module = sir.build(program, typing,
                           module_name or path.stem, LANG_VERSION)
    except CompileError as error:
        printer.err(error.diagnostic.render(str(path), source))
        printer.bad("1 error interno al bajar a IR")
        return None

    if len(typing.string_literals) > 0:
        module.note = f"{len(typing.string_literals)} textos en el fuente"
    return module


# ------------------------------------------------------------------ comandos


def cargar_contrato(nivel: int | None, printer: Printer):
    """Carga `subset.toml` si se pidió un nivel. Devuelve None si no."""
    if nivel is None:
        return None
    ruta = (rutas.raiz() or Path(".")) / "subset.toml"
    if not ruta.is_file():
        printer.bad(f"pides nivel {nivel} y no hay contrato en {ruta}. "
                    "El contrato de subconjunto vive en el repo, no dentro "
                    "del compilador: es una puerta de desarrollo, no una "
                    "feature del lenguaje.")
        raise SystemExit(2)
    try:
        contrato = subset.cargar(ruta)
    except ValueError as error:
        # El contrato roto es un problema del repositorio, no del programa que
        # se está compilando, así que el mensaje no lleva posición ni sugerencia.
        printer.bad(str(error))
        raise SystemExit(2)
    if contrato.nivel != nivel:
        printer.bad(f"pides nivel {nivel} y subset.toml declara el "
                    f"nivel {contrato.nivel}")
        raise SystemExit(2)
    return contrato


def cmd_check(args, printer: Printer) -> int:
    paths = _sources(args)
    if not paths:
        printer.bad("no hay fuentes que comprobar")
        return 2

    failures = 0
    contrato = cargar_contrato(getattr(args, "nivel", None), printer)
    started = time.perf_counter()
    for path in paths:
        printer.header(f"check {path}")
        module = compile_file(path, printer, contrato=contrato)
        if module is None:
            failures += 1
        else:
            printer.good(f"  ok  {sir.count_instrs(module)} instrucciones IR")
    elapsed = (time.perf_counter() - started) * 1000
    printer.info(f"{len(paths) - failures}/{len(paths)} correctos "
                 f"en {elapsed:.0f} ms")
    return 1 if failures else 0


def cmd_run(args, printer: Printer) -> int:
    paths = _sources(args)
    if not paths:
        printer.bad("no hay fuentes que ejecutar")
        return 2
    if len(paths) > 1:
        printer.bad("`run` acepta un solo archivo por ahora")
        return 2

    path = paths[0]
    # `--salida` deja la stdout limpia: sólo lo que escribe el programa. Sin
    # esto, capturar la salida de un programa significa recortar el encabezado a
    # mano, y un generador que recorta una línea es un generador que se rompe el
    # día que el encabezado cambia de formato. Es la misma razón por la que
    # cualquier herramienta de línea de órdenes tiene una bandera así.
    if not getattr(args, "salida", False):
        printer.header(f"run {path}")
    module = compile_file(path, printer)
    if module is None:
        return 1

    keys = [k for k in (args.keys or "").split(",") if k]
    try:
        result = interp.run(module, step_limit=args.max_steps,
                            max_frames=args.frames)
    except interp.StepLimitExceeded as error:
        printer.bad(f"bucle infinito: {error}")
        return 1
    except interp.RuntimeFault as error:
        printer.bad(f"fallo en runtime: {error}")
        return 1

    printer.stream.write(result.output)
    printer.stream.flush()

    if args.ppm:
        # El directorio de salida se crea aquí: pedir un PPM no debería
        # obligar a saber que existe un `dist/`.
        Path(args.ppm).parent.mkdir(parents=True, exist_ok=True)
    if args.ppm and result.display_list:
        width, height = (result.screen[:2] if result.screen else (640, 480))
        count = interp.write_ppm(args.ppm, width, height, result.display_list)
        printer.info(f"  {args.ppm}: {width}x{height}, {count} comandos")

    if args.stats:
        _print_stats(printer, module, result)
    return result.exit_code


def cmd_build(args, printer: Printer) -> int:
    paths = _sources(args)
    if not paths:
        printer.bad("no hay fuentes que compilar")
        return 2

    failures = 0
    contrato = cargar_contrato(getattr(args, "nivel", None), printer)
    for path in paths:
        printer.header(f"build {path}")
        fuente = path.read_text(encoding="utf-8")
        module = compile_file(path, printer, contrato=contrato, source=fuente)
        if module is None:
            failures += 1
            continue
        printer.good(f"  IR listo: {sir.count_instrs(module)} instrucciones")

        if args.emit in ("sir", "all"):
            target = path.with_suffix(".sir")
            target.write_text(sir.serialize(module), encoding="utf-8")
            printer.info(f"  escrito {target}")

        if args.target == "wasm" and args.emit == "all":
            try:
                raw = backend.compile_module(module)
            except CompileError as error:
                # El `except` que captura el fallo del backend **no puede
                # fallar**. `Printer` no tiene `diagnostic`, así que el
                # diagnóstico acababa sustituido por un `AttributeError` con
                # una flecha apuntando al `except`: el mensaje decía
                # `AttributeError` en vez de `E0058`. Un diagnóstico que al
                # renderizarse se cae no es un diagnóstico.
                printer.err(error.diagnostic.render(str(path), fuente))
                failures += 1
                continue
            target = path.with_suffix(".wasm")
            target.write_bytes(raw)
            printer.good(f"  escrito {target} ({len(raw)} bytes)")

        if args.release and args.target == "wasm":
            fallos = _empaquetar(path, raw, printer)
            if fallos:
                failures += fallos

        if args.stats:
            _print_stats(printer, module, None)
    return 1 if failures else 0


def _empaquetar(path: Path, wasm: bytes, printer: Printer) -> int:
    """Escribe el `dist/` de un programa, o no escribe nada.

    El presupuesto es condición y no informe: si el pegamento se ha pasado, el
    `dist/` no sale. Un `dist/` que se escribe siempre y avisa después es una
    copia con pasos extra, y anybody puede cogerlo y subirlo.
    """
    nombre = f"{path.stem}.wasm"
    try:
        ficheros = paquete.preparar(wasm, nombre)
    except AssertionError as error:
        printer.bad(str(error))
        return 1

    try:
        incumplimientos = paquete.medir_presupuestos(
            ficheros, PAQUETE_PRESUPUESTOS)
    except budget.BudgetsIlegales as error:
        printer.bad(str(error))
        return 1
    # `dist/<programa>/` y nada más: un `dist` por programa, para que compilar
    # tres ejemplos seguidos no los mezcle en un directorio con tres `main.wasm`
    # fighting por el mismo nombre.
    destino = Path(args_dist()) / path.stem
    resultado = paquete.escribir(destino, ficheros, incumplimientos)

    if not resultado.ok:
        printer.bad(f"{destino}: no se empaqueta porque no cabe")
        for fallo in incumplimientos:
            printer.err(f"  {fallo}")
        return 1

    printer.good(f"  dist en {resultado.destino}")
    for contenido in resultado.ficheros:
        printer.info(f"    {contenido.nombre}: {len(contenido.datos)} B")
    return 0


# Dónde mira `--release` los presupuestos.
#
# Es una constante y no un parámetro porque hay un `budgets.toml` por proyecto,
# pero el entorno lo puede cambiar. Eso no es sólo para el test: un `mock.patch`
# **no llega a un subproceso**, así que un test que quiere comprobar que un
# techo pasado impide el empaquetado necesita una forma de decirlo por fuera.
def _presupuestos_del_empaquetado() -> Path | None:
    """La ruta de `budgets.toml`, o `None` si hay que leerlo de otro sitio.

    `None` **significa** "no hay ruta", y es un valor que hay que dejar pasar
    intacto. Convertirlo con `str()` aquí producía `Path("None")`, que no es
    `None` y que `budget.cargar` abría como un fichero llamado `None` -- con un
    error que no dizia nada de dónde salía. Dentro del `.pyz` es justo el caso
    que falla: no hay ruta, y el fichero vive dentro del archivo.
    """
    desde_entorno = os.environ.get("SOURC_BUDGETS")
    if desde_entorno:
        return Path(desde_entorno)
    return paquete.RUTA_PRESUPUESTOS


PAQUETE_PRESUPUESTOS = _presupuestos_del_empaquetado()


def args_dist() -> str:
    """Dónde va el `dist/`.

    Se lee de `SOURC_DIST` para que un test pueda escribir en un directorio
    temporal sin ensuciar el repo. El valor por defecto es `dist`, que `make
    clean` ya borra.
    """
    return os.environ.get("SOURC_DIST", "dist")


def cmd_diff(args, printer: Printer) -> int:
    """Compara dos IR canónicos y clasifica la diferencia (ADR 0016).

    El bootstrap no compara binarios: compara este texto. Y el texto sin
    clasificar no dice nada --una diferencia que puede ser "se reordenó una
    línea" o "Stage 1 calcula otra cosa" se investiga igual de lento en los dos
    casos--, así que cada bloque se clasifica como semántica, de orden o de
    formato.
    """
    try:
        antes = Path(args.antes).read_text(encoding="utf-8")
        despues = Path(args.despues).read_text(encoding="utf-8")
    except OSError as error:
        printer.bad(f"no se pudo leer: {error}")
        return 2

    diferencias = diffir.clasificar(antes, despues)
    for diferencia in diferencias:
        printer.info(f"  {diferencia['clase']:<10} {diferencia['cabecera']}")
        if args.detallado:
            for linea in diferencia["lineas"]:
                printer.info(f"      {linea}")
    printer.info("")
    veredicto = diffir.resumen(diferencias)
    if diffir.SEMANTICA in {d["clase"] for d in diferencias}:
        printer.bad(veredicto)
        return 1
    printer.good(veredicto)
    return 0


def cmd_sir(args, printer: Printer) -> int:
    paths = _sources(args)
    if not paths:
        return 2
    for path in paths:
        module = compile_file(path, printer)
        if module is None:
            return 1
        printer.stream.write(sir.serialize(module))
    return 0


def cmd_parse(args, printer: Printer) -> int:
    """El AST en forma canónica, que es texto.

    Es el juez de la rebanada 2 del bootstrap, el parser escrito en Sourc. Como
    `sourc lex`, existe para que el bootstrap tenga contra qué mirar **antes**
    de tener implementación: un juez escrito después se ajusta al código que
    juzga y deja de juzgar.

    Se para en el primer error estructural, que es lo que hace el parser, y
    entonces no imprime árbol. Un AST a medias no se parece al bueno de ningún
    sitio, y comparar los dos mide el sitio donde se paró en vez de la forma.
    """
    from .parser import parse

    paths = _sources(args)
    if not paths:
        return 2
    fallos = 0
    for path in paths:
        fuente = path.read_text(encoding="utf-8")
        bolsa = DiagnosticBag()
        try:
            programa = parse(fuente, bolsa)
        except CompileError as error:
            printer.err(error.diagnostic.render(str(path), fuente))
            printer.bad(f"{path.name}: {error.diagnostic.code}")
            fallos += 1
            if args.salida:
                printer.stream.flush()
            continue
        astdump.volcar(programa, printer.stream)
        if args.salida:
            printer.stream.flush()
    return 1 if fallos else 0


def cmd_lex(args, printer: Printer) -> int:
    """El flujo de tokens en una forma canónica, que es texto.

    Existe para el bootstrap (Fase 8) y no para las personas: es el juez del
    **primer** trozo del compilador escrito en Source, el lexer. Sin él, la
    rebanada uno no tiene contra qué compararse, y una rebanada sin juez es
    código que se lee y no se sabe si funciona.

    El formato lleva columna, tipo, valor canónico y texto original. Los dos
    últimos están porque son cosas distintas y las dos importan: el **valor** es
    lo que consume el parser --identificadores en minúsculas-- y el **texto** es
    lo que sale en un diagnóstico. Un golden que sólo guardara el valor no
    detectaría un token que pierde mayúsculas; uno que sólo guardara el texto no
    detectaría que la canonicalización dejó de hacerse.
    """
    from sourc.lexer import tokenize

    paths = _sources(args)
    if not paths:
        return 2
    fallos = 0
    for path in paths:
        fuente = path.read_text(encoding="utf-8")
        bolsa = DiagnosticBag()
        tokens = tokenize(fuente, bolsa)
        for token in tokens:
            printer.stream.write(
                f"{token.span.start:>4} {token.span.end:>5} {token.kind:<13} "
                f"{visible(token.value)} | {visible(token.text)}\n")
        printer.stream.write(f"\n; {len(tokens)} token(s)\n")
        if args.salida:
            printer.stream.flush()
        # Los tokens se imprimen aunque haya errores: para el bootstrap un
        # programa con un error léxico sigue siendo tokenizable, y ver hasta
        # dónde llegó el lexer es justo lo que hace falta para compararlo.
        if bolsa.has_errors():
            for diagnostico in bolsa.errors:
                printer.err(diagnostico.render(str(path), fuente))
            printer.bad(f"{len(bolsa.errors)} error(es) léxicos en {path.name}")
            fallos += 1
    return 1 if fallos else 0


def cmd_test(args, printer: Printer) -> int:
    root = rutas.raiz()
    if root is None:
        printer.bad("la suite no viaja en el compilador: hace falta el repo. "
                    "Desde el `.pyz` esto no funciona, y es a propósito -- los "
                    "tests comprueban el repo, no el empaquetado.")
        return 2
    sys.path.insert(0, str(root / "tools"))
    try:
        import testharness
    except ImportError:
        printer.bad("no encuentro tools/testharness.py")
        return 2
    argv: list[str] = []
    if args.pattern:
        argv += ["-k", args.pattern]
    if args.fast:
        argv.append("--fast")
    return testharness.main(argv, printer)


def _print_stats(printer: Printer, module, result) -> None:
    histogram = sir.op_histogram(module)
    printer.info("  operaciones:")
    total = sum(histogram.values()) or 1
    for op, count in sorted(histogram.items(), key=lambda kv: -kv[1])[:12]:
        share = count * 100 // total
        printer.info(f"    {op:<24} {count:>5}  {share:>3}%")
    printer.info(f"  textos: {len(module.strings)} "
                 f"({sum(len(s) for s in module.strings)} bytes)")
    printer.info(f"  runtime: {' '.join(module.runtime_modules) or '(ninguno)'}")
    if result is not None:
        printer.info(f"  ejecuciones: {result.steps} pasos, "
                     f"{result.allocations} alocaciones, "
                     f"{result.frames_flipped} frames")


def cmd_size(args, printer: Printer) -> int:
    """Informe de pesos y **gate** contra `budgets.toml`.

    Sale con código 1 si algún techo se pasa. Un presupuesto que sólo se
    informa no aprieta nada, y este es el único sitio donde el compilador
    puede mirar su propio tamaño.
    """
    paths = _sources(args)
    if not paths:
        return 2

    raiz = rutas.raiz()
    archivo_presupuestos = rutas.ruta_de_presupuestos()
    if archivo_presupuestos is None and not rutas.DENTRO_DE_ARCHIVO:
        printer.bad("no hay presupuestos en budgets.toml")
        return 2
        return 2
    if archivo_presupuestos is None:
        # Dentro del `.pyz` el fichero está, pero no se puede abrir por ruta.
        # El parser no necesita saber de dónde vino el texto.
        tolerancia, presupuestos = budget.desde_texto(rutas.texto_de_presupuestos())
    else:
        tolerancia, presupuestos = budget.cargar(archivo_presupuestos)
    informe = budget.Informe()

    printer.header("pesos del fuente y del IR")
    total_source = 0
    total_instrs = 0
    fallos = 0
    for path in paths:
        module = compile_file(path, printer)
        if module is None:
            fallos += 1
            continue
        instrs = sir.count_instrs(module)
        total_source += path.stat().st_size
        total_instrs += instrs
        printer.info(
            f"  {path.name:<24} fuente {path.stat().st_size:>6} B   "
            f"IR {instrs:>4} instr   "
            f"datos {sum(len(s) for s in module.strings):>5} B")

        try:
            wasm = backend.compile_module(module)
        except CompileError as error:
            # La misma trampa que estaba en `cmd_build`: `Printer` no tiene
            # `diagnostic`, así que este `except` se caía él mismo y el mensaje
            # acababa siendo un `AttributeError` en vez del código. El arreglo
            # se hizo una vez y esta era la **otra** copia; por eso ahora hay un
            # test que las cuenta (ver `tests/test_contrato_diagnostico.py`).
            printer.err(error.diagnostic.render(str(path),
                                                path.read_text(encoding="utf-8")))
            fallos += 1
            continue
        budget.comprobar_programa(path, wasm, raiz, presupuestos, tolerancia,
                                  informe, module.runtime_modules)

    printer.info(f"  {'TOTAL':<24} fuente {total_source:>6} B   "
                 f"IR {total_instrs:>4} instr")

    budget.comprobar_estructura(raiz, presupuestos, informe)

    printer.info("")
    printer.header("pesos transferidos")
    if not informe.filas:
        printer.info("  nada que medir")
    for nombre, size, clase in informe.filas:
        printer.info(f"  {nombre:<24} {clase:<8} {size}")

    if informe.sin_medir:
        printer.info("")
        printer.info("  no medido:")
        for nota in informe.sin_medir:
            printer.info(f"    {nota}")

    printer.info("")
    # El veredicto va después del informe y en stdout: un gate que se lee antes
    # de los números no sirve para saber qué se pasó.
    sys.stdout.flush()
    if informe.incumplimientos:
        printer.bad(f"{len(informe.incumplimientos)} techo(s) superado(s), "
                    f"tolerancia {tolerancia:.0%}")
        for fallo in informe.incumplimientos:
            printer.bad(f"  {fallo}")
        return 1
    # El veredicto dice qué se comprobó y qué no. "Todos los techos se cumplen"
    # leído sin el bloque de arriba parece que no quedó nada sin mirar, y con
    # un artefacto de una fase futura sin crear ése es el caso: el techo existe,
    # el número no, y la diferencia entre "cumple" y "no comprobado" es justo la
    # que este gate existe para no perder.
    if informe.sin_medir:
        printer.good(f"los techos medidos se cumplen, pero "
                     f"{len(informe.sin_medir)} no se pudieron medir "
                     f"(tolerancia {tolerancia:.0%})")
    else:
        printer.good(f"todos los techos se cumplen (tolerancia {tolerancia:.0%})")
    return 1 if fallos else 0


def _sources(args) -> list[Path]:
    paths: list[Path] = []
    for raw in args.paths:
        path = Path(raw)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.src")))
        else:
            paths.append(path)
    return paths


# ------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sourc",
        description="Source — compilador de Source a WebAssembly",
    )
    parser.add_argument("--version", action="version",
                        version=f"sourc {VERSION} (lang {LANG_VERSION})")

    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="parsea y comprueba tipos")
    check.add_argument("paths", nargs="+")
    check.add_argument("--nivel", type=int, metavar="N",
                       help="exige que el programa sólo use features del "
                            "nivel N del subconjunto (subset.toml). Por "
                            "defecto no se comprueba nada.")
    check.set_defaults(func=cmd_check)

    run = sub.add_parser("run", help="ejecuta el programa en el intérprete")
    run.add_argument("paths", nargs="+")
    run.add_argument("--keys", help='teclas mantenidas, por ejemplo "w,a"')
    run.add_argument("--max-steps", type=int, default=5_000_000)
    run.add_argument("--frames", type=int,
                     help="para después de N frames (programas de juego)")
    run.add_argument("--ppm", help="escribe el último frame a un PPM")
    run.add_argument("--stats", action="store_true")
    run.add_argument("--salida", action="store_true",
                     help="stdout sólo con lo que escribe el programa, sin "
                          "encabezado: para usar sourc dentro de un script")
    run.set_defaults(func=cmd_run)

    build = sub.add_parser("build", help="compila")
    build.add_argument("paths", nargs="+")
    build.add_argument("--nivel", type=int, metavar="N",
                       help="exige que el programa sólo use features del "
                            "nivel N del subconjunto (subset.toml)")
    build.add_argument("--emit", choices=["sir", "all"], default="all",
                       help="qué artefactos escribir (el WASM llega en la Fase 2)")
    build.add_argument("--target", choices=["wasm", "js", "auto"], default="wasm")
    build.add_argument("--release", action="store_true")
    build.add_argument("--stats", action="store_true")
    build.set_defaults(func=cmd_build)

    diff = sub.add_parser("diff",
                          help="compara dos IR canónicos y clasifica la "
                               "diferencia (ADR 0016)")
    diff.add_argument("antes")
    diff.add_argument("despues")
    diff.add_argument("--detallado", action="store_true",
                      help="enseña las líneas de cada bloque")
    diff.set_defaults(func=cmd_diff)

    emit = sub.add_parser("sir", help="imprime el IR canónico")
    emit.add_argument("paths", nargs="+")
    emit.set_defaults(func=cmd_sir)

    parse_cmd = sub.add_parser(
        "parse", help="el AST en forma canónica; es el juez del parser "
                      "escrito en Source")
    parse_cmd.add_argument("paths", nargs="+")
    parse_cmd.add_argument("--salida", action="store_true",
                           help="stdout sólo con el AST, sin encabezado")
    parse_cmd.set_defaults(func=cmd_parse)

    lex = sub.add_parser("lex", help="el flujo de tokens en forma canónica; "
                                     "es el juez del lexer escrito en Source")
    lex.add_argument("paths", nargs="+")
    lex.add_argument("--salida", action="store_true",
                     help="stdout sólo con los tokens, sin encabezado")
    lex.set_defaults(func=cmd_lex)

    size = sub.add_parser("size", help="informe de pesos")
    size.add_argument("paths", nargs="+")
    size.set_defaults(func=cmd_size)

    test = sub.add_parser("test", help="ejecuta la suite de tests")
    test.add_argument("-k", dest="pattern", help="filtra por nombre de test")
    test.add_argument("--fast", action="store_true",
                      help="omite los tests de corpus")
    test.set_defaults(func=cmd_test)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    printer = Printer()
    try:
        return args.func(args, printer)
    except FileNotFoundError as error:
        printer.bad(f"no encuentro el archivo: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())