"""Intérprete: la semántica del lenguaje, ejecutada.

Estos tests son el contrato de comportamiento. Cuando el backend WASM entre en
juego (Fase 2), ejecutará exactamente este mismo corpus y tendrá que coincidir
byte a byte con estas salidas (ADR P3, testing diferencial).
"""

import unittest

from sourc import interp, sir
from sourc.diagnostics import CompileError, DiagnosticBag
from sourc.parser import parse
from sourc.types import check


def run(source, keys=(), max_frames=None, max_steps=2_000_000):
    bag = DiagnosticBag()
    program = parse(source, bag)
    if bag.has_errors():
        raise AssertionError(f"el fuente de test no compila:\n{bag.render('t', source)}")
    typing = check(program, bag)
    if bag.has_errors():
        raise AssertionError(f"no typechequea:\n{bag.render('t', source)}")
    module = sir.build(program, typing)
    if keys:
        return interp.run_with_keys(module, list(keys), max_steps, max_frames)
    return interp.run(module, max_steps, max_frames)


def output(source, **kwargs):
    return run(source, **kwargs).output


class TestPrint(unittest.TestCase):
    def test_hello_world(self):
        self.assertEqual(output('PRINT "hola mundo"\n'), "hola mundo\n")

    def test_interpolation_is_resolved_at_build_time(self):
        # ADR 0013: con constantes, la interpolación no deja código de runtime.
        self.assertEqual(output('DIM n AS STRING = "mundo"\nPRINT "hola {n}"\n'),
                         "hola mundo\n")

    def test_integer_has_no_decimals(self):
        self.assertEqual(output("DIM x AS SINGLE = 2.0\nPRINT x\n"), "2\n")

    def test_concat_operator(self):
        self.assertEqual(output('PRINT "a" & "b" & "c"\n'), "abc\n")

    def test_semicolon_does_not_break_the_line(self):
        self.assertEqual(output('PRINT "a" ; "b"\nPRINT "c"\n'), "ab\nc\n")

    def test_conversion_to_text(self):
        self.assertEqual(output('DIM x AS SINGLE = 1.5\nPRINT STR(x)\n'), "1.5\n")


class TestArithmetic(unittest.TestCase):
    def test_integer_math(self):
        self.assertEqual(output("DIM r AS INTEGER = 7 DIV 2\nPRINT r\n"), "3\n")

    def test_backslash_truncates_toward_zero(self):
        self.assertEqual(output("DIM r AS INTEGER = -7 \\ 2\nPRINT r\n"), "-3\n")

    def test_mod_follows_the_dividend_sign(self):
        # `MOD` de BASIC: el signo del resto lo marca el dividendo.
        self.assertEqual(output("DIM r AS INTEGER = -7 MOD 3\nPRINT r\n"), "-1\n")

    def test_promotion_to_double(self):
        self.assertEqual(output("DIM x AS DOUBLE = 3.0 / 2.0\nPRINT x\n"), "1.5\n")

    def test_power_is_always_double(self):
        self.assertEqual(output("DIM x AS DOUBLE = 2 ^ 10\nPRINT x\n"), "1024\n")

    def test_division_by_zero_is_a_runtime_fault(self):
        bag = DiagnosticBag()
        program = parse("DIM r AS INTEGER = 1 DIV 0\nPRINT r\n", bag)
        module = sir.build(program, check(program, bag))
        with self.assertRaises(interp.RuntimeFault):
            interp.run(module, 1000)


class TestControlFlow(unittest.TestCase):
    def test_for_inclusive(self):
        self.assertEqual(output(
            "DIM t AS INTEGER = 0\n"
            "FOR i = 1 TO 4\n    t = t + i\nNEXT i\nPRINT t\n"), "10\n")

    def test_for_step_negative(self):
        self.assertEqual(output(
            "DIM t AS INTEGER = 0\n"
            "FOR i = 3 TO 1 STEP -1\n    t = t * 10 + i\nNEXT i\nPRINT t\n"),
            "321\n")

    def test_while(self):
        self.assertEqual(output(
            "DIM i AS INTEGER = 0\nWHILE i < 5\n    i = i + 1\nWEND\nPRINT i\n"),
            "5\n")

    def test_else_if_chain(self):
        source = ("DIM x AS INTEGER = 2\n"
                  "IF x = 1 THEN\n    PRINT \"uno\"\n"
                  "ELSE IF x = 2 THEN\n    PRINT \"dos\"\n"
                  "ELSE\n    PRINT \"otro\"\nEND IF\n")
        self.assertEqual(output(source), "dos\n")

    def test_while_with_break_via_flag(self):
        source = ("DIM i AS INTEGER = 0\nDIM salida AS BOOLEAN = FALSE\n"
                  "WHILE NOT salida\n    i = i + 1\n"
                  "    IF i > 4 THEN salida = TRUE\nWEND\nPRINT i\n")
        self.assertEqual(output(source), "5\n")


