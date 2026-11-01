"""Encoder WASM: LEB128, secciones, y verificación opcode a opcode.

La parte importante de este fichero no es que el encoder funcione, sino que
**sus opcodes estén comprobados**. Escribir noventa opcodes de memoria es una
fuente de errores garantizada: uno mal escrito no falla al compilar el módulo,
falla en el navegador tres fases después.

Así que cada opcode se verifica mecánicamente: se genera un módulo mínimo que lo
usa y se le pide a Node que lo valide. Node implementa la especificación
completa, así que es un juez autoritativo, no un testigo generoso.
"""

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sourc.wasm import encoder as w

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


VALIDATE_SCRIPT = r"""
import { readFileSync } from "node:fs";
const hexes = readFileSync(process.argv[2], "utf8").trim().split("\n").filter(Boolean);
const results = hexes.map((hex) => {
  const bytes = new Uint8Array(hex.match(/../g).map((h) => parseInt(h, 16)));
  return WebAssembly.validate(bytes);
});
process.stdout.write(JSON.stringify(results));
"""


def validate_with_node(modules: list[bytes], timeout: int = 120):
    """Pide a Node que valide cada módulo. Devuelve la lista de resultados.

    Un solo proceso para todos los módulos: lanzar Node mil veces cuesta más
    que el test entero, y la validación es la parte lenta.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".hex", delete=False) as handle:
        handle.write("\n".join(m.hex() for m in modules))
        path = handle.name
    try:
        script_path = path + ".mjs"
        Path(script_path).write_text(VALIDATE_SCRIPT, encoding="utf-8")
        result = subprocess.run([NODE, script_path, path],
                                capture_output=True, text=True,
                                timeout=timeout)
    finally:
        for leftover in (path, path + ".mjs"):
            try:
                os.unlink(leftover)
            except OSError:
                pass
    if result.returncode != 0:
        raise AssertionError(f"node falló: {result.stderr[:400]}")
    return json.loads(result.stdout)


def node_validates(module: bytes) -> bool:
    return validate_with_node([module])[0]


def node_rejects(module: bytes) -> bool:
    return not node_validates(module)


class TestLEB128(unittest.TestCase):
    """Vectores de la especificación y los casos donde la gente se equivoca."""

    def test_single_byte_range(self):
        for value in (0, 1, 62, 63, 64, 127, 128):
            with self.subTest(valor=value):
                encoded = w.uleb(value)
                decoded, consumed = w.read_uleb(encoded, 0)
                self.assertEqual(decoded, value)
                self.assertEqual(consumed, len(encoded))

    def test_uleb_spec_examples(self):
        # Vectores del apéndice del Core Specification.
        self.assertEqual(w.uleb(0).hex(), "00")
        self.assertEqual(w.uleb(1).hex(), "01")
        self.assertEqual(w.uleb(124).hex(), "7c")
        self.assertEqual(w.uleb(128).hex(), "8001")
        self.assertEqual(w.uleb(624485).hex(), "e5 8e 26".replace(" ", ""))
        self.assertEqual(w.uleb(624485), bytes((0xE5, 0x8E, 0x26)))

    def test_sleb_spec_examples(self):
        self.assertEqual(w.sleb(0).hex(), "00")
        self.assertEqual(w.sleb(1).hex(), "01")
        self.assertEqual(w.sleb(-1).hex(), "7f")
        self.assertEqual(w.sleb(63).hex(), "3f")
        self.assertEqual(w.sleb(64).hex(), "c000")
        self.assertEqual(w.sleb(-64).hex(), "40")
        self.assertEqual(w.sleb(-65).hex(), "bf7f")

    def test_sleb_round_trips_extremes(self):
        for value in (-2**63, -2**31, -129, -128, -127, -1, 0, 1, 63, 64, 127,
                      128, 2**31 - 1, 2**63 - 1):
            with self.subTest(valor=value):
                encoded = w.sleb(value)
                self.assertEqual(decode_sleb(encoded), value)

    def test_uleb_rejects_negative(self):
        with self.assertRaises(w.WasmEncodeError):
            w.uleb(-1)

    def test_float_bytes_are_little_endian(self):
        self.assertEqual(w.encode_f32(1.0).hex(), "0000803f")
        self.assertEqual(w.encode_f64(1.0).hex(), "000000000000f03f")


def section_ids(module: bytes) -> list[int]:
    """Lista de identificadores de sección en orden.

    Buscar un byte suelto dentro del módulo no dice nada: casi todos los valores
    posibles aparecen en el código. Hay que recorrer la estructura.
    """
    position = 8
    found: list[int] = []
    while position < len(module):
        section_id = module[position]
        found.append(section_id)
        position += 1
        size, consumed = w.read_uleb(module, position)
        position += consumed + size
    return found


def decode_sleb(data: bytes) -> int:
    result = 0
    shift = 0
    for index, byte in enumerate(data):
        result |= (byte & 0x7F) << shift
        shift += 7
        if (byte & 0x80) == 0:
            if byte & 0x40:
                result -= 1 << shift
            return result
    raise AssertionError("LEB sin byte final")


class TestModuleStructure(unittest.TestCase):
    """El esqueleto del módulo, byte a byte."""

    def test_minimum_module_is_exact(self):
        builder = w.ModuleBuilder()
        func = builder.add_function("main", results=(w.I32,))
        func.emit(w.i32_const(0))
        builder.export("main", w.KIND_FUNC, builder.func_index(func))
        data = builder.to_bytes()

        self.assertEqual(data[:8], bytes((0x00, 0x61, 0x73, 0x6D, 0x01, 0x00,
                                          0x00, 0x00)))
        if NODE:
            self.assertTrue(node_validates(data), data.hex())

    def test_magic_and_version(self):
        self.assertEqual(w.MAGIC, b"\x00asm")
        self.assertEqual(w.VERSION, bytes((1, 0, 0, 0)))

    def test_empty_sections_are_omitted(self):
        """Una sección ausente es una de tamaño cero: escribirla cuesta bytes.

        El presupuesto pesa en bytes, no enHAMR.
        """
        builder = w.ModuleBuilder()
        func = builder.add_function("main", results=(w.I32,))
        func.emit(w.i32_const(0))
        data = builder.to_bytes()
        # Sin imports, memoria, globales ni datos: sólo type, function, code.
        self.assertEqual(sorted(section_ids(data)), [w.SEC_TYPE,
                                                     w.SEC_FUNCTION,
                                                     w.SEC_CODE])

    def test_sections_are_in_order(self):
        builder = w.ModuleBuilder()
        builder.import_function("env", "f", (), ())
        builder.add_memory(1)
        builder.add_global(w.I32, True, w.i32_const(0))
        builder.add_data(0, b"hola")
        func = builder.add_function("main", results=(w.I32,))
        func.emit(w.i32_const(0))
        data = builder.to_bytes()
        w.validate(data)
        if NODE:
            self.assertTrue(node_validates(data))

    def test_validate_rejects_bad_magic(self):
        builder = w.ModuleBuilder()
        func = builder.add_function("main", results=(w.I32,))
        func.emit(w.i32_const(0))
        data = bytearray(builder.to_bytes())
        data[1] = 0x62
        with self.assertRaises(w.WasmEncodeError):
            w.validate(bytes(data))

    def test_validate_rejects_out_of_order_sections(self):
        types = w.encode_vector([w.FuncType((), (w.I32,)).render()])
        # CODE (10) antes que TYPE (1): el formato exige orden creciente.
        raw = (w.MAGIC + w.VERSION
               + bytes((w.SEC_CODE,)) + w.uleb(len(types)) + types
               + bytes((w.SEC_TYPE,)) + w.uleb(len(types)) + types)
        with self.assertRaises(w.WasmEncodeError):
            w.validate(raw)

    def test_validate_rejects_unknown_section(self):
        raw = (w.MAGIC + w.VERSION + bytes((200,)) + w.uleb(1) + b"\x00")
        with self.assertRaises(w.WasmEncodeError):
            w.validate(raw)

    def test_validate_rejects_truncated_section(self):
        raw = w.MAGIC + w.VERSION + bytes((w.SEC_TYPE, 40)) + b"\x01"
        with self.assertRaises(w.WasmEncodeError):
            w.validate(raw)


class TestLocalsEncoding(unittest.TestCase):
    def test_locals_are_grouped_without_being_reordered(self):
        """Run-length encoding: contiguos del mismo tipo se agrupan.

        Y **no** se reordenan por tipo, porque eso permuta los índices en
        silencio. El fuzzer del encoder lo cazó: `f64` (0x7C) ordena antes que
        `i32` (0x7F), así que el local 0 dejaba de ser el primero declarado y
        todos los índices del código apuntaban a otro tipo. El módulo seguía
        validando, que es lo que lo hace peligroso.
        """
        builder = w.ModuleBuilder()
        func = builder.add_function(
            "f", results=(w.I32,),
            locals=[(2, w.I64), (1, w.F64), (2, w.I32)])
        func.emit(w.i32_const(0))
        rendered = func.render()
        # Declaración: (2 i64, 1 f64, 2 i32). Ningún par contiguo del mismo
        # tipo, así que salen tres grupos tal cual.
        self.assertTrue(rendered.endswith(b"\x00\x0b"), rendered.hex())
        self.assertEqual(rendered[1:8],
                         bytes((3, 2, w.I64, 1, w.F64, 2, w.I32)))

    def test_adjacent_same_type_locals_merge(self):
        builder = w.ModuleBuilder()
        func = builder.add_function("f", results=(w.I32,),
                                    locals=[(1, w.I32), (1, w.I32), (2, w.F64)])
        func.emit(w.i32_const(0))
        rendered = func.render()
        self.assertEqual(rendered[1:6],
                         bytes((2, 2, w.I32, 2, w.F64)))

    def test_zero_count_locals_are_dropped(self):
        """Un grupo de tamaño cero es inválido según la especificación.

        El backend los genera sin darse cuenta: basta con pedir (1, i32) y
        (0, i32) al agrupar y el módulo entero deja de validar.
        """
        builder = w.ModuleBuilder()
        func = builder.add_function("f", results=(w.I32,),
                                    locals=[(0, w.I32), (1, w.I32)])
        func.emit(w.i32_const(0))
        rendered = func.render()
        self.assertEqual(rendered[1:4], bytes((1, 1, w.I32)))

    def test_local_indices_are_stable(self):
        """El índice 0 es siempre el primer local declarado.

        Este test y el fuzzer son la misma defensa por dos lados: aquí se
        comprueba la propiedad, allí se buscan las consecuencias.
        """
        builder = w.ModuleBuilder()
        func = builder.add_function("main", results=(w.I32,),
                                    locals=[(1, w.F64), (1, w.I32)])
        func.emit(w.local_get(0))
        body = func.render()
        # Vector de locales: (1, f64) luego (1, i32), en ese orden.
        self.assertEqual(body[1:6], bytes((2, 1, w.F64, 1, w.I32)))
        self.assertIn(b"\x20\x00", body)   # local.get 0

    def test_function_indexes_account_for_imports(self):
        """El espacio de funciones continúa el de imports. Confundirlos genera
        llamadas a la función equivocada sin ningún error de validación."""
        builder = w.ModuleBuilder()
        builder.import_function("env", "uno", (), ())
        builder.import_function("env", "dos", (), ())
        body = builder.add_function("main", results=(w.I32,))
        self.assertEqual(builder.func_index(body), 2)
        self.assertEqual(builder.import_count, 2)


# ------------------------------------------------------------------ opcodes
#
# Tablas para verificar mecánicamente que cada opcode es el que dice ser.
# `entradas` son operandos que se dejan en la pila antes de la instrucción.
# `resultado` es lo que debe dejar, para emitir el cuerpo completo.

UNARY = [
    ("i32.eqz", w.I32_EQZ, w.I32, w.I32),
    ("i64.eqz", w.I64_EQZ, w.I64, w.I32),
    ("i32.clz", w.I32_CLZ, w.I32, w.I32),
    ("i32.ctz", w.I32_CTZ, w.I32, w.I32),
    ("i32.popcnt", w.I32_POPCNT, w.I32, w.I32),
    ("i64.clz", w.I64_CLZ, w.I64, w.I32),
    ("i64.ctz", w.I64_CTZ, w.I64, w.I32),
    ("i64.popcnt", w.I64_POPCNT, w.I64, w.I32),
    ("f32.abs", w.F32_ABS, w.F32, w.F32),
    ("f32.neg", w.F32_NEG, w.F32, w.F32),
    ("f32.ceil", w.F32_CEIL, w.F32, w.F32),
    ("f32.floor", w.F32_FLOOR, w.F32, w.F32),
    ("f32.trunc", w.F32_TRUNC, w.F32, w.F32),
    ("f32.nearest", w.F32_NEAREST, w.F32, w.F32),
    ("f32.sqrt", w.F32_SQRT, w.F32, w.F32),
    ("f64.abs", w.F64_ABS, w.F64, w.F64),
    ("f64.neg", w.F64_NEG, w.F64, w.F64),
    ("f64.ceil", w.F64_CEIL, w.F64, w.F64),
    ("f64.floor", w.F64_FLOOR, w.F64, w.F64),
    ("f64.trunc", w.F64_TRUNC, w.F64, w.F64),
    ("f64.nearest", w.F64_NEAREST, w.F64, w.F64),
    ("f64.sqrt", w.F64_SQRT, w.F64, w.F64),
    ("i32.wrap_i64", w.I32_WRAP_I64, w.I64, w.I32),
    ("i32.trunc_f32_s", w.I32_TRUNC_F32_S, w.F32, w.I32),
    ("i32.trunc_f32_u", w.I32_TRUNC_F32_U, w.F32, w.I32),
    ("i32.trunc_f64_s", w.I32_TRUNC_F64_S, w.F64, w.I32),
    ("i32.trunc_f64_u", w.I32_TRUNC_F64_U, w.F64, w.I32),
    ("i64.extend_i32_s", w.I64_EXTEND_I32_S, w.I32, w.I64),
    ("i64.extend_i32_u", w.I64_EXTEND_I32_U, w.I32, w.I64),
    ("i64.trunc_f32_s", w.I64_TRUNC_F32_S, w.F32, w.I64),
    ("i64.trunc_f32_u", w.I64_TRUNC_F32_U, w.F32, w.I64),
    ("i64.trunc_f64_s", w.I64_TRUNC_F64_S, w.F64, w.I64),
    ("i64.trunc_f64_u", w.I64_TRUNC_F64_U, w.F64, w.I64),
    ("f32.convert_i32_s", w.F32_CONVERT_I32_S, w.I32, w.F32),
    ("f32.convert_i32_u", w.F32_CONVERT_I32_U, w.I32, w.F32),
    ("f32.convert_i64_s", w.F32_CONVERT_I64_S, w.I64, w.F32),
    ("f32.convert_i64_u", w.F32_CONVERT_I64_U, w.I64, w.F32),
    ("f32.demote_f64", w.F32_DEMOTE_F64, w.F64, w.F32),
    ("f64.convert_i32_s", w.F64_CONVERT_I32_S, w.I32, w.F64),
    ("f64.convert_i32_u", w.F64_CONVERT_I32_U, w.I32, w.F64),
    ("f64.convert_i64_s", w.F64_CONVERT_I64_S, w.I64, w.F64),
    ("f64.convert_i64_u", w.F64_CONVERT_I64_U, w.I64, w.F64),
    ("f64.promote_f32", w.F64_PROMOTE_F32, w.F32, w.F64),
    ("i32.reinterpret_f32", w.I32_REINTERPRET_F32, w.F32, w.I32),
    ("i64.reinterpret_f64", w.I64_REINTERPRET_F64, w.F64, w.I64),
    ("f32.reinterpret_i32", w.F32_REINTERPRET_I32, w.I32, w.F32),
    ("f64.reinterpret_i64", w.F64_REINTERPRET_I64, w.I64, w.F64),
    ("i32.extend8_s", w.I32_EXTEND8_S, w.I32, w.I32),
    ("i32.extend16_s", w.I32_EXTEND16_S, w.I32, w.I32),
    ("i64.extend8_s", w.I64_EXTEND8_S, w.I64, w.I64),
    ("i64.extend16_s", w.I64_EXTEND16_S, w.I64, w.I64),
    ("i64.extend32_s", w.I64_EXTEND32_S, w.I64, w.I64),
]

BINARY = [
    # comparación con signo: `i32.eq` toma dos operandos, no uno
    ("i32.eq", w.I32_EQ, w.I32, w.I32),
    ("i32.ne", w.I32_NE, w.I32, w.I32),
    ("i32.lt_s", w.I32_LT_S, w.I32, w.I32),
    ("i32.lt_u", w.I32_LT_U, w.I32, w.I32),
    ("i32.gt_s", w.I32_GT_S, w.I32, w.I32),
    ("i32.gt_u", w.I32_GT_U, w.I32, w.I32),
    ("i32.le_s", w.I32_LE_S, w.I32, w.I32),
    ("i32.le_u", w.I32_LE_U, w.I32, w.I32),
    ("i32.ge_s", w.I32_GE_S, w.I32, w.I32),
    ("i32.ge_u", w.I32_GE_U, w.I32, w.I32),
    ("i64.eq", w.I64_EQ, w.I64, w.I32),
    ("i64.ne", w.I64_NE, w.I64, w.I32),
    ("i64.lt_s", w.I64_LT_S, w.I64, w.I32),
    ("i64.lt_u", w.I64_LT_U, w.I64, w.I32),
    ("i64.gt_s", w.I64_GT_S, w.I64, w.I32),
    ("i64.gt_u", w.I64_GT_U, w.I64, w.I32),
    ("i64.le_s", w.I64_LE_S, w.I64, w.I32),
    ("i64.le_u", w.I64_LE_U, w.I64, w.I32),
    ("i64.ge_s", w.I64_GE_S, w.I64, w.I32),
    ("i64.ge_u", w.I64_GE_U, w.I64, w.I32),
    ("f32.eq", w.F32_EQ, w.F32, w.I32),
    ("f32.ne", w.F32_NE, w.F32, w.I32),
    ("f32.lt", w.F32_LT, w.F32, w.I32),
    ("f32.gt", w.F32_GT, w.F32, w.I32),
    ("f32.le", w.F32_LE, w.F32, w.I32),
    ("f32.ge", w.F32_GE, w.F32, w.I32),
    ("f64.eq", w.F64_EQ, w.F64, w.I32),
    ("f64.ne", w.F64_NE, w.F64, w.I32),
    ("f64.lt", w.F64_LT, w.F64, w.I32),
    ("f64.gt", w.F64_GT, w.F64, w.I32),
    ("f64.le", w.F64_LE, w.F64, w.I32),
    ("f64.ge", w.F64_GE, w.F64, w.I32),
    ("i32.add", w.I32_ADD, w.I32, w.I32),
    ("i32.sub", w.I32_SUB, w.I32, w.I32),
    ("i32.mul", w.I32_MUL, w.I32, w.I32),
    ("i32.div_s", w.I32_DIV_S, w.I32, w.I32),
    ("i32.div_u", w.I32_DIV_U, w.I32, w.I32),
    ("i32.rem_s", w.I32_REM_S, w.I32, w.I32),
    ("i32.rem_u", w.I32_REM_U, w.I32, w.I32),
    ("i32.and", w.I32_AND, w.I32, w.I32),
    ("i32.or", w.I32_OR, w.I32, w.I32),
    ("i32.xor", w.I32_XOR, w.I32, w.I32),
    ("i32.shl", w.I32_SHL, w.I32, w.I32),
    ("i32.shr_s", w.I32_SHR_S, w.I32, w.I32),
    ("i32.shr_u", w.I32_SHR_U, w.I32, w.I32),
    ("i32.rotl", w.I32_ROTL, w.I32, w.I32),
    ("i32.rotr", w.I32_ROTR, w.I32, w.I32),
    ("i64.add", w.I64_ADD, w.I64, w.I64),
    ("i64.sub", w.I64_SUB, w.I64, w.I64),
    ("i64.mul", w.I64_MUL, w.I64, w.I64),
    ("i64.div_s", w.I64_DIV_S, w.I64, w.I64),
    ("i64.div_u", w.I64_DIV_U, w.I64, w.I64),
    ("i64.rem_s", w.I64_REM_S, w.I64, w.I64),
    ("i64.rem_u", w.I64_REM_U, w.I64, w.I64),
    ("i64.and", w.I64_AND, w.I64, w.I64),
    ("i64.or", w.I64_OR, w.I64, w.I64),
    ("i64.xor", w.I64_XOR, w.I64, w.I64),
    ("i64.shl", w.I64_SHL, w.I64, w.I64),
    ("i64.shr_s", w.I64_SHR_S, w.I64, w.I64),
    ("i64.shr_u", w.I64_SHR_U, w.I64, w.I64),
    ("i64.rotl", w.I64_ROTL, w.I64, w.I64),
    ("i64.rotr", w.I64_ROTR, w.I64, w.I64),
    ("f32.add", w.F32_ADD, w.F32, w.F32),
    ("f32.sub", w.F32_SUB, w.F32, w.F32),
    ("f32.mul", w.F32_MUL, w.F32, w.F32),
    ("f32.div", w.F32_DIV, w.F32, w.F32),
    ("f32.min", w.F32_MIN, w.F32, w.F32),
    ("f32.max", w.F32_MAX, w.F32, w.F32),
    ("f32.copysign", w.F32_COPYSIGN, w.F32, w.F32),
    ("f64.add", w.F64_ADD, w.F64, w.F64),
    ("f64.sub", w.F64_SUB, w.F64, w.F64),
    ("f64.mul", w.F64_MUL, w.F64, w.F64),
    ("f64.div", w.F64_DIV, w.F64, w.F64),
    ("f64.min", w.F64_MIN, w.F64, w.F64),
    ("f64.max", w.F64_MAX, w.F64, w.F64),
    ("f64.copysign", w.F64_COPYSIGN, w.F64, w.F64),
]

# (nombre, código, alineación log2, tipo de carga, tipo de valor)
LOADS = [
    ("i32.load", w.I32_LOAD, 2, w.I32, w.I32),
    ("i64.load", w.I64_LOAD, 3, w.I64, w.I64),
    ("f32.load", w.F32_LOAD, 2, w.F32, w.F32),
    ("f64.load", w.F64_LOAD, 3, w.F64, w.F64),
    ("i32.load8_s", w.I32_LOAD8_S, 0, w.I32, w.I32),
    ("i32.load8_u", w.I32_LOAD8_U, 0, w.I32, w.I32),
    ("i32.load16_s", w.I32_LOAD16_S, 1, w.I32, w.I32),
    ("i32.load16_u", w.I32_LOAD16_U, 1, w.I32, w.I32),
    ("i64.load8_s", w.I64_LOAD8_S, 0, w.I64, w.I64),
    ("i64.load8_u", w.I64_LOAD8_U, 0, w.I64, w.I64),
    ("i64.load16_s", w.I64_LOAD16_S, 1, w.I64, w.I64),
    ("i64.load16_u", w.I64_LOAD16_U, 1, w.I64, w.I64),
    ("i64.load32_s", w.I64_LOAD32_S, 2, w.I64, w.I64),
    ("i64.load32_u", w.I64_LOAD32_U, 2, w.I64, w.I64),
]

STORES = [
    ("i32.store", w.I32_STORE, 2, w.I32),
    ("i64.store", w.I64_STORE, 3, w.I64),
    ("f32.store", w.F32_STORE, 2, w.F32),
    ("f64.store", w.F64_STORE, 3, w.F64),
    ("i32.store8", w.I32_STORE8, 0, w.I32),
    ("i32.store16", w.I32_STORE16, 1, w.I32),
    ("i64.store8", w.I64_STORE8, 0, w.I64),
    ("i64.store16", w.I64_STORE16, 1, w.I64),
    ("i64.store32", w.I64_STORE32, 2, w.I64),
]


def push(value_type: int) -> bytes:
    if value_type == w.I32:
        return w.i32_const(1)
    if value_type == w.I64:
        return w.i64_const(1)
    if value_type == w.F32:
        return w.f32_const(1.0)
    return w.f64_const(1.0)


def module_for_unary(code: int, operand: int) -> bytes:
    builder = w.ModuleBuilder("unary")
    builder.add_memory(1)
    body = builder.add_function("f", results=(w.I32,), locals=[(1, w.I32)])
    body.emit(push(operand))
    body.emit(bytes((code,)))
    body.emit(w.drop())
    body.emit(w.i32_const(0))
    return builder.to_bytes()


def module_for_binary(code: int, operand: int) -> bytes:
    builder = w.ModuleBuilder("binary")
    builder.add_memory(1)
    body = builder.add_function("f", results=(w.I32,), locals=[(1, w.I32)])
    body.emit(push(operand))
    body.emit(push(operand))
    body.emit(bytes((code,)))
    body.emit(w.drop())
    body.emit(w.i32_const(0))
    return builder.to_bytes()


def module_for_load(code: int, align: int, value_type: int) -> bytes:
    builder = w.ModuleBuilder("load")
    builder.add_memory(1)
    body = builder.add_function("f", results=(w.I32,), locals=[(1, w.I32)])
    body.emit(w.i32_const(0))
    body.emit(w.memarg(code, align, 0))
    body.emit(w.drop())
    body.emit(w.i32_const(0))
    return builder.to_bytes()


def module_for_store(code: int, align: int, value_type: int) -> bytes:
    builder = w.ModuleBuilder("store")
    builder.add_memory(1)
    body = builder.add_function("f", results=(w.I32,), locals=[(1, w.I32)])
    body.emit(w.i32_const(0))
    body.emit(push(value_type))
    body.emit(w.memarg(code, align, 0))
    body.emit(w.i32_const(0))
    return builder.to_bytes()


def module_for_prefixed(subcode: int, operand: int) -> bytes:
    builder = w.ModuleBuilder("prefixed")
    body = builder.add_function("f", results=(w.I32,), locals=[(1, w.I32)])
    body.emit(push(operand))
    body.emit(w.prefixed(subcode))
    body.emit(w.drop())
    body.emit(w.i32_const(0))
    return builder.to_bytes()


@unittest.skipUnless(NODE, "node no está instalado")
class TestOpcodesAreCorrect(unittest.TestCase):
    """Cada opcode, verificado por Node contra la especificación.

    Si alguno de estos falla, el opcode está mal escrito. No hay nada que
    negociar: la tabla de `encoder.py` está mal y hay que arreglarla.
    """

    def test_unary_opcodes(self):
        modules = [module_for_unary(code, operand)
                   for _, code, operand, _ in UNARY]
        results = validate_with_node(modules)
        bad = [name for (name, _, _, _), ok in zip(UNARY, results) if not ok]
        self.assertEqual(bad, [], f"opcodes incorrectos: {bad}")

    def test_binary_opcodes(self):
        modules = [module_for_binary(code, operand)
                   for _, code, operand, _ in BINARY]
        results = validate_with_node(modules)
        bad = [name for (name, _, _, _), ok in zip(BINARY, results) if not ok]
        self.assertEqual(bad, [], f"opcodes incorrectos: {bad}")

    def test_load_opcodes(self):
        modules = [module_for_load(code, align, value)
                   for _, code, align, _, value in LOADS]
        results = validate_with_node(modules)
        bad = [name for (name, _, _, _, _), ok in zip(LOADS, results) if not ok]
        self.assertEqual(bad, [], f"opcodes incorrectos: {bad}")

    def test_store_opcodes(self):
        modules = [module_for_store(code, align, value)
                   for _, code, align, value in STORES]
        results = validate_with_node(modules)
        bad = [name for (name, _, _, _), ok in zip(STORES, results) if not ok]
        self.assertEqual(bad, [], f"opcodes incorrectos: {bad}")

    def test_saturating_truncations(self):
        cases = [
            ("i32.trunc_sat_f32_s", w.I32_TRUNC_SAT_F32_S, w.F32),
            ("i32.trunc_sat_f32_u", w.I32_TRUNC_SAT_F32_U, w.F32),
            ("i32.trunc_sat_f64_s", w.I32_TRUNC_SAT_F64_S, w.F64),
            ("i32.trunc_sat_f64_u", w.I32_TRUNC_SAT_F64_U, w.F64),
            ("i64.trunc_sat_f32_s", w.I64_TRUNC_SAT_F32_S, w.F32),
            ("i64.trunc_sat_f32_u", w.I64_TRUNC_SAT_F32_U, w.F32),
            ("i64.trunc_sat_f64_s", w.I64_TRUNC_SAT_F64_S, w.F64),
            ("i64.trunc_sat_f64_u", w.I64_TRUNC_SAT_F64_U, w.F64),
        ]
        modules = [module_for_prefixed(code, operand)
                   for _, code, operand in cases]
        results = validate_with_node(modules)
        bad = [name for (name, _, _), ok in zip(cases, results) if not ok]
        self.assertEqual(bad, [], f"subcódigos incorrectos: {bad}")

    def test_opcodes_are_unique(self):
        """Dos nombres distintos con el mismo código es un bug esperando."""
        table: dict[int, str] = {}
        clashes = []
        for name, code, *_ in UNARY + BINARY:
            if code in table:
                clashes.append(f"{table[code]} y {name} comparten 0x{code:02X}")
            table[code] = name
        self.assertEqual(clashes, [], "opcodes duplicados en el módulo raíz: "
                         + "; ".join(clashes))

    def test_no_reserved_opcodes_used_as_values(self):
        """0xFC es un prefijo, no una instrucción.

        Si alguien asigna 0xFC a una instrucción "normal", el byte siguiente se
        interpreta como subcódigo y el módulo hace otra cosa.
        """
        for name, code, *_ in UNARY + BINARY:
            self.assertNotEqual(code, w.FC_PREFIX,
                                f"{name} usa el prefijo 0xFC como opcode")


@unittest.skipUnless(NODE, "node no está instalado")
class TestExecutionSemantics(unittest.TestCase):
    """No basta con que valide: tiene que calcular lo correcto."""

    def run_module(self, module: bytes, memory_pages: int = 1):
        script = f"""
