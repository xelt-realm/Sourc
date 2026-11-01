"""Empaqueta el compilador en un solo fichero ejecutable: `sourc.pyz`.

## Qué resuelve y qué no

**Resuelve**: el `git clone`. Hoy instalar Source es `git clone` + `make hola`,
que para alguien que sólo quiere compilar un `.src` son tres pasos y un
repositorio entero. Con esto son uno: descargar un fichero y
`python3 sourc.pyz hola.src`.

**No resuelve**: Python. El `.pyz` sigue siendo Python, porque el compilador
es Python --es la semilla de la cadena de confianza del bootstrap, ADR 0016-- y
convertirlo en WASM es la Fase 8. Decir que esto quita Python sería mentira, y
por eso el nombre del módulo no promete eso.

## Por qué `zipapp` y no un instalador

Porque `zipapp` es de la biblioteca estándar y no añade dependencia. Un
instalador sería un `pip install`, y el manifiesto dice que el compilador no
tiene dependencias externas **a propósito**: vendorizable, y en la Fase 8 tiene
que compilarse a sí mismo sin nada instalado (P7 de `ROADMAP.md`).

## Qué lleva dentro

El código y **los ficheros de datos**: `budgets.toml`, `subset.toml` y
`runtime/`. Los datos van dentro porque si no `sourc build --release` --el
comando que un usuario ejecuta para obtener el artefacto-- falla con un
`FileNotFoundError` sobre un fichero que sí está, a cuatro líneas de distancia.

Y **no** lleva la suite, ni el corpus, ni los ejemplos: los tests comprueban el
repo, no el empaquetado, y meterlos triplicaría el fichero para que sirieran de
algo a nadie.

## Determinismo

El `.pyz` lleva **mtime fijo** y las entradas ordenadas por nombre. Dos
construcciones del mismo código dan el mismo fichero byte a byte, que es el
requisito duro del bootstrap (ADR 0016) aplicado al propio empaquetado. Con
`zipfile` a pelo, la fecha de modificación de cada fichero se cuela en el
ZIP y dos builds distintos dan bytes distintos.
"""

from __future__ import annotations

import argparse
import stat
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

# 1980-01-01, que es la fecha mínima del formato ZIP (1980 en MS-DOS). Cualquier
# otra hace el `.pyz` irreproducible entre dos builds.
EPOCA_FIJA = (1980, 1, 1, 0, 0, 0)

# Los ficheros de datos que viajan con el compilador. Cada uno lo lee alguien:
# `budgets.toml` lo leen `size` y `build --release`; `subset.toml`, `check
# --nivel`; `runtime/`, el empaquetado.
DATOS = ("budgets.toml", "subset.toml")
DIRECTORIOS = ("runtime",)

# Lo que NO entra, y por qué. Va escrito porque la lista corta es la que se
# cuela sola: añadir `tests/` porque "son pequeños" triplica el fichero.
NO_ENTRA = {
    "tests/": "los tests comprueban el repo, no el empaquetado",
    "docs/": "no lo necesita ningún comando; pesa y no se usa",
    "site/": "lo genera el propio compilador, no al revés",
    "dist/": "es salida, y la salida no se empaqueta en la entrada",
    "bench/": "no existe todavía y si existiera tampoco viajaría",
}

MAIN = '''"""Punto de entrada del compilador empaquetado.

Va aparte del `sourc.py` del repo porque aquél calcula la raíz del repositorio
para encontrar `budgets.toml`, `subset.toml` y `runtime/`. Aquí esos ficheros
están **dentro** del propio `.pyz`, y es `sourc.rutas` quien los saca de ahí.
"""

import sys

from sourc.cli import main

sys.exit(main())
'''


def _excluir(ruta: Path) -> bool:
    relativo = ruta.relative_to(RAIZ).as_posix()
    return any(relativo == patron or relativo.startswith(patron)
               for patron in NO_ENTRA)


def entradas() -> list[tuple[str, bytes]]:
    """`(nombre dentro del .pyz, bytes)`, ordenado y sin datos del sistema.

    Se ordena por nombre porque el orden de escritura es el orden central del
    ZIP, y dos builds en ese orden distinto dan ficheros distintos.
    """
    encontradas: dict[str, bytes] = {}

    for base in sorted((RAIZ / "compiler" / "sourc").rglob("*.py")):
        relativas = base.relative_to(RAIZ).as_posix()
        # Dentro del zipapp el paquete va en `sourc/`, no en `compiler/sourc/`.
        encontradas["sourc/" + relativas.split("sourc/", 1)[1]] = \
            base.read_bytes()
    encontradas["__main__.py"] = MAIN.encode("utf-8")

    for nombre in DATOS:
        encontradas[nombre] = (RAIZ / nombre).read_bytes()

    for directorio in DIRECTORIOS:
        for base in sorted((RAIZ / directorio).rglob("*")):
            if base.is_file() and not _excluir(base):
                encontradas[base.relative_to(RAIZ).as_posix()] = base.read_bytes()

    return sorted(encontradas.items())


def construir(destino: Path) -> int:
    destino.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as paquete:
        for nombre, datos in entradas():
            info = zipfile.ZipInfo(nombre, date_time=EPOCA_FIJA)
            info.compress_type = zipfile.ZIP_DEFLATED
            # 0o644: dentro del zipapp los permisos no se usan, pero un
            # `0o600` Unexpected sería un aviso en cada instalación.
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            paquete.writestr(info, datos)
    return destino.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="build_zipapp",
        description="empaqueta el compilador en un solo fichero .pyz")
    parser.add_argument("--salida", default="dist/sourc.pyz")
    parser.add_argument("--comprobar", action="store_true",
                        help="construye dos veces y dice si son idénticos")
    args = parser.parse_args()

    destino = Path(args.salida)
    tamano = construir(destino)
    print(f"escrito {destino} ({tamano} B, "
          f"{len(entradas())} entradas)")

    if args.comprobar:
        segundo = destino.with_suffix(".pyz.2")
        construir(segundo)
        iguales = segundo.read_bytes() == destino.read_bytes()
        segundo.unlink()
        if not iguales:
            print("DOS BUILDS DISTINTOS: el empaquetado no es reproducible")
            return 1
        print("dos builds dan los mismos bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())