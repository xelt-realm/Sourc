"""Fuzzer diferencial: el intérprete contra el módulo, sobre programas generados.

Es el que más bugs encuentra de todo el repo, y la razón está en qué compara.
Un test de ejemplo compara **un** programa que alguien eligió; este compara
cientos que nadie eligió, y la mitad de los caminos raros de un compilador --
una promoción que nadie probó, un tipo de retorno que se solapa con otro, un
`CASE` que se cuela en un sitio donde `--` hacia menos infinito-- salen por
aquí y no por ningún otro sitio.

Los tres bugs que encontró el round trip --el exponente de los flotantes
normales, los dígitos de la mantisa de f32 y el `0x0.` del subnormal-- eran de
codificación, y ningún fuzzer de los anteriores podía verlos: el del encoder
genera **módulos**, no programas. Esta es la pieza que faltaba.

## Válido por construcción

La parte difícil no es generar, es no ensuciar. Un generador que produce
programas que no compilan llena el fuzzer de errores que no son del backend y
que además apuntan al sitio equivocado. Por eso la generación es **consciente
de tipos**: `expresion(DOUBLE, 2)` devuelve una expresión que *es* un DOUBLE,
compila y además da un valor acotado.

Y hay una razón menos evidente para los límites: **el intérprete usa enteros de
Python y el módulo i64 con desbordamiento**. Un programa que multiplica veinte
veces por cien se sale de i64 en el módulo y no en el intérprete, y eso no es un
bug del backend: es una diferencia conocida entre los dos, y ensuciar el
fuzzer con ella lo haría ilegible. Por eso las magnitudes van acotadas y el
límite está escrito abajo, con su número.

## Determinismo

Semilla fija (ADR 0016): un fallo que sale una vez de cada doscientos no sirve
de nada. `--semilla N` reproduce, y el programa que falla se imprime entero para
poder guardarlo como corpus si interesa.

## Qué compara

stdout, la lista de display normalizada y el número de reservas. Las tres cosas
que el intérprete puede medir y el módulo también. El número de reservas no
está en la salida de un programa: es un contador del runtime, y compararlo
detecta que el módulo reservó de más aunque imprima lo mismo.
"""

from __future__ import annotations

import argparse
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "compiler"))

from sourc import interp, sir                                 # noqa: E402
from sourc.diagnostics import CompileError, DiagnosticBag    # noqa: E402
from sourc.parser import parse                                # noqa: E402
from sourc.types import check                                 # noqa: E402
from sourc.wasm.backend import compile_module                # noqa: E402

SEMILLA = 20261004

# El techo de magnitud de un entero. Por encima de esto el intérprete y el
# módulo empiezan a discrepar por el desbordamiento de i64, que no es un bug del
# backend sino una diferencia entre Python y WASM. Con 10^6 y multiplicaciones
# de profundidad acotada, ningún intermedio llega cerca.
COTA_ENTERO = 10 ** 6

# Los bucles están acotados porque cada vuelta es una instrucción del módulo y
# un paso del intérprete, y el fuzzer corre cientos de programas.
VUELTAS = 5

ALFABETO = "abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,;:!?-+*/()"

TIPO_ENTERO = "INTEGER"
TIPO_REAL = "DOUBLE"
TIPO_CHICO = "SINGLE"
TIPO_BOOL = "BOOLEAN"
TIPO_TEXTO = "STRING"

ESCALARES = (TIPO_ENTERO, TIPO_REAL, TIPO_CHICO, TIPO_BOOL, TIPO_TEXTO)


