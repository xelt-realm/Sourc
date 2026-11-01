"""Intérprete del SIR.

Es la herramienta de desarrollo más importante del proyecto, y no un producto
público. Sirve para tres cosas:

  1. Validar la semántica del lenguaje en horas en vez de semanas. La Fase 2
     no es la primera vez que se ejecuta un programa Source.
  2. Ser el oráculo del testing diferencial: el mismo corpus se ejecuta aquí y
     en WASM, y las salidas tienen que coincidir byte a byte.
  3. Renderizar frames a PPM desde Node, para poder testear el pipeline de
     dibujo sin navegador.

Contador de alocaciones: cada instrucción que reserva memoria lo suma. Es lo
que permite verificar de forma mecánica la regla de oro del manifiesto (cero
asignaciones en el game loop) en vez de por inspección (ADR 0004).

Separación importante: los slots de variables y los valores del IR viven en
listas distintas. En WASM son locales y una pila de evaluación; aquí, una lista
por cada uno. Mezclarlas haría que un `DIM x` y un `%0` colisionaran.

Representación de valores:
    INTEGER, BOOLEAN, COLOR   int
    SINGLE, DOUBLE            float
    STRING                    tupla (bytes, longitud) — el par (ptr, len) del IR
    ARRAY                     lista de valores
"""

from __future__ import annotations

import math
import struct
import sys
from dataclasses import dataclass, field

from .sir import Func, Instr, Module, Value
from .types import DOUBLE, Type, zero_of_code

I64_MIN = -(2 ** 63)
I64_MAX = 2 ** 63 - 1

DEFAULT_STEP_LIMIT = 50_000_000


class RuntimeFault(Exception):
    """Fallo en tiempo de ejecución: bug del programa o del compilador."""


class StepLimitExceeded(RuntimeFault):
    pass


class FrameLimitReached(Exception):
    """Se alcanzó el número de frames pedido. No es un error.

    Los programas de juego (`WHILE true ... FLIP ... WEND`) no terminan nunca.
    Para poder probarlos sin navegador hay que poder decir "ejecuta 60 frames y
    para". Un límite de pasos no sirve: el mismo programa puede estar
    bloqueado en un bucle infinito sin hacer frames.
    """


@dataclass
class Frame:
    slots: list = field(default_factory=list)
    values: list = field(default_factory=list)
    strings: bytes = b""


@dataclass
class InterpResult:
    exit_code: int
    output: str
    allocations: int
    steps: int
    frames_flipped: int = 0
    display_list: tuple = ()
    screen: tuple | None = None


