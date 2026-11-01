"""Módulo WASM → texto WAT.

Existe por una razón concreta: **depurar**. Un fuzzer encuentra que un módulo
no valida, y el error de Node no dice dónde. Con el texto, el diff dice en qué
línea, y comparar dos_Text versions de un programa de 4000 instrucciones es
manejable; comparar dos binarios no.

Es también la mitad de un oráculo. El emisor y el ensamblador son dos
implementaciones independientes del mismo formato, y que coincidan byte a byte
es el chequeo estructural más fuerte que hay sin herramientas externas -- la
misma razón por la que el intérprete es el oráculo de la *semántica* (ADR 0019).

**Lo que este módulo no hace: adivinar.** Una instrucción que no conoce no se
imprime como algo parecido: se lanza un error con el offset. Un emisor que
inventa el texto de un opcode desconocido produce un WAT que *parece* bueno y
reensambla a algo distinto, y el round trip pasa por encima del bug que
existía para cazarlo.

**Los flotantes se imprimen en hexadecimal**, no en decimal. `f32.const 0.1`
en texto son los bits de `0.1` en f64, redondeados a f32, y hay valores --
denormales-- donde el camino de ida y vuelta no vuelve a los mismos bits. La
forma hexadecimal es exacta por especificación, y la diferencia se ve como un
mismo módulo con un byte distinto.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from . import encoder as w

MAGIC = w.MAGIC


class WatError(Exception):
    """No se puede emitir WAT de este módulo, y se sabe por qué."""


# --------------------------------------------------------------- lectura LEB


def _uleb(datos: bytes, pos: int) -> tuple[int, int]:
    valor = 0
    shift = 0
    while True:
        if pos >= len(datos):
            raise WatError(f"ULEB sin fin en {pos}")
        byte = datos[pos]
        pos += 1
        valor |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return valor, pos
        shift += 7
        if shift > 35:
            raise WatError(f"ULEB de más de 5 bytes en {pos}")


def _sleb(datos: bytes, pos: int) -> tuple[int, int]:
    valor = 0
    shift = 0
    while True:
        if pos >= len(datos):
            raise WatError(f"SLEB sin fin en {pos}")
        byte = datos[pos]
        pos += 1
        valor |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            if byte & 0x40:
                valor -= 1 << shift
            return valor, pos


def _vector(datos: bytes, pos: int) -> tuple[int, int]:
    """Lee una cuenta de vector y la devuelve; el contenido se lee aparte."""
    return _uleb(datos, pos)


# ---------------------------------------------------------- flotantes exactos


def _hex_f32(cur: _Cursor) -> str:
    return _hex_float(cur, 8, 23, -126)


def _hex_f64(cur: _Cursor) -> str:
    return _hex_float(cur, 11, 52, -1022)


def _hex_fraccion(fraccion: int, frac_bits: int) -> str:
    """La carga útil en hexadecimal, con los dígitos justos.

    23 bits de mantisa no son un número entero de dígitos hexadecimales: son 5
    dígitos y sobran 3 bits. Imprimirlo como 5 dígitos se come el bit bajo y
    `1.5` en f32 --que es `0x1.4p+0` con 24 bits-- salía `0x1.4p+0` leído como
    `1.25`. La fracción se desplaza a la izquierda lo justo para ocupar
    dígitos enteros, que es lo que dice la especificación.
    """
    relleno = (4 - frac_bits % 4) % 4
    return f"{fraccion << relleno:0{(frac_bits + relleno) // 4}x}".rstrip("0") or "0"


def _hex_float(cur: _Cursor, exp_bits: int, frac_bits: int, min_exp: int) -> str:
    """La forma hexadecimal de la especificación, que es exacta.

    `0x1.<fracción>p<exponente>` para los normales y `0x0.<fracción>p<exp>`
    para los subnormales, con la especificación de que un subnormal se escribe
    con exponente del menor normal. Los ceros finales de la fracción se quitan
    porque no cambian el valor.

    Decimal no vale: `f32.const 0.1` en texto son los bits de `0.1` en f64
    redondeados a f32, y para los denormales el camino de ida y vuelta no vuelve
    a los mismos bits. El round trip compara bytes, así que "equivalente" no
    sirve.

    El NaN se imprime con su carga útil. No afecta al valor --cualquier NaN es
    un NaN-- pero sí a los bytes, y un NaN canónico que el round trip no
    reproduzca es ruido en el único oráculo que hay.
    """
    ancho = 4 if exp_bits == 8 else 8
    formato = "<I" if ancho == 4 else "<Q"
    crudo = cur.bytes(ancho)
    entero = struct.unpack(formato, crudo)[0]
    signo = entero >> (exp_bits + frac_bits)
    exponente = (entero >> frac_bits) & ((1 << exp_bits) - 1)
    fraccion = entero & ((1 << frac_bits) - 1)
    prefijo = "-" if signo else ""

    if exponente == (1 << exp_bits) - 1:              # inf o NaN
        if fraccion == 0:
            return f"{prefijo}inf"
        carga = fraccion | (1 << (frac_bits - 1))      # el bit de silencio puesto
        return f"{prefijo}nan:0x{carga:x}"
    if exponente == 0 and fraccion == 0:
        return f"{prefijo}0x0p+0"
    if exponente == 0:                                # subnormal
        # `0x0.<fracción>`, **con el punto**. Sin él, `0x0000000000001p-1022` se
        # lee como un entero por 2^-1022 --el menor normal-- y el menor
        # subnormal se convierte en el normal de al lado. Es el mismo error con
        # dos letras de diferencia.
        return f"{prefijo}0x0.{_hex_fraccion(fraccion, frac_bits)}p{min_exp}"

    # El exponente de un normal es el sesgo del campo, y **sólo** eso. Sumarle
    # también `min_exp` --que es el exponente de los subnormales-- hacía que
    # `1.0` saliera como `0x1.0p-1022`, y un WAT así reensambla a otro módulo.
    sesgo = (1 << (exp_bits - 1)) - 1
    real = exponente - sesgo
    texto = _hex_fraccion(fraccion, frac_bits)
    return f"{prefijo}0x1.{texto}p{real:+d}"


# ------------------------------------------------------- tabla de opcodes
#
# Cada entrada: nombre en WAT y qué inmediatos sigue. El orden de los tipos de
# inmediato importa y es el que decide cuánto se lee:
#
#   u32      ULEB: índice de local, función, global, profundidad de salto
#   i32      SLEB
#   i64      SLEB
#   f32      cuatro bytes little-endian
#   f64      ocho bytes little-endian
#   memarg   ULEB de alineación + ULEB de desplazamiento
#   bloque   un byte: 0x40 vacío o un valtype
#   tabla    vector de ULEB + un ULEB por defecto

TABLA_INMEDIATOS: dict[int, tuple[str, tuple[str, ...]]] = {
    0x00: ("unreachable", ()),
    0x01: ("nop", ()),
    0x02: ("block", ("bloque",)),
    0x03: ("loop", ("bloque",)),
    0x04: ("if", ("bloque",)),
    0x05: ("else", ()),
    0x0B: ("end", ()),
    0x0C: ("br", ("u32",)),
    0x0D: ("br_if", ("u32",)),
    0x0F: ("return", ()),
    0x10: ("call", ("u32",)),
    0x1A: ("drop", ()),
    0x1B: ("select", ()),
    0x20: ("local.get", ("u32",)),
    0x21: ("local.set", ("u32",)),
    0x22: ("local.tee", ("u32",)),
    0x23: ("global.get", ("u32",)),
    0x24: ("global.set", ("u32",)),
    0x25: ("table.get", ("u32",)),
    0x26: ("table.set", ("u32",)),
    0x28: ("i32.load", ("memarg",)),
    0x29: ("i64.load", ("memarg",)),
    0x2A: ("f32.load", ("memarg",)),
    0x2B: ("f64.load", ("memarg",)),
    0x2C: ("i32.load8_s", ("memarg",)),
    0x2D: ("i32.load8_u", ("memarg",)),
    0x2E: ("i32.load16_s", ("memarg",)),
    0x2F: ("i32.load16_u", ("memarg",)),
    0x30: ("i64.load8_s", ("memarg",)),
    0x31: ("i64.load8_u", ("memarg",)),
    0x32: ("i64.load16_s", ("memarg",)),
    0x33: ("i64.load16_u", ("memarg",)),
    0x34: ("i64.load32_s", ("memarg",)),
    0x35: ("i64.load32_u", ("memarg",)),
    0x36: ("i32.store", ("memarg",)),
    0x37: ("i64.store", ("memarg",)),
    0x38: ("f32.store", ("memarg",)),
    0x39: ("f64.store", ("memarg",)),
    0x3A: ("i32.store8", ("memarg",)),
    0x3B: ("i32.store16", ("memarg",)),
    0x3C: ("i64.store8", ("memarg",)),
    0x3D: ("i64.store16", ("memarg",)),
    0x3E: ("i64.store32", ("memarg",)),
    0x3F: ("memory.size", ("u32",)),
    0x40: ("memory.grow", ("u32",)),
    0x41: ("i32.const", ("i32",)),
    0x42: ("i64.const", ("i64",)),
    0x43: ("f32.const", ("f32",)),
    0x44: ("f64.const", ("f64",)),
    0x45: ("i32.eqz", ()),
    0x46: ("i32.eq", ()),
    0x47: ("i32.ne", ()),
    0x48: ("i32.lt_s", ()),
    0x49: ("i32.lt_u", ()),
    0x4A: ("i32.gt_s", ()),
    0x4B: ("i32.gt_u", ()),
    0x4C: ("i32.le_s", ()),
    0x4D: ("i32.le_u", ()),
    0x4E: ("i32.ge_s", ()),
    0x4F: ("i32.ge_u", ()),
    0x50: ("i64.eqz", ()),
    0x51: ("i64.eq", ()),
    0x52: ("i64.ne", ()),
    0x53: ("i64.lt_s", ()),
    0x54: ("i64.lt_u", ()),
    0x55: ("i64.gt_s", ()),
    0x56: ("i64.gt_u", ()),
    0x57: ("i64.le_s", ()),
    0x58: ("i64.le_u", ()),
    0x59: ("i64.ge_s", ()),
    0x5A: ("i64.ge_u", ()),
    0x5B: ("f32.eq", ()),
    0x5C: ("f32.ne", ()),
    0x5D: ("f32.lt", ()),
    0x5E: ("f32.gt", ()),
    0x5F: ("f32.le", ()),
    0x60: ("f32.ge", ()),
    0x61: ("f64.eq", ()),
    0x62: ("f64.ne", ()),
    0x63: ("f64.lt", ()),
    0x64: ("f64.gt", ()),
    0x65: ("f64.le", ()),
    0x66: ("f64.ge", ()),
    0x67: ("i32.clz", ()),
    0x68: ("i32.ctz", ()),
    0x69: ("i32.popcnt", ()),
    0x6A: ("i32.add", ()),
    0x6B: ("i32.sub", ()),
    0x6C: ("i32.mul", ()),
    0x6D: ("i32.div_s", ()),
    0x6E: ("i32.div_u", ()),
    0x6F: ("i32.rem_s", ()),
    0x70: ("i32.rem_u", ()),
    0x71: ("i32.and", ()),
    0x72: ("i32.or", ()),
    0x73: ("i32.xor", ()),
    0x74: ("i32.shl", ()),
    0x75: ("i32.shr_s", ()),
    0x76: ("i32.shr_u", ()),
    0x77: ("i32.rotl", ()),
    0x78: ("i32.rotr", ()),
    0x79: ("i64.clz", ()),
    0x7A: ("i64.ctz", ()),
    0x7B: ("i64.popcnt", ()),
    0x7C: ("i64.add", ()),
    0x7D: ("i64.sub", ()),
    0x7E: ("i64.mul", ()),
    0x7F: ("i64.div_s", ()),
    0x80: ("i64.div_u", ()),
    0x81: ("i64.rem_s", ()),
    0x82: ("i64.rem_u", ()),
    0x83: ("i64.and", ()),
    0x84: ("i64.or", ()),
    0x85: ("i64.xor", ()),
    0x86: ("i64.shl", ()),
    0x87: ("i64.shr_s", ()),
    0x88: ("i64.shr_u", ()),
    0x89: ("i64.rotl", ()),
    0x8A: ("i64.rotr", ()),
    0x8B: ("f32.abs", ()),
    0x8C: ("f32.neg", ()),
    0x8D: ("f32.ceil", ()),
    0x8E: ("f32.floor", ()),
    0x8F: ("f32.trunc", ()),
    0x90: ("f32.nearest", ()),
    0x91: ("f32.sqrt", ()),
    0x92: ("f32.add", ()),
    0x93: ("f32.sub", ()),
    0x94: ("f32.mul", ()),
    0x95: ("f32.div", ()),
    0x96: ("f32.min", ()),
    0x97: ("f32.max", ()),
    0x98: ("f32.copysign", ()),
    0x99: ("f64.abs", ()),
    0x9A: ("f64.neg", ()),
    0x9B: ("f64.ceil", ()),
    0x9C: ("f64.floor", ()),
    0x9D: ("f64.trunc", ()),
    0x9E: ("f64.nearest", ()),
    0x9F: ("f64.sqrt", ()),
    0xA0: ("f64.add", ()),
    0xA1: ("f64.sub", ()),
    0xA2: ("f64.mul", ()),
    0xA3: ("f64.div", ()),
    0xA4: ("f64.min", ()),
    0xA5: ("f64.max", ()),
    0xA6: ("f64.copysign", ()),
    0xA7: ("i32.wrap_i64", ()),
    0xA8: ("i32.trunc_f32_s", ()),
    0xA9: ("i32.trunc_f32_u", ()),
    0xAA: ("i32.trunc_f64_s", ()),
    0xAB: ("i32.trunc_f64_u", ()),
    0xAC: ("i64.extend_i32_s", ()),
    0xAD: ("i64.extend_i32_u", ()),
    0xAE: ("i64.trunc_f32_s", ()),
    0xAF: ("i64.trunc_f32_u", ()),
    0xB0: ("i64.trunc_f64_s", ()),
    0xB1: ("i64.trunc_f64_u", ()),
    0xB2: ("f32.convert_i32_s", ()),
    0xB3: ("f32.convert_i32_u", ()),
    0xB4: ("f32.convert_i64_s", ()),
    0xB5: ("f32.convert_i64_u", ()),
    0xB6: ("f32.demote_f64", ()),
    0xB7: ("f64.convert_i32_s", ()),
    0xB8: ("f64.convert_i32_u", ()),
    0xB9: ("f64.convert_i64_s", ()),
    0xBA: ("f64.convert_i64_u", ()),
    0xBB: ("f64.promote_f32", ()),
    0xBC: ("i32.reinterpret_f32", ()),
    0xBD: ("i64.reinterpret_f64", ()),
    0xBE: ("f32.reinterpret_i32", ()),
    0xBF: ("f64.reinterpret_i64", ()),
    0xC0: ("i32.extend8_s", ()),
    0xC1: ("i32.extend16_s", ()),
    0xC2: ("i64.extend8_s", ()),
    0xC3: ("i64.extend16_s", ()),
    0xC4: ("i64.extend32_s", ()),
    0xD0: ("ref.null", ("tipo_referencia",)),
    0xD1: ("ref.is_null", ()),
    0xD2: ("ref.func", ("u32",)),
}

# El prefijo 0xFC: el subopcode va en ULEB y las conversiones saturadas son las
# únicas que existen ahí en el MVP.
PREFIJO_FC: dict[int, tuple[str, tuple[str, ...]]] = {
    0: ("i32.trunc_sat_f32_s", ()),
    1: ("i32.trunc_sat_f32_u", ()),
    2: ("i32.trunc_sat_f64_s", ()),
    3: ("i32.trunc_sat_f64_u", ()),
    4: ("i64.trunc_sat_f32_s", ()),
    5: ("i64.trunc_sat_f32_u", ()),
    6: ("i64.trunc_sat_f64_s", ()),
    7: ("i64.trunc_sat_f64_u", ()),
}

# Las que no son del MVP pero el backend las usa para `memory.copy` y
# `memory.fill`. Vienen de "bulk memory" y son post-MVP: los navegadores las
# tienen, y el ensamblador tiene que saber leerlas para que el round trip no
# se quede corto justo en el módulo nuevo.
PREFIJO_FC_POSTMVP: dict[int, tuple[str, tuple[str, ...]]] = {
    10: ("memory.copy", ("u32", "u32")),
    11: ("memory.fill", ("u32",)),
}

VALTYPES = {0x7F: "i32", 0x7E: "i64", 0x7D: "f32", 0x7C: "f64",
            0x70: "funcref", 0x6F: "externref"}


# --------------------------------------------------------------- desensamblado


@dataclass
class _Cursor:
    datos: bytes
    pos: int = 0

    def byte(self) -> int:
        if self.pos >= len(self.datos):
            raise WatError(f"seacabaron los bytes en {self.pos}")
        valor = self.datos[self.pos]
        self.pos += 1
        return valor

    def uleb(self) -> int:
        valor, self.pos = _uleb(self.datos, self.pos)
        return valor

    def sleb(self) -> int:
        valor, self.pos = _sleb(self.datos, self.pos)
        return valor

    def f32(self) -> float:
        crudo = self.datos[self.pos:self.pos + 4]
        if len(crudo) != 4:
            raise WatError(f"f32 truncado en {self.pos}")
        self.pos += 4
        return struct.unpack("<f", crudo)[0]

    def f64(self) -> float:
        crudo = self.datos[self.pos:self.pos + 8]
        if len(crudo) != 8:
            raise WatError(f"f64 truncado en {self.pos}")
        self.pos += 8
        return struct.unpack("<d", crudo)[0]

    def bytes(self, cuantos: int) -> bytes:
        if self.pos + cuantos > len(self.datos):
            raise WatError(f"seacabaron los bytes en {self.pos}")
        crudo = self.datos[self.pos:self.pos + cuantos]
        self.pos += cuantos
        return crudo

    def nombre(self) -> str:
        largo = self.uleb()
        crudo = self.bytes(largo)
        return crudo.decode("utf-8", "replace")


def _identificador(nombre: str) -> str:
    """Un nombre de WAT es `$` seguido de un identificador.

    Los nombres del IR son nombres de Source y casi siempre valen, pero un
    módulo puede tener uno con espacios o con acentos, y un WAT con `$mi
    función` no lo parsea nadie. Lo que no se puede renombrar a mano es un
    módulo que hay que comparar byte a byte, así que aquí se escapan.
    """
    if nombre and all(c.isalnum() or c in "_-.<>!$%&'*+-/:=?@\\^`|~" for c in nombre):
        return "$" + nombre
    escapado = "".join(c if c.isprintable() and c not in '"\\' else f"\\{ord(c):02x}"
                       for c in nombre)
    return f'${escapado}'


def _tipo_bloque(byte: int) -> str:
    """El byte de tipo de bloque: 0x40 vacío o un valtype."""
    if byte == 0x40:
        return ""
    nombre = VALTYPES.get(byte)
    if nombre is None:
        raise WatError(f"tipo de bloque desconocido: 0x{byte:02x}")
    return f" (result {nombre})"


def _instruccion(cur: _Cursor, piezas: list[str]) -> None:
    inicio = cur.pos
    code = cur.byte()

    if code == 0xFC:
        sub = cur.uleb()
        entrada = PREFIJO_FC.get(sub) or PREFIJO_FC_POSTMVP.get(sub)
        if entrada is None:
            raise WatError(
                f"subopcode 0xFC {sub} desconocido en {inicio}; añadirlo a la "
                "tabla antes de emitirlo, no adivinar")
        nombre, inm = entrada
    else:
        entrada = TABLA_INMEDIATOS.get(code)
        if entrada is None:
            raise WatError(
                f"opcode 0x{code:02x} desconocido en {inicio}; añadirlo a "
                "TABLA_INMEDIATOS antes de emitirlo, no adivinar")
        nombre, inm = entrada

    partes = [nombre]
    for tipo in inm:
        if tipo == "u32":
            partes.append(str(cur.uleb()))
        elif tipo == "i32":
            partes.append(str(cur.sleb()))
        elif tipo == "i64":
            partes.append(str(cur.sleb()))
        elif tipo == "f32":
            partes.append(_hex_f32(cur))
        elif tipo == "f64":
            partes.append(_hex_f64(cur))
        elif tipo == "memarg":
            alineado = cur.uleb()
            desplazamiento = cur.uleb()
            partes.append(f"offset={desplazamiento} align={alineado}")
        elif tipo == "bloque":
            partes.append(_tipo_bloque(cur.byte()).strip())
        elif tipo == "tipo_referencia":
            byte = cur.byte()
            partes.append(VALTYPES.get(byte, f"0x{byte:02x}"))
        else:
            raise WatError(f"tipo de inmediato {tipo!r} sin implementar")

    piezas.append(" ".join(p for p in partes if p != ""))


# ------------------------------------------------------------- las secciones


def _secciones(datos: bytes) -> list[tuple[int, bytes]]:
    """Reparte el módulo en secciones, en el orden en que aparecen.

    No se comprueba el orden canónico: al servidor no le interesa y el módulo
    es válido si el orden es el que corresponde. Lo que sí se rechaza es un id
    de sección que no existe, porque a partir de ahí no se sabe qué length
    leer.
    """
    if datos[:4] != MAGIC:
        raise WatError("no empieza por el magma \\0asm")
    if datos[4:8] != w.VERSION:
        raise WatError(f"versión {datos[4:8]!r}, se esperaba {w.VERSION!r}")
    salida: list[tuple[int, bytes]] = []
    pos = 8
    while pos < len(datos):
        section_id = datos[pos]
        pos += 1
        largo, pos = _uleb(datos, pos)
        if pos + largo > len(datos):
            raise WatError(f"la sección {section_id} dice {largo} B y no caben")
        salida.append((section_id, datos[pos:pos + largo]))
        pos += largo
    return salida


NOMBRES_SECCION = {
    w.SEC_TYPE: "type", w.SEC_IMPORT: "import", w.SEC_FUNCTION: "function",
    w.SEC_TABLE: "table", w.SEC_MEMORY: "memory", w.SEC_GLOBAL: "global",
    w.SEC_EXPORT: "export", w.SEC_START: "start", w.SEC_ELEMENT: "element",
    w.SEC_CODE: "code", w.SEC_DATA: "data", w.SEC_DATACOUNT: "datacount",
    w.SEC_CUSTOM: "custom",
}

KIND_NOMBRE = {0: "func", 1: "table", 2: "memory", 3: "global"}


def _firmas(payload: bytes) -> list[tuple[list[str], list[str]]]:
    cur = _Cursor(payload)
    firmas = []
    for _ in range(cur.uleb()):
        if cur.byte() != 0x60:
            raise WatError("una firma que no empieza por 0x60 no es una firma")
        params = [VALTYPES.get(cur.byte(), "?") for _ in range(cur.uleb())]
        results = [VALTYPES.get(cur.byte(), "?") for _ in range(cur.uleb())]
        firmas.append((params, results))
    return firmas


def _limites(cur: _Cursor) -> str:
    flag = cur.byte()
    minimo = cur.uleb()
    if flag & 0x01:
        return f"{minimo} {cur.uleb()}"
    return str(minimo)


def _cuerpo(cur: _Cursor) -> list[tuple[str, int]]:
    """Las instrucciones de una función con su nivel de anidamiento.

    El `end` que cierra la función es el que baja la profundidad por debajo de
    cero, y no se puede contar a mano: contarlo mal es la fuente clásica de un
    cuerpo que se traga el principio del siguiente. Por eso se cuenta en el
    recorrido y no leyendo el tamaño: el tamaño dice dónde termina, y el
    recorrido dice dónde está cada `end`.
    """
    salida: list[tuple[str, int]] = []
    profundidad = 0
    while True:
        if cur.pos >= len(cur.datos):
            raise WatError("el cuerpo se acabó sin `end` final")
        code = cur.datos[cur.pos]
        if code == 0x0B and profundidad == 0:
            # El `end` que cierra la **función** no se imprime: en WAT lo cierra
            # el paréntesis de `(func ...)`. Se emite y luego lo añade el
            # ensamblador; dejarlo en el texto lo hacía aparecer dos veces en el
            # módulo reensamblado, y un `end` de más descoloca el bytecode de la
            # función siguiente.
            cur.byte()
            return salida
        piezas: list[str] = []
        _instruccion(cur, piezas)
        salida.append((" ".join(p for p in piezas if p), profundidad))
        if code in (0x02, 0x03, 0x04):
            profundidad += 1
        elif code == 0x0B:
            profundidad -= 1


@dataclass
class _Modulo:
    """El módulo desensamblado, en piezas, antes de imprimirlo.

    Se recoge en dos pasadas porque el código y la firma de una función están en
    secciones distintas, y emitirlos en WAT exige tener las dos a la vez: la
    forma `(func (type N) (local ...) ...)` es la que lee un ensamblador, y una
    firma en un comentario no la lee nadie.
    """

    firmas: list[tuple[list[str], list[str]]] = None
    imports: list[tuple[str, str, int]] = None
    memoria: list[str] = None
    globals: list[str] = None
    exports: list[tuple[str, int, int]] = None
    inicio: int | None = None
    tipos_de_funcion: list[int] = None
    cuerpos: list[list[tuple[str, int, list[str]]]] = None
    datos: list[tuple[int, bytes]] = None
    tables: list[int] = None

    def __post_init__(self):
        self.firmas = self.firmas or []
        self.imports = self.imports or []
        self.memoria = self.memoria or []
        self.globals = self.globals or []
        self.exports = self.exports or []
        self.tipos_de_funcion = self.tipos_de_funcion or []
        self.cuerpos = self.cuerpos or []
        self.datos = self.datos or []
        self.tables = self.tables or []


def _leer(datos: bytes) -> _Modulo:
    """Lee el módulo entero a piezas."""
    mod = _Modulo()
    for section_id, payload in _secciones(datos):
        nombre = NOMBRES_SECCION.get(section_id)
        if nombre is None:
            raise WatError(f"sección {section_id} desconocida")
        cur = _Cursor(payload)

        if section_id == w.SEC_TYPE:
            mod.firmas = _firmas(payload)

        elif section_id == w.SEC_IMPORT:
            for _ in range(cur.uleb()):
                modulo = cur.nombre()
                cosa = cur.nombre()
                if cur.byte() != 0:
                    raise WatError("import de algo que no es función, que el "
                                   "backend no emite")
                mod.imports.append((modulo, cosa, cur.uleb()))

        elif section_id == w.SEC_FUNCTION:
            mod.tipos_de_funcion = [cur.uleb() for _ in range(cur.uleb())]

        elif section_id == w.SEC_TABLE:
            for _ in range(cur.uleb()):
                if cur.byte() != 0x70:
                    raise WatError("una tabla que no es de funciones")
                cur.byte()
                mod.tables.append(cur.uleb())

        elif section_id == w.SEC_MEMORY:
            for _ in range(cur.uleb()):
                mod.memoria.append(_limites(cur))

        elif section_id == w.SEC_GLOBAL:
            for _ in range(cur.uleb()):
                tipo = VALTYPES.get(cur.byte(), "?")
                mut = cur.byte()
                if cur.byte() != 0x41:
                    raise WatError("un global que no se inicializa con "
                                   "`i32.const`, que el encoder no emite")
                valor = cur.sleb()
                if cur.byte() != 0x0B:
                    raise WatError("la inicialización de un global no termina "
                                   "en `end`")
                mod.globals.append(
                    f"{'mut ' if mut else ''}{tipo} (i32.const {valor})")

        elif section_id == w.SEC_EXPORT:
            for _ in range(cur.uleb()):
                mod.exports.append((cur.nombre(), cur.byte(), cur.uleb()))

        elif section_id == w.SEC_START:
            mod.inicio = cur.uleb()

        elif section_id == w.SEC_CODE:
            for _ in range(cur.uleb()):
                largo = cur.uleb()
                fin = cur.pos + largo
                cuerpo = _Cursor(cur.datos[:fin], cur.pos)
                cur.pos = fin
                locales: list[str] = []
                for _ in range(cuerpo.uleb()):
                    cuenta = cuerpo.uleb()
                    tipo = VALTYPES.get(cuerpo.byte(), "?")
                    locales += [tipo] * cuenta
                mod.cuerpos.append((locales, _cuerpo(cuerpo)))

        elif section_id == w.SEC_DATA:
            for _ in range(cur.uleb()):
                if cur.byte() != 0x00:
                    raise WatError("segmento de datos pasivo, que el encoder "
                                   "no emite")
                # El desplazamiento es una expresión de inicialización, no un
                # entero suelto: viene como instrucción `i32.const` completa,
                # opcode incluido. Leer el SLEB sin el opcode delante consumía
                # el 0x41 y devolvía un número sin sentido.
                if cur.byte() != 0x41:
                    raise WatError("el desplazamiento de un segmento de datos "
                                   "no es un `i32.const`")
                offset = cur.sleb()
                if cur.byte() != 0x0B:
                    raise WatError("el desplazamiento de un segmento de datos "
                                   "no termina en `end`")
                mod.datos.append((offset, cur.bytes(cur.uleb())))

        elif section_id in (w.SEC_DATACOUNT, w.SEC_CUSTOM, w.SEC_ELEMENT):
            raise WatError(f"sección {nombre} presente; el backend no la emite")

    return mod


def desensamblar(datos: bytes, sangria: str = "  ") -> str:
    """El módulo entero como texto WAT.

    Es **WAT de verdad**, no un formato propio con comentarios: si el emisor
    escribiera la firma de una función en un `;;`, el ensamblador tendría que
    leer comentarios y el round trip no compararía nada, porque las dos mitades
    acordarían un formato que no es el del formato.
    """
    mod = _leer(datos)
    lineas = ["(module"]

    for params, results in mod.firmas:
        linea = f"{sangria}(type (func"
        if params:
            linea += f" (param {' '.join(params)})"
        if results:
            linea += f" (result {' '.join(results)})"
        lineas.append(linea + "))")

    for modulo, cosa, tipo in mod.imports:
        lineas.append(f'{sangria}(import "{modulo}" "{cosa}" '
                     f"(func (type {tipo})))")

    for minimo in mod.tables:
        lineas.append(f"{sangria}(table funcref (elem {minimo}))")

    for limites in mod.memoria:
        lineas.append(f"{sangria}(memory {limites})")

    for global_ in mod.globals:
        lineas.append(f"{sangria}(global {global_})")

    for nombre, kind, indice in mod.exports:
        clase = KIND_NOMBRE.get(kind, str(kind))
        lineas.append(f'{sangria}(export "{nombre}" ({clase} {indice}))')

    if mod.inicio is not None:
        lineas.append(f"{sangria}(start {mod.inicio})")

    for indice, (locales, cuerpo) in enumerate(mod.cuerpos):
        tipo = (mod.tipos_de_funcion[indice] if indice < len(mod.tipos_de_funcion)
                else 0)
        cabecera = f"{sangria}(func (type {tipo})"
        if locales:
            cabecera += f" (local {' '.join(locales)})"
        lineas.append(cabecera)
        for texto, nivel in cuerpo:
            lineas.append("  " * (nivel + 2) + texto)
        lineas.append(f"{sangria})")

    for offset, crudo in mod.datos:
        lineas.append(f'{sangria}(data (i32.const {offset}) '
                     f'"{_escapar(crudo)}")')

    lineas.append(")")
    return "\n".join(lineas) + "\n"


def _escapar(crudo: bytes) -> str:
    """Bytes de un segmento de datos como cadena WAT.

    Se escapan **los bytes**, no el Unicode: un segmento puede tener un `0x80`
    suelto que UTF-8 no representa, y `bytes.decode('utf-8').encode('utf-8')`
    no vuelve a dar los mismos bytes.
    """
    partes = []
    for byte in crudo:
        if byte == 0x22:
            partes.append('\\"')
        elif byte == 0x5C:
            partes.append("\\\\")
        elif 0x20 <= byte < 0x7F:
            partes.append(chr(byte))
        else:
            partes.append(f"\\{byte:02x}")
    return "".join(partes)
