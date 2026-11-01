"""Lexer: fuente -> tokens con span.

Decisiones que viven aquí y en GRAMMAR.md:

  - Palabras clave e identificadores no distinguen mayúsculas (ADR 0012).
    Los identificadores se canonizan a `value.lower()` y se guarda el texto
    original para diagnósticos y documentación.
  - `LEN(s)` devuelve bytes (ADR 0003).
  - `IF cond THEN` seguido de salto de línea abre bloque; en la misma línea, el
    cuerpo es una sentencia (ADR 0006). El lexer marca los saltos de línea como
    tokens y el parser decide; así la decisión queda en un solo sitio.
  - La interpolación `{expr}` se resuelve en el parser, no aquí. El lexer sólo
    entrega los fragmentos de texto con su corrección de escapes, que es donde
    la información existe.
"""

from __future__ import annotations

from dataclasses import dataclass

from .diagnostics import DiagnosticBag, Span

# Palabras clave de Source.
#
# Los nombres de la capa web (H1, P, BUTTON, LINK, META...) NO están aquí, y es
# deliberado: `p`, `text`, `input`, `list`, `div`, `name` y `content` son
# nombres de variable perfectamente normales, y quien viene de BASIC los usa sin
# pensar. Los elementos web se reconocen por contexto, dentro de BODY, que es
# donde no puede haber una ambigüedad (ver GRAMMAR.md §7).
#
# `TEXT` sí es palabra clave, porque `TEXT x, y, "hola", color` es una primitiva
# de dibujo que existe desde el principio.
KEYWORDS = frozenset({
    "DIM", "AS", "ARRAY", "OF", "NEW", "STRUCT", "FUNCTION", "END",
    "RETURN", "IF", "THEN", "ELSE", "WHILE", "WEND", "FOR", "TO", "STEP",
    "NEXT", "GOTO", "GOSUB", "AND", "OR", "NOT", "XOR", "MOD", "DIV",
    "TRUE", "FALSE", "NIL", "USE", "IMPORT", "FROM", "ARENA", "RESET",
    "PRINT", "SCREEN", "OFF", "CLS", "PSET", "LINE", "RECT", "FILLED",
    "CIRCLE", "TRIANGLE", "TEXT", "FLIP", "COLOR",
    "INTEGER", "SINGLE", "DOUBLE", "STRING", "BOOLEAN",
    "VEC2", "VEC3", "VEC4", "MAT3", "MAT4", "VOID",
})

# Nombres de tipos primitivos, para el typechecker.
PRIM_TYPES = frozenset({
    "INTEGER", "SINGLE", "DOUBLE", "STRING", "BOOLEAN",
    "VEC2", "VEC3", "VEC4", "MAT3", "MAT4", "VOID",
})

# Módulos del runtime recognized por USE.
RUNTIME_MODULES = frozenset({
    "SOURCE_CORE", "SOURCE_2D", "SOURCE_3D", "SOURCE_WEB", "SOURCE_SOUND",
    "SOURCE_NET",
})

# Sentencias que sólo existen para detectar si falta su módulo (ADR 0010).
PRIMITIVE_MODULES = {
    "PSET": "SOURCE_2D",
    "LINE": "SOURCE_2D",
    "RECT": "SOURCE_2D",
    "CIRCLE": "SOURCE_2D",
    "TRIANGLE": "SOURCE_2D",
    "FLIP": "SOURCE_2D",
    "SCREEN": "SOURCE_2D",
    "CLS": "SOURCE_2D",
}

# Palabras clave que pueden ser primitivas de sentencia. El contexto decide.
AMBIGUOUS = frozenset({"TEXT", "COLOR", "INPUT", "DIV", "LINE", "END", "LIST"})

_IDENT_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_IDENT_CONT = _IDENT_START | frozenset("0123456789")
_DIGITS = frozenset("0123456789")
_HEX = frozenset("0123456789abcdefABCDEF")
_SPACES = frozenset(" \t\r")

# Tokens sin valor textual propio.
EOF = "eof"
NEWLINE = "newline"
INT = "int"
FLOAT = "float"
STRING_TOK = "string"
IDENT = "ident"
KEYWORD = "keyword"
OP = "op"

