"""Parser: tokens -> AST.

Descendente recursivo con precedencia climbed en las expresiones
(`ARCHITECTURE.md`, sección de gramática).

Decisiones que viven aquí:

  - `IF cond THEN` + salto de línea abre bloque; en la misma línea el cuerpo es
    una sentencia (ADR 0006). Se implementa exactamente con lo que dice el ADR:
    una bandera "saw_newline tras THEN".
  - La variable de `FOR` se declara sola, con ámbito de bucle (ADR 0002).
  - `NEXT` con nombre debe coincidir con el de su `FOR`; si no, error `E0041`.
  - No hay `SUB` (ADR 0011). Hay keywords `SUB` no: no existe, así que aparece
    como identificador y el error sale del typechecker. En realidad aquí
    interceptamos `FUNCTION` sin nombre de retorno para diagnosticar mejor.
  - Los saltos de línea son tokens. El parser los consume explícitamente donde
    importan (cabeceras de bloque) y los tolera donde no.
"""

from __future__ import annotations

from .ast import (
    Arena, ArrayLit, Assign, Binary, BoolLit, Call, ColorStmt, Dim, Field,
    FloatLit, For, FunctionDecl, Gosub, Goto, If, ImportFrom, Index,
    IntLit, Interpolate, Label, Name, NewArray, NilLit, Primitive, Program,
    Return, StringLit, TypeRef, Unary, Use, While,
)
from .diagnostics import CompileError, Diagnostic, DiagnosticBag, Span, suggest
from .lexer import (
    EOF, FLOAT, IDENT, INT, KEYWORD, NEWLINE, OP, STRING_TOK, RUNTIME_MODULES,
    Token, tokenize,
)

# Primitivas con número de argumentos variable o con defaults documentados.
_PRIMITIVE_ARITY = {
    "PRINT": (1, None),
    "SCREEN": (0, 3),
    "CLS": (0, 1),
    "PSET": (2, 3),
    "LINE": (4, 5),
    "RECT": (4, 6),
    "CIRCLE": (3, 4),
    "TRIANGLE": (5, 7),
    "TEXT": (3, 4),
    "COLOR": (1, 1),
    "INPUT": (1, 1),
}

# Nombres conocidos, para sugerencias de error.
_KNOWN_NAMES = [
    "DIM", "PRINT", "IF", "THEN", "ELSE", "WHILE", "WEND", "FOR", "NEXT",
    "FUNCTION", "RETURN", "ARENA", "USE", "IMPORT", "SCREEN", "CLS", "RECT",
    "CIRCLE", "KEY", "KEYDOWN", "FLIP", "RGB", "LEN", "LENU", "CHARCODE",
    "CHR",
    "TRUE", "FALSE", "NIL", "RESET",
]