class Generador:
    """Un programa Source, válido por construcción y con tipos conocidos."""

    def __init__(self, semilla: int):
        self.rng = random.Random(semilla)
        self.lineas: list[str] = []
        # Un `DIM` dentro de un `IF` declara la variable **en ese bloque**, y
        # fuera no existe: `DIM x` dentro de un `IF` y `PRINT x` después es
        # "variable no declarada", y lo dice el typechecker con E0022. El
        # generador tiene que llevar la cuenta de los alcances o produce
        # programas que no compilan.
        self.variables: dict[str, list[str]] = {t: [] for t in ESCALARES}
        self.alcance_actual: dict[str, list[str]] = {t: [] for t in ESCALARES}
        self.pila_alcances: list[dict[str, list[str]]] = []
        self.arrays: list[tuple[str, str]] = []       # (nombre, tipo del elemento)
        self.funciones: list[tuple[str, str]] = []    # (nombre, tipo que devuelve)
        self.contador = 0

    # ------------------------------------------------------------- utilidades

    def nuevo(self, prefijo: str) -> str:
        self.contador += 1
        return f"{prefijo}{self.contador}"

    def entero(self) -> int:
        return self.rng.randint(-50, 50)

    def texto(self) -> str:
        largo = self.rng.randint(0, 12)
        return "".join(self.rng.choice(ALFABETO) for _ in range(largo))

    def literal_texto(self) -> str:
        """Un literal entrecomillado, sin `{` ni `"`.

        El `{` abre una interpolación y se resuelve en el lexer, así que un
        texto con llave genera una expresión que el generador no ha pedido. Y la
        comilla hay que doblarla, que el lexer también lo trata aparte.
        """
        return '"' + self.texto().replace("{", "(").replace("}", ")") + '"'

    def declared(self, tipo: str) -> str | None:
        opciones = self.variables[tipo]
        return self.rng.choice(opciones) if opciones else None

    def abrir(self) -> None:
        """Empieza un alcance. El anterior se apila, no se tira.

        Sustituirlo sin apilar rompe en cuanto hay dos niveles: un `WHILE`
        dentro de un `IF` borra los nombres pendientes del `IF`, y cuando el
        `IF` cierra intenta quitar de `variables` algo que ya no está.
        """
        self.pila_alcances.append(self.alcance_actual)
        self.alcance_actual = {t: [] for t in ESCALARES}

    def cerrar(self) -> None:
        for tipo, nombres in self.alcance_actual.items():
            for nombre in nombres:
                self.variables[tipo].remove(nombre)
        self.alcance_actual = self.pila_alcances.pop()

    def divisor(self) -> str:
        """Un divisor que no puede ser cero.

        Dividir por cero es una trampa en los dos lados, y una trampa no es un bug
        del backend: es el generador produciendo un programa que nadie ejecutaría
        a propósito.

        **Los paréntesis deluden.** ``ABS(x) MOD 7 + 1`` pegado a una división
        entera da ``16 \\ (ABS(x) MOD 7) + 1``, que se lee como
        ``(16 \\ ...) + 1`` --porque la división entera liga más que el ``+``-- y
        el divisor acaba siendo un `MOD` que **sí** puede dar cero. Lo cazó la
        primera ejecución con "división entera por cero".
        """
        base = self.expresion(TIPO_ENTERO, 1)
        return f"((ABS({base}) MOD 7) + 1)"

    # ---------------------------------------------------------- expresiones

    def expresion(self, tipo: str, profundidad: int = 2) -> str:
        """Una expresión de `tipo`, acotada en magnitud si es entera."""
        if tipo == TIPO_ENTERO:
            return self._expresion_entera(profundidad)
        if tipo in (TIPO_REAL, TIPO_CHICO):
            return self._expresion_real(profundidad)
        if tipo == TIPO_BOOL:
            return self._expresion_booleana(profundidad)
        return self._expresion_texto(profundidad)

    def _expresion_entera(self, profundidad: int) -> str:
        dado = self.declared(TIPO_ENTERO)
        opciones = ["lit"]
        if dado:
            opciones += ["var", "var"]
        if self.funciones:
            opciones.append("llamada")
        if self.variables[TIPO_TEXTO]:
            # Sólo `LEN`, que devuelve INTEGER. `VAL` devuelve DOUBLE y no se
            # puede usar donde se espera un INTEGER: Source no estrecha en
            # silencio, y el typechecker lo dice con "un índice necesita
            # INTEGER, no DOUBLE".
            opciones += ["len", "len"]
        eleccion = self.rng.choice(opciones)

        if eleccion == "var":
            return dado
        if eleccion == "llamada":
            nombre, devuelve = self.rng.choice(self.funciones)
            if devuelve != TIPO_ENTERO:
                return self.literal_booleano() and "1" or "0"
            return f"{nombre}({self.expresion(TIPO_ENTERO, 0)})"
        if eleccion == "len":
            return f"LEN({self.expresion(TIPO_TEXTO, 0)})"
        if profundidad <= 0:
            return str(self.entero())

        operator = self.rng.choice(["+", "-", "*", "\\", "DIV", "MOD"])
        izquierda = self.expresion(TIPO_ENTERO, profundidad - 1)
        if operator in ("\\", "DIV", "MOD"):
            return f"({izquierda} {operator} {self.divisor()})"
        if operator == "*":
            # Multiplicar dos cotas uncertainties las multiplican. Con la cota
            # global, dos niveles de profundidad con `*` bastan para acercarse al
            # límite y aquí se corta: se usa una de las partes con la otra
            # partida por un número pequeño.
            return (f"({izquierda} * "
                    f"(ABS({self.expresion(TIPO_ENTERO, 0)}) MOD 50))")
        return f"({izquierda} {operator} {self.expresion(TIPO_ENTERO, profundidad - 1)})"

    def literal_booleano(self) -> bool:
        return bool(self.rng.getrandbits(1))

    def _expresion_real(self, profundidad: int) -> str:
        opciones = ["lit"]
        for tipo in (TIPO_REAL, TIPO_CHICO, TIPO_ENTERO):
            if self.variables[tipo]:
                opciones.append("var")
        if self.variables[TIPO_TEXTO]:
            opciones.append("val")
        eleccion = self.rng.choice(opciones)

        if eleccion == "var":
            return self.declared(self.rng.choice(
                [t for t in (TIPO_REAL, TIPO_CHICO, TIPO_ENTERO)
                 if self.variables[t]]))
        if profundidad <= 0:
            return f"{self.rng.randint(-20, 20)}.{self.rng.randint(0, 99)}"

        operator = self.rng.choice(["+", "-", "*", "/"])
        izquierda = self.expresion(TIPO_REAL, profundidad - 1)
        if operator == "/":
            return f"({izquierda} / {self.divisor()})"
        if operator == "*":
            return (f"({izquierda} * "
                    f"{self.rng.randint(1, 20)}.5)")
        return (f"({izquierda} {operator} "
                f"{self.expresion(TIPO_REAL, profundidad - 1)})")

    def _expresion_booleana(self, profundidad: int) -> str:
        # `AND` y `OR` sólo entran en el sorteo si queda profundidad para
        # recursionar. Si no --y antes no se comprobaba-- caían al final de la
        # función y emitían `(38 - 0) OR 0`, que no es un BOOLEAN y el
        # typechecker rechaza con "OR necesita BOOLEAN, no INTEGER".
        operadores = ["=", "<>", "<", ">", "<=", ">="]
        if profundidad > 0:
            operadores += ["AND", "OR"]
        operador = self.rng.choice(operadores)
        if operador in ("AND", "OR"):
            return (f"({self._expresion_booleana(profundidad - 1)} {operador} "
                    f"{self._expresion_booleana(profundidad - 1)})")
        if operador == "=" and self.rng.random() < 0.3:
            return (f"({self.expresion(TIPO_TEXTO, 0)} = "
                    f"{self.expresion(TIPO_TEXTO, 0)})")
        izquierda = self.expresion(
            TIPO_ENTERO if self.rng.random() < 0.6 else TIPO_REAL, 1)
        derecha = self.expresion(
            TIPO_ENTERO if self.rng.random() < 0.6 else TIPO_REAL, 1)
        return f"({izquierda} {operador} {derecha})"

    def _expresion_texto(self, profundidad: int) -> str:
        opciones = ["lit"]
        if self.variables[TIPO_TEXTO]:
            opciones += ["var", "var"]
        if profundidad <= 0:
            return self.literal_texto()

        eleccion = self.rng.choice(opciones + [
            "concat", "concat", "left", "right", "mid", "str", "interp"])
        if eleccion == "var":
            return self.declared(TIPO_TEXTO)
        if eleccion == "concat":
            return (f"({self.expresion(TIPO_TEXTO, profundidad - 1)} & "
                    f"{self.expresion(TIPO_TEXTO, profundidad - 1)})")
        if eleccion == "left":
            return (f"LEFT({self.expresion(TIPO_TEXTO, 0)}, "
                    f"{self.rng.randint(0, 6)})")
        if eleccion == "right":
            return (f"RIGHT({self.expresion(TIPO_TEXTO, 0)}, "
                    f"{self.rng.randint(0, 6)})")
        if eleccion == "mid":
            return (f"MID({self.expresion(TIPO_TEXTO, 0)}, "
                    f"{self.rng.randint(0, 4)}, {self.rng.randint(0, 4)})")
        if eleccion == "str":
            tipo = self.rng.choice([TIPO_ENTERO, TIPO_REAL, TIPO_BOOL])
            return f"STR({self.expresion(tipo, 1)})"
        return (f'"{{{self.expresion(TIPO_ENTERO, 0)}}}"')

    # ---------------------------------------------------------- sentencias

    def declarar(self, tipo: str) -> str:
        nombre = self.nuevo("v")
        valor = self.expresion(tipo, 1)
        self.lineas.append(f"DIM {nombre} AS {tipo} = {valor}")
        self.variables[tipo].append(nombre)
        self.alcance_actual[tipo].append(nombre)
        return nombre

    def imprimir(self) -> None:
        piezas = []
        for _ in range(self.rng.randint(1, 3)):
            tipo = self.rng.choice(ESCALARES)
            piezas.append(self.expresion(tipo, 1))
        self.lineas.append("PRINT " + "; ".join(piezas))

    def si(self, profundidad: int) -> None:
        self.lineas.append(f"IF {self.expresion(TIPO_BOOL, 1)} THEN")
        self.abrir()
        self.cuerpo(1, profundidad)
        self.cerrar()
        if self.rng.random() < 0.4:
            self.lineas.append(f"ELSE IF {self.expresion(TIPO_BOOL, 1)} THEN")
            self.abrir()
            self.cuerpo(1, profundidad)
            self.cerrar()
        if self.rng.random() < 0.5:
            self.lineas.append("ELSE")
            self.abrir()
            self.cuerpo(1, profundidad)
            self.cerrar()
        self.lineas.append("END IF")

    def mientras(self, profundidad: int) -> None:
        contador = self.nuevo("i")
        self.lineas.append(f"DIM {contador} AS INTEGER = 0")
        self.lineas.append(f"WHILE {contador} < {VUELTAS}")
        self.lineas.append(f"  {contador} = {contador} + 1")
        self.abrir()
        self.cuerpo(1, profundidad)
        self.cerrar()
        self.lineas.append("WEND")

    def para(self, profundidad: int) -> None:
        variable = self.nuevo("k")
        paso = self.rng.choice(["", " STEP 2"])
        self.lineas.append(f"FOR {variable} = 0 TO {VUELTAS - 1}{paso}")
        self.abrir()
        self.cuerpo(1, profundidad)
        self.cerrar()
        self.lineas.append("NEXT")

    def array(self) -> None:
        nombre = self.nuevo("a")
        elemento = self.rng.choice([TIPO_ENTERO, TIPO_REAL, TIPO_CHICO,
                                     TIPO_TEXTO, TIPO_BOOL])
        tamano = self.rng.randint(1, 5)
        if self.rng.random() < 0.5:
            inicial = ", ".join(
                self._inicial(elemento) for _ in range(tamano))
            self.lineas.append(f"DIM {nombre} AS ARRAY OF {elemento} = "
                               f"[{inicial}]")
        else:
            self.lineas.append(f"DIM {nombre} AS ARRAY OF {elemento} = "
                               f"NEW {elemento}[{tamano}]")
        self.arrays.append((nombre, elemento))
        indice = self._indice(nombre)
        self.lineas.append(f"{nombre}[{indice}] = {self._inicial(elemento)}")
        self.lineas.append(f"PRINT {nombre}[{indice}]; \" \"; LEN({nombre})")

    def _indice(self, nombre: str) -> str:
        """Un índice dentro del array.

        `MOD` puede ser negativo en el intérprete y en el módulo, y un índice
        negativo es una trampa en los dos. El `ABS` lo vuelve inocuo.
        """
        return (f"ABS({self.expresion(TIPO_ENTERO, 0)}) "
                f"MOD LEN({nombre})")

    def _inicial(self, tipo: str) -> str:
        """Un valor literal del tipo, para un literal de array."""
        if tipo == TIPO_ENTERO:
            return str(self.entero())
        if tipo in (TIPO_REAL, TIPO_CHICO):
            return f"{self.rng.randint(-9, 9)}.{self.rng.randint(0, 99)}"
        if tipo == TIPO_BOOL:
            return "TRUE" if self.rng.getrandbits(1) else "FALSE"
        return self.literal_texto()

    def funcion(self) -> None:
        nombre = self.nuevo("f")
        devuelve = self.rng.choice([TIPO_ENTERO, TIPO_REAL, TIPO_BOOL,
                                    TIPO_TEXTO])
        # Una FUNCTION también es un alcance: lo que se declara dentro no se ve
        # desde fuera, ni desde la siguiente. Se guardan **los dos** diccionarios
        # porque restaurar sólo `variables` deja `alcance_actual` con nombres que
        # ya no existen, y `cerrar()` revienta al quitar uno.
        antes = {t: list(v) for t, v in self.variables.items()}
        alcance_antes = {t: list(v) for t, v in self.alcance_actual.items()}
        arrays_antes = list(self.arrays)

        # El cuerpo se genera con las variables **vacías**: una FUNCTION no ve
        # los globales del programa (E0067, y con razón: en WASM cada función
        # tiene sus propios locales, así que un global no se puede compartir).
        # Lo único visible es el parámetro `p`.
        self.variables = {t: [] for t in ESCALARES}
        self.variables[TIPO_ENTERO] = ["p"]
        self.arrays = []

        self.lineas.append(f"FUNCTION {nombre}(p AS INTEGER) AS {devuelve}")
        self.lineas.append("  DIM r AS " + devuelve + " = " +
                           self.expresion(devuelve, 1))
        if self.rng.random() < 0.5:
            self.lineas.append("  IF p > 0 THEN")
            self.lineas.append(f"    r = {self.expresion(devuelve, 1)}")
            self.lineas.append("  END IF")
        self.lineas.append("  RETURN r")
        self.lineas.append("END FUNCTION")
        self.variables = antes
        self.alcance_actual = alcance_antes
        self.arrays = arrays_antes
        self.funciones.append((nombre, devuelve))

    def cuerpo(self, sangria: int, profundidad: int) -> None:
        espacio = "  " * sangria
        for _ in range(self.rng.randint(1, 3)):
            eleccion = self.rng.random()
            if eleccion < 0.30:
                self.declarar(self.rng.choice(ESCALARES))
            elif eleccion < 0.50:
                self.imprimir()
            elif eleccion < 0.62:
                tipo = self.rng.choice(ESCALARES)
                candidatos = self.variables[tipo]
                if candidatos:
                    self.lineas.append(
                        f"{self.rng.choice(candidatos)} = "
                        f"{self.expresion(tipo, 1)}")
            elif eleccion < 0.72 and profundidad > 0:
                self.si(profundidad - 1)
            elif eleccion < 0.80:
                self.mientras(profundidad)
            elif eleccion < 0.88:
                self.para(profundidad)
            elif eleccion < 0.94:
                self.array()
            elif self.funciones:
                nombre, devuelve = self.rng.choice(self.funciones)
                self.lineas.append(
                    f"PRINT {nombre}(ABS({self.expresion(TIPO_ENTERO, 0)}) "
                    f"MOD 3)")
            else:
                self.imprimir()

    def programa(self) -> str:
        self.lineas.append(f"' Programa generado por tools/fuzz_source.py")
        self.lineas.append(f"' No se versiona: se reproduce con --semilla")
        for _ in range(self.rng.randint(1, 3)):
            self.declarar(self.rng.choice(ESCALARES))
        for _ in range(self.rng.randint(0, 2)):
            self.funcion()
        self.cuerpo(0, 2)
        self.lineas.append("PRINT \"fin\"")
        return "\n".join(self.lineas) + "\n"


