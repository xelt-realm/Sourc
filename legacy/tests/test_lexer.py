"""Lexer: tokens, números en tres bases, escapes y errores léxicos."""

import unittest

from sourc.diagnostics import DiagnosticBag, Span
from sourc.lexer import (
    EOF, FLOAT, IDENT, INT, KEYWORD, NEWLINE, OP, STRING_TOK, tokenize,
)


def lex(source):
    bag = DiagnosticBag()
    tokens = tokenize(source, bag)
    return tokens, bag


class TestTokens(unittest.TestCase):
    def test_keyword_is_uppercase_canonical(self):
        tokens, bag = lex("dim")
        self.assertEqual(tokens[0].kind, KEYWORD)
        self.assertEqual(tokens[0].value, "DIM")

    def test_identifier_is_lowercased_but_keeps_text(self):
        # ADR 0012: no distinguen mayúsculas, pero el texto original se guarda.
        tokens, _ = lex("miVariable")
        self.assertEqual(tokens[0].kind, IDENT)
        self.assertEqual(tokens[0].value, "mivariable")
        self.assertEqual(tokens[0].text, "miVariable")

    def test_rem_is_a_comment_but_similar_words_are_not(self):
        tokens, _ = lex("REM esto es un comentario")
        self.assertEqual(tokens[-1].kind, "eof")
        tokens, _ = lex("DIM remolque = 1")
        self.assertEqual(tokens[1].kind, IDENT)

    def test_comments_are_skipped(self):
        tokens, bag = lex("' comentario\nREM otro\n42")
        self.assertEqual([t.kind for t in tokens
                          if t.kind not in (NEWLINE, EOF)], [INT])
        self.assertFalse(bag.has_errors())

    def test_operators_longest_first(self):
        tokens, _ = lex("a <= b <> c")
        ops = [t.value for t in tokens if t.kind == OP]
        self.assertEqual(ops, ["<=", "<>"])

    def test_dot_after_integer_is_member_access(self):
        tokens, _ = lex("1.toString")
        self.assertEqual(tokens[0].kind, INT)
        self.assertEqual(tokens[1].value, ".")

    def test_float_requires_digit_after_dot(self):
        tokens, _ = lex("4.2")
        self.assertEqual(tokens[0].kind, FLOAT)
        self.assertEqual(tokens[0].value, "4.2")

    def test_exponent_without_digits_is_not_exponent(self):
        tokens, bag = lex("1e")
        self.assertEqual(tokens[0].kind, INT)
        self.assertEqual(tokens[0].value, "1")

    def test_span_covers_the_token(self):
        tokens, _ = lex("hola")
        self.assertEqual(tokens[0].span, Span(0, 4))


class TestNumbers(unittest.TestCase):
    def test_prefixed_bases(self):
        for source in ("&HFF", "&B1010", "&O777"):
            tokens, bag = lex(source)
            self.assertEqual(tokens[0].kind, INT, source)
            self.assertFalse(bag.has_errors())

    def test_empty_prefixed_number_is_an_error(self):
        _, bag = lex("&H")
        self.assertTrue(bag.has_errors())
        self.assertEqual(bag.errors[0].code, "E0002")


class TestStrings(unittest.TestCase):
    def test_escapes(self):
        tokens, bag = lex(r'"a\nb\tc\\d\"e\u{41}"')
        self.assertEqual(tokens[0].value, "a\nb\tc\\d\"eA")
        self.assertFalse(bag.has_errors())

    def test_interpolation_splits_into_fragments(self):
        tokens, bag = lex('"hola {nombre}"')
        kinds = [t.kind for t in tokens if t.kind != NEWLINE]
        self.assertEqual(kinds[0], STRING_TOK)
        self.assertEqual(tokens[0].value, "hola ")
        self.assertEqual(tokens[1].value, "{")
        self.assertEqual(tokens[2].kind, IDENT)
        self.assertEqual(tokens[3].value, "}")

    def test_doubled_brace_is_a_literal_brace(self):
        tokens, _ = lex('"{{"')
        self.assertEqual(tokens[0].value, "{")

    def test_unterminated_string_reports_position(self):
        _, bag = lex('PRINT "hola')
        self.assertTrue(bag.has_errors())
        self.assertIn("sin cerrar", bag.errors[0].message)

    def test_unknown_escape_lists_valid_ones(self):
        _, bag = lex(r'"\q"')
        self.assertTrue(bag.has_errors())
        self.assertIn("\\n", bag.errors[0].suggestion)


class TestErrors(unittest.TestCase):
    def test_unknown_character(self):
        _, bag = lex("§")
        self.assertEqual(bag.errors[0].code, "E0001")

    def test_errors_do_not_stop_tokenizing(self):
        """Varios errores léxicos en una pasada: es la mitad del valor del lexer."""
        _, bag = lex("§ §")
        self.assertGreaterEqual(len(bag.errors), 2)

    def test_eof_token_always_present(self):
        tokens, _ = lex("")
        self.assertEqual(tokens[-1].kind, "eof")


if __name__ == "__main__":
    unittest.main()