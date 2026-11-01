"""Diagnósticos: el texto de cada error es parte de la API del lenguaje.

Un compilador con errores vagos es un compilador que la gente no usa. Estos
tests congelan los mensajes, las sugerencias y las posiciones, porque cambiar
"¿quiso decir X?" por "quizás quisiste decir X?" es un cambio de API.

Cuando se cambia un mensaje a propósito, hay que actualizar el snapshot aquí y
anotarlo en el commit: es una decisión de producto, no un refactor.
"""

import unittest

from sourc.diagnostics import DiagnosticBag, edit_distance, suggest
from sourc.parser import parse
from sourc.types import check


def compile_source(source: str):
    """Compila y devuelve (errores, avisos) como diagnostics."""
    bag = DiagnosticBag()
    program = parse(source, bag)
    if bag.has_errors():
        return bag
    check(program, bag)
    return bag


def codes(bag):
    return sorted(d.code for d in bag.errors)


class TestUndeclared(unittest.TestCase):
    def test_assignment_to_undeclared(self):
        # ADR 0002: la regla más importante para quien viene de QBasic.
        bag = compile_source("y = 5")
        self.assertEqual(codes(bag), ["E0022"])
        self.assertEqual(len(bag.errors), 1, "el mismo error no se repite")
        self.assertIn("DIM y", bag.errors[0].suggestion)
        self.assertIn("ADR 0002", bag.errors[0].suggestion)

    def test_suggestion_for_typo(self):
        bag = compile_source("DIM ancho AS INTEGER = 1\nPRINT anclho")
        self.assertIn("¿quiso decir 'ancho'?", bag.errors[0].suggestion)

    def test_position_is_the_identifier(self):
        bag = compile_source("PRINT 1\nPRINT 2\nindefinido")
        diagnostic = bag.errors[0]
        self.assertEqual(diagnostic.span.start, 16)


class TestTypeErrors(unittest.TestCase):
    def test_integer_is_not_a_boolean(self):
        # Sin verdad implícita: el error sugiere la comparación explícita.
        bag = compile_source("DIM x AS INTEGER = 1\nIF x THEN PRINT 1")
        self.assertIn("E0024", codes(bag))
        self.assertIn("<> 0", bag.errors[0].suggestion)

    def test_plus_on_strings_is_rejected(self):
        # ADR 0013: la concatenación es `&`, `+` es aritmética.
        bag = compile_source('DIM a AS STRING = "x"\nDIM b AS STRING = a + "y"')
        self.assertIn("E0021", codes(bag))
        self.assertIn("&", bag.errors[0].suggestion)

    def test_missing_use_module(self):
        # ADR 0010: la dependencia es explícita, y por eso el peso se puede
        # decidir en compilación.
        bag = compile_source('RECT 0, 0, 10, 10, RGB(1,2,3)')
        self.assertIn("E0060", codes(bag))
        self.assertIn("USE SOURCE_2D", bag.errors[0].suggestion)

    def test_index_out_of_type(self):
        bag = compile_source("DIM x AS INTEGER = 0\nDIM y AS INTEGER = x[0]")
        self.assertIn("E0029", codes(bag))

    def test_calling_a_variable(self):
        bag = compile_source("DIM f AS INTEGER = 0\nDIM y AS INTEGER = f(1)")
        self.assertIn("E0037", codes(bag))
        self.assertIn("quita los paréntesis", bag.errors[0].suggestion)

    def test_wrong_arity_shows_the_signature(self):
        bag = compile_source(
            "FUNCTION suma(a AS INTEGER, b AS INTEGER) AS INTEGER\n"
            "    RETURN a + b\nEND FUNCTION\n"
            "DIM r AS INTEGER = suma(1)\n")
        self.assertIn("E0036", codes(bag))
        self.assertIn("a AS INTEGER, b AS INTEGER", bag.errors[0].suggestion)


class TestWarnings(unittest.TestCase):
    def test_goto_warns(self):
        bag = compile_source("salto:\nGOTO salto")
        self.assertEqual([w.code for w in bag.warnings], ["W0001"])

    def test_loop_without_flip_warns(self):
        bag = compile_source(
            "USE SOURCE_2D\nDIM i AS INTEGER = 0\n"
            "WHILE i < 10\n    i = i + 1\nWEND")
        self.assertIn("W0004", [w.code for w in bag.warnings])

    def test_loop_with_flip_does_not_warn(self):
        bag = compile_source(
            "USE SOURCE_2D\nDIM i AS INTEGER = 0\n"
            "WHILE i < 10\n    i = i + 1\n    FLIP\nWEND")
        self.assertNotIn("W0004", [w.code for w in bag.warnings])

    def test_inner_loop_does_not_warn(self):
        """Un bucle de dibujo dentro del bucle de juego no presenta nada.

        Si avisáramos también, cada programa con un `FOR` de celdas dentro de
        un `WHILE` produciría un aviso falso por cada celda.
        """
        bag = compile_source(
            "USE SOURCE_2D\n"
            "WHILE TRUE\n"
            "    FOR fila = 0 TO 8\n"
            "        RECT fila, fila, 10, 10, RGB(1,2,3)\n"
            "    NEXT fila\n"
            "    FLIP\n"
            "WEND")
        self.assertNotIn("W0004", [w.code for w in bag.warnings])

    def test_flip_outside_loop_is_an_error(self):
        bag = compile_source("USE SOURCE_2D\nFLIP")
        self.assertIn("E0051", codes(bag))


