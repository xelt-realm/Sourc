"""El AST de Source, escrito como texto canónico.

Es el juez de la rebanada 2 del bootstrap. Nace **antes** que el parser escrito
en Sourc, y esa es la parte importante: un juez escrito después se ajusta al
código que juzga, y entonces deja de juzgar.

## Por qué un volcado y no comparar objetos

Porque el objeto que hay que comparar --un árbol-- no se puede comparar
"casi". Y porque un compilador escrito en Source va a producir un árbol con
arrays e índices enteros, donde la pregunta "¿son el mismo árbol?" se responde
mejor sobre texto: una diferencia es una línea, y esa línea dice el nodo, el
campo y el valor.

## El formato

    idx kind span_ini span_fin | campo=valor campo=valor ...

Una línea por nodo, en pre-orden. `idx` es el índice de pre-orden del nodo, que
es lo que permite que los campos que apuntan a hijos sean enteros y que el
golden se lea de arriba abajo.

**Cada nodo imprime todos sus campos, siempre**, en el orden de declaración, y
con nombre. Tres decisiones, y las tres para quitar decisiones al serializador:

- *Todos y siempre*: un formato condicional ("este campo sólo si aplica") mete
  al serializador en el sitio donde una divergencia se esconde. Aquí no hay
  nada que decidir.
- *Con nombre*: `name=x` no se confunde con `name` de otro nodo. Con ranuras
  posicionales, un índice mal escrito lee el campo de otro nodo sin error.
- *Con tipo declarado*: un hijo es un índice, un texto va escapado con
  `visible()`, un entero va decimal, un booleano va `1`/`0`, y un `None` va
  `-`. La lista está en `FORMATO` y es el contrato.

## La puerta que protects de la tabla

`tests/test_ast_dump.py` comprueba que `FORMATO` menciona **exactamente** los
campos de cada dataclass, sin duplicados y sin sobras. Si alguien añade un
campo a un nodo y no lo vuelca, la puerta falla en vez de que el campo
aparezca en silencio como `-` en todos los goldens y nadie lo note hasta que
falte algo.
"""

from __future__ import annotations

import dataclasses

from . import ast
from .texto import visible

# Formatos de campo. El valor es un formato de `render()`.
NODO = "nodo"        # un hijo: su índice de pre-orden, o `-`
NODOS = "nodos"      # una tupla de hijos: índices separados por comas
TEXTO = "texto"      # una cadena, escapada
ENTERO = "entero"    # un entero, decimal
LOGICO = "logico"    # un booleano: 1 o 0
LITERAL = "literal"  # el texto tal como se escribió (`text`)
SPAN = "span"        # un span secundario: `ini-fin`
PARAM = "param"      # una tupla de (nombre, tipo)