# ------------------------------------------------------------- comparación


def _display_de(resultado) -> list[str]:
    """La lista de dibujo del intérprete, como el runner de Node la escribe."""
    lineas = []
    for entrada in resultado.display_list:
        if isinstance(entrada, tuple) and len(entrada) == 2:
            if entrada[0] in ("screen", "screen_off", "flip"):
                continue
            lineas.append(f"{entrada[0]} "
                          + " ".join(str(v) for v in entrada[1]))
    return lineas


def _normaliza(lineas: list[str]) -> list[str]:
    """Compara **valores**, no su texto.

    El intérprete guarda un SINGLE como float de Python y lo imprime `32.0`;
    JavaScript tiene un solo número y lo imprime `32`. Los dos son el mismo
    valor, y comparar los textos daría una diferencia en cada línea con
    decimales sin que nada esté mal.
    """
    salida = []
    for linea in lineas:
        tokens = []
        for token in linea.split(" "):
            try:
                tokens.append(repr(float(token)))
                continue
            except ValueError:
                pass
            tokens.append(token)
        salida.append(" ".join(tokens))
    return salida


class FallaGenerador(Exception):
    """El generador produjo algo que no compila, y no es culpa del backend."""


def preparar(fuente: str):
    """Compila el fuente. Si falla, es del generador."""
    bag = DiagnosticBag()
    try:
        program = parse(fuente, bag)
    except CompileError as error:
        raise FallaGenerador(f"no parsea: {error.diagnostic.message}") from None
    if bag.has_errors():
        raise FallaGenerador("no parsea: "
                             + "; ".join(d.message for d in bag.errors))
    typing = check(program, bag)
    if bag.has_errors():
        raise FallaGenerador("no typecheckea: "
                             + "; ".join(d.message for d in bag.errors))
    try:
        return sir.build(program, typing, "fuzz", "1.0")
    except CompileError as error:
        # El lowering también puede rehusar lo que el typechecker dejó pasar.
        # Sin este `try` el error sale como una excepción y el fuzzer se cae
        # entero en vez de decir que el programa es suyo.
        raise FallaGenerador(f"no baja a IR: {error.diagnostic.message}"
                             + (f"\n  {error.diagnostic.suggestion}"
                                if error.diagnostic.suggestion else "")) from None


