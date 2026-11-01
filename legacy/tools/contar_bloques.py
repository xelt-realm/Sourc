"""Cuenta los bloques de `compiler/parser.src` y dice dónde está el descuadre.

Contar `IF`/`END IF` a ojo no funciona en este fichero: tiene 1400 líneas, hay
comillas simples dentro de literales que parecen comentarios, y los `IF` que
*detectan* un `END IF` --`IF tkValor[pTok] = "END"`-- no abren nada. Un contador
basado en regex da tres o cuatro descuadres que no existen y esconde el que sí.

Así que esto tokeniza de verdad, como el lexer del propio lenguaje: literales
con sus escapes, comentarios con apóstrofo, y `REM`. Y luego devuelve la
profundidad al final y el primer punto donde se vuelve negativa, que es donde
está el cierre de más.

Es una herramienta de desarrollo, no una puerta: vive en `tools/` y su salida no
la mira nadie más que quien está escribiendo el parser.
"""

from __future__ import annotations

import pathlib
import re
import sys

ABRE = re.compile(r"^(IF|ELSE IF)\b(.*\bTHEN)$")
WHILE_FOR = re.compile(r"^(WHILE|FOR)\b")
DETECT = re.compile(r'valT = "(WEND|NEXT|END|ELSE)"')
DETECT2 = re.compile(r'tkValor\[pTok \+ 1\] = "(IF|WHILE|FUNCTION)"')

CERRA = ("END IF", "END WHILE", "WEND")


def lineas_de_codigo(texto: str):
    """Cada línea con su código, sin comentarios y sin literales.

    Los literales se sustituyen por `""` para que un `'` dentro de ellos no
    abra un comentario que no existe, y para que un `IF` escrito dentro de un
    literal no cuente como apertura.
    """
    for numero, cruda in enumerate(texto.split("\n"), 1):
        fuera = []
        i = 0
        while i < len(cruda):
            c = cruda[i]
            if c == '"':
                fuera.append('""')
                i += 1
                while i < len(cruda) and cruda[i] != '"':
                    i += 2 if cruda[i] == "\\" else 1
                i += 1
                continue
            if c == "'":
                break
            fuera.append(c)
            i += 1
        yield numero, "".join(fuera).strip()


def main() -> int:
    ruta = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else "compiler/parser.src")
    profundidad = 0
    minimo = 0
    primero_negativo = None
    pila: list[tuple[int, str]] = []

    for numero, t in lineas_de_codigo(ruta.read_text(encoding="utf-8")):
        if not t:
            continue
        if t in CERRA or t.startswith("NEXT"):
            profundidad -= 1
            if profundidad < minimo:
                minimo, primero_negativo = profundidad, (numero, t)
            if pila:
                pila.pop()
            continue
        if ABRE.match(t) and not DETECT.search(t) and not DETECT2.search(t):
            profundidad += 1
            pila.append((numero, t.split()[0]))
            continue
        if WHILE_FOR.match(t):
            profundidad += 1
            pila.append((numero, t.split()[0]))
            continue

    print(f"profundidad final: {profundidad}")
    if primero_negativo:
        print(f"primer cierre de más: línea {primero_negativo[0]} "
              f"({primero_negativo[1]})")
    if pila:
        print("sin cerrar:", pila[:5])
    if profundidad == 0 and primero_negativo is None:
        print("cuadra")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())