class Parser:
    def __init__(self, source: str, diagnostics: DiagnosticBag):
        self.source = source
        self.diagnostics = diagnostics
        self.tokens = tokenize(source, diagnostics)
        self.pos = 0
        # Profundidad de anidamiento de bloques, para los límites de error.
        self.depth = 0

    # ------------------------------------------------------------------ utils

    @property
    def current(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, offset: int = 1) -> Token:
        index = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[index]

    def advance(self) -> Token:
        token = self.tokens[self.pos]
        if token.kind != EOF:
            self.pos += 1
        return token

    def check(self, kind: str, value: str | None = None) -> bool:
        token = self.current
        if token.kind != kind:
            return False
        return value is None or token.value == value

    def check_op(self, *ops: str) -> bool:
        return self.current.kind == OP and self.current.value in ops

    def check_kw(self, *names: str) -> bool:
        return self.current.kind == KEYWORD and self.current.value in names

    def accept_op(self, *ops: str) -> Token | None:
        if self.check_op(*ops):
            return self.advance()
        return None

    def accept_kw(self, *names: str) -> Token | None:
        if self.check_kw(*names):
            return self.advance()
        return None

    def expect_op(self, op: str, context: str) -> Token:
        if self.check_op(op):
            return self.advance()
        self.fail(f"se esperaba '{op}' {context}", self.current)

    def expect_kw(self, name: str, context: str) -> Token:
        if self.check_kw(name):
            return self.advance()
        self.fail(f"se esperaba '{name}' {context}", self.current)

    def fail(self, message: str, token: Token, hint: str | None = None) -> None:
        """Registra un error y aborta.

        El parser no intenta recuperarse de errores de estructura: un AST
        incorrecto produce diagnósticos inventados, que son peores que uno solo
        bien situado. Los errores locales (expresión mal formada, argumento
        equivocado) sí se acumulan.
        """
        raise CompileError(Diagnostic("E0010", message, token.span, hint, True))

    def skip_newlines(self) -> None:
        while self.current.kind == NEWLINE:
            self.advance()

    def end_statement(self) -> None:
        """Consume el final de una sentencia: salto de línea o `;`."""
        if self.current.kind == NEWLINE:
            self.advance()
            return
        if self.check_op(";"):
            self.advance()
            self.skip_newlines()
            return
        if self.current.kind == EOF:
            return
        self.fail(
            "final de sentencia inesperado",
            self.current,
            "las sentencias van una por línea, separadas por salto de línea o ';'",
        )

    def span_from(self, start: Token) -> Span:
        prev = self.tokens[max(0, self.pos - 1)]
        return Span(start.span.start, prev.span.end)

    # ---------------------------------------------------------------- programa

    def parse_program(self) -> Program:
        start = self.current
        body: list = []
        self.skip_newlines()
        while self.current.kind != EOF:
            if self.current.kind == NEWLINE:
                self.advance()
                continue
            body.append(self.parse_statement())
        return Program(body=tuple(body), span=self.span_from(start))

    def _is_primitive_assignment(self) -> bool:
        """Tras una primitiva, ¿lo que viene es un `=`?"""
        index = min(self.pos + 1, len(self.tokens) - 1)
        return self.tokens[index].is_op("=")

    def parse_statement(self):
        token = self.current

        if token.kind == KEYWORD:
            handler = _STATEMENT_HANDLERS.get(token.value)
            if handler is not None and self._is_primitive_assignment():
                # `color = 3` es una asignación a una variable llamada `color`,
                # no la sentencia de paleta. Las dos existen y las dos se
                # escriben; el `=` detrás lo dice sin ambigüedad.
                handler = None
            if handler is not None:
                return handler(self)

        # `ident:` es una etiqueta.
        if token.kind == IDENT and self.peek().is_op(":"):
            self.advance()
            self.advance()
            return Label(name=token.value, span=self.span_from(token))

        # Asignación o expresión suelta.
        #
        # El lado izquierdo se parsea como expresión *postfija*, no como
        # expresión completa: si se usara la expresión completa, el `=` lo
        # se tragaría la regla relacional y `x = 1` acabaría siendo una
        # comparación en lugar de una asignación.
        expr = self.parse_postfix()
        if self.check_op("="):
            self.advance()
            value = self.parse_expression()
            self.end_statement()
            return Assign(target=expr, value=value, span=self.span_from(token))
        if self.check_op(","):
            return self.parse_primitive(token, expr)

        self.end_statement()
        return expr

    # -------------------------------------------------------------- sentencia

    def parse_dim(self):
        start = self.advance()  # DIM
        name_token = self.current
        if name_token.kind != IDENT:
            if name_token.kind == KEYWORD:
                # Una palabra clave reservada no puede ser un nombre de
                # variable, igual que en BASIC clásico. El mensaje lo dice
                # explícitamente, porque `DIM color` es un intento razonable
                # y el error tiene que decir la razón.
                #
                # Se informa y **después** se aborta, así que este diagnóstico
                # siempre viene acompañado del `E0010` de "DIM necesita un
                # nombre". No es un defecto: es que el token ya no sirve como
                # nombre y el parseo no puede continuar. Por eso el caso de
                # `tests/casos_de_diagnostico.py` declara ese `E0010` como
                # compañero esperado en vez de fingir que va solo.
                self.diagnostics.error(
                    "E0014", f"'{name_token.text}' es una palabra clave reservada",
                    name_token.span,
                    "las palabras clave no pueden ser nombres de variable; "
                    "prueba con 'tono', 'matiz' u otro nombre",
                )
            self.fail("DIM necesita un nombre de variable", name_token,
                      "por ejemplo DIM x AS INTEGER = 10")
        self.advance()
        declared_name = name_token.value

        type_ref = None
        if self.accept_kw("AS"):
            type_ref = self.parse_type_ref()

        value = None
        if self.accept_op("="):
            value = self.parse_expression()

        self.end_statement()

        if type_ref is None and value is None:
            self.diagnostics.error(
                "E0010", f"'{name_token.text}' declarado sin tipo ni valor",
                name_token.span,
                "escribe DIM x AS INTEGER = 0; Source no pone valores cero "
                "por omisión (ADR 0002)",
            )
        elif type_ref is not None and value is None:
            if not _is_array_type(type_ref):
                self.diagnostics.error(
                    "E0011", f"'{name_token.text}' tiene tipo pero no valor",
                    name_token.span,
                    "los escalares necesitan valor inicial; los ARRAY pueden "
                    "omitirlo: DIM b AS ARRAY OF SINGLE = NEW SINGLE[64]",
                )

        return Dim(
            name=declared_name,
            name_span=name_token.span,
            type_ref=type_ref,
            value=value,
            span=self.span_from(start),
        )

    def parse_type_ref(self) -> TypeRef:
        token = self.current
        if token.kind != KEYWORD:
            self.fail("se esperaba un tipo", token,
                      "tipos: INTEGER SINGLE DOUBLE STRING BOOLEAN VEC3 MAT4 "
                      "ARRAY OF ...")
        if token.value == "ARRAY":
            self.advance()
            self.expect_kw("OF", "después de ARRAY")
            element = self.parse_type_ref()
            return TypeRef(name="ARRAY", element=element, span=token.span)
        if token.value == "STRUCT":
            self.advance()
            if self.current.kind != IDENT:
                self.fail("STRUCT necesita un nombre", self.current)
            self.advance()
            return TypeRef(name="STRUCT", element=None, span=token.span)
        self.advance()
        return TypeRef(name=token.value, element=None, span=token.span)

    def parse_if(self, chained: bool = False):
        """Parsea un IF.

        `chained` marca los nodos de una cadena `ELSE IF`. La diferencia no es
        cosmética: un `ELSE IF` **no** lleva su propio `END IF`. Si cada IF
        anidado consumiera un `END IF`, el IF más interno se llevaría el del
        exterior y la outermost se quedaría sin cerrar. Por eso los nodos de la
        cadena devuelven el control sin consumir el `END`.

        Sin esto, `IF a ... ELSE IF b ... ELSE ... END IF` no compila, y es
        exactamente la forma que escribe la gente.
        """
        start = self.advance()  # IF
        condition = self.parse_expression()
        self.expect_kw("THEN", "después de la condición")

        # ADR 0006: bloque si y sólo si hay salto de línea justo después de THEN.
        one_liner = not self.skip_newlines_after_then()
        then_body: tuple = ()
        else_body: tuple | None = None

        if one_liner:
            then_body = (self.parse_statement(),)
        else:
            then_body = self.parse_block_body(("ELSE", "END"))

            if self.check_kw("ELSE"):
                self.advance()
                if self.check_kw("IF"):
                    # Cadena `ELSE IF`: el nodo nuevo no consume el END IF.
                    else_body = (self.parse_if(chained=True),)
                elif self.current.kind == NEWLINE:
                    self.skip_newlines()
                    else_body = self.parse_block_body(("END",))
                else:
                    else_body = (self.parse_statement(),)
                    self.skip_newlines()

            if not chained:
                self.expect_kw("END", "para cerrar el IF")
                self.expect_kw("IF", "después de END")
                self.end_statement()

        return If(
            condition=condition,
            then_body=then_body,
            else_body=else_body,
            one_liner=one_liner,
            span=self.span_from(start),
        )

    def skip_newlines_after_then(self) -> bool:
        """Consume saltos de línea. Devuelve si había alguno (ADR 0006)."""
        had_newline = False
        while self.current.kind == NEWLINE:
            self.advance()
            had_newline = True
        return had_newline

    def parse_block_body(self, terminators: tuple[str, ...],
                         body: tuple | None = None) -> tuple:
        """Sentencias hasta una palabra clave terminadora o EOF."""
        if body is not None:
            return body
        collected: list = []
        self.depth += 1
        if self.depth > 200:
            self.fail("anidamiento demasiado profundo", self.current,
                      "el límite actual es 200 bloques; refactoriza")
        self.skip_newlines()
        while self.current.kind != EOF:
            if self.current.kind == KEYWORD and self.current.value in terminators:
                break
            if self.current.kind == NEWLINE:
                self.advance()
                continue
            collected.append(self.parse_statement())
        self.depth -= 1
        return tuple(collected)

    def parse_while(self):
        start = self.advance()  # WHILE
        condition = self.parse_expression()
        self.end_statement()
        body = self.parse_block_body(("WEND",))
        self.expect_kw("WEND", "para cerrar el WHILE")
        self.end_statement()
        return While(condition=condition, body=body, span=self.span_from(start))

    def parse_for(self):
        start = self.advance()  # FOR
        var_token = self.current
        if var_token.kind != IDENT:
            self.fail("FOR necesita un nombre de variable", var_token,
                      "la variable del bucle se declara sola, no hace falta DIM")
        self.advance()
        self.expect_op("=", "después de la variable del bucle")
        begin = self.parse_expression()
        self.expect_kw("TO", "después del valor inicial")
        end = self.parse_expression()

        step = None
        if self.accept_kw("STEP"):
            step = self.parse_expression()

        self.end_statement()
        body = self.parse_block_body(("NEXT",))

        # `NEXT` con nombre opcional; debe coincidir con el del FOR (GRAMMAR.md, 3.3).
        self.expect_kw("NEXT", "para cerrar el FOR")
        next_token = None
        if self.current.kind == IDENT:
            next_token = self.advance()
        self.end_statement()

        if next_token is not None and next_token.value != var_token.value:
            self.diagnostics.error(
                "E0041",
                f"'NEXT {next_token.text}' no cierra el FOR de '{var_token.text}'",
                next_token.span,
                f"cambia NEXT {next_token.text} por NEXT {var_token.text}, "
                "o quita el nombre",
            )

        return For(
            var=var_token.value,
            var_span=var_token.span,
            start=begin,
            stop=end,
            step=step,
            body=body,
            span=self.span_from(start),
        )

    def parse_goto(self, keyword: str = "GOTO"):
        start = self.advance()
        target = self.current
        if target.kind != IDENT:
            self.fail(f"{keyword} necesita una etiqueta", target,
                      f"las etiquetas se escriben nombre: antes de la sentencia")
        self.advance()

        self.diagnostics.warn(
            "W0001", f"'{keyword}' es una construcción heredada de BASIC",
            target.span,
            "en Source el control de flujo se expresa con IF, WHILE y FOR; "
            "si portas código clásico, esto es esperable",
        )
        self.end_statement()

        node = Goto(label=target.value, span=self.span_from(start))
        if keyword == "GOSUB":
            return Gosub(label=target.value, span=self.span_from(start))
        return node

    def parse_return(self):
        start = self.advance()  # RETURN
        value = None
        if not (self.current.kind == NEWLINE or self.current.kind == EOF
                or self.check_op(";")):
            value = self.parse_expression()
        self.end_statement()
        return Return(value=value, span=self.span_from(start))

    def parse_arena(self):
        start = self.advance()  # ARENA
        name_token = self.current
        if name_token.kind != IDENT:
            self.fail("ARENA necesita un nombre", name_token,
                      "por ejemplo ARENA frame")
        self.advance()
        self.end_statement()
        body = self.parse_block_body(("END",))
        self.expect_kw("END", "para cerrar el ARENA")
        self.expect_kw("ARENA", "después de END")
        self.end_statement()
        return Arena(name=name_token.value, body=body, span=self.span_from(start))

    def parse_function(self):
        start = self.advance()  # FUNCTION

        name_token = self.current
        if name_token.kind != IDENT:
            self.fail("FUNCTION necesita un nombre", name_token)
        self.advance()

        params: list[tuple[str, TypeRef | None]] = []
        if self.accept_op("("):
            if not self.check_op(")"):
                while True:
                    pname = self.current
                    if pname.kind != IDENT:
                        self.fail("parámetro sin nombre", pname)
                    self.advance()
                    ptype = None
                    if self.accept_kw("AS"):
                        ptype = self.parse_type_ref()
                    params.append((pname.value, ptype))
                    if not self.accept_op(","):
                        break
            self.expect_op(")", "al cerrar la lista de parámetros")

        return_type = None
        if self.accept_kw("AS"):
            return_type = self.parse_type_ref()

        self.end_statement()
        body = self.parse_block_body(("END",))
        self.expect_kw("END", "para cerrar la FUNCTION")
        self.expect_kw("FUNCTION", "después de END")
        self.end_statement()

        return FunctionDecl(
            name=name_token.value,
            params=tuple(params),
            return_type=return_type,
            body=body,
            span=self.span_from(start),
        )

    def parse_use(self):
        start = self.advance()  # USE
        token = self.current
        if token.kind != IDENT and token.kind != KEYWORD:
            self.fail("USE necesita el nombre de un módulo", token,
                      "por ejemplo USE SOURCE_2D")
        self.advance()
        self.end_statement()

        module = token.value.upper()
        if module not in RUNTIME_MODULES:
            self.diagnostics.error(
                "E0060", f"módulo de runtime desconocido '{token.text}'",
                token.span,
                "módulos: " + ", ".join(sorted(RUNTIME_MODULES)),
            )
        return Use(module=module, span=self.span_from(start))

    def parse_import(self):
        start = self.advance()  # IMPORT
        self.expect_kw("FROM", "después de IMPORT")
        path = self.current
        if path.kind != STRING_TOK:
            self.fail("IMPORT FROM necesita una ruta entre comillas", path,
                      'por ejemplo IMPORT FROM "juego/nivel.src"')
        self.advance()

        alias = None
        if self.accept_kw("AS"):
            alias_token = self.current
            if alias_token.kind != IDENT:
                self.fail("AS necesita un nombre", alias_token)
            self.advance()
            alias = alias_token.value

        self.end_statement()
        return ImportFrom(
            path=StringLit(value=path.value, span=path.span),
            alias=alias,
            span=self.span_from(start),
        )

    # -------------------------------------------------------------- primitivas

    def parse_primitive_stmt(self):
        """Entrada de sentencia para las primitivas con nombre de palabra clave."""
        start = self.advance()
        return self.parse_primitive(start)

    def parse_primitive(self, start: Token, first_arg=None):
        """Primitivas con argumentos posicionales.

        Se llama desde `parse_statement` para dos casos:
          - `RECT 0, 0, 10, 10, color` (identificador que ya se leyó como nombre)
          - `PRINT ...` / `SCREEN ...` (palabra clave)
        """
        name = start.value.upper()
        if name == "COLOR":
            # ADR 0005: `COLOR 5` es la sentencia de paleta.
            index = first_arg if first_arg is not None else self.parse_expression()
            self.end_statement()
            return ColorStmt(index=index, span=self.span_from(start))

        if name in ("KEY", "KEYDOWN", "RGB"):
            return self.parse_call_expr(first_arg, start)

        # `SCREEN OFF` no es una llamada con argumentos sino una forma aparte.
        if name == "SCREEN" and self.check_kw("OFF"):
            self.advance()
            self.end_statement()
            return Primitive(name="SCREEN_OFF", args=(), span=self.span_from(start))

        args: list = []
        if first_arg is not None:
            args.append(first_arg)
        if args or self.starts_expression():
            args.append(self.parse_expression())
            while self.accept_op(","):
                args.append(self.parse_expression())

        # `PRINT "a" ; "b"` imprime en la misma línea (ver `inline`).
        inline = False
        while self.check_op(";"):
            self.advance()
            args.append(self.parse_expression())
            inline = True

        self.end_statement()
        self._check_primitive_arity(name, args, start)
        return Primitive(name=name, args=tuple(args), inline=inline,
                         span=self.span_from(start))

    def _check_primitive_arity(self, name: str, args: list, start: Token) -> None:
        spec = _PRIMITIVE_ARITY.get(name)
        if spec is None:
            return
        minimum, maximum = spec
        if len(args) < minimum:
            self.diagnostics.error(
                "E0062", f"'{name}' necesita al menos {minimum} argumento(s)",
                start.span,
                f"recibidos {len(args)}",
            )
        elif maximum is not None and len(args) > maximum:
            self.diagnostics.error(
                "E0062", f"'{name}' acepta como máximo {maximum} argumento(s)",
                start.span,
                f"recibidos {len(args)}",
            )

    # -------------------------------------------------------------- expresiones

    def starts_expression(self) -> bool:
        """¿El token actual puede empezar una expresión?

        Lo necesitan las primitivas con argumentos opcionales, como `CLS` y
        `FLIP`, donde no hay paréntesis que marquen el fin de la lista.
        """
        token = self.current
        if token.kind in (INT, FLOAT, STRING_TOK, IDENT):
            return True
        if token.kind == OP and token.value in ("(", "[", "-", "+", "{"):
            return True
        # `NOT` cuenta: `PRINT NOT a` es un argumento, no una sentencia nueva.
        if token.kind == KEYWORD and token.value in ("TRUE", "FALSE", "NIL",
                                                     "NOT"):
            return True
        return False

    def parse_expression(self):
        return self.parse_or()

    def _binary_level(self, sub, *ops):
        left = sub()
        while self.current.kind == OP and self.current.value in ops:
            op = self.advance().value
            right = sub()
            left = Binary(op=op, left=left, right=right,
                          span=left.span.merge(right.span))
        return left

    def parse_or(self):
        left = self.parse_and()
        while self.check_kw("OR"):
            self.advance()
            right = self.parse_and()
            left = Binary(op="OR", left=left, right=right,
                          span=left.span.merge(right.span))
        return left

    def parse_and(self):
        left = self.parse_xor()
        while self.check_kw("AND"):
            self.advance()
            right = self.parse_xor()
            left = Binary(op="AND", left=left, right=right,
                          span=left.span.merge(right.span))
        return left

    def parse_xor(self):
        left = self.parse_relational()
        while self.check_kw("XOR"):
            self.advance()
            right = self.parse_relational()
            left = Binary(op="XOR", left=left, right=right,
                          span=left.span.merge(right.span))
        return left

    def parse_relational(self):
        left = self.parse_additive()
        if self.current.kind == OP and self.current.value in (
            "=", "<>", "<", "<=", ">", ">="
        ):
            op = self.advance().value
            right = self.parse_additive()
            return Binary(op=op, left=left, right=right,
                          span=left.span.merge(right.span))
        return left

    def parse_additive(self):
        left = self.parse_multiplicative()
        while self.current.kind == OP and self.current.value in ("+", "-", "&"):
            op = self.advance().value
            right = self.parse_multiplicative()
            left = Binary(op=op, left=left, right=right,
                          span=left.span.merge(right.span))
        return left

    def parse_multiplicative(self):
        left = self.parse_unary()
        while (self.current.kind == OP and self.current.value in ("*", "/", "\\")
               or self.check_kw("MOD") or self.check_kw("DIV")):
            op = self.advance().value
            right = self.parse_unary()
            left = Binary(op=op, left=left, right=right,
                          span=left.span.merge(right.span))
        return left

    def parse_unary(self):
        if self.check_op("-"):
            start = self.advance()
            operand = self.parse_unary()
            return Unary(op="-", operand=operand, span=start.span.merge(operand.span))
        if self.check_kw("NOT"):
            start = self.advance()
            operand = self.parse_unary()
            return Unary(op="NOT", operand=operand, span=start.span.merge(operand.span))
        if self.check_op("+"):
            self.advance()
            return self.parse_unary()
        return self.parse_power()

    def parse_power(self):
        base = self.parse_postfix()
        if self.check_op("^"):
            self.advance()
            exponent = self.parse_unary()
            return Binary(op="^", left=base, right=exponent,
                          span=base.span.merge(exponent.span))
        return base

    def parse_postfix(self):
        expr = self.parse_primary()
        while True:
            if self.check_op("["):
                start = self.advance()
                index = self.parse_expression()
                self.expect_op("]", "cerrando el índice")
                expr = Index(target=expr, index=index, span=expr.span.merge(index.span))
            elif self.check_op("."):
                self.advance()
                name_token = self.current
                if name_token.kind not in (IDENT, KEYWORD):
                    self.fail("se esperaba un nombre de campo", name_token)
                self.advance()
                expr = Field(target=expr, name=name_token.value,
                             span=expr.span.merge(name_token.span))
            elif self.check_op("("):
                self.advance()
                args: list = []
                if not self.check_op(")"):
                    while True:
                        args.append(self.parse_expression())
                        if not self.accept_op(","):
                            break
                close = self.expect_op(")", "cerrando la llamada")
                expr = Call(callee=expr, args=tuple(args),
                            span=expr.span.merge(close.span))
            else:
                return expr

    def parse_call_expr(self, first_arg, start: Token):
        """`KEY("w")`, `KEYDOWN("w")`, `RGB(r, g, b)`.

        Son identificadores, no palabras clave: llegan por la vía normal de
        `parse_postfix` como `Call`.
        """
        if first_arg is None:
            args: list = []
        else:
            args = [first_arg]
        while self.accept_op(","):
            args.append(self.parse_expression())
        close = self.expect_op(")", "cerrando la llamada")
        return Call(callee=Name(value=start.value, span=start.span),
                    args=tuple(args), span=start.span.merge(close.span))

    def parse_primary(self):
        token = self.current

        if token.kind == INT:
            self.advance()
            return IntLit(value=_parse_int(token), span=token.span,
                         text=token.value)

        if token.kind == FLOAT:
            self.advance()
            return FloatLit(value=float(token.value), span=token.span,
                              text=token.value)

        if token.kind == STRING_TOK:
            return self.parse_string_literal()

        if token.kind == OP and token.value == "(":
            self.advance()
            inner = self.parse_expression()
            self.expect_op(")", "cerrando el paréntesis")
            return inner

        if token.kind == OP and token.value == "[":
            return self.parse_array_literal()

        if token.kind == IDENT:
            self.advance()
            upper = token.value.upper()
            if upper == "NEW":
                return self.parse_new_array()
            if upper == "TRUE":
                return BoolLit(value=True, span=token.span)
            if upper == "FALSE":
                return BoolLit(value=False, span=token.span)
            if upper == "NIL":
                return NilLit(span=token.span)
            return Name(value=token.value, span=token.span)

        if token.kind == KEYWORD:
            if token.value == "NEW":
                return self.parse_new_array()
            if token.value in ("TRUE", "FALSE"):
                self.advance()
                return BoolLit(value=(token.value == "TRUE"), span=token.span)
            if token.value == "NIL":
                self.advance()
                return NilLit(span=token.span)

        self._fail_primary(token)

    def _fail_primary(self, token: Token):
        if token.kind == KEYWORD:
            self.fail(
                f"'{token.text}' no empieza una expresión",
                token,
                _KEYWORD_HINTS.get(token.value),
            )
        if token.kind == EOF:
            self.fail("el archivo termina antes de tiempo", token,
                      "falta una expresión")
        self.fail(f"expresión inesperada '{token.text}'", token,
                  _hint_for_unknown(token))

    def parse_string_literal(self):
        """Ensambla los fragmentos de texto e interpolaciones del lexer."""
        start = self.current
        pieces: list = []

        while self.current.kind == STRING_TOK:
            if pieces:
                # Texto contiguo sin operador entre medias: hay un `&` o algo
                # raro. Se acepta la concatenación implícita con aviso.
                self.diagnostics.warn(
                    "W0008", "dos textos contiguos",
                    self.current.span,
                    "concatena con '&': PRINT \"a\" & \"b\"",
                )
            # El token se coge **antes** de avanzar. La versión anterior
            # escribía `self.advance().value` y luego leía `self.current.span`,
            # así que el `StringLit` se quedaba con el span del token *siguiente*
            # --en `PRINT "texto"` era el salto de línea-- y cualquier
            # diagnóstico que apuntara a un literal de texto subrayaba el sitio
            # equivocado. Lo encontró el volcado del AST: ni un test de
            # Stage 0 miraba la forma del árbol.
            token = self.advance()
            pieces.append(StringLit(value=token.value, span=token.span))
            if self.check_op("{"):
                pieces.append(self.parse_interpolation())
            elif self.current.kind == STRING_TOK:
                continue
            else:
                break

        result = pieces[0]
        for piece in pieces[1:]:
            result = Binary(op="&", left=result, right=piece,
                            span=result.span.merge(piece.span))
        return result

    def parse_interpolation(self):
        start = self.advance()  # {
        expr = self.parse_expression()
        close = self.expect_op("}", "cerrando la interpolación")
        return Interpolate(value=expr, span=start.span.merge(close.span))

    def parse_array_literal(self):
        start = self.advance()  # [
        elements: list = []
        self.skip_newlines()
        if not self.check_op("]"):
            while True:
                elements.append(self.parse_expression())
                self.skip_newlines()
                if not self.accept_op(","):
                    break
                self.skip_newlines()
        close = self.expect_op("]", "cerrando el literal de array")
        return ArrayLit(elements=tuple(elements), span=start.span.merge(close.span))

    def parse_new_array(self):
        start = self.current  # NEW
        self.advance()
        type_token = self.current
        if type_token.kind != KEYWORD:
            self.fail("NEW necesita un tipo", type_token,
                      "por ejemplo NEW SINGLE[1024]")
        self.advance()
        self.expect_op("[", "abriendo el tamaño")
        count = self.parse_expression()
        close = self.expect_op("]", "cerrando el tamaño")
        return NewArray(
            type_name=type_token.value,
            count=count,
            type_span=type_token.span,
            span=start.span.merge(close.span),
        )


