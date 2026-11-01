"""Las diferencias conocidas del fuzzer, con su motivo, en un fichero que sólo puede encogirse.

## Qué es

`tools/fuzz_source.py` compara el intérprete contra el módulo sobre programas que
nadie eligió, y encontró siete bugs en una tarde. Los que se arreglaron. Los que
no, están aquí, con su semilla y su motivo.

## Por qué un fichero y no un `--tolerate`

Porque tolerar es olvidar. Un flag que dice "descarta estos tres fallos" no sabe
cuáles son, no dice por qué, y en dos meses nadie recuerda que existían: el
fuzzer da 97 de 100 y todo el mundo asume que el tres por ciento es ruido del
generador.

Un ratchet es distinto en tres cosas:

1. **Nombra los fallos.** Cada entrada es una semilla concreta, reproducible con
   el comando que se escribe en su motivo.
2. **Sólo puede encogerse.** El test compara el número de entradas con el
   histórico y falla si ha crecido. Arreglar un bug obliga a borrar su entrada,
   y eso es justo lo que hay que hacer.
3. **Dice por qué.** Una entrada sin motivo es un fallo tolerado con pasos extra,
   y el test la rechaza.

## Y por qué estas tres están aquí y no arregladas

Las tres son la misma forma: la salida del módulo sale **truncada en las últimas
líneas**, y sólo en ellas. Sospechan de los límites de paso o de frames, porque el
intérprete y el módulo cuentan distinto y con mil iteraciones eso se nota. No se ha
investigado más, y escribir una hipótesis sin comprobar sería peor que dejar la
observación.

Están fuera de la puerta de release a propósito. Un gate que tolera tres fallos
entrena a ignorar el cartel, y el cartel del fuzzer es el que más bugs ha
encontrado del repositorio entero.

## Cómo se arregla una

Se ejecuta el motivo, se encuentra la causa, se corrige, y se **borra la
entrada**. El test falla si sigue ahí, así que no se puede arreglar en silencio.
"""

from __future__ import annotations

import json
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
RUTA = RAIZ / "tests" / "fuzz_diffs.json"
VERSION = 1


def cargar() -> dict:
    if not RUTA.exists():
        return {"version": VERSION, "diferencias": {}}
    with RUTA.open(encoding="utf-8") as flujo:
        return json.load(flujo)


def guardar(datos: dict) -> None:
    RUTA.write_text(
        json.dumps(datos, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")


def diferencias() -> dict[str, str]:
    """`semilla -> motivo`, que es lo que tiene que significar cada entrada."""
    return dict(cargar().get("diferencias", {}))


def toleradas(semilla: int) -> bool:
    return str(semilla) in diferencias()