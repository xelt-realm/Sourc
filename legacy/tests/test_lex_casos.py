"""Casos del lexador que ningún programa puede llegar a tener.

Los goldens de `tests/golden/lex/` juzgan al lexador sobre programas que
compilan. Es un juez bueno, pero tiene un agujero de forma: **sólo puede ver lo
que el resto del lenguaje permite escribir**. Y hay caminos del lexador que
sólo se alcanzan con fuente que no compila:

- `1e` son dos tokens, un entero y un identificador, así que no aparece en
  ninguna expresión válida. Sirve para el camino en que la "e" **no** era
  exponente y hay que devolver la posición.
- `&H` sin dígitos es un error léxico, no un programa.
- Un texto sin cerrar es un error léxico.
- `\\u{...}` es la divergencia declarada de la rebanada 1.

Un hueco así no se tapa con el corpus, porque el corpus son programas que
funcionan. Se tapa con un fichero de casos y con la regla de que **cada ruta del
lexador que el lexador implemente tiene que aparecer aquí o en un golden**.

Las cuatro primeras líneas son la verdad; las de `\\u` son la divergencia
declarada, y por eso el test que las mira es distinto del que mira las demás: no
pide que coincidan, pide que se Sepan.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compiler"))
sys.path.insert(0, str(ROOT / "tools"))

import bootstrap  # noqa: E402


def lex_python(fuente: str) -> str:
    from sourc import cli

    temporal = ROOT / "tests" / "_caso.src"
    temporal.write_text(fuente, encoding="utf-8")
    salida = io.StringIO()
    try:
        # Se redirige el flujo en vez de fabricar un Printer: `stream` es un
        # atributo de la instancia, así que `patch.object` sobre la clase no
        # encuentra nada, y lo que se quiere probar es que el COMANDO escribe
        # los tokens, no que se le puede dar un escritor.
        with contextlib.redirect_stdout(salida):
            cli.main(["lex", str(temporal), "--salida"])
        return salida.getvalue()
    finally:
        temporal.unlink(missing_ok=True)


def lex_sourc(fuente: str) -> str:
    temporal = ROOT / "tests" / "_caso.src"
    temporal.write_text(fuente, encoding="utf-8")
    destino = ROOT / "tests" / "_caso_lex.gen.src"
    try:
        bootstrap.generar(temporal, destino)
        codigo, salida, error = bootstrap.ejecutar(destino)
        if codigo:
            raise AssertionError(f"el lexador no compiló:\n{error[:800]}")
        return salida
    finally:
        temporal.unlink(missing_ok=True)
        destino.unlink(missing_ok=True)


# Casos que los dos lexadores tienen que leer igual. Cada uno es un camino, y el
# comentario dice cuál.
COINCIDEN = [
    ("entero y flotante", "1 1.5 2E-2 3e+4\n"),
    ("la e no es exponente", "1e\n"),
    ("la e no es exponente con signo", "1e-\n"),
    ("punto de miembro", "a.1\n"),
    ("miembro de flotante", "a.1.2\n"),
    ("prefijos", "&HFF &hff &B1010 &b1 &O17 &o7\n"),
    ("ampersand suelto", "a & b\n"),
    ("rem es comentario", "REM hola\n"),
    ("rem con continuacion es identificador", "remolque = 1\n"),
    ("rem en minúsculas", "rem hola\n"),
    ("rem al final sin nada detras", "rem"),
    ("comentario hasta el fin", "a = 1 ' no\nb = 2\n"),
    ("comentario sin salto final", "a = 1 ' no acaba"),
    ("operadores de dos", "<> <= >= == =\n"),
    ("llaves dobles", "' {{ }} {{a}}\n"),
    ("texto con interpolacion", '"a{1 + 2}b"\n'),
    ("texto vacio", '""\n'),
    ("texto con llaves sueltas", '"a}b"\n'),
    ("escapes", '"a\\nb\\tc\\\\d\\"e"\n'),
    ("acentos en comentario", "' aña\nPRINT 1\n"),
    ("acentos dentro de texto", 'PRINT "ñ€"\n'),
    ("eof sin salto de linea", "PRINT 1"),
    ("solo espacios", "   \n  \t \n"),
]


class TestLosCasosDelLexador(unittest.TestCase):
    def test_cada_caso_coincide(self):
        """Los dos lexadores, sobre fuente que el parser no puede usar.

        Comparar la salida entera y no el número de tokens: el número igual en
        el caso de "1e" si uno leyera "1e" como flotante y el otro como entero
        y una letra, y el número es justo lo que no distingue ese fallo.
        """
        for nombre, fuente in COINCIDEN:
            with self.subTest(caso=nombre):
                mio = lex_sourc(fuente)
                suyo = lex_python(fuente)
                self.assertEqual(mio, suyo,
                                 f"el caso {nombre!r} difiere\n"
                                 f"--- sourc\n{mio}\n--- python\n{suyo}")

    def test_el_escape_unicode_esta_declarado_y_se_distingue(self):
        r"""`\u{...}` es la divergencia de la rebanada 1, y se mide.

        No se pide que coincidan --no pueden--. Se pide que **se diferencien**, y
        que se diferencien de una forma concreta: el de Python decodifica y lo
        lleva al valor del texto, y el de Sourc no lo decodifica y lo deja tal
        cual. Un test que dejara de comprobar esta divergencia dejaría que
        apareciera sola, y entonces nadie sabría si era un fallo nuevo o el
        hueco viejo.

        La razón está escrita en el propio lexador, en la cabecera: pasar de un
        entero a un carácter necesita CHR, y CHR no existe. Un rango ASCII se
        podría tapar con una tabla de 128 entradas; el no-ASCII, no. Por eso
        la rebanada 1 se queda sin `\u` entero y no lo disimula.
        """
        mio = lex_sourc('"a\\u{41}b"\n')
        suyo = lex_python('"a\\u{41}b"\n')
        self.assertNotEqual(mio, suyo,
                            "ya no difieren: o el hueco se tapó, y entonces "
                            "hay que quitarlo de la cabecera del lexador y "
                            "añadir CHR al presupuesto del pegamento; o el "
                            "lexador de Python cambió, y entonces los goldens "
                            "mienten. Las dos cosas hay que saberlas.")
        # Y la forma de la divergencia: el de Python decodifica a "aAb".
        self.assertIn("aAb", suyo)
        self.assertNotIn("aAb", mio)

    def test_un_texto_sin_cerrar_no_se_traga_el_resto(self):
        """Un texto sin cerrar para en el salto de línea, no se come el fichero.

        Es el mismo camino que el de Python y por eso tiene que coincidir, pero
        merece su propio test porque es el que distingue "el lexador entiende
        los textos" de "el lexador entiende el caso fácil de los textos".
        """
        mio = lex_sourc('"sin cerrar\nPRINT 1\n')
        suyo = lex_python('"sin cerrar\nPRINT 1\n')
        self.assertEqual(mio, suyo)
        # Y el programa de detrás se sigue leyendo: eso es lo que permite
        # reportar tres errores de una vez en vez de uno.
        self.assertIn("keyword", mio)


if __name__ == "__main__":
    unittest.main()