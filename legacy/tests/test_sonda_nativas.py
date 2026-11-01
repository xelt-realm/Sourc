"""Cada nativa de la lengua tiene que atravesar las tres mitades: `check`, `build` y los dos Gaddafi.

Este módulo es la decisión 2 de ADR 0019 aplicada mecánicamente en vez de por
disciplina. La decisión dice que para cada operación se escribe primero el test
que define qué entra, qué sale y de qué tipo, y después el lowering. Eso se
hizo, y funcionó -- pero "se hizo" significa que alguien lo recuerda. Aquí no hay
que recordar nada: se añade una fila a una tabla y la puerta sale sola.

## El bug que justifica el fichero

`ABS`, `MIN`, `MAX` y `CLAMP` estaban declaradas en `types.py`, el intérprete
las ejecutaba y el backend no las bajaba. `sourc check` aceptaba el programa y
`sourc build` lo rechazaba con `E0058`. Con 392 tests en verde. Cuatro funciones
que la lengua ofrece y que nadie podía compilar, durante meses.

## Las tres mitades, y por qué tres y no dos

`check` acepta y `build` emite es la mitad que falla hoy. La otra mitad es que
`build` **no basta**: una operación puede emitir código bien formado que calcula
otra cosa. Cuando se implementaron las cuatro, el `select` tenía los dos valores
intercambiados: el módulo validaba, Node lo ejecutaba sin quejarse y `MIN(3, 4)`
daba 4. Ningún test de estructura lo ve; sólo comparar contra el intérprete.

Y el intérprete no es un juez neutro: es la misma persona que escribió el
frontend, con la misma idea de lo que significa `MIN`. Por eso la comparación se
hace contra **las dos** cosas que no son el intérprete: los bytes que salen del
módulo, y el valor que el módulo produce. Un juez que se puede meter en un
contenedor vale más que un juez que lo sabe todo.

## Por qué una tabla y no un bucle sobre `NATIVE_FUNCTIONS`

Porque una tabla puede llevar lo que la lista no puede: **qué argumento hay que
darle a cada nativa para que se pueda compilar**. `KEYDOWN` necesita un
`STRING`, `RGB` tres enteros, `SCREEN` tres. Y porque la fila es legible: cuando
alguien añada una nativa ve la fila que tiene que rellenar, en el mismo sitio
donde se documenta el resto.
"""

import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compiler"))
sys.path.insert(0, "tools")

from sourc import interp, sir                              # noqa: E402
from sourc.diagnostics import CompileError, DiagnosticBag  # noqa: E402
from sourc.parser import parse                              # noqa: E402
from sourc.types import NATIVE_FUNCTIONS, check             # noqa: E402
from sourc.wasm.backend import compile_module               # noqa: E402

NODE = shutil.which("node")


# Lo que hay que poner antes de la llamada. Sin esto, `KEY` falla con `E0060`
# --falta `USE SOURCE_2D`-- y el test falla por el diagnóstico equivocado, que
# es peor que no tener sonda: la puerta verde con la native sin comprobar.
PREAMBULO = {"key": "USE SOURCE_2D\n", "keydown": "USE SOURCE_2D\n"}


def _sonda(llamada: str, preambulo: str = "") -> str:
    """Un programa mínimo que llama a la nativa y usa el resultado.

    El `DIM` declarado y sin usar está a propósito: obliga a que la expresión
    tenga un tipo asignable, que es media parte del contrato. La otra media --
    que el valor sea el correcto-- la comprueba la comparación con el
    intérprete.
    """
    return f"{preambulo}DIM r AS STRING = \"\"\nPRINT {llamada}\n"


# La tabla. `nativa -> (llamada mínima, tipo de la expresión)`.
#
# Las que no aparecen aquí están en `tests/test_cobertura_features.py` como
# reserva con motivo: son de la Fase 3 o no tienen consumidor.
SONDAS = {
    "abs": ("ABS(-2)", "DOUBLE"),
    "min": ("MIN(1, 2)", "DOUBLE"),
    "max": ("MAX(1, 2)", "DOUBLE"),
    "clamp": ("CLAMP(1, 2, 3)", "DOUBLE"),
    "sqrt": ("SQRT(4)", "DOUBLE"),
    "floor": ("FLOOR(1.5)", "DOUBLE"),
    "sin": ("SIN(0)", "DOUBLE"),
    "cos": ("COS(0)", "DOUBLE"),
    "len": ('LEN("ab")', "INTEGER"),
    "lenu": ('LENU("ab")', "INTEGER"),
    "val": ('VAL("12")', "DOUBLE"),
    "str": ("STR(1)", "STRING"),
    "left": ('LEFT("abc", 1)', "STRING"),
    "right": ('RIGHT("abc", 1)', "STRING"),
    "mid": ('MID("abc", 1, 1)', "STRING"),
    "concat": ('CONCAT("a", "b")', "STRING"),
    "compare": ('COMPARE("a", "b")', "INTEGER"),
    "contains": ('CONTAINS("abc", "b")', "BOOLEAN"),
    "indexof": ('INDEXOF("abc", "b")', "INTEGER"),
    "charcode": ('CHARCODE("a", 0)', "INTEGER"),
    # ADR 0021. La sonda imprime el caracter, asi que el byte 65 sale por stdout
    # como una `A` y la comparacion con el interprete lo comprueba de verdad.
    "chr": ('CHR(65)', "STRING"),
    "rgb": ("RGB(1, 2, 3)", "COLOR"),
    "key": ('KEY("w")', "BOOLEAN"),
}


def _modulo(fuente: str):
    bag = DiagnosticBag()
    programa = parse(fuente, bag)
    if bag.has_errors():
        raise AssertionError("no parsea: " + bag.errors[0].message)
    typing = check(programa, bag)
    if bag.has_errors():
        raise AssertionError("no typecheckea: " + bag.errors[0].message)
    return sir.build(programa, typing, "sonda", "1.0")


