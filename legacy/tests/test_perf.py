"""El gate de escalado tiene que **picar**, y eso hay que demostrarlo.

Un gate de rendimiento cuya única prueba es "ahora pasa" no tiene ninguna
garantía de que atraparía una regresión. La forma barata de demostrarlo es
separar la aritmética de la decisión --`perf.es_lineal`-- y comprobarla con
números concretos, sin tener que meter un cuadrado en el compilador para ver que
el gate lo pilla.

Y el generador tiene que ser determinista: si `fuente_lineas(n)` devolviera algo
distinto en cada llamada, el número sería de otro programa y un fallo de
rendimiento no sería reproducible.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import perf                                                     # noqa: E402


class TestLaDecisionDelGate(unittest.TestCase):
    def test_un_crecimiento_lineal_pasa(self):
        """x8 de fuente con x8 de tiempo es lineal, y tiene que pasar."""
        self.assertTrue(perf.es_lineal(8, 8))
        self.assertTrue(perf.es_lineal(8, 6.9))   # sub-lineal: mejor que lineal
        self.assertTrue(perf.es_lineal(8, 0.5))

    def test_un_cuadratico_se_pilla(self):
        """x8 de fuente con x64 de tiempo es n² exacto, y tiene que fallar.
        """
        self.assertFalse(perf.es_lineal(8, 64))
        self.assertFalse(perf.es_lineal(8, 70))
        self.assertFalse(perf.es_lineal(8, 1e6))

    def test_un_cubico_tambien(self):
        self.assertFalse(perf.es_lineal(8, 512))

    def test_el_margen_es_el_que_dice(self):
        self.assertEqual(perf.FACTOR_ACEPTABLE, 3.0)
        # Justo en el borde pasa; una unidad de margen más, no. El borde tiene
        # que estar en un sitio y no en el otro, o el margen es una casualidad.
        self.assertTrue(perf.es_lineal(1, 3.0))
        self.assertFalse(perf.es_lineal(1, 3.1))

    def test_un_cuadratico_no_cabe_en_el_margen_real(self):
        """La razón que importa es cuánto peor que lineal, no cuánto más lento.

        x8 de fuente con x64 de tiempo es un n² perfecto, y su razón es 8.0 --
        muy por encima de 3.0. Con un margen de 20 habría pasado.
        """
        self.assertEqual(64 / 8, 8.0)
        self.assertGreater(64 / 8, perf.FACTOR_ACEPTABLE)
        self.assertFalse(perf.es_lineal(8, 64))

    def test_la_razon_de_fuente_entra_en_la_cuenta(self):
        """Subir el margen tiene que **aflojar** el gate.

        Si `es_lineal` ignorase la razón de fuente, `factor` no valdría para
        nada y cambiarlo en la línea de órdenes no haría nada. Es la
        comprobación que separa "un margen" de "un número decorativo".
        """
        # 42/8 = 5.25: por encima del margen de 5, por debajo del de 10.
        self.assertFalse(perf.es_lineal(8, 42, factor=5))
        self.assertTrue(perf.es_lineal(8, 42, factor=10))


class TestElGenerador(unittest.TestCase):
    """El programa que se mide tiene que medir algo."""

    def test_crece_el_tamano_pedido(self):
        self.assertEqual(perf.fuente_lineas(10).count("\n"), 22)
        self.assertEqual(perf.fuente_lineas(100).count("\n"), 202)

    def test_los_textos_no_se_repiten(self):
        """Si todos fueran el mismo, el deduplicador los colapsaría a uno y el
        programa mediría menos de lo que dice medir."""
        fuente = perf.fuente_lineas(20)
        distintos = {linea for linea in fuente.splitlines()
                     if linea.startswith("PRINT")}
        self.assertEqual(len(distintos), 20)

    def test_es_determinista(self):
        """El mismo `n` da el mismo fuente, byte a byte.

        Es lo que permite que un fallo de rendimiento sea reproducible: si el
        generador cambiara entre ejecuciones, el número sería de otro programa.
        """
        self.assertEqual(perf.fuente_lineas(50), perf.fuente_lineas(50))

    def test_el_programo_generado_compila(self):
        """Si no compilara, `medir` mediría el arranque del parser y punto."""
        from sourc import sir
        from sourc.diagnostics import DiagnosticBag
        from sourc.parser import parse
        from sourc.types import check

        bag = DiagnosticBag()
        program = parse(perf.fuente_lineas(20), bag)
        typing = check(program, bag)
        self.assertFalse(bag.has_errors(), bag.render("perf", ""))
        module = sir.build(program, typing, "perf", "1.0")
        self.assertGreater(sir.count_instrs(module), 0)


class TestLaMedidaNoEsElReloj(unittest.TestCase):
    def test_el_primer_pasado_se_descarta(self):
        """La primera vuelta mide el arranque de caché, no el compilador.

        Sin descartarla, un programa pequeño parece lentísimo --todo el
        `import`-- y la razón sale inflada, que es un falso positivo.
        """
        _, instr = perf.medir(10, repeticiones=1)
        self.assertGreater(instr, 0)

    def test_medir_es_finite_rapido(self):
        import time

        inicio = time.perf_counter()
        perf.medir(20, repeticiones=1)
        self.assertLess(time.perf_counter() - inicio, 30.0,
                        "el gate de rendimiento no puede tardar medio minuto")


if __name__ == "__main__":
    unittest.main()