class TestFunctions(unittest.TestCase):
    def test_call(self):
        self.assertEqual(output(
            "FUNCTION doble(x AS INTEGER) AS INTEGER\n    RETURN x * 2\n"
            "END FUNCTION\nPRINT doble(21)\n"), "42\n")

    def test_recursion(self):
        self.assertEqual(output(
            "FUNCTION fib(n AS INTEGER) AS INTEGER\n"
            "    IF n < 2 THEN RETURN n\n"
            "    RETURN fib(n - 1) + fib(n - 2)\nEND FUNCTION\n"
            "PRINT fib(20)\n"), "6765\n")

    def test_falling_off_the_end_returns_zero(self):
        self.assertEqual(output(
            "FUNCTION nada() AS INTEGER\nEND FUNCTION\nPRINT nada()\n"), "0\n")

    def test_function_without_type_returns_nothing(self):
        bag = DiagnosticBag()
        program = parse("FUNCTION avisa()\n    PRINT \"x\"\nEND FUNCTION\n"
                        "avisa()\n", bag)
        typing = check(program, bag)
        self.assertFalse(bag.has_errors())
        module = sir.build(program, typing)
        self.assertEqual(interp.run(module).output, "x\n")


class TestStrings(unittest.TestCase):
    def test_len_is_bytes(self):
        # ADR 0003: LEN de un texto son bytes.
        self.assertEqual(output('DIM n AS INTEGER = LEN("hola")\nPRINT n\n'), "4\n")

    def test_lenu_is_codepoints(self):
        self.assertEqual(output('DIM n AS INTEGER = LEN("ñ")\nPRINT n\n'), "2\n")

    def test_len_of_array(self):
        self.assertEqual(output(
            'DIM a AS ARRAY OF STRING = ["uno", "dos"]\n'
            'DIM n AS INTEGER = LEN(a)\nPRINT n\n'), "2\n")

    def test_indexing_gives_a_character(self):
        self.assertEqual(output('PRINT "hola"[1]\n'), "o\n")

    def test_byte_compare_does_not_allocate(self):
        """La razón de ser del ADR 0004.

        El laberinto compara 15x16 caracteres por frame. Si esa comparación
        materializara un texto de un byte, el juego asignaría 14 400 veces por
        segundo. El test lo mide, no lo promete.
        """
        result = run('DIM palabra AS STRING = "hola"\n'
                     'DIM igual AS BOOLEAN = palabra[1] = "o"\n'
                     'PRINT igual\n')
        self.assertEqual(result.allocations, 0,
                         "la comparación de un carácter no debe reservar memoria")
        self.assertEqual(result.output, "TRUE\n")

    def test_general_indexing_does_allocate(self):
        # El caso no optimizado sí reserva: por eso existe CHARCODE.
        result = run('DIM c AS STRING = "hola"[1]\nPRINT c\n')
        self.assertEqual(result.allocations, 1)

    def test_charcode_does_not_allocate(self):
        result = run('DIM b AS INTEGER = CHARCODE("hola", 1)\nPRINT b\n')
        self.assertEqual(result.allocations, 0)

    def test_text_functions(self):
        source = ('PRINT LEFT("hola mundo", 4)\n'
                  'PRINT RIGHT("hola mundo", 5)\n'
                  'PRINT MID("hola mundo", 6, 5)\n'
                  'PRINT CONTAINS("hola", "ol")\n'
                  'PRINT INDEXOF("hola", "la")\n')
        self.assertEqual(output(source), "hola\nmundo\nundo\nTRUE\n2\n")

    def test_unicode_is_utf8_bytes(self):
        self.assertEqual(output('DIM n AS INTEGER = LEN("ñ")\nPRINT n\n'), "2\n")