class TestRendering(unittest.TestCase):
    def test_render_has_position_caret_and_note(self):
        source = "PRINT 1\ny = 5\n"
        bag = compile_source(source)
        rendered = bag.render("prueba.src", source)
        self.assertIn("prueba.src:2:1:", rendered)
        self.assertIn("^", rendered)
        self.assertIn("= nota:", rendered)

    def test_diagnostics_are_sorted_deterministically(self):
        source = "PRINT zzz\nDIM x AS INTEGER = 1\nPRINT aaa\nPRINT mmm\n"
        first = [d.code + str(d.span.start) for d in compile_source(source).sorted()]
        second = [d.code + str(d.span.start) for d in compile_source(source).sorted()]
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first, key=lambda t: int(t.split("E")[-1])))


class TestSuggestionHelper(unittest.TestCase):
    def test_edit_distance(self):
        self.assertEqual(edit_distance("abc", "abc"), 0)
        self.assertEqual(edit_distance("abc", "abd"), 1)
        self.assertEqual(edit_distance("hola", "hola mundo"), 4)

    def test_suggest_respects_the_limit(self):
        self.assertIsNone(suggest("xyzzy", ["abc", "def"]))
        self.assertEqual(suggest("holaa", ["hola"]), "hola")

    def test_suggest_is_deterministic(self):
        candidates = ["cola", "casa", "cena"]
        results = {suggest("cosa", candidates) for _ in range(20)}
        self.assertEqual(len(results), 1)


class TestSubconjunto(unittest.TestCase):
    """`E0066`: el único código que no habla del lenguaje sino del contrato.

    Los diagnósticos son la API del repositorio (AGENTS.md), y éste se lee más
    que otros: es el que le dice a alguien que escribe el compilador por qué no
    puede usar algo. Un texto vago aquí se traduce en una búsqueda a ciegas.
    """

    def setUp(self):
        from pathlib import Path
        self.raiz = Path(__file__).resolve().parents[1]

    def _viola(self, nombre):
        from sourc import subset
        from sourc.diagnostics import DiagnosticBag
        from sourc.parser import parse

        contrato = subset.cargar(self.raiz / "subset.toml")
        bag = DiagnosticBag()
        programa = parse((self.raiz / "tests" / "subset" / "mal" / nombre)
                         .read_text(encoding="utf-8"), bag)
        return subset.violaciones(programa, contrato)

    def test_el_codigo_es_estable(self):
        """Cambiar `E0066` rompe el texto snapshotteado y a quien lo compruebe."""
        from sourc import subset
        self.assertEqual(subset.CODIGO_FUERA_DE_NIVEL, "E0066")

    def test_el_texto_del_error_esta_snapshotteado(self):
        self.assertEqual(
            [d.message for d in self._viola("01_arena.src")],
            ["`arena` está por encima del nivel 2"])

    def test_la_nota_dice_el_por_que_y_el_coste(self):
        """La nota lleva el motivo **y** lo que cuesta trabajar sin la feature.

        El motivo explica por qué no está; el coste explica qué se está pagando
        mientras tanto. Sin el coste, la nota dice que no se puede y no dice qué
        se está pagando, y entonces la Fase 4 entra por presión.
        """
        notas = [d.suggestion for d in self._viola("03_campo.src")]
        self.assertTrue(notas)
        for nota in notas:
            self.assertIn("nivel 2 no admite `campo`", nota)
            self.assertIn("mientras tanto", nota)
            self.assertIn("arrays paralelos", nota)

    def test_cada_uso_da_un_diagnostico(self):
        """Uno por uso, no uno por feature: la línea es lo que hay que arreglar.

        Un compilador con doce ficheros que usa `ARENA` en cuarenta sitios
        necesita los cuarenta, porque si no el mensaje dice qué feature y no
        dónde.
        """
        from sourc.diagnostics import CompileError, DiagnosticBag
        from sourc import subset
        from sourc.parser import parse

        contrato = subset.cargar(self.raiz / "subset.toml")
        bag = DiagnosticBag()
        fuente = ("ARENA a\n  PRINT 1\nEND ARENA\n"
                  "ARENA b\n  PRINT 2\nEND ARENA\n")
        try:
            programa = parse(fuente, bag)
        except CompileError:                          # pragma: no cover
            self.skipTest("ARENA ha dejado de parsear: mueva el fixture")
        diagnosticos = subset.violaciones(programa, contrato)
        self.assertEqual(len(diagnosticos), 2)
        posiciones = {(d.span.start, d.span.end) for d in diagnosticos}
        self.assertEqual(len(posiciones), 2, "los dos usos comparten posición")

    def test_un_programo_en_el_nivel_no_da_ningun_diagnostico(self):
        from sourc.diagnostics import DiagnosticBag
        from sourc import subset
        from sourc.parser import parse

        contrato = subset.cargar(self.raiz / "subset.toml")
        bag = DiagnosticBag()
        programa = parse(
            (self.raiz / "tests" / "subset" / "ok" / "01_datos.src")
            .read_text(encoding="utf-8"), bag)
        self.assertEqual(subset.violaciones(programa, contrato), [])


