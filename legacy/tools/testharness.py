"""Arranque de la suite de tests.

Sin dependencias externas: `unittest` de la biblioteca estándar y nada más
(ADR P7, el compilador tiene que ser vendorizable).

    python3 tools/testharness.py            # todo
    python3 tools/testharness.py --fast     # sin los tests de corpus
    python3 tools/testharness.py -k sir     # sólo lo que coincida

Qué cubre cada grupo:

    test_lexer.py      tokens, números en tres bases, escapes, errores léxicos
    test_parser.py     estructura, precedencia, IF corto/bloque, FOR, NEXT
    test_diagnostics.py  el texto exacto de cada error: es la API del lenguaje
    test_sir.py        determinismo del IR, serialización canónica
    test_interp.py     semántica: operaciones, control de flujo, textos
    test_corpus.py     programas del corpus contra su salida esperada
    test_docs.py       los bloques de código de la documentación compilan
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compiler"))


def build_suite(pattern: str | None = None, fast: bool = False) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    tests = ROOT / "tests"

    if pattern:
        loader.testNamePatterns = [f"*{pattern}*"]

    discovered = loader.discover(str(tests), pattern="test_*.py", top_level_dir=str(ROOT))

    if not fast:
        return discovered

    # Los tests de corpus son los caros: compilan y ejecutan 14 programas.
    # Se quitan por módulo, que es el nivel en el que unittest los agrupa.
    # La suite viene anidada a varios niveles, así que se aplana primero: un
    # `TestSuite` no tiene `id()` y abortaría el filtrado.
    def flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                yield from flatten(item)
            else:
                yield item

    kept = [t for t in flatten(discovered) if "test_corpus" not in t.id()]
    dropped = len(list(flatten(discovered))) - len(kept)
    if dropped:
        print(f"(--fast) se omiten {dropped} tests de corpus")
    return unittest.TestSuite(kept)


class _Reporter(unittest.TestResult):
    """Salida legible y sin color cuando no hay terminal.

    Cuenta también los saltados, que es el punto (ADR 0019, decisión 5): Node
    es opcional y sus tests se saltan si no está, así que "265 tests en verde"
    en una máquina sin Node y en una con Node **no son el mismo resultado**. Si
    el panel no dice cuántos se han saltado y por qué, un entorno con menos
    garantías parece tan completo como otro con todas, y eso es justo lo que la
    decisión 5 dice que se acepta de forma explícita en vez de ocultarlo.
    """

    def __init__(self, printer):
        super().__init__()
        self.printer = printer
        self.started = 0
        # Motivo -> número de tests. Un dict y no una lista porque lo que se
        # informa es "por qué falta cobertura", no qué tests faltan.
        self.saltados: dict[str, int] = {}

    def startTest(self, test):
        self.started += 1
        self.printer.info(f"  {test.id().split('tests.')[-1]}")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.saltados[reason] = self.saltados.get(reason, 0) + 1

    def addFailure(self, test, err):
        self.failures.append((test, self._exc_info_to_string(err, test)))

    def addError(self, test, err):
        self.errors.append((test, self._exc_info_to_string(err, test)))

    @staticmethod
    def _exc_info_to_string(err, test):
        import traceback
        return "".join(traceback.format_exception(*err))


def main(argv: list[str] | None = None, printer=None) -> int:
    from sourc.cli import Printer

    printer = printer or Printer()
    parser = argparse.ArgumentParser(prog="testharness")
    parser.add_argument("-k", dest="pattern", help="filtra por nombre")
    parser.add_argument("--fast", action="store_true",
                        help="omite los tests de corpus")
    parser.add_argument("-v", action="store_true", help="detallado")
    args = parser.parse_args(argv)

    suite = build_suite(args.pattern, args.fast)
    printer.header(f"tests ({args.pattern or 'todo'})")

    reporter = _Reporter(printer)
    suite.run(reporter)

    total = reporter.started
    failures = len(reporter.failures)
    errors = len(reporter.errors)

    resumen = f"{total} tests en verde"
    if reporter.saltados:
        motivos = ", ".join(f"{motivo} x{cuantos}"
                            for motivo, cuantos in sorted(reporter.saltados.items()))
        # Aviso, no error: la suite está verde, pero con menos red que la que
        # tiene la máquina de al lado. Se dice en la última línea, que es donde
        # se lee, y no entre los nombres de los tests.
        resumen += f" · {sum(reporter.saltados.values())} saltados ({motivos})"

    if failures or errors:
        for test, detail in reporter.failures + reporter.errors:
            printer.bad(f"\nFALLO {test.id()}")
            printer.err(detail)
        printer.bad(f"\n{failures} fallo(s), {errors} error(es) de {total}")
        if reporter.saltados:
            printer.bad(f"y {sum(reporter.saltados.values())} saltados")
        return 1

    printer.good(f"\n{resumen}")
    return 0


if __name__ == "__main__":
    sys.exit(main())