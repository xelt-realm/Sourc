"""Fuzzer del encoder WASM.

Un compilador que emite binarios a mano tiene un modo de fallo que ningún test de
ejemplo cubre: un opcode mal escrito, una alineación que no cuadra, un LEB con
un byte de más. Nada de eso falla aquí; falla en el navegador, tres fases
después.

El fuzzer genera módulos **diversos pero válidos por construcción** y pide a
Node que los valide. Node implementa la especificación completa, así que es el
juez. La utilidad del fuzzer está en el número de módulos, no en su
sofisticación: mil módulos que validan cubren el espacio mucho mejor que uno que
valida.

**La parte difícil no es generar, sino no ensuciar.** Un generador que produce
módulos inválidos por sus propias reglas (una rama que deja un valor en la
pila) llena el fuzzer de errores que no son del encoder y que además apuntan al
sitio equivocado. Por eso `Pila` simula el efecto de cada instrucción y
`emitir()` exige que la rama quadre. Un error del generador se dice en el
generador.

Semilla fija: un fallo que sale una vez de cada doscientos no sirve de nada
(ADR 0016, requisito 3).
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "compiler"))

from sourc.wasm import encoder as w  # noqa: E402

SEED = 20261004


class GeneradorDesbalanceado(Exception):
    """Una rama generada deja valores en la pila.

    Es un error del generador, no del encoder. Sin esta comprobación, el
    síntoma aparece como "módulo inválido" dentro del mensaje de Node, que
    señala al archivo equivocado.
    """


# --------------------------------------------------------------- simulación

# (operando, resultado) de las operaciones que usa el generador. Es una tabla
# mínima a propósito: si el backend necesita más, se añade aquí con su test.
PILA = {
    w.I32_ADD: (2, 1), w.I32_SUB: (2, 1), w.I32_MUL: (2, 1),
    w.I32_AND: (2, 1), w.I32_SHL: (2, 1),
    w.I64_ADD: (2, 1), w.I64_SUB: (2, 1), w.I64_MUL: (2, 1),
    w.F64_ADD: (2, 1), w.F64_MUL: (2, 1),
    w.I32_EQZ: (1, 1), w.I32_CLZ: (1, 1),
    w.I64_EQZ: (1, 1),
    w.F64_ABS: (1, 1), w.F64_FLOOR: (1, 1),
    w.I32_EXTEND8_S: (1, 1), w.I64_EXTEND32_S: (1, 1),
    w.I32_WRAP_I64: (1, 1), w.F32_DEMOTE_F64: (1, 1), w.F64_PROMOTE_F32: (1, 1),
    w.I32_LOAD8_U: (1, 1), w.I32_LOAD16_U: (1, 1),
    w.F64_LOAD: (1, 1), w.I64_LOAD: (1, 1),
}

# Cargas y almacenamientos comparten opcode con un `memarg` detrás.
STORE_EFFECT = {
    w.I32_STORE8: 2, w.I32_STORE16: 2, w.I32_STORE: 2,
    w.I64_STORE: 2, w.F64_STORE: 2,
}

CONST_EFFECT = {
    w.I32_CONST: 1, w.I64_CONST: 1, w.F32_CONST: 1, w.F64_CONST: 1,
}


class Pila:
    """Saldo de valores de una rama, con los tipos que espera la función."""

    def __init__(self, result_type: int):
        self.result_type = result_type
        self.height = 0
        self.types: list[int] = []

    def const(self, value_type: int) -> None:
        self.height += 1
        self.types.append(value_type)

    def unary(self, opcode: int, result_type: int) -> None:
        pops, pushes = PILA[opcode]
        self._check(pops)
        del self.types[-pops:]
        self.height += pushes - pops
        for _ in range(pushes):
            self.types.append(result_type)

    def tee(self) -> None:
        """`local.tee` deja el valor en la pila: no cambia el saldo.

        Es el detalle que hace que la simulación valga la pena: a ojo, un
        `local.tee` parece consumir el valor como `local.set`, y el módulo
        resultante tiene un valor de más y no valida.
        """
        return

    def store(self, opcode: int) -> None:
        self._check(STORE_EFFECT[opcode])
        del self.types[-2:]
        self.height -= 2

    def drop(self) -> None:
        self._check(1)
        self.types.pop()
        self.height -= 1

    def balance(self) -> None:
        if self.height != 0:
            raise GeneradorDesbalanceado(
                f"la rama deja {self.height} valor(es) sin usar")

    def _check(self, needed: int) -> None:
        if self.height < needed:
            raise GeneradorDesbalanceado(
                f"la rama gasta {needed} valores y sólo tiene {self.height}")


# --------------------------------------------------------------- operaciones

BINARY_OPS = [
    (w.I32_ADD, w.I32, w.I32), (w.I32_SUB, w.I32, w.I32),
    (w.I32_MUL, w.I32, w.I32), (w.I32_AND, w.I32, w.I32),
    (w.I32_SHL, w.I32, w.I32),
    (w.I64_ADD, w.I64, w.I64), (w.I64_MUL, w.I64, w.I64),
    (w.F64_ADD, w.F64, w.F64), (w.F64_MUL, w.F64, w.F64),
]

UNARY_OPS = [
    (w.I32_EQZ, w.I32, w.I32), (w.I32_CLZ, w.I32, w.I32),
    (w.I64_EQZ, w.I64, w.I32), (w.F64_ABS, w.F64, w.F64),
    (w.F64_FLOOR, w.F64, w.F64),
    (w.I32_EXTEND8_S, w.I32, w.I32), (w.I64_EXTEND32_S, w.I64, w.I64),
    (w.I32_WRAP_I64, w.I64, w.I32),
    (w.F32_DEMOTE_F64, w.F64, w.F32), (w.F64_PROMOTE_F32, w.F32, w.F64),
]

MEM_OPS = [
    # (alineación, carga, almacenamiento, tipo del valor)
    (0, w.I32_LOAD8_U, w.I32_STORE8, w.I32),
    (1, w.I32_LOAD16_U, w.I32_STORE16, w.I32),
    (2, w.F64_LOAD, w.F64_STORE, w.F64),
    (3, w.I64_LOAD, w.I64_STORE, w.I64),
]

VALUES = {
    w.I32: [0, 1, -1, 2 ** 31 - 1, -(2 ** 31), 7, 255, 65535],
    w.I64: [0, 1, -1, 2 ** 63 - 1, -(2 ** 63), 1_000_000, -999_999],
    w.F32: [0.0, -0.0, 1.0, -1.5, 3.4e38, 1.2e-38],
    w.F64: [0.0, -0.0, 1.0, -1.5, 1.7e308, 5e-324],
}

CONST = {w.I32: w.i32_const, w.I64: w.i64_const,
         w.F32: w.f32_const, w.F64: w.f64_const}


def push(value_type: int, rng: random.Random) -> bytes:
    return CONST[value_type](rng.choice(VALUES[value_type]))


# --------------------------------------------------------------- generación


def branch_binary(body, pila: Pila, rng: random.Random) -> None:
    opcode, operand, result = rng.choice(BINARY_OPS)
    body.emit(push(operand, rng))
    pila.const(operand)
    body.emit(push(operand, rng))
    pila.const(operand)
    body.emit(bytes((opcode,)))
    pila.unary(opcode, result)
    body.emit(w.drop())
    pila.drop()


def branch_unary(body, pila: Pila, rng: random.Random) -> None:
    opcode, operand, result = rng.choice(UNARY_OPS)
    body.emit(push(operand, rng))
    pila.const(operand)
    body.emit(bytes((opcode,)))
    pila.unary(opcode, result)
    body.emit(w.drop())
    pila.drop()


def branch_block(body, pila: Pila, rng: random.Random) -> None:
    """`block` con salida condicional.

    Los dos saltos van al bloque de dentro, no al cuerpo de la función: el
    cuerpo tiene un resultado y el bloque ninguno, así que `br 1` desde aquí
    pediría un valor que ya se gastó. Es la misma aritmética de etiquetas por
    profundidad que tendrá que hacer el backend, y por eso se comprueba aquí.
    """
    body.emit(push(w.I32, rng))
    pila.const(w.I32)
    body.emit(w.block())
    body.emit(push(w.I32, rng))
    pila.const(w.I32)
    body.emit(w.op_unary(w.I32_EQZ))
    pila.unary(w.I32_EQZ, w.I32)
    body.emit(w.br_if(0))
    pila.drop()          # la condición del br_if
    body.emit(w.br(0))
    body.emit(w.end())
    body.emit(w.drop())
    pila.drop()


def branch_loop(body, pila: Pila, rng: random.Random, i32_locals: int) -> None:
    if i32_locals == 0:
        return
    body.emit(w.i32_const(rng.randint(1, 4)))
    pila.const(w.I32)
    body.emit(w.local_set(0))
    pila.drop()
    body.emit(w.loop())
    body.emit(w.local_get(0))
    pila.const(w.I32)
    body.emit(w.i32_const(1))
    pila.const(w.I32)
    body.emit(bytes((w.I32_SUB,)))
    pila.unary(w.I32_SUB, w.I32)
    body.emit(w.local_tee(0))
    pila.tee()
    body.emit(w.br_if(0))
    pila.drop()
    body.emit(w.end())


def branch_memory(body, pila: Pila, rng: random.Random) -> None:
    align, load, store, value_type = rng.choice(MEM_OPS)
    body.emit(w.i32_const(rng.randint(0, 4096)))
    pila.const(w.I32)
    body.emit(push(value_type, rng))
    pila.const(value_type)
    body.emit(w.memarg(store, align, 0))
    pila.store(store)
    body.emit(w.i32_const(rng.randint(0, 4096)))
    pila.const(w.I32)
    body.emit(w.memarg(load, align, 0))
    pila.unary(load, value_type)
    body.emit(w.drop())
    pila.drop()


def random_function(rng: random.Random, index: int) -> bytes:
    """Una función con mezcla aleatoria de operaciones, y saldo cero garantizado."""
    builder = w.ModuleBuilder(f"fuzz{index}")
    builder.add_memory(1)
    i32_locals = rng.randint(0, 3)
    i64_locals = rng.randint(0, 2)
    f64_locals = rng.randint(0, 2)

    body = builder.add_function(
        "main", results=(w.I32,),
        locals=[(i32_locals, w.I32), (i64_locals, w.I64), (f64_locals, w.F64)])
    pila = Pila(w.I32)

    for _ in range(rng.randint(1, 12)):
        kind = rng.random()
        if kind < 0.35:
            branch_binary(body, pila, rng)
        elif kind < 0.62:
            branch_unary(body, pila, rng)
        elif kind < 0.76:
            branch_block(body, pila, rng)
        elif kind < 0.88:
            branch_loop(body, pila, rng, i32_locals)
        else:
            branch_memory(body, pila, rng)

    pila.balance()

    body.emit(w.i32_const(rng.choice([0, 1, -1, 42])))
    pila.const(w.I32)
    return builder.to_bytes()


def generate(count: int, seed: int = SEED) -> list[bytes]:
    rng = random.Random(seed)
    return [random_function(rng, index) for index in range(count)]


# --------------------------------------------------------------- comprobación


def node_validates(hexes: list[str]) -> list[str]:
    """Devuelve los mensajes de error de los módulos que Node rechaza."""
    import json
    import subprocess
    import tempfile

    script = r"""