class Interpreter:
    """Ejecuta un `Module` del SIR."""

    def __init__(self, module: Module, step_limit: int = DEFAULT_STEP_LIMIT,
                 capture_output: bool = True, max_frames: int | None = None):
        self.module = module
        self.step_limit = step_limit
        self.max_frames = max_frames
        self.capture_output = capture_output
        self.allocations = 0
        self.steps = 0
        self.frames_flipped = 0
        self.screen: tuple | None = None
        self.display_list: list = []
        self.key_state: dict[str, bool] = {}
        self.out: list[str] = []

    # ------------------------------------------------------------- ejecución

    def run(self) -> InterpResult:
        main = next((f for f in self.module.funcs if f.name == "main"), None)
        if main is None:
            raise RuntimeFault("el módulo no tiene @main")
        exit_code = self.call(main, [])
        return InterpResult(
            exit_code=int(exit_code or 0),
            output="".join(self.out),
            allocations=self.allocations,
            steps=self.steps,
            frames_flipped=self.frames_flipped,
            display_list=tuple(self.display_list),
            screen=self.screen,
        )

    def call(self, func: Func, arguments: list):
        """Ejecuta una función y devuelve su valor de retorno."""
        frame = Frame(slots=[None] * len(func.locals),
                      values=[None] * func.next_value)
        for position, (name, _) in enumerate(func.params):
            frame.slots[func.slot_index[name]] = arguments[position]

        instrs = func.instrs
        labels = func.labels()
        pc = 0
        steps = 0
        try:
            while pc < len(instrs):
                steps += 1
                if steps > self.step_limit:
                    raise StepLimitExceeded(
                        f"se superaron {self.step_limit} pasos dentro de "
                        f"@{func.name}"
                    )
                instr = instrs[pc]
                if instr.op == "ret":
                    if instr.args:
                        return frame.values[instr.args[0].id]
                    return None
                pc = self.step(instr, pc, frame, labels)
            return None
        finally:
            # Los pasos se cuentan también cuando la ejecución se corta por el
            # límite de frames: si no, `--stats` mentiría justo en el caso en el
            # que uno quiere mirar.
            self.steps += steps

    def step(self, instr: Instr, pc: int, frame: Frame,
             labels: dict[str, int]) -> int:
        op = instr.op
        args = instr.args

        # ---- etiquetas y saltos
        if op == "label":
            return pc + 1
        if op == "br":
            return labels[args[0]]
        if op == "br_if.false":
            # El operando es un valor: hay que resolverlo antes de mirar su
            # verdad. Un objeto `Value` siempre es verdadero, así que saltarse
            # este paso convertiría cualquier `WHILE` en un bucle infinito.
            return labels[args[1]] if not operand(frame, args[0]) else pc + 1

        # ---- host
        if op == "newline":
            self.emit("\n")
            return pc + 1
        if op in _PRINT_OPS:
            value = frame.values[args[0].id]
            if op == "host.print_bool":
                self.emit("TRUE" if value else "FALSE")
            else:
                self.emit(format_value(value))
            return pc + 1
        if op == "flip":
            self.frames_flipped += 1
            if self.max_frames is not None and self.frames_flipped >= self.max_frames:
                raise FrameLimitReached()
            return pc + 1

        # ---- literales
        if op == "const.i64":
            frame.values[args[0].id] = wrap_int(args[1])
            return pc + 1
        if op == "const.f64":
            frame.values[args[0].id] = float(args[1])
            return pc + 1
        if op == "const.bool":
            frame.values[args[0].id] = 1 if args[1] else 0
            return pc + 1
        if op == "to.bool":
            # `DIM b AS BOOLEAN = 1`: cero es falso, el resto verdadero.
            frame.values[args[0].id] = 1 if operand(frame, args[1]) else 0
            return pc + 1
        if op == "const.zero":
            frame.values[args[0].id] = zero_of(args[0].type)
            return pc + 1
        if op == "const.strref":
            frame.values[args[0].id] = self.module.strings[args[1]]
            return pc + 1
        if op == "const.str":
            # (ptr, len): el ptr es el valor referenciado, no el Value.
            frame.values[args[0].id] = (frame.values[args[1].id], args[2])
            return pc + 1

        # ---- variables locales
        if op == "store.slot":
            frame.slots[args[0]] = operand(frame, args[1])
            return pc + 1
        if op == "load.slot":
            frame.values[args[0].id] = frame.slots[args[1]]
            return pc + 1

        if op == "load.global":
            frame.values[args[0].id] = self.globals[args[1]]
            return pc + 1

        # ---- aritmética
        if op[:4] in ("add.", "sub.", "mul.", "div.") or op[:3] == "neg":
            if op.startswith("neg"):
                frame.values[args[0].id] = -operand(frame, args[1])
            else:
                frame.values[args[0].id] = arith(
                    op.split(".")[0],
                    operand(frame, args[1]),
                    operand(frame, args[2]),
                )
            return pc + 1
        if op.startswith("rem."):
            frame.values[args[0].id] = _mod(operand(frame, args[1]),
                                            operand(frame, args[2]))
            return pc + 1
        if op.startswith("idiv."):
            # `\\` trunca hacia cero.
            left = operand(frame, args[1])
            right = operand(frame, args[2])
            if right == 0:
                raise RuntimeFault("división entera por cero")
            quotient = abs(left) // abs(right)
            # El signo sale de **los dos** operandos, no sólo del primero. Con
            # sólo el del dividendo, `-9 \ -9` daba -1: el cociente de los
            # absolutos es 1, el dividendo es negativo, y el signo se ponía mal.
            # La puerta de paridad lo encontró en `17_truncamiento.src`; ningún
            # ejemplo del repo dividía dos negativos.
            mismo_signo = (left >= 0) == (right > 0)
            frame.values[args[0].id] = quotient if mismo_signo else -quotient
            return pc + 1
        if op.startswith("divfloor."):
            frame.values[args[0].id] = _idiv(operand(frame, args[1]),
                                             operand(frame, args[2]))
            return pc + 1
        if op in ("shl.i64", "shr.i64"):
            left = frame.values[args[1].id]
            shift = operand(frame, args[2])
            frame.values[args[0].id] = (left << shift) if op == "shl.i64" \
                else (left >> shift)
            return pc + 1
        if op in ("and.i64", "or.i64", "xor.i64"):
            left = operand(frame, args[1])
            right = operand(frame, args[2])
            frame.values[args[0].id] = (left & right if op == "and.i64" else
                                        left | right if op == "or.i64" else
                                        left ^ right)
            return pc + 1

        # ---- booleanos y comparaciones
        if op == "and.bool":
            left = operand(frame, args[1])
            right = operand(frame, args[2])
            frame.values[args[0].id] = 1 if (left and right) else 0
            return pc + 1
        if op == "or.bool":
            left = operand(frame, args[1])
            right = operand(frame, args[2])
            frame.values[args[0].id] = 1 if (left or right) else 0
            return pc + 1
        if op == "xor.bool":
            left = bool(operand(frame, args[1]))
            right = bool(operand(frame, args[2]))
            frame.values[args[0].id] = 1 if left != right else 0
            return pc + 1
        if op == "not.bool":
            frame.values[args[0].id] = 0 if operand(frame, args[1]) else 1
            return pc + 1
        if op == "eq" or op[0:3] == "eq.":
            # `eq.f32` y `eq.f64` son las mismas reglas que `eq`: el `==` de
            # Python ya es la igualdad de IEEE, con `0.0 = -0.0` verdadero y
            # `NaN = NaN` falso. Lo que **no** se puede es comparar los bits,
            # que es lo que hacía el backend al usar `i64.eq` sobre dos `f64`.
            left = operand(frame, args[1])
            right = operand(frame, args[2])
            frame.values[args[0].id] = 1 if left == right else 0
            return pc + 1
        if op[0:3] in ("lt.", "le.", "gt.", "ge."):
            frame.values[args[0].id] = 1 if compare(
                op[:2], frame.values[args[1].id], frame.values[args[2].id]) else 0
            return pc + 1
        if op == "select":
            frame.values[args[0].id] = (operand(frame, args[2])
                                        if operand(frame, args[1])
                                        else operand(frame, args[3]))
            return pc + 1

        # ---- conversiones
        if op == "conv.f64":
            frame.values[args[0].id] = float(operand(frame, args[1]))
            return pc + 1
        if op == "conv.f32":
            frame.values[args[0].id] = to_f32(operand(frame, args[1]))
            return pc + 1
        if op == "conv.i64":
            frame.values[args[0].id] = wrap_int(int(operand(frame, args[1])))
            return pc + 1

        # ---- textos
        if op == "array.len":
            frame.values[args[0].id] = len(frame.values[args[1].id])
            return pc + 1
        if op == "str.len":
            frame.values[args[0].id] = len(frame.values[args[1].id][0])
            return pc + 1
        if op == "str.lenu":
            frame.values[args[0].id] = len(
                frame.values[args[1].id][0].decode("utf-8", "replace"))
            return pc + 1
        if op == "str.byte":
            raw = frame.values[args[1].id][0]
            index = int(operand(frame, args[2]))
            if index < 0 or index >= len(raw):
                raise RuntimeFault(
                    f"índice {index} fuera de un texto de {len(raw)} bytes")
            frame.values[args[0].id] = raw[index]
            return pc + 1
        if op == "str.from_code":
            # ADR 0021. En Python un `str` son caracteres, no bytes, asi que el
            # byte se mete con latin-1: el unico codepage de un a uno, y el que
            # no lanza al pasar de 127. Un `chr(0xE9)` son dos bytes en UTF-8 y
            # uno solo aqui, que es lo que el backend hace con `i32.store8`.
            frame.values[args[0].id] = (bytes(
                (int(operand(frame, args[1])) & 0xFF,)),)
            return pc + 1
        if op == "str.concat":
            left = operand(frame, args[1])[0]
            right = operand(frame, args[2])[0]
            frame.values[args[0].id] = (left + right, len(left) + len(right))
            return pc + 1
        if op == "to.str":
            valor = operand(frame, args[1])
            # Un BOOLEAN viaja en el IR como i64 (regla 3, sin boxing), así que
            # `format_value` ve un `1` y devolvería "1". El módulo usa el mismo
            # formateador que `PRINT` --`source.bool2str`, que es `v ? "TRUE" :
            # "FALSE"`-- y por eso `PRINT TRUE` daba "TRUE" y `STR(TRUE)` daba
            # "1": el oráculo **de sí mismo**. La diferencia se vio comparando
            # las dos salidas, no leyendo el código.
            if isinstance(args[1], Value) and args[1].type.kind == "BOOLEAN":
                texto = "TRUE" if valor else "FALSE"
            else:
                texto = format_value(valor)
            raw = texto.encode("utf-8")
            frame.values[args[0].id] = (raw, len(raw))
            return pc + 1
        if op == "to.num":
            text = operand(frame, args[1])[0].decode("utf-8", "replace").strip()
            frame.values[args[0].id] = _parse_number(text)
            return pc + 1
        if op in ("str.left", "str.right"):
            raw = operand(frame, args[1])[0]
            count = int(operand(frame, args[2]))
            chunk = raw[:count] if count > 0 and op == "str.left" else (
                raw[-count:] if count > 0 else b"")
            frame.values[args[0].id] = (chunk, len(chunk))
            return pc + 1
        if op == "str.mid":
            raw = operand(frame, args[1])[0]
            start = int(operand(frame, args[2]))
            count = int(operand(frame, args[3]))
            chunk = raw[start:start + count] if start >= 0 else b""
            frame.values[args[0].id] = (chunk, len(chunk))
            return pc + 1
        if op == "str.contains":
            raw = operand(frame, args[1])[0]
            needle = operand(frame, args[2])[0]
            frame.values[args[0].id] = 1 if needle in raw else 0
            return pc + 1
        if op == "str.indexof":
            raw = operand(frame, args[1])[0]
            needle = operand(frame, args[2])[0]
            frame.values[args[0].id] = raw.find(needle)
            return pc + 1
        if op == "str.sub":
            raw = frame.values[args[1].id][0]
            index = int(operand(frame, args[2]))
            if index < 0 or index >= len(raw):
                raise RuntimeFault(
                    f"índice {index} fuera de un texto de {len(raw)} bytes")
            # Un texto de un byte es una asignación, y aquí se cuenta.
            self.allocations += 1
            frame.values[args[0].id] = (raw[index:index + 1], 1)
            return pc + 1
        if op == "str.eq_byte":
            raw = operand(frame, args[1])[0]
            index = int(operand(frame, args[2]))
            needle = operand(frame, args[3])[0]
            frame.values[args[0].id] = (
                1 if 0 <= index < len(raw) and raw[index:index + 1] == needle else 0)
            return pc + 1
        if op == "str.compare":
            # Tres vías, como la comparación de BASIC: -1, 0, 1.
            left = operand(frame, args[1])[0]
            right = operand(frame, args[2])[0]
            frame.values[args[0].id] = 0 if left == right else (
                -1 if left < right else 1)
            return pc + 1
        if op == "str.eq":
            equal = operand(frame, args[1])[0] == operand(frame, args[2])[0]
            frame.values[args[0].id] = 1 if equal else 0
            return pc + 1

        # ---- matemática
        if op == "pow.f64":
            frame.values[args[0].id] = float(
                operand(frame, args[1]) ** operand(frame, args[2]))
            return pc + 1
        if op in ("floor.f64", "sin.f64", "cos.f64", "sqrt.f64"):
            argument = operand(frame, args[1])
            table = {"floor.f64": math.floor, "sin.f64": math.sin,
                     "cos.f64": math.cos}
            if op == "sqrt.f64":
                if argument < 0:
                    raise RuntimeFault("raíz de un número negativo")
                frame.values[args[0].id] = math.sqrt(argument)
            else:
                frame.values[args[0].id] = float(table[op](argument))
            return pc + 1

        # ---- arrays
        if op == "array.alloc":
            self.allocations += 1
            count = int(operand(frame, args[2]))
            if count < 0:
                raise RuntimeFault(f"tamaño de array negativo: {count}")
            element_code = args[1]
            frame.values[args[0].id] = [
                zero_of_code(element_code) for _ in range(count)]
            return pc + 1
        if op == "array.init":
            # Misma normalización que en `store.index`: un BOOLEAN se guarda
            # como booleano, para que un literal y una asignación impriman lo
            # mismo.
            value = operand(frame, args[2])
            elemento = args[0].type.element
            if getattr(elemento, "kind", elemento) == "BOOLEAN":
                value = bool(value)
            frame.values[args[0].id][args[1]] = value
            return pc + 1
        if op == "load.index":
            array = frame.values[args[1].id]
            index = int(operand(frame, args[2]))
            if index < 0 or index >= len(array):
                raise RuntimeFault(
                    f"índice {index} fuera de un array de {len(array)}")
            frame.values[args[0].id] = array[index]
            return pc + 1
        if op == "store.index":
            array = frame.values[args[0].id]
            index = int(operand(frame, args[1]))
            if index < 0 or index >= len(array):
                raise RuntimeFault(
                    f"índice {index} fuera de un array de {len(array)}")
            value = frame.values[args[2].id]
            # Un BOOLEAN se guarda como booleano de Python. El IR lo lleva como
            # i32, y sin esto `PRINT` de un array de booleanos sacaba `[1]`
            # mientras que `PRINT a[0]` sacaba `TRUE`: el mismo valor con dos
            # nombres según por dónde se mirara.
            elemento = args[0].type.element
            if getattr(elemento, "kind", elemento) == "BOOLEAN":
                value = bool(value)
            array[index] = value
            return pc + 1

        # ---- llamadas
        if op == "call":
            callee = self.module.funcs[args[0]]
            operands = args[1]
            returns_value = instr.type is not None and instr.type.kind != "VOID"
            if returns_value:
                destination, rest = operands[0], operands[1:]
            else:
                destination, rest = None, operands
            produced = self.call(
                callee, [operand(frame, value) for value in rest])
            if destination is not None:
                frame.values[destination.id] = produced
            return pc + 1
        if op == "call.native":
            operands = args[1]
            returns_value = instr.type is not None and instr.type.kind != "VOID"
            if returns_value:
                destination, rest = operands[0], operands[1:]
            else:
                destination, rest = None, operands
            result = self.native(args[0], rest, frame)
            if destination is not None:
                frame.values[destination.id] = result
            return pc + 1

        raise RuntimeFault(f"el intérprete no implementa '{op}'")

    # --------------------------------------------------------------- nativas

    def native(self, name: str, operands: tuple, frame: Frame):
        values = [frame.values[operand.id] for operand in operands]

        if name == "rgb":
            red, green, blue = (int(v) for v in values[:3])
            alpha = int(values[3]) if len(values) > 3 else 255
            return (alpha << 24) | (red << 16) | (green << 8) | blue
        if name == "key":
            return 1 if self.key_state.get(as_text(values[0]), False) else 0
        if name == "keydown":
            return 0
        if name == "abs":
            return abs(values[0])
        if name == "min":
            return min(values[0], values[1])
        if name == "max":
            return max(values[0], values[1])
        if name == "clamp":
            low, value, high = values
            return max(low, min(value, high))
        if name in ("cls", "cls_default"):
            self.display_list.append(("cls", tuple(values)))
            return None
        if name == "screen":
            title = as_text(values[2]) if len(values) > 2 else ""
            self.screen = (int(values[0]), int(values[1]), title)
            return None
        if name == "screen_off":
            self.screen = None
            return None
        if name in _DRAW_OPS:
            self.display_list.append((name, tuple(values)))
            return None
        raise RuntimeFault(f"el intérprete no implementa la nativa '{name}'")

    # ---------------------------------------------------------------- salida

    def emit(self, text: str) -> None:
        if self.capture_output:
            self.out.append(text)
        else:
            sys.stdout.write(text)


