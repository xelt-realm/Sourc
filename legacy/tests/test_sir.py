"""SIR: determinismo, serialización canónica y lashape del IR.

Lo que se prueba aquí es lo que hace posible el auto-hospedaje. Si el IR no es
determinista, Stage 0 y Stage 1 no se pueden comparar, y el bootstrap se
convierte en una adivinanza (ADR 0016).
"""

import unittest

from sourc import sir
from sourc.diagnostics import CompileError, DiagnosticBag
from sourc.parser import parse
from sourc.types import check

HOLA = 'PRINT "hola mundo"\n'


def build(source, name="main"):
    bag = DiagnosticBag()
    program = parse(source, bag)
    if bag.has_errors():
        raise AssertionError(f"el fuente de test no compila:\n{bag.render('t', source)}")
    typing = check(program, bag)
    if bag.has_errors():
        raise AssertionError(f"el fuente de test no typechequea:\n{bag.render('t', source)}")
    return sir.build(program, typing, name, "1.0")


class TestHoloMundo(unittest.TestCase):
    """El objetivo de la Fase 2, medido desde el IR.

    `PRINT "hola mundo"` debería ser un segmento de datos y una llamada al host.
    Todo lo que aparezca de más aquí es tamaño que pays el usuario final.
    """

    def setUp(self):
        self.module = build(HOLA)

    def test_instruction_count(self):
        # 2 para la constante de texto, 1 para imprimir, 1 salto de línea,
        # 1 constante del código de salida, 1 ret.
        self.assertEqual(sir.count_instrs(self.module), 6)

    def test_string_is_a_single_data_segment(self):
        self.assertEqual(len(self.module.strings), 1)
        self.assertEqual(self.module.strings[0], b"hola mundo")

    def test_host_imports_are_only_printing(self):
        """La mecánica de pesos, medida en el programa más pequeño posible.

        Un hola mundo no descarga nada del runtime de dibujo. Si este test
        falla, es que la mecánica de carga condicional se ha roto.

        Son dos imports y no uno porque `newline` es una llamada al host: WASM
        no tiene ninguna instrucción de salto de línea, y escribir un byte en
        memoria desde el módulo obligaría al glue a conocer la disposición de
        los datos, que es justo lo que el IR no debería filtrar.
        """
        self.assertEqual(self.module.host_imports,
                         ["source.print_str", "source.newline"])

    def test_no_runtime_modules(self):
        # La mecánica de pesos: un programa que sólo imprime no arrastra
        # ningún módulo del runtime.
        self.assertEqual(self.module.runtime_modules, [])


class TestDeterminism(unittest.TestCase):
    """Requisito duro del bootstrap (ADR 0016)."""

    SOURCE = ('DIM tabla AS ARRAY OF SINGLE = NEW SINGLE[8]\n'
              'DIM total AS DOUBLE = 0.0\n'
              'FOR i = 0 TO LEN(tabla) - 1\n'
              '    total = total + tabla[i]\n'
              'NEXT i\n'
              'PRINT "total: " & STR(total)\n')

    def test_same_source_same_bytes(self):
        first = sir.serialize(build(self.SOURCE))
        for _ in range(20):
            self.assertEqual(sir.serialize(build(self.SOURCE)), first)

    def test_string_constants_are_deduplicated(self):
        module = build('PRINT "a" & "a" & "a"\n')
        self.assertEqual(len(module.strings), 1)

    def test_float_serializes_by_bits(self):
        """Un flotante se serializa por su patrón de 64 bits, no por su
        representación decimal: `0.1` escrito de tres maneras distintas da el
        mismo IR en cualquier plataforma."""
        module = build("DIM a AS DOUBLE = 0.1\nDIM b AS DOUBLE = 1e-1\n")
        text = sir.serialize(module)
        bits = [line.split()[2] for line in text.splitlines()
                if line.strip().startswith("const.f64")]
        self.assertEqual(len(set(bits)), 1, f"bits distintos: {bits}")

    def test_op_histogram_is_sorted(self):
        histogram = sir.op_histogram(build(HOLA))
        self.assertEqual(list(histogram), sorted(histogram))


class TestRendering(unittest.TestCase):
    def test_values_carry_their_type(self):
        text = sir.serialize(build(HOLA))
        self.assertIn("%0:i64", text)
        self.assertIn("%1:str", text)

    def test_arrays_show_their_element_type(self):
        text = sir.serialize(build("DIM a AS ARRAY OF STRING = [\"x\"]\n"))
        self.assertIn("a: ARRAY OF STRING", text)

    def test_call_native_shows_a_list(self):
        module = build('USE SOURCE_2D\nRECT 0, 0, 1, 1, RGB(1, 2, 3)\n')
        text = sir.serialize(module)
        self.assertIn("call.native rect [", text)


class TestStructura(unittest.TestCase):
    def test_loop_declares_its_variable_as_a_slot(self):
        module = build("FOR i = 0 TO 3\nNEXT\n")
        self.assertEqual(module.funcs[0].slot_index["i"], 0)

    def test_function_becomes_its_own_module_function(self):
        module = build(
            "FUNCTION doble(x AS INTEGER) AS INTEGER\n    RETURN x * 2\n"
            "END FUNCTION\nDIM r AS INTEGER = doble(21)\n")
        names = [f.name for f in module.funcs]
        self.assertEqual(sorted(names), ["doble", "main"])
        # main es la última: su índice es len(funcs) - 1, constante.
        self.assertEqual(module.funcs[-1].name, "main")

    def test_labels_are_resolved(self):
        func = build("USE SOURCE_2D\nWHILE TRUE\n    FLIP\nWEND\n").funcs[0]
        positions = func.labels()
        self.assertEqual(len(positions), 2)
        for label, position in positions.items():
            self.assertEqual(func.instrs[position].op, "label")
            self.assertEqual(func.instrs[position].args[0], label)

    def test_unimplemented_constructs_fail_loudly(self):
        """Un constructo aún no soportado debe decir que lo está.

        Preferimos un error explícito a emitir un IR incorrecto que se descubre
        tres fases después.
        """
        with self.assertRaises(CompileError) as caught:
            build("salto:\nGOTO salto\n")
        self.assertIn("aún no bajan a IR", caught.exception.diagnostic.message)

        with self.assertRaises(CompileError):
            build("ARENA f\nDIM b AS ARRAY OF SINGLE = NEW SINGLE[4]\nEND ARENA\n")


class TestByteCompare(unittest.TestCase):
    """ADR 0004: comparar `s[i]` con un literal no debe asignar nada."""

    SOURCE = ('DIM palabra AS STRING = "hola"\n'
              'DIM igual AS BOOLEAN = palabra[1] = "o"\n')

    def test_lowered_to_a_byte_comparison(self):
        text = sir.serialize(build(self.SOURCE))
        self.assertIn("str.eq_byte", text)
        # El camino sin optimizar habría materializado un texto de un byte.
        self.assertNotIn("str.sub", text)

    def test_general_index_still_materializes(self):
        """Sin literal al otro lado, `s[i]` sí devuelve un texto de un byte."""
        text = sir.serialize(build('DIM c AS STRING = "hola"[1]\n'))
        self.assertIn("str.sub", text)

    def test_multi_byte_literal_is_not_a_byte_comparison(self):
        text = sir.serialize(build('DIM p AS STRING = "hola"\n'
                                   'DIM igual AS BOOLEAN = p[1] = "ol"\n'))
        self.assertNotIn("str.eq_byte", text)


if __name__ == "__main__":
    unittest.main()