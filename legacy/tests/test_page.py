"""La página ejecuta el módulo y dice lo mismo que el intérprete.

La página es el camino de entrega: es lo que se abre en un navegador. El resto
de la Fase 2 lo demuestra con Node, y un camino sin comprobar es un camino que
se rompe el día que lo usa alguien.

Aquí no hay navegador: se ejecuta el script de `runtime/page/index.html` contra
un DOM simulado (`tools/page_check.mjs`). El E2E de verdad, con Chrome y CDP, es
el bloque siguiente de la Fase 2. Lo que esto pilla es lo que el E2E no pilla
como error: la página referenciando algo inexistente, el import mal escrito, el
módulo sin ejecutarse, `sourcResultado` vacío.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from sourc import interp, sir
from sourc.diagnostics import DiagnosticBag
from sourc.parser import parse
from sourc.types import check
from sourc.wasm.backend import compile_module

RAIZ = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
CHECK = RAIZ / "tools" / "page_check.mjs"


def _modulo(source: str):
    bag = DiagnosticBag()
    program = parse(source, bag)
    typing = check(program, bag)
    return sir.build(program, typing)


def _pagina(modulo_bytes: bytes, frames: int = 1, nombre: str = "main.wasm"):
    """Ejecuta la página y devuelve lo que dejó en `sourcResultado`."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        wasm = Path(tmp) / "programa.wasm"
        wasm.write_bytes(modulo_bytes)
        resultado = subprocess.run(
            [NODE, str(CHECK), str(wasm), str(frames), nombre],
            capture_output=True, text=True, timeout=60)
    if resultado.returncode != 0:
        raise AssertionError(f"la página no llegó al final:\n"
                             f"{resultado.stderr[-600:]}")
    return json.loads(resultado.stdout)


@unittest.skipUnless(NODE, "node no está instalado")
class TestPagina(unittest.TestCase):
    def test_imprime_lo_que_imprime_el_interprete(self):
        source = 'PRINT "hola mundo"\n'
        modulo = _modulo(source)
        esperado = interp.run(modulo).output
        resultado = _pagina(compile_module(modulo))
        self.assertIsNone(resultado["error"])
        self.assertEqual(resultado["salida"], esperado)

    def test_registra_el_dibujo(self):
        """La Fase 2 no pinta: registra. Y registra lo mismo que el intérprete.

        El esperado sale del intérprete en vez de estar escrito a mano: los
        números de un color empaquetado no se calcular bien de cabeza, y un test
        que falla por su propio número esconde el fallo que sí importa.
        """
        source = ('DIM suelo AS COLOR = RGB(12, 12, 20)\n'
                  'CLS suelo\n'
                  'RECT 0, 0, 32, 32, suelo\n')
        modulo = _modulo(source)
        referencia = interp.run(modulo)
        esperado = [f"{nombre} {' '.join(str(v) for v in valores)}"
                    for nombre, valores in referencia.display_list]
        resultado = _pagina(compile_module(modulo))
        self.assertEqual(resultado["display"], esperado)

    def test_se_para_en_el_limite_de_frames(self):
        """Un `WHILE true` no puede dejar la pestaña colgada."""
        source = ('DIM suelo AS COLOR = RGB(0, 0, 0)\n'
                  'WHILE true\n'
                  'CLS suelo\n'
                  'FLIP\n'
                  'WEND\n')
        resultado = _pagina(compile_module(_modulo(source)), frames=1)
        self.assertIsNone(resultado["error"])
        self.assertEqual(resultado["frames"], 1)

    def test_un_programa_que_no_existe_se_ve_en_la_pagina(self):
        """En blanco es indistinguible de un programa que no imprime nada."""
        resultado = _pagina(compile_module(_modulo('PRINT "x"\n')),
                            nombre="no-existe.wasm")
        self.assertIn("404", resultado["error"])
        self.assertEqual(resultado["salida"], "")

    def test_una_trampa_se_reporta(self):
        """Un índice fuera de rango es una trampa en WASM; la página la enseña."""
        source = ('DIM t AS ARRAY OF INTEGER = NEW INTEGER[2]\n'
                  't[9] = 1\n')
        resultado = _pagina(compile_module(_modulo(source)))
        self.assertIsNotNone(resultado["error"])


if __name__ == "__main__":
    unittest.main()