_PRINT_OPS = frozenset({
    "host.print_int", "host.print_num", "host.print_str", "host.print_arr",
    "host.print_bool",
})

_DRAW_OPS = frozenset({
    "pset", "line", "rect", "circle", "triangle", "text", "color",
})


# ------------------------------------------------------------------- helpers


def operand(frame: Frame, item):
    """Resuelve un operando del IR.

    Un operando puede ser un valor (`Value`, referencia a un `%id`) o un
    inmediato escrito en la instrucción (un desplazamiento, un tamaño de array,
    una cantidad de bits de desplazamiento). El compilador elige la forma más
    corta cuando el valor es constante; el intérprete tiene que aceptar las
    dos sin distinguir.
    """
    return frame.values[item.id] if isinstance(item, Value) else item


def _parse_number(text: str) -> float:
    """`VAL`: convierte texto a número. Devuelve 0 si no hay número."""
    stripped = text.strip()
    if not stripped:
        return 0.0
    try:
        return float(stripped)
    except ValueError:
        for index, char in enumerate(stripped):
            if char in "+-." or char.isdigit():
                try:
                    return float(stripped[index:])
                except ValueError:
                    return 0.0
        return 0.0


def arith(op: str, left, right):
    if op == "add":
        value = left + right
    elif op == "sub":
        value = left - right
    elif op == "mul":
        value = left * right
    else:
        if right == 0:
            raise RuntimeFault("división por cero")
        value = left / right
    return wrap_int(value) if isinstance(value, int) else value


