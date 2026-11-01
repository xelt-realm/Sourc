"""Encoder binario de WebAssembly.

Por qué a mano en vez de `wat2wasm`:

1. El auto-hospedaje (ADR 0016) exige que el compilador emita binarios sin
   herramientas externas. Un compilador que invoca un binario de C++ no se puede
   compilar a sí mismo en el navegador.
2. Sin la herramienta no se puede construir, y este proyecto no puede tener
   dependencias externas (ADR P7).
3. El control sobre los bytes es el punto: el presupuesto de peso se cumple o
   no se cumple, y saber exactamente dónde va cada byte es lo que permite
   cumplirlo.

Referencia: Core Specification 1.0 de WebAssembly.

**Los opcodes de esta tabla se verifican, no se confían.** Escribir 90 opcodes
de memoria es una fuente de errores garantizada, así que
`tests/test_wasm_encoder.py` genera un módulo mínimo por cada opcode y pide a
Node que lo valide. Un opcode mal escrito no llega ni a codegen.

Estructura de un módulo:

    magic     00 61 73 6D
    version   01 00 00 00
    secciones, cada una:  id (1 byte) · tamaño (u32 LEB) · cuerpo

Las secciones van en orden creciente de id. Las opcionales se omiten cuando
están vacías: una sección ausente equivale a una de tamaño cero, y escribirla
cuesta bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

MAGIC = b"\x00asm"
VERSION = b"\x01\x00\x00\x00"

PAGE_SIZE = 65536

# --------------------------------------------------------------- secciones

SEC_CUSTOM = 0
SEC_TYPE = 1
SEC_IMPORT = 2
SEC_FUNCTION = 3
SEC_TABLE = 4
SEC_MEMORY = 5
SEC_GLOBAL = 6
SEC_EXPORT = 7
SEC_START = 8
SEC_ELEMENT = 9
SEC_CODE = 10
SEC_DATA = 11
SEC_DATACOUNT = 12

SECTION_NAMES = {
    SEC_TYPE: "type", SEC_IMPORT: "import", SEC_FUNCTION: "function",
    SEC_TABLE: "table", SEC_MEMORY: "memory", SEC_GLOBAL: "global",
    SEC_EXPORT: "export", SEC_START: "start", SEC_ELEMENT: "element",
    SEC_CODE: "code", SEC_DATA: "data", SEC_DATACOUNT: "datacount",
}

# --------------------------------------------------------------- tipos de valor

I32 = 0x7F
I64 = 0x7E
F32 = 0x7D
F64 = 0x7C
V128 = 0x7B
FUNCREF = 0x70
EXTERNREF = 0x6F

VALTYPE_NAMES = {I32: "i32", I64: "i64", F32: "f32", F64: "f64",
                 V128: "v128", FUNCREF: "funcref", EXTERNREF: "externref"}

# Clases de importación y exportación.
KIND_FUNC = 0
KIND_TABLE = 1
KIND_MEMORY = 2
KIND_GLOBAL = 3


class WasmEncodeError(Exception):
    """El módulo no se puede construir. Es un bug del compilador, no del programa."""


# --------------------------------------------------------------- LEB128


def uleb(value: int) -> bytes:
    """Entero sin signo en base 128, byte de continuation en el bit 7.

    El error clásico aquí es emitir un byte de más: el último byte nunca lleva
    el bit alto a 1.
    """
    if value < 0:
        raise WasmEncodeError(f"uleb no acepta negativos: {value}")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def sleb(value: int) -> bytes:
    """Entero con signo en base 128.

    Lo que hace la aritmética en Python es lo correcto: `>>` es una
    desplazamiento aritmético, así que los negativos se propagan solos. El
    bucle para cuando los bits restantes son todos del signo del último byte.
    """
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        sign_bit_set = (byte & 0x40) != 0
        if (value == 0 and not sign_bit_set) or (value == -1 and sign_bit_set):
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)


def encode_f32(value: float) -> bytes:
    """IEEE 754 binary32, little endian.

    Los bits literales NaN y los infinitos se trapearían al pasar por float de
    Python; aquí se escriben como lo que son: un patrón de bits.
    """
    return struct.pack("<f", value)


def encode_f64(value: float) -> bytes:
    return struct.pack("<d", value)


def decode_f32(raw: bytes) -> float:
    return struct.unpack("<f", raw)[0]


def decode_f64(raw: bytes) -> float:
    return struct.unpack("<d", raw)[0]


def encode_name(text: str) -> bytes:
    """Un nombre: longitud en LEB seguida de los bytes UTF-8."""
    raw = text.encode("utf-8")
    return uleb(len(raw)) + raw


def encode_vector(items: list[bytes]) -> bytes:
    """Un vector: longitud en LEB seguida de los elementos concatenados."""
    return uleb(len(items)) + b"".join(items)


def encode_limits(minimum: int, maximum: int | None = None) -> bytes:
    if maximum is None:
        return b"\x00" + uleb(minimum)
    return b"\x01" + uleb(minimum) + uleb(maximum)


# --------------------------------------------------------------- firmas


@dataclass(frozen=True)
class FuncType:
    params: tuple[int, ...] = ()
    results: tuple[int, ...] = ()

    def render(self) -> bytes:
        return (b"\x60"
                + encode_vector([bytes((p,)) for p in self.params])
                + encode_vector([bytes((r,)) for r in self.results]))

    def __str__(self) -> str:
        params = ", ".join(VALTYPE_NAMES.get(p, str(p)) for p in self.params)
        results = ", ".join(VALTYPE_NAMES.get(r, str(r)) for r in self.results)
        return f"({params}) -> ({results})"


# --------------------------------------------------------------- opcodes
#
# Los nombres siguen la especificación. Los valores NO se memorizan: los
# verifica el test. Un comentario con el nombre completo es suficiente para
# que una lectura rápida no cometa errores, y el test cubre el resto.

# --- control
UNREACHABLE = 0x00
NOP = 0x01
BLOCK = 0x02
LOOP = 0x03
IF = 0x04
ELSE = 0x05
END = 0x0B
BR = 0x0C
BR_IF = 0x0D
RETURN = 0x0F
CALL = 0x10
DROP = 0x1A
SELECT = 0x1B
GLOBAL_GET = 0x23
GLOBAL_SET = 0x24
MEMORY_SIZE = 0x3F
MEMORY_GROW = 0x40

# --- cargas yalmacenes (0x28..0x3E). El primer operando es la alineación
#     como log2, el segundo el desplazamiento estático.
I32_LOAD = 0x28
I64_LOAD = 0x29
F32_LOAD = 0x2A
F64_LOAD = 0x2B
I32_LOAD8_S = 0x2C
I32_LOAD8_U = 0x2D
I32_LOAD16_S = 0x2E
I32_LOAD16_U = 0x2F
I64_LOAD8_S = 0x30
I64_LOAD8_U = 0x31
I64_LOAD16_S = 0x32
I64_LOAD16_U = 0x33
I64_LOAD32_S = 0x34
I64_LOAD32_U = 0x35
I32_STORE = 0x36
I64_STORE = 0x37
F32_STORE = 0x38
F64_STORE = 0x39
I32_STORE8 = 0x3A
I32_STORE16 = 0x3B
I64_STORE8 = 0x3C
I64_STORE16 = 0x3D
I64_STORE32 = 0x3E

# --- constantes
I32_CONST = 0x41
I64_CONST = 0x42
F32_CONST = 0x43
F64_CONST = 0x44

# --- comparación
I32_EQZ = 0x45
I32_EQ = 0x46
I32_NE = 0x47
I32_LT_S = 0x48
I32_LT_U = 0x49
I32_GT_S = 0x4A
I32_GT_U = 0x4B
I32_LE_S = 0x4C
I32_LE_U = 0x4D
I32_GE_S = 0x4E
I32_GE_U = 0x4F
I64_EQZ = 0x50
I64_EQ = 0x51
I64_NE = 0x52
I64_LT_S = 0x53
I64_LT_U = 0x54
I64_GT_S = 0x55
I64_GT_U = 0x56
I64_LE_S = 0x57
I64_LE_U = 0x58
I64_GE_S = 0x59
I64_GE_U = 0x5A
F32_EQ = 0x5B
F32_NE = 0x5C
F32_LT = 0x5D
F32_GT = 0x5E
F32_LE = 0x5F
F32_GE = 0x60
F64_EQ = 0x61
F64_NE = 0x62
F64_LT = 0x63
F64_GT = 0x64
F64_LE = 0x65
F64_GE = 0x66

# --- aritmética i32
I32_CLZ = 0x67
I32_CTZ = 0x68
I32_POPCNT = 0x69
I32_ADD = 0x6A
I32_SUB = 0x6B
I32_MUL = 0x6C
I32_DIV_S = 0x6D
I32_DIV_U = 0x6E
I32_REM_S = 0x6F
I32_REM_U = 0x70
I32_AND = 0x71
I32_OR = 0x72
I32_XOR = 0x73
I32_SHL = 0x74
I32_SHR_S = 0x75
I32_SHR_U = 0x76
I32_ROTL = 0x77
I32_ROTR = 0x78

# --- aritmética i64
I64_CLZ = 0x79
I64_CTZ = 0x7A
I64_POPCNT = 0x7B
I64_ADD = 0x7C
I64_SUB = 0x7D
I64_MUL = 0x7E
I64_DIV_S = 0x7F
I64_DIV_U = 0x80
I64_REM_S = 0x81
I64_REM_U = 0x82
I64_AND = 0x83
I64_OR = 0x84
I64_XOR = 0x85
I64_SHL = 0x86
I64_SHR_S = 0x87
I64_SHR_U = 0x88
I64_ROTL = 0x89
I64_ROTR = 0x8A

# --- f32
F32_ABS = 0x8B
F32_NEG = 0x8C
F32_CEIL = 0x8D
F32_FLOOR = 0x8E
F32_TRUNC = 0x8F
F32_NEAREST = 0x90
F32_SQRT = 0x91
F32_ADD = 0x92
F32_SUB = 0x93
F32_MUL = 0x94
F32_DIV = 0x95
F32_MIN = 0x96
F32_MAX = 0x97
F32_COPYSIGN = 0x98

# --- f64
F64_ABS = 0x99
F64_NEG = 0x9A
F64_CEIL = 0x9B
F64_FLOOR = 0x9C
F64_TRUNC = 0x9D
F64_NEAREST = 0x9E
F64_SQRT = 0x9F
F64_ADD = 0xA0
F64_SUB = 0xA1
F64_MUL = 0xA2
F64_DIV = 0xA3
F64_MIN = 0xA4
F64_MAX = 0xA5
F64_COPYSIGN = 0xA6

# --- conversiones
I32_WRAP_I64 = 0xA7
I32_TRUNC_F32_S = 0xA8
I32_TRUNC_F32_U = 0xA9
I32_TRUNC_F64_S = 0xAA
I32_TRUNC_F64_U = 0xAB
I64_EXTEND_I32_S = 0xAC
I64_EXTEND_I32_U = 0xAD
I64_TRUNC_F32_S = 0xAE
I64_TRUNC_F32_U = 0xAF
I64_TRUNC_F64_S = 0xB0
I64_TRUNC_F64_U = 0xB1
F32_CONVERT_I32_S = 0xB2
F32_CONVERT_I32_U = 0xB3
F32_CONVERT_I64_S = 0xB4
F32_CONVERT_I64_U = 0xB5
F32_DEMOTE_F64 = 0xB6
F64_CONVERT_I32_S = 0xB7
F64_CONVERT_I32_U = 0xB8
F64_CONVERT_I64_S = 0xB9
F64_CONVERT_I64_U = 0xBA
F64_PROMOTE_F32 = 0xBB
I32_REINTERPRET_F32 = 0xBC
I64_REINTERPRET_F64 = 0xBD
F32_REINTERPRET_I32 = 0xBE
F64_REINTERPRET_I64 = 0xBF

# --- extensión de signo
I32_EXTEND8_S = 0xC0
I32_EXTEND16_S = 0xC1
I64_EXTEND8_S = 0xC2
I64_EXTEND16_S = 0xC3
I64_EXTEND32_S = 0xC4

# --- prefijadas con 0xFC (truncamientos saturados)
FC_PREFIX = 0xFC
I32_TRUNC_SAT_F32_S = 0
I32_TRUNC_SAT_F32_U = 1
I32_TRUNC_SAT_F64_S = 2
I32_TRUNC_SAT_F64_U = 3
I64_TRUNC_SAT_F32_S = 4
I64_TRUNC_SAT_F32_U = 5
I64_TRUNC_SAT_F64_S = 6
I64_TRUNC_SAT_F64_U = 7


# --------------------------------------------------------------- emisores


def op(code: int) -> bytes:
    return bytes((code,))


def br(depth: int) -> bytes:
    return bytes((BR,)) + uleb(depth)


def br_if(depth: int) -> bytes:
    return bytes((BR_IF,)) + uleb(depth)


def call(index: int) -> bytes:
    return bytes((CALL,)) + uleb(index)


def block(block_type: int = 0x40) -> bytes:
    """Abre un bloque. 0x40 es "sin resultado", que es el caso habitual."""
    return bytes((BLOCK, block_type))


def loop(block_type: int = 0x40) -> bytes:
    return bytes((LOOP, block_type))


def if_(block_type: int = 0x40) -> bytes:
    return bytes((IF, block_type))


def else_() -> bytes:
    return bytes((ELSE,))


def end() -> bytes:
    return bytes((END,))


def i32_const(value: int) -> bytes:
    return bytes((I32_CONST,)) + sleb(value)


def i64_const(value: int) -> bytes:
    return bytes((I64_CONST,)) + sleb(value)


def f32_const(value: float) -> bytes:
    return bytes((F32_CONST,)) + encode_f32(value)


def f64_const(value: float) -> bytes:
    return bytes((F64_CONST,)) + encode_f64(value)


def local_get(index: int) -> bytes:
    return b"\x20" + uleb(index)


def local_set(index: int) -> bytes:
    return b"\x21" + uleb(index)


def local_tee(index: int) -> bytes:
    return b"\x22" + uleb(index)


def drop() -> bytes:
    return bytes((DROP,))


def op_unary(code: int) -> bytes:
    """Instrucción de un solo operando. El generador y el backend la usan igual."""
    return bytes((code,))


def select() -> bytes:
    """Elige entre dos valores según un i32. Popula a, b, cond → devuelve a si
    `cond` no es cero, si no b."""
    return bytes((SELECT,))


def unreachable() -> bytes:
    return bytes((UNREACHABLE,))


def memory_size() -> bytes:
    """Páginas de memoria.

    El byte que sigue al opcode es el índice de memoria, reservado a 0 en el
    MVP. Sin él, el decodificador del módulo se come la instrucción siguiente
    como índice y dice `memory index 73`, que no dice nada del error real.
    """
    return bytes((MEMORY_SIZE, 0))


def memory_grow(delta: int) -> bytes:
    """Pide `delta` páginas más. Devuelve las que había, o -1 si no pudo.

    El valor anterior no se encadena a mano porque el código generado por el
    backend es lineal: no hay una pila de expresiones donde el valor anterior
    quede disponible.
    """
    return bytes((MEMORY_GROW, 0)) + sleb(delta)


def global_get(index: int) -> bytes:
    return bytes((GLOBAL_GET,)) + uleb(index)


def global_set(index: int) -> bytes:
    return bytes((GLOBAL_SET,)) + uleb(index)


def memarg(code: int, align: int, offset: int) -> bytes:
    return bytes((code,)) + uleb(align) + uleb(offset)


def prefixed(subcode: int) -> bytes:
    return bytes((FC_PREFIX,)) + uleb(subcode)


# Subcódigos de *bulk memory*, que es posterior al MVP. Los tres navegadores y
# Node los tienen; el verificador los conoce (`TABLA_FC`) y el round trip los
# reensambla, que es lo que evita que una instrucción exista sólo en una mitad.
FC_MEMORY_COPY = 10
FC_MEMORY_FILL = 11


def memory_copy() -> bytes:
    """`memory.copy`: destino, origen y bytes.

    Los dos ceros del final son los índices de memoria, que en el MVP siempre
    son 0 porque sólo hay una.
    """
    return prefixed(FC_MEMORY_COPY) + uleb(0) + uleb(0)


def memory_fill() -> bytes:
    """`memory.fill`: destino, byte y bytes."""
    return prefixed(FC_MEMORY_FILL) + uleb(0)


# --------------------------------------------------------------- construcción


@dataclass
class FuncBody:
    """Cuerpo de una función: los bytes del código y su declaración.

    Los bloques estructurados los emite el backend con `block()`/`loop()` y
    `end()`. Contar `end` a mano es la fuente clásica de módulos inválidos, así
    que el backend usará un objeto `Block` que devuelva apertura y cierre
    (ver `blocks.py`); aquí sólo está el contenedor.
    """

    name: str
    signature: FuncType
    locals: list[tuple[int, int]] = field(default_factory=list)
    pieces: list[bytes] = field(default_factory=list)

    def emit(self, data: bytes) -> "FuncBody":
        self.pieces.append(data)
        return self

    def extend(self, data: bytes) -> "FuncBody":
        self.pieces.append(data)
        return self

    def render(self) -> bytes:
        """Vector de locales + código + `end`, con su tamaño por delante.

        El formato usa run-length encoding, así que se agrupan los locals
        contiguos del mismo tipo. Lo que **no** se hace es reordenarlos por
        tipo, aunque agrupara más.

        Ordenarlos por tipo era una "optimización" que el fuzzer del encoder
        cazó en un día: al reordenar, el local 0 deja de ser el primero
        declarado y pasa a ser el primero de la agrupación por valor de tipo
        (`f64` = 0x7C antes que `i32` = 0x7F). Todos los índices del código
        quedan permutados en silencio, y el módulo sigue validando: no hay
        ningún error, sólo valores equivocados en el sitio equivocado.

        Los índices que el backend emite tienen que ser estables y predecibles.
        Agrupar por tipo es trabajo de `wasm-opt`, que además informa del
        ahorro; aquí no se reordena nada.
        """
        groups: list[tuple[int, int]] = []
        for count, value_type in self.locals:
            # Un grupo de tamaño cero es inválido según la especificación, y el
            # backend lo genera sin querer: si pide (1, i32) y (0, i32) al
            # agrupar, el módulo entero deja de validar. Se descarta aquí.
            if count == 0:
                continue
            if groups and groups[-1][1] == value_type:
                previous = groups[-1][0]
                groups[-1] = (previous + count, value_type)
            else:
                groups.append((count, value_type))

        head = encode_vector([uleb(count) + bytes((value_type,))
                              for count, value_type in groups])
        code = b"".join(self.pieces) + end()
        return uleb(len(head) + len(code)) + head + code


@dataclass
class Import:
    module: str
    name: str
    kind: int = KIND_FUNC
    type_index: int = 0

    def render(self) -> bytes:
        return (encode_name(self.module) + encode_name(self.name)
                + bytes((self.kind,)) + uleb(self.type_index))


@dataclass
class Export:
    name: str
    kind: int
    index: int

    def render(self) -> bytes:
        return encode_name(self.name) + bytes((self.kind,)) + uleb(self.index)


@dataclass
class GlobalVar:
    value_type: int
    mutable: bool
    init: bytes

    def render(self) -> bytes:
        return (bytes((self.value_type, 1 if self.mutable else 0))
                + self.init + end())


class ModuleBuilder:
    """Declara lo que existe y lo serializa.

    No hay representación intermedia a medio construir: si el backend puede
    producir el módulo, puede serializarlo, y un error aparece al construirlo,
    no al ejecutarlo en el navegador.
    """

    def __init__(self, name: str = "module"):
        self.name = name
        self.types: list[FuncType] = []
        self.imports: list[Import] = []
        self.funcs: list[FuncBody] = []
        self.globals: list[GlobalVar] = []
        self.exports: list[Export] = []
        self.data: list[tuple[int, bytes]] = []
        self.memory_min: int | None = None
        self.memory_max: int | None = None
        self.start: int | None = None
        self.table_count: int | None = None
        self.exported_memory: str | None = None

    # --------------------------------------------------------------- tipos

    def add_type(self, signature: FuncType) -> int:
        """Declara una firma y devuelve su índice.

        Una firma repetida devuelve el índice existente. Deduplicar ahorra
        bytes y no cambia nada observable, porque el índice es lo único que se
        referencia.
        """
        if signature in self.types:
            return self.types.index(signature)
        self.types.append(signature)
        return len(self.types) - 1

    # -------------------------------------------------------------- imports

    def import_function(self, module: str, name: str, params: tuple[int, ...],
                        results: tuple[int, ...]) -> int:
        """Importa una función. Devuelve su índice en el espacio de funciones.

        Los índices de función del módulo empiezan **después** de los imports,
        así que quien llame necesita esta cuenta: las funciones propias van de
        `import_count` en adelante. Confundir los dos espacios es el error más
        habitual al escribir un backend.
        """
        type_index = self.add_type(FuncType(params, results))
        self.imports.append(Import(module, name, KIND_FUNC, type_index))
        return len(self.imports) - 1

    @property
    def import_count(self) -> int:
        return len(self.imports)

    # ------------------------------------------------------------ funciones

    def add_function(self, name: str, params: tuple[int, ...] = (),
                     results: tuple[int, ...] = (),
                     locals: list[tuple[int, int]] | None = None) -> FuncBody:
        signature = FuncType(params, results)
        # La firma se registra aquí, no en `to_bytes`: la sección de tipos tiene
        # que estar completa antes de escribir la de funciones, y buscar el
        # índice sobre la marcha durante la serialización escondería el error.
        self.add_type(signature)
        body = FuncBody(name=name, signature=signature, locals=locals or [])
        self.funcs.append(body)
        return body

    def func_index(self, body: FuncBody) -> int:
        return self.import_count + self.funcs.index(body)

    def func_index_by_name(self, name: str) -> int:
        for position, func in enumerate(self.funcs):
            if func.name == name:
                return self.import_count + position
        raise WasmEncodeError(f"no hay ninguna función llamada '{name}'")

    # -------------------------------------------------------------- memoria

    def add_memory(self, minimum_pages: int, maximum_pages: int | None = None):
        self.memory_min = minimum_pages
        self.memory_max = maximum_pages

    def add_data(self, offset: int, raw: bytes) -> None:
        self.data.append((offset, raw))

    def add_table(self, count: int) -> int:
        self.table_count = count
        return 0

    # ------------------------------------------------------------- globales

    def add_global(self, value_type: int, mutable: bool, init: bytes) -> int:
        self.globals.append(GlobalVar(value_type, mutable, init))
        return len(self.globals) - 1

    # -------------------------------------------------------------- exports

    def export(self, name: str, kind: int, index: int) -> None:
        self.exports.append(Export(name, kind, index))

    def export_memory(self, name: str = "memory") -> None:
        """Exporta la memoria con este nombre.

        El nombre importa: el glue lo busca por él, y cambiarlo rompe los dos
        lados a la vez sin que ninguno avise.
        """
        if self.memory_min is None:
            raise WasmEncodeError("no hay memoria que exportar")
        self.exported_memory = name

    # ------------------------------------------------------------ serializar

    def _section(self, section_id: int, payload: bytes) -> bytes:
        if not payload:
            return b""
        return bytes((section_id,)) + uleb(len(payload)) + payload

    def to_bytes(self) -> bytes:
        out = bytearray(MAGIC + VERSION)

        if self.types:
            out += self._section(SEC_TYPE, encode_vector(
                [t.render() for t in self.types]))

        if self.imports:
            out += self._section(SEC_IMPORT, encode_vector(
                [i.render() for i in self.imports]))

        if self.funcs:
            out += self._section(SEC_FUNCTION, encode_vector(
                [uleb(self.types.index(func.signature)) for func in self.funcs]))

        if self.table_count is not None:
            # Una tabla de funciones: `funcref`, con límite `min` igual al
            # número de funciones. En la Fase 2 no la usa nadie; está para que
            # el backend de funciones de primera clase tenga dónde mirar.
            out += self._section(SEC_TABLE, encode_vector([
                bytes((FUNCREF, 0x00)) + uleb(self.table_count)]))

        if self.memory_min is not None:
            out += self._section(SEC_MEMORY, encode_vector([
                encode_limits(self.memory_min, self.memory_max)]))

        if self.globals:
            out += self._section(SEC_GLOBAL, encode_vector(
                [g.render() for g in self.globals]))

        all_exports = list(self.exports)
        if self.exported_memory is not None:
            all_exports.append(Export(self.exported_memory, KIND_MEMORY, 0))
        if all_exports:
            out += self._section(SEC_EXPORT, encode_vector(
                [e.render() for e in all_exports]))

        if self.start is not None:
            out += self._section(SEC_START, uleb(self.start))

        if self.funcs:
            out += self._section(SEC_CODE, encode_vector(
                [func.render() for func in self.funcs]))

        if self.data:
            out += self._section(SEC_DATA, encode_vector([
                render_data(offset, raw) for offset, raw in self.data]))

        return bytes(out)


def render_data(offset: int, raw: bytes) -> bytes:
    """Segmento de datos pasivo con desplazamiento explícito.

    El byte 0x00 indica "modo activo, memoria 0"; el desplazamiento va como
    constante `i32` y termina con `end`, igual que un bloque.
    """
    return (b"\x00" + i32_const(offset) + end()
            + encode_vector([bytes((b,)) for b in raw]))


def validate(bytes_: bytes) -> None:
    """Comprobaciones propias antes de enseñarle el módulo a nadie.

    No sustituye a la validación de la spec: eso lo hace Node en los tests. Lo
    que hace es atrapar los errores que un testValidity tardaría en señalar,
    con un mensaje que dice qué se quiso escribir.
    """
    if len(bytes_) < 8:
        raise WasmEncodeError("módulo demasiado corto para ser válido")
    if bytes_[:4] != MAGIC:
        raise WasmEncodeError("el módulo no empieza con el magic 0x0061736d")
    if bytes_[4:8] != VERSION:
        raise WasmEncodeError("la versión del módulo no es la 1")

    position = 8
    last_id = -1
    while position < len(bytes_):
        section_id = bytes_[position]
        position += 1
        size, consumed = read_uleb(bytes_, position)
        position += consumed
        if section_id > SEC_DATACOUNT:
            raise WasmEncodeError(
                f"identificador de sección desconocido: {section_id}")
        if section_id <= last_id:
            raise WasmEncodeError(
                f"secciones fuera de orden: {SECTION_NAMES.get(section_id, section_id)}"
                f" después de {SECTION_NAMES.get(last_id, last_id)}")
        last_id = section_id
        position += size
        if position > len(bytes_):
            raise WasmEncodeError(
                f"la sección {SECTION_NAMES.get(section_id, section_id)} "
                f"declara {size} bytes y el módulo se acaba antes")


def read_uleb(data: bytes, position: int) -> tuple[int, int]:
    """Lee un LEB128 sin signo. Devuelve (valor, bytes consumidos)."""
    result = 0
    shift = 0
    consumed = 0
    while True:
        if position + consumed >= len(data):
            raise WasmEncodeError("LEB128 truncado")
        byte = data[position + consumed]
        result |= (byte & 0x7F) << shift
        consumed += 1
        if (byte & 0x80) == 0:
            return result, consumed
        shift += 7
        if shift > 63:
            raise WasmEncodeError("LEB128 demasiado largo")