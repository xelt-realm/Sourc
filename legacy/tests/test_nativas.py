"""ABS, MIN, MAX y CLAMP: la lengua los promete y el backend los tenía que bajar.

El bug que este archivo existe para que no vuelva es de una clase que no aparece
en ningún otro sitio del repositorio. `sourc check` decía que el programa estaba
bien, `sourc run` lo ejecutaba, y `sourc build` lo rechazaba con `E0058`: **las
dos mitades del mismo compilador se contradecían sobre el mismo fuente**. El
tripwire avisaba, así que no era un fallo silencioso, pero `ABS`/`MIN`/`MAX`/
`CLAMP` eran cuatro funciones que la documentación de `types.py` daba por
buenas y que nadie podía compilar.

Y lo que había debajo era peor que un `raise`: cuando se implementaron, los
cuatro **`select` tenían los dos valores intercambiados**. Eso no da un módulo
inválido. Da un módulo que valida, que el verificador acepta, que Node ejecuta
sin quejarse y que devuelve **el número equivocado**: `MIN(3, 4)` daba 4,
`ABS(5)` daba -5. Ningún gate de este repositorio lo habría visto, porque todos
comprueban que las cosas estén bien formadas, y eso estaba impeccable.

De ahí la forma de estos tests: no comprueban que la función esté, comprueban
que **diga lo mismo que el intérprete** en los casos donde el orden importa, y
en esos casos el orden importa siempre.

El otro detalle que se fija aquí es `select`: WASM devuelve el **primer** valor
cuando la condición no es cero, y ese orden no se parece en nada al del IR
(`destino, condición, si, no`). Invertirlo compila, valida y miente.
"""

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compiler"))
sys.path.insert(0, str(ROOT / "tools"))

from sourc import interp, sir                           # noqa: E402
from sourc.diagnostics import CompileError, DiagnosticBag  # noqa: E402
from sourc.parser import parse                          # noqa: E402
from sourc.types import check                           # noqa: E402
from sourc.wasm.backend import compile_module            # noqa: E402

NODE = shutil.which("node")

# (expresión, intérprete, módulo)
#
# Los casos negatives están porque `select` elige el primer valor cuando la
# condición **no** es cero: si inviertes los dos valores, todos estos dan el
# segundo número de cada fila y el módulo sigue siendo válido.
CASOS = [
    ("ABS(-5)", "5", "5"),
    ("ABS(5)", "5", "5"),
    ("ABS(0)", "0", "0"),
    ("ABS(-2147483648)", "2147483648", "2147483648"),
    ("ABS(-2.5)", "2.5", "2.5"),
    ("ABS(0.0)", "0", "0"),
    ("MIN(3, 4)", "3", "3"),
    ("MIN(4, 3)", "3", "3"),
    ("MIN(-1, 1)", "-1", "-1"),
    ("MIN(1.5, 2)", "1.5", "1.5"),
    ("MAX(3.5, 1)", "3.5", "3.5"),
    ("MAX(1, 3.5)", "3.5", "3.5"),
    ("MAX(2, 7)", "7", "7"),
    ("MAX(-2.0, -7.0)", "-2", "-2"),
    ("CLAMP(5, 1, 3)", "5", "5"),
    ("CLAMP(1, 5, 3)", "3", "3"),
    ("CLAMP(1, 2.5, 3)", "2.5", "2.5"),
    ("CLAMP(0.0, 9.0, 1.0)", "1", "1"),
]


def _modulo(source: str):
    bag = DiagnosticBag()
    program = parse(source, bag)
    typing = check(program, bag)
    if bag.has_errors():
        raise AssertionError("el fuente no pasa el typechecker: "
                             + bag.errors[0].message)
    return sir.build(program, typing, "prueba", "1.0")