def compare(op: str, left, right) -> bool:
    if op == "lt":
        return left < right
    if op == "le":
        return left <= right
    if op == "gt":
        return left > right
    return left >= right


def wrap_int(value: int) -> int:
    """Ajusta a i64 con signo, igual que WASM."""
    if I64_MIN <= value <= I64_MAX:
        return value
    return (value - I64_MIN) % (2 ** 64) + I64_MIN


def _mod(left: int, right: int) -> int:
    """`MOD` de BASIC: el signo del resto lo marca el dividendo."""
    if right == 0:
        raise RuntimeFault("módulo por cero")
    value = abs(left) % abs(right)
    return -value if left < 0 else value


def _idiv(left, right):
    """`DIV`: trunca hacia menos infinito. `\\` usa idiv truncado."""
    if right == 0:
        raise RuntimeFault("división entera por cero")
    return math.floor(left / right)


def to_f32(value) -> float:
    return struct.unpack(">f", struct.pack(">f", float(value)))[0]


def zero_of(type_: Type):
    if type_.kind == "STRING":
        return (b"", 0)
    if type_.kind == "ARRAY":
        return []
    if type_.kind in ("SINGLE", "DOUBLE"):
        return 0.0
    return 0


def as_text(value) -> str:
    if isinstance(value, tuple):
        return value[0].decode("utf-8", "replace")
    return str(value)


