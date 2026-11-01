"""La diferencia entre dos IR, clasificada.

ADR 0016 elige la **equivalencia funcional** en vez de la identidad de bytes,
justo porque comparar dos binarios obliga a perseguir diferencias cosméticas.
La forma de que eso sea manejable es comparar el IR canónico --que es texto-- y
**clasificar** cada diferencia, para separar la que importa de la que no:

    semántica   Stage 0 y Stage 1 compilan el programa a algo distinto.
                Es la única clase que rompe el bootstrap, y la única que no se
                puede aceptar con un "es cosmético".
    orden       Las mismas instrucciones en otro orden. No cambia lo que hace
                el programa; sí cambia el texto, así que sin esta categoría
                cualquier diferencia parece semántica y se investigan en
                minutos lo que son segundos.
    formato     Comentarios, espacios, el recuento de una línea de resumen.
                No dice nada del programa.

Una diferencia sin clasificar no sirve: quien lee un diff de dos IR no sabe si
acaba de encontrar un bug del compilador en Source o una línea reordenada.

**Por qué `sourc diff` y no `--explain-stage-diff`.** El ADR 0016 nombra un flag
suelto en la raíz. El CLI de este compilador es de subcomandos y no tiene
ningún otro flag en la raíz, así que un flag único ahí sería el único de su
clase. El nombre del ADR se actualiza a `sourc diff` en el propio ADR, que es
donde se documentan estas cosas.
"""

from __future__ import annotations

import difflib
from collections import Counter

SEMANTICA = "semantica"
ORDEN = "orden"
FORMATO = "formato"

CLASES = (SEMANTICA, ORDEN, FORMATO)


def _sin_comentario(linea: str) -> str:
    """La línea sin el `;` de comentario, que el serializador pone al final.

    El recuento de instrucciones y el número de funciones cambian entre etapas
    por Narices del propio formato, no por lo que el programa calcula.
    """
    return linea.split(";", 1)[0].rstrip()


def _solo_espacios(a: str, b: str) -> bool:
    return _sin_comentario(a).strip() == _sin_comentario(b).strip()


def clasificar(antes: str, despues: str) -> list[dict]:
    """Las diferencias entre dos IR, una entrada por bloque."""
    lineas_a = antes.splitlines()
    lineas_b = despues.splitlines()

    # ¿Es sólo un reordenamiento? Se decide sobre el texto **sin comentarios** y
    # con las líneas ordenadas: si los dos multiconjuntos son iguales, lo único
    # que cambió es el orden, y eso no cambia lo que el programa hace.
    multiset_a = Counter(_sin_comentario(l) for l in lineas_a if l.strip())
    multiset_b = Counter(_sin_comentario(l) for l in lineas_b if l.strip())
    solo_orden = multiset_a == multiset_b

    diferencias: list[dict] = []
    generador = difflib.unified_diff(
        [l + "\n" for l in lineas_a], [l + "\n" for l in lineas_b],
        fromfile="stage0", tofile="stage1", n=2, lineterm="\n")
    actual: list[str] = []
    for linea in generador:
        if linea.startswith("@@"):
            if actual:
                diferencias.append(_cierra(actual, solo_orden))
            actual = [linea]
        elif linea.startswith(("---", "+++")):
            continue
        else:
            actual.append(linea)
    if actual:
        diferencias.append(_cierra(actual, solo_orden))
    return diferencias


def _cierra(bloque: list[str], solo_orden: bool) -> dict:
    """La clase de un bloque de diferencias, y sus líneas.

    La regla es una sola pregunta con dos salidas posibles: *¿el texto sin
    comentarios y sin espacios es el mismo?*

    - Sí, y además los multiconjuntos coinciden: cambió el **orden**.
    - Sí, y los multiconjuntos no coinciden: sólo cambió el **formato**, porque
      lo que se movió son espacios y comentarios.
    - No: cambió el **texto**, y eso es **semántica**.
    """
    cabecera = bloque[0] if bloque and bloque[0].startswith("@@") else ""
    lineas = [l.rstrip("\n") for l in bloque[1:]]
    if solo_orden:
        return {"clase": ORDEN, "cabecera": cabecera, "lineas": lineas}

    quitadas = Counter(_sin_comentario(l[1:]).strip() for l in lineas
                       if l.startswith("-"))
    anadidas = Counter(_sin_comentario(l[1:]).strip() for l in lineas
                       if l.startswith("+"))
    if quitadas == anadidas and not (quitadas - anadidas):
        return {"clase": FORMATO, "cabecera": cabecera, "lineas": lineas}
    return {"clase": SEMANTICA, "cabecera": cabecera, "lineas": lineas}


def resumen(diferencias: list[dict]) -> str:
    """Una línea por clase, con el veredicto."""
    cuentas = Counter(d["clase"] for d in diferencias)
    partes = [f"{clase} {cuentas[clase]}" for clase in CLASES if cuentas[clase]]
    if not partes:
        return "sin diferencias"
    veredicto = ("el IR es el mismo" if set(cuentas) <= {FORMATO, ORDEN}
                 else "**el IR calcula otra cosa**")
    return f"{veredicto} ({', '.join(partes)})"
