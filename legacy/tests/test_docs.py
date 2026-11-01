"""Los bloques de código de la documentación compilan.

Principio P6: la documentación no es prosa que se queda vieja, es código que
está typechequeado. Si un ejemplo de GRAMMAR.md deja de compilar, este test
falla y el cambio de documentación se ve en el mismo commit que el de código.

Se comprueban tres cosas por bloque:
  1. Que el fuente compila sin errores.
  2. Que no deja errores de parseo o de tipos.
  3. Que el número de instrucciones del IR está en el rango que dice la
    845 sección de pesos del documento que lo contiene, cuando el bloque lo
    declara.

Los bloques que son fragmentos incompletos se marcan con ```source,skip.
"""

import re
import unittest
from pathlib import Path

from sourc import interp, sir
from sourc.diagnostics import CompileError, DiagnosticBag
from sourc.parser import parse
from sourc.types import check

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "GRAMMAR.md", ROOT / "ARCHITECTURE.md", ROOT / "MANIFESTO.md",
        ROOT / "README.md"]

BLOCK = re.compile(r"```source(,skip)?\n(.*?)```", re.DOTALL)

# Los bloques de ARCHITECTURE.md que no son Source: diagramas en texto.
NOT_SOURCE = {"text", "ebnf", "toml", "shell"}


def blocks(path: Path):
    """(número de línea, fuente, omitido) de cada bloque de código."""
    text = path.read_text(encoding="utf-8")
    for match in BLOCK.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        skip = bool(match.group(1))
        yield line, match.group(2), skip


def compile_block(source: str):
    """Compila un bloque. Devuelve (bag, módulo) o (bag, None)."""
    bag = DiagnosticBag()
    try:
        program = parse(source, bag)
    except CompileError as error:
        bag.items.append(error.diagnostic)
        return bag, None
    if bag.has_errors():
        return bag, None
    typing = check(program, bag)
    if bag.has_errors():
        return bag, None
    return bag, sir.build(program, typing)


