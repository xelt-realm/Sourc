"""SIR — Source IR. El contrato entre el frontend y los backends.

Esta es la pieza más importante del compilador, y también la que más
restricciones tiene. Un solo frontend alimenta dos backends, así que el IR
tiene que ser expresivo para WASM y para JavaScript a la vez.

Reglas (ARCHITECTURE.md §3.4), y el porqué de cada una:

  1. Un texto es un valor de dos componentes, no un objeto. En WASM son dos
     i32 contiguos; en el intérprete es un par. Nunca hay un tipo `Str` con
     métodos.
  2. No hay conversiones implícitas. `conv.*` es explícito y siempre lo insertsé
     el typechecker, no el backend.
  3. No hay boxing. Un INTEGER es un entero. Un DOUBLE es un flotante. Un
     BOOLEAN es 0 o 1 y nada más.
  4. Un ARRAY es un descriptor de tres palabras (ptr, len, cap).
  5. Serialización determinista: claves ordenadas, flotantes por patrón de bits.
     Es requisito del bootstrap (ADR 0016).

Representación en memoria de los valores:
    INTEGER   1 palabra (i64)
    SINGLE    1 palabra (f32)
    DOUBLE    1 palabra (f64)
    BOOLEAN   1 palabra (i32, 0 o 1)
    STRING    2 palabras (ptr, len)
    ARRAY     3 palabras (ptr, len, cap)
    COLOR     1 palabra (i32)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from . import ast
from .diagnostics import CompileError, Diagnostic, Span
from .types import BOOL, DOUBLE, INT, SINGLE, STRING, VOID, Typing, Type
from .types import layout_of, type_code, zero_value_of

# Códigos de operando dentro de una instrucción.
IMM = "imm"
FUNC = "func"
NATIVE = "native"
LABEL = "label"


def _f64_bits(value: float) -> str:
    """Serializa un flotante por bits: idéntico en cualquier plataforma."""
    return "0x" + struct.pack(">d", value).hex()


def _f32_bits(value: float) -> str:
    return "0x" + struct.pack(">f", value).hex()


@dataclass(frozen=True)
class Instr:
    """Una instrucción del IR.

    `op` es un nombre estable con sufijo de tipo para las aritméticas:
    `add.i64`, `add.f64`, `conv.f64.i64`...
    `args` es una tupla heterogénea; la forma la fija `op`. Mantener todo en
    una sola lista, en vez de una clase por operación, hace que el intérprete
    sea un `match` y que el serializador sea trivial, y las dos cosas las
    necesita la velocidad de este proyecto.
    """

    op: str
    args: tuple = ()
    type: Type | None = None
    span: Span | None = None

    def render(self, tags: dict[int, str]) -> str:
        parts = [render_operand(arg, tags) for arg in self.args]
        if not parts:
            return f"  {self.op}"
        return f"  {self.op} " + " ".join(parts)


@dataclass(frozen=True)
class Value:
    """Referencia a un valor del IR (un `%id`)."""

    id: int
    type: Type

    def __str__(self) -> str:
        return f"%{self.id}"


def render_operand(arg, tags: dict[int, str]) -> str:
    """Renderiza un operando del IR: valor, inmediato o lista de valores."""
    if isinstance(arg, Value):
        tag = tags.get(arg.id)
        return f"%{arg.id}:{tag}" if tag else f"%{arg.id}"
    if isinstance(arg, float):
        return _f64_bits(arg)
    if isinstance(arg, (list, tuple)):
        return "[" + " ".join(render_operand(item, tags) for item in arg) + "]"
    return str(arg)


# ------------------------------------------------------- ABI y colocación
#
# Dónde vive cada cosa. Esta tabla es el contrato entre el IR y los backends:
# el backend WASM la recorre y falla si encuentra una operación sin entrada, de
# modo que "el backend está completo" nunca es una sorpresa.
#
#   INLINE  -> el backend emite una instrucción WASM. Sin glue, sin imports.
#   HOST    -> import del host, implementado en `source_glue.js`.
#   RUNTIME -> función del módulo de runtime, en Source. En la Fase 2 todavía
#              no existe (llega con ARENA en la Fase 4), así que estas
#              operaciones caen en INLINE u HOST y la tabla dice adónde.
#
# La categoría de destino final está anotada como comentario, para que la deuda
# sea visible y no se pierda: cada HOST que debería ser RUNTIME es un paso
# pendiente de la Fase 4, no un forgotten work.

INLINE = "inline"
HOST = "host"
RUNTIME = "runtime"

# Operaciones del IR que se emiten directamente como instrucción WASM.
# Operaciones que el IR declara y el backend baja, pero que **ningún programa
# puede emitir** con la gramática de hoy. No son un mistake: son el sitio donde
# aterriza una feature que ya se decidió hacer, y quitarlas del IR para que el
# censo quede en cero sería borrar el trabajo de planning del backend.
#
# La condición para que una operación entre aquí es una sola: que no haya
# productor. Por eso la lista se comprueba en dos direcciones --una operación
# reservada que el corpus ya ejercita es una reserva que hay que borrar, y una
# operación sin ejercitar que no está aquí es un olvido.
#
#   y    &            sobran para cuando lleguen
#   x    |            sobran para cuando lleguen
#   shl  shr          sobran para cuando lleguen
#   /    con los dos operandos INTEGER: `/` es división verdadera y siempre da
#         DOUBLE (Fase 2). La división entera es `\` y `DIV`, que bajan a
#         `idiv.i64` y `divfloor.i64`.
#   /    con los dos operandos SINGLE: por la misma regla que la anterior.
#   INT() como conversión: no haycasts explícitos; el tipo sale del contexto.
#   el cero como operación: `zero_value` emite `const.i64`/`const.f64` con el
#         valor puesto, porque un `const.zero` obligaría a materializar un
#         literal en un segundo paso.
#   LEN como import: la longitud la devuelve el propio formateador del host
#         --`int2str` y `num2str` devuelven el puntero--, así que no hace
#         falta una llamada aparte.
OPS_RESERVADOS: dict[str, str] = {
    "and.i64": "operadores de bits; no hay sintaxis que los produzca",
    "or.i64": "operadores de bits; no hay sintaxis que los produzca",
    "xor.i64": "operadores de bits; no hay sintaxis que los produzca",
    "shl.i64": "operadores de bits; no hay sintaxis que los produzca",
    "shr.i64": "operadores de bits; no hay sintaxis que los produzca",
    "div.i64": "`/` es división verdadera y siempre da DOUBLE",
    "div.f32": "`/` es división verdadera y siempre da DOUBLE",
    "conv.i64": "no hay casts explícitos; el tipo sale del contexto",
    "const.zero": "`zero_value` emite el literal con su valor",
    "str_len": "el formateador del host ya devuelve la longitud",
}

INLINE_OPS = frozenset({
    # control y literales
    "const.i64", "const.f64", "const.bool", "const.strref", "const.str",
    "const.zero", "to.bool",
    # memoria local y de datos
    "store.slot", "load.slot", "load.index", "store.index",
    "array.alloc", "array.init", "array.len",
    # aritmética
    "add.i64", "sub.i64", "mul.i64", "div.i64", "rem.i64", "idiv.i64",
    "divfloor.i64", "neg.i64",
    "add.f32", "sub.f32", "mul.f32", "div.f32", "neg.f32",
    "add.f64", "sub.f64", "mul.f64", "div.f64", "rem.f64", "idiv.f64",
    "divfloor.f64", "neg.f64",
    "shl.i64", "shr.i64", "and.i64", "or.i64", "xor.i64",
    # comparación y lógica
    "eq", "eq.f32", "eq.f64",
    "and.bool", "or.bool", "xor.bool", "not.bool", "select",
    "lt.i64", "le.i64", "gt.i64", "ge.i64",
    "lt.f32", "le.f32", "gt.f32", "ge.f32",
    "lt.f64", "le.f64", "gt.f64", "ge.f64",
    # conversiones
    "conv.i64", "conv.f32", "conv.f64",
    # math: WASM las tiene como instrucción, así que no pasan por el host.
    "sqrt.f64", "floor.f64",
    # `pow.f64` **no** la tiene: el `^` va al host como `source.pow`.
    "pow.f64",
    # textos: operates sobre memoria lineal, sin glue
    "str.len", "str.lenu", "str.byte", "str.sub", "str.concat",
    "str.compare", "str.eq", "str.eq_byte", "str.left", "str.right",
    "str.mid", "str.contains", "str.indexof", "str.from_code",
    # control de flujo
    "label", "br", "br_if.false", "ret", "call",
})

# Operaciones que hablan con el host. La clave es el nombre del op y el valor el
# nombre exacto del import; el glue implementa esa firma y sólo esa.
HOST_ABI = {
    "host.print_str":   "source.print_str",     # destino: RUNTIME (Fase 4)
    "host.print_int":   "source.print_int",     # destino: RUNTIME
    "host.print_num":   "source.print_num",     # destino: RUNTIME
    "host.print_bool":  "source.print_bool",    # destino: RUNTIME
    "flip":             "source.flip",          # destino: source_2d (Fase 3)
    # El salto de línea es una llamada al host, no una instrucción: WASM no
    # tiene ninguna y escribir un byte en memoria desde el módulo obligaría al
    # glue a conocer la disposición de los datos.
    "newline":          "source.newline",       # destino: RUNTIME (Fase 4)
}

# Operaciones que el backend baja a una función **del módulo**, no a un import.
# No son ni inline --son una función entera-- ni host --el host no las ve--, así
# que necesitan su propia lista o el tripwire las daría por sin bajar.
OPS_DEL_MODULO = frozenset({
    "host.print_arr",
})

# La función de formateo de números vive en el glue durante la Fase 2: un
# formateador de DOUBLE a texto son ~200 instrucciones de WASM que no vale la
# pena escribir en el backend, y ~12 líneas de JavaScript que sí.
# `STR(x)` puede recibir un INTEGER, un DOUBLE, un BOOLEAN o un COLOR, y cada
# uno necesita su propio formateador en el host: pasar un entero por f64
# cambiaría sus últimos dígitos. El mapping está aquí y no en el backend porque
# es el SIR quien declara qué imports usa el módulo.
HOST_FORMATTING = {
    "to.str": {
        "INTEGER":  "source.int2str",
        "COLOR":    "source.int2str",
        "SINGLE":   "source.num2str",
        "DOUBLE":   "source.num2str",
        "BOOLEAN":  "source.bool2str",
    },                                            # destino: RUNTIME (Fase 4)
    "to.num":  "source.str2num",   # destino: RUNTIME (Fase 4)
    "str_len": "source.str_len",    # destino: RUNTIME (Fase 4)
}

# Nativas del lenguaje y su colocación. Lo que WASM ya tiene como instrucción,
# se emite inline; lo que necesita estado externo, va al host.
NATIVE_PLACEMENT = {
    "abs":      (INLINE, None),
    "min":      (INLINE, None),
    "max":      (INLINE, None),
    "clamp":    (INLINE, None),
    "sqrt":     (INLINE, None),
    "floor":    (INLINE, None),
    # `sin` y `cos` no son inline: el MVP de WASM no tiene instrucción para
    # ninguna de las dos. Emitirlas inline obligaría a escribir un polinomio de
    # Taylor en el backend, con su error de redondeo y su bytes de más. Pasan
    # por el host como el resto de las que no son una instrucción.
    "sin":      (HOST,   "source.sin"),
    "cos":      (HOST,   "source.cos"),
    # `pow` tampoco es instrucción del MVP. `sqrt` sí, y por eso sigue inline.
    "pow":      (HOST,   "source.pow"),
    # ADR 0021: reservar un byte en el heap y escribirlo. Ni tabla de datos
    # ni una funcion en el pegamento.
    "chr":      (INLINE, None),
    "rgb":      (INLINE, None),   # sólo empaquetar cuatro bytes
    "key":      (HOST,   "source.key"),
    "keydown":  (HOST,   "source.keydown"),
    # dibujo: Fase 3 las mueve al módulo source_2d.wasm
    "cls":          (HOST, "source.dl_cls"),
    "cls_default":  (HOST, "source.dl_cls"),
    "screen":       (HOST, "source.dl_screen"),
    "screen_off":   (HOST, "source.dl_screen_off"),
    "rect":         (HOST, "source.dl_rect"),
    "circle":       (HOST, "source.dl_circle"),
    "pset":         (HOST, "source.dl_pset"),
    "line":         (HOST, "source.dl_line"),
    "triangle":     (HOST, "source.dl_triangle"),
    "text":         (HOST, "source.dl_text"),
    "color":        (HOST, "source.dl_color"),
}

_PRINT_OP = {
    "BOOLEAN": "host.print_bool",
    "STRING": "host.print_str",
    "SINGLE": "host.print_num",
    "DOUBLE": "host.print_num",
    "ARRAY": "host.print_arr",
    "INTEGER": "host.print_int",
    "COLOR": "host.print_int",
}

@dataclass
class Func:
    index: int
    name: str
    params: list[tuple[str, Type]]
    return_type: Type
    locals: list[tuple[str, Type]] = field(default_factory=list)
    instrs: list[Instr] = field(default_factory=list)
    next_value: int = 0
    next_label: int = 0
    slot_index: dict[str, int] = field(default_factory=dict)
    span: Span | None = None
    _labels: dict[str, int] | None = None

    def labels(self) -> dict[str, int]:
        """Posición de cada etiqueta. Se calcula una vez por función.

        El intérprete lo consulta en cada llamada, así que cachearlo aquí
        importa: una función recursiva de 10000 niveles no debe recorrer sus
        propias instrucciones para encontrar sus etiquetas.
        """
        if self._labels is None:
            positions: dict[str, int] = {}
            for position, instr in enumerate(self.instrs):
                if instr.op == "label":
                    positions[instr.args[0]] = position
            self._labels = positions
        return self._labels

    def local(self, name: str) -> int:
        """Índice de slot de una variable local. El tipo se consulta aparte."""
        if name not in self.slot_index:
            self.slot_index[name] = len(self.locals)
            self.locals.append((name, INT))
        return self.slot_index[name]

    def set_local_type(self, name: str, type_: Type) -> None:
        index = self.local(name)
        self.locals[index] = (name, type_)

    def new_label(self) -> str:
        label = f"L{self.next_label}"
        self.next_label += 1
        return label


@dataclass
class Module:
    """Un módulo compilado: una unidad de código con su runtime declarado."""

    name: str
    lang_version: str
    strings: list[bytes] = field(default_factory=list)
    string_index: dict[bytes, int] = field(default_factory=dict)
    funcs: list[Func] = field(default_factory=list)
    runtime_modules: list[str] = field(default_factory=list)
    host_imports: list[str] = field(default_factory=list)
    globals: list[tuple[str, Type]] = field(default_factory=list)
    _string_refs: dict[int, int] = field(default_factory=dict)
    note: str = ""

    def add_string(self, text: str) -> int:
        raw = text.encode("utf-8")
        if raw not in self.string_index:
            self.string_index[raw] = len(self.strings)
            self.strings.append(raw)
        return self.string_index[raw]

    def add_func(self, func: Func) -> int:
        func.index = len(self.funcs)
        self.funcs.append(func)
        return func.index

    def uses_host(self, name: str) -> None:
        if name not in self.host_imports:
            self.host_imports.append(name)


# ---------------------------------------------------------------- serializar


# Instrucciones cuyo primer argumento es el valor que definen.
PRODUCING_OPS = frozenset({
    "const.i64", "const.f64", "const.bool", "to.bool",
    "const.strref", "const.str", "const.zero",
    "add.i64", "sub.i64", "mul.i64", "div.i64", "rem.i64", "idiv.i64",
    "divfloor.i64", "neg.i64",
    "add.f32", "sub.f32", "mul.f32", "div.f32", "neg.f32",
    "add.f64", "sub.f64", "mul.f64", "div.f64", "rem.f64", "idiv.f64",
    "divfloor.f64", "neg.f64",
    "shl.i64", "shr.i64", "and.i64", "or.i64", "xor.i64",
    "and.bool", "or.bool", "xor.bool", "not.bool",
    "str.concat", "str.compare", "str.eq", "str.len", "str.lenu", "str.byte",
    "array.len", "str.sub", "str.eq_byte", "str.from_code",
    "to.str", "to.num", "str.left", "str.right", "str.mid", "str.contains",
    "str.indexof",
    "eq", "eq.f32", "eq.f64", "pow.f64", "floor.f64",
    "sin.f64", "cos.f64", "sqrt.f64",
    "lt.i64", "le.i64", "gt.i64", "ge.i64",
    "lt.f32", "le.f32", "gt.f32", "ge.f32",
    "lt.f64", "le.f64", "gt.f64", "ge.f64",
    "conv.i64", "conv.f32", "conv.f64",
    # math: WASM las tiene como instrucción, así que no pasan por el host.
    "sin.f64", "cos.f64", "sqrt.f64", "floor.f64", "pow.f64",
    "select", "load.slot", "load.index", "array.alloc",
    "call.native", "call",
})

# Sufijo corto con el que se anota el tipo de un valor en el texto del IR.
_TYPE_TAG = {
    "INTEGER": "i64", "SINGLE": "f32", "DOUBLE": "f64", "BOOLEAN": "b",
    "STRING": "str", "ARRAY": "arr", "COLOR": "c", "VOID": "void",
}


def result_of(instr: Instr):
    """El valor que define la instrucción, o None.

    Casi toda operación define su resultado en el primer operando. Las llamadas
    son la excepción: `call <índice> [destino, argumentos...]`, con el destino
    dentro de la lista de operandos porque el índice va delante. Sin este caso,
    el destino de una llamada parece no tener local y el backend falla al
    escribirlo.
    """
    if instr.op in ("call", "call.native"):
        operands = instr.args[1] if len(instr.args) > 1 else None
        if not isinstance(operands, list) or not operands:
            return None
        first = operands[0]
        return first if isinstance(first, Value) else None
    if instr.op not in PRODUCING_OPS:
        return None
    first = instr.args[0] if instr.args else None
    return first if isinstance(first, Value) else None


def serialize(module: Module) -> str:
    """Serialización canónica y determinista del IR.

    Este texto es lo que comparan los tests golden, lo que sirve de clave de
    caché, y lo que seMira en el bootstrap para comparar Stage 0 y Stage 1 sin
    comparar binarios (ADR 0016).
    """
    out: list[str] = []
    out.append(f"module {module.name} lang={module.lang_version}")
    if module.runtime_modules:
        out.append("  use " + " ".join(module.runtime_modules))
    for name in module.host_imports:
        out.append(f"  host {name}")
    for index, raw in enumerate(module.strings):
        out.append(f"  str@{index} len={len(raw)} bytes={list(raw)}")

    for func in module.funcs:
        out.append("")
        sig = ", ".join(f"{name}: {type_}" for name, type_ in func.params)
        out.append(f"func @{func.name}({sig}) -> {func.return_type}:")

        # Los tipos de los valores se calculan antes de renderizar, para que el
        # texto no dependa del formato de la instrucción.
        tags: dict[int, str] = {}
        for instr in func.instrs:
            value = result_of(instr)
            if value is not None and instr.type is not None:
                tags[value.id] = _TYPE_TAG.get(instr.type.kind, "?")

        for slot, (name, type_) in enumerate(func.locals):
            out.append(f"  .slot {slot} {name}: {type_}")
        if not func.locals:
            out.append("  .slot none")

        for instr in func.instrs:
            out.append(instr.render(tags))

    out.append("")
    out.append(f"  ; {count_instrs(module)} instrucciones, "
               f"{len(module.funcs)} funciones, "
               f"{len(module.strings)} constantes de texto")
    return "\n".join(out) + "\n"


def count_instrs(module: Module) -> int:
    return sum(len(func.instrs) for func in module.funcs)


def op_histogram(module: Module) -> dict[str, int]:
    """Recuento de operaciones por nombre. Base de `--stats` y del presupuesto."""
    histogram: dict[str, int] = {}
    for func in module.funcs:
        for instr in func.instrs:
            histogram[instr.op] = histogram.get(instr.op, 0) + 1
    return dict(sorted(histogram.items()))


# ------------------------------------------------------------------- builder


class Builder:
    """Baja el AST tipado a SIR.

    Recibe el resultado del typechecker y no vuelve a mirar el fuente: si un
    tipo no está en la tabla, es un bug del compilador, no del programa, y se
    lanza `CompileError` en vez de adivinar.
    """

    def __init__(self, typing: Typing, module_name: str = "main",
                 lang_version: str = "1.0"):
        self.typing = typing
        self.module = Module(name=module_name, lang_version=lang_version)
        self.current: Func | None = None
        self.func_types = typing.functions
        # `(nombre, tipo)` de cada global, en el orden en que se declararon.
        # Lo rellena `build()` al terminar `main`, porque los globales sólo se
        # descubren al bajar las sentencias de nivel superior.
        self.globales: list[tuple[str, Type]] = []

    # ------------------------------------------------------------------ utils

    def emit(self, op: str, *args, type_: Type | None = None,
             span: Span | None = None) -> Instr:
        instr = Instr(op=op, args=args, type=type_, span=span)
        self.current.instrs.append(instr)
        return instr

    def value(self, type_: Type, span: Span | None = None) -> Value:
        func = self.current
        result = Value(id=func.next_value, type=type_)
        func.next_value += 1
        return result

    def emit_result(self, op: str, type_: Type, *args,
                    span: Span | None = None) -> Value:
        result = self.value(type_, span)
        self.emit(op, result, *args, type_=type_, span=span)
        return result

    def type_of(self, node: ast.Node) -> Type:
        return self.typing.of(node)

    # --------------------------------------------------------------- programa

    def build(self, program: ast.Program) -> Module:
        self.module.runtime_modules = list(self.typing.runtime_modules)

        # Todas las FUNCTION se registran antes de bajar nada, para que una
        # función pueda llamar a otra declarada más abajo. Es lo que permite
        # escribir el runtime en Source (ADR 0015) sin declarar en orden.
        shells: list[tuple[str, Func, object]] = []
        for name, info in self.func_types.items():
            shell = Func(index=0, name=name, params=list(info.params),
                         return_type=info.return_type, span=info.node.span)
            self.module.add_func(shell)
            shells.append((name, shell, info))

        main = Func(index=0, name="main", params=[], return_type=INT)
        self.module.add_func(main)
        self.current = main
        for statement in program.body:
            self.statement(statement)
        exit_code = self.emit_result("const.i64", INT, 0, span=program.span)
        self.emit("ret", exit_code, type_=INT, span=program.span)
        self.current = None

        # Los globales se **descubren** al bajar `main`, y `main` se baja antes
        # que ningún cuerpo de FUNCTION. Se guardan para que cada función los
        # registre con su tipo bueno.
        #
        # Sin esto, una FUNCTION que lee un global `SINGLE` lo declaraba como
        # INTEGER --porque `Func.local()` presupone INTEGER-- y el módulo no
        # validaba: `local.set` de un i64 en un local declarado f32. El síntoma
        # era E0065 en la instrucción 1 de la función, a cuatro líneas de la
        # causa, y sólo con el tipo equivocado en la segunda.
        self.globales = list(main.locals)

        for name, shell, info in shells:
            self.fill_function(shell, info)
        return self.module

    def fill_function(self, func: Func, info) -> None:
        previous = self.current
        self.current = func

        # Los parámetros van **primero**, siempre: son los argumentos de la
        # llamada y su posición es la convención. Los globales se añaden
        # después, y sólo si no hay un local con ese nombre, para que un local
        # que tape a un global conserve su propio tipo.
        for pname, ptype in func.params:
            func.set_local_type(pname, ptype)
        for nombre, tipo in self.globales:
            if nombre not in func.slot_index:
                func.set_local_type(nombre, tipo)

        for statement in info.node.body:
            self.statement(statement)

        # Caer fuera del cuerpo devuelve el valor cero del tipo de retorno
        # (GRAMMAR.md, 2.2 regla 3). Sólo hace falta si el cuerpo no termina
        # en RETURN: si lo hace, el código de después es inalcanzable y
        # ocuparía bytes que el usuario final paga.
        if func.instrs and func.instrs[-1].op == "ret":
            return
        if info.return_type.kind == "VOID":
            self.emit("ret", type_=VOID, span=info.node.span)
        else:
            zero = self.emit_result("const.zero", info.return_type)
            self.emit("ret", zero, type_=info.return_type, span=info.node.span)

        self.current = previous

    # -------------------------------------------------------------- sentencia

    def statement(self, node: ast.Node) -> None:
        if isinstance(node, ast.Use):
            return
        if isinstance(node, ast.ImportFrom):
            return
        if isinstance(node, (ast.Goto, ast.Gosub, ast.Label)):
            raise CompileError(Diagnostic(
                "E0043", "GOTO y GOSUB aún no bajan a IR",
                node.span,
                "llegarán en la Fase 2; el compilador todavía no sabe "
                "generar saltos hacia atrás",
            ))
        if isinstance(node, ast.Dim):
            return self.statement_dim(node)
        if isinstance(node, ast.Assign):
            return self.statement_assign(node)
        if isinstance(node, ast.If):
            return self.statement_if(node)
        if isinstance(node, ast.While):
            return self.statement_while(node)
        if isinstance(node, ast.For):
            return self.statement_for(node)
        if isinstance(node, ast.FunctionDecl):
            return  # ya construido aparte, por nombre
        if isinstance(node, ast.Arena):
            raise CompileError(Diagnostic(
                "E0044", "ARENA todavía no baja a IR",
                node.span, "las arenas llegan en la Fase 4",
            ))
        if isinstance(node, ast.Return):
            return self.statement_return(node)
        if isinstance(node, ast.Call):
            # Llamada a una FUNCTION usada como sentencia. El typechecker ya
            # comprobó que no devuelve nada; aquí sólo se baja la llamada.
            self.expr(node)
            return

        if isinstance(node, ast.Primitive):
            return self.statement_primitive(node)
        if isinstance(node, ast.ColorStmt):
            return self.statement_primitive(
                ast.Primitive(name="COLOR", args=(node.index,), span=node.span))
        raise CompileError(Diagnostic(
            "E0012", f"el builder no sabe bajar {type(node).__name__}",
            node.span))

    def statement_dim(self, node: ast.Dim) -> None:
        type_ = self.typing.decl_types[id(node)]
        slot = self.current.local(node.name)
        self.current.set_local_type(node.name, type_)

        if node.value is None:
            value = self.zero_value(type_, node.span)
        elif isinstance(node.value, ast.ArrayLit) and type_.kind == "ARRAY":
            value = self.expr_array_literal(node.value, declared=type_)
        else:
            value = self.coerce(self.expr(node.value), type_, node.span)

        self.emit("store.slot", slot, value, type_=type_, span=node.span)

    def zero_value(self, type_: Type, span: Span) -> Value:
        if type_.kind == "ARRAY":
            return self.emit_result("array.alloc", type_,
                                    type_code(type_.element or INT), 0, span=span)
        if type_.kind == "STRING":
            index = self.module.add_string("")
            ref = self.value(INT, span)
            self.emit("const.i64", ref, index, type_=INT, span=span)
            return self.emit_result("const.str", STRING, ref, 0, span=span)
        literal = zero_value_of(type_)
        if isinstance(literal, float) or type_.kind in ("SINGLE", "DOUBLE"):
            return self.emit_result("const.f64", type_, float(literal), span=span)
        return self.emit_result("const.i64", type_, int(literal), span=span)

    def statement_assign(self, node: ast.Assign) -> None:
        value = self.expr(node.value)
        target = node.target

        if isinstance(target, ast.Name):
            slot = self.current.local(target.value)
            target_type = self.type_of(target)
            value = self.coerce(value, target_type, node.span)
            self.emit("store.slot", slot, value, type_=target_type, span=node.span)
            return

        if isinstance(target, ast.Index):
            array = self.expr(target.target)
            index = self.expr(target.index)
            element = (array.type.element or INT) if array.type.kind == "ARRAY" \
                else self.type_of(target.target)
            coerced = self.coerce(value, element, node.span)
            self.emit("store.index", array, index, coerced, type_=element,
                      span=node.span)
            return

        raise CompileError(Diagnostic(
            "E0026", "no se puede asignar a esta expresión", node.span))

    def statement_if(self, node: ast.If) -> None:
        condition = self.bool_expr(node.condition)
        else_label = self.current.new_label()
        end_label = self.current.new_label()

        self.emit("br_if.false", condition, else_label, span=node.span)
        for inner in node.then_body:
            self.statement(inner)
        if node.else_body is not None:
            self.emit("br", end_label, span=node.span)
        self.emit("label", else_label, span=node.span)
        if node.else_body is not None:
            for inner in node.else_body:
                self.statement(inner)
            self.emit("label", end_label, span=node.span)

    def statement_while(self, node: ast.While) -> None:
        start_label = self.current.new_label()
        end_label = self.current.new_label()

        self.emit("label", start_label, span=node.span)
        condition = self.bool_expr(node.condition)
        self.emit("br_if.false", condition, end_label, span=node.span)
        for inner in node.body:
            self.statement(inner)
        self.emit("br", start_label, span=node.span)
        self.emit("label", end_label, span=node.span)

    def statement_for(self, node: ast.For) -> None:
        loop_type = self.typing.decl_types[id(node)]
        slot = self.current.local(node.var)
        self.current.set_local_type(node.var, loop_type)

        start_value = self.expr(node.start)
        self.emit("store.slot", slot, start_value, type_=loop_type, span=node.span)

        stop_value = self.expr(node.stop)
        step_value = (self.expr(node.step) if node.step is not None
                      else self.literal(loop_type, 1, node.span))

        start_label = self.current.new_label()
        end_label = self.current.new_label()

        self.emit("label", start_label, span=node.span)

        index = self.emit_result("load.slot", loop_type, slot, span=node.span)
        # La comparación depende del signo del paso, que puede no ser constante.
        ascending = self.emit_result(
            _compare_op(loop_type, "le"), BOOL, index, stop_value, span=node.span)
        descending = self.emit_result(
            _compare_op(loop_type, "ge"), BOOL, index, stop_value, span=node.span)
        zero = self.literal(loop_type, 0, node.span)
        step_negative = self.emit_result(
            _compare_op(loop_type, "lt"), BOOL, step_value, zero, span=node.span)
        condition = self.emit_result(
            "select", BOOL, step_negative, descending, ascending, span=node.span)
        self.emit("br_if.false", condition, end_label, span=node.span)

        for inner in node.body:
            self.statement(inner)

        current = self.emit_result("load.slot", loop_type, slot, span=node.span)
        next_value = self.emit_result(
            _arith_op(loop_type, "add"), loop_type, current, step_value, span=node.span)
        self.emit("store.slot", slot, next_value, type_=loop_type, span=node.span)
        self.emit("br", start_label, span=node.span)
        self.emit("label", end_label, span=node.span)

    def statement_return(self, node: ast.Return) -> None:
        if node.value is None:
            self.emit("ret", type_=VOID, span=node.span)
            return
        value = self.expr(node.value)
        self.emit("ret", value, type_=value.type, span=node.span)

    def statement_primitive(self, node: ast.Primitive) -> None:
        args = [self.expr(arg) for arg in node.args]
        self.emit_primitive(node.name, args, node)

    def emit_primitive(self, name: str, args: list[Value],
                       node: ast.Node) -> None:
        module = self.module
        span = node.span

        if name == "PRINT":
            # `PRINT "a" ; "b"` escribe los dos en la misma línea y el salto
            # de línea va al final. Sin `;`, cada argumento lleva el suyo.
            for position, arg in enumerate(args):
                self.emit_print(arg, span)
                last = position == len(args) - 1
                if not node.inline or last:
                    self.module.uses_host(HOST_ABI["newline"])
                    self.emit("newline", span=span)
            return

        if name == "CLS":
            if args:
                self.native_call("cls", args, VOID, span)
            else:
                self.native_call("cls_default", [], VOID, span)
            return

        if name == "FLIP":
            self.module.uses_host(HOST_ABI["flip"])
            self.emit("flip", span=span)
            return

        if name == "SCREEN":
            width = args[0] if len(args) > 0 else self.literal(INT, 640, span)
            height = args[1] if len(args) > 1 else self.literal(INT, 480, span)
            title = args[2] if len(args) > 2 else self.empty_string(span)
            self.native_call("screen", [width, height, title], VOID, span)
            return

        if name == "SCREEN_OFF":
            self.native_call("screen_off", [], VOID, span)
            return

        self.native_call(name.lower(), args, VOID, span)

    def emit_print(self, value: Value, span: Span) -> None:
        op = _PRINT_OP.get(value.type.kind, "host.print_int")
        if op == "host.print_arr":
            # `PRINT` de un array **no** pide nada al host. El formateo lo hace
            # `@__printArray`, dentro del módulo: es semántica del lenguaje y
            # pesa en cada página si vive en el glue. Lo único que el host sigue
            # haciendo es formatear un número suelto.
            self.emit(op, value, span=span)
            return
        self.module.uses_host(HOST_ABI[op])
        if op in ("host.print_bool", "host.print_str"):
            self.emit(op, value, span=span)
        else:
            self.emit(op, value, type_=INT, span=span)

    # -------------------------------------------------------------- expresión

    def expr(self, node: ast.Node) -> Value:
        if isinstance(node, ast.IntLit):
            return self.literal(INT, node.value, node.span)
        if isinstance(node, ast.FloatLit):
            return self.literal(DOUBLE, node.value, node.span)
        if isinstance(node, ast.StringLit):
            return self.string_const(node.value, node.span)
        if isinstance(node, ast.BoolLit):
            # `const.bool`, no `const.i64`: un BOOLEAN tiene que tener una sola
            # representación (i32), o el backend no puede elegir el tipo de un
            # local. Sin esta instrucción, `DIM b AS BOOLEAN = TRUE` metía un
            # i64 en un slot declarado BOOLEAN, y `PRINT b` lo leía como i32.
            return self.emit_result("const.bool", BOOL, 1 if node.value else 0,
                                    span=node.span)
        if isinstance(node, ast.NilLit):
            return self.literal(INT, 0, node.span)
        if isinstance(node, ast.Name):
            return self.load_name(node)
        if isinstance(node, ast.Unary):
            return self.expr_unary(node)
        if isinstance(node, ast.Binary):
            return self.expr_binary(node)
        if isinstance(node, ast.Index):
            return self.expr_index(node)
        if isinstance(node, ast.Call):
            return self.expr_call(node)
        if isinstance(node, ast.ArrayLit):
            return self.expr_array_literal(node)
        if isinstance(node, ast.Interpolate):
            inner = self.expr(node.value)
            if inner.type.kind == "STRING":
                return inner
            self.note_formatting("to.str", inner.type.kind)
            return self.emit_result("to.str", STRING, inner, span=node.span)

        if isinstance(node, ast.NewArray):
            return self.expr_new_array(node)
        raise CompileError(Diagnostic(
            "E0013", f"el builder no sabe bajar la expresión {type(node).__name__}",
            node.span))

    def literal(self, type_: Type, value, span: Span) -> Value:
        if type_.kind in ("SINGLE", "DOUBLE"):
            return self.emit_result("const.f64", type_, float(value), span=span)
        return self.emit_result("const.i64", type_, int(value), span=span)

    def string_const(self, text: str, span: Span) -> Value:
        index = self.module.add_string(text)
        ptr = self.value(INT, span)
        self.emit("const.strref", ptr, index, type_=INT, span=span)
        result = self.value(STRING, span)
        self.emit("const.str", result, ptr, len(text.encode("utf-8")),
                  type_=STRING, span=span)
        self.module._string_refs[result.id] = index
        return result

    def empty_string(self, span: Span) -> Value:
        return self.string_const("", span)

    def load_name(self, node: ast.Name) -> Value:
        slot = self.current.local(node.value)
        type_ = self.type_of(node)
        return self.emit_result("load.slot", type_, slot, span=node.span)

    def expr_unary(self, node: ast.Unary) -> Value:
        if node.op == "-" and isinstance(node.operand, ast.IntLit):
            # Un menos pegado a un entero se resuelve **antes** de emitir nada.
            #
            # `-9223372036854775808` --el entero más pequeño del i64-- no se
            # puede bajar como `const.i64 9223372036854775808` seguido de `neg`:
            # esa constante no es representable, su LEB128 con signo necesita un
            # byte de más y el módulo entero no valida. Bajarla ya con el signo
            # puesto sí cabe, y de paso es lo que significa.
            #
            # La comprobación tiene que ir **antes** de `self.expr(operando)`
            # porque si no la constante positiva --que no se puede representar--
            # ya está en el cuerpo, y aunque luego se pliegue el signo queda
            # muerta y el módulo sigue sin validar.
            return self.emit_result("const.i64", INT, -int(node.operand.value),
                                    span=node.span)
        operand = self.expr(node.operand)
        if node.op == "-":
            # Una constante que llega por otro camino (una variable ya plegada,
            # un `const.i64` emitido antes) también se pliega aquí, que es donde
            # se puede sin dejar rastro.
            if operand.type.kind == INT.kind:
                entero = self._constante_de(operand)
                if entero is not None:
                    return self.emit_result("const.i64", INT, -entero,
                                            span=node.span)
            return self.emit_result(_arith_op(operand.type, "neg"), operand.type,
                                    operand, span=node.span)
        return self.emit_result("not.bool", BOOL, operand, span=node.span)

    def _constante_de(self, value: Value) -> int | None:
        """El entero que define a `value`, si lo define una `const.i64`.

        Hay que mirar hacia atrás porque el IR no lleva el valor dentro del
        operando: `%3` podría estar definido hace veinte instrucciones y no ser
        una constante. La búsqueda se detiene en cuanto encuentra otra definición
        de ese mismo `%id` --una reasignación es un `store`, no un `const`--, así
        que no puede coger el valor de una versión anterior de la variable.
        """
        for instr in reversed(self.current.instrs):
            destino = result_of(instr)
            if destino is None or destino.id != value.id:
                continue
            if instr.op == "const.i64":
                return instr.args[1]
            return None
        return None

    def expr_binary(self, node: ast.Binary) -> Value:
        if node.op == "&":
            left = self.expr(node.left)
            right = self.expr(node.right)
            return self.emit_result("str.concat", STRING, left, right, span=node.span)

        if node.op == "=":
            # ADR 0004: `mapa[fila][col] = "#"` no debe asignar nada.
            #
            # Es el caso más importante de todo el lenguaje en el laberinto: sin
            # esto, cada celda del mapa por frame crearía un texto de un byte y
            # el game loop asignaría 14 400 veces por segundo. Con esto, cero.
            optimized = self._byte_compare(node.left, node.right)
            if optimized is not None:
                return optimized
            optimized = self._byte_compare(node.right, node.left)
            if optimized is not None:
                return optimized
            left = self.expr(node.left)
            right = self.expr(node.right)
            if left.type.kind == "STRING" or right.type.kind == "STRING":
                return self.emit_result("str.eq", BOOL, left, right, span=node.span)
            return self._numeric_eq(left, right, node.span)

        left = self.expr(node.left)
        right = self.expr(node.right)

        if node.op == "<>":
            result = self._numeric_eq(left, right, node.span)
            return self.emit_result("not.bool", BOOL, result, span=node.span)
        if node.op in ("AND", "OR", "XOR"):
            return self.emit_result(
                {"AND": "and.bool", "OR": "or.bool", "XOR": "xor.bool"}[node.op],
                BOOL, left, right, span=node.span)
        if node.op in ("<", "<=", ">", ">="):
            suffix = {"<": "lt", "<=": "le", ">": "gt", ">=": "ge"}[node.op]
            # Igual que la aritmética: los operandos se coercifican al tipo de
            # la comparación. `x < 0.5` con `x` SINGLE emitía `lt.f32` con un
            # operando DOUBLE.
            result_type = self._arith_type(left, right)
            left = self.coerce(left, result_type, node.left.span)
            right = self.coerce(right, result_type, node.right.span)
            return self.emit_result(_compare_op(result_type, suffix),
                                    BOOL, left, right, span=node.span)

        if node.op == "^":
            left = self.to_double(left, node.span)
            right = self.to_double(right, node.span)
            self._math_host("pow")
            return self.emit_result("pow.f64", DOUBLE, left, right,
                                    span=node.span)
        if node.op in ("MOD", "DIV"):
            # DIV trunca hacia menos infinito, MOD sigue el signo del
            # dividendo, y `\` trunca hacia cero. No es lo mismo y no se puede
            # bajar todo a la misma instrucción.
            result_type = self._arith_type(left, right)
            left = self.coerce(left, result_type, node.left.span)
            right = self.coerce(right, result_type, node.right.span)
            if node.op == "MOD":
                return self.emit_result(_arith_op(result_type, "rem"),
                                        result_type, left, right, span=node.span)
            return self.emit_result(_arith_op(result_type, "divfloor"),
                                    result_type, left, right, span=node.span)
        if node.op == "\\":
            result_type = self._arith_type(left, right)
            left = self.coerce(left, result_type, node.left.span)
            right = self.coerce(right, result_type, node.right.span)
            return self.emit_result(_arith_op(result_type, "idiv"), result_type,
                                    left, right, span=node.span)

        result_type = DOUBLE if node.op == "/" else self._arith_type(left, right)
        name = _BINARY_TO_ARITH.get(node.op)
        if name is None:
            raise CompileError(Diagnostic(
                "E0013", f"operador sin bajar: {node.op}", node.span))
        # Los operandos se coercifican al tipo de la operación. Sin esto,
        # `vx + 0.5` con `vx` SINGLE emitía `add.f32` con un operando DOUBLE: el
        # IR queda mal tipado y cada backend tiene que defenderse. La regla es
        # que una operación del IR nunca ve operandos de otro tipo.
        left = self.coerce(left, result_type, node.left.span)
        right = self.coerce(right, result_type, node.right.span)
        return self.emit_result(_arith_op(result_type, name), result_type,
                                left, right, span=node.span)

    def _byte_compare(self, indexed: ast.Node, literal: ast.Node):
        """Baja `s[i] = "x"` a una comparación de bytes.

        Devuelve None si el patrón no aplica, para que el llamante siga por la
        vía normal.
        """
        if not isinstance(literal, ast.StringLit):
            return None
        if not isinstance(indexed, ast.Index):
            return None
        if self.type_of(indexed.target).kind != "STRING":
            return None
        raw = literal.value.encode("utf-8")
        if len(raw) != 1:
            return None

        target = self.expr(indexed.target)
        index = self.expr(indexed.index)
        needle = self.string_const(literal.value, literal.span)
        return self.emit_result("str.eq_byte", BOOL, target, index, needle,
                                span=indexed.span.merge(literal.span))

    def _arith_type(self, left: Value, right: Value) -> Type:
        if left.type.kind == "SINGLE" or right.type.kind == "SINGLE":
            return SINGLE
        return left.type

    def _numeric_eq(self, left: Value, right: Value, span: Span) -> Value:
        """`=` y `<>` sobre números, con el sufijo que toca.

        El caso de los enteros y los booleanos es `eq` a secas. El de los
        flotantes necesita su propia operación, y no por gusto: el `eq` sin
        sufijo elige opcode mirando el **destino**, que es BOOLEAN y por tanto
        siempre i32, así que comparaba dos `f64` con `i64.eq`. Eso compara bits,
        y comparar bits acierta casi siempre --dos números iguales tienen los
        mismos bits-- y falla justo en los dos casos que IEEE define: `0.0` y
        `-0.0` no son iguales en bits pero **sí** en IEEE, y `NaN = NaN` da
        verdadero en bits y falso en IEEE.

        Por eso aquí no hay atajo: la operación la decide el tipo común de los
        operandos, como ya hacía la rama de `<`, `<=`, `>` y `>=`.
        """
        tipo = self._arith_type(left, right)
        left = self.coerce(left, tipo, span)
        right = self.coerce(right, tipo, span)
        if tipo.kind in ("SINGLE", "DOUBLE"):
            return self.emit_result(f"eq.{_suffix(tipo)}", BOOL, left, right,
                                    span=span)
        return self.emit_result("eq", BOOL, left, right, span=span)

    def expr_index(self, node: ast.Index) -> Value:
        target = self.expr(node.target)
        index = self.expr(node.index)
        if target.type.kind == "STRING":
            # ADR 0003: indexar un texto devuelve un texto de un carácter.
            # Eso reserva memoria, y por eso existe el peephole de abajo.
            return self.emit_result("str.sub", STRING, target, index, span=node.span)
        element = target.type.element or INT
        return self.emit_result("load.index", element, target, index, span=node.span)

    def expr_call(self, node: ast.Call) -> Value:
        callee = node.callee.value if isinstance(node.callee, ast.Name) else ""
        args = [self.expr(arg) for arg in node.args]

        if callee == "len":
            # LEN es polimórfico (ADR 0003): bytes de un texto, elementos de un
            # array. Son dos instrucciones distintas en el IR.
            op = "array.len" if args[0].type.kind == "ARRAY" else "str.len"
            return self.emit_result(op, INT, args[0], span=node.span)
        if callee == "lenu":
            return self.emit_result("str.lenu", INT, args[0], span=node.span)
        if callee == "charcode":
            return self.emit_result("str.byte", INT, args[0], args[1], span=node.span)
        if callee == "chr":
            # ADR 0021. INLINE y no host: en WASM es reservar un byte y
            # escribirlo. Una tabla de 256 caracteres en los datos serian 256
            # bytes en cada programa que use CHR, y el pegamento entero tiene
            # un techo de 2880.
            return self.emit_result("str.from_code", STRING, args[0], span=node.span)
        if callee in ("sin", "cos", "sqrt"):
            argument = self.to_double(args[0], node.span)
            _, host = NATIVE_PLACEMENT[callee]
            if host is not None:
                self.module.uses_host(host)
            return self.emit_result(f"{callee}.f64", DOUBLE, argument,
                                    span=node.span)
        if callee == "floor":
            argument = self.to_double(args[0], node.span)
            return self.emit_result("floor.f64", DOUBLE, argument, span=node.span)
        if callee == "concat":
            return self.emit_result("str.concat", STRING, args[0], args[1],
                                    span=node.span)
        if callee == "compare":
            return self.emit_result("str.compare", INT, args[0], args[1],
                                    span=node.span)
        if callee == "str":
            self.note_formatting("to.str", args[0].type.kind)
            return self.emit_result("to.str", STRING, args[0], span=node.span)
        if callee == "val":
            self.module.uses_host(HOST_FORMATTING["to.num"])
            return self.emit_result("to.num", DOUBLE, args[0], span=node.span)
        if callee == "left":
            return self.emit_result("str.left", STRING, args[0], args[1],
                                    span=node.span)
        if callee == "right":
            return self.emit_result("str.right", STRING, args[0], args[1],
                                    span=node.span)
        if callee == "mid":
            return self.emit_result("str.mid", STRING, args[0], args[1],
                                    args[2], span=node.span)
        if callee == "contains":
            return self.emit_result("str.contains", BOOL, args[0], args[1],
                                    span=node.span)
        if callee == "indexof":
            return self.emit_result("str.indexof", INT, args[0], args[1],
                                    span=node.span)
        if callee == "rgb":
            return self.native_call("rgb", args, self.type_of(node), node.span)
        if callee in ("key", "keydown"):
            return self.native_call(callee, args, BOOL, node.span)
        if callee == "flip":
            self.module.uses_host(HOST_ABI["flip"])
            self.emit("flip", span=node.span)
            return self.value(VOID, node.span)

        info = self.func_types.get(callee)
        if info is not None:
            coerced = [self.coerce(arg, param_type, node.span)
                       for arg, (_, param_type) in zip(args, info.params)]
            return self.native_call_user(callee, coerced,
                                         info.return_type, node.span)

        if callee in ("min", "max", "clamp"):
            # Los tres comparan entre sí, así que sus argumentos tienen que
            # llegar con el **mismo** tipo a la pila. El typechecker declara el
            # tipo del resultado, pero sin esto el segundo `1` de `MAX(3.5, 1)`
            # se quedaba en `i64` donde la comparación quería `f64`, y lo
            # detectó el verificador como E0065 al final del camino. Es una
            # corrección que tiene que estar **antes** de la instrucción: el
            # error sale a mil líneas de distancia de su causa.
            destino = self.type_of(node)
            args = [self.coerce(arg, destino, node.span) for arg in args]
            return self.native_call(callee, args, destino, node.span)

        return self.native_call(callee, args, self.type_of(node), node.span)

    def note_placement(self, name: str) -> None:
        """Registra el import del host que necesita una nativa."""
        placement = NATIVE_PLACEMENT.get(name)
        if placement is None:
            raise CompileError(Diagnostic(
                "E0057",
                f"la nativa '{name}' no tiene colocación declarada",
                Span(0, 0),
                "añádela a NATIVE_PLACEMENT en sir.py; sin eso el backend no "
                "sabría si emitirla inline o importarla",
            ))
        _, host = placement
        if host is not None:
            self.module.uses_host(host)

    def _math_host(self, name: str) -> None:
        """Registra el import de una matemática que no es instrucción de WASM.

        `sin`, `cos` y `pow` salen como operaciones del IR y no como
        `call.native`, así que no pasan por `note_placement`. Sin esto el
        backend emitted una llamada a un import que el módulo no declaraba, y el
        fallo aparecía como índice de función inexistente.
        """
        _, host = NATIVE_PLACEMENT[name]
        if host is not None:
            self.module.uses_host(host)

    def note_formatting(self, op: str, kind: str) -> None:
        """Registra el import de formateo que corresponde al tipo del valor.

        `STR` no tiene un solo import: uno por tipo de entrada. Registrar el
        equivocado deja al backend llamando a una función que el módulo no
        declara, y el fallo sale como un índice inexistente y no como "falta
        este import".
        """
        target = HOST_FORMATTING[op]
        import_name = target[kind] if isinstance(target, dict) else target
        self.module.uses_host(import_name)
        if op == "to.str":
            # El backend pregunta la longitud del texto recién escrito, y eso es
            # un segundo import. Declararlo aquí y no en el backend es lo que
            # mantiene la regla: el módulo declara todo lo que usa.
            self.module.uses_host(HOST_FORMATTING["str_len"])

    def native_call(self, name: str, args: list[Value], result: Type,
                    span: Span) -> Value:
        self.note_placement(name)
        """Llamada a una función del runtime.

        Si devuelve algo, el destino va explícito como primer operando. No se
        sobreescribe el primer argumento: reutilizar un argumento como destino
        ahorra una palabra en el IR y cuesta tres horas de depuración cuando
        el argumento es un literal.
        """
        if result.kind == "VOID":
            self.emit("call.native", name, list(args), type_=VOID, span=span)
            return self.value(VOID, span)
        destination = self.value(result, span)
        self.emit("call.native", name, [destination] + list(args), type_=result,
                  span=span)
        return destination

    def native_call_user(self, name: str, args: list[Value], result: Type,
                         span: Span) -> Value:
        """Llamada a una FUNCTION del fuente.

        El destino va explícito como primer operando, igual que en las nativas:
        `call <índice> [destino, argumentos...]`.
        """
        index = self._func_index(name)
        if result.kind == "VOID":
            self.emit("call", index, list(args), type_=VOID, span=span)
            return self.value(VOID, span)
        destination = self.value(result, span)
        self.emit("call", index, [destination] + list(args), type_=result,
                  span=span)
        return destination

    def _func_index(self, name: str) -> int:
        for func in self.module.funcs:
            if func.name == name:
                return func.index
        raise CompileError(Diagnostic(
            "E0012", f"llamada a FUNCTION no construida: {name}",
            Span(0, 0)))

    def expr_array_literal(self, node: ast.ArrayLit,
                           declared: Type | None = None) -> Value:
        """Un literal de array.

        `declared` es el tipo que escribió el `DIM`, y manda sobre el que se
        deduciría de los elementos. Sin él, `DIM m AS ARRAY OF SINGLE = [1.5]`
        creaba un ARRAY OF DOUBLE porque el primer elemento es DOUBLE: el
        array era el doble de grande y `m[0]` valía cero.
        """
        array_type = self.type_of(node)
        element = (declared.element if declared is not None
                   and declared.element is not None
                   else array_type.element) or INT
        if declared is not None and declared.kind == "ARRAY":
            array_type = declared
        # El tipo declarado baja **un nivel**: en un literal anidado, sin esto,
        # `DIM m AS ARRAY OF ARRAY OF SINGLE = [[1.5]]`anthusbittava el array
        # interior como DOUBLE --ocho bytes por elemento-- mientras que la
        # lectura `m[0][0]` se emitía como SINGLE. El módulo leía cuatro bytes
        # donde había ocho y devolvía 0.
        interior = (declared.element if declared is not None
                    and declared.kind == "ARRAY" else None)
        elements = [
            self.expr_array_literal(item, declared=interior)
            if interior is not None and interior.kind == "ARRAY"
            and isinstance(item, ast.ArrayLit)
            else self.expr(item)
            for item in node.elements
        ]
        pointer = self.emit_result("array.alloc", array_type,
                                   type_code(element), len(elements), span=node.span)
        for offset, item in enumerate(elements):
            # El elemento se coerciciona al tipo del array: `[1.5, 2.5]` en un
            # ARRAY OF SINGLE emitía `array.init` con un DOUBLE y lo escribía
            # con ocho bytes donde caben cuatro, pisando el siguiente elemento.
            item = self.coerce(item, element, node.span)
            self.emit("array.init", pointer, offset, item, type_=element,
                      span=node.span)
        return pointer

    def expr_new_array(self, node: ast.NewArray) -> Value:
        array_type = self.type_of(node)
        element = array_type.element or INT
        count = self.expr(node.count)
        return self.emit_result("array.alloc", array_type, type_code(element),
                                count, span=node.span)

    # ----------------------------------------------------------------- tipos

    def coerce(self, value: Value, target: Type, span: Span) -> Value:
        """Inserta la conversión que el typechecker ya autorizó.

        Reparto de responsabilidades: el typechecker decide qué conversiones son
        legales (y distingue un literal de una variable, para no permitir
        estrechamientos en silencio); aquí sólo se emite la instrucción. Si
        aparece un par que el typechecker no debería haber dejado pasar, es un
        bug del compilador y se lanza en vez de adivinar.
        """
        source = value.type
        if source.kind == target.kind:
            return value

        numeric = ("INTEGER", "SINGLE", "DOUBLE", "COLOR")
        if target.kind in numeric and source.kind in numeric:
            if source.kind in ("SINGLE", "DOUBLE") and target.kind == "INTEGER":
                return self.emit_result("conv.i64", INT, value, span=span)
            if source.kind in ("INTEGER", "DOUBLE") and target.kind == "SINGLE":
                return self.emit_result("conv.f32", SINGLE, value, span=span)
            return self.emit_result("conv.f64", DOUBLE, value, span=span)

        # Un BOOLEAN siempre es i32. Si el origen no lo era (un entero, que es
        # lo que pasa con `DIM b AS BOOLEAN = 1`), se convierte explícitamente:
        # cero es falso, cualquier otra cosa verdadera.
        if target.kind == "BOOLEAN" and source.kind in ("INTEGER", "PTR", "COLOR"):
            return self.emit_result("to.bool", BOOL, value, span=span)

        if target.kind in ("INTEGER", "COLOR") and \
                source.kind in ("BOOLEAN", "INTEGER", "PTR"):
            return value

        if target.kind == "PTR":
            return value

        raise CompileError(Diagnostic(
            "E0017", f"conversión no implementada: {source} -> {target}", span))

    def to_double(self, value: Value, span: Span | None = None) -> Value:
        if value.type.kind == "DOUBLE":
            return value
        return self.emit_result("conv.f64", DOUBLE, value, span=span or Span(0, 0))

    def bool_expr(self, node: ast.Node) -> Value:
        """Baja una condición.

        El tipo se comprueba contra la tabla del typechecker, no contra el
        valor emitido: un BOOLEAN viaja en el IR como un entero de 0 o 1
        (regla 3, sin boxing), así que el valor resultante siempre es INTEGER.
        """
        if self.type_of(node).kind != BOOL.kind:
            raise CompileError(Diagnostic(
                "E0024", "condición no booleana tras el typechecker",
                node.span))
        return self.expr(node)

_BINARY_TO_ARITH = {"+": "add", "-": "sub", "*": "mul", "/": "div"}
_ARITH_SUFFIX = {"add", "sub", "mul", "div", "rem", "idiv", "divfloor", "neg"}

_COMPARE_SUFFIX = {"lt": "<", "le": "<=", "gt": ">", "ge": ">="}


def _arith_op(type_: Type, op: str) -> str:
    if op not in _ARITH_SUFFIX:
        raise CompileError(Diagnostic(
            "E0013", f"operación aritmética desconocida: {op}", Span(0, 0)))
    return f"{op}.{_suffix(type_)}"


def _suffix(type_: Type) -> str:
    if type_.kind == "SINGLE":
        return "f32"
    if type_.kind == "DOUBLE":
        return "f64"
    return "i64"


def _compare_op(type_: Type, op: str) -> str:
    return f"{op}.{_suffix(type_)}"


def build(program: ast.Program, typing: Typing, module_name: str = "main",
          lang_version: str = "1.0") -> Module:
    return Builder(typing, module_name, lang_version).build(program)