class TestMitadDeCheck(unittest.TestCase):
    """`check` tiene que aceptar lo que la lengua declara."""

    def test_cada_sonda_typecheckea(self):
        for nativa, (llamada, _tipo) in sorted(SONDAS.items()):
            with self.subTest(nativa=nativa):
                _modulo(_sonda(llamada, PREAMBULO.get(nativa, "")))

    def test_la_tabla_cubre_lo_que_afirma_cubrir(self):
        """Una tabla que no se contrasta con la lista de nativas no vigila nada."""
        declaradas = set(NATIVE_FUNCTIONS)
        # Lo declarado que no está en la tabla tiene que estar reservado en la
        # puerta de cobertura; si aparece algo nuevo, esta puerta no lo mira.
        from casos_de_diagnostico import CASOS          # noqa: F401
        import test_cobertura_features as cobertura
        sin_sonar = declaradas - set(SONDAS) - set(
            cobertura.NATIVAS_SIN_DEMOSTRAR)
        self.assertEqual(
            sin_sonar, set(),
            f"nativas que ni la sonda ni la reserva cubren: {sorted(sin_sonar)}")

    def test_la_sonda_usa_el_resultado(self):
        """Una nativa cuyo resultado no se usa no demuestra el tipo de retorno."""
        for nativa, (llamada, _tipo) in sorted(SONDAS.items()):
            with self.subTest(nativa=nativa):
                self.assertIn(llamada, _sonda(llamada, PREAMBULO.get(nativa, "")))


class TestMitadDeBuild(unittest.TestCase):
    """`build` tiene que emitir. Ésta es la mitad que falló con ABS/MIN/MAX/CLAMP."""

    def test_cada_sonda_baja(self):
        for nativa, (llamada, _tipo) in sorted(SONDAS.items()):
            with self.subTest(nativa=nativa):
                try:
                    compile_module(_modulo(_sonda(llamada, PREAMBULO.get(nativa, ""))))
                except CompileError as error:
                    self.fail(f"{nativa}: check acepta y build no baja: "
                              f"{error.diagnostic.message}")

    def test_lo_emitido_pasa_la_comprobacion_del_encoder(self):
        """La comprobación de estructura del encoder.

        Es distinta de la del verificador de pila: aquélla mira los tipos de los
        locales mientras se compila (`E0065`), y ésta comprueba que las secciones
        estén donde deben y que ningún índice se salga. Las dos hacen falta y
        ninguna ve lo que ve la otra, así que la sonda corre las dos.
        """
        from sourc.wasm import encoder as w
        for nativa, (llamada, _tipo) in sorted(SONDAS.items()):
            with self.subTest(nativa=nativa):
                crudo = compile_module(_modulo(_sonda(llamada, PREAMBULO.get(nativa, ""))))
                try:
                    w.validate(crudo)
                except w.WasmEncodeError as error:
                    self.fail(f"{nativa}: el módulo no pasa la comprobación "
                              f"del encoder: {error}")

    def test_lo_emitido_lo_acepta_node(self):
        """Node es el judge independiente: si no valida ahí, no es un módulo."""
        if NODE is None:
            self.skipTest("node no está instalado")
        import tempfile
        for nativa, (llamada, _tipo) in sorted(SONDAS.items()):
            with self.subTest(nativa=nativa):
                crudo = compile_module(_modulo(_sonda(llamada, PREAMBULO.get(nativa, ""))))
                with tempfile.TemporaryDirectory() as tmp:
                    ruta = Path(tmp) / "m.wasm"
                    ruta.write_bytes(crudo)
                    import subprocess
                    resultado = subprocess.run(
                        [NODE, "--input-type=module", "-e",
                         "import {readFileSync} from 'node:fs';"
                         "WebAssembly.compile(readFileSync(process.argv[1]));",
                         str(ruta)],
                        capture_output=True, text=True, timeout=60)
                self.assertEqual(
                    resultado.returncode, 0,
                    f"{nativa}: node no acepta el módulo:\n"
                    f"{resultado.stderr[-300:]}")


class TestLasDosEjecuciones(unittest.TestCase):
    """El intérprete y el módulo tienen que decir lo mismo."""

    @unittest.skipUnless(NODE, "node no está instalado")
    def test_coinciden(self):
        import parity

        for nativa, (llamada, _tipo) in sorted(SONDAS.items()):
            with self.subTest(nativa=nativa):
                modulo = _modulo(_sonda(llamada, PREAMBULO.get(nativa, "")))
                referencia = interp.run(modulo, max_frames=1).output.strip()
                salida, _ = parity.ejecutar_en_node(compile_module(modulo),
                                                    frames=1)
                self.assertEqual(
                    salida.strip(), referencia,
                    f"{nativa}: el módulo dice {salida.strip()!r} y el "
                    f"intérprete dice {referencia!r}")

    def test_el_interprete_tambien_la_ejecuta(self):
        """La mitad del contrato que se olvida: el oráculo también tiene que
        poder correr lo que la puerta del backend acaba de compilar.

        No es redundante con lo de arriba: aquí no hace falta Node, así que en
        una máquina sin él la mitad del intérprete sigue comprobándose.
        """
        for nativa, (llamada, _tipo) in sorted(SONDAS.items()):
            with self.subTest(nativa=nativa):
                modulo = _modulo(_sonda(llamada, PREAMBULO.get(nativa, "")))
                try:
                    interp.run(modulo, max_frames=1)
                except interp.RuntimeFault as error:
                    self.fail(f"{nativa}: el intérprete hace trampa: {error}")


if __name__ == "__main__":
    unittest.main()