FORMATO: dict[str, tuple[tuple[str, str], ...]] = {
    # ------------------------------------------------------------ literales
    "IntLit": (("value", LITERAL),),
    "FloatLit": (("value", LITERAL),),
    "StringLit": (("value", TEXTO),),
    "BoolLit": (("value", LOGICO),),
    "NilLit": (),
    "ArrayLit": (("elements", NODOS),),
    "NewArray": (("type_name", TEXTO), ("count", NODO), ("type_span", SPAN)),

    # ---------------------------------------------------------- expresiones
    "Name": (("value", TEXTO),),
    "Unary": (("op", TEXTO), ("operand", NODO),),
    "Binary": (("op", TEXTO), ("left", NODO), ("right", NODO)),
    "Index": (("target", NODO), ("index", NODO)),
    "Field": (("target", NODO), ("name", TEXTO)),
    "Call": (("callee", NODO), ("args", NODOS)),
    "Interpolate": (("value", NODO),),

    # ----------------------------------------------------------------- tipos
    "TypeRef": (("name", TEXTO), ("element", NODO)),

    # ----------------------------------------------------------- sentencias
    "Use": (("module", TEXTO),),
    "ImportFrom": (("path", NODO), ("alias", TEXTO),),
    "Dim": (("name", TEXTO), ("name_span", SPAN), ("type_ref", NODO),
            ("value", NODO)),
    "Assign": (("target", NODO), ("value", NODO)),
    "If": (("condition", NODO), ("then_body", NODOS), ("else_body", NODOS),
           ("one_liner", LOGICO)),
    "While": (("condition", NODO), ("body", NODOS)),
    "For": (("var", TEXTO), ("var_span", SPAN), ("start", NODO), ("stop", NODO),
            ("step", NODO), ("body", NODOS)),
    "Goto": (("label", TEXTO),),
    "Gosub": (("label", TEXTO),),
    "Label": (("name", TEXTO),),
    "Return": (("value", NODO),),
    "Arena": (("name", TEXTO), ("body", NODOS)),
    "FunctionDecl": (("name", TEXTO), ("params", PARAM), ("return_type", NODO),
                     ("body", NODOS)),
    "Primitive": (("name", TEXTO), ("args", NODOS), ("inline", LOGICO)),
    "ColorStmt": (("index", NODO),),

    # ----------------------------------------------------------------- capas
    "Program": (("body", NODOS),),
}


def hijos_de(nodo, campo: str, formato: str) -> list:
    """Los nodos hijos de un campo, en el orden en que se vuelcan.

    El orden es el de `FORMATO`, y es el de declaración en el dataclass. Es el
    orden que fija el índice de pre-orden, así que cambiarlo cambia todos los
    goldens: por eso está en una tabla y no repartido por el código.
    """
    valor = getattr(nodo, campo)
    if formato == NODO:
        return [] if valor is None else [valor]
    if formato == NODOS:
        return [] if valor is None else list(valor)
    if formato == PARAM:
        return [] if valor is None else [t for _, t in valor if t is not None]
    return []


def render(nodo, campo: str, formato: str, indice) -> str:
    """El valor de un campo, ya en su forma canónica."""
    valor = getattr(nodo, campo)
    # Se busca por `id()` y no por el nodo, y no por descuido: los nodos son
    # dataclasses *frozen*, así que dos `StringLit` con el mismo valor y el mismo
    # span **son iguales** y colgarían en la misma clave de un dict. Un índice de
    # pre-orden que se repitiera porque dos nodos se parecieran daría un golden
    # que parece un árbol y describe otro.
    if formato == NODO:
        return "-" if valor is None else str(indice[id(valor)])
    if formato == NODOS:
        if valor is None:
            return "-"
        return ",".join(str(indice[id(hijo)]) for hijo in valor)
    if formato == PARAM:
        if valor is None:
            return "-"
        partes = []
        for nombre, tipo in valor:
            partes.append(f"{visible(nombre)}:"
                          + ("-" if tipo is None else str(indice[id(tipo)])))
        return ",".join(partes)
    if formato == TEXTO:
        return visible(valor)
    if formato == LITERAL:
        return visible(getattr(nodo, "text"))
    if formato == ENTERO:
        return str(valor)
    if formato == LOGICO:
        return "1" if valor else "0"
    if formato == SPAN:
        return f"{valor.start}-{valor.end}"
    raise AssertionError(f"formato desconocido: {formato}")  # pragma: no cover


def indexar(nodo, indice: dict) -> None:
    """Numerar el árbol en pre-orden.

    Se hace en una pasada aparte del volcado porque el índice de un hijo hace
    falta para imprimir el campo del padre, y el padre se imprime antes que sus
    hijos. Con una sola pasada, el padre tendría que esperar al hijo y el orden
    de salida dejaría de ser pre-orden.
    """
    if id(nodo) in indice:
        return
    indice[id(nodo)] = len(indice)
    for campo, formato in FORMATO[type(nodo).__name__]:
        for hijo in hijos_de(nodo, campo, formato):
            indexar(hijo, indice)


