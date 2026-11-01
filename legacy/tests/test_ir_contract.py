"""El IR no puede contener nada específico de Python.

Regla que sostiene el auto-hospedaje (ADR 0016): el compilador en Python es
andamiaje temporal y se sustituye por uno escrito en Source en la Fase 8. Para
que ese cambio sea mecánico, nada en el contrato puede depender de que el
productor sea Python.

Qué significa en la práctica:

  - Un operando del IR sólo puede ser `Value`, entero, flotante, texto o lista
    de esos. Ni clases, ni tuplas heterogéneas, ni `repr` de nada.
  - La serialización canónica es texto escrito a mano, no `str()` de objetos.
  - Los goldens son texto diffable, para que Stage 0 y Stage 1 puedan
    compararse sin comparar binarios.

La razón de ser concreta: `array.alloc` llevaba un `Type` de Python como
operando. Se>printaba como `INTEGER` por casualidad de `Type.__str__`, así que
el bug era invisible en los goldens y habría estallado en el backend WASM al
necesitar el tamaño del elemento.
"""

import unittest
from pathlib import Path

from sourc import sir
from sourc.diagnostics import DiagnosticBag
from sourc.parser import parse
from sourc.sir import Module, Value
from sourc.types import check

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted((ROOT / "tests" / "corpus").glob("*.src")) + \
    sorted((ROOT / "examples").glob("*.src"))

# Los tipos que el IR admite en un operando. Cualquier otra cosa es una fuga.
PERMITTED = (Value, int, float, str, list)


def build(path: Path) -> Module:
    source = path.read_text(encoding="utf-8")
    bag = DiagnosticBag()
    program = parse(source, bag)
    if bag.has_errors():
        raise AssertionError(f"{path.name} no compila")
    typing = check(program, bag)
    if bag.has_errors():
        raise AssertionError(f"{path.name} no typechequea")
    return sir.build(program, typing, path.stem, "1.0")


class TestIRIsLanguageNeutral(unittest.TestCase):
    def test_operands_are_only_portable_values(self):
        for path in SOURCES:
            with self.subTest(program=path.name):
                module = build(path)
                for func in module.funcs:
                    for instr in func.instrs:
                        for index, arg in enumerate(instr.args):
                            # `bool` es subclase de `int` en Python, así que
                            # `isinstance(True, int)` es True y no hay que
                            # excluirlo aparte.
                            self.assertIsInstance(
                                arg, PERMITTED,
                                f"{path.name}:{func.name} instrucción "
                                f"'{instr.op}', operando {index}: {arg!r} es "
                                f"{type(arg).__name__}, que no existe en Source")

    def test_serialized_output_has_no_python_reprs(self):
        """`str()` de un objeto Python se imprime como `Type(kind='INTEGER')`.

        Es el modo de fallo silencioso: el golden se ve "normal" hasta que
        alguien lo lee con otros ojos.
        """
        for path in SOURCES:
            with self.subTest(program=path.name):
                text = sir.serialize(build(path))
                for marker in ("Type(", "Span(", "kind=", "dataclass",
                               "object at 0x", "<"):
                    self.assertNotIn(
                        marker, text,
                        f"{path.name}: el IR serializado contiene '{marker}', "
                        "que es una representación interna de Python")

    def test_goldens_are_plain_text(self):
        # Los de IR también: son goldens, se comparan en el bootstrap y un byte
        # raro en cualquiera de los dos estorba igual.
        for golden in (list((ROOT / "tests" / "golden" / "out").glob("*.txt"))
                       + list((ROOT / "tests" / "golden" / "ir").glob("*.sir"))):
            with self.subTest(golden=golden.name):
                text = golden.read_text(encoding="utf-8")
                # Los goldens son salidas de programa: texto UTF-8 y nada más.
                text.encode("utf-8")
                self.assertNotIn("\x00", text)