_KEYWORD_HINTS = {
    "DIM": "una sentencia empieza por DIM",
    "THEN": "THEN va después de la condición de un IF",
    "TO": "TO va entre los dos valores de un FOR",
    "NEXT": "NEXT cierra un FOR",
    "WEND": "WEND cierra un WHILE",
    "ELSE": "ELSE va dentro de un IF de bloque",
    "AS": "AS va entre el nombre y el tipo",
    "OF": "OF va después de ARRAY",
}

_STATEMENT_HANDLERS = {
    "DIM": Parser.parse_dim,
    "IF": Parser.parse_if,
    "WHILE": Parser.parse_while,
    "FOR": Parser.parse_for,
    # `GOSUB` necesita su palabra clave y `GOTO` también, porque `parse_goto`
    # construye un nodo distinto según cuál sea: `GOSUB x` producía un `ast.Goto`
    # porque el despacho hacía `handler(self)` y se quedaba con el valor por
    # defecto. Estaba enmascarado --los dos dan E0043-- y dos puertas lo
    # documentaban como rareza, pero un AST que dice `Goto` donde el fuente dice
    # GOSUB es un AST incorrecto, y en cuanto el salto baje a IR `GOSUB` haría
    # un salto incondicional. Las lambdas son el arreglo más pequeño que deja
    # el despacho como estaba.
    "GOTO": lambda self: self.parse_goto("GOTO"),
    "GOSUB": lambda self: self.parse_goto("GOSUB"),
    "RETURN": Parser.parse_return,
    "ARENA": Parser.parse_arena,
    "FUNCTION": Parser.parse_function,
    "USE": Parser.parse_use,
    "IMPORT": Parser.parse_import,
    "PRINT": Parser.parse_primitive_stmt,
    "SCREEN": Parser.parse_primitive_stmt,
    "CLS": Parser.parse_primitive_stmt,
    "PSET": Parser.parse_primitive_stmt,
    "LINE": Parser.parse_primitive_stmt,
    "RECT": Parser.parse_primitive_stmt,
    "CIRCLE": Parser.parse_primitive_stmt,
    "TRIANGLE": Parser.parse_primitive_stmt,
    "TEXT": Parser.parse_primitive_stmt,
    "COLOR": Parser.parse_primitive_stmt,
    "INPUT": Parser.parse_primitive_stmt,
    "FLIP": Parser.parse_primitive_stmt,
}