def format_value(value) -> str:
    """Formato de `PRINT`. Los enteros salen sin decimales."""
    if isinstance(value, tuple):
        return value[0].decode("utf-8", "replace")
    if isinstance(value, list):
        # Un array se imprime con sus elementos en notación del lenguaje:
        # `[1, 2, 0]`, `["uno", "dos"]`. Antes salía `str(lista)` de Python,
        # que es un accidente --`[True, False]`, `['uno']`-- y no una decisión.
        return "[" + ", ".join(format_element(v) for v in value) + "]"

    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.6g}"
    return str(value)


def format_element(value) -> str:
    """Un elemento dentro de un array impreso.

    Un texto va entre comillas dobles, como se escribe en el fuente, para que
    `["a", "b"]` se pueda volver a leer como un literal.
    """
    if isinstance(value, tuple):
        return '"' + value[0].decode("utf-8", "replace") + '"'
    return format_value(value)


_format = format_value


def run(module: Module, step_limit: int = DEFAULT_STEP_LIMIT,
        max_frames: int | None = None) -> InterpResult:
    """Ejecuta hasta terminar, hasta `max_frames` frames, o hasta que se agoten
    los pasos."""
    interpreter = Interpreter(module, step_limit, max_frames=max_frames)
    try:
        return interpreter.run()
    except FrameLimitReached:
        return InterpResult(
            exit_code=0,
            output="".join(interpreter.out),
            allocations=interpreter.allocations,
            steps=interpreter.steps,
            frames_flipped=interpreter.frames_flipped,
            display_list=tuple(interpreter.display_list),
            screen=interpreter.screen,
        )


