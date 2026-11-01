"""El gate de escalado: la línea de tiempo, no el cronómetro.

Medido hoy con programas generados de 100 a 800 líneas: **8× más fuente → 8×
más tiempo**. Lineal, y con ~90× de holgura contra el criterio de la Fase 8
("500 líneas en menos de 3 s en el navegador").

Un gate de milisegundos en CI sería inútil por dos razones: la máquina de quien
lo corre cambia el número, y un test de tiempo que se pone nervioso acaba
desactivándose, que es la forma más común de que un gate deje de existir.

Lo que **sí** merece vigilancia es el **escalado**, porque los riesgos de
cuadrático son conocidos y van a llegar:

- El compilador en Source necesitará tablas hash donde Source sólo tiene arrays
  con búsqueda lineal. Con 40 cadenas de código, una búsqueda lineal por cadena
  es tolerable; con 40 000 no lo es, y la diferencia se ve como un salto.
- El serializador del IR no puede concatenar cadenas: son dos.reserve por
  línea y sale cuadrático en el tamaño del módulo.
- `add_string` deduplica con un `dict` en Python, que es O(1). En Source será
  una lista, y eso es O(n) por literal.

Ninguno de los tres se ve en 100 líneas. Por eso la comparación es de **razón**:
un factor 8× de fuente puede costar 8×, 16× o 64×, y el gate sólo nota el 64×.

Un test con semilla fija y sin reloj: el número de líneas es exacto y el margen
es ancho a propósito, porque lo que se caza es un cambio de orden de magnitud.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "compiler"))

from sourc import sir                                    # noqa: E402
from sourc.diagnostics import CompileError, DiagnosticBag  # noqa: E402
from sourc.parser import parse                           # noqa: E402
from sourc.types import check                            # noqa: E402
from sourc.wasm.backend import compile_module            # noqa: E402

# Cuántas veces **peor que lineal** puede crecer el tiempo.
#
# El número clave es la razón entre el crecimiento del tiempo y el de la fuente:
#
#   1.0  lineal          -- lo que mide hoy
#   8.0  cuadrático      -- x8 de fuente da x64 de tiempo
#   64   cúbico          -- x8 de fuente da x512
#
# El factor va a 3.0, que es 3× la medida observada y una fracción de la
# posición del cuadrático. Con un margen de 20 el gate dejaría pasar un n² sin
# pestañear --8 ≤ 20-- y sería inútil; con uno de 1.5 el ruido de la máquina lo
# haría fallar un martes y acabarían desactivándolo.
FACTOR_ACEPTABLE = 3.0

# Un programa de 100 líneas ya tiene suficientes valores y textos distintos
# para que cualquier búsqueda lineal se note; uno de 800 tiene 4 veces más y la
# razón sale de ahí.
CHICO = 100
GRANDE = 800


def fuente_lineas(n: int) -> str:
    """Un programa de `n` sentencies con valores y textos distintos.

    Los textos **no** se repiten a propósito: si todos fueran el mismo, el
    deduplicador de constantes los colapsaría a uno y el programa mediría menos
    de lo que dice medir.
    """
    lineas = ["' Programa generado por tools/perf.py. No se versiona.",
              "DIM total AS INTEGER = 0"]
    for i in range(n):
        lineas.append(f'total = total + {i}')
        lineas.append(f'PRINT "v{i}="; total')
    return "\n".join(lineas) + "\n"


def medir(n: int, repeticiones: int = 3) -> tuple[float, int]:
    """(segundos por compilación, número de instrucciones). Devuelve lo peor.

    Lo peor y no la media: un gate de rendimiento no debe poder pasar por
    suerte. Y la primera vuelta se descarta, porque carga el módulo de Python y
    esa primera vez mide el arranque de caché, no el compilador.
    """
    source = fuente_lineas(n)
    mejor = None
    instrucciones = 0
    for vuelta in range(repeticiones + 1):
        bag = DiagnosticBag()
        inicio = time.perf_counter()
        program = parse(source, bag)
        typing = check(program, bag)
        module = sir.build(program, typing, "perf", "1.0")
        compile_module(module)
        segundos = time.perf_counter() - inicio
        if vuelta == 0:
            instrucciones = sir.count_instrs(module)
            continue
        mejor = segundos if mejor is None else max(mejor, segundos)
    return mejor or 0.0, instrucciones


def es_lineal(razon_fuente: float, razon_tiempo: float,
              factor: float = FACTOR_ACEPTABLE) -> bool:
    """¿El crecimiento del tiempo es compatible con ser lineal?

    Va aparte de `main` para poder comprobarlo con números puts, sin tener que
    inyectar un cuadrado en el compilador para ver que el gate lo pilla. Y esa
    es la razón de que exista: un gate de rendimiento whose only test is "it
    passes right now" no tiene ninguna prueba de que atraparía algo.
    """
    return razon_tiempo <= factor * max(razon_fuente, 1.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="perf")
    parser.add_argument("--factor", type=float, default=FACTOR_ACEPTABLE,
                        help=f"razón máxima tolerable (por defecto {FACTOR_ACEPTABLE})")
    args = parser.parse_args(argv)

    print("escalado del pipeline (razón, no milisegundos)\n", flush=True)
    print(f"  {'programa':<12} {'lineas':>7} {'instr':>7} {'tiempo':>10}", flush=True)

    tiempo_chico, instr_chico = medir(CHICO)
    tiempo_grande, instr_grande = medir(GRANDE)

    for etiqueta, n, instr, t in (("chico", CHICO, instr_chico, tiempo_chico),
                                  ("grande", GRANDE, instr_grande, tiempo_grande)):
        print(f"  {etiqueta:<12} {n * 2:>7} {instr:>7} {t * 1000:>8.1f}ms",
              flush=True)

    razon_fuente = (GRANDE * 2) / (CHICO * 2)
    razon_tiempo = tiempo_grande / max(tiempo_chico, 1e-6)
    print(f"\n  la fuente crece x{razon_fuente:.0f} y el tiempo x{razon_tiempo:.1f}",
          flush=True)

    if not es_lineal(razon_fuente, razon_tiempo, args.factor):
        print(f"\n**Cuadrático o peor.** Con x{razon_fuente:.0f} de fuente el "
              f"tiempo crece x{razon_tiempo:.1f}, que es "
              f"{razon_tiempo / max(razon_fuente, 1.0):.1f} veces peor que "
              f"lineal y el margen es {args.factor:.1f}. Suele ser una búsqueda "
              f"lineal donde cabía una tabla, o concatenación de cadenas en un "
              f"bucle.", flush=True)
        return 1

    print(f"\n  {razon_tiempo / max(razon_fuente, 1.0):.2f} veces peor que "
          f"lineal, dentro del margen de {args.factor:.1f}. Un n² daría 8.0 "
          f"aquí.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