# Qué tipos de nodo produce el parser, y por qué está escrito.
#
# El tripwire mide la cobertura por **operación del IR**, que es el contrato del
# backend. Este es el mismo contrato del otro lado: qué formas de sentencia
# admite la gramática. Sin esta lista, la puerta de cobertura por feature
# (`tests/test_cobertura_features.py`) tendría que adivinarlo, y una lista
# adivinada que se queda corta deja passers sin mirar.
NODES_DE_SENTENCIA = (
    "Dim", "Assign", "If", "While", "For", "FunctionDecl", "Return",
    "Goto", "Gosub", "Label", "Arena", "Use", "ImportFrom", "Primitive",
    "Index", "Interpolate", "Field",
)


def _hint_for_unknown(token: Token) -> str:
    name = suggest(token.value if token.kind == IDENT else token.text,
                   _KNOWN_NAMES)
    if name:
        return f"¿quiso decir '{name}'?"
    return None


def _parse_int(token: Token):
    """Interpreta un literal entero, incluidos `&H`, `&B` y `&O`."""
    text = token.value
    try:
        if len(text) > 1 and text[0] == "&":
            base = {"H": 16, "B": 2, "O": 8}[text[1].upper()]
            return int(text[2:], base)
        return int(text, 10)
    except ValueError:  # pragma: no cover - el lexer ya lo validó
        return 0


def _is_array_type(type_ref: TypeRef) -> bool:
    return type_ref.name == "ARRAY"


def parse(source: str, diagnostics: DiagnosticBag) -> Program:
    """Punto de entrada del parser. Lanza `CompileError` en error estructural."""
    return Parser(source, diagnostics).parse_program()