const bytes = new Uint8Array([{','.join(str(b) for b in module)}]);
const memory = new WebAssembly.Memory({{ initial: {memory_pages} }});
const instance = new WebAssembly.Instance(new WebAssembly.Module(bytes), {{}});
const result = instance.exports.main ? instance.exports.main() : 0;
console.log(JSON.stringify(result));
"""
        out = subprocess.run([NODE, "--input-type=module", "-e", script],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise AssertionError(out.stderr[:400])
        return json.loads(out.stdout)

    def test_arithmetic_is_correct(self):
        builder = w.ModuleBuilder("calc")
        body = builder.add_function("main", results=(w.I32,))
        body.emit(w.i64_const(7))
        body.emit(w.i64_const(3))
        body.emit(bytes((w.I64_MUL,)))      # 21
        body.emit(w.i64_const(5))
        body.emit(bytes((w.I64_SUB,)))      # 16
        body.emit(w.i64_const(5))
        body.emit(bytes((w.I64_DIV_S,)))    # 3
        body.emit(bytes((w.I32_WRAP_I64,)))
        builder.export("main", w.KIND_FUNC, builder.func_index(body))
        self.assertEqual(self.run_module(builder.to_bytes()), 3)

    def test_locals_round_trip(self):
        builder = w.ModuleBuilder("locals")
        body = builder.add_function("main", results=(w.I32,), locals=[(1, w.I32)])
        body.emit(w.i32_const(42))
        body.emit(w.local_set(0))
        body.emit(w.local_get(0))
        builder.export("main", w.KIND_FUNC, builder.func_index(body))
        self.assertEqual(self.run_module(builder.to_bytes()), 42)

    def test_data_segment_lands_in_memory(self):
        builder = w.ModuleBuilder("data")
        builder.add_memory(1)
        builder.add_data(16, b"hola")
        builder.export_memory("memory")
        body = builder.add_function("main", results=(w.I32,))
        body.emit(w.i32_const(16))
        body.emit(w.memarg(w.I32_LOAD8_U, 0, 0))
        builder.export("main", w.KIND_FUNC, builder.func_index(body))
        # El 'h' de "hola" es 104.
        self.assertEqual(self.run_module(builder.to_bytes()), ord("h"))

    def test_memory_is_exported_and_readable(self):
        builder = w.ModuleBuilder("mem")
        builder.add_memory(1)
        builder.add_data(0, b"\x2a")
        builder.export_memory("memory")
        body = builder.add_function("main", results=(w.I32,))
        body.emit(w.i32_const(0))
        body.emit(w.memarg(w.I32_LOAD8_U, 0, 0))
        builder.export("main", w.KIND_FUNC, builder.func_index(body))
        script = f"""