class TestLasNativasSeBajan(unittest.TestCase):
    """El contrato mínimo: `check` acepta, y `build` tiene que aceptar también.

    Este test no necesita Node y es el que habría atrapado el bug original en
    el momento de написа la implementación, antes de que nadie ejecutara nada.
    """

    def test_se_bajan_todas(self):
        for expresion, _int, _mod in CASOS:
            with self.subTest(expresion=expresion):
                try:
                    compile_module(_modulo(f"PRINT {expresion}"))
                except CompileError as error:
                    self.fail(f"{expresion} typecheckea y no baja: "
                              f"{error.diagnostic.message}")

    def test_abs_sobre_un_flotante_usa_la_instruccion(self):
        """`f64.abs` existe; `select` devolvería `-0.0` en vez de `0.0`.

        No es un detalle estético: `-0.0` y `0.0` son el mismo número para
        comparar y **distintos** para imprimir, y para la aritmética de Signs.
        """
        codigo = compile_module(_modulo("PRINT ABS(-2.5)"))
        self.assertIn(b"\x99", codigo, "f64.abs (0x99) no está en el módulo")
        self.assertNotIn(b"\x1b", codigo,
                         "no debería haber un `select`: ABS de flotante es una "
                         "sola instrucción")

    def test_min_sobre_enteros_no_usa_una_instruccion_que_no_existe(self):
        """WASM no tiene `i64.min`, `i64.max` ni `i64.abs`.

        Los enteros se sintetizan con `select`. Si algún día el backend intenta
        emitir el opcode, el módulo deja de existir: `0xA4` es `f64.min` y sobre
        dos `i64` es un módulo que no valida.
        """
        codigo = compile_module(_modulo("PRINT MIN(2, 7)"))
        self.assertIn(b"\x1b", codigo, "el `select` que sintetiza el min falta")
        self.assertNotIn(b"\xa4", codigo,
                         "0xA4 es f64.min, no i64.min: ese opcode no existe")


class TestContraElInterprete(unittest.TestCase):
    """Intérprete contra módulo, caso por caso."""

    @unittest.skipUnless(NODE, "node no está instalado")
    def test_coinciden(self):
        import parity

        for expresion, esperado_int, esperado_mod in CASOS:
            with self.subTest(expresion=expresion):
                module = _modulo(f"PRINT {expresion}")
                referencia = interp.run(module, max_frames=1).output.strip()
                self.assertEqual(referencia, esperado_int,
                                 f"el intérprete cambió: {expresion}")
                salida, _ = parity.ejecutar_en_node(compile_module(module),
                                                    frames=1)
                self.assertEqual(salida.strip(), esperado_mod,
                                 f"el módulo se equivoca: {expresion}")

    @unittest.skipUnless(NODE, "node no está installed")
    def test_el_orden_de_los_argumentos_no_altera_el_resultado(self):
        """`MIN(a, b)` y `MIN(b, a)` dan lo mismo, aunque el intérprete no.

        El intérprete escribe `min(a, b)` como `b si b < a si no a`, que con NaN
        **no** es simétrico. Por eso `MIN` no se baja con `f64.min`, que sí lo
        es: se baja con `select` y el orden se copia del intérprete.
        """
        import parity

        for a, b in (("3", "4"), ("-7", "-2"), ("0.5", "0.25")):
            with self.subTest(a=a, b=b):
                primero = _modulo(f"PRINT MIN({a}, {b})")
                segundo = _modulo(f"PRINT MIN({b}, {a})")
                uno, _ = parity.ejecutar_en_node(compile_module(primero),
                                                frames=1)
                otro, _ = parity.ejecutar_en_node(compile_module(segundo),
                                                 frames=1)
                self.assertEqual(uno.strip(), otro.strip())