if __name__ == "__main__":
    unittest.main()

class TestGlobalesEnFunciones(unittest.TestCase):
    """`E0067`: una FUNCTION no puede tocar un global, y hay que saber por qué.

    Este no es un detalle de implementación sino un hole con tres respuestas
    distintas. Los globales de un programa viven en la memoria lineal del módulo,
    y en WASM **cada función tiene sus propios locales**: no hay ninguna manera de
    que `f4` lea el local que `main` usó para `v2`. Con la rejected implícita,
    el typechecker aceptaba el programa, el módulo imprimía `0` y el intérprete
    imprimía el valor del global. Tres respuestas, ninguna documentada.

    Un diagnóstico es mejor que las tres: la regla 3 de AGENTS.md dice que los
    diagnósticos son API, y un error con la corrección incluida es más útil que
    un `0` que nadie sabe interpretar.
    """

    def _error(self, fuente: str):
        from sourc.diagnostics import DiagnosticBag
        from sourc.parser import parse
        from sourc.types import check

        bag = DiagnosticBag()
        programa = parse(fuente, bag)
        if not bag.has_errors():
            check(programa, bag)
        return [d for d in bag.errors if d.code == "E0067"]

    def test_lectura_de_un_global_dentro_de_una_funcion_falla(self):
        errores = self._error(
            "DIM total AS INTEGER = 10\n"
            "FUNCTION f(p AS INTEGER) AS INTEGER\n"
            "  RETURN total + p\n"
            "END FUNCTION\n")
        self.assertEqual(len(errores), 1)
        self.assertEqual(errores[0].message,
                         "una FUNCTION no puede usar el global 'total'")

    def test_escritura_tambien(self):
        errores = self._error(
            "DIM total AS INTEGER = 10\n"
            "FUNCTION f(p AS INTEGER) AS INTEGER\n"
            "  total = total + p\n"
            "  RETURN total\n"
            "END FUNCTION\n")
        self.assertTrue(errores)
        self.assertIn("no puede usar el global", errores[0].message)

    def test_uno_por_uso(self):
        """Cada uso da su error, con su posición.

        Con dos usos salen dos: quien escribe el programa tiene dos líneas que
        arreglar, y el mensaje le dice cuáles en vez de cuál feature.
        """
        errores = self._error(
            "DIM a AS INTEGER = 1\n"
            "DIM b AS INTEGER = 2\n"
            "FUNCTION f(p AS INTEGER) AS INTEGER\n"
            "  RETURN a + b\n"
            "END FUNCTION\n")
        self.assertEqual(len(errores), 2)
        self.assertNotEqual(errores[0].span.start, errores[1].span.start)

    def test_la_nota_dice_las_dos_salidas_y_las_donde_estan(self):
        errores = self._error(
            "DIM total AS INTEGER = 10\n"
            "FUNCTION f(p AS INTEGER) AS INTEGER\n"
            "  RETURN total + p\n"
            "END FUNCTION\n")
        nota = errores[0].suggestion
        self.assertIn("pasa el valor como parámetro", nota)
        self.assertIn("baja el DIM dentro de la FUNCTION", nota)
        self.assertIn("memoria lineal", nota)

    def test_un_global_en_una_funcion_anidada_tambien_falla(self):
        """El error es del nombre, no del nivel de anidamiento."""
        errores = self._error(
            "DIM total AS INTEGER = 10\n"
            "FUNCTION f(p AS INTEGER) AS INTEGER\n"
            "  IF p > 0 THEN\n"
            "    RETURN total\n"
            "  END IF\n"
            "  RETURN 0\n"
            "END FUNCTION\n")
        self.assertTrue(errores)

    def test_un_local_que_tapa_a_un_global_sí_se_puede_usar(self):
        """El ámbito sigue mandando: un DIM local con el mismo nombre vale."""
        bag_errors = self._error(
            "DIM total AS INTEGER = 10\n"
            "FUNCTION f(p AS INTEGER) AS INTEGER\n"
            "  DIM total AS INTEGER = 5\n"
            "  RETURN total + p\n"
            "END FUNCTION\n")
        self.assertEqual(bag_errors, [])
