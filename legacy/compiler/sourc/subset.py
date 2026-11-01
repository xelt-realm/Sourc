"""El subconjunto del lenguaje que puede usar el compilador.

ADR 0015 dice dos cosas que aquí se implementan:

1. *«Cada fase declara un subset level. El runtime sólo puede usar features del
   nivel de la fase en curso, y un test de CI falla si usa algo más.»*
2. *«La regla del subconjunto es el riesgo número uno del proyecto. Por eso es
   un test de CI, no una intención.»*

Este módulo es el extractor: recorre el AST y nombra **cada feature por su
nombre**, sin mirar una lista de permitidos. La política va en `subset.toml` y
se comprueba en otro sitio. La separación es a propósito: un extractor con la
política dentro no se puede probar con fixtures, porque no se puede saber qué característica se está probando.

**Por qué por nombre de nodo del AST y no «lo que el parser acepta hoy».** Es
la diferencia entre un gate y un adorno. `STRUCT`, `ENUM` y los arrays
multidimensionales no parsean todavía, así que un vocabulario construido con «las
cosas que hoy compilan» no lo mencionaría, y el día que la Fase 4 los añada el
gate se autoautorizaría solo el día exacto en que tiene que apretar. Nombrando
nodos del AST, un `Struct` aparece en el informe sin que nadie lo escriba, y
`subset.toml` no lo tiene, así que falla hasta que alguien lo admita a mano.

**Casos.** `Primitive.name` llega en mayúsculas y `Name.value` en minúsculas --
el lexer baja los identificadores (ADR 0012). Un vocabulario mezclado es una
trampa: `Print` y `print` serían dos features y nadie sabría cuál está en la
lista. Aquí todo sale en minúsculas.

**Determinismo.** El resultado son `dict` y `list` ordenados, nunca `set`: el
informe va a un fichero de texto que se versiona y se compara, y un orden que
dependa del hash de un conjunto lo haría cambiar entre procesos (ADR 0016).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import ast
from .diagnostics import Diagnostic, Span
from .types import NATIVE_FUNCTIONS

# Nodos que son **sintaxis**, no features. Todo lo demás que aparezca en el
# recorrido es una feature y tiene que estar clasificada en `subset.toml`.
#
# Los literales no son una capacidad: un `HexLit` nuevo es otra forma de
# escribir un entero, no una cosa que el compilador en Source pueda destrabar o
# bloquear. `name` tampoco: no se puede usar una variable sin nombrar.
SINTAXIS = frozenset({
    "program", "span",
    "intlit", "floatlit", "stringlit", "boollit", "nillit", "name",
})

# Nodos que ya aportan su nombre en el `if/elif` de abajo y que no deben además
# aportar el de su clase. Sin esto, `a.b` se nombraría `campo` **y** `field`, y
# el contrato tendría que listar las dos o admitir la primera sin querer la
# segunda.
CON_NOMBRE_PROPIO = frozenset({
    "typeref", "primitive", "call", "use", "field", "importfrom",
})

# Las nativas que se pueden llamar por nombre. `PRINT` y `SCREEN` no están:
# son sentencias y llegan como `Primitive`.
_NATIVAS = frozenset(NATIVE_FUNCTIONS)

# `Call` cuyo callee no es un `Name` es pasar una función como valor. GRAMMAR §5
# lo permite explícitamente, así que tiene nombre propio y no se confunde con
# llamar a una primitiva por su nombre.
FUNCION_COMO_VALOR = "call:valor"
# Una llamada por nombre que no es una nativa.
CALL_POR_NOMBRE = "call:nombre"


def _minusculas(nombre: str) -> str:
    return nombre.lower()


def _tipo(nombre: str, elemento: "ast.TypeRef | None") -> list[str]:
    """Los nombres que aporta un `TypeRef`.

    `ARRAY OF INTEGER` da dos: `tipo:array` y `tipo:array-de-integer`. El
    compuesto es lo que permite admitir el array y prohibir el anidado por
    separado, que es la diferencia entre un contrato útil y uno que no dice nada.
    """
    if elemento is None:
        return [f"tipo:{_minusculas(nombre)}"]
    return [f"tipo:{_minusculas(nombre)}",
            f"tipo:{_minusculas(nombre)}-de-{_minusculas(elemento.name)}"]


class Recorrido:
    """Acumula feature -> dónde se usó cada una.

    Se guardan **todos** los sitios, no el primero: un informe que dice «el
    compilador usa `ARENA`» sin decir dónde obliga a buscarlo a mano, y con once
    o doce ficheros el lugar importa.
    """

    def __init__(self) -> None:
        self.usos: dict[str, list[Span]] = {}

    def anotar(self, feature: str, span: Span | None) -> None:
        self.usos.setdefault(feature, []).append(span or Span(0, 0))

    def nombres(self) -> list[str]:
        return sorted(self.usos)

    def primero(self, feature: str) -> Span | None:
        return self.usos[feature][0]


def features(program: ast.Program) -> dict[str, list[Span]]:
    """Las features que usa un programa, con dónde aparece cada una.

    Las claves van **en orden alfabético**, no en orden de aparición. El
    informe se versiona y se compara, y con orden de aparición mover dos
    líneas en el fuente mueve dos líneas del informe sin que haya cambiado nada
    de lo que el informe dice.
    """
    recorrido = Recorrido()
    _visitar(program, recorrido)
    return {nombre: recorrido.usos[nombre] for nombre in sorted(recorrido.usos)}


def _visitar(nodo, recorrido: Recorrido) -> None:
    """Recorre el AST y anota cada nodo.

    El recorrido es genérico a propósito --no una lista de casos-- porque
    `Span` es un dataclass y aparece como campo de todos los nodos: cualquier
    recorrido por «los que tienen campos» lo cuenta como si fuera una feature.
    Se filtra por nombre de clase, y lo que no esté en `SINTAXIS` se anota.
    """
    if isinstance(nodo, Span):
        return

    if isinstance(nodo, ast.TypeRef):
        for feature in _tipo(nodo.name, nodo.element):
            recorrido.anotar(feature, nodo.span)

    elif isinstance(nodo, ast.Primitive):
        # `PRINT` es una sentencia; `LEN` es un `Call`. Separados a propósito:
        # el contrato necesita poder admitir imprimir y prohibir `rgb`.
        recorrido.anotar(f"primitiva:{_minusculas(nodo.name)}", nodo.span)

    elif isinstance(nodo, ast.Call):
        if not isinstance(nodo.callee, ast.Name):
            recorrido.anotar(FUNCION_COMO_VALOR, nodo.span)
        elif nodo.callee.value.lower() in _NATIVAS:
            # `funcion:` es **sólo** para las nativas. Llamar a una función que
            # declara el propio programa no es una feature del contrato: si
            # también se nominara, la lista de permitidas tendría que
            # enumerar cada función que el compilador defina, y el contrato
            # dejaría de hablar de capacidades para hablar de los internals de
            # un fichero que todavía no existe.
            recorrido.anotar(f"funcion:{_minusculas(nodo.callee.value)}",
                             nodo.callee.span)
        else:
            recorrido.anotar("call:nombre", nodo.callee.span)

    elif isinstance(nodo, ast.Use):
        recorrido.anotar(f"use:{_minusculas(nodo.module)}", nodo.span)

    elif isinstance(nodo, ast.ImportFrom):
        recorrido.anotar("import", nodo.span)

    elif isinstance(nodo, ast.Field):
        # El acceso a campo tiene nombre propio y no necesita el del struct: lo
        # que se prohíbe es *leer un campo*, no el tipo que lo contiene.
        recorrido.anotar("campo", nodo.span)

    nombre = type(nodo).__name__.lower()
    if nombre not in SINTAXIS and nombre not in CON_NOMBRE_PROPIO:
        recorrido.anotar(nombre, getattr(nodo, "span", None))

    for campo in getattr(nodo, "__dataclass_fields__", {}):
        if campo == "span":
            continue
        valor = getattr(nodo, campo, None)
        for hijo in (valor if isinstance(valor, (list, tuple)) else (valor,)):
            if hasattr(hijo, "__dataclass_fields__"):
                _visitar(hijo, recorrido)


# ------------------------------------------------------------------ política


@dataclass(frozen=True)
class Prohibida:
    """Una feature fuera del nivel, con el porqué y con lo que cuesta."""

    feature: str
    motivo: str
    coste: str


@dataclass(frozen=True)
class Contrato:
    """El subconjunto de un nivel: lo permitido y lo prohibido con su motivo."""

    nivel: int
    permitidas: frozenset[str]
    prohibidas: dict[str, Prohibida]

    def permitidas_fuera(self, vistas: dict) -> list[str]:
        """Features usadas que el nivel no permite, en orden alfabético."""
        return sorted(set(vistas) - self.permitidas)

    def prohibidas_inmortales(self, producibles: set[str]) -> list[str]:
        """Features prohibidas que ningún programa puede llegar a usar.

        Documentación muerta: una prohibición que nada puede violar no aprieta
        nada y ocupa sitio en el contrato. Es el caso de `ast.Gosub`, que existe
        en el AST y que el parser nunca construye.

        `producibles` lo aporta quien llama, porque el extractor no puede saber
        qué frases existen: sólo qué nombres produce.
        """
        return sorted(set(self.prohibidas) - producibles)

    def vocabulario_posible(self) -> set[str]:
        return set(self.permitidas) | set(self.prohibidas)


def cargar(ruta: Path) -> Contrato:
    """Lee `subset.toml`.

    Falla si una feature está en las dos listas o si una prohibición no dice por
    qué. Las dos cosas son el mismo error --una lista que no significa nada-- y
    se comprueban al cargar, no en un test, para que ningún llamante pueda
    saltárselas.
    """
    import tomllib

    datos = tomllib.loads(ruta.read_text(encoding="utf-8"))
    permitidas = frozenset(datos.get("permitido", []))
    prohibidas: dict[str, Prohibida] = {}
    for entrada in datos.get("prohibido", []):
        feature = entrada["feature"]
        if feature in permitidas:
            raise ValueError(
                f"{ruta.name}: '{feature}' está en `permitido` y en "
                "`prohibido`. Una feature en las dos listas no significa nada.")
        if feature in prohibidas:
            raise ValueError(f"{ruta.name}: '{feature}' está prohibido dos veces.")
        prohibidas[feature] = Prohibida(
            feature=feature,
            motivo=entrada.get("motivo", "").strip(),
            coste=entrada.get("coste", "").strip())

    sin_motivo = sorted(f.feature for f in prohibidas.values() if not f.motivo)
    if sin_motivo:
        raise ValueError(
            f"{ruta.name}: prohibiciones sin motivo: {sin_motivo}. Una "
            "prohibición sin razón es una intención, y una intención no es un "
            "gate.")
    return Contrato(nivel=int(datos.get("nivel", 0)), permitidas=permitidas,
                   prohibidas=prohibidas)


# --------------------------------------------------------------- el diagnóstico

# E0066. Los códigos llegan hasta E0065, y este es el primero que no es sobre
# el **lenguaje**: es sobre el contrato entre el compilador y su propio fuente.
CODIGO_FUERA_DE_NIVEL = "E0066"


def violaciones(program: ast.Program, contrato: "Contrato") -> list[Diagnostic]:
    """Los diagnósticos de un programa que se sale del nivel.

    Uno por **uso**, no uno por feature: un compilador con doce ficheros que usa
    `ARENA` en cuarenta sitios necesita los cuarenta, porque el mensaje con la
    línea es lo que hace falta para arreglarlo.
    """
    vistas = features(program)
    salida: list[Diagnostic] = []
    for feature in contrato.permitidas_fuera(vistas):
        entrada = contrato.prohibidas.get(feature)
        motivo = _a_una_linea(entrada.motivo) if entrada else \
            "no está en la lista de permitidas"
        sugerencia = f"el nivel {contrato.nivel} no admite `{feature}`: {motivo}"
        if entrada is not None:
            sugerencia += (f" -- mientras tanto: "
                           f"{_a_una_linea(entrada.coste)}")
        for span in vistas[feature]:
            salida.append(Diagnostic(
                CODIGO_FUERA_DE_NIVEL,
                f"`{feature}` está por encima del nivel {contrato.nivel}",
                span, sugerencia))
    return salida


def _a_una_linea(texto: str) -> str:
    """Plega un texto de `subset.toml` a una línea.

    Los motivos y los costes se escriben en TOML en varias líneas, con su
    sangría, para que el fichero se lea. En el diagnóstico esa sangría es ruido:
    el renderizador enseña la línea entera, y un bloque con la sangría de un
    bloque es más largo que el mensaje.
    """
    return " ".join(texto.split())