def volcar(programa, salida) -> int:
    """Escribe el AST y devuelve cuántos nodos ha escrito.

    El resumen final (`; N nodo(s)`) es lo mismo que hace `sourc lex`, y por el
    mismo motivo: una diferencia de un nodo en mil es fácil de no ver comparando
    texto, y el número la hace visible sin mirar.
    """
    indice: dict = {}
    indexar(programa, indice)
    _emitir(programa, indice, salida)
    salida.write(f"\n; {len(indice)} nodo(s)\n")
    return len(indice)


def _emitir(nodo, indice: dict, salida) -> None:
    campos = FORMATO[type(nodo).__name__]
    partes = [f"{render(nodo, campo, formato, indice)}"
              for campo, formato in campos]
    detalle = " ".join(f"{campo}={valor}" for (campo, _), valor
                       in zip(campos, partes))
    salida.write(
        f"{indice[id(nodo)]:>4} {type(nodo).__name__:<13} "
        f"{nodo.span.start:>5} {nodo.span.end:>5} | {detalle}\n")
    for campo, formato in campos:
        for hijo in hijos_de(nodo, campo, formato):
            _emitir(hijo, indice, salida)


# --------------------------------------------------------------- la puerta


def comprobar_tabla() -> list[str]:
    """Devuelve los problemas de `FORMATO` con el AST real.

    Tres cosas y las tres rompen en silencio: un campo del dataclass que no está
    en la tabla (nunca se vuelca, sale como si no existiera), un campo en la
    tabla que no existe (un `getattr` revienta en el primer programa), y un
    nodo del AST que no está en la tabla (el compilador entero lo usa y el
    volcado no lo contempla).
    """
    problemas: list[str] = []

    # Sólo las clases que de verdad son nodos. En `ast.py` además están `Node`,
    # que es la base y sólo tiene `span`, y `Span`, que se importa para las
    # anotaciones: las dos son dataclasses y ninguna es un nodo del árbol.
    clases = {
        nombre: getattr(ast, nombre)
        for nombre in dir(ast)
        if dataclasses.is_dataclass(getattr(ast, nombre))
        and isinstance(getattr(ast, nombre), type)
        and issubclass(getattr(ast, nombre), ast.Node)
        and getattr(ast, nombre) is not ast.Node
    }
    for nombre, clase in sorted(clases.items()):
        reales = [campo.name for campo in dataclasses.fields(clase)
                  if campo.name != "span"]
        if nombre not in FORMATO:
            problemas.append(
                f"{nombre} es un nodo del AST y no está en FORMATO")
            continue
        declarados = [campo for campo, _ in FORMATO[nombre]]
        # Un campo de formato LITERAL vuelca **dos** campos del dataclass: el
        # declarado --`value`-- y `text`, que es el texto tal como se escribió.
        # Sin esto la puerta pediría `text` en la tabla y no está, y acabaría
        # marking de más un formato que está bien.
        for campo, formato in FORMATO[nombre]:
            if formato == LITERAL and "text" not in declarados:
                declarados.append("text")
        for campo in reales:
            if campo not in declarados:
                problemas.append(
                    f"{nombre}.{campo} existe y no se vuelca")
        for campo in declarados:
            if campo not in reales:
                problemas.append(
                    f"{nombre}.{campo} se vuelca y no existe en el dataclass")
        if len(set(declarados)) != len(declarados):
            problemas.append(f"{nombre} declara un campo dos veces")

    for nombre, campos in FORMATO.items():
        if nombre not in clases:
            problemas.append(f"FORMATO nombra {nombre}, que no está en ast.py")
            continue
        for _, formato in campos:
            if formato not in (NODO, NODOS, TEXTO, ENTERO, LOGICO, LITERAL,
                               SPAN, PARAM):
                problemas.append(f"{nombre}: formato desconocido {formato}")
    return problemas