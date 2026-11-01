"""Paridad: el intérprete y el módulo tienen que decir lo mismo.

Es el test que sustituye a "el módulo valida". Un backend puede producir un
módulo perfectamente válido que imprime la primera línea del bucle y termina,
y ningún test de estructura lo ve: sólo comparar las dos salidas lo detecta.

La comparación es de `stdout` y del código de salida. Para los programas con
dibujo se compara además la lista de display, que es donde se esconden los
errores de `rect` y `circle`.

    python3 tools/parity.py                  todos los programas
    python3 tools/parity.py 07_arrays        sólo uno
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "compiler"))

from sourc import interp, sir                                    # noqa: E402
from sourc.diagnostics import DiagnosticBag, CompileError       # noqa: E402
from sourc.parser import parse                                   # noqa: E402
from sourc.types import check                                    # noqa: E402
from sourc.wasm import encoder as w                              # noqa: E402
from sourc.wasm.backend import compile_module                    # noqa: E402


def construir(path: Path) -> sir.Module:
    bag = DiagnosticBag()
    program = parse(path.read_text(encoding="utf-8"), bag)
    if bag.has_errors():
        raise SystemExit(f"{path.name}: no compila: "
                         + "; ".join(d.message for d in bag.diagnostics))
    typing = check(program, bag)
    if bag.has_errors():
        raise SystemExit(f"{path.name}: no typecheckea: "
                         + "; ".join(d.message for d in bag.diagnostics))
    return sir.build(program, typing)


def ejecutar_en_node(module: bytes, frames: int) -> tuple[str, str]:
    """Ejecuta el módulo con el glue real y devuelve (stdout, lista de display)."""
    with tempfile.TemporaryDirectory() as tmp:
        wasm = Path(tmp) / "programa.wasm"
        wasm.write_bytes(module)
        resultado = subprocess.run(
            ["node", str(RAIZ / "tools" / "host_node.mjs"), str(wasm),
             "--frames", str(frames), "--display"],
            capture_output=True, text=True, timeout=60)
    if resultado.returncode != 0:
        return (f"<node: {resultado.stderr.strip()[:200]}>", "")
    salida, _, display = resultado.stdout.partition("\n-- display --\n")
    return (salida, display)


def comparar(path: Path) -> tuple[bool, str]:
    """Compara un programa. Devuelve (pasa, detalle)."""
    module = construir(path)

    # El intérprete corre sin dibujo real: su lista de display es la referencia.
    referencia = interp.run(module, max_frames=1)

    try:
        crudo = compile_module(module)
        w.validate(crudo)
    except CompileError as error:
        detalle = error.diagnostic.message
        if error.diagnostic.suggestion:
            detalle += " -- " + error.diagnostic.suggestion.splitlines()[0]
        return (False, f"no baja: {detalle}")
    except Exception as error:                     # noqa: BLE001
        return (False, f"no baja: {type(error).__name__}: {error}")

    salida, display = ejecutar_en_node(crudo, frames=1)
    detalle = []
    if salida != referencia.output:
        detalle.append(f"stdout distinto\n  intérprete: "
                       f"{referencia.output!r}\n  módulo:     {salida!r}")
    esperado = _display_de_referencia(referencia) or ""
    # `flip` tampoco va en la lista del intérprete: cuenta frames. Y `screen`
    # va a un campo aparte. Los dos se filtran para comparar sólo el dibujo.
    modulo_filtrado = _normaliza([linea for linea in display.splitlines()
                                  if linea and not linea.startswith(
                                      ("screen ", "screen_off", "flip"))])
    referencia_filtrada = _normaliza([l for l in esperado.splitlines() if l])
    if modulo_filtrado != referencia_filtrada:
        detalle.append("dibujo distinto\n  intérprete:\n"
                       + "\n".join(referencia_filtrada)
                       + "\n  módulo:\n" + "\n".join(modulo_filtrado))
    if detalle:
        return (False, "\n".join(detalle))
    return (True, f"{len(crudo)} bytes")


def _display_de_referencia(resultado) -> str | None:
    """La lista de display del intérprete, en el mismo texto que el runner."""
    if not resultado.display_list:
        return None
    lineas = []
    for entrada in resultado.display_list:
        if isinstance(entrada, tuple) and len(entrada) == 2:
            # `SCREEN` no va en la lista de display: el intérprete lo guarda en
            # su propio campo, y compararlo aquí daría una diferencia que no es
            # de dibujo sino de dónde se eligió guardarlo.
            if entrada[0] in ("screen", "screen_off"):
                continue
            lineas.append(f"{entrada[0]} " + " ".join(str(v) for v in entrada[1]))
    return "\n".join(lineas)


def _normaliza(lineas: list[str]) -> list[str]:
    """Compara **valores**, no su texto.

    El intérprete guarda un SINGLE como float de Python y lo imprime `32.0`;
    JavaScript tiene un solo número y lo imprime `32`. Los dos son el mismo
    valor, y comparar los textos daría una diferencia en cada línea con
    decimales sin que nada esté mal.

    Cada token que parece un número se canoniza por su valor; el resto se
    compara tal cual.
    """
    salida = []
    for linea in lineas:
        tokens = []
        for token in linea.split(" "):
            try:
                tokens.append(repr(float(token)))
                continue
            except ValueError:
                pass
            tokens.append(token)
        salida.append(" ".join(tokens))
    return salida


def programas() -> list[Path]:
    return (sorted((RAIZ / "tests" / "corpus").glob("*.src"))
            + sorted((RAIZ / "examples").glob("*.src")))


# ---------------------------------------------------------------- la tabla

# `tests/parity.json` es el ratchet de ADR 0019, decisión 3: una tabla de
# paridad por programa que **sólo crece**. Sin ella, "el módulo calcula otra
# cosa" y "el intérprete cambió de opinión" se pueden compensar y salir en
# verde; con ella, cualquier cambio de salida es un cambio de fichero que hay
# que escribir a mano.
#
# Se guardan **hashes**, no salidas: la tabla no puede convertirse en una segunda
# copia del golden, que competiría con él y con el corpus. Y no lleva fecha ni
# nada que varíe entre ejecuciones, porque el artefacto tiene que ser
# reproducible (ADR 0016).
TABELA = RAIZ / "tests" / "parity.json"


def huella(texto: str) -> str:
    """SHA-256 de un texto, en hexadecimal y recortado.

    Recortado a 16 caracteres porque es una señal de "esto cambió", no un
    control de integridad: 64 caracteres de hexadecimal en un fichero que se
    lee a menudo sólo estorban.
    """
    import hashlib
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:16]


def referencia(path: Path) -> dict:
    """Lo que el intérprete dice de un programa: salida y dibujo, con hash."""
    module = construir(path)
    resultado = interp.run(module, max_frames=1)
    dibujo = _normaliza([l for l in
                         (_display_de_referencia(resultado) or "").splitlines()
                         if l and not l.startswith(("screen ", "screen_off",
                                                    "flip"))])
    return {"salida": huella(resultado.output),
            "display": huella("\n".join(dibujo))}


def leer_tabla() -> dict:
    if not TABELA.exists():
        return {}
    return json.loads(TABELA.read_text(encoding="utf-8")).get("programas", {})


def escribir_tabla() -> None:
    tabla = {}
    for path in programas():
        try:
            pasa, detalle = comparar(path)
        except Exception as error:                 # noqa: BLE001
            pasa, detalle = False, f"{type(error).__name__}: {error}"
        if pasa:
            tabla[path.name] = referencia(path)
            print(f"  ok   {path.name:<26} {detalle}")
        else:
            # Un programa que no pasa también entra, con su motivo. Ocultarlo
            # haría que "está en la tabla" significara "está bien", y la tabla
            # dejaría de poder distinguir un fallo de un olvido.
            tabla[path.name] = {"motivo": detalle.splitlines()[0]}
            print(f"  FALLA {path.name:<25} {detalle}")
    TABELA.write_text(
        json.dumps({"version": 1, "programas": tabla},
                   ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(f"\nescrito {TABELA.relative_to(RAIZ)} con {len(tabla)} programa(s)")


def main() -> int:
    if "--update" in sys.argv[1:]:
        if subprocess.run(["node", "--version"],
                          capture_output=True).returncode != 0:
            print("node no está: actualizar la tabla necesita ejecutarlo")
            return 2
        escribir_tabla()
        return 0

    pedidos = [p for p in sys.argv[1:] if not p.startswith("-")]
    Node = subprocess.run(["node", "--version"], capture_output=True)
    if Node.returncode != 0:
        print("node no está: la paridad necesita ejecutarlo")
        return 2

    fallos = 0
    tabla = leer_tabla()
    for path in programas():
        if pedidos and not any(p in path.name for p in pedidos):
            continue
        try:
            pasa, detalle = comparar(path)
        except Exception as error:                 # noqa: BLE001
            pasa, detalle = False, f"{type(error).__name__}: {error}"
        # La tabla dice cómo estaba, y lo dice el estado **real**, no lo que
        # alguien escribió en el fichero. Marcar un programa verde como rojo
        # --con un motivo inventado-- es la forma más fácil de encoger la
        # cobertura sin que nada se queje, y aquí es donde se pilla: este es el
        # único sitio donde se puede comprobar, porque hace falta ejecutar el
        # módulo. La tabla sin Node (tests/test_parity_tabla.py) sólo puede
        # mirar la forma de las entradas.
        entrada = tabla.get(path.name)
        if entrada is not None and not _coincide_con_la_tabla(entrada, pasa):
            fallos += 1
            estado = "verde" if "salida" in entrada else f"rojo: {entrada.get('motivo')}"
            print(f"  FALLA {path.name:<25} la tabla lo da {estado} y "
                  f"ahora {'pasa' if pasa else 'falla'}")
            continue
        if pasa:
            print(f"  ok   {path.name:<26} {detalle}")
        else:
            fallos += 1
            print(f"  FALLA {path.name:<25} {detalle}")
    print(f"\n{fallos} programa(s) con diferencias")
    return 1 if fallos else 0


def _coincide_con_la_tabla(entrada: dict, pasa: bool) -> bool:
    """¿La tabla coincide con lo que acaba de pasar?

    Una entrada con hashes es un programa que se comprobó verde; una con
    `motivo` es uno que se comprobó rojo.
    """
    return pasa == ("salida" in entrada)


if __name__ == "__main__":
    raise SystemExit(main())