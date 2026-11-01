"""Sistema de tipos e inferencia.

Reglas que este módulo aplica (todas en GRAMMAR.md, todas con ADR):

  - No hay conversiones implícitas entre variables (ARCHITECTURE.md §3.4).
    Lo único que se ensancha sin pedirlo es un literal entero hacia SINGLE o
    DOUBLE, porque es una decisión de lectura, no una conversión de datos.
  - Un literal entero es INTEGER; uno con parte decimal es DOUBLE. `2.0` es
    DOUBLE, no SINGLE.
  - Las condiciones de IF y WHILE deben ser BOOLEAN. `IF x THEN` es error con
    sugerencia `<> 0`: no hay verdad implícita.
  - `+` entre dos STRING es error. La concatenación es `&` (ADR 0013).
  - Los identificadores no declarados son error, con sugerencia (ADR 0002).

Anotación: este módulo no muta el AST (los nodos son inmutables, y queremos
poder cachear el IR por hash del fuente). En su lugar guarda una tabla
`expr_types` indexada por `id(nodo)`, válida mientras viva el AST. Es la parte
más pythonista del compilador; en el puerto a Source será un índice de
instrucción, que es lo natural allí.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ast
from .diagnostics import DiagnosticBag, Span, suggest
from .lexer import RUNTIME_MODULES

# --------------------------------------------------------------- tipos base


@dataclass(frozen=True)
class Type:
    kind: str                  # "INTEGER" | "ARRAY" | ...
    element: "Type | None" = None   # ARRAY OF T
    name: str = ""             # nombre declarado, para mensajes

    def __str__(self) -> str:
        if self.kind == "ARRAY":
            return f"ARRAY OF {self.element}"
        return self.kind

    @property
    def is_numeric(self) -> bool:
        return self.kind in ("INTEGER", "SINGLE", "DOUBLE", "COLOR")

    @property
    def is_integer(self) -> bool:
        return self.kind == "INTEGER"


VOID = Type("VOID")
BOOL = Type("BOOLEAN")
INT = Type("INTEGER")
SINGLE = Type("SINGLE")
DOUBLE = Type("DOUBLE")
STRING = Type("STRING")
COLOR = Type("COLOR")
NIL_TYPE = Type("PTR")

_PRIM_BY_NAME = {
    "VOID": VOID,
    "INTEGER": INT,
    "SINGLE": SINGLE,
    "DOUBLE": DOUBLE,
    "STRING": STRING,
    "BOOLEAN": BOOL,
    "COLOR": COLOR,
    "VEC2": Type("VEC2"),
    "VEC3": Type("VEC3"),
    "VEC4": Type("VEC4"),
    "MAT3": Type("MAT3"),
    "MAT4": Type("MAT4"),
}

_ZERO_VALUE = {
    "INTEGER": 0,
    "SINGLE": 0.0,
    "DOUBLE": 0.0,
    "STRING": "",
    "BOOLEAN": False,
    "COLOR": 0,
    "VEC2": Type("VEC2"),
    "VEC3": Type("VEC3"),
    "VEC4": Type("VEC4"),
    "MAT3": Type("MAT3"),
    "MAT4": Type("MAT4"),
}

# ----------------------------------------------------------------- layout
#
# Fuente única de la disposición en memoria de cada tipo. El backend WASM, el
# futuro backend JS y el allocator la consultan; nadie calcula anchos por su
# cuenta.
#
# `size` y `align` son bytes. `code` es el número entero que viaja en el IR: el
# IR lleva números, nunca objetos de tipo. Ese detalle no es cosmético: en la
# Fase 8 el compilador estará escrito en Source, donde no hay clases, y el
# `array.alloc` tenía un `Type` de Python como operando (ver
# test_ir_is_language_neutral).
#
# `words` es cuántas palabras de la pila ocupa un valor de ese tipo: 1 para un
# escalar, 2 para un texto (ptr, len) y 3 para un array (ptr, len, cap).


@dataclass(frozen=True)
class Layout:
    kind: str
    size: int
    align: int
    code: int
    words: int


T_BOOL, T_COLOR, T_INT, T_F32, T_F64, T_STR, T_ARR = range(7)
T_VEC2, T_VEC3, T_VEC4, T_MAT3, T_MAT4 = range(7, 12)
T_PTR, T_VOID = 12, 13

_LAYOUTS: dict[str, Layout] = {
    "BOOLEAN": Layout("BOOLEAN", 4, 4, T_BOOL, 1),
    "COLOR":   Layout("COLOR", 4, 4, T_COLOR, 1),
    "INTEGER": Layout("INTEGER", 8, 8, T_INT, 1),
    "SINGLE":  Layout("SINGLE", 4, 4, T_F32, 1),
    "DOUBLE":  Layout("DOUBLE", 8, 8, T_F64, 1),
    "STRING":  Layout("STRING", 8, 4, T_STR, 2),
    # La cuarta palabra es el tipo del elemento. Sin ella, el host que
    # imprime un array de arrays no sabe qué hay dentro de cada
    # elemento, y el tipo no se puede adivinar de los datos.
    "ARRAY":   Layout("ARRAY", 16, 4, T_ARR, 4),
    "VEC2":    Layout("VEC2", 8, 4, T_VEC2, 2),
    "VEC3":    Layout("VEC3", 12, 4, T_VEC3, 3),
    "VEC4":    Layout("VEC4", 16, 4, T_VEC4, 4),
    "MAT3":    Layout("MAT3", 36, 4, T_MAT3, 9),
    "MAT4":    Layout("MAT4", 64, 4, T_MAT4, 16),
    # PTR y VOID tienen código propio. Compartir código con otro tipo haría
    # que `layout_by_code(type_code(x)).kind != x`, que es exactamente el tipo
    # de bug que un round-trip tiene que detectar.
    "PTR":     Layout("PTR", 4, 4, T_PTR, 1),
    "VOID":    Layout("VOID", 0, 1, T_VOID, 0),
}

_LAYOUT_BY_CODE: dict[int, Layout] = {
    layout.code: layout for layout in _LAYOUTS.values()
}


def layout_of(type_: "Type | str") -> Layout:
    """Disposición en memoria de un tipo.

    Acepta el `Type` o su nombre, para que el llamante no tenga que saber
    cuál de los dos tiene a mano.
    """
    kind = type_ if isinstance(type_, str) else type_.kind
    layout = _LAYOUTS.get(kind)
    if layout is None:
        raise KeyError(f"tipo sin layout definido: {kind}")
    return layout


def type_code(type_: "Type | str") -> int:
    """El número entero que viaja dentro del IR."""
    return layout_of(type_).code


def layout_by_code(code: int) -> Layout:
    return _LAYOUT_BY_CODE[code]


# Orden de promotion numérica: INTEGER < SINGLE < DOUBLE.
_PROMOTION = {"INTEGER": 0, "SINGLE": 1, "DOUBLE": 2}


# Funciones nativas que el typechecker conoce, con su aridad y tipo de retorno.
# `PUNT` = aridad variable. Se amplía en cada fase; la lista es la superficie
# pública del runtime, así que cambiarla es un cambio de versión (ADR 0010).
NATIVE_FUNCTIONS = {
    # (mínimo, máximo, tipo de retorno o None = como el argumento, módulo)
    "len":      (1, 1, INT,     None),
    "lenu":     (1, 1, INT,     None),
    "charcode": (2, 2, INT,     None),
    # ADR 0021: el inverso de `CHARCODE`, y la condición mínima para que
    # un programa en Source pueda producir un binario.
    "chr":      (1, 1, STRING,  None),
    "str":      (1, 1, STRING,  None),
    "val":      (1, 1, DOUBLE,  None),
    "left":     (2, 2, STRING,  None),
    "right":    (2, 2, STRING,  None),
    "mid":      (3, 3, STRING,  None),
    "concat":   (2, 2, STRING,  None),
    "compare":  (2, 2, INT,     None),
    "contains": (2, 2, BOOL,    None),
    "indexof":  (2, 2, INT,     None),
    "key":      (1, 1, BOOL,    "SOURCE_2D"),
    "keydown":  (1, 1, BOOL,    "SOURCE_2D"),
    # RGB es del runtime base: COLOR es un tipo del núcleo, no del dibujante.
    "rgb":      (3, 4, COLOR,   None),
    "sin":      (1, 1, DOUBLE,  None),
    "cos":      (1, 1, DOUBLE,  None),
    "sqrt":     (1, 1, DOUBLE,  None),
    "floor":    (1, 1, DOUBLE,  None),
    "abs":      (1, 1, None,    None),
    "min":      (2, 2, None,    None),
    "max":      (2, 2, None,    None),
    "clamp":    (3, 3, None,    None),
}

# Primitivas de dibujo: (min_args, max_args, módulo requerido).
_PRIMITIVE_MODULES = {
    "SCREEN": "SOURCE_2D",
    "SCREEN_OFF": "SOURCE_2D",
    "CLS": "SOURCE_2D",
    "PSET": "SOURCE_2D",
    "LINE": "SOURCE_2D",
    "RECT": "SOURCE_2D",
    "CIRCLE": "SOURCE_2D",
    "TRIANGLE": "SOURCE_2D",
    "TEXT": "SOURCE_2D",
    "FLIP": "SOURCE_2D",
}

KNOWN_KEYS = ["KEY", "KEYDOWN", "LEN", "LENU", "CHARCODE", "CHR", "RGB",
              "CONCAT",
              "STR", "VAL", "LEFT", "RIGHT", "MID", "CONTAINS", "INDEXOF",
              "FLOOR", "SQRT", "SIN", "COS", "ABS", "MIN", "MAX", "CLAMP"]


# --------------------------------------------------------------- ámbitos


@dataclass
class Scope:
    symbols: dict[str, ast.Node] = field(default_factory=dict)
    parent: "Scope | None" = None

    def lookup(self, name: str) -> ast.Node | None:
        scope: Scope | None = self
        while scope is not None:
            if name in scope.symbols:
                return scope.symbols[name]
            scope = scope.parent
        return None

    def declare(self, name: str, node: ast.Node) -> None:
        self.symbols[name] = node


@dataclass
class FunctionInfo:
    name: str
    params: list[tuple[str, Type]]
    return_type: Type
    node: ast.FunctionDecl


@dataclass
class Typing:
    """Resultado del typechecker, que el constructor de SIR consulta."""

    expr_types: dict[int, Type] = field(default_factory=dict)
    # Tipo de cada **declaración**, con clave `id()` del nodo que la hace.
    #
    # Antes era `dict[str, Type]`, con el nombre como clave, y eso mezclaba dos
    # variables que se llaman igual en sitios distintos: `DIM r AS DOUBLE` en
    # una función y `DIM r AS INTEGER` en otra se pisaban, y la segunda en
    # escribirse ganaba. El typechecker no lo notaba porque la cadena de `Scope`
    # sí resuelve bien y cada expresión tiene su tipo en `expr_types`; lo que
    # quedaba mal era la tabla que consulta el SIR al bajar, que veía `INTEGER`
    # donde el fuente decía `DOUBLE` y emitía un `conv.i64` que nadie pedía.
    #
    # Con la clave por nodo no hay dos declaraciones que puedan colisionar,
    # porque dos nodos son dos objetos.
    decl_types: dict[int, Type] = field(default_factory=dict)
    # Tipo de cada **parámetro**, con clave `(id(FunctionDecl), nombre)`.
    #
    # Van aparte porque los parámetros de una función comparten nodo: todos se
    # declaran en el mismo `FunctionDecl`, así que por `id()` del nodo no se
    # pueden distinguir. Meterlos en `decl_types` los mezclaba entre funciones y
    # además los pisaba con cualquier variable local del mismo nombre.
    param_types: dict[tuple[int, str], Type] = field(default_factory=dict)
    functions: dict[str, FunctionInfo] = field(default_factory=dict)
    runtime_modules: list[str] = field(default_factory=list)
    string_literals: list[str] = field(default_factory=list)

    def of(self, node: ast.Node) -> Type:
        return self.expr_types.get(id(node), VOID)


# --------------------------------------------------------------- typechecker


class TypeChecker:
    def __init__(self, diagnostics: DiagnosticBag):
        self.diagnostics = diagnostics
        self.result = Typing()
        self.modules: set[str] = set()
        self.functions: dict[str, FunctionInfo] = {}
        self.scope = Scope()
        self.loop_depth = 0
        self.in_function: list[str] = []
        # `id()` de las declaraciones de nivel de programa. Una FUNCTION no
        # puede usarlas todavía: ver `TestGlobalesEnFunciones`.
        #
        # Se guardan los **nodos** y no los nombres a propósito: un `DIM local`
        # que se llama igual que un global debe poder usarse, y con una lista de
        # nombres el error saltaba igual y detrás de la razón de verdad.
        self.globales: set[int] = set()

    # ------------------------------------------------------------------ scopes

    def push(self) -> Scope:
        self.scope = Scope(parent=self.scope)
        return self.scope

    def pop(self) -> None:
        if self.scope.parent is not None:
            self.scope = self.scope.parent

    # --------------------------------------------------------------- programa

    def check_program(self, program: ast.Program) -> Typing:
        # Los módulos se recogen primero: el uso de una primitiva sin `USE`
        # depende de saber si alguien más del programa lo declaro.
        for statement in program.body:
            if isinstance(statement, ast.Use):
                self.modules.add(statement.module)

        # Dos pasadas: primero las firmas de las FUNCTION, luego los cuerpos.
        # Así una función puede llamar a otra declarada más abajo, que es lo
        # que hace falta para escribir el runtime en Source (ADR 0015).
        for statement in program.body:
            if isinstance(statement, ast.FunctionDecl):
                self.declare_function(statement)

        for statement in program.body:
            self.check_statement(statement)

        self.result.runtime_modules = sorted(self.modules)
        self.result.functions = dict(sorted(self.functions.items()))
        return self.result

    def declare_function(self, decl: ast.FunctionDecl) -> None:
        if decl.name in self.functions:
            self.diagnostics.error(
                "E0054", f"FUNCTION '{decl.name}' ya declarada",
                decl.span,
                "renombra una de las dos",
            )
            return

        params: list[tuple[str, Type]] = []
        for index, (pname, type_ref) in enumerate(decl.params):
            if type_ref is None:
                self.diagnostics.error(
                    "E0055", f"parámetro '{pname}' sin tipo",
                    decl.span,
                    "los parámetros necesitan tipo explícito: "
                    f"({pname} AS INTEGER)",
                )
                params.append((pname, INT))
                continue
            params.append((pname, self.resolve_type(type_ref, default=INT)))

        return_type = VOID
        if decl.return_type is not None:
            return_type = self.resolve_type(decl.return_type, default=VOID)

        info = FunctionInfo(decl.name, params, return_type, decl)
        self.functions[decl.name] = info
        self.result.functions[decl.name] = info

    def check_statement(self, statement: ast.Node) -> None:
        if isinstance(statement, ast.Use):
            return

        if isinstance(statement, ast.ImportFrom):
            return

        if isinstance(statement, ast.FunctionDecl):
            self.check_function_body(statement)
            return

        if isinstance(statement, ast.Dim):
            self.check_dim(statement)
            return

        if isinstance(statement, ast.Assign):
            self.check_assign(statement)
            return

        if isinstance(statement, ast.If):
            condition = self.check_expr(statement.condition)
            self.require_boolean(condition, statement.condition, "IF")
            self.push()
            for inner in statement.then_body:
                self.check_statement(inner)
            self.pop()
            if statement.else_body:
                self.push()
                for inner in statement.else_body:
                    self.check_statement(inner)
                self.pop()
            return

        if isinstance(statement, ast.While):
            condition = self.check_expr(statement.condition)
            self.require_boolean(condition, statement.condition, "WHILE")
            self.loop_depth += 1
            self.require_flip(statement, statement.body, inside_loop=self.loop_depth > 1)
            self.push()
            for inner in statement.body:
                self.check_statement(inner)
            self.pop()
            self.loop_depth -= 1
            return

        if isinstance(statement, ast.For):
            self.check_for(statement)
            return

        if isinstance(statement, ast.Arena):
            self.push()
            for inner in statement.body:
                self.check_statement(inner)
            self.pop()
            return

        if isinstance(statement, ast.Return):
            self.check_return(statement)
            return

        if isinstance(statement, (ast.Goto, ast.Gosub, ast.Label)):
            return

        if isinstance(statement, ast.Call):
            # Llamada a una FUNCTION sin usar su resultado: es una sentencia.
            if self.result.of(statement).kind == "VOID":
                self.check_call(statement, lvalue=False)
                return
            self.diagnostics.error(
                "E0015", "el resultado de esta llamada no se usa",
                statement.span,
                "si el resultado no importa, la FUNCTION debería declarar "
                "que no devuelve nada",
            )
            return

        if isinstance(statement, ast.Primitive):
            self.check_primitive(statement)
            return

        if isinstance(statement, ast.ColorStmt):
            self.require_numeric(self.check_expr(statement.index), statement.index)
            return

        self.diagnostics.error(
            "E0012", f"sentencia no reconocida: {type(statement).__name__}",
            statement.span,
        )

    # ------------------------------------------------------------------- DIM

    def check_dim(self, statement: ast.Dim) -> None:
        declared_type: Type | None = None
        if statement.type_ref is not None:
            declared_type = self.resolve_type(statement.type_ref)

        value_type: Type | None = None
        if statement.value is not None:
            value_type = self.check_expr(statement.value)
            self.register_string_literals(statement.value)

        final_type = self.reconcile_dim(statement, declared_type, value_type)
        self.result.decl_types[id(statement)] = final_type
        if not self.in_function:
            self.globales.add(id(statement))
        self.scope.declare(statement.name, statement)

        if final_type.kind == "ARRAY" and statement.value is None:
            return  # ARRAY sin valor: tamaño cero o len de NEW, ya comprobado

    def reconcile_dim(self, statement: ast.Dim,
                      declared: Type | None, value: Type | None) -> Type:
        if declared is not None and value is not None:
            if self.assignable(value, declared, statement.value):
                return declared
            self.report_mismatch(statement.name, value, declared, statement)
            return declared

        if declared is not None:
            return declared

        if value is not None:
            return value

        # El parser ya avisó; aquí sólo hay que devolver algo utilizable.
        return INT

    # -------------------------------------------------------------- asignación

    def check_assign(self, statement: ast.Assign) -> None:
        # `check_expr` ya avisa de un destino no declarado; volver a comprobarlo
        # aquí duplicaría el mismo error dos veces en la salida.
        target_type = self.check_expr(statement.target, lvalue=True)
        value_type = self.check_expr(statement.value)

        if not self.assignable(value_type, target_type, statement.value):
            self.report_mismatch(self._target_name(statement.target),
                                 value_type, target_type, statement)

    def _target_name(self, target: ast.Node) -> str:
        if isinstance(target, ast.Name):
            return target.value
        return "expresión"

    # ------------------------------------------------------------------- FOR

    def check_for(self, statement: ast.For) -> None:
        start_type = self.check_expr(statement.start)
        stop_type = self.check_expr(statement.stop)
        step_type = INT if statement.step is None else self.check_expr(statement.step)

        self.require_numeric(start_type, statement.start)
        self.require_numeric(stop_type, statement.stop)
        if statement.step is not None:
            self.require_numeric(step_type, statement.step)

        # La variable del bucle se declara sola, con ámbito de bucle (ADR 0002).
        is_integer = (start_type.is_integer and stop_type.is_integer
                      and step_type.is_integer)
        loop_type = INT if is_integer else self.promote(start_type, stop_type)
        self.result.decl_types[id(statement)] = loop_type
        self.scope.declare(statement.var, statement)

        self.loop_depth += 1
        self.require_flip(statement, statement.body, inside_loop=self.loop_depth > 1)
        self.push()
        for inner in statement.body:
            self.check_statement(inner)
        self.pop()
        self.loop_depth -= 1

    def require_flip(self, statement: ast.Node, body: tuple,
                     inside_loop: bool) -> None:
        """Avisa de un bucle sin `FLIP` (GRAMMAR.md, 5.3).

        Sin FLIP el bucle gira al 100% de la CPU sin dibujar nada. Es el error
        más fácil de cometer al portar de BASIC, donde no hacía falta presentar
        nada.

        Se comprueban dos cosas, y las dos importan:

        1. Sólo el bucle exterior. Un `FOR` que dibuja 15x16 celdas dentro de
           un `WHILE` no presenta: el `FLIP` del `WHILE` ya lo hace.
        2. Sólo si el programa usa `SOURCE_2D`. Un programa de cálculo con
           bucles no es un juego y no debe quejarse de no presentar nada. Con
           el aviso sin esta condición, `04_controles` recibía tres avisos
           falsos, y un aviso que aparece en programas correctos enseña a
           ignorar los avisos.
        """
        if inside_loop or "SOURCE_2D" not in self.modules:
            return
        if _contains_flip(body):
            return
        self.diagnostics.warn(
            "W0004", "bucle sin FLIP",
            statement.span,
            "en Source hay que presentar el frame a mano: añade FLIP al final "
            "del bucle, si no el programa no dibuja nada y quema la CPU",
        )

    # -------------------------------------------------------------- funciones

    def check_function_body(self, decl: ast.FunctionDecl) -> None:
        info = self.functions.get(decl.name)
        if info is None:
            return

        self.push()
        for (pname, ptype) in info.params:
            self.result.param_types[(id(decl), pname)] = ptype
            self.scope.declare(pname, decl)
        self.in_function.append(decl.name)
        for inner in decl.body:
            self.check_statement(inner)
        self.in_function.pop()
        self.pop()

    def check_return(self, statement: ast.Return) -> None:
        if not self.in_function:
            self.diagnostics.error(
                "E0056", "RETURN fuera de una FUNCTION",
                statement.span,
                "pon el RETURN dentro de un FUNCTION",
            )
            return

        info = self.functions.get(self.in_function[-1])
        value_type = VOID if statement.value is None else self.check_expr(statement.value)

        if info is None:
            return
        if info.return_type.kind == "VOID":
            if statement.value is not None:
                self.diagnostics.error(
                    "E0053", f"RETURN con valor en una FUNCTION sin tipo",
                    statement.span,
                    "declara el tipo: FUNCTION "
                    f"{info.name}(...) AS INTEGER",
                )
            return
        if statement.value is None:
            self.diagnostics.warn(
                "W0009", f"RETURN sin valor en FUNCTION que declara "
                         f"{info.return_type}",
                statement.span,
                f"se devolverá {_zero_literal(info.return_type)}",
            )
            return
        if not self.assignable(value_type, info.return_type):
            self.report_mismatch("RETURN", value_type, info.return_type, statement)

    # ------------------------------------------------------------- primitivas

    def check_primitive(self, statement: ast.Primitive) -> None:
        module = _PRIMITIVE_MODULES.get(statement.name)
        if module and module not in self.modules:
            self.diagnostics.error(
                "E0060", f"falta 'USE {module}' para usar {statement.name}",
                statement.span,
                f"añade USE {module} al principio del programa",
            )

        if statement.name == "INPUT":
            self.check_input(statement)
            return

        for arg in statement.args:
            self.check_expr(arg)
            self.register_string_literals(arg)

        if statement.name == "FLIP" and self.loop_depth == 0:
            self.diagnostics.error(
                "E0051", "FLIP fuera de un bucle",
                statement.span,
                "FLIP presenta un frame; tiene sentido dentro de un bucle "
                "de juego",
            )

        if statement.name == "PRINT":
            for arg in statement.args:
                if not self._is_printable(self.result.of(arg)):
                    self.diagnostics.error(
                        "E0063", "PRINT no acepta este tipo",
                        arg.span,
                        "PRINT acepta números, textos y booleanos",
                    )

    def _is_printable(self, type_: Type) -> bool:
        return type_.is_numeric or type_.kind in ("STRING", "BOOLEAN", "ARRAY")

    def check_input(self, statement: ast.Primitive) -> None:
        if len(statement.args) != 1:
            return
        target = statement.args[0]
        if not isinstance(target, ast.Name):
            self.diagnostics.error(
                "E0064", "INPUT necesita el nombre de una variable",
                target.span,
                "DIM nombre AS STRING   y luego   INPUT nombre",
            )
            return
        if self.scope.lookup(target.value) is None:
            self.report_undeclared(target)
            return
        declared = self.result.decl_types.get(target.value)
        if declared is None or declared.kind not in ("STRING", "INTEGER"):
            self.diagnostics.error(
                "E0065", f"INPUT sólo funciona con STRING o INTEGER, "
                         f"no con {declared}",
                target.span,
                "declárala antes: DIM respuesta AS STRING",
            )
            return
        # El destino tiene que existir ya; lo registramos como usada.
        self.result.expr_types[id(target)] = declared

    # ----------------------------------------------------------- expresiones

    def check_expr(self, node: ast.Node, lvalue: bool = False) -> Type:
        result = self._check_expr(node, lvalue)
        self.result.expr_types[id(node)] = result
        return result

    def _check_expr(self, node: ast.Node, lvalue: bool = False) -> Type:
        if isinstance(node, ast.IntLit):
            return INT

        if isinstance(node, ast.FloatLit):
            return DOUBLE

        if isinstance(node, ast.StringLit):
            self.result.string_literals.append(node.value)
            return STRING

        if isinstance(node, ast.BoolLit):
            return BOOL

        if isinstance(node, ast.NilLit):
            return NIL_TYPE

        if isinstance(node, ast.Name):
            declaracion = self.scope.lookup(node.value)
            if declaracion is None:
                self.report_undeclared(node)
                return INT
            # El tipo se busca por el nodo que **declaró** la variable, no por
            # su nombre: la cadena de `Scope` resuelve el nombre y esa
            # declaración es un nodo concreto, con su clave propia en
            # `decl_types`. Pedirlo por nombre devolvía el tipo de otra variable
            # que se llamara igual.
            #
            # Un parámetro es el caso aparte: su nodo es el `FunctionDecl`, que
            # comparten todos los parámetros de la función, así que su tipo
            # está en `param_types` con el nombre como segunda parte de la clave.
            if self.in_function and id(declaracion) in self.globales:
                # Los globales viven en la memoria lineal del módulo y cada
                # función tiene sus propios locales en WASM, así que compartirlos
                # no es un problema de codegen: es que **no se puede**, con esta
                # representación. Sin este error, el typechecker aceptaba, el
                # módulo daba 0 y el intérprete daba el valor del global.
                # Tres respuestas y ninguna documentada; el que escribe el
                # programa no tenía forma de saber cuál recibiría.
                self.diagnostics.error(
                    "E0067",
                    f"una FUNCTION no puede usar el global '{node.value}'",
                    node.span,
                    "pasa el valor como parámetro, o baja el DIM dentro de la "
                    "FUNCTION. Compartir globales entre funciones necesita que "
                    "vivan en la memoria lineal, que es trabajo de la Fase 4 "
                    "(ver ROADMAP-FASES.md)",
                )
                return INT
            if isinstance(declaracion, ast.FunctionDecl):
                return self.result.param_types.get(
                    (id(declaracion), node.value), INT)
            return self.result.decl_types.get(id(declaracion), INT)

        if isinstance(node, ast.Unary):
            operand = self.check_expr(node.operand)
            if node.op == "NOT":
                self.require_boolean(operand, node.operand, "NOT")
                return BOOL
            self.require_numeric(operand, node.operand)
            return operand

        if isinstance(node, ast.Binary):
            return self.check_binary(node)

        if isinstance(node, ast.Index):
            return self.check_index(node, lvalue)

        if isinstance(node, ast.Field):
            self.check_expr(node.target)
            self.diagnostics.error(
                "E0025", "acceso a campo todavía no implementado",
                node.span,
                "los structs llegan en la Fase 4",
            )
            return INT

        if isinstance(node, ast.Call):
            return self.check_call(node, lvalue)

        if isinstance(node, ast.ArrayLit):
            if lvalue:
                self.diagnostics.error(
                    "E0026", "no se puede asignar a un literal de array",
                    node.span,
                    "un literal crea un valor nuevo y no se puede modificar; "
                    "si querías cambiar un elemento, indexa una variable: "
                    "DIM a AS ARRAY OF INTEGER = [1, 2]   y luego   a[0] = 9",
                )
            element = INT
            if node.elements:
                first = self.check_expr(node.elements[0])
                element = first
                for other in node.elements[1:]:
                    other_type = self.check_expr(other)
                    if not self.assignable(other_type, element):
                        self.report_mismatch("elemento del array", other_type,
                                             element, other)
            return Type("ARRAY", element=element)

        if isinstance(node, ast.Interpolate):
            inner = self.check_expr(node.value)
            if not (inner.is_numeric or inner.kind in ("STRING", "BOOLEAN")):
                self.diagnostics.error(
                    "E0020", f"no se puede interpolar un valor de tipo {inner}",
                    node.span,
                    "dentro de un texto sólo caben números, textos y "
                    "booleanos",
                )
            return STRING

        if isinstance(node, ast.NewArray):
            element = self.resolve_type(ast.TypeRef(name=node.type_name,
                                                   span=node.type_span))
            count = self.check_expr(node.count)
            self.require_numeric(count, node.count)
            return Type("ARRAY", element=element)

        self.diagnostics.error(
            "E0013", f"expresión no reconocida: {type(node).__name__}",
            node.span,
        )
        return INT

    def check_binary(self, node: ast.Binary) -> Type:
        left = self.check_expr(node.left)
        right = self.check_expr(node.right)

        if node.op in ("AND", "OR", "XOR"):
            self.require_boolean(left, node.left, node.op)
            self.require_boolean(right, node.right, node.op)
            return BOOL

        if node.op == "&":
            if left.kind != "STRING" or right.kind != "STRING":
                offender = node.left if left.kind != "STRING" else node.right
                offender_type = left if left.kind != "STRING" else right
                self.diagnostics.error(
                    "E0021", f"'&' necesita STRING, no {offender_type}",
                    offender.span,
                    "usa '+' para números y '&' para concatenar",
                )
            return STRING

        if node.op in ("=", "<>", "<", "<=", ">", ">="):
            self.check_comparable(node, left, right)
            return BOOL

        if node.op in ("+", "-", "*", "/") and "STRING" in (left.kind, right.kind):
            offender = node.left if left.kind == "STRING" else node.right
            self.diagnostics.error(
                "E0021", f"'{node.op}' no funciona con STRING",
                offender.span,
                "la concatenación es '&': pon comillas sólo al lado derecho, "
                "por ejemplo \"total: \" & texto",
            )
            return STRING if node.op == "+" else INT

        self.require_numeric(left, node.left)
        self.require_numeric(right, node.right)
        if node.op == "/":
            # `/` es división verdadera y siempre produce DOUBLE, como en BASIC y
            # como hace el intérprete. `7 / 2` da 3.5, no 3: la división entera
            # la hacen `\` y `DIV`.
            #
            # Antes `/` heredaba el tipo de los operandos, y el compilador y el
            # intérprete discrepaban: el IR pedía INTEGER y el intérprete
            # devolvía 3.5. La paridad lo delató, no el typechecker.
            return DOUBLE
        return self.promote(left, right)

    def check_comparable(self, node: ast.Binary, left: Type, right: Type) -> None:
        if left.is_numeric and right.is_numeric:
            return
        if left.kind == "STRING" and right.kind == "STRING":
            return
        if left.kind == "BOOLEAN" and right.kind == "BOOLEAN":
            return
        if right.kind == "PTR" or left.kind == "PTR":
            return
        # `{expr}` de interpolación puede haber metido un entero donde se
        # esperaba un texto: es el error más común al escribir el ejemplo.
        if (left.kind == "STRING") != (right.kind == "STRING"):
            offender = node.left if left.kind != "STRING" else node.right
            offender_type = left if left.kind != "STRING" else right
            self.diagnostics.error(
                "E0027", f"no se puede comparar STRING con {offender_type}",
                node.span,
                f"convierte con STR({self._target_name(offender)}) o quita las "
                "comillas del otro lado",
            )
            return
        self.diagnostics.error(
            "E0028", f"no se pueden comparar {left} con {right}",
            node.span,
            "los operadores de comparación son para números, booleanos y "
            "textos; para comparar cualquier otra cosa, compárala con un "
            "número o conviértela antes",
        )

    def check_index(self, node: ast.Index, lvalue: bool) -> Type:
        target = self.check_expr(node.target)
        index_type = self.check_expr(node.index)
        self.require_integer(index_type, node.index, "un índice")

        if target.kind == "ARRAY":
            return target.element or INT
        if target.kind == "STRING":
            # ADR 0003: indexar un texto devuelve un carácter de un byte.
            # ADR 0004: si se compara con un literal, el codegen lo baja a
            # comparación de bytes y no asigna nada.
            return STRING
        self.diagnostics.error(
            "E0029", f"no se puede indexar un valor de tipo {target}",
            node.target.span,
            "sólo ARRAY OF T y STRING se pueden indexar",
        )
        return INT

    def check_call(self, node: ast.Call, lvalue: bool) -> Type:
        if not isinstance(node.callee, ast.Name):
            self.check_expr(node.callee)
            self.diagnostics.error(
                "E0030", "no se puede llamar a una expresión",
                node.callee.span,
                "sólo se pueden llamar FUNCTION y funciones nativas por nombre",
            )
            return INT

        name = node.callee.value
        arg_types = [self.check_expr(arg) for arg in node.args]

        native = NATIVE_FUNCTIONS.get(name)
        if native is not None:
            minimum, maximum, returns, module = native
            if len(arg_types) < minimum or (maximum is not None
                                            and len(arg_types) > maximum):
                self.diagnostics.error(
                    "E0033", f"'{name.upper()}' espera entre {minimum} y "
                             f"{maximum} argumentos, recibidos {len(arg_types)}",
                    node.span,
                    f"{name.upper()} siempre ha tenido esa aridad; revisa los "
                    f"argumentos que le has pasado",
                )
                return returns or INT
            if module and module not in self.modules:
                self.diagnostics.error(
                    "E0060", f"falta 'USE {module}' para usar {name.upper()}",
                    node.span,
                    f"añade USE {module} al principio del programa",
                )
            if name == "len":
                target = arg_types[0]
                if target.kind == "STRING":
                    return INT  # bytes, ADR 0003
                if target.kind == "ARRAY":
                    return INT
                self.diagnostics.error(
                    "E0034", f"LEN no acepta {target}",
                    node.args[0].span,
                    "LEN funciona con STRING (bytes) y ARRAY (elementos)",
                )
                return INT
            if name in ("key", "keydown"):
                if arg_types[0].kind != "STRING":
                    self.diagnostics.error(
                        "E0035", f"{name.upper()} necesita una tecla entre "
                                 "comillas",
                        node.args[0].span,
                        'por ejemplo KEY("w")',
                    )
                return BOOL
            if name == "chr":
                # ADR 0021. Una cadena en Source **son bytes**: `str.byte` baja a
                # `i32.load8_u` y LEN cuenta bytes (ADR 0003). Asi que el rango
                # de CHR no es una prudencia, es lo que la representacion da:
                # `CHR(256)` es inexpresable, y por eso lleva codigo de
                # diagnostico en vez de truncarse en silencio.
                self.require_numeric(arg_types[0], node.args[0])
                self.comprueba_rango_chr(node)
                return STRING
            if name in ("rgb",):
                return COLOR
            if name in ("abs", "min", "max", "clamp"):
                for arg_type, arg in zip(arg_types, node.args):
                    self.require_numeric(arg_type, arg)
                if name == "abs":
                    return arg_types[0]
                # `MIN`, `MAX` y `CLAMP` comparan sus argumentos entre sí, así que
                # el tipo del resultado es el **más ancho** de los que llegan, no
                # el del primero. Declarar el del primero obligaba a estrechar en
                # silencio: `MAX(1, 3.5)` decía devolver `INTEGER` y el
                # intérprete devolvía `3.5`.
                return max(arg_types, key=lambda t: _PROMOTION[t.kind])
            for arg_type, arg in zip(arg_types, node.args):
                if name in ("sin", "cos", "sqrt", "floor"):
                    self.require_numeric(arg_type, arg)
            return returns or INT

        if name == "flip":
            self.check_flip_call(node)
            return VOID

        info = self.functions.get(name)
        if info is not None:
            if len(arg_types) != len(info.params):
                self.diagnostics.error(
                    "E0036", f"'{name}' espera {len(info.params)} argumento(s), "
                             f"recibidos {len(arg_types)}",
                    node.span,
                    "firma: FUNCTION " + name
                    + "(" + ", ".join(f"{p} AS {t}" for p, t in info.params)
                    + ")",
                )
                return info.return_type
            for (pname, ptype), arg_type, arg in zip(info.params, arg_types, node.args):
                if not self.assignable(arg_type, ptype):
                    self.report_mismatch(pname, arg_type, ptype, arg)
            return info.return_type

        # Nombre no declarado: distinguir entre "función desconocida" y
        # "variable que se usó como función" ahorra un viaje de ida y vuelta.
        if self.scope.lookup(name) is not None:
            self.diagnostics.error(
                "E0037", f"'{name}' no es una FUNCTION",
                node.callee.span,
                "una variable no se puede llamar; quita los paréntesis",
            )
            return INT

        self.report_undeclared(node.callee)
        return INT

    def valor_entero_literal(self, node: ast.Expr) -> int | None:
        """El valor de un literal entero, y el de un menos literal tambien.

        `CHR(-1)` es `Unary("-", IntLit(1))`, no un `IntLit(-1)`: el parser
        construye el signo como operacion. Sin esta indireccion el negativo se
        escapaba de la comprobacion y `i32.store8` lo convertia en 255 sin decir
        nada, que es el truncamiento en silencio que ADR 0021 dice que no.
        """
        if isinstance(node, ast.IntLit):
            return node.value
        if isinstance(node, ast.Unary) and node.op == "-" \
                and isinstance(node.operand, ast.IntLit):
            return -node.operand.value
        return None

    def comprueba_rango_chr(self, node: ast.Call) -> None:
        """CHR solo acepta 0..255, y solo se comprueba si el valor se ve.

        Con un literal el error es en tiempo de compilacion y con la correccion
        escrita. Con una expresion el valor no se ve aqui, y lo trunca el
        backend con `i32.store8`: `CHR(300)` escribiria el byte 44 sin decir
        nada. Un literal es el caso que se puede好心; una expresion no, y
        fingir lo contrario seria un diagnostico que miente.
        """
        valor = self.valor_entero_literal(node.args[0])
        if valor is None:
            return
        if 0 <= valor <= 255:
            return
        if valor < 0:
            self.diagnostics.error(
                "E0068", f"CHR({valor}) no es un byte",
                node.args[0].span,
                "CHR construye un byte, y un byte va de 0 a 255; si lo que "
                "quieres es un caracter de un texto, usa MID$(texto, i, 1)",
            )
        else:
            self.diagnostics.error(
                "E0068", f"CHR({valor}) no cabe en un byte",
                node.args[0].span,
                "CHR construye un byte, y un byte va de 0 a 255 (ADR 0021); "
                "una cadena en Source es una secuencia de bytes, no de "
                "caracteres Unicode, asi que 256 y siguientes no tienen forma",
            )

    def check_flip_call(self, node: ast.Call) -> None:
        if "SOURCE_2D" not in self.modules:
            self.diagnostics.error(
                "E0060", "falta 'USE SOURCE_2D' para usar FLIP",
                node.span,
                "añade USE SOURCE_2D al principio del programa",
            )
        if self.loop_depth == 0:
            self.diagnostics.error(
                "E0051", "FLIP fuera de un bucle",
                node.span,
                "FLIP presenta un frame; tiene sentido dentro de un bucle",
            )

    # ------------------------------------------------------------- utilidades

    def register_string_literals(self, node: ast.Node) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.StringLit):
                self.result.string_literals.append(child.value)

    def resolve_type(self, type_ref: ast.TypeRef, default: Type | None = None) -> Type:
        if type_ref.name == "ARRAY":
            element = self.resolve_type(type_ref.element, default=INT) \
                if type_ref.element is not None else INT
            return Type("ARRAY", element=element)
        if type_ref.name == "STRUCT":
            self.diagnostics.error(
                "E0025", "STRUCT todavía no implementado",
                type_ref.span,
                "los structs llegan en la Fase 4",
            )
            return INT
        resolved = _PRIM_BY_NAME.get(type_ref.name)
        if resolved is None:
            self.diagnostics.error(
                "E0020", f"tipo desconocido '{type_ref.name}'",
                type_ref.span,
                "tipos: " + ", ".join(sorted(_PRIM_BY_NAME)),
            )
            return default if default is not None else INT
        return resolved

    def promote(self, left: Type, right: Type) -> Type:
        left_rank = _PROMOTION.get(left.kind, -1)
        right_rank = _PROMOTION.get(right.kind, -1)
        if left_rank < 0:
            return left
        if right_rank < 0:
            return right
        if left.kind == "COLOR" or right.kind == "COLOR":
            return COLOR if left_rank >= right_rank else right
        return DOUBLE if max(left_rank, right_rank) == 2 else (
            SINGLE if max(left_rank, right_rank) == 1 else INT
        )

    def assignable(self, value: Type, target: Type, expr: ast.Node = None) -> bool:
        """¿Se puede asignar un valor de este tipo a una variable de ese otro?

        La familia numérica es asignable en ambos sentidos. `vx = vx + 0.5` con
        `vx AS SINGLE` es el bucle más normal del mundo, y rechazarlo obligaría
        a escribir `SINGLE(vx + 0.5)` en todas partes, que es la clase de
        ruido que hace que la gente odie un lenguaje. La pérdida de precisión es
        justo lo que significa elegir SINGLE.

        Lo que sí es un error es el desbordamiento: un literal que no cabe en el
        tipo destino se detecta en compilación (`DIM v AS SINGLE = 1e40`).
        """
        if value.kind == target.kind:
            return True
        if expr is not None and isinstance(expr, (ast.FloatLit, ast.IntLit)):
            # Un literal tiene que caber de verdad en el tipo destino.
            if target.kind in ("SINGLE", "DOUBLE") and value.kind in (
                    "INTEGER", "SINGLE", "DOUBLE"):
                return _fits_in(expr, target)
        if value.kind == "PTR":
            return True
        # Ensanchado numérico: INTEGER cabe en SINGLE y en DOUBLE.
        if target.kind in ("SINGLE", "DOUBLE") and value.kind == "INTEGER":
            return True
        # Cualquier número va a cualquier número: el compilador inserta la
        # conversión. Es una decisión de ergonomía, no de seguridad, y está
        # documentada en GRAMMAR.md §2.1.
        if value.is_numeric and target.is_numeric:
            return True
        if target.kind == "DOUBLE" and value.kind == "SINGLE":
            return True
        if target.kind == "COLOR" and value.kind == "INTEGER":
            return True
        if target.kind == "BOOLEAN" and value.kind == "INTEGER":
            return True
        return False

    def require_boolean(self, type_: Type, node: ast.Node, context: str) -> None:
        if type_.kind == "BOOLEAN":
            return
        if type_.is_numeric or type_.kind == "PTR":
            # La sugerencia cita la expresión real, no su tipo: "escribe y <> 0"
            # es accionable y "escribe SINGLE <> 0" no lo es.
            quoted = f"{node.value} " if isinstance(node, ast.Name) else ""
            self.diagnostics.error(
                "E0024", f"{context} necesita BOOLEAN, no {type_}",
                node.span,
                f"en Source no hay verdad implícita: escribe {quoted}<> 0",
            )
            return
        self.diagnostics.error(
            "E0024", f"{context} necesita BOOLEAN, no {type_}",
            node.span,
            f"compara explícitamente: {type_} = ...",
        )

    def require_numeric(self, type_: Type, node: ast.Node) -> None:
        if type_.is_numeric:
            return
        self.diagnostics.error(
            "E0038", f"se esperaba un número, no {type_}",
            node.span,
            "las operaciones aritméticas sólo aceptan INTEGER, SINGLE, "
            "DOUBLE y COLOR",
        )

    def require_integer(self, type_: Type, node: ast.Node, what: str) -> None:
        if type_.is_integer or type_.kind == "PTR":
            return
        self.diagnostics.error(
            "E0039", f"{what} necesita INTEGER, no {type_}",
            node.span,
            "las dimensiones e índices son enteros",
        )

    def report_undeclared(self, node: ast.Node) -> None:
        name = node.value if isinstance(node, ast.Name) else "?"
        candidates = list(self.scope.symbols.keys()) + list(self.functions.keys())
        candidates += KNOWN_KEYS
        hint = None
        guess = suggest(name, candidates)
        if guess:
            hint = f"¿quiso decir '{guess}'?"
        else:
            hint = (f"declárala con DIM {name} AS INTEGER = 0; Source no crea "
                    "variables implícitas (ADR 0002)")
        self.diagnostics.error(
            "E0022", f"variable no declarada '{name}'", node.span, hint,
        )

    def report_mismatch(self, name: str, value: Type, target: Type,
                        node: ast.Node) -> None:
        if value.is_numeric and target.is_numeric:
            hint = "no hay conversiones implícitas entre variables; declara el " \
                   f"destino como {value}, o escribe el valor con el tipo " \
                   f"correcto"
        else:
            hint = f"'{name}' es {target}, no {value}"
        self.diagnostics.error(
            "E0022" if isinstance(node, ast.Assign) else "E0016",
            f"tipo incompatible en '{name}': {value} donde se esperaba {target}",
            node.span, hint,
        )


def _fits_in(expr: ast.Node, target: Type) -> bool:
    """Rango de un literal entero: i64 con signo."""
    """¿El literal cabe en el tipo destino?

    `DIM v AS SINGLE = 0.1` es correcto (0.1 se puede aproximar en f32);
    `DIM v AS SINGLE = 1e40` no lo es, y sí es un error de compilación: mejor
    un diagnóstico ahora que un `inf` silencioso después.
    """
    if not isinstance(expr, ast.FloatLit):
        return True
    import struct
    value = expr.value
    if target.kind == "DOUBLE":
        return True
    if target.kind == "SINGLE":
        try:
            packed = struct.pack(">f", value)
        except OverflowError:
            return False
        return not (packed[3] == 0x7F and struct.unpack(">I", packed[:4])[0] != 0x7F800000)
    return True


def _contains_flip(body: tuple) -> bool:
    """¿Hay un FLIP en este cuerpo, a cualquier profundidad?"""
    for statement in body:
        if isinstance(statement, ast.Primitive) and statement.name == "FLIP":
            return True
        for attribute in ("then_body", "else_body", "body"):
            nested = getattr(statement, attribute, None)
            if nested and _contains_flip(nested):
                return True
    return False


def _zero_literal(type_: Type) -> str:
    value = _ZERO_VALUE.get(type_.kind)
    if isinstance(value, bool):
        return "FALSE" if value else "TRUE"
    if isinstance(value, str):
        return f'"{value}"'
    if value is None:
        return type_.kind
    return str(value)


def check(program: ast.Program, diagnostics: DiagnosticBag) -> Typing:
    return TypeChecker(diagnostics).check_program(program)


def zero_value_of(type_: Type):
    """Valor cero de un tipo, para el inicializador implícito de `DIM x AS T`."""
    if type_.kind == "ARRAY":
        return []
    if type_.kind == "PTR":
        return None
    value = _ZERO_VALUE.get(type_.kind, 0)
    if isinstance(value, Type):
        return None  # VEC3/MAT4: pendiente de la Fase 4
    return value


def zero_of_code(code: int):
    """Valor cero a partir del código entero que lleva el IR.

    El intérprete descuplica la representación en memoria: el IR sólo lleva
    números, y quien ejecuta decide qué significa cada código. En WASM el cero
    de un array de `SINGLE` son bytes cero; aquí es una lista de `0.0`.
    """
    layout = layout_by_code(code)
    if layout.kind == "STRING":
        return (b"", 0)
    if layout.kind == "ARRAY":
        return []
    if layout.kind in ("SINGLE", "DOUBLE"):
        return 0.0
    if layout.kind == "BOOLEAN":
        # El cero de un BOOLEAN es FALSE, no el entero 0. Un escalar ya se
        # imprimía como FALSE, porque `PRINT` de un booleano pasa por
        # `print_bool`; dentro de un array se imprime elemento a elemento y
        # salía `[0, 0]`, que es el mismo valor con dos nombres.
        return False
    if layout.kind in ("VEC2", "VEC3", "VEC4", "MAT3", "MAT4"):
        return [0.0] * layout.words
    return 0


ALL_RUNTIME_MODULES = tuple(sorted(RUNTIME_MODULES))