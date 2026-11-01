"""La puerta de release: lo que tiene que estar en verde antes de etiquetar.

Es ADR 0019, decisión 4, y la razón de existir es una sola: **no etiquetar si
algo falla**. Un tag es la afirmación de que un estado concreto del compilador es
bueno, y esa afirmación se queda desfasada en cuanto alguien etiqueta con la
suite en rojo por prisa.

Los seis pasos son los que la decisión enumera, más la reproducibilidad, que es
requisito del bootstrap (ADR 0016) y por tanto entra ahora y no al final:

    1. suite completa
    2. fuzzer del encoder
    3. paridad del corpus Stage0 ↔ Stage1
    4. presupuesto de peso
    5. reproducibilidad del artefacto
    6. typecheck de los ejemplos

Cada paso dice si pasó y **cuánto costó**. Un gate que no dice cuánto tarda es un
gate que nadie espera: la gente empieza a saltárselo.

Dos reglas sobre lo que este script NO hace:

- **No etiqueta.** Etiquetar es un acto deliberado de una persona; un script que
  crea tags automáticamente tarde o pronto crea uno en el momento equivocado.
  Aquí se comprueba, y quien etiqueta ya sabe que puede.
- **No se salta pasos.** Si Node no está, el paso de paridad **falla**. Puede
  sonar harsh, pero es lo que la decisión 4 pide: un release que comprueba menos
  cosas parece igual y garantiza menos. El entorno sin Node sirve para
  `make test`, que sí lo tolera; para esto no.

Y una regla sobre lo que este script **tampoco** hace, que se costó un codespace
congelado: ningún paso es este mismo script. Un paso que se invoca a sí mismo con
un flag se llama a sí mismo con ese flag otra vez, y otra vez, y la máquina se
queda sin procesos que hacer otra cosa. Cada paso es un comando externo, y
`tests/test_release.py` lo comprueba.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class Paso:
    """Un paso de la puerta: un nombre, un comando y para qué está.

    `necesita` es el programa externo sin el cual el paso no puede correr. Se
    comprueba **antes** de lanzarlo, y no se deduce del fallo: si `node` no está,
    el hijo peta con un `FileNotFoundError` dentro de su propio traceback, y un
    gate que dice "FileNotFoundError: 'node'" no dice que la máquina no puede
    soltar una release, que es lo que hay que arreglar.
    """

    def __init__(self, nombre: str, comando: list[str], nota: str = "",
                 necesita: str = ""):
        self.nombre = nombre
        self.comando = comando
        self.nota = nota
        self.necesita = necesita


PASOS = [
    Paso("suite completa", [PYTHON, "tools/testharness.py"],
         "todo, incluidos los tests de corpus"),
    Paso("fuzzer del encoder", [PYTHON, "tools/fuzz_encoder.py", "1000"],
         "módulos generados, validados por Node", necesita="node"),
    Paso("paridad Stage0 <-> Stage1", [PYTHON, "tools/parity.py"],
         "intérprete contra módulo, por programa", necesita="node"),
    Paso("presupuesto de peso", [PYTHON, "compiler/sourc.py", "size",
                                  "examples/", "tests/corpus/"],
         "todos los techos de budgets.toml"),
    Paso("reproducibilidad del artefacto",
         [PYTHON, "tools/testharness.py", "-k", "TestReproducibilidad"],
         "mismo fuente, mismos bytes, entre procesos"),
    Paso("typecheck de los ejemplos",
         [PYTHON, "compiler/sourc.py", "check", "examples/"],
         "los ejemplos compilan"),
    Paso("contrato del subconjunto", [PYTHON, "tools/testharness.py",
                                      "-k", "test_subset"],
         "el nivel 2 y los fixtures que lo vigilan"),
    Paso("escalado del pipeline", [PYTHON, "tools/perf.py"],
         "razón de crecimiento, no milisegundos"),
    Paso("cobertura por feature", [PYTHON, "tools/testharness.py",
                                   "-k", "test_cobertura"],
         "toda nativa y toda sentencia del EBNF, demostrada o reservada"),
    Paso("contrato de diagnósticos", [PYTHON, "tools/testharness.py",
                                      "-k", "test_contrato"],
         "cada código con un programa que lo provoque, y sugiera algo"),
    Paso("sonda de las tres mitades", [PYTHON, "tools/testharness.py",
                                       "-k", "test_sonda"],
         "por cada nativa: check acepta, build emite, los dos ejecutan igual"),
    Paso("invariantes del fuzzer", [PYTHON, "tools/testharness.py",
                                    "-k", "test_ratchet"],
         "el ratchet de diferencias sólo puede encogerse"),
    Paso("dist/: cuatro ficheros y el presupuesto manda",
         [PYTHON, "tools/testharness.py", "-k", "test_dist"],
         "el paquete es autocontenido y no se escribe si no cabe"),
    Paso("E2E por CDP (salta si no hay Chromium)",
         [PYTHON, "tools/testharness.py", "-k", "test_e2e"],
         "el servidor del dist y el camino de carga; con Chromium, el E2E real"),
    Paso("compilador en un fichero", [PYTHON, "tools/testharness.py",
                                     "-k", "test_zipapp"],
         "dist/sourc.pyz compila y empaqueta fuera del repo, y es reproducible"),
    Paso("el lexador escrito en Sourc", [PYTHON, "tools/bootstrap.py"],
         "el lexador de la rebanada 1 del bootstrap coincide con el de Python "
         "en los 20 programas del corpus y en los casos que ningún programa "
         "puede tener, como un '1e' o un texto sin cerrar"),
    Paso("el AST del parser escrito en Sourc",
         [PYTHON, "tools/bootstrap.py", "--parser"],
         "los seis casos de la rebanada 2a dan el mismo AST que el parser de "
         "Python, byte a byte: la pila de cuerpos y las sentencias con cuerpo"),
    Paso("la página y sus tres puertas", [PYTHON, "tools/testharness.py",
                                        "-k", "test_sitio"],
         "se genera desde Source, sin esquinas redondeadas, en oscuro, y las "
         "cifras que dice son las de verdad"),
]


def ejecutar(paso: Paso) -> tuple[bool, str, float]:
    """Corre un paso y devuelve (pasó, detalle, segundos)."""
    inicio = time.monotonic()
    try:
        resultado = subprocess.run(paso.comando, cwd=RAIZ, capture_output=True,
                                   text=True, timeout=900)
    except FileNotFoundError as error:
        return (False, f"no se pudo ejecutar: {error}",
                time.monotonic() - inicio)
    except subprocess.TimeoutExpired:
        return (False, "se pasó de los 15 minutos", time.monotonic() - inicio)
    return (resultado.returncode == 0, resultado.stdout + resultado.stderr,
            time.monotonic() - inicio)


def main() -> int:
    print("puerta de release (ADR 0019, decisión 4)\n", flush=True)
    fallos = []
    for paso in PASOS:
        # La línea se imprime **antes**, en su propia línea y sin `\r`: un gate
        # que no dice por dónde va parece colgado --la primera versión de este
        # script lo parecía porque `print` sin `flush` en una tubería no escribe
        # nada hasta el final-- y un `\r` deja media línea huérfana en el log
        # de CI, que es donde se lee cuando algo falla.
        if paso.necesita and shutil.which(paso.necesita) is None:
            print(f"  FALLA {paso.nombre:<32}         "
                  f"falta `{paso.necesita}` en el PATH: esta máquina no puede "
                  "soltar una release", flush=True)
            fallos.append((paso, f"falta `{paso.necesita}` en el PATH"))
            continue
        print(f"  ...   {paso.nombre} ({paso.nota})", flush=True)
        ok, detalle, segundos = ejecutar(paso)
        if ok:
            print(f"  ok    {paso.nombre:<32} {segundos:5.1f}s", flush=True)
            continue
        lineas = detalle.strip().splitlines()
        resumen = lineas[-1] if lineas else "sin salida"
        print(f"  FALLA {paso.nombre:<32} {segundos:5.1f}s  {resumen}",
              flush=True)
        fallos.append((paso, detalle))

    print(flush=True)
    if not fallos:
        print("todo en verde. Etiquetar es un acto deliberado: este script no "
              "etiqueta.", flush=True)
        return 0

    for paso, detalle in fallos:
        print(f"--- {paso.nombre}")
        # El detalle entero sólo de lo que falló: la salida de la suite son
        # cientos de líneas, y aquí lo que hace falta es el mensaje.
        for linea in detalle.strip().splitlines()[-25:]:
            print(f"    {linea}")
    print(f"\n{len(fallos)} paso(s) en rojo. **no se etiqueta.**", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