def run_with_keys(module: Module, keys: list[str],
                  step_limit: int = DEFAULT_STEP_LIMIT,
                  max_frames: int | None = None) -> InterpResult:
    """Ejecuta con teclas mantenidas. Para los tests de `KEY`."""
    interpreter = Interpreter(module, step_limit, max_frames=max_frames)
    for key in keys:
        interpreter.key_state[key] = True
    try:
        return interpreter.run()
    except FrameLimitReached:
        return InterpResult(
            exit_code=0,
            output="".join(interpreter.out),
            allocations=interpreter.allocations,
            steps=interpreter.steps,
            frames_flipped=interpreter.frames_flipped,
            display_list=tuple(interpreter.display_list),
            screen=interpreter.screen,
        )


def write_ppm(path: str, width: int, height: int, display_list: tuple,
              background: int = 0xFF101018) -> int:
    """Dibuja un frame a PPM. Sirve para testear el 2D sin navegador.

    Es un rasterizador mínimo y deliberadamente simple: su trabajo no es ser
    rápido, es dar una imagen estable contra la que comparar goldens.
    """
    canvas = _Canvas(width, height, background)
    for name, values in display_list:
        canvas.draw(name, values)
    with open(path, "wb") as handle:
        handle.write(f"P6\n{width} {height}\n255\n".encode("ascii"))
        handle.write(canvas.pixels)
    return len(display_list)


