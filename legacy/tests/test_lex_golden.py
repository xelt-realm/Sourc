"""Puerta del juez del lexer escrito en Source.

Aquí hay tres comprobaciones y las tres están sobre lo mismo: que
`tests/golden/lex/*.lex` sea la verdad. Las tres hacen falta porque fallan de
forma distinta:

1. **Que no falte ningún golden.** El juez del bootstrap crece con el corpus, y
   un programa nuevo sin golden no falla hoy: simplemente no lo cubre nadie. Es
   el fallo que no se ve, y por eso se comprueba que la lista cuadra en las dos
   direcciones.
2. **Que no sobre ningún golden.** Un programa borrado deja un golden que
   vuelve a pasar porque ya nadie lo genera. Sin esta comprobación, `make corpus`
   acaba siendo la única fuente de verdad y el fichero se vuelve intocable.
3. **Que `sourc lex` lo reproduzca por el camino del CLI.** El golden lo escribe
   una función interna y lo lee el comando; si divergen, se comparan dos cosas
   que no son la misma. Comparar por el comando es lo que hace que el judge sea
   el judge y no una copia.

Y una cuarta cosa, que no es una comprobación: **no se ejecuta todavía el lexer
escrito en Source**. Este fichero es su judge, y va creciendo conforme el lexer
avance. Está aquí antes que el lexer a propósito --un juez escrito después se
ajusta al código que judges, y entonces no judge nada-- y está vacío por dentro
por la misma razón: si el golden está mal hoy, no lo va a decir nadie hasta que
el lexer que se compare mal lo plafonee como definición.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compiler"))

CORPUS = ROOT / "tests" / "corpus"
EJEMPLOS = ROOT / "examples"
GOLDEN = ROOT / "tests" / "golden" / "lex"


class TestElJuezDelLexer(unittest.TestCase):
    def _programas(self) -> list[Path]:
        return sorted(CORPUS.glob("*.src")) + sorted(EJEMPLOS.glob("*.src"))

    def _lex(self, path: Path) -> str:
        """`sourc lex` por el camino del comando, no por la función interna.

        Se llama a `main` con `argv` y se redirigen los flujos en vez de
        construir un `Printer` a mano. La diferencia importa: `main` es el que
        construye el parser, y un golden comprobado contra un comando invocado
        a mano no comprueba que el comando exista, ni que su bandera se llame
        como creo, ni que el subcomando esté registrado. Todo eso se rompe sin
        que se note si el test se salta el parser.
        """
        from sourc import cli

        salida, errores = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(errores):
            codigo = cli.main(["lex", str(path), "--salida"])
        self.assertEqual(codigo, 0,
                         f"{path.name}: sourc lex sale con {codigo}\n"
                         f"{errores.getvalue()}")
        return salida.getvalue()

    def test_no_falta_ningun_golden(self):
        """Cada programa tiene su golden de tokens."""
        faltan = [p.stem for p in self._programas()
                  if not (GOLDEN / (p.stem + ".lex")).is_file()]
        self.assertEqual(faltan, [],
                         f"estos programas no tienen golden de tokens: {faltan}. "
                         "El juez del lexer escrito en Source crece con el "
                         "corpus, y un programa sin golden no falla: no lo "
                         "cubre nadie. `make corpus` lo escribe.")

    def test_no_sobra_ningun_golden(self):
        """Ningún golden sin programa que lo produzca."""
        sobras = sorted(p.stem for p in GOLDEN.glob("*.lex")
                        if not (CORPUS / (p.stem + ".src")).is_file()
                        and not (EJEMPLOS / (p.stem + ".src")).is_file())
        self.assertEqual(sobras, [],
                         f"estos goldens no tienen programa: {sobras}. Un "
                         "programa borrado deja un golden que sigue pasando "
                         "porque ya nadie lo regenera.")

    def test_sourc_lex_reproduce_el_golden(self):
        """El comando y el golden dicen lo mismo."""
        for path in self._programas():
            with self.subTest(programa=path.stem):
                esperado = (GOLDEN / (path.stem + ".lex")).read_text(
                    encoding="utf-8")
                self.assertEqual(self._lex(path), esperado)

    def test_los_spans_de_los_tokens_son_coherentes(self):
        """Los spans tienen que ser coherentes, y no sólo parecerse a los de Python.

        El golden compara el flujo **completo**, así que un `tkFin` mal escrito
        saldría en la comparación... en cuanto el parser exista. Antes de eso, el
        `fin` es un número que nadie lee: se guarda, se imprime y ya. Estas tres
        propiedades son las que un golden todavía no puede cubrir, y las tres son
        cosas que un parser va a depender.

        - `inicio <= fin` en todos: un token al revés significa que se ha
          escrito el final antes del principio.
        - Los tokens de una misma línea **no se solapan**: `fin[i] <= inicio[i+1]`
          dentro de la línea, porque entre dos tokens sólo puede haber espacio en
          blanco o un comentario. Un solapamiento significa que un token se ha
          comido parte del siguiente.
        - El `eof` empieza y acaba en el número de **caracteres** del fichero. Es
          el que más veces se equivoca: el byte count y el character count sólo
          coinciden en un fichero sin nada no-ASCII, y 13 de los 20 programas
          tienen acentos.
        """
        for golden in sorted(GOLDEN.glob("*.lex")):
            with self.subTest(golden=golden.stem):
                filas = []
                for linea in golden.read_text(encoding="utf-8").splitlines():
                    if not linea.strip() or linea.lstrip().startswith(";"):
                        continue
                    partes = linea.split()
                    filas.append((int(partes[0]), int(partes[1]), partes[2]))

                for inicio, fin, tipo in filas:
                    self.assertLessEqual(
                        inicio, fin,
                        f"{golden.stem}: el token {tipo} en {inicio} acaba en "
                        f"{fin}, antes de empezar")

                anterior_fin = 0
                anterior_fin_linea = -1
                for inicio, fin, tipo in filas:
                    if tipo == "newline":
                        anterior_fin_linea = inicio
                    elif inicio > anterior_fin_linea:
                        self.assertGreaterEqual(
                            inicio, anterior_fin,
                            f"{golden.stem}: el token {tipo} en {inicio} se "
                            f"pisa con el anterior, que acaba en {anterior_fin}")
                    anterior_fin = fin

                eof = [fila for fila in filas if fila[2] == "eof"]
                self.assertEqual(len(eof), 1, f"{golden.stem}: hay {len(eof)} eof")
                self.assertEqual(eof[0][0], eof[0][1],
                                 f"{golden.stem}: el eof tiene inicio y fin "
                                 "distintos")

    def test_el_eof_termina_en_el_fichero(self):
        """El `eof` está donde acaba el fuente, contado en caracteres.

        Es la misma propiedad que arriba pero con el número real delante, y hace
        falta por separado porque es la única que depende de la cuenta de
        caracteres y no de la relación entre tokens: un acento en un comentario
        mueve el byte pero no la columna, y el único token que lo delata es el
        último.
        """
        for fuente in sorted(CORPUS.glob("*.src")) + sorted(EJEMPLOS.glob("*.src")):
            with self.subTest(programa=fuente.stem):
                texto = fuente.read_text(encoding="utf-8")
                lineas = [l for l in
                          (GOLDEN / (fuente.stem + ".lex")).read_text(
                              encoding="utf-8").splitlines()
                          if l.strip() and not l.lstrip().startswith(";")]
                inicio, fin, _ = lineas[-1].split()[:3]
                self.assertEqual(int(inicio), len(texto),
                                 f"{fuente.stem}: el eof está en {inicio} y el "
                                 f"fichero tiene {len(texto)} caracteres")
                self.assertEqual(int(fin), len(texto))

    def test_el_formato_no_se_puede_confundir(self):
        r"""El separador de una línea está escapado en el texto.

        La puerta de arriba compara cadenas y pasa igual si dos listas distintas
        dan el mismo texto. Esta comprueba que la forma canónica no puede
        confundirse consigo misma: si un programa Source escribiera `\|` en un
        literal, el golden tendría que distinguirlo de una barra de verdad, y
        para eso el escape tiene que estar en la salida.
        """
        from sourc.cli import visible

        casos = {
            "a|b": "a\\pb",
            "a\nb": "a\\nb",
            "a\tb": "a\\tb",
            "a\\b": "a\\\\b",
        }
        for entrada, esperado in casos.items():
            with self.subTest(texto=entrada):
                self.assertEqual(visible(entrada), esperado)

        # Y la propiedad que lo justifica: escapar dos veces no puede devolver
        # el mismo texto que escapar una vez sobre entradas distintas. Si
        # `visible` fuera ambigua, el golden de dos programas distintos podría
        # coincidir.
        distintos = {"a|p", "a\\pb", "a|b", "a\\p|b"}
        vistos = {}
        for entrada in distintos:
            vistos.setdefault(visible(entrada), []).append(entrada)
        for salida, entradas in vistos.items():
            self.assertEqual(len(entradas), 1,
                             f"visible() no distingue {entradas}: las dos "
                             f"salen como {salida!r}")


if __name__ == "__main__":
    unittest.main()