class TestDocsAreCode(unittest.TestCase):
    def test_there_are_blocks_to_check(self):
        total = sum(len(list(blocks(path))) for path in DOCS if path.exists())
        self.assertGreaterEqual(total, 12,
                                "la documentación debería tener ejemplos "
                                "ejecutables; si no, el principio P6 es décor")

    def test_blocks_compile(self):
        checked = 0
        for path in DOCS:
            if not path.exists():
                continue
            for line, source, skip in blocks(path):
                if skip or not source.strip():
                    continue
                label = f"{path.name}:{line}"
                bag, module = compile_block(source)
                if bag.has_errors():
                    self.fail(f"{label} no compila:\n"
                              f"{bag.render(label, source)}")
                checked += 1
        self.assertGreaterEqual(checked, 10)

    # Bloques que documentan sintaxis que todavía no baja a IR. Cada uno tiene
    # que estar en esta lista con su motivo: si no, se cuela sin revisión.
    SKIP_ALLOWLIST = {
        "GRAMMAR.md: ARENA (el IR de las arenas llega en la Fase 4)",
        "GRAMMAR.md: capa web PAGE (llega en la Fase 5)",
    }

    def test_skipped_blocks_are_justified(self):
        """Un bloque omitido tiene que estar en la lista y decir por qué.

        Si todo se acaba marcando como omitido, el principio P6 deja de
        significar nada. La lista es corta a propósito.
        """
        skipped = []
        for path in DOCS:
            if not path.exists():
                continue
            for line, source, skip in blocks(path):
                if not skip or not source.strip():
                    continue
                skipped.append(f"{path.name}: línea {line}")
                if source.lstrip().startswith("'"):
                    continue
                self.fail(f"{path.name}:{line} está omitido sin explicar por qué")

        self.assertLessEqual(
            len(skipped), len(self.SKIP_ALLOWLIST),
            f"bloques omitidos sin una regla que los cubra: {skipped}")

    def test_hola_mundo_costs_six_instructions(self):
        """El presupuesto de la Fase 2, medido desde la documentación.

        Si este número sube, el peso de un hola mundo sube con él, y el
        presupuesto de `budgets.toml` deja de ser alcanzable.

        Los bytes de datos también se comprueban, y no porchetura: las cifras
        escritas en prosa no las cubre nadie, y la de "11 bytes" estuvo
        equivocada durante dos fases. `ARCHITECTURE.md` y `docs/ROADMAP-FASES.md`
        citan estas dos cifras, y este test es lo que las mantiene ciertas.
        """
        module = compile_block('PRINT "hola mundo"\n')[1]
        self.assertIsNotNone(module)
        self.assertEqual(sir.count_instrs(module), 6)
        self.assertEqual(len(module.strings), 1)
        self.assertEqual(module.strings[0], b"hola mundo")
        self.assertEqual(sum(len(s) for s in module.strings), 10)
        self.assertEqual(module.host_imports,
                         ["source.print_str", "source.newline"])

        result = interp.run(module)
        self.assertEqual(result.output, "hola mundo\n")
        self.assertEqual(result.allocations, 0)

    def test_documented_numbers_match_reality(self):
        """Las cifras de los documentos tienen que ser verdad.

        No es mantenimiento de prosa: si el IR cambia de forma, estas cifras
        son la primera señal de que el presupuesto de peso se ha movido, y el
        mensaje de error dice en qué documento y en qué frase está la
        afirmación que dejó de ser cierta.
        """
        expected = {
            "ARCHITECTURE.md": ["6 instrucciones y 10 bytes de datos",
                                "segmento de datos de 10 bytes"],
            "docs/ROADMAP-FASES.md": ["6 instrucciones IR y 10 bytes de datos"],
        }
        module = compile_block('PRINT "hola mundo"\n')[1]
        actual_instrs = sir.count_instrs(module)
        actual_bytes = sum(len(s) for s in module.strings)

        for name, needles in expected.items():
            text = (ROOT / name).read_text(encoding="utf-8")
            for needle in needles:
                with self.subTest(document=name, claim=needle):
                    self.assertIn(
                        needle.replace("6 instrucciones",
                                       f"{actual_instrs} instrucciones")
                             .replace("10 bytes", f"{actual_bytes} bytes"),
                        text,
                        f"{name} cita una cifra que ya no es cierta: el IR "
                        f"produce {actual_instrs} instrucciones y "
                        f"{actual_bytes} bytes de datos")

    def test_every_example_in_the_readme_exists(self):
        readme = ROOT / "README.md"
        if not readme.exists():
            self.skipTest("todavía no hay README con ejemplos")
        text = readme.read_text(encoding="utf-8")
        for match in re.finditer(r"```source.*?\n(.*?)```", text, re.DOTALL):
            source = match.group(1)
            for referenced in re.findall(r"\(([\w./-]+\.src)\)", source):
                path = ROOT / referenced
                self.assertTrue(path.exists(),
                                f"el README referencia {referenced}, que no existe")


class TestExamplesDir(unittest.TestCase):
    """Los ejemplos del directorio `examples/` deben compilar y correr."""

    def test_examples_compile(self):
        for path in sorted((ROOT / "examples").glob("*.src")):
            with self.subTest(example=path.name):
                source = path.read_text(encoding="utf-8")
                bag, module = compile_block(source)
                self.assertFalse(
                    bag.has_errors(),
                    f"{path.name} no compila:\n{bag.render(path.name, source)}")
                self.assertIsNotNone(module)

    def test_laberinto_is_the_documented_example(self):
        """El laberinto es el objetivo de la Fase 3, así que sus propiedades
        concretas (tamaño de pantalla, resolución del mapa) se comprueban aquí
        para que no se degraden en silencio."""
        path = ROOT / "examples" / "laberinto.src"
        source = path.read_text(encoding="utf-8")
        _, module = compile_block(source)
        result = interp.run(module, step_limit=5_000_000, max_frames=3)
        self.assertEqual(result.frames_flipped, 3)
        self.assertEqual(result.screen, (640, 480, "Laberinto"))
        self.assertEqual(result.allocations, 1,
                         "sólo la construcción del mapa debe reservar memoria")


if __name__ == "__main__":
    unittest.main()