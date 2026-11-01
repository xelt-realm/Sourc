"""El fuzzer tiene que ser reproducible, o no sirve para nada.

Un fallo que sale una vez de cada doscientos no es un fallo: es ruido. Y el
ruido en un gate es peor que no tener gate, porque entrena a ignorar el cartel.
Por eso la semilla es fija (ADR 0016) y por eso hay un test que lo comprueba: la
misma semilla tiene que dar el mismo programa, byte a byte, y en el mismo orden.

El orden importa tanto como el contenido. Un generador que usa un `set` para
recorrer nombres --que es lo más natural del mundo y lo que hace `PYTHONHASHSEED`
depender-- da programas distintos en cada proceso con la misma semilla. Aquí no
hay ningún `set` en el camino de generación, y este test es lo que lo fija.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from fuzz_source import Generador, SEMILLA  # noqa: E402


class TestDeterminismo(unittest.TestCase):
    def _programa(self, semilla: int) -> str:
        return Generador(semilla).programa()

    def test_la_misma_semilla_da_el_mismo_programa(self):
        for semilla in (SEMILLA, SEMILLA + 7, 424242):
            with self.subTest(semilla=semilla):
                self.assertEqual(self._programa(semilla),
                                 self._programa(semilla))

    def test_semillas_distintas_dan_programas_distintos(self):
        """Si dos semillas dieran lo mismo, la semilla no estaría fijando nada."""
        primero = self._programa(SEMILLA)
        segundo = self._programa(SEMILLA + 1)
        self.assertNotEqual(primero, segundo)

    def test_la_semilla_por_defecto_no_cambia(self):
        """El número está escrito en el módulo y en la documentación.

        Cambiarlo hace que todos los fallos reportados dejen de reproducirse con
        el comando que se les dio, así que es un número que hay que cambiar a
        propósito y no por limpar.
        """
        self.assertEqual(SEMILLA, 20261004)

    def test_el_programa_empieza_por_un_comentario(self):
        """Una línea de comentario que dice cómo reproducirlo.

        Es lo primero que lee quien encuentra un fallo en un log, y sin ella hay
        que adivinar la semilla a partir del texto.
        """
        lineas = self._programa(SEMILLA).splitlines()
        self.assertTrue(lineas[0].startswith("'"))
        self.assertIn("fuzz_source.py", lineas[0])
        self.assertIn("--semilla", " ".join(lineas[:3]))


class TestElGeneradorEsValidoPorConstruccion(unittest.TestCase):
    """Lo que el generador produce tiene que compilar, o el fuzzer no mide nada.

    Un generador que ensucia el fuzzer con programas que no compilan no es un
    problema pequeño: hace que los fallos apunten al sitio equivocado. Cada
    error de estos vale por un bug que el fuzzer no llegó a buscar.
    """

    def _comprueba(self, fuente: str) -> None:
        from sourc.diagnostics import DiagnosticBag
        from sourc.parser import parse
        from sourc.types import check

        bag = DiagnosticBag()
        programa = parse(fuente, bag)
        if not bag.has_errors():
            check(programa, bag)
        mensajes = [d.message for d in bag.errors]
        self.assertEqual(mensajes, [], f"el generador produjo esto:\n{fuente}")

    def test_los_primeros_programas_compilan(self):
        for numero in range(12):
            with self.subTest(semilla=SEMILLA + numero):
                self._comprueba(Generador(SEMILLA + numero).programa())

    def test_ninguna_funcion_usa_un_global(self):
        """Una FUNCTION no puede tocar un global: E0067, y con razón.

        En WASM cada función tiene sus propios locales, así que un global no se
        puede compartir. El generador limpia el alcance antes de bajar el cuerpo
        de la FUNCTION, y esto es lo que lo fija: si algún día se le pasa, los
        veinte programas siguientes fallan con `E0067` y el fuzzer parece roto.
        """
        import re

        for numero in range(12):
            fuente = Generador(SEMILLA + numero).programa()
            fuera, dentro, bloques = [], None, []
            for linea in fuente.splitlines():
                cabecera = re.match(r"\s*FUNCTION\s+\w+", linea)
                if cabecera:
                    dentro = []
                    continue
                if re.match(r"\s*END FUNCTION", linea):
                    bloques.append(dentro)
                    dentro = None
                    continue
                declarado = re.match(r"\s*DIM\s+(\w+)", linea)
                if declarado and dentro is None:
                    fuera.append(declarado.group(1))
                elif declarado and dentro is not None:
                    dentro.append(declarado.group(1))
            for bloque in bloques:
                for nombre in fuera:
                    self.assertNotIn(
                        nombre, bloque,
                        f"la FUNCTION usa el global '{nombre}':\n{fuente}")


if __name__ == "__main__":
    unittest.main()