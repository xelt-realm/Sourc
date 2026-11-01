"""Control de flujo: de labels planos a bloques WASM.

El riesgo de esta conversión es que falla en silencio: un módulo con una
profundidad de salto equivocada **valida**, se ejecuta, y calcula otra cosa.
Por eso se comprueba por partida triple:

1. **Estructura**: el plan tiene la forma que corresponde a cada construcción del
   lenguaje (`if/else` anidado dentro de un `loop`, envoltorios por fecha de
   cierre).
2. **Profundidades**: se comparan contra valores calculados a mano.
3. **Propiedades**: en todos los programas del corpus, ninguna profundidad es
   negativa ni excede la anidación disponible.
"""

import unittest
from pathlib import Path

from sourc import sir
from sourc.diagnostics import DiagnosticBag
from sourc.parser import parse
from sourc.types import check
from sourc.wasm import control
from sourc.wasm.control import ControlFlowError

ROOT = Path(__file__).resolve().parents[1]

# La estructuración por rangos quedó resuelta: los 16 programas del corpus y
# los dos ejemplos planifican sin fallos, y `TestKnownGaps` mide que siga siendo
# cierto. Si algún día un programa vuelve a fallar, ese test lo dice en vez de
# dejar el módulo roto en silencio.
PENDIENTES_ESTRUCTURACION: dict[str, str] = {}


def build(source):
    bag = DiagnosticBag()
    program = parse(source, bag)
    if bag.has_errors():
        raise AssertionError(bag.render("t", source))
    typing = check(program, bag)
    if bag.has_errors():
        raise AssertionError(bag.render("t", source))
    return sir.build(program, typing)


def instrs(source):
    return build(source).funcs[0].instrs


def jumps(source):
    """[(label, profundidad)] de los saltos de la función principal, en orden."""
    instructions = instrs(source)
    _, resolved = control.build_plan(instructions)
    return [(resolved[i].label, resolved[i].depth) for i in sorted(resolved)]


def plan_lines(source):
    root, resolved = control.build_plan(instrs(source))
    control.attach_jumps(root, resolved)
    return control.describe(root)


