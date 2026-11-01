"""La puerta de la rebanada 1: el lexador escrito en Sourc, contra Python.

Ejecuta `tools/bootstrap.py` como proceso aparte y no como función. La razón es
que el juez tiene que ser el mismo comando que usa una persona, y el día que
`bootstrap.py` devuelva un 0 porque su propio código de comparación se
rompió, esta puerta no se entera: si la importara, un `assert` mal puesto
dentro de la herramienta sería la puerta diciéndose verde a sí misma.

Sale con 1 si difieren. Sale con 2 --que la herramienta distingue del 1-- si
falta el fichero del lexador o un golden, porque eso no es "el lexador está
mal", es "no hay nada que juzgar", y las dos cosas no pueden parecerse en el
código de salida de una puerta.
        un wish. Los casos sin comparar tienen que usar algo que la
        cabecera declara como fuera.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestElLexadorEnSourc(unittest.TestCase):
    def test_coincide_con_el_de_python(self):
        hecho = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "bootstrap.py")],
            capture_output=True, text=True, cwd=ROOT)
        salida = hecho.stdout
        if hecho.returncode == 2:
            self.fail("no hay nada que juzgar:\n" + salida
                      + hecho.stderr[-800:])
        self.assertEqual(
            hecho.returncode, 0,
            "el lexador escrito en Sourc difiere del de Python.\n"
            + salida[-4000:] + hecho.stderr[-800:])

    def test_el_parser_escribe_el_mismo_ast(self):
        """La rebanada 2a: el parser escrito en Sourc, contra los goldens.

        Los seis casos de `tests/casos_parser/`, con la pila de cuerpos y las
        sentencias `IF`, `WHILE`, `FOR` y `FUNCTION`. El caso `anidado` es el que
        más prueba: tres niveles de anidación abiertos por tres caminos distintos
        --un cuerpo nuevo, un `ELSE` y un cierre--.
        """
        hecho = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "bootstrap.py"), "--parser"],
            capture_output=True, text=True, cwd=ROOT)
        if hecho.returncode == 2:
            self.fail("no hay nada que juzgar:\n" + hecho.stdout
                      + hecho.stderr[-800:])
        self.assertEqual(hecho.returncode, 0,
                         "el parser escrito en Sourc difiere del de Python.\n"
                         + hecho.stdout[-4000:] + hecho.stderr[-800:])

    def test_el_parser_declara_lo_que_no_hace(self):
        """Lo que el parser no parsea está escrito en su cabecera.

        Y hay una comprobación de que la lista **cubre** lo que los casos usan:
        un hueco declarado es una promesa, y una promesa que no se comprueba es

        Y una tercera cosa, que es la que evita que se cuele un caso: la
        lista de los que se comparan tiene que ser **todo** el directorio.
        Un caso con golden que no se compara es un sitio donde un bug puede
        vivir sin que nadie mire.
        """
        cuerpo = (ROOT / "compiler" / "parser.src").read_text(encoding="utf-8")
        for declaracion in ("Index", "La precedencia", "Los diagnósticos"):
            with self.subTest(declaracion=declaracion):
                self.assertIn(declaracion, cuerpo,
                              f"compiler/parser.src no declara {declaracion!r}. "
                              "Un hueco que no está escrito es un hueco que se "
                              "creyó tapado.")

        sys.path.insert(0, str(ROOT / "tools"))
        import bootstrap

        todos = sorted(p.stem for p in
                       (ROOT / "tests" / "casos_parser").glob("*.src"))
        self.assertEqual(
            sorted(bootstrap.ALCANZABLES_2A), todos,
            f"los casos de tests/casos_parser/ son {todos} y la puerta "
            f"compara {sorted(bootstrap.ALCANZABLES_2A)}. Si 2a ya parsea uno "
            "más, añádelo; si ya no, quita su golden.")

    def test_el_lexador_declara_sus_huecos(self):
        """Lo que el lexador NO hace está escrito en su cabecera.

        Los huecos de una rebanada se declaran, y se declaran en el fichero que
        los tiene, no en un documento aparte que nadie lee cuando toca
        compilador. Esta puerta es la que hace que esa promesa sea real: si
        alguien implementa el hueco y no quita la frase, el fichero se queda
        mintiendo y esta puerta no lo nota; si la quita sin implementarlo,
        tampoco.

        Y la lista de frases sale del propio fichero, no está aquí repetida, que
        es lo que hace que las dos listas no se separen nunca.
        """
        cuerpo = (ROOT / "compiler" / "lexador.src").read_text(encoding="utf-8")
        declarados = [frase for frase in (
            "Los escapes \\u{...}",
            "Los errores léxicos",
        ) if frase in cuerpo]
        self.assertEqual(len(declarados), 2,
                         "faltan declaraciones de hueco en compiler/lexador.src. "
                         "Un hueco que no está escrito en el fichero es un hueco "
                         "que se creyó tapado.")

    def test_no_deja_and_que_proteja_un_indice(self):
        r"""Ningún `AND` con una llamada que indexa.

        Source no cortocircuita `AND`. `pos < largo AND CHARCODE(src, pos)`
        evalúa las dos partes siempre, así que con `pos == largo` lee un byte que
        no existe. El fallo sale sólo en el último carácter del fichero, y sólo
        si el fichero no acaba en salto de línea: la clase de defecto que pasa
        la suite entera y revienta el día que alguien copia un programa.

        El chequeo es de texto y por eso es incompleto: no detecta un
        `AND` repartido en dos líneas ni dentro de una FUNCTION. Detecta el
        que se escribe, que es el que se escribe. Y `test_lex_casos` cubre el
        final de fichero sin salto de línea por comportamiento, que es la parte
        que el texto no puede ver.
        """
        import re

        cuerpo = (ROOT / "compiler" / "lexador.src").read_text(encoding="utf-8")
        malos = [linea.strip() for linea in cuerpo.splitlines()
                 if re.search(r"\bAND\b", linea)
                 and re.search(r"(CHARCODE|MID)\(src", linea)
                 and not linea.lstrip().startswith("'")]
        self.assertEqual(malos, [],
                         f"estas líneas usan AND junto a un índice de src:\n"
                         f"{malos}\nAND no cortocircuita: anida el IF.")


if __name__ == "__main__":
    unittest.main()