class TestArrays(unittest.TestCase):
    def test_new_and_index(self):
        self.assertEqual(output(
            "DIM a AS ARRAY OF INTEGER = NEW INTEGER[4]\n"
            "a[0] = 10\na[2] = 30\nPRINT a[0] + a[2]\n"), "40\n")

    def test_array_of_strings(self):
        self.assertEqual(output(
            'DIM a AS ARRAY OF STRING = ["uno", "dos"]\nPRINT a[1]\n'), "dos\n")

    def test_write_and_read_back(self):
        self.assertEqual(output(
            "DIM a AS ARRAY OF INTEGER = NEW INTEGER[3]\n"
            "FOR i = 0 TO 2\n    a[i] = i * i\nNEXT i\nPRINT a[2]\n"), "4\n")


class TestTwoDimensional(unittest.TestCase):
    SOURCE = (
        'USE SOURCE_2D\n'
        'DIM mapa AS ARRAY OF STRING = ["##", "#."]\n'
        "WHILE TRUE\n"
        "    CLS RGB(0, 0, 0)\n"
        "    FOR fila = 0 TO 1\n"
        "        FOR col = 0 TO 1\n"
        '            IF mapa[fila][col] = "#" THEN '
        "RECT col * 32, fila * 32, 32, 32, RGB(40, 40, 60)\n"
        "        NEXT col\n"
        "    NEXT fila\n"
        "    CIRCLE 32, 32, 8, RGB(255, 200, 0)\n"
        "    FLIP\n"
        "WEND\n"
    )

    def test_frame_limit_stops_the_game_loop(self):
        result = run(self.SOURCE, max_frames=4)
        self.assertEqual(result.frames_flipped, 4)

    def test_the_game_loop_does_not_allocate(self):
        """El laberinto dibuja 240 comparaciones por frame. Cero de ellas debe
        reservar memoria (ADR 0004)."""
        result = run(self.SOURCE, max_frames=4)
        # Sólo la construcción inicial del mapa reserva.
        self.assertEqual(result.allocations, 1)

    def test_display_list_has_the_expected_commands(self):
        result = run(self.SOURCE, max_frames=1)
        names = [name for name, _ in result.display_list]
        self.assertEqual(names.count("rect"), 3)
        self.assertEqual(names.count("circle"), 1)

    def test_screen_is_registered(self):
        result = run('USE SOURCE_2D\nSCREEN 320, 240, "t"\n'
                     "WHILE TRUE\n    FLIP\nWEND\n", max_frames=1)
        self.assertEqual(result.screen, (320, 240, "t"))

    def test_key_reacts_to_held_keys(self):
        source = ('USE SOURCE_2D\n'
                  "DIM x AS INTEGER = 0\n"
                  'WHILE TRUE\n'
                  '    IF KEY("w") THEN x = x + 1\n'
                  "    FLIP\nWEND\n")
        with_key = run(source, keys=["w"], max_frames=3)
        without_key = run(source, max_frames=3)
        self.assertEqual(with_key.allocations, without_key.allocations)


class TestRenderer(unittest.TestCase):
    def test_writes_a_ppm(self):
        import os
        import tempfile

        result = run('USE SOURCE_2D\nSCREEN 8, 8\nCLS RGB(0,0,0)\n'
                     'RECT 0, 0, 4, 4, RGB(255, 0, 0)\n'
                     "WHILE TRUE\n    FLIP\nWEND\n", max_frames=1)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "frame.ppm")
            interp.write_ppm(path, 8, 8, result.display_list)
            data = open(path, "rb").read()
        self.assertTrue(data.startswith(b"P6\n8 8\n255\n"))
        pixels = data[data.index(b"255\n") + 4:]
        self.assertEqual(tuple(pixels[0:3]), (255, 0, 0))
        self.assertEqual(tuple(pixels[(7 * 8 + 7) * 3:(7 * 8 + 7) * 3 + 3]),
                         (0, 0, 0))


class TestLimits(unittest.TestCase):
    def test_step_limit_catches_infinite_loops(self):
        with self.assertRaises(interp.StepLimitExceeded):
            run("WHILE TRUE\nWEND\n", max_steps=5000)

    def test_flip_outside_loop_is_caught_before_running(self):
        bag = DiagnosticBag()
        program = parse("USE SOURCE_2D\nFLIP\n", bag)
        check(program, bag)
        self.assertTrue(bag.has_errors())


if __name__ == "__main__":
    unittest.main()