import { readFileSync } from "node:fs";
const hexes = readFileSync(process.argv[2], "utf8").trim().split("\n").filter(Boolean);
const bad = [];
hexes.forEach((hex, index) => {
  const bytes = new Uint8Array(hex.match(/../g).map((h) => parseInt(h, 16)));
  if (!WebAssembly.validate(bytes)) bad.push(index);
});
process.stdout.write(JSON.stringify(bad));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".hex", delete=False) as handle:
        handle.write("\n".join(hexes))
        path = handle.name
    script_path = path + ".mjs"
    Path(script_path).write_text(script, encoding="utf-8")
    try:
        result = subprocess.run(["node", script_path, path], capture_output=True,
                                text=True, timeout=300)
    finally:
        for leftover in (path, script_path):
            Path(leftover).unlink(missing_ok=True)
    if result.returncode != 0:
        raise AssertionError(f"node falló: {result.stderr[:300]}")
    return json.loads(result.stdout)


def main(count: int = 1000, seed: int = SEED) -> int:
    modules = generate(count, seed)
    bad = node_validates([m.hex() for m in modules])
    total_bytes = sum(len(m) for m in modules)
    if bad:
        print(f"{len(bad)} módulo(s) inválido(s) de {count}:")
        for index in bad[:5]:
            print(f"  #{index}: {modules[index].hex()[:140]}")
        return 1
    print(f"{count} módulos válidos, {total_bytes} bytes, "
          f"media {total_bytes // count} B")
    return 0


if __name__ == "__main__":
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    sys.exit(main(total))