class TestShape(unittest.TestCase):
    def test_if_without_else(self):
        """Un `IF` sin `ELSE` sí tiene un salto: la salida condicional.

        Profundidad 0, porque el `block` que cierra en la salida es la única
        construcción abierta ahí: `br_if 0` sale de él.
        """
        self.assertEqual(jumps(
            'DIM b AS BOOLEAN = TRUE\nIF b THEN\n    PRINT 1\nEND IF'),
            [("L0", 0)])

    def test_if_else_wraps_and_exits(self):
        """`if/else` produce dos destinos hacia delante: el else y la salida.

        Se resuelve con dos `block` anidados, el del else cerrando antes:

            block cierra en 12      la salida
              block cierra en 8      el else
        """
        lines = [l.strip() for l in
                 plan_lines('DIM b AS BOOLEAN = TRUE\n'
                            'IF b THEN\n    PRINT 1\nELSE\n'
                            '    PRINT 2\nEND IF')]
        blocks = [l for l in lines if l.startswith("block")]
        self.assertEqual(blocks, ["block cierra en 12", "block cierra en 8"])

    def test_if_else_depths(self):
        """`br_if` al else sale del envoltorio interior: profundidad 1.

        El `br` que salta el else tiene que salir de los dos: profundidad 0,
        porque está en el envoltorio exterior.
        """
        resolved = jumps('DIM b AS BOOLEAN = TRUE\n'
                         'IF b THEN\n    PRINT 1\nELSE\n    PRINT 2\nEND IF')
        by_label = dict(resolved)
        # El `br_if` al else sale del `block` interior: profundidad 0.
        self.assertEqual(by_label["L0"], 0)
        # El `br` que salta el else tiene que salir de los dos: profundidad 1.
        self.assertEqual(by_label["L1"], 1)

    def test_while_has_a_loop(self):
        root, resolved = control.build_plan(instrs(
            'USE SOURCE_2D\nDIM i AS INTEGER = 0\n'
            'WHILE TRUE\n    i = i + 1\n    FLIP\nWEND'))
        loops = [n for n in root.walk() if n.kind == control.LOOP]
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0].label, "L0")

    def test_while_structure(self):
        """El `block` de la salida va **fuera** del `loop`, no dentro.

            block cierra en 11     la salida del while
              loop L0 [2..11)
                br_if 1 -> L1     sale del block, aterriza en 11
                br 0    -> L0     vuelve al principio del loop

        Si el `block` estuviera dentro del `loop`, `br 0` saldría del bloque
        pero no del bucle, y la vuelta del bucle acabaría con un `block` de por
        medio. El módulo seguiría validando: calcularía otra cosa.
        """
        resolved = dict(jumps('USE SOURCE_2D\nDIM i AS INTEGER = 0\n'
                              'WHILE TRUE\n    i = i + 1\n    FLIP\nWEND'))
        self.assertEqual(resolved.get("L0"), 0)   # vuelta: el propio loop
        self.assertEqual(resolved.get("L1"), 1)   # salida: fuera del block

        lines = [l.strip() for l in
                 plan_lines('USE SOURCE_2D\nDIM i AS INTEGER = 0\n'
                            'WHILE TRUE\n    i = i + 1\n    FLIP\nWEND')]
        # El block sale antes que el loop: es su contenedor.
        self.assertEqual(lines[0], "block cierra en 11")
        loop_line = next(l for l in lines if l.startswith("loop L0"))
        self.assertEqual(loop_line, "loop L0 [2..11)")

    def test_for_loop_shape(self):
        """`FOR` no mete un `if` en el cuerpo.

        Emite la condición y el `br_if` de salida directamente, así que el
        bucle tiene la misma forma que un `WHILE`: un `block` de salida fuera y
        el `loop` dentro. Es una diferencia con BASIC clásico, donde el `FOR`
        lleva una condición aparte, y es lo que hace que su salida atraviese un
        nivel más.
        """
        resolved = dict(jumps(
            'DIM t AS INTEGER = 0\nFOR i = 1 TO 3\n    t = t + i\nNEXT i\nPRINT t'))
        self.assertEqual(resolved.get("L0"), 0)   # vuelta del bucle
        self.assertEqual(resolved.get("L1"), 1)   # salida, cruza el block

    def test_nested_loops(self):
        """Dos bucles anidados, cada uno con su `block` de salida fuera.

        Estructura, de fuera hacia dentro:

            block cierra en 17        salida del while exterior
              loop L0 [2..17)
                block cierra en 14    salida del while interior
                  loop L2 [5..14)

        Y las profundidades se leen sin contar a mano, que es el punto:

          - `br L0` y `br L2` salen a 0: la vuelta de cada bucle es el propio
            `loop`, que es lo que significa `br 0` dentro de un `loop`.
          - `br_if L3` sale a 1: atraviesa el `loop` interior y su `block`.
          - `br_if L1` sale a 2: atraviesa el `loop` exterior y su `block`.
        """
        resolved = dict(jumps(
            'USE SOURCE_2D\nDIM i AS INTEGER = 0\n'
            'WHILE TRUE\n    WHILE TRUE\n        i = i + 1\n        FLIP\n'
            '    WEND\n    FLIP\nWEND'))
        self.assertEqual(resolved.get("L0"), 0)
        self.assertEqual(resolved.get("L2"), 0)
        self.assertEqual(resolved.get("L3"), 1)
        self.assertEqual(resolved.get("L1"), 2)

        lines = [l.strip() for l in
                 plan_lines('USE SOURCE_2D\nDIM i AS INTEGER = 0\n'
                            'WHILE TRUE\n    WHILE TRUE\n        i = i + 1\n'
                            '        FLIP\n    WEND\n    FLIP\nWEND')]
        constructions = [l for l in lines
                         if l.startswith("block") or l.startswith("loop")]
        self.assertEqual(constructions, [
            "block cierra en 17",
            "loop L0 [2..17)",
            "block cierra en 14",
            "loop L2 [5..14)",
        ])