def ejecutar_en_node(crudo: bytes, timeout: int = 30) -> tuple[str, list[str]]:
    """El módulo en Node con el pegamento real.

    Devuelve `(stdout, display)`. Reutiliza `parity` para no mantener un tercer
    pegamento: tres copias del mismo host divergen, y el bug acaba siendo de la
    copia.
    """
    sys.path.insert(0, str(RAIZ / "tools"))
    import parity                                    # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / "programa.wasm"
        ruta.write_bytes(crudo)
        return parity.ejecutar_en_node(crudo, frames=1)


def comparar(fuente: str) -> tuple[bool, str]:
    """Intérprete contra módulo. Devuelve (pasa, detalle)."""
    module = preparar(fuente)

    try:
        referencia = interp.run(module, max_frames=1)
    except interp.RuntimeFault as error:
        # Una trampa es del generador, no del backend: el programa no debería
        # haber podido llegar ahí. Se propaga como `FallaGenerador` para que
        # cuente aparte y no se mezcle con las diferencias de semántica.
        raise FallaGenerador(f"el intérprete hace trampa: {error}") from None
    except Exception as error:                     # noqa: BLE001
        # Cualquier otra cosa es un **bug**, no un programa malo: un fuente que
        # typecheckea no puede reventar el intérprete con un `KeyError` a mil
        # líneas de distancia. Se devuelve como fallo para que se imprima el
        # programa entero, que es la mitad del valor de un fuzzer.
        return (False, f"el intérprete revienta: "
                       f"{type(error).__name__}: {error}")

    try:
        crudo = compile_module(module)
    except CompileError as error:
        return (False, f"no baja: {error.diagnostic.message}\n"
                       f"  {error.diagnostic.suggestion.splitlines()[0]}")
    except Exception as error:                     # noqa: BLE001
        return (False, f"no baja: {type(error).__name__}: {error}")

    try:
        salida, display = ejecutar_en_node(crudo)
    except Exception as error:                     # noqa: BLE001
        return (False, f"no ejecuta en node: {type(error).__name__}: {error}")

    diferencias = []
    if salida != referencia.output:
        diferencias.append(
            f"stdout distinto\n  intérprete: {referencia.output!r}\n"
            f"  módulo:     {salida!r}")
    esperado = _normaliza([l for l in _display_de(referencia) if l])
    obtenido = _normaliza([l for l in display.splitlines()
                           if l and not l.startswith(("screen ", "screen_off",
                                                       "flip"))])
    if esperado != obtenido:
        diferencias.append(
            "dibujo distinto\n  intérprete: " + " | ".join(esperado) +
            "\n  módulo:     " + " | ".join(obtenido))
    if diferencias:
        return (False, "\n".join(diferencias))
    return (True, f"{len(crudo)} B")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fuzz_source")
    parser.add_argument("--n", type=int, default=100,
                        help="cuántos programas (por defecto 100)")
    parser.add_argument("--semilla", type=int, default=SEMILLA,
                        help=f"semilla (por defecto {SEMILLA})")
    parser.add_argument("--parar-en-generador", action="store_true",
                        help="sale en el primer programa que el generador "
                             "produce mal, para depurarlo")
    parser.add_argument("--ver", type=int, metavar="N",
                        help="imprime el programa número N y sale")
    parser.add_argument("--sin-ratchet", action="store_true",
                        help="falla también con las diferencias ya registradas "
                             "en tests/fuzz_diffs.json, para verlas todas")
    args = parser.parse_args(argv)

    if shutil_which("node") is None:
        print("node no está: el fuzzer compara contra el módulo y necesita "
              "ejecutarlo")
        return 2

    if args.ver is not None:
        print(Generador(args.semilla + args.ver).programa())
        return 0

    inicio = time.monotonic()
    fallos_generador = 0
    fallos = 0
    # Las diferencias que ya están en el ratchet se cuentan aparte. Un
    # `--tolerate N` no sabría cuáles son ni por qué; aquí cada una tiene su
    # semilla y su motivo en `tests/fuzz_diffs.json`, y `tests/test_ratchet.py`
    # exige que la lista sólo pueda encogerse.
    import fuzz_diffs                                     # noqa: PLC0415

    conocidas = fuzz_diffs.diferencias()
    toleradas = 0
    for numero in range(args.n):
        semilla = args.semilla + numero
        fuente = Generador(semilla).programa()
        try:
            pasa, detalle = comparar(fuente)
        except FallaGenerador as error:
            # Un programa que el generador produce y que no compila --o que hace
            # trampa-- es un bug **del generador**, y se cuenta aparte:
            # mezclarlo con los fallos del backend haría que el fuzzer apuntara
            # al sitio equivocado.
            fallos_generador += 1
            print(f"  generador  semilla {semilla}: {error}")
            print("\n".join(f"      {l}" for l in fuente.splitlines()[:12]))
            if args.parar_en_generador:
                print("  (--parar-en-generador)")
                return 1
            continue
        if not pasa:
            if not args.sin_ratchet and str(semilla) in conocidas:
                toleradas += 1
                continue
            fallos += 1
            print(f"  FALLA  semilla {semilla}\n{detalle}")
            print("\n".join(f"      {l}" for l in fuente.splitlines()))
            print(f"  reproducir: python3 tools/fuzz_source.py --ver "
                  f"{numero} --semilla {args.semilla}")

    segundos = time.monotonic() - inicio
    total = args.n
    print(f"\n{total - fallos - fallos_generador - toleradas}/{total} programas "
          f"coinciden en {segundos:.1f}s")
    if fallos_generador:
        print(f"{fallos_generador} programa(s) del generador no compilaron")
    if toleradas:
        print(f"{toleradas} diferencia(s) conocidas, registradas en "
              f"tests/fuzz_diffs.json")
    if fallos:
        print(f"{fallos} diferencia(s) nueva(s) entre intérprete y módulo")
    return 1 if (fallos or fallos_generador) else 0


def shutil_which(programa: str):
    import shutil
    return shutil.which(programa)


if __name__ == "__main__":
    raise SystemExit(main())