class _Canvas:
    """Rasterizador software de los comandos del DisplayList."""

    def __init__(self, width: int, height: int, background: int):
        self.width = width
        self.height = height
        row = _rgb_bytes(background)
        self.pixels = bytearray(row * width * height)

    def draw(self, name: str, values: tuple) -> None:
        if name == "cls":
            color = int(values[0]) if values else 0xFF101018
            row = _rgb_bytes(color)
            for y in range(self.height):
                start = y * self.width * 3
                self.pixels[start:start + self.width * 3] = row * self.width
            return
        if name == "rect":
            x, y, w, h = (int(v) for v in values[:4])
            color = int(values[4]) if len(values) > 4 else 0xFFFFFFFF
            self._fill_rect(x, y, w, h, color)
            return
        if name == "circle":
            x, y, radius = (int(v) for v in values[:3])
            color = int(values[3]) if len(values) > 3 else 0xFFFFFFFF
            self._fill_circle(x, y, radius, color)
            return
        if name == "pset":
            x, y = int(values[0]), int(values[1])
            color = int(values[2]) if len(values) > 2 else 0xFFFFFFFF
            self._pixel(x, y, color)
            return
        if name == "line":
            x1, y1, x2, y2 = (int(v) for v in values[:4])
            color = int(values[4]) if len(values) > 4 else 0xFFFFFFFF
            self._line(x1, y1, x2, y2, color)

    def _pixel(self, x: int, y: int, color: int) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        offset = (y * self.width + x) * 3
        self.pixels[offset:offset + 3] = _rgb_bytes(color)

    def _fill_rect(self, x: int, y: int, w: int, h: int, color: int) -> None:
        row = _rgb_bytes(color)
        for py in range(max(0, y), min(self.height, y + h)):
            start = (py * self.width + max(0, x)) * 3
            span = min(self.width, x + w) - max(0, x)
            if span > 0:
                self.pixels[start:start + span * 3] = row * span

    def _fill_circle(self, cx: int, cy: int, radius: int, color: int) -> None:
        if radius <= 0:
            return
        row = _rgb_bytes(color)
        for dy in range(-radius, radius + 1):
            py = cy + dy
            if not (0 <= py < self.height):
                continue
            span = int((radius * radius - dy * dy) ** 0.5)
            start = (py * self.width + max(0, cx - span)) * 3
            width = min(self.width, cx + span + 1) - max(0, cx - span)
            if width > 0:
                self.pixels[start:start + width * 3] = row * width

    def _line(self, x1: int, y1: int, x2: int, y2: int, color: int) -> None:
        steps = max(abs(x2 - x1), abs(y2 - y1), 1)
        for step in range(steps + 1):
            x = x1 + (x2 - x1) * step // steps
            y = y1 + (y2 - y1) * step // steps
            self._pixel(x, y, color)


def _rgb_bytes(color: int) -> bytes:
    red = (color >> 16) & 0xFF
    green = (color >> 8) & 0xFF
    blue = color & 0xFF
    return bytes((red, green, blue))