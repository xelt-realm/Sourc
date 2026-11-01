"""Dónde está cada cosa, tanto en un repo como dentro de un `.pyz`.

## Por qué existe este módulo

El compilador necesita tres ficheros que no son código: `budgets.toml`,
`subset.toml` y `runtime/`. En un repo se localizan subiendo desde
`__file__`, que es lo que se hacía en cuatro sitios distintos.

Pero el compilador también se distribuye como **un fichero**, `sourc.pyz`, hecho
con `zipapp` de la biblioteca estándar. Dentro de un zipapp `__file__` vale
`/ruta/sourc.pyz/sourc/paquete.py`, que **no existe en el disco**, así que
`Path(__file__).parents[2]` da una ruta sin sentido y `read_bytes()` revienta
con `FileNotFoundError` en un fichero que sí está ahí.

La comprobación es esa misma: si `__file__` no existe en disco, estamos dentro
de un archivo. Es un detalle de implementación de zipapp del que no conviene
hacer la detección, pero su **síntoma** es fiable y es lo único que hace falta.

## Por qué no basta con decir "el compilador no empaqueta"

Porque entonces `sourc build --release` desde el `.pyz` no funciona, que es
justo el comando que un usuario ejecuta para obtener el artefacto. El `.pyz`
lleva los datos dentro, y este módulo los saca de ahí.

## Y por qué no se extraen a un temporal

Porque sería un segundo mecanismo --"si estamos en un zipapp, extrae, lee,
borra"-- para algo que `zipfile` ya hace en cuatro líneas. Extraer deja basura en
el disco del usuario y dos rutas de código que mantener para el mismo fichero.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# `__file__` no existe en disco dentro de un zipapp. Esa es la detección.
DENTRO_DE_ARCHIVO = not Path(__file__).exists()


class SinRecurso(FileNotFoundError):
    """Un fichero de datos que debería venir con el compilador y no está.

    Es un `FileNotFoundError` porque es lo que el código que espera un fichero
    sabe capturar, y además lleva el nombre del recurso y los dos sitios donde
    se ha buscado. Un `FileNotFoundError` pelado decía `/ruta/que/no/existe` y
    no decía ni qué se pedía ni dónde se miró.
    """

    def __init__(self, relativo: str, buscado: str):
        self.relativo = relativo
        self.buscado = buscado
        super().__init__(f"falta {relativo}; se buscó en {buscado}")


def raiz() -> Path | None:
    """La raíz del repo, o `None` si estamos dentro de un zipapp."""
    if DENTRO_DE_ARCHIVO:
        return None
    return Path(__file__).resolve().parents[2]


def _archivo() -> str:
    """La ruta del `.pyz` que se está ejecutando.

    Se prueban tres fuentes porque ninguna sola sirve en todos los casos:
    `sys.argv[0]` es lo normal pero falla si se ejecuta con `python -m`, el
    cargador de `__main__` sabe el nombre del archivo, y `SOURC_PYZ` es el
    escape para un caso raro y para los tests.
    """
    import os

    for candidata in (os.environ.get("SOURC_PYZ"), sys.argv[0]):
        if candidata and candidata.endswith(".pyz") and Path(candidata).is_file():
            return candidata
    cargador = getattr(sys.modules.get("__main__"), "__loader__", None)
    nombre = getattr(cargador, "archive", None)
    if nombre and Path(nombre).is_file():
        return nombre
    raise SinRecurso("<el .pyz>", "sys.argv[0], el cargador de __main__ "
                                   "y SOURC_PYZ")


def leer_bytes(relativo: str) -> bytes:
    """Lee un fichero de datos, del disco o del `.pyz`."""
    base = raiz()
    if base is not None:
        ruta = base / relativo
        if not ruta.is_file():
            raise SinRecurso(relativo, str(ruta))
        return ruta.read_bytes()
    ruta_archivo = _archivo()
    with zipfile.ZipFile(ruta_archivo) as paquete:
        try:
            return paquete.read(relativo)
        except KeyError:
            raise SinRecurso(relativo, ruta_archivo) from None


def leer_texto(relativo: str) -> str:
    return leer_bytes(relativo).decode("utf-8")


def ruta_de_presupuestos() -> Path | None:
    """La ruta de `budgets.toml`, o `None` si estamos dentro de un zipapp.

    `None` no es un fallo: es la señal de que el texto hay que sacarlo del
    archivo. Devolver la ruta sólo cuando existe evita que un
    `ruta.is_file()` sobre una ruta inventada dé `False` y se confunda con "no
    hay presupuestos" en el mensaje.
    """
    base = raiz()
    if base is None:
        return None
    ruta = base / "budgets.toml"
    return ruta if ruta.is_file() else None


def texto_de_presupuestos() -> str:
    """El contenido de `budgets.toml`, del repo o del `.pyz`.

    Va como texto y no como `Path` porque `budget.cargar` sólo necesita el
    contenido, y así el compilador no depende de dónde estén los ficheros para
    leerlos. Es el mismo camino que en un repo, donde esto es un `read_text`.
    """
    return leer_texto("budgets.toml")