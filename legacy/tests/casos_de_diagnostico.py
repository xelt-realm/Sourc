"""Un programa por código de diagnóstico, y una reserva para los que no se pueden provocar.

Este fichero es el otro lado de `tests/test_contrato_diagnostico.py`. El test
dice qué tiene que pasar; aquí están los programas.

## Por qué un programa y no una cadena

Porque un código que aparece en el fuente no prueba que su camino funcione. El
`E0058` --"el backend todavía no baja esta operación"-- estaba escrito, era
correcto, y su ruta de renderizado estaba rota: el CLI reventaba con
`AttributeError` antes de imprimirlo. Un test que comprueba que el código existe
no se entera. Uno que **ejecuta** el programa entero sí, y por eso cada caso es
un fuente que se compila de verdad.

## Por qué `contiene` y no el mensaje entero

El **código** es la API estable (`E0022` no cambia). El texto es parte del
contrato --AGENTS.md, regla 3-- pero congelar 49 mensajes completos convierte
cualquier reordenación de una frase en 50 fallos que hay que actualizar a mano, y
esa fricción hace que la puerta se apague. Así que cada caso fija el código, el
fragmento que identifica el error, y que la sugerencia **no esté vacía** -- que es
lo que hace útil un diagnóstico. Los textos que de verdad son API --`E0066`,
`E0067`, `E0058`-- sí están snapshotteados enteros en `tests/test_diagnostics.py`.

## La reserva, y por qué tiene que ser corta

Unos códigos **no se pueden provocar desde un fuente**: son guardas internas del
builder y del verificador, que existen para que un error interno se vea rápido y
no para avisar a quien escribe programas. Escribirlos es fácil; incluirlos en un
programa es imposible, porque el programa que los provocara sería el bug que
guardan.

Se listan aparte, con motivo. Y un test exige que la reserva no contenga códigos
que el compilador ya no emite, porque una reserva caducada es ruido que esconde el
caso real: se lee, no dice nada, y pasa.
"""

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Caso:
    """Un programa que tiene que dar un código, y el fragmento que lo identifica."""
    fuente: str
    contiene: str = ""
    # Qué fases tienen que correr. Por defecto parseo y typecheck, que es donde
    # vive la mayoría de los diagnósticos; el lexer y el SIR se ejecutan aparte
    # porque fallan lanzando `CompileError` en vez de llenar un `DiagnosticBag`.
    fases: tuple[str, ...] = ("check",)
    # Códigos que también tienen que salir, y sólo esos.
    #
    # Existe para los diagnósticos que **informan y abortan**: `E0014` dice que
    # `for` es palabra clave y a continuación el parser lanza `E0010` porque el
    # token ya no puede ser nombre. Declarar el compañero es más honesto que
    # aflojar el test entero, y sigue detecting un error extra de verdad.
    tambien: tuple[str, ...] = ()


def _lexer(fuente: str):
    """Sólo lexar. Los errores del lexer no pasan por el parser."""
    from sourc.diagnostics import CompileError, DiagnosticBag
    from sourc.lexer import tokenize
    bag = DiagnosticBag()
    try:
        tokenize(fuente, bag)
    except CompileError as error:
        return [error.diagnostic]
    return list(bag.errors)


def _bajar(fuente: str):
    """Parsear, typecheck y bajar a IR. Para lo que sólo falla en el SIR."""
    from sourc import sir
    from sourc.diagnostics import CompileError, DiagnosticBag
    from sourc.parser import parse
    from sourc.types import check

    bag = DiagnosticBag()
    try:
        programa = parse(fuente, bag)
        if bag.has_errors():
            return list(bag.errors)
        typing = check(programa, bag)
        if bag.has_errors():
            return list(bag.errors)
        sir.build(programa, typing, "caso", "1.0")
    except CompileError as error:
        return [error.diagnostic]
    return list(bag.errors)


def _check(fuente: str):
    """Parsear y typecheck, capturando también el `CompileError` Immediate.

    Hay diagnósticos --`E0010` entre ellos-- que el parser lanza en vez de
    añadirlos a la bolsa, porque no puede seguir parseando sin la posición.
    Ignorarlos hacía que tres códigos parecieran no provocables, que es peor que
    no tener caso: el test pasaba sin comprobar nada.
    """
    from sourc.diagnostics import CompileError, DiagnosticBag
    from sourc.parser import parse
    from sourc.types import check

    bag = DiagnosticBag()
    try:
        programa = parse(fuente, bag)
        if not bag.has_errors():
            check(programa, bag)
    except CompileError as error:
        return list(bag.errors) + [error.diagnostic]
    return list(bag.errors)


