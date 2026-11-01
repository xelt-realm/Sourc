"""La clasificación de diferencias, probada con las tres clases.

Es la herramienta que hace manejable la equivalencia funcional de ADR 0016. Sin
clasificación, un diff de dos IR obliga a mirar cada línea sin saber si lo que
se ve es "se reordenó" o "calcula otra cosa" -- que es exactamente el coste que
el ADR dice querer evitar al no comparar binarios.

Y una herramienta de clasificación sin tests es una herramienta de silencios:
si clasifica casi todo como `formato`, nadie se fía y se lee el diff entero. Por
eso cada clase tiene su caso, y por eso se comprueba también **qué clase se le
asigna a un caso**.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compiler"))

from sourc import diffir                                 # noqa: E402

BASE = """module ejemplo lang=1.0
  host source.print_str
  host source.newline
  str@0 len=10 bytes=[104, 111, 108, 97, 32, 109, 117, 110, 100, 111]

func @main() -> INTEGER:
  const.strref %0:i64 0
  const.str %1:str %0:i64 10
  host.print_str %1:str
  newline
  const.i64 %2:i64 0
  ret %2:i64

  ; 6 instrucciones, 1 funciones, 1 constantes de texto
"""


class TestClasificacion(unittest.TestCase):
    def test_identico_no_dice_nada(self):
        self.assertEqual(diffir.clasificar(BASE, BASE), [])
        self.assertEqual(diffir.resumen([]), "sin diferencias")

    def test_una_constante_mas_larga_es_semantica(self):
        """El cambio que de verdad rompe el bootstrap."""
        otro = BASE.replace("len=10", "len=11").replace(
            "bytes=[104, 111, 108, 97, 32, 109",
            "bytes=[104, 111, 108, 97, 32, 33, 109")
        diferencias = diffir.clasificar(BASE, otro)
        self.assertTrue(diferencias)
        clases = {d["clase"] for d in diferencias}
        self.assertEqual(clases, {diffir.SEMANTICA},
                         "una constante distinta cambia lo que el programa "
                         "imprime, y no es cosmético")
        self.assertIn("calcula otra cosa", diffir.resumen(diferencias))

    def test_una_instruccion_distinta_es_semantica(self):
        otro = BASE.replace("const.i64 %2:i64 0", "const.i64 %2:i64 1")
        clases = {d["clase"] for d in diffir.clasificar(BASE, otro)}
        self.assertIn(diffir.SEMANTICA, clases)

    def test_espacios_y_recuento_son_formato(self):
        """La sangría y el `;` de resumen no dicen nada del programa."""
        otro = "\n".join("    " + l if l.strip().startswith("host") else l
                         for l in BASE.splitlines())
        otro = otro.replace("; 6 instrucciones, 1 funciones",
                            "; 99 instrucciones, 42 funciones")
        clases = {d["clase"] for d in diffir.clasificar(BASE, otro)}
        self.assertEqual(clases, {diffir.FORMATO},
                         "cambiar la sangría y el recuento de una línea de "
                         "comentario no puede ser semántico")
        self.assertIn("el IR es el mismo", diffir.resumen(
            diffir.clasificar(BASE, otro)))

    def test_lineas_intercambiadas_son_orden(self):
        """Las mismas instrucciones en otro sitio calculan lo mismo.

        Es la clase que más tiempo ahorra: sin ella, un reordenamiento obliga a
        revisar el diff entero buscando qué instrucción falta.
        """
        lineas = BASE.splitlines()
        i = lineas.index("  host source.print_str")
        j = lineas.index("  host source.newline")
        lineas[i], lineas[j] = lineas[j], lineas[i]
        otro = "\n".join(lineas) + "\n"
        clases = {d["clase"] for d in diffir.clasificar(BASE, otro)}
        self.assertEqual(clases, {diffir.ORDEN})
        self.assertIn("el IR es el mismo", diffir.resumen(
            diffir.clasificar(BASE, otro)))

    def test_orden_y_formato_juntos_no_pisan_semantica(self):
        """Si además hay una diferencia de verdad, el veredicto lo dice.

        Un bloque que mezcla un reordenamiento con un cambio real tiene que
        salir semántico. Clasificar el bloque por su parte "más fácil" sería
        justo el fallo que hace que la herramienta no sirva.
        """
        otro = BASE.replace("const.i64 %2:i64 0", "const.i64 %2:i64 7")
        lineas = otro.splitlines()
        i = lineas.index("  host source.print_str")
        j = lineas.index("  host source.newline")
        lineas[i], lineas[j] = lineas[j], lineas[i]
        otro = "\n".join(lineas) + "\n"
        clases = {d["clase"] for d in diffir.clasificar(BASE, otro)}
        self.assertIn(diffir.SEMANTICA, clases)

    def test_una_linea_de_mas_no_se_confunde_con_orden(self):
        """Añadir una instrucción no es reordenar: es cambiar el programa."""
        otro = BASE.replace("  newline\n", "  newline\n  drop\n")
        clases = {d["clase"] for d in diffir.clasificar(BASE, otro)}
        self.assertEqual(clases, {diffir.SEMANTICA})


class TestElVeredicto(unittest.TestCase):
    def test_el_orden_no_es_el_veredicto_final(self):
        """`orden` y `formato` juntos: el IR es el mismo, y se dice."""
        lineas = BASE.splitlines()
        i = lineas.index("  host source.print_str")
        j = lineas.index("  host source.newline")
        lineas[i], lineas[j] = lineas[j], lineas[i]
        otro = "\n".join(lineas) + "\n"
        otro = otro.replace("  str@0", "    str@0")
        veredicto = diffir.resumen(diffir.clasificar(BASE, otro))
        self.assertIn("el IR es el mismo", veredicto)

    def test_las_tres_clases_existen(self):
        self.assertEqual(diffir.CLASES,
                         (diffir.SEMANTICA, diffir.ORDEN, diffir.FORMATO))


if __name__ == "__main__":
    unittest.main()