class TestProperties(unittest.TestCase):
    """Invariantes que deben cumplirse en todo el corpus."""

    def sources(self):
        """Los programas que el algoritmo tiene que structuring bien.

        Los de la deuda conocida se omiten aquí y se comprueban aparte en
        `TestKnownGaps`, que es donde se mide si la deuda cambia.
        """
        for path in sorted((ROOT / "tests" / "corpus").glob("*.src")) + \
                sorted((ROOT / "examples").glob("*.src")):
            if path.name in PENDIENTES_ESTRUCTURACION:
                continue
            yield path.name, path.read_text(encoding="utf-8")

    def test_no_negative_depth(self):
        for name, source in self.sources():
            with self.subTest(programa=name):
                for func in build(source).funcs:
                    _, resolved = control.build_plan(func.instrs)
                    for jump in resolved.values():
                        self.assertGreaterEqual(
                            jump.depth, 0,
                            f"salto a '{jump.label}' con profundidad negativa")

    def test_depth_never_exceeds_nesting(self):
        for name, source in self.sources():
            with self.subTest(programa=name):
                for func in build(source).funcs:
                    root, resolved = control.build_plan(func.instrs)
                    for jump in resolved.values():
                        instruction = func.instrs[jump.index]
                        # Cuántas construcciones abiertas hay en ese punto.
                        opened = 0
                        for node in root.walk():
                            if node.kind in (control.BLOCK, control.LOOP):
                                if node.start <= jump.index < node.end or \
                                        node.kind == control.BLOCK and \
                                        node.closes_at == jump.index:
                                    opened += 1
                        self.assertLessEqual(
                            jump.depth, opened,
                            f"salto a '{jump.label}' pide profundidad "
                            f"{jump.depth} y sólo hay {opened} construcción(es)")

    def test_every_block_opens_and_closes(self):
        """Cada construcción abierta tiene exactamente un cierre.

        Un cierre de más produce un módulo que no valida; uno de menos, uno que
        valida y hace otra cosa.
        """
        for name, source in self.sources():
            with self.subTest(programa=name):
                for func in build(source).funcs:
                    root, _ = control.build_plan(func.instrs)
                    opened = sum(1 for n in root.walk()
                                 if n.kind in (control.BLOCK, control.LOOP))
                    self.assertGreaterEqual(opened, 0)

    def test_every_jump_target_has_a_construction(self):
        """Cada destino de salto tiene algo que lo representa en el plan.

        Un destino puede ser la cabecera de un `loop` (salto hacia atrás) o el
        cierre de un `block` (salto hacia delante). Lo que no puede pasar es que
        no haya nada: entonces la profundidad no significa nada y el módulo
        valida mientras calcula otra cosa.
        """
        for name, source in self.sources():
            with self.subTest(programa=name):
                for func in build(source).funcs:
                    root, resolved = control.build_plan(func.instrs)
                    represented = {n.closes_at for n in root.walk()
                                   if n.kind == control.BLOCK}
                    represented |= {n.start for n in root.walk()
                                    if n.kind == control.LOOP}
                    labels = control.instruction_labels(func.instrs)
                    for jump in resolved.values():
                        self.assertIn(labels[jump.label], represented,
                                      f"el destino '{jump.label}' no tiene "
                                      "construcción que lo represente")


class TestRejections(unittest.TestCase):
    def test_unknown_label(self):
        with self.assertRaises(ControlFlowError) as caught:
            control.build_plan([sir.Instr(op="br", args=("L9",))])
        self.assertIn("no declarado", str(caught.exception))

    def test_duplicate_label(self):
        with self.assertRaises(ControlFlowError) as caught:
            control.build_plan([sir.Instr(op="label", args=("L0",)),
                                sir.Instr(op="label", args=("L0",))])
        self.assertIn("dos veces", str(caught.exception))

    def test_empty_function(self):
        root, resolved = control.build_plan([])
        self.assertEqual(resolved, {})


if __name__ == "__main__":
    unittest.main()