const bytes = new Uint8Array([{','.join(str(b) for b in builder.to_bytes())}]);
const instance = new WebAssembly.Instance(new WebAssembly.Module(bytes), {{}});
const memory = instance.exports.memory;
console.log(JSON.stringify({{
  exit: instance.exports.main(),
  firstByte: new Uint8Array(memory.buffer)[0],
  pages: memory.buffer.byteLength / 65536,
}}));
"""
        out = subprocess.run([NODE, "--input-type=module", "-e", script],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr[:300])
        payload = json.loads(out.stdout)
        self.assertEqual(payload["exit"], 42)
        self.assertEqual(payload["firstByte"], 42)
        self.assertEqual(payload["pages"], 1)

    def test_import_is_called(self):
        builder = w.ModuleBuilder("importa")
        slot = builder.import_function("host", "diez", (), (w.I32,))
        body = builder.add_function("main", results=(w.I32,))
        body.emit(w.call(slot))
        builder.export("main", w.KIND_FUNC, builder.func_index(body))
        script = f"""
const bytes = new Uint8Array([{','.join(str(b) for b in builder.to_bytes())}]);
const instance = new WebAssembly.Instance(new WebAssembly.Module(bytes), {{
  host: {{ diez: () => 1234 }},
}});
console.log(JSON.stringify(instance.exports.main()));
"""
        out = subprocess.run([NODE, "--input-type=module", "-e", script],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr[:300])
        self.assertEqual(json.loads(out.stdout), 1234)

    def test_global_variable(self):
        builder = w.ModuleBuilder("global")
        counter = builder.add_global(w.I32, True, w.i32_const(5))
        body = builder.add_function("main", results=(w.I32,))
        body.emit(w.global_get(counter))
        body.emit(w.i32_const(37))
        body.emit(bytes((w.I32_ADD,)))
        body.emit(w.global_set(counter))
        body.emit(w.global_get(counter))
        builder.export("main", w.KIND_FUNC, builder.func_index(body))
        self.assertEqual(self.run_module(builder.to_bytes()), 42)


class TestDeterminism(unittest.TestCase):
    def test_same_input_same_bytes(self):
        """Requisito duro del bootstrap (ADR 0016).

        Un backend no determinista rompe el build cache y hace imposible
        comparar Stage 0 con Stage 1.
        """

        def build():
            builder = w.ModuleBuilder("d")
            builder.add_memory(1)
            builder.add_data(8, b"hola mundo")
            builder.add_global(w.I32, True, w.i32_const(0))
            builder.import_function("host", "p", (w.I32, w.I32), ())
            body = builder.add_function("main", results=(w.I32,),
                                        locals=[(3, w.I64), (2, w.I32)])
            body.emit(w.i32_const(8))
            body.emit(w.i32_const(11))
            body.emit(w.call(0))
            body.emit(w.i32_const(0))
            return builder.to_bytes()

        first = build()
        for _ in range(50):
            self.assertEqual(build(), first)



class TestFuzzerHarness(unittest.TestCase):
    """El generador del fuzzer se verifica a sí mismo.

    Un generador que produce módulos inválidos por sus propias reglas llena el
    fuzzer de errores que no son del encoder, y además los señala en el archivo
    equivocado. La comprobación de saldo de la pila es lo que convierte al
    fuzzer en una herramienta utilizable.
    """

    def test_generated_functions_are_balanced(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import fuzz_encoder
        except ImportError:
            self.skipTest("tools/fuzz_encoder.py no disponible")

        rng = random.Random(fuzz_encoder.SEED)
        for index in range(150):
            with self.subTest(indice=index):
                try:
                    fuzz_encoder.random_function(rng, index)
                except fuzz_encoder.GeneradorDesbalanceado as error:
                    self.fail(f"el generador produce ramas sin cuadrar: {error}")

    def test_a_hundred_modules_validate(self):
        """Un recorte del fuzzer completo, para que el CI lo vea siempre.

        Los 1000 módulos del fuzzer tardan unos segundos y se lanzan desde
        `make fuzz`; aquí hay una muestra suficiente para detectar una
        regresión en cada commit.
        """
        if not NODE:
            self.skipTest("node no está instalado")
        sys.path.insert(0, str(ROOT / "tools"))
        import fuzz_encoder

        modules = fuzz_encoder.generate(100)
        bad = validate_with_node(modules)
        failing = [index for index, ok in enumerate(bad) if not ok]
        self.assertEqual(failing, [],
                         f"módulos inválidos: {failing[:5]}")
if __name__ == "__main__":
    unittest.main()
