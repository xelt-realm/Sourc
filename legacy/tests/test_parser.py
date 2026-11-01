"""Parser: estructura, precedencia de operadores y las dos formas de IF."""

import unittest

from sourc import ast
from sourc.diagnostics import CompileError, DiagnosticBag
from sourc.parser import parse


def parsed(source):
    bag = DiagnosticBag()
    program = parse(source, bag)
    return program, bag


class TestStatements(unittest.TestCase):
    def test_dim_with_inference(self):
        program, bag = parsed('DIM x = 10')
        self.assertFalse(bag.has_errors())
        self.assertIsInstance(program.body[0], ast.Dim)
        self.assertIsNone(program.body[0].type_ref)

    def test_dim_without_type_or_value_is_an_error(self):
        # ADR 0002: no hay valores cero por omisión.
        _, bag = parsed("DIM x")
        self.assertTrue(bag.has_errors())
        self.assertEqual(bag.errors[0].code, "E0010")

    def test_scalar_dim_needs_value(self):
        _, bag = parsed("DIM x AS INTEGER")
        self.assertTrue(bag.has_errors())
        self.assertEqual(bag.errors[0].code, "E0011")

    def test_array_dim_may_omit_value(self):
        _, bag = parsed("DIM b AS ARRAY OF SINGLE")
        self.assertFalse(bag.has_errors())

    def test_function(self):
        program, _ = parsed("FUNCTION doble(x AS INTEGER) AS INTEGER\n"
                            "    RETURN x * 2\nEND FUNCTION")
        func = program.body[0]
        self.assertIsInstance(func, ast.FunctionDecl)
        self.assertEqual(len(func.params), 1)

    def test_for_declares_its_own_variable(self):
        # ADR 0002: excepción única a "DIM obligatorio".
        program, bag = parsed("FOR i = 0 TO 10\nNEXT")
        self.assertFalse(bag.has_errors())
        self.assertIsInstance(program.body[0], ast.For)

    def test_next_with_wrong_name_is_an_error(self):
        _, bag = parsed("FOR i = 0 TO 10\nNEXT j")
        self.assertTrue(bag.has_errors())
        self.assertEqual(bag.errors[0].code, "E0041")

    def test_arena(self):
        program, _ = parsed("ARENA frame\nDIM b AS ARRAY OF SINGLE = "
                            "NEW SINGLE[8]\nEND ARENA")
        self.assertIsInstance(program.body[0], ast.Arena)

    def test_goto_warns_but_compiles(self):
        # W0001: GOTO existe por compatibilidad, y avisa siempre.
        program, bag = parsed("salto:\nGOTO salto")
        self.assertFalse(bag.has_errors())
        self.assertTrue(bag.has_warnings())
        self.assertEqual(bag.warnings[0].code, "W0001")

    def test_use_unknown_module(self):
        _, bag = parsed("USE SOURCE_TRESD")
        self.assertEqual(bag.errors[0].code, "E0060")


class TestIfForms(unittest.TestCase):
    def test_one_liner(self):
        # ADR 0006: sin salto de línea después de THEN, el cuerpo es una
        # sentencia.
        program, _ = parsed("IF a > 1 THEN PRINT \"si\"")
        node = program.body[0]
        self.assertTrue(node.one_liner)
        self.assertEqual(len(node.then_body), 1)

    def test_block(self):
        program, _ = parsed("IF a > 1 THEN\n    PRINT \"si\"\nEND IF")
        node = program.body[0]
        self.assertFalse(node.one_liner)

    def test_else_if_chain(self):
        """`ELSE IF` es una cadena: sólo el IF exterior lleva END IF."""
        program, bag = parsed(
            "IF a THEN\n    PRINT 1\nELSE IF b THEN\n    PRINT 2\n"
            "ELSE\n    PRINT 3\nEND IF")
        outer = program.body[0]
        self.assertFalse(bag.has_errors())
        self.assertEqual(len(outer.then_body), 1)
        chained = outer.else_body[0]
        self.assertIsInstance(chained, ast.If)
        self.assertEqual(len(chained.then_body), 1)
        self.assertEqual(len(chained.else_body), 1)

    def test_if_without_end_if_is_a_fatal_error(self):
        with self.assertRaises(CompileError):
            parsed("IF a THEN\n    PRINT 1\n")

    def test_else_on_one_liner_is_an_error(self):
        # El error más probable al portar código: mezclar las dos formas.
        with self.assertRaises(CompileError):
            parsed("IF a THEN PRINT 1 ELSE PRINT 2")


class TestPrecedence(unittest.TestCase):
    def shape(self, source):
        program, bag = parsed(source)
        self.assertFalse(bag.has_errors())
        return program.body[0].value

    def test_multiply_before_add(self):
        node = self.shape("DIM x = 1 + 2 * 3")
        self.assertEqual(node.op, "+")
        self.assertEqual(node.right.op, "*")

    def test_power_binds_tighter_than_multiply(self):
        node = self.shape("DIM x = 2 * 3 ^ 2")
        self.assertEqual(node.op, "*")
        self.assertEqual(node.right.op, "^")

    def test_unary_minus(self):
        node = self.shape("DIM x = -3 + 4")
        self.assertEqual(node.op, "+")

    def test_comparison_binds_looser_than_arithmetic(self):
        node = self.shape("DIM x = 1 + 2 = 3")
        self.assertEqual(node.op, "=")
        self.assertEqual(node.left.op, "+")

    def test_and_binds_tighter_than_or(self):
        node = self.shape("DIM x = TRUE AND FALSE OR TRUE")
        self.assertEqual(node.op, "OR")

    def test_concatenation(self):
        node = self.shape('DIM x = "a" & "b"')
        self.assertEqual(node.op, "&")

    def test_index_and_call_chain(self):
        node = self.shape("DIM x = LEN(mapa[0])")
        self.assertIsInstance(node, ast.Call)
        self.assertIsInstance(node.args[0], ast.Index)

    def test_assignment_is_not_a_comparison(self):
        # El lado izquierdo se parsea como postfijo; si no, el `=` lo se
        # tragaría la regla relacional y esto sería una comparación.
        program, _ = parsed("DIM x AS INTEGER = 0\nx = 5")
        self.assertIsInstance(program.body[1], ast.Assign)


class TestErrors(unittest.TestCase):
    def test_missing_end_statement(self):
        with self.assertRaises(CompileError):
            parsed("DIM x = 1 DIM y = 2")

    def test_deep_nesting_is_bounded(self):
        source = "IF TRUE THEN\n" * 300 + "PRINT 1\n"
        with self.assertRaises(CompileError):
            parsed(source + "END IF\n" * 300)

    def test_keyword_in_expression_position(self):
        with self.assertRaises(CompileError):
            parsed("DIM x = THEN")

    def test_unterminated_array_literal(self):
        with self.assertRaises(CompileError):
            parsed("DIM x = [1, 2")


if __name__ == "__main__":
    unittest.main()