class TestElTipoDelResultado(unittest.TestCase):
    """`MIN`, `MAX` y `CLAMP` comparan entre sí, así que widen al más ancho."""

    def _tipo_de(self, expresion: str) -> str:
        bag = DiagnosticBag()
        program = parse(f"PRINT {expresion}", bag)
        typing = check(program, bag)
        errores = [e.message for e in bag.errors]
        # Los mensajes se calculan **antes** del assert: `assertFalse(ok,
        # bag.errors[0].message)` evalúa el mensaje aunque el assert pase, y sin
        # errores eso es un `IndexError` en un test que debería estar en verde.
        self.assertEqual(errores, [], f"{expresion} no typecheckea")
        # `PRINT <expr>` es un `Primitive` con un `Call` dentro, y el tipo que
        # importa es el de la llamada, no el de la sentencia.
        return typing.of(program.body[0].args[0]).kind

    def test_un_entero_y_un_real_dan_real(self):
        self.assertEqual(self._tipo_de("MAX(1, 3.5)"), "DOUBLE")
        self.assertEqual(self._tipo_de("MIN(1, 3.5)"), "DOUBLE")

    def test_un_chico_y_un_real_dan_real(self):
        self.assertEqual(self._tipo_de("MAX(1.5, 3.5)"), "DOUBLE")

    def test_abs_conserva_el_tipo(self):
        self.assertEqual(self._tipo_de("ABS(-2.5)"), "DOUBLE")
        self.assertEqual(self._tipo_de("ABS(-2)"), "INTEGER")


class TestElSignoDeUnaConstante(unittest.TestCase):
    """`-9223372036854775808` es el entero más pequeño del i64, y se puede
    escribir. Lo que no se puede es emitir `const.i64 9223372036854775808`: esa
    constante no existe en i64 y su LEB128 con signo lleva un byte de más, así
    que el módulo entero no valida.

    El síntoma era `extra bits in varint @+110` thrown por Node, que no dice
    nada sobre el autor. Y sólo aparecía dentro de `ABS(...)`: en un `PRINT` a
    secas el backend knew plegar `neg` de una constante y el programa pasaba.
    """

    def test_el_signo_se_plica_antes_de_emitir(self):
        texto = sir.serialize(_modulo("PRINT -5"))
        self.assertIn("const.i64 %0:i64 -5", texto)
        self.assertNotIn("neg.i64", texto,
                         "un menos sobre un literal no necesita una instrucción")

    def test_el_entero_mas_pequeno_del_i64_se_puede_escribir(self):
        texto = sir.serialize(_modulo("PRINT -9223372036854775808"))
        self.assertIn("-9223372036854775808", texto)
        self.assertNotIn("9223372036854775808 ", texto,
                         "la constante positiva no es representable en i64")

    def test_una_variable_no_se_plega(self):
        """El plegamiento es sólo del **literal**: `DIM a = 5` no es una
        constante aunque valga 5, y `0 - a` tiene que ser una resta de verdad."""
        texto = sir.serialize(_modulo("DIM a AS INTEGER = 5\nPRINT -a"))
        self.assertIn("neg.i64", texto)


class TestElBackendNoSeRompeAlFallar(unittest.TestCase):
    """Un diagnóstico que al renderizarse se cae no es un diagnóstico.

    `Printer` no tenía `diagnostic`, así que el `except CompileError` que
    recoge el fallo del backend se caía él mismo con un `AttributeError` y la
    flecha del diagnóstico apuntaba al `except`. El mensaje que veía quien
    escribía el programa era `AttributeError` en vez de `E0058`.
    """

    def test_un_error_del_backend_no_es_un_attributeerror(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fuente = Path(tmp) / "roto.src"
            fuente.write_text("PRINT 1 / 0\n", encoding="utf-8")
            resultado = subprocess.run(
                [sys.executable, str(ROOT / "compiler" / "sourc.py"),
                 "build", str(fuente)],
                capture_output=True, text=True, timeout=60)
            salida = resultado.stdout + resultado.stderr
        self.assertNotIn("AttributeError", salida)
        self.assertNotIn("Traceback", salida,
                         "un fallo del backend no puede ser una excepción")


if __name__ == "__main__":
    unittest.main()
