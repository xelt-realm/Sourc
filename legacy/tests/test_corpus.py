"""Corpus: programas completos contra su salida esperada.

Este es el corazón del testing diferencial (ADR P3). Cuando el backend WASM
entre en juego en la Fase 2, ejecutará estos mismos programas en WASM y en el
intérprete, y comparará las salidas byte a byte. El intérprete es el oráculo.

Para regenerar las salidas después de un cambio intencionado en la semántica:

    python3 tools/update_corpus.py
"""

import unittest
from pathlib import Path

from sourc import interp, sir
from sourc.diagnostics import CompileError, DiagnosticBag
from sourc.parser import parse
from sourc.types import check

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "corpus"
EJEMPLOS = ROOT / "examples"
EXPECTED = ROOT / "tests" / "golden" / "out"
IR = ROOT / "tests" / "golden" / "ir"


def con_ir():
    """Corpus y ejemplos: todo lo que tiene IR goldenizado.

    La salida esperada es sólo del corpus --`laberinto` tiene bucle de juego y
    sin límite de frames no termina--, pero el IR no necesita ejecutarse, así que
    cubre los dos. Es además la lista que usa `tools/parity.py`.
    """
    return sorted(CORPUS.glob("*.src")) + sorted(EJEMPLOS.glob("*.src"))


def load(path: Path):
    source = path.read_text(encoding="utf-8")
    bag = DiagnosticBag()
    try:
        program = parse(source, bag)
    except CompileError as error:
        raise AssertionError(
            f"{path.name} no parsea:\n{error.diagnostic.render(str(path), source)}"
        )
    if bag.has_errors():
        raise AssertionError(f"{path.name} no compila:\n{bag.render(str(path), source)}")

    typing = check(program, bag)
    if bag.has_errors():
        raise AssertionError(f"{path.name} no typechequea:\n{bag.render(str(path), source)}")

    module = sir.build(program, typing, path.stem, "1.0")
    return source, module


class TestCorpus(unittest.TestCase):
    """Cada programa del corpus tiene una salida esperada en disco."""

    def programs(self):
        return sorted(CORPUS.glob("*.src"))

    def test_corpus_is_not_empty(self):
        # Un corpus vacío haría pasar todos los tests sin comprobar nada.
        self.assertGreaterEqual(len(self.programs()), 12)

    def test_program(self):
        for path in self.programs():
            with self.subTest(program=path.name):
                _, module = load(path)
                expected_path = EXPECTED / (path.stem + ".txt")
                self.assertTrue(expected_path.exists(),
                                f"falta el golden {expected_path.name}")

                result = interp.run(module, step_limit=2_000_000)
                expected = expected_path.read_text(encoding="utf-8")
                self.assertEqual(result.output, expected,
                                 f"la salida de {path.name} cambió")

    def test_goldens_are_all_used(self):
        used = {p.stem for p in self.programs()}
        orphans = {p.stem for p in EXPECTED.glob("*.txt")} - used
        self.assertEqual(orphans, set(),
                         f"goldens sin programa: {sorted(orphans)}")

    def test_output_is_deterministic(self):
        """El mismo programa, cinco veces, da la misma salida y el mismo número
        de alocaciones. Sin esto no hay build cache ni bootstrap."""
        for path in self.programs():
            with self.subTest(program=path.name):
                _, module = load(path)
                first = interp.run(module, step_limit=2_000_000)
                for _ in range(4):
                    again = interp.run(module, step_limit=2_000_000)
                    self.assertEqual(again.output, first.output)
                    self.assertEqual(again.allocations, first.allocations)

    # Programas cuyo bucle es el objeto de la prueba: la reserva sólo puede
    # venir de la construcción inicial, nunca de una iteración.
    LOOP_PROGRAMS = ("11_laberinto.src", "12_juego.src")

    def test_game_loops_allocate_only_when_building_them(self):
        """La regla de oro del manifiesto, verificada sobre el corpus.

        Un bucle de juego dibuja cientos de primitivas por frame. Si reservara
        memoria en cada iteración, el recolector de basura del navegador haría
        pausas impredecibles. La regla se mide: aquí la reserva total debe ser
        exactamente la construcción inicial, una por cada array declarado.
        """
        for name in self.LOOP_PROGRAMS:
            with self.subTest(program=name):
                source, module = load(CORPUS / name)
                result = interp.run(module, step_limit=2_000_000)
                # Lo esperado es exactamente un array declarado en el fuente y
                # nada más. Contar las declaraciones es mecánico y no depende de
                # que alguien recuerde actualizar una cifra.
                declarados = source.count("ARRAY OF")
                self.assertEqual(result.allocations, declarados,
                                 f"{name} reserva {result.allocations} veces y "
                                 f"declara {declarados} arrays; toda reserva "
                                 "adicional cae dentro de un bucle")

    def test_allocation_counts_are_stable(self):
        """Ninguna reserva del corpus es inesperada.

        No es una prueba de cero alocaciones: un programa que crea diez arrays
        reserva diez veces, y eso es correcto. Lo que no debe pasar es que el
        número crezca con el número de iteraciones, y eso lo cubren los tests
        deterministas de arriba junto con los de la Fase 3.
        """
        for path in self.programs():
            with self.subTest(program=path.name):
                _, module = load(path)
                result = interp.run(module, step_limit=2_000_000)
                self.assertLessEqual(
                    result.allocations, 8,
                    f"{path.name} reserva {result.allocations} veces; revisa si "
                    "alguna reserva ha caído dentro de un bucle")