class TestLayoutIsSingleSource(unittest.TestCase):
    """Los anchos se calculan en un sitio, no en tres."""

    def test_layout_covers_every_prim_type(self):
        from sourc import types

        for name in types._PRIM_BY_NAME:
            with self.subTest(tipo=name):
                layout = types.layout_of(name)
                if name == "VOID":
                    # VOID no ocupa memoria; se comprueba aparte para que el
                    # caso siga siendo explícito y no una excepción silenciosa.
                    self.assertEqual(layout.size, 0)
                    self.assertEqual(layout.words, 0)
                    continue
                self.assertGreater(layout.size, 0)
                self.assertGreaterEqual(layout.align, 1)
                self.assertGreaterEqual(layout.words, 1)

    def test_widths_match_the_documented_table(self):
        """ANCHITUD.md §"Layout de memoria" dice cuántas palabras ocupa cada tipo.

        Si estas cifras cambian, el documento cambia con ellas.
        """
        from sourc import types

        self.assertEqual(types.layout_of("INTEGER").words, 1)
        self.assertEqual(types.layout_of("SINGLE").words, 1)
        self.assertEqual(types.layout_of("DOUBLE").words, 1)
        self.assertEqual(types.layout_of("BOOLEAN").words, 1)
        self.assertEqual(types.layout_of("COLOR").words, 1)
        self.assertEqual(types.layout_of("STRING").words, 2)
        # ARRAY son cuatro: puntero, longitud, capacidad y el tipo del
        # elemento. La cuarta palabra es lo que permite al host imprimir un
        # array de arrays sin adivinar de qué es cada elemento.
        self.assertEqual(types.layout_of("ARRAY").words, 4)

    def test_every_code_round_trips(self):
        from sourc import types

        for name in types._PRIM_BY_NAME:
            code = types.type_code(name)
            self.assertIsInstance(code, int)
            self.assertEqual(types.layout_by_code(code).kind, name)

    def test_array_alloc_carries_a_code_not_a_type(self):
        """El caso que originó este fichero."""
        for path in SOURCES:
            with self.subTest(program=path.name):
                for func in build(path).funcs:
                    for instr in func.instrs:
                        if instr.op != "array.alloc":
                            continue
                        self.assertIsInstance(
                            instr.args[1], int,
                            "array.alloc debe llevar el código entero del "
                            f"tipo del elemento; encontré {instr.args[1]!r}")


class TestBooleanHasOneRepresentation(unittest.TestCase):
    """Un BOOLEAN es i32. Siempre.

    Sin esta regla, `DIM b AS BOOLEAN = TRUE` metía un i64 en un slot declarado
    BOOLEAN y el backend no podía elegir el tipo del local: o generaba un módulo
    inválido, o leía 4 bytes donde había 8.

    La lista de ops que producen i32 es la lista de ops que el backend tiene
    que bajar a un valor de 32 bits. Es el contrato, escrito al revés.
    """

    I32_PRODUCERS = {
        # literales y conversión
        "const.bool", "to.bool",
        # lógica y comparación
        "eq", "and.bool", "or.bool", "xor.bool", "not.bool", "select",
        # `f32.eq` y `f64.eq` son productores de i32 aunque el opcode sea de
        # flotante: en WASM toda comparación devuelve un entero de 32 bits, y
        # por eso su destino BOOLEAN tiene que ser i32 y no f64. Es lo mismo que
        # `lt.f64`, que ya estaba, y por eso van en la misma línea y no aparte.
        "eq.f32", "eq.f64",
        "lt.i64", "le.i64", "gt.i64", "ge.i64",
        "lt.f32", "le.f32", "gt.f32", "ge.f32",
        "lt.f64", "le.f64", "gt.f64", "ge.f64",
        # textos
        "str.eq", "str.eq_byte", "str.contains",
        # reenvío: la anchura la impone la declaración del slot, que ya es
        # BOOLEAN. No es un productor i32 en sentido estricto, pero el backend
        # lo trata igual, y por eso está en la lista.
        "load.slot",
        # llamadas: el i32 no lo produce la instrucción sino la firma. El
        # destino de `call` y de `call.native` es un BOOLEAN si la función o la
        # nativa lo declaran así, y la anchura viene de HOST_SIGNATURES. Estaban
        # fuera de esta comprobación porque `result_of` no los miraba.
        "call", "call.native",
    }

    def test_boolean_values_come_from_i32_producers(self):
        for path in SOURCES:
            with self.subTest(program=path.name):
                for func in build(path).funcs:
                    for instr in func.instrs:
                        value = sir.result_of(instr)
                        if value is None or instr.type is None:
                            continue
                        if instr.type.kind != "BOOLEAN":
                            continue
                        self.assertIn(
                            instr.op, self.I32_PRODUCERS,
                            f"{path.name}:{func.name}: '{instr.op}' produce un "
                            f"valor BOOLEAN pero no está en la lista de "
                            "productores de i32; el backend no podría elegir "
                            "el tipo del local")

    def test_print_bool_takes_an_i32(self):
        module = build(ROOT / "tests" / "corpus" / "09_boolenos.src")
        ops = {i.op for f in module.funcs for i in f.instrs}
        self.assertIn("host.print_bool", ops)
        self.assertNotIn("to.bool_bool", ops)

    def test_call_native_returning_boolean_is_declared_i32(self):
        """`KEY` y `KEYDOWN` devuelven i32 en la ABI, no i64."""
        from sourc import types

        self.assertEqual(types.NATIVE_FUNCTIONS["key"][2].kind, "BOOLEAN")
        from sourc import types as t
        self.assertEqual(t.layout_of(types.NATIVE_FUNCTIONS["key"][2]).size, 4)


if __name__ == "__main__":
    unittest.main()