# Interpolación: el lexer emite un token por fragmento.
INTERP_START = "interp_start"
INTERP_TEXT = "interp_text"
INTERP_END = "interp_end"


@dataclass(frozen=True)
class Token:
    kind: str
    value: str          # valor canónico: identificadores en minúsculas
    text: str           # texto original tal cual aparece
    span: Span

    def is_op(self, *ops: str) -> bool:
        return self.kind == OP and self.value in ops

    def is_kw(self, *names: str) -> bool:
        return self.kind == KEYWORD and self.value in names

    def __str__(self) -> str:  # pragma: no cover - sólo para depurar
        return f"{self.kind}({self.value!r})@{self.span.start}"


# Orden de operadores: más largo primero, para que "<=" no se lea como "<" "=".
_OPERATORS = (
    "<>", "<=", ">=",
    "+", "-", "*", "/", "\\", "^", "&", "=", "<", ">",
    "(", ")", "[", "]", ",", ":", ".", ";", "{", "}",
)

_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "0": "\0",
    "\\": "\\", '"': '"', "'": "'",
}


class Lexer:
    """Convierte texto fuente en una lista de tokens.

    No lanza excepciones por errores léxicos: los acumula en `diagnostics` y
    sigue, saltándose el trozo problemático hasta un delimitador seguro. Es lo
    que permite reportar tres errores de un vistazo en vez de uno por compilación.
    """

    def __init__(self, source: str, diagnostics: DiagnosticBag):
        self.source = source
        self.diagnostics = diagnostics
        self.pos = 0
        self.tokens: list[Token] = []
        self.string_start = 0

    def tokenize(self) -> list[Token]:
        src = self.source
        n = len(src)
        while self.pos < n:
            ch = src[self.pos]

            if ch == "}":
                # Cierre de interpolación: se emite la llave y se sigue leyendo
                # el texto que queda. No se vacía el buffer aquí porque acaba
                # de vaciarse al abrir la interpolación.
                brace = self.pos
                self._emit(OP, "}", "}", Span(brace, brace + 1))
                self.pos += 1
                # No se vuelve a abrir el flag del texto: si lo que sigue es la
                # comilla de cierre, ese fragmento vacío no aporta nada y sólo
                # haría que el parser viera dos textos contiguos.
                self._string_open = False
                self._scan_string_body()
                continue

            if ch in _SPACES:
                self.pos += 1
                continue

            if ch == "\n":
                start = self.pos
                self.pos += 1
                self._emit(NEWLINE, "\n", src[start:self.pos], Span(start, self.pos))
                continue

            if ch == "'":
                self._skip_comment()
                continue

            if ch in "rR" and self._at_rem_comment():
                self._skip_comment()
                continue

            if ch in _IDENT_START:
                self._lex_word()
                continue

            if ch in _DIGITS:
                self._lex_number()
                continue

            if ch == '"':
                self._lex_string()
                continue

            if ch == "&" and self.pos + 1 < n and src[self.pos + 1] in "HhBbOo":
                self._lex_prefixed_number()
                continue

            matched = None
            for op in _OPERATORS:
                if src.startswith(op, self.pos):
                    matched = op
                    break
            if matched is not None:
                start = self.pos
                self.pos += len(matched)
                self._emit(OP, matched, matched, Span(start, self.pos))
                continue

            self._error_unknown(ch)

        self._emit(EOF, "", "", Span(self.pos, self.pos))
        return self.tokens

    # ------------------------------------------------------------------ utils

    def _emit(self, kind: str, value: str, text: str, span: Span) -> None:
        self.tokens.append(Token(kind, value, text, span))

    def _at_rem_comment(self) -> bool:
        """¿Estamos en un `REM` que es comentario y no un identificador?

        Se exige que sea al principio de la línea y que no le siga un carácter
        de identificador, para que `remolque` siga siendo un identificador.
        """
        word = self.source[self.pos:self.pos + 3].upper()
        if word != "REM":
            return False
        following = self.source[self.pos + 3:self.pos + 4]
        if following and following in _IDENT_CONT:
            return False
        return True

    def _skip_comment(self) -> None:
        nl = self.source.find("\n", self.pos)
        self.pos = len(self.source) if nl < 0 else nl

    def _error_unknown(self, ch: str) -> None:
        start = self.pos
        self.pos += 1
        self.diagnostics.error(
            "E0001", f"carácter inesperado '{ch}'",
            Span(start, self.pos),
            "identificadores y palabras clave empiezan con letra o '_'",
        )

    # ------------------------------------------------------------------ words

    def _lex_word(self) -> None:
        src = self.source
        start = self.pos
        while self.pos < len(src) and src[self.pos] in _IDENT_CONT:
            self.pos += 1
        text = src[start:self.pos]
        upper = text.upper()

        if upper in KEYWORDS:
            self._emit(KEYWORD, upper, text, Span(start, self.pos))
        else:
            # ADR 0012: los identificadores no distinguen mayúsculas.
            self._emit(IDENT, text.lower(), text, Span(start, self.pos))

    # ---------------------------------------------------------------- números

    def _lex_number(self) -> None:
        src = self.source
        start = self.pos
        while self.pos < len(src) and src[self.pos] in _DIGITS:
            self.pos += 1

        is_float = False
        # El punto sólo es decimal si no es el acceso a miembro de un entero.
        if (self.pos + 1 < len(src) and src[self.pos] == "."
                and src[self.pos + 1] in _DIGITS):
            is_float = True
            self.pos += 1
            while self.pos < len(src) and src[self.pos] in _DIGITS:
                self.pos += 1

        if self.pos < len(src) and src[self.pos] in "eE":
            save = self.pos
            self.pos += 1
            if self.pos < len(src) and src[self.pos] in "+-":
                self.pos += 1
            if self.pos < len(src) and src[self.pos] in _DIGITS:
                is_float = True
                while self.pos < len(src) and src[self.pos] in _DIGITS:
                    self.pos += 1
            else:
                # "1e" sin dígitos: el `e` no era exponente.
                self.pos = save

        text = src[start:self.pos]
        if is_float:
            self._emit(FLOAT, text, text, Span(start, self.pos))
        else:
            self._emit(INT, text, text, Span(start, self.pos))

    def _lex_prefixed_number(self) -> None:
        src = self.source
        start = self.pos
        kind = src[self.pos + 1].upper()
        self.pos += 2

        allowed = {"H": _HEX, "B": frozenset("01"), "O": frozenset("01234567")}[kind]
        digits_start = self.pos
        while self.pos < len(src) and src[self.pos] in allowed:
            self.pos += 1

        if self.pos == digits_start:
            self.diagnostics.error(
                "E0002", f"literal {kind} sin dígitos",
                Span(start, self.pos),
                f"escribe al menos un dígito, por ejemplo &{kind}1",
            )
            return

        text = src[start:self.pos]
        self._emit(INT, text, text, Span(start, self.pos))

    # ----------------------------------------------------------------- textos

    def _lex_string(self) -> None:
        """Abre un texto y devuelve el control.

        Se escanea por trozos porque la interpolación (`"hola {nombre}"`) saca
        al lexer de la cadena: dentro de las llaves hay expresiones, y para
        tokenizarlas como tales hay que volver al bucle principal. `in_string`
        guarda el estado para poder reentrar después de la llave de cierre.

        Un texto produce, por tanto, una secuencia de tokens que el parser
        vuelve a ensamblar:
            string  "hola "     op "{"   ...   op "}"   string  "!"
        """
        self.string_start = self.pos
        self.pos += 1  # comilla inicial
        self._string_open = True
        self._scan_string_body()

    def _scan_string_body(self) -> None:
        """Escanea un fragmento de texto.

        Termina al encontrar la comilla de cierre, una llave de interpolación,
        o un error irrecuperable. Al salir por una llave, `in_string` sigue en
        True y el bucle principal reentrará por aquí después de la expresión.
        """
        src = self.source
        buf: list[str] = []
        buf_start = self.pos

        while self.pos < len(src):
            ch = src[self.pos]

            if ch == "\n":
                self.diagnostics.error(
                    "E0003", "texto sin cerrar",
                    Span(self.string_start, self.pos),
                    "falta la comilla de cierre antes del fin de línea",
                )
                self._flush_text(buf, buf_start)
                return

            if ch == '"':
                self.pos += 1
                self._flush_text(buf, buf_start)
                self._string_open = False
                return

            if ch == "\\":
                self._lex_escape(buf, self.string_start)
                continue

            if ch == "{":
                # ADR 0013: `{{` es el escape de una llave literal.
                if self.pos + 1 < len(src) and src[self.pos + 1] == "{":
                    buf.append("{")
                    self.pos += 2
                    continue
                self._flush_text(buf, buf_start)
                brace = self.pos
                self._emit(OP, "{", "{", Span(brace, brace + 1))
                self.pos += 1
                self._string_open = True
                return

            if ch == "}":
                if self.pos + 1 < len(src) and src[self.pos + 1] == "}":
                    buf.append("}")
                    self.pos += 2
                    continue
                # Una llave suelta dentro del texto (`"a}b"`) cierra la
                # interpolación que el parser está esperando.
                if buf:
                    self._flush_text(buf, buf_start)
                brace = self.pos
                self._emit(OP, "}", "}", Span(brace, brace + 1))
                self.pos += 1
                buf_start = self.pos
                continue

            buf.append(ch)
            self.pos += 1

        self.diagnostics.error(
            "E0004", "texto sin cerrar al final del archivo",
            Span(self.string_start, self.pos),
            'falta la comilla de cierre "',
        )
        self._flush_text(buf, buf_start)

    def _flush_text(self, buf: list[str], start: int) -> None:
        text = "".join(buf)
        if not text and not getattr(self, "_string_open", False):
            return
        self._emit(STRING_TOK, text, text, Span(start, self.pos))
        buf.clear()

    def _lex_escape(self, buf: list[str], string_start: int) -> None:
        src = self.source
        start = self.pos
        self.pos += 1
        if self.pos >= len(src):
            self.diagnostics.error(
                "E0005", "escape incompleto al final del archivo",
                Span(start, self.pos),
            )
            return

        ch = src[self.pos]
        if ch == "u":
            self.pos += 1
            if self.pos >= len(src) or src[self.pos] != "{":
                self.diagnostics.error(
                    "E0006", r"escape unicode mal formado, se esperaba \u{...}",
                    Span(start, self.pos),
                    r"escribe \u{1F600} con los dígitos hexadecimales entre "
                    r"llaves: \u{ más el código, y } al final",
                )
                return
            self.pos += 1
            hex_start = self.pos
            while self.pos < len(src) and src[self.pos] in _HEX:
                self.pos += 1
            if self.pos >= len(src) or src[self.pos] != "}" or self.pos == hex_start:
                self.diagnostics.error(
                    "E0006", r"escape unicode mal formado, se esperaba \u{...}",
                    Span(start, min(self.pos + 1, len(src))),
                    r"falta la llave de cierre: \u{1F600} y no \u{1F600",
                )
                return
            digits = src[hex_start:self.pos]
            self.pos += 1
            try:
                buf.append(chr(int(digits, 16)))
            except ValueError:
                self.diagnostics.error(
                    "E0006", f"código unicode inválido '\\u{{{digits}}}'",
                    Span(start, self.pos),
                    f"el máximo es \\u{{10FFFF}}; escribe menos dígitos o uno "
                    f"de un plano que exista",
                )
            return

        replacement = _ESCAPES.get(ch)
        if replacement is None:
            self.diagnostics.error(
                "E0005", f"escape desconocido '\\{ch}'",
                Span(start, self.pos + 1),
                "válidos: \\n \\t \\r \\0 \\\\ \\\" \\' \\u{...}",
            )
            self.pos += 1
            return

        buf.append(replacement)
        self.pos += 1


def tokenize(source: str, diagnostics: DiagnosticBag) -> list[Token]:
    """Punto de entrada. Devuelve los tokens; los errores van en `diagnostics`."""
    return Lexer(source, diagnostics).tokenize()