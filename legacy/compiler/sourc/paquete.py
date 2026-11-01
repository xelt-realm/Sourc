"""`sourc build --release`: un `dist/` que se abre en un navegador y se basta solo.

Tres ficheros y un manifiesto. Tres porque son los que hacen falta y nada más:
un `.wasm`, el pegamento y una página. Un cuarto por cada cosa que "se puede
añadir después" es un cuarto que alguien va a añadir.

## Por qué un `dist/` y no compilar a mano

Porque el paso que falta al «compila esto y ábrelo» a mano es el que **falla
cuando toca**. Un `dist/` que se escribe aunque el pegamento se haya pasado de
techo no es un release: es una copia con pasos extra. Aquí el presupuesto es
condición para escribir, no un informe que sale después:

1. Se mide el pegamento y el módulo **antes** de escribir nada.
2. Si algún techo se pasa, no se escribe el `dist/` y se sale con código 1.
3. Si algo falla a medias, el directorio que había antes se queda como estaba.

El punto 3 es el que evita el peor estado posible: un `dist/` a medio escribir que
parece bueno. Se escribe en un directorio temporal y se mueve entero al final,
que es una operación atómica en el mismo sistema de ficheros.

## Por qué la página se reescribe

`runtime/page/index.html` importa el pegamento con una ruta relativa al repo
(`../glue/glue_core.js`), que es lo correcto mientras la página vive dentro del
árbol. En `dist/` los dos ficheros están al lado, así que el import pasa a ser
`./glue_core.js`.

Se reescribe **el import y nada más**. Reescribir más --meter el módulo en
base64, quitar los comentarios-- sería cómodo y caro: en base64 el `.wasm` crece
un 33%, y ese peso lo paga el 100% de las descargas. Aquí los bytes que miden
los presupuestos son los bytes que se descargan.

## El manifiesto

`tamaños y hashes` es lo mínimo para que dos `dist/` se puedan comparar sin
diferencia de herramientas. El hash es **sha256** y no un hash propio: los bytes son los mismos en cualquier
máquina, y un hash hecho con números de Python depende de la versión de Python.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import rutas

# Dónde están los presupuestos. Es una constante y no un parámetro porque hay un
# `budgets.toml` por proyecto, pero se puede cambiar: un test tiene que poder
# estrangular un techo sin editar el fichero del repo, porque un test que edita
# un fichero versionado deja el repo modificado si algo va mal.
RUTA_PRESUPUESTOS = rutas.ruta_de_presupuestos()

# Los tres ficheros del `dist/`, en el orden en que se listan.
GLUE = "glue_core.js"
PAGINA = "index.html"
MODULO = "main.wasm"
MANIFIESTO = "sourc-manifest.json"

# El nombre del `.wasm` sale del fuente, no del revés: un programa se llama como
# su fuente y el `dist/` lo llama como él.
NOMBRE_MODULO = "main.wasm"


@dataclass(frozen=True)
class Contenido:
    nombre: str
    datos: bytes


@dataclass
class Resultado:
    destino: Path
    ficheros: list[Contenido]
    incumplimientos: list[str]

    @property
    def ok(self) -> bool:
        return not self.incumplimientos


def _sha256(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


# El nombre del fichero en `dist/` y el nombre con el que aparece en
# `budgets.toml`. Son distintos y por eso hace falta el mapa.
#
# Sin este mapa el pegamento **no se comprobaba**: se busca `glue_core.js` en un
# fichero de presupuestos donde se llama `source_glue.js`, no se encuentra nada,
# y el `--release` empaqueta un pegamento que se ha pasado del techo sin decir
# nada. Es el techo que aprieta más de todos --lo descarga el 100% de las
# páginas-- y era el único que no miraba.
ALIAS_PRESUPUESTO = {
    GLUE: "source_glue.js",
    PAGINA: "index.html",
}


def _presupuesto_de(nombre: str) -> str:
    """La clave con la que un artefacto aparece en `budgets.toml`.

    Para el pegamento y la página es un alias; para el módulo, el patrón
    `main.wasm de <fuente>.src`, que es como se llaman los presupuestos de los
    programas del corpus.
    """
    if nombre in ALIAS_PRESUPUESTO:
        return ALIAS_PRESUPUESTO[nombre]
    return f"{MODULO} de hola.src"


def medir_presupuestos(ficheros: list[Contenido],
                       ruta_presupuestos: Path | None = None) -> list[str]:
    """Los techos que se pasan. Vacío es que todo cabe.

    Se mide sobre los **bytes que se van a escribir**, no sobre los ficheros del
    repo. Es lo que hace que este módulo detecte un pegamento que se ha pasado en
    el repo antes de copiarlo: si se midiera el origen, un `dist/` con una
    versión vieja del pegamento habría pasado.
    """
    from . import budget

    if ruta_presupuestos is None:
        tolerancia, presupuestos = budget.desde_texto(rutas.texto_de_presupuestos())
    else:
        tolerancia, presupuestos = budget.cargar(ruta_presupuestos)
    fallos: list[str] = []
    for contenido in ficheros:
        for presu in presupuestos:
            if presu.clase != "artefacto":
                continue
            clave = presu.artifact
            if clave not in (contenido.nombre, _presupuesto_de(contenido.nombre)):
                continue
            medida = budget.medir(contenido.datos)
            if presu.gzip is not None:
                techo = int(presu.gzip * (1 + tolerancia))
                if medida.gzip > techo:
                    fallos.append(
                        f"{clave}: {medida.gzip} B gzip supera el techo de "
                        f"{presu.gzip} B")
            if presu.raw is not None:
                techo = int(presu.raw * (1 + tolerancia))
                if medida.raw > techo:
                    fallos.append(
                        f"{clave}: {medida.raw} B raw superan el techo de "
                        f"{presu.raw} B")
    return fallos


def preparar(wasm: bytes, nombre_modulo: str = NOMBRE_MODULO) -> list[Contenido]:
    """Los bytes del `dist/`, sin escribir nada.

    Separado de `escribir` a propósito: `tests/test_release.py` necesita comprobar
    que la página reescribe el import y que el pegamento no se toca, y eso se
    puede mirar sin crear un directorio.
    """
    from . import budget

    glue_crudo = rutas.leer_bytes(f"runtime/glue/{GLUE}")
    pagina_cruda = rutas.leer_texto(f"runtime/page/{PAGINA}")

    import_repositorio = '../glue/glue_core.js'
    if import_repositorio not in pagina_cruda:
        raise AssertionError(
            f"runtime/page/{PAGINA} ya no importa '{import_repositorio}'; "
            "el empaquetado deja de saber qué reescribir")

    import_dist = f"./{GLUE}"
    pagina = pagina_cruda.replace(import_repositorio, import_dist)
    if nombre_modulo != MODULO:
        # `?p=` es lo que la página lee para saber qué módulo cargar.
        pagina = pagina.replace(f'"{MODULO}"', f'"{nombre_modulo}"')

    return [
        Contenido(nombre_modulo, wasm),
        Contenido(GLUE, glue_crudo),
        Contenido(PAGINA, pagina.encode("utf-8")),
    ]


def manifiesto(ficheros: list[Contenido], nombre_modulo: str) -> bytes:
    """El `sourc-manifest.json`.

    Se escribe al final y ordenado, para que dos `dist/` de la misma fuente den
    el mismo fichero byte a byte. Un manifiesto que depende del orden de un
    `dict` hace imposible comparar dos Builds, que es justo para lo que sirve.
    """
    from . import budget

    entradas = {}
    total = 0
    for contenido in ficheros:
        medida = budget.medir(contenido.datos)
        entradas[contenido.nombre] = {
            "bytes": len(contenido.datos),
            "gzip": medida.gzip,
            "sha256": _sha256(contenido.datos),
        }
        total += len(contenido.datos)
    datos = {
        "artefactos": entradas,
        "modulo": nombre_modulo,
        "total_bytes": total,
        "version": 1,
    }
    return (json.dumps(datos, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n").encode("utf-8")


def escribir(destino: Path, ficheros: list[Contenido],
             incumplimientos: list[str]) -> Resultado:
    """Escribe el `dist/`, o no escribe nada.

    Se monta en un directorio al lado del destino y se mueve entero al final. Si
    algo falla a mitad --un disco lleno, un permiso-- el `dist/` anterior se
    queda como estaba, que es el único estado en el que se puede confiar.
    """
    if incumplimientos:
        return Resultado(destino, ficheros, incumplimientos)

    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.parent / (destino.name + ".en-empaquetado")
    if temporal.exists():
        shutil.rmtree(temporal)
    temporal.mkdir(parents=True)
    try:
        for contenido in ficheros:
            (temporal / contenido.nombre).write_bytes(contenido.datos)
        (temporal / MANIFIESTO).write_bytes(manifiesto(ficheros, ficheros[0].nombre))
        if destino.exists():
            shutil.rmtree(destino)
        temporal.rename(destino)
    except Exception:
        shutil.rmtree(temporal, ignore_errors=True)
        raise
    return Resultado(destino, ficheros, [])