if __name__ == "__main__":
    unittest.main()

class TestIRCanónico(unittest.TestCase):
    """El IR de cada programa, pinado en disco.

    El texto canónico es el juez del bootstrap (ADR 0016): Stage 0 y Stage 1 se
    comparan sobre este fichero y no sobre binarios, porque un binario no dice
    dónde está la diferencia. Sin goldens, la comparación del bootstrap no tiene
    contra qué mirar y un bug se localiza en horas en vez de en minutos.

    También es lo que fija el comportamiento del serializador. Sin esto, un
    cambio de formato del IR --el orden de unas claves, el nombre de un campo--
    pasa los 281 tests y se descubre cuando Stage 1 emite un texto distinto.
    """

    def test_el_ir_de_cada_programa_no_ha_cambiado(self):
        for path in con_ir():
            with self.subTest(program=path.name):
                _, module = load(path)
                golden = IR / (path.stem + ".sir")
                self.assertTrue(golden.exists(),
                                f"falta el golden de IR {golden.name}; se "
                                "genera con `make corpus`")
                self.assertEqual(sir.serialize(module),
                                 golden.read_text(encoding="utf-8"),
                                 f"el IR de {path.name} cambió")

    def test_no_hay_ir_sin_programa(self):
        """Un golden huérfano es un programa borrado que nadie se dichо."""
        usados = {p.stem for p in con_ir()}
        huerfanos = {p.stem for p in IR.glob("*.sir")} - usados
        self.assertEqual(huerfanos, set(),
                         f"IR sin programa: {sorted(huerfanos)}. Si se borró "
                         "el programa, borra su IR con `make corpus`.")

    def test_los_goldens_de_ir_no_estan_ignorados(self):
        """Un golden que `.gitignore` se come no es un golden: es local.

        `.gitignore` tiene `*.sir` porque un build deja el IR por ahí, y con esa
        línea **tal cual** los 17 goldens de IR desaparecían del control de
        versiones. Los tests ensuing verde, el gate parecía existir y en otra
        máquina no había nada que comparar. Se pregunta a git en vez de leer el
        fichero de patterns, porque la pregunta es la de git.
        """
        import shutil
        import subprocess

        if shutil.which("git") is None or not (ROOT / ".git").exists():
            self.skipTest("no hay git: el estado del índice no se puede preguntar")
        for path in sorted(IR.glob("*.sir")):
            with self.subTest(golden=path.name):
                ignorar = subprocess.run(
                    ["git", "check-ignore", "-q", str(path.relative_to(ROOT))],
                    cwd=ROOT, capture_output=True)
                self.assertNotEqual(
                    ignorar.returncode, 0,
                    f"{path.name} está en .gitignore: se genera en local, no se "
                    "versiona, y el bootstrap no tiene contra qué comparar")

    def test_el_oro_del_ir_esta_poblado(self):
        """Un directorio vacío haría pasar los otros dos tests sin comprobar."""
        self.assertGreaterEqual(len(list(IR.glob("*.sir"))),
                                len(list(con_ir())) - 1,
                                "falta IR goldenizado: `make corpus`")

    def test_el_ir_es_determinista_en_todo_el_corpus(self):
        """Dos serializaciones del mismo módulo, iguales, para todo el corpus.

        `test_sir.TestDeterminism` lo comprueba con un programa. Aquí son
        diecisiete, y la diferencia es la que importa: un orden que dependa del
        hash de un dict o de un `set` puede coincidir en un programa pequeño y
        divergir en uno con muchas cadenas, donde el número de colisiones es
        otro. El corpus es justo el caso donde se ve.
        """
        for path in con_ir():
            with self.subTest(program=path.name):
                _, module = load(path)
                self.assertEqual(sir.serialize(module),
                                 sir.serialize(load(path)[1]))

    def test_el_ir_es_texto_plano_y_ascii(self):
        """Un byte alto o un `\x00` en el IR rompe la comparación del bootstrap.

        El texto canónico se compara entre Stage 0 y Stage 1, y si lleva
        caracteres no ASCII la diferencia depende de cómo los codifique cada
        entorno. Los literales del IR son bytes, y salen como `bytes=[...]`, que
        es exactamente donde un salto de codificación se colaría.
        """
        for path in con_ir():
            with self.subTest(program=path.name):
                texto = (IR / (path.stem + ".sir")).read_text(encoding="utf-8")
                self.assertNotIn("\x00", texto)
                self.assertTrue(texto.isascii(),
                                f"{path.name}.sir tiene bytes no ASCII: el "
                                "formato canónico tiene que ser ASCII para "
                                "que Stage 0 y Stage 1 comparen el mismo texto")
                self.assertTrue(texto.endswith("\n"),
                                f"{path.name}.sir no termina en salto de "
                                "línea: el formato canónico es una línea por "
                                "instrucción y siempre cierra")
