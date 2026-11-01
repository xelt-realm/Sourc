"""AST del lenguaje Source.

Los nodos son dataclasses inmutables con `span`. Se serializan de forma
determinista en `sir.py`, así que el orden de los campos importa.

Convenciones:
  - Cada nodo tiene exactamente un `kind` (el nombre de la clase), que es lo
    que se serializa.
  - `Statement` y `Expr` son las dos familias. El typechecker recorre las dos por
    igual, así que las interfaces se parecen a propósito.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from .diagnostics import Span


@dataclass(frozen=True)
class Node:
    span: Span


# ------------------------------------------------------------------ literales


@dataclass(frozen=True)
class IntLit(Node):
    value: int
    # El texto tal como se escribió, y no sólo el valor. Sin él, el volcado del
    # AST tiene que volver a imprimir el número, y un compilador escrito en
    # Sourc **no puede**: `&HFFFFFFFFFFFFFFFF` no cabe en un `SINGLE` y
    # `VAL()` devuelve `DOUBLE`, así que Source no puede recuperar el entero ni
    # la base en que estaba escrito. Guardar el texto hace que esa parte del
    # golden sea idéntica por construcción en vez de por suerte, y convierte la
    # conversión a número en trabajo del typechecker, que es donde debe estar.
    text: str = ""


@dataclass(frozen=True)
class FloatLit(Node):
    value: float
    # El mismo motivo que en IntLit, y además por el formato: Source y Python no
    # tienen por qué imprimir un `SINGLE` igual, y un golden que dependa de eso
    # mide dos formateadores en vez de un parser. El texto escrito es el mismo
    # para los dos sin haber que coincidir en nada.
    text: str = ""


@dataclass(frozen=True)
class StringLit(Node):
    value: str


@dataclass(frozen=True)
class BoolLit(Node):
    value: bool


@dataclass(frozen=True)
class NilLit(Node):
    pass


@dataclass(frozen=True)
class ArrayLit(Node):
    elements: tuple["Expr", ...]


@dataclass(frozen=True)
class NewArray(Node):
    type_name: str
    count: "Expr"
    type_span: Span


# ---------------------------------------------------------------- expresiones


@dataclass(frozen=True)
class Name(Node):
    """Identificador. `value` en minúsculas (ADR 0012)."""

    value: str


@dataclass(frozen=True)
class Unary(Node):
    op: str
    operand: "Expr"


@dataclass(frozen=True)
class Binary(Node):
    op: str
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class Index(Node):
    target: "Expr"
    index: "Expr"


@dataclass(frozen=True)
class Field(Node):
    target: "Expr"
    name: str


@dataclass(frozen=True)
class Call(Node):
    callee: "Expr"
    args: tuple["Expr", ...]


@dataclass(frozen=True)
class Interpolate(Node):
    """`"{expresión}"`: convierte la expresión a texto.

    Existe como nodo y no como un `Binary("&")` porque la conversión es
    implícita y sólo vale dentro de la interpolación. En cualquier otro sitio,
    concatenar un entero sigue siendo un error: `"total: " & entero` sigue
    exigiendo `STR(entero)`.
    """

    value: "Expr"


# ------------------------------------------------------------------- tipos


@dataclass(frozen=True)
class TypeRef(Node):
    """Referencia a un tipo tal como aparece en el fuente.

    `name` es la forma escrita: `INTEGER`, `ARRAY`, `STRUCT`, `VEC3`...
    `element` sólo para `ARRAY OF T`. El typechecker lo resuelve a un `Type`.
    """

    name: str
    element: "TypeRef | None" = None


# ---------------------------------------------------------------- sentencias


@dataclass(frozen=True)
class Use(Node):
    module: str


@dataclass(frozen=True)
class ImportFrom(Node):
    path: StringLit
    alias: str | None


@dataclass(frozen=True)
class Dim(Node):
    name: str
    name_span: Span
    type_ref: TypeRef | None
    value: "Expr | None"


@dataclass(frozen=True)
class Assign(Node):
    target: "Expr"
    value: "Expr"


@dataclass(frozen=True)
class If(Node):
    condition: "Expr"
    then_body: tuple["Statement", ...]
    else_body: tuple["Statement", ...] | None
    one_liner: bool


@dataclass(frozen=True)
class While(Node):
    condition: "Expr"
    body: tuple["Statement", ...]


@dataclass(frozen=True)
class For(Node):
    var: str
    var_span: Span
    start: "Expr"
    stop: "Expr"
    step: "Expr | None"
    body: tuple["Statement", ...]


@dataclass(frozen=True)
class Goto(Node):
    label: str


@dataclass(frozen=True)
class Gosub(Node):
    label: str


@dataclass(frozen=True)
class Label(Node):
    name: str


@dataclass(frozen=True)
class Return(Node):
    value: "Expr | None"


@dataclass(frozen=True)
class Arena(Node):
    name: str
    body: tuple["Statement", ...]


@dataclass(frozen=True)
class FunctionDecl(Node):
    name: str
    params: tuple[tuple[str, TypeRef | None], ...]
    return_type: TypeRef | None
    body: tuple["Statement", ...]


# ----------------------------------------------------------------- primitivas


@dataclass(frozen=True)
class Primitive(Node):
    """Llamada a una primitiva nativa.

    `name` en mayúsculas. `args` ya viene con los valores por defecto resueltos
    por el parser cuando la primitiva los tiene (por ejemplo `TEXT` sin color).
    """

    name: str
    args: tuple["Expr", ...]
    inline: bool = False  # PRINT "a" ; "b"  usa `;` para no añadir salto


@dataclass(frozen=True)
class ColorStmt(Node):
    """`COLOR 5` — la sentencia de paleta (ADR 0005)."""

    index: "Expr"


# --------------------------------------------------------------------- capas


@dataclass(frozen=True)
class Program(Node):
    body: tuple["Statement", ...]


Expr = Union[
    IntLit, FloatLit, StringLit, BoolLit, NilLit, ArrayLit, NewArray,
    Name, Unary, Binary, Index, Field, Call, Interpolate,
]

Statement = Union[
    Use, ImportFrom, Dim, Assign, If, While, For, Goto, Gosub, Label,
    Return, Arena, FunctionDecl, Primitive, ColorStmt,
]


def kind_of(node: Node) -> str:
    return type(node).__name__


def walk(node: Node):
    """Recorre en profundidad los hijos de un nodo.

    Se usa en el typechecker y en el fuzzer. Deliberadamente genérico: cualquier
    campo que sea un Node o una tupla de Node se visita.
    """
    from dataclasses import fields as dc_fields

    for f in dc_fields(node):
        value = getattr(node, f.name)
        if isinstance(value, Node):
            yield value
            yield from walk(value)
        elif isinstance(value, tuple):
            for item in value:
                if isinstance(item, Node):
                    yield item
                    yield from walk(item)
                elif isinstance(item, tuple):
                    # Parametros de FUNCTION: (nombre, TypeRef|None)
                    for sub in item:
                        if isinstance(sub, Node):
                            yield sub
                            yield from walk(sub)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Node):
                    yield item
                    yield from walk(item)