def _subset(fuente: str):
    """Sólo el contrato del subconjunto (ADR 0015). `E0066` no lo emite el
    compilador: lo emite la puerta que decide si un programa cabe en el nivel 2,
    y por eso necesita su propio colector."""
    from pathlib import Path as _Path
    from sourc import subset
    from sourc.diagnostics import DiagnosticBag
    from sourc.parser import parse

    raiz = _Path(__file__).resolve().parents[1]
    bag = DiagnosticBag()
    try:
        programa = parse(fuente, bag)
    except Exception:                                # noqa: BLE001
        return []
    return list(subset.violaciones(programa,
                                subset.cargar(raiz / "subset.toml")))


COLECTORES: dict[str, Callable[[str], list]] = {
    "lexer": _lexer,
    "check": _check,
    "bajar": _bajar,
    "subset": _subset,
}


CASOS: dict[str, Caso] = {}


def _c(codigo: str, fuente: str, contiene: str = "",
      fases: tuple[str, ...] = ("check",),
      tambien: tuple[str, ...] = ()) -> None:
    """Registra un caso. Es una función y no un diccionario literal para que la
    tabla se pueda leer de arriba abajo y se vea qué código cubre qué."""
    CASOS[codigo] = Caso(fuente, contiene, fases, tambien)


# ------------------------------------------------------------------ E0001..E0006
_c("E0001", "PRINT #", "carácter inesperado", ("lexer",))
_c("E0002", "PRINT &H", "sin dígitos", ("lexer",))
_c("E0003", 'PRINT "ab\ncd"\n', "sin cerrar", ("lexer",))
_c("E0004", 'PRINT "ab', "sin cerrar al final", ("lexer",))
_c("E0005", 'PRINT "ab\\q"\n', "escape desconocido", ("lexer",))
_c("E0006", 'PRINT "\\u{ZZ}"\n', "unicode", ("lexer",))

# ------------------------------------------------------------------ E0010..E0016
_c("E0010", "DIM\n", "necesita un nombre")
_c("E0011", "DIM x AS INTEGER\nPRINT x\n", "tipo pero no valor")
_c("E0014", "DIM for AS INTEGER = 1\n", "palabra clave",
   ("check",), ("E0010",))
_c("E0016", 'DIM x AS INTEGER = "hola"\n', "tipo incompatible")

# ------------------------------------------------------------------ E0020..E0030
_c("E0020", "DIM a AS ARRAY OF INTEGER = [1]\nPRINT \"{a}\"\n", "interpolar")
_c("E0021", 'PRINT "a" & 1\n', "&")
_c("E0022", "PRINT x\n", "no declarada")
_c("E0024", "IF 1 THEN\nPRINT 1\nEND IF\n", "BOOLEAN")
_c("E0025", "DIM s AS STRUCT Punto = 0\n", "STRUCT")
_c("E0026", "DIM a AS ARRAY OF INTEGER = [1]\n[1, 2] = [3, 4]\n",
   "literal de array")
_c("E0027", 'PRINT ("a" = 1)\n', "STRING")
_c("E0028", "DIM a AS ARRAY OF INTEGER = [1]\nDIM b AS ARRAY OF INTEGER = [2]\n"
           "PRINT (a = b)\n", "comparar")
_c("E0029", "DIM x AS INTEGER = 1\nPRINT x[0]\n", "indexar")
_c("E0030", "PRINT (1 + 2)(3)\n", "llamar")

# ------------------------------------------------------------------ E0033..E0039
_c("E0033", "PRINT MIN(1)\n", "argumentos")
_c("E0034", "DIM x AS INTEGER = 1\nPRINT LEN(x)\n", "LEN")
_c("E0035", "USE SOURCE_2D\nPRINT KEY(1)\n", "tecla")
_c("E0036", "FUNCTION f(a AS INTEGER) AS INTEGER\nRETURN a\nEND FUNCTION\n"
           "PRINT f()\n", "argumento")
_c("E0037", "DIM f AS INTEGER = 1\nPRINT f()\n", "FUNCTION")
_c("E0038", 'PRINT -"a"\n', "número")
_c("E0039", 'DIM a AS ARRAY OF INTEGER = [1]\nPRINT a["x"]\n', "INTEGER")

# ------------------------------------------------------------------ E0041..E0044
_c("E0041", "FOR i = 0 TO 1\nNEXT j\n", "NEXT")
_c("E0043", "salto:\nGOTO salto\n", "GOTO", ("bajar",))
_c("E0044", "ARENA buf\nDIM v AS INTEGER = 0\nEND ARENA\n", "ARENA", ("bajar",))

# ------------------------------------------------------------------ E0051..E0068
_c("E0051", "USE SOURCE_2D\nFLIP\n", "bucle")
_c("E0053", "FUNCTION f()\nRETURN 1\nEND FUNCTION\n", "valor")
_c("E0054", "FUNCTION f() AS INTEGER\nRETURN 1\nEND FUNCTION\n"
           "FUNCTION f() AS INTEGER\nRETURN 2\nEND FUNCTION\n", "ya declarada")
_c("E0055", "FUNCTION f(x)\nPRINT 1\nEND FUNCTION\n", "tipo")
_c("E0056", "PRINT 1\nRETURN 1\n", "fuera")
_c("E0060", "PSET 1, 1, 1\n", "USE")
_c("E0062", "USE SOURCE_2D\nPSET 1\n", "al menos")
_c("E0066", "ARENA buf\nDIM v AS INTEGER = 0\nEND ARENA\n", "", ("subset",))
_c("E0067", "DIM t AS INTEGER = 1\nFUNCTION f() AS INTEGER\nRETURN t\n"
           "END FUNCTION\n", "global")
_c("E0068", "PRINT CHR(256)\n", "byte")
_c("E0068", "PRINT CHR(-1)\n", "byte")

# Códigos que no se pueden provocar desde un fuente.
#
# Son guardas internas: existen para que un error **del propio compilador** se vea
# con un código y una línea, en vez de como un `AttributeError` veinte líneas más
# abajo. El programa que los provocaría es el bug que previenen, y ninguno de
# ellos se puede escribir sin romper antes otra cosa.
#
# El número está acotado a propósito. Una reserva larga es una lista de
# invariantes que nadie ha revisado, y a partir de cierto tamaño lo útil es
# arreglar la puerta en vez de ampliar la excusa.
RESERVADOS: dict[str, str] = {
    "E0012": "guarda de `Builder.statement` para un tipo de nodo que el "
             "frontend no emite; ningún fuente reaches porque el parser ya no "
             "deja pasar nada que no sepa",
    "E0013": "guarda de `Builder.expr` para una expresión que el typechecker "
             "dejó pasar; es la forma de decir que el bug es del compilador",
    "E0015": "el parser registra una llamada usada como sentencia con tipo VOID, "
             "así que la rama de `check_statement` no se alcanza. Es codigo "
             "preparado para cuando las llamadas como sentencia se implementen",
    "E0017": "`Builder.coerce` lanza cuando el typechecker autorizó una "
             "conversión que el SIR no sabe emitir. Con el `decl_types` por "
             "nombre arreglado no se ha vuelto a ver; si aparece, es un bug",
    "E0057": "`note_placement` avisa de una nativa sin sitio asignado; sólo una "
             "nativa nueva sin registrar en `NATIVE_PLACEMENT` lo dispara",
    "E0058": "guarda del backend para una operación declarada y no emitida. Lo "
             "comprueba `tests/test_wasm_tripwire.py`, que monta el caso de "
             "verdad: una operación nueva sin bajar",
    "E0061": "guarda del backend sobre el orden de los slots; sólo la dispara un "
             "backend que monte mal la firma de una función",
    "E0063": "`PRINT` rechaza lo que no es número, texto, booleano ni array, y "
             "eso deja COLOR fuera... que además `is_numeric` incluye, así que "
             "sólo unreachable con un PTR. Hoy no hay PTR en la lengua",
    "E0065": "el verificador de pila del módulo: sólo salta si el backend emite "
             "tipos equivocados, o sea un bug del backend. Lo comprueba "
             "`tests/test_wasm_backend.py`",
    "E0064": "`check_input` avisa de que `INPUT` necesita el nombre de una "
             "variable, pero el parser ya no acepta ninguna otra cosa: `INPUT "
             "\"hola\"` es `E0010` y `INPUT 1` también. Defensa en profundidad "
             "contra un parser que se solte",
}
