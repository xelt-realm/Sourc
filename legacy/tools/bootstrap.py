"""Ejecuta el lexador escrito en Sourc contra los goldens de `sourc lex`.

El juez de la rebanada 1 del bootstrap (Fase 8). No es un test más: es lo
primero que hace Sourc sobre Sourc, y su valor no está en que pase, sino en lo
que hace cuando falla. Los dos corpora son el mismo lenguaje escrito de dos
formas --una en Python y otra en Sourc-- y sólo pueden coincidir si la máquina
de estados está bien. Ungolden que sólo saldría bien porque las dos
implementaciones comparten un error no es posible aquí: están en ficheros
distintos y las dos están escritas contra los tokens, no contra la otra.

El generador hace tres cosas y las tres importan:

1. Escapa el fuente a un literal de Sourc y lo antepone al cuerpo del lexador.
   La alternativa --un `INPUT`-- no existe: no hay E/S en el lenguaje, y por eso
   el bootstrap necesita un contrato de host que todavía no está escrito. Cuando
   exista, este generador se simplifica y esta es la parte que cambia.
2. Compara la salida **exacta**. Sin "casi" y sin normalizar: si las dos
   implementaciones sólo coinciden en el número de tokens, coinciden en que
   ambas están rotas.
3. Muestra la diferencia. Un golden que dice "falló" sin decir dónde obliga a
   leer dos ficheros en paralelo, que es la forma más lenta de depurar que hay.

Y una cuarta cosa, que es una decisión y no una comprobación: **el fallo cuenta
las líneas y dice cuántas son**. No dice "está mal", dice "288 de 331 líneas
coinciden y 43 no", porque un compilador que difiere en un 13% de los tokens
tiene un fallo sistémico y uno que difiere en tres líneas tiene tres fallos
puntuales, y son problemas distintos con.Documento distinto.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = ROOT / "tests" / "corpus"
EJEMPLOS = ROOT / "examples"
GOLDEN = ROOT / "tests" / "golden" / "lex"
CUERPO = ROOT / "compiler" / "lexador.src"
PARSER = ROOT / "compiler" / "parser.src"
CASOS_PARSER = ROOT / "tests" / "casos_parser"
GOLDEN_PARSER = ROOT / "tests" / "golden" / "parser"

# Los seis casos de `tests/casos_parser/`, que es todo lo que 2a sabe. El
# primero escoge el subconjunto y el resto queda **en el directorio** con su
# golden, porque un golden que existe y no se compara es un sitio donde un bug
# puede vivir sin que nadie mire.
#
# Lo que 2a **no** sabe, y está declarado en la cabecera de `parser.src`:
# `Index`, `Call`, `Interpolate`, `ArrayLit`, `Field`, `Unary`, `Binary`, la
# precedencia de las expresiones y los diagnósticos. Los casos evitan a propósito
# los operadores binarios, porque un caso que los use juzga el árbol y la
# precedencia a la vez, y cuando fallara no se sabría cuál.
ALCANZABLES_2A = ("dim", "print", "asignacion", "funcion", "flujo", "anidado")


def escapar(texto: str) -> str:
    """Un texto de Python como literal de Sourc.

    Los cuatro personajes que hay que tocar y por qué:

    - `\\` y `"`, escapes del propio lenguaje.
    - El salto de línea y el tabulador, porque un literal no puede contener un
      salto de línea real.
    - `{` y `}`, que son `{{` y `}}`. Es el que se olvida uno, y el síntoma es
      un error de compilación que apunta a la línea del literal y no a la del
      programa que se está compilando.
    """
    return (texto.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t")
                .replace("{", "{{")
                .replace("}", "}}"))


def generar(fuente: pathlib.Path, destino: pathlib.Path,
            con_parser: bool = False) -> None:
    """Escribe el programa completo: el literal del fuente, el lexer y el parser.

    `con_parser` pone `volcarTokens` a FALSE. El lexer deja siempre los tokens en
    sus arrays, que es lo que consume el parser, y lo único que decide es si
    además los imprime. Imprimir los dos haría que la salida del programa fuese el
    golden del lexer seguido del del AST, y el golden del AST no estaría en
    ninguna parte.
    """
    cuerpo = CUERPO.read_text(encoding="utf-8")
    if con_parser:
        cuerpo += PARSER.read_text(encoding="utf-8")
    destino.write_text(
        f'DIM src AS STRING = "{escapar(fuente.read_text(encoding="utf-8"))}"\n'
        f"DIM volcarTokens AS BOOLEAN = {'FALSE' if con_parser else 'TRUE'}\n"
        + cuerpo,
        encoding="utf-8")


def generar_parser(fuente: pathlib.Path, destino: pathlib.Path) -> None:
    """Lexer + parser, que es la composición de la rebanada 2a."""
    generar(fuente, destino, con_parser=True)


def ejecutar(programa: pathlib.Path) -> tuple[int, str, str]:
    """Compila y ejecuta, y devuelve el código, stdout y stderr."""
    hecho = subprocess.run(
        [sys.executable, str(ROOT / "compiler" / "sourc.py"), "run",
         str(programa), "--salida"],
        capture_output=True, text=True, cwd=ROOT)
    return hecho.returncode, hecho.stdout, hecho.stderr


def lineas(s: str) -> list[str]:
    return s.splitlines()


def comparar(obtenido: str, esperado: str) -> tuple[int, int, list[str], list[str]]:
    """Cuántas líneas coinciden, cuántas no, y las dos versiones de las que no.

    Compara por posición y no como conjuntos. Un conjunto de tokens no necesita
    orden para ser el mismo conjunto, y aquí el orden es parte de lo que hay que
    demostrar: el parser depende de que el orden sea el del fuente.
    """
    a, b = lineas(obtenido), lineas(esperado)
    # `buenas` cuenta posiciones que coinciden sobre el Prefijo más corto. La
    # cola común se quita sólo para ensayar la diferencia: si se contara aquí,
    # `buenas` podría superar `total` y el informe mentiría justo cuando hay
    # más líneas de las que，y，应当 haber.
    buenas = sum(1 for x, y in zip(a, b) if x == y)
    total = len(b)
    while a and b and a[-1] == b[-1]:
        a.pop()
        b.pop()
    return buenas, total, a, b


def programa(nombre: str) -> str:
    return f"{nombre}.src"


def main_parser() -> int:
    """El parser escrito en Sourc contra los goldens de `sourc parse`."""
    verde = "\033[32m" if sys.stdout.isatty() else ""
    rojo = "\033[31m" if sys.stdout.isatty() else ""
    apagado = "\033[2m" if sys.stdout.isatty() else ""
    fin = "\033[0m" if sys.stdout.isatty() else ""

    if not PARSER.is_file():
        print(f"falta {PARSER}", file=sys.stderr)
        return 2

    fallos = 0
    with tempfile.TemporaryDirectory() as temporal:
        destino = pathlib.Path(temporal) / "parser.src"
        for nombre in ALCANZABLES_2A:
            fuente = CASOS_PARSER / (nombre + ".src")
            golden = GOLDEN_PARSER / (nombre + ".ast")
            if not fuente.is_file() or not golden.is_file():
                print(f"{rojo}  {nombre}: falta el caso o el golden{fin}")
                fallos += 1
                continue
            generar_parser(fuente, destino)
            codigo, obtenido, error = ejecutar(destino)
            esperado = golden.read_text(encoding="utf-8")
            if codigo:
                print(f"{rojo}  {nombre}: no compiló o no ejecutó{fin}")
                print(apagado + error[:600] + fin)
                fallos += 1
                continue
            if obtenido == esperado:
                lineas = len(esperado.splitlines())
                print(f"{verde}  {nombre}: {lineas} líneas{fin}")
                continue
            fallos += 1
            buenas, total, mios, suyos = comparar(obtenido, esperado)
            print(f"{rojo}  {nombre}: {buenas}/{total} líneas{fin}")
            for i, (mio, suyo) in enumerate(zip(mios, suyos)):
                if mio == suyo:
                    continue
                print(f"    línea {i + 1}")
                print(f"      sourc: {mio!r}")
                print(f"      python: {suyo!r}")
                if i > 3:
                    print(apagado + "      ..." + fin)
                    break

    if fallos:
        print(f"{rojo}{fallos} de {len(ALCANZABLES_2A)} programas difieren{fin}")
        return 1
    print(f"{verde}el parser coincide en {len(ALCANZABLES_2A)} programas{fin}")
    return 0



def main() -> int:
    if not CUERPO.is_file():
        print(f"falta {CUERPO}", file=sys.stderr)
        return 2

    objetivos = (sorted(CORPUS.glob("*.src")) + sorted(EJEMPLOS.glob("*.src")))
    faltan = [p.stem for p in objetivos if not (GOLDEN / (p.stem + ".lex")).is_file()]
    if faltan:
        print(f"sin golden de tokens: {faltan}. `make corpus` los escribe.",
              file=sys.stderr)
        return 2

    verde = "\033[32m" if sys.stdout.isatty() else ""
    rojo = "\033[31m" if sys.stdout.isatty() else ""
    apagado = "\033[2m" if sys.stdout.isatty() else ""
    fin = "\033[0m" if sys.stdout.isatty() else ""

    with tempfile.TemporaryDirectory() as temporal:
        destino = pathlib.Path(temporal) / "lex.src"
        lineas_totales = 0
        fallos = 0
        for fuente in objetivos:
            generar(fuente, destino)
            codigo, obtenido, error = ejecutar(destino)
            esperado = (GOLDEN / (fuente.stem + ".lex")).read_text(encoding="utf-8")
            if codigo:
                print(f"{rojo}  {fuente.stem}: no compiló{fin}")
                print(apagado + error[:1200] + fin)
                fallos += 1
                continue
            buenas, total, mios, suyos = comparar(obtenido, esperado)
            if not mios:
                lineas_totales += total
                print(f"{verde}  {fuente.stem}: {total} líneas{fin}")
                continue
            fallos += 1
            lineas_totales += buenas
            print(f"{rojo}  {fuente.stem}: {buenas}/{total} líneas{fin}")
            for i, (mio, suyo) in enumerate(zip(mios, suyos)):
                if mio == suyo:
                    continue
                print(f"    línea {i + 1}")
                print(f"      sourc: {mio!r}")
                print(f"      python: {suyo!r}")
                if i > 3:
                    print(apagado + "      ..." + fin)
                    break
        total = len(objetivos)
        if not fallos:
            print(f"{verde}el lexador coincide en {total} programas, "
                  f"{lineas_totales} líneas{fin}")
            return 0
        print(f"{rojo}{fallos} de {total} programas difieren{fin}")
        return 1


if __name__ == "__main__":
    if "--parser" in sys.argv[1:]:
        sys.exit(main_parser())
    sys.exit(main())
