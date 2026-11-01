"""Texto WAT → módulo WASM.

Es la segunda mitad del oráculo de `wat.py`. Los dos son implementaciones
independientes del mismo formato, y que coincidan byte a byte es el chequeo
estructural más fuerte disponible sin herramientas externas: si el encoder
tiene un error de LEB128, de orden de secciones o de agrupado de locales, el
reenensamblado no puede coincidir y sale en el test.

Y es lo que ADR 0014 pide para `runtime/native/*.wat`: un ensamblador propio,
sin clang y sin Rust, para que el runtime se pueda compilar con el compilador
del proyecto cuando se auto-hospede.

## El subconjunto

ADR 0014 limita el subconjunto a lo que el runtime necesita --tipos escalares,
locals, load/store, constantes enteras, aritmética, `call` y `return`-- y deja
fuera bloques, tablas, GC y control estructurado.

Ese listado se escribió cuando el ensamblador sólo servía para el runtime. Este
también sirve para el round trip, y el round trip necesita bloques: todos los
programas del corpus tienen `IF` y `WHILE`. Así que el subconjunto **crece**,
con una condición: los ficheros de `runtime/native/` siguen siendo rectos,
porque el límite de 1 KB por módulo es lo que los mantiene PEQUEÑOS.

La línea que separa "el runtime usa esto" de "el round trip necesita esto" está
en `SUBTUJENO_RIZADO`: lo que no esté en la lista se puede ensamblar, pero el
error lo dice.

## Lo que no se adivina

Un texto que no se entiende es un error con la línea, no un opcode inventado. Un
ensamblador que salta un token desconocido produce un módulo que **valida** y
calcula otra cosa, que es el peor sitio para descubrirlo.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import encoder as w

# La parte que ADR 0014 llama "el subconjunto": lo que un módulo de
# `runtime/native/` puede usar. Un ensamblaje que se salga de aquí avisa, porque
# la限制 de 1 KB por módulo sólo tiene sentido si los módulos son rectos.
SUBTUJENO_RIZADO = frozenset({
    "local.get", "local.set", "local.tee", "global.get", "global.set",
    "i32.const", "i64.const", "f32.const", "f64.const",
    "i32.add", "i32.sub", "i32.mul", "i32.and", "i32.or", "i32.xor",
    "i32.shl", "i32.shr_s", "i32.shr_u",
    "i64.add", "i64.sub", "i64.mul", "i64.and", "i64.or", "i64.xor",
    "i64.shl", "i64.shr_s", "i64.shr_u",
    "call", "return", "drop", "select",
    "i32.eq", "i32.ne", "i32.lt_s", "i32.gt_s", "i32.le_s", "i32.ge_s",
    "i64.eq", "i64.ne", "i64.lt_s", "i64.gt_s", "i64.le_s", "i64.ge_s",
    "i32.wrap_i64", "i64.extend_i32_s", "i64.extend_i32_u",
    "f64.promote_f32", "f32.demote_f64",
    "i32.trunc_f64_s", "i64.trunc_f64_s", "f64.convert_i64_s",
})

# Las de memoria y bloque están fuera del conjunto "rizado" pero son parte del
# formato: un módulo de runtime necesita `memory.copy` y `memory.fill`, y el
# round trip necesita todo lo que el backend emite.
# Memoria y control, que están fuera del "rizado" pero son parte del formato.
# Un módulo de runtime **sí** los necesita: `memory.copy` y `memory.fill` son
# el motivo de existir de `runtime/native/`, porque con el MVP son dos bucles de
# instrucciones y sin él son dos instrucciones.
MEMORIA = frozenset({
    "i32.load", "i64.load", "f32.load", "f64.load",
    "i32.load8_u", "i32.load16_u", "i64.load8_u", "i64.load32_u",
    "i32.store", "i64.store", "f32.store", "f64.store",
    "i32.store8", "i32.store16",
    "memory.copy", "memory.fill", "memory.size", "memory.grow",
})

# Control estructurado. Fuera del rizado por ADR 0014, y no por capricho: un
# bucle son unas 40 líneas de WAT para 20 bytes de binario, y lo que cuesta no
# es el tamaño --que lo acota el techo-- sino que corregirlo exige mantener el
# estado de la pila en la cabeza.
CONTROL_ESTRUCTURADO = frozenset({
    "block", "loop", "if", "else", "end", "br", "br_if",
    "unreachable", "nop",
})


class WatEnsamblarError(Exception):
    """El texto no se puede ensamblar, y el error dice en qué línea."""

    def __init__(self, mensaje: str, linea: int = 0):
        super().__init__(f"línea {linea}: {mensaje}" if linea else mensaje)
        self.linea = linea


@dataclass
class _Linea:
    numero: int
    texto: str

    @property
    def limpio(self) -> str:
        """Sin el `;;` de comentario. Los comentarios del emisor son issuance
        sobre el módulo, no parte de él, y si se leyeran como código el
        round trip no significaría nada."""
        return self.texto.split(";;")[0].strip()


def _partes(texto: str) -> list[str]:
    """Trocea una línea en palabras, respetando comillas y paréntesis.

    Un `split()` a secas rompe los nombres entrecomillados de los imports, que
    son `"source"` y `"print_str"`, y un ensamblador que lee `source` sin las
    comillas genera un módulo con otros nombres de import.
    """
    palabras: list[str] = []
    actual: list[str] = []
    dentro = False
    escapado = False
    for caracter in texto:
        if escapado:
            actual.append(caracter)
            escapado = False
            continue
        if caracter == "\\":
            actual.append(caracter)
            escapado = True
            continue
        if caracter == '"':
            dentro = not dentro
            actual.append(caracter)
            continue
        if caracter in " \t" and not dentro:
            if actual:
                palabras.append("".join(actual))
                actual = []
            continue
        actual.append(caracter)
    if actual:
        palabras.append("".join(actual))
    return palabras


def _desescapar(texto: str) -> bytes:
    """El interior de una cadena WAT, como bytes.

    Los escapes son de **byte**, no de carácter: un `\\80` es un byte 0x80, que
    no es válido en UTF-8 y sólo se puede escribir escapado. `bytes.decode` en
    el camino de vuelta lo rompería.
    """
    cuerpo = texto[1:-1]
    salida = bytearray()
    i = 0
    while i < len(cuerpo):
        caracter = cuerpo[i]
        if caracter != "\\":
            salida.extend(caracter.encode("utf-8"))
            i += 1
            continue
        i += 1
        if i >= len(cuerpo):
            raise WatEnsamblarError("escape sin nada detrás")
        clase = cuerpo[i]
        escapes = {"n": 10, "t": 9, "r": 13, '"': 34, "\\": 92, "'": 39}
        if clase in escapes:
            salida.append(escapes[clase])
            i += 1
        elif clase == "u":
            fin = cuerpo.index("}", i)
            salida.extend(chr(int(cuerpo[i + 2:fin], 16)).encode("utf-8"))
            i = fin + 1
        else:                                          # \XX hexadecimal
            salida.append(int(cuerpo[i:i + 2], 16))
            i += 2
    return bytes(salida)


def _sin_parentesis(palabras: list[str]) -> list[str]:
    """Quita los paréntesis de envoltura y las comillas de los nombres.

    El emisor escribe `(local.get 4)` y el ensamblador necesita `local.get 4`.
    Quitar los paréntesis de una vez, en vez de por instrucción, evita tener que
    decidir en cada token si abre o cierra.
    """
    limpio = []
    for palabra in palabras:
        if palabra in ("(", ")", ""):
            continue
        if palabra.startswith('"') and palabra.endswith('"'):
            limpio.append(palabra)
        else:
            limpio.append(palabra.strip("()"))
    return limpio


def _balance(texto: str) -> None:
    """Los paréntesis tienen que cuadrar **en el texto entero**, no por línea.

    Comprobarlo por línea parece más Early y falla con el primer WAT legítimo:
    `(module` abre y su `)` está veinte líneas más abajo. Un WAT real pone una
    s-expresión por línea y los paréntesis no se equilibran hasta el final.
    """
    if texto.count("(") != texto.count(")"):
        raise WatEnsamblarError(
            f"paréntesis descompensados en el módulo: "
            f"{texto.count('(')} de abre y {texto.count(')')} de cierra")


def _float_desde_texto(texto: str) -> float:
    """La forma hexadecimal de la especificación, leída aquí de nuevo.

    Es una segunda implementación a propósito: si compartiera el conversor con
    el emisor, el round trip no compararía nada. `float.fromhex` cubre el
    número; `inf`, `nan` y `nan:0x...` los trata a mano porque no los acepta.

    **Un decimal no va por `fromhex`.** `float.fromhex("1.5")` es 1.3125, porque
    lee los dígitos como hexadecimales: la forma sin `0x` y sin `p` de la
    especificación es decimal, y mandarla al lector hexadecimal convierte cada
    literal escrito a mano. Salía un módulo que valida y devuelve otro número.
    """
    limpio = texto.strip()
    if limpio in ("inf", "+inf"):
        return float("inf")
    if limpio == "-inf":
        return float("-inf")
    if limpio.startswith("nan"):
        return float("nan")
    es_hexadecimal = limpio.lower().startswith(("0x", "-0x", "+0x")) or "p" in limpio
    try:
        return float.fromhex(limpio) if es_hexadecimal else float(limpio)
    except ValueError as error:
        raise WatEnsamblarError(f"literal flotante {texto!r}: {error}") from None


# ------------------------------------------------------------- el ensamblador


def ensamblar(texto: str, *, rizado: bool = False) -> bytes:
    """Texto WAT → bytes del módulo.

    `rizado=True` avisa de cualquier instrucción fuera de `SUBTUJENO_RIZADO`,
    que es el límite que ADR 0014 pone a los módulos de `runtime/native/`. El
    round trip lo usa sin `rizado`, porque tiene que poder meter bloques.
    """
    lineas = _tokenizar(texto)
    estado = _Estado(rizado=rizado)
    for linea in lineas:
        _una_linea(linea, estado)
    if not estado.cerrado:
        raise WatEnsamblarError("falta el `)` que cierra el `(module`")
    return _serializar(estado)


def _tokenizar(texto: str) -> list[_Linea]:
    """Una línea por línea, con el número para el mensaje de error.

    Los comentarios `;;` se conservan en la línea y se podan al leer: el emisor
    los pone como issuance sobre el módulo, y leerlos como código haría que el
    round trip no significara nada.
    """
    salida = []
    for numero, cruda in enumerate(texto.split("\n"), start=1):
        limpia = cruda.split(";;")[0].strip()
        if not limpia:
            continue
        salida.append(_Linea(numero, limpia))
    _balance(texto)
    return salida


@dataclass
class _Estado:
    firmas: list = None
    imports: list = None
    memoria: list = None
    globals_: list = None
    exports: list = None
    inicio: int | None = None
    funciones: list = None
    datos: list = None
    tablas: list = None
    rizado: bool = False
    cerrado: bool = False
    locales: list = None
    # `None` significa "no hay ninguna función abierta". **No** puede ser una
    # lista vacía: con `[]` el estado parece una función abierta desde el
    # principio, y la primera `(func` del módulo se lee como una segunda.
    cuerpo: list | None = None
    tipo_actual: int = 0

    def __post_init__(self):
        self.firmas = self.firmas if self.firmas is not None else []
        self.imports = self.imports if self.imports is not None else []
        self.memoria = self.memoria if self.memoria is not None else []
        self.globals_ = self.globals_ if self.globals_ is not None else []
        self.exports = self.exports if self.exports is not None else []
        self.funciones = self.funciones if self.funciones is not None else []
        self.datos = self.datos if self.datos is not None else []
        self.tablas = self.tablas if self.tablas is not None else []
        self.locales = self.locales if self.locales is not None else []


def _una_linea(linea: _Linea, estado: _Estado) -> None:
    limpia = linea.limpio
    # El `)` que cierra el módulo se mira **antes** de quitar paréntesis: es un
    # token de estructura, y al quitarlo queda una línea vacía que no dice nada.
    if limpia == ")":
        if estado.cuerpo is not None:
            _cierra_funcion(estado)
            return
        estado.cerrado = True
        return

    palabras = _sin_parentesis(_partes(limpia))
    if not palabras:
        return
    cabeza = palabras[0]

    if cabeza == "module":
        return
    if cabeza == "type":
        estado.firmas.append(_una_firma(palabras, linea.numero))
        return
    if cabeza == "import":
        estado.imports.append(_un_import(palabras, linea.numero))
        return
    if cabeza == "table":
        estado.tablas.append(int(palabras[-1]))
        return
    if cabeza == "memory":
        estado.memoria.append(" ".join(palabras[1:]))
        return
    if cabeza == "global":
        estado.globals_.append(" ".join(palabras[1:]))
        return
    if cabeza == "export":
        estado.exports.append(_una_exportacion(palabras, linea.numero))
        return
    if cabeza == "start":
        estado.inicio = int(palabras[1])
        return
    if cabeza == "func":
        _abre_funcion(palabras, linea.numero, estado)
        return
    if cabeza == "data":
        estado.datos.append(_un_dato(palabras, linea.numero))
        return

    if estado.cuerpo is not None:
        estado.cuerpo.append(_instruccion(palabras, linea.numero, estado))
        return

    raise WatEnsamblarError(f"'{cabeza}' no se espera aquí", linea.numero)


VALTIPO = {"i32": 0x7F, "i64": 0x7E, "f32": 0x7D, "f64": 0x7C}


def _una_firma(palabras: list[str], numero: int) -> tuple[list[str], list[str]]:
    if "func" not in palabras:
        raise WatEnsamblarError("una firma que no dice `func`", numero)
    params: list[str] = []
    results: list[str] = []
    destino = None
    for palabra in palabras:
        if palabra == "param":
            destino = params
            continue
        if palabra == "result":
            destino = results
            continue
        if palabra == "func":
            continue
        # `param` y `result` toman **una lista** de tipos, no uno: `(param i32
        # i32)` son dos parámetros. Consumir uno por palabra dejaba la firma con
        # un tipo de menos, y el módulo reensamblado era un byte más corto que
        # el original --una diferencia de longitud que el validador no señala
        # porque el tipo siguiente se reinterpretaría--.
        if destino is not None:
            destino.append(palabra)
    for tipo in params + results:
        if tipo not in VALTIPO:
            raise WatEnsamblarError(f"tipo {tipo!r} fuera del subconjunto: "
                                    f"sólo {', '.join(sorted(VALTIPO))}", numero)
    return (params, results)


def _un_import(palabras: list[str], numero: int) -> tuple[str, str, int]:
    comillas = [p for p in palabras if p.startswith('"')]
    if len(comillas) != 2:
        raise WatEnsamblarError("un import necesita dos nombres entrecomillados",
                                numero)
    if "type" not in palabras:
        raise WatEnsamblarError("sólo se ensamblan imports de función, que es "
                                "lo único que el backend emite", numero)
    tipo = palabras[palabras.index("type") + 1]
    return (_desescapar(comillas[0]).decode("utf-8"),
            _desescapar(comillas[1]).decode("utf-8"),
            int(tipo))


def _una_exportacion(palabras: list[str], numero: int) -> tuple[str, int, int]:
    nombre = next((p for p in palabras if p.startswith('"')), None)
    if nombre is None:
        raise WatEnsamblarError("una exportación sin nombre", numero)
    try:
        donde = palabras.index("func") if "func" in palabras else \
            palabras.index("memory")
    except ValueError:
        raise WatEnsamblarError("sólo se ensamblan exportaciones a función y "
                                "a memoria, que es lo que el backend emite",
                                numero) from None
    kind = 0 if palabras[donde] == "func" else 2
    return (_desescapar(nombre).decode("utf-8"), kind,
            int(palabras[donde + 1]))


def _abre_funcion(palabras: list[str], numero: int, estado: _Estado) -> None:
    if estado.cuerpo is not None:
        raise WatEnsamblarError("una función dentro de otra", numero)
    if "type" not in palabras:
        raise WatEnsamblarError("una función sin `(type N)`, que es como las "
                                "declara el emisor", numero)
    estado.tipo_actual = int(palabras[palabras.index("type") + 1])
    estado.locales = []
    if "local" in palabras:
        estado.locales = [t for t in palabras[palabras.index("local") + 1:]
                          if t in VALTIPO]
        for tipo in palabras[palabras.index("local") + 1:]:
            if tipo not in VALTIPO:
                raise WatEnsamblarError(f"local de tipo {tipo!r}", numero)
    estado.cuerpo = []


def _cierra_funcion(estado: _Estado) -> None:
    estado.funciones.append((estado.tipo_actual, list(estado.locales),
                             list(estado.cuerpo)))
    estado.cuerpo = None
    estado.locales = []


def _un_dato(palabras: list[str], numero: int) -> tuple[int, bytes]:
    if "i32.const" not in palabras:
        raise WatEnsamblarError("un segmento de datos cuyo desplazamiento no "
                                "es un `i32.const`", numero)
    offset = int(palabras[palabras.index("i32.const") + 1])
    crudos = [p for p in palabras if p.startswith('"')]
    if len(crudos) != 1:
        raise WatEnsamblarError("un segmento de datos con más o menos de una "
                                "cadena", numero)
    return (offset, _desescapar(crudos[0]))


def palabra_a_piezas(palabras: list[str]) -> list[str]:
    """Las palabras de una instrucción, tal cual.

    Se devuelven tal cual porque una instrucción tiene un número variable de
    inmediatos y es la tabla de opcodes la que sabe cuántos; separarlas aquí
    sería repetir esa tabla en dos sitios.
    """
    return palabras


# El opcode de cada nombre WAT. Es el **inverso** de la tabla del emisor
# y se escribe a mano: derivarla de aquélla haría que las dos mitades
# no fueran dos.
_CODIGO: dict = {
    'unreachable': 0x00,
    'nop': 0x01,
    'block': 0x02,
    'loop': 0x03,
    'if': 0x04,
    'else': 0x05,
    'end': 0x0B,
    'br': 0x0C,
    'br_if': 0x0D,
    'return': 0x0F,
    'call': 0x10,
    'drop': 0x1A,
    'select': 0x1B,
    'local.get': 0x20,
    'local.set': 0x21,
    'local.tee': 0x22,
    'global.get': 0x23,
    'global.set': 0x24,
    'table.get': 0x25,
    'table.set': 0x26,
    'i32.load': 0x28,
    'i64.load': 0x29,
    'f32.load': 0x2A,
    'f64.load': 0x2B,
    'i32.load8_s': 0x2C,
    'i32.load8_u': 0x2D,
    'i32.load16_s': 0x2E,
    'i32.load16_u': 0x2F,
    'i64.load8_s': 0x30,
    'i64.load8_u': 0x31,
    'i64.load16_s': 0x32,
    'i64.load16_u': 0x33,
    'i64.load32_s': 0x34,
    'i64.load32_u': 0x35,
    'i32.store': 0x36,
    'i64.store': 0x37,
    'f32.store': 0x38,
    'f64.store': 0x39,
    'i32.store8': 0x3A,
    'i32.store16': 0x3B,
    'i64.store8': 0x3C,
    'i64.store16': 0x3D,
    'i64.store32': 0x3E,
    'memory.size': 0x3F,
    'memory.grow': 0x40,
    'i32.const': 0x41,
    'i64.const': 0x42,
    'f32.const': 0x43,
    'f64.const': 0x44,
    'i32.eqz': 0x45,
    'i32.eq': 0x46,
    'i32.ne': 0x47,
    'i32.lt_s': 0x48,
    'i32.lt_u': 0x49,
    'i32.gt_s': 0x4A,
    'i32.gt_u': 0x4B,
    'i32.le_s': 0x4C,
    'i32.le_u': 0x4D,
    'i32.ge_s': 0x4E,
    'i32.ge_u': 0x4F,
    'i64.eqz': 0x50,
    'i64.eq': 0x51,
    'i64.ne': 0x52,
    'i64.lt_s': 0x53,
    'i64.lt_u': 0x54,
    'i64.gt_s': 0x55,
    'i64.gt_u': 0x56,
    'i64.le_s': 0x57,
    'i64.le_u': 0x58,
    'i64.ge_s': 0x59,
    'i64.ge_u': 0x5A,
    'f32.eq': 0x5B,
    'f32.ne': 0x5C,
    'f32.lt': 0x5D,
    'f32.gt': 0x5E,
    'f32.le': 0x5F,
    'f32.ge': 0x60,
    'f64.eq': 0x61,
    'f64.ne': 0x62,
    'f64.lt': 0x63,
    'f64.gt': 0x64,
    'f64.le': 0x65,
    'f64.ge': 0x66,
    'i32.clz': 0x67,
    'i32.ctz': 0x68,
    'i32.popcnt': 0x69,
    'i32.add': 0x6A,
    'i32.sub': 0x6B,
    'i32.mul': 0x6C,
    'i32.div_s': 0x6D,
    'i32.div_u': 0x6E,
    'i32.rem_s': 0x6F,
    'i32.rem_u': 0x70,
    'i32.and': 0x71,
    'i32.or': 0x72,
    'i32.xor': 0x73,
    'i32.shl': 0x74,
    'i32.shr_s': 0x75,
    'i32.shr_u': 0x76,
    'i32.rotl': 0x77,
    'i32.rotr': 0x78,
    'i64.clz': 0x79,
    'i64.ctz': 0x7A,
    'i64.popcnt': 0x7B,
    'i64.add': 0x7C,
    'i64.sub': 0x7D,
    'i64.mul': 0x7E,
    'i64.div_s': 0x7F,
    'i64.div_u': 0x80,
    'i64.rem_s': 0x81,
    'i64.rem_u': 0x82,
    'i64.and': 0x83,
    'i64.or': 0x84,
    'i64.xor': 0x85,
    'i64.shl': 0x86,
    'i64.shr_s': 0x87,
    'i64.shr_u': 0x88,
    'i64.rotl': 0x89,
    'i64.rotr': 0x8A,
    'f32.abs': 0x8B,
    'f32.neg': 0x8C,
    'f32.ceil': 0x8D,
    'f32.floor': 0x8E,
    'f32.trunc': 0x8F,
    'f32.nearest': 0x90,
    'f32.sqrt': 0x91,
    'f32.add': 0x92,
    'f32.sub': 0x93,
    'f32.mul': 0x94,
    'f32.div': 0x95,
    'f32.min': 0x96,
    'f32.max': 0x97,
    'f32.copysign': 0x98,
    'f64.abs': 0x99,
    'f64.neg': 0x9A,
    'f64.ceil': 0x9B,
    'f64.floor': 0x9C,
    'f64.trunc': 0x9D,
    'f64.nearest': 0x9E,
    'f64.sqrt': 0x9F,
    'f64.add': 0xA0,
    'f64.sub': 0xA1,
    'f64.mul': 0xA2,
    'f64.div': 0xA3,
    'f64.min': 0xA4,
    'f64.max': 0xA5,
    'f64.copysign': 0xA6,
    'i32.wrap_i64': 0xA7,
    'i32.trunc_f32_s': 0xA8,
    'i32.trunc_f32_u': 0xA9,
    'i32.trunc_f64_s': 0xAA,
    'i32.trunc_f64_u': 0xAB,
    'i64.extend_i32_s': 0xAC,
    'i64.extend_i32_u': 0xAD,
    'i64.trunc_f32_s': 0xAE,
    'i64.trunc_f32_u': 0xAF,
    'i64.trunc_f64_s': 0xB0,
    'i64.trunc_f64_u': 0xB1,
    'f32.convert_i32_s': 0xB2,
    'f32.convert_i32_u': 0xB3,
    'f32.convert_i64_s': 0xB4,
    'f32.convert_i64_u': 0xB5,
    'f32.demote_f64': 0xB6,
    'f64.convert_i32_s': 0xB7,
    'f64.convert_i32_u': 0xB8,
    'f64.convert_i64_s': 0xB9,
    'f64.convert_i64_u': 0xBA,
    'f64.promote_f32': 0xBB,
    'i32.reinterpret_f32': 0xBC,
    'i64.reinterpret_f64': 0xBD,
    'f32.reinterpret_i32': 0xBE,
    'f64.reinterpret_i64': 0xBF,
    'i32.extend8_s': 0xC0,
    'i32.extend16_s': 0xC1,
    'i64.extend8_s': 0xC2,
    'i64.extend16_s': 0xC3,
    'i64.extend32_s': 0xC4,
    'ref.null': 0xD0,
    'ref.is_null': 0xD1,
    'ref.func': 0xD2,
    'i32.trunc_sat_f32_s': (0xFC, 0),
    'i32.trunc_sat_f32_u': (0xFC, 1),
    'i32.trunc_sat_f64_s': (0xFC, 2),
    'i32.trunc_sat_f64_u': (0xFC, 3),
    'i64.trunc_sat_f32_s': (0xFC, 4),
    'i64.trunc_sat_f32_u': (0xFC, 5),
    'i64.trunc_sat_f64_s': (0xFC, 6),
    'i64.trunc_sat_f64_u': (0xFC, 7),
    'memory.copy': (0xFC, 10),
    'memory.fill': (0xFC, 11),
}


# ------------------------------------- cuántas palabras lleva cada instrucción
#
# El nombre dice cuántos inmediatos, y el tipo de cada uno. Es el inverso de
# `TABLA_INMEDIATOS` en `wat.py`, y **no** se deriva de ella a propósito: las dos
# tablas son la segunda implementación de la que habla el módulo, y una tabla
# derivada de la otra no compararía nada.
#
#   entero   ULEB: local, función, global, profundidad de salto
#   con_signo SLEB: constante entera
#   f32/f64  bytes little-endian del flotante
#   memoria  `offset=N align=N`
#   bloque   un tipo de resultado opcional
#   vector   `n` más un valor por defecto

FORMA_INMEDIATOS: dict[str, str] = {
    "local.get": "entero", "local.set": "entero", "local.tee": "entero",
    "global.get": "entero", "global.set": "entero", "call": "entero",
    "br": "entero", "br_if": "entero", "table.get": "entero",
    "table.set": "entero", "ref.func": "entero",
    "memory.size": "entero", "memory.grow": "entero",
    "memory.copy": "entero,entero", "memory.fill": "entero",
    "i32.const": "con_signo", "i64.const": "con_signo",
    "f32.const": "f32", "f64.const": "f64",
    "block": "bloque", "loop": "bloque", "if": "bloque",
    "i32.load": "memoria", "i64.load": "memoria", "f32.load": "memoria",
    "f64.load": "memoria",
    "i32.load8_s": "memoria", "i32.load8_u": "memoria",
    "i32.load16_s": "memoria", "i32.load16_u": "memoria",
    "i64.load8_s": "memoria", "i64.load8_u": "memoria",
    "i64.load16_s": "memoria", "i64.load16_u": "memoria",
    "i64.load32_s": "memoria", "i64.load32_u": "memoria",
    "i32.store": "memoria", "i64.store": "memoria",
    "f32.store": "memoria", "f64.store": "memoria",
    "i32.store8": "memoria", "i32.store16": "memoria",
    "i64.store8": "memoria", "i64.store16": "memoria",
    "i64.store32": "memoria",
}


def _instruccion(palabras: list[str], numero: int, estado: _Estado) -> bytes:
    """Los bytes de una instrucción, con sus inmediatos **en su orden**.

    El orden importa y no es uniforme: `local.set` es opcode y luego índice,
    mientras que `memory.copy` es prefijo, subcódigo y luego dos ceros. Escribir
    el inmediato antes del opcode produce un módulo que **valida** --los bytes
    son todos legales en algún sitio-- y ejecuta otra cosa.
    """
    import struct

    nombre = palabras[0]
    forma = FORMA_INMEDIATOS.get(nombre, "")

    if estado.rizado and not (nombre in SUBTUJENO_RIZADO
                              or nombre in MEMORIA):
        raise WatEnsamblarError(
            f"'{nombre}' está fuera del subconjunto de ADR 0014. Los módulos "
            "de `runtime/native/` son rectos a propósito: es lo que mantiene el "
            "techo de 1 KB por módulo.", numero)

    entrada = _CODIGO.get(nombre)
    if entrada is None:
        raise WatEnsamblarError(
            f"no sé emitir '{nombre}'; añadirlo a _CODIGO y a FORMA_INMEDIATOS",
            numero)
    cabeza = (bytes((entrada[0],)) + w.uleb(entrada[1])
              if isinstance(entrada, tuple) else bytes((entrada,)))

    def indice() -> int:
        """El ULEB que sigue al nombre: `local.get 4`, `call 1`."""
        if len(palabras) < 2:
            raise WatEnsamblarError(f"'{nombre}' sin su índice", numero)
        try:
            return int(palabras[1], 0)
        except ValueError:
            raise WatEnsamblarError(
                f"'{nombre}' con un índice que no es un número: "
                f"{palabras[1]!r}", numero) from None

    def necesita(que: str) -> str:
        try:
            return palabras[palabras.index(que) + 1]
        except (ValueError, IndexError):
            raise WatEnsamblarError(
                f"'{nombre}' sin su inmediato {que!r}", numero) from None

    if nombre in ("memory.copy", "memory.fill", "memory.size", "memory.grow"):
        # El MVP no tiene memoria con nombre, así que el índice es 0 y siempre.
        # `memory.copy` lleva **dos**: destino y origen.
        cuantos = 2 if nombre == "memory.copy" else 1
        return cabeza + w.uleb(0) * cuantos

    if forma == "entero":
        return cabeza + w.uleb(indice())

    if forma == "con_signo":
        try:
            return cabeza + w.sleb(indice())
        except WatEnsamblarError as error:
            raise WatEnsamblarError(
                f"'{nombre}' con un valor que no es un entero: "
                f"{palabras[1] if len(palabras) > 1 else ''!r}", numero) from None

    if forma in ("f32", "f64"):
        if len(palabras) < 2:
            raise WatEnsamblarError(f"'{nombre}' sin su valor", numero)
        valor = _float_desde_texto(palabras[1])
        return cabeza + struct.pack("<f" if forma == "f32" else "<d", valor)

    if forma == "memoria":
        alineado, desplazamiento = _memarg(palabras[1:], numero)
        return cabeza + w.uleb(alineado) + w.uleb(desplazamiento)

    if forma == "bloque":
        tipo = 0x40
        if "result" in palabras:
            nombre_tipo = palabras[palabras.index("result") + 1]
            if nombre_tipo not in VALTIPO:
                raise WatEnsamblarError(f"tipo de bloque {nombre_tipo!r}", numero)
            tipo = VALTIPO[nombre_tipo]
        return cabeza + bytes((tipo,))

    return cabeza


def _memarg(resto: list[str], numero: int) -> tuple[int, int]:
    """`offset=N align=N` → (align, offset), en ese orden.

    El orden en el texto no es el del binario: el memarg es alineación y luego
    desplazamiento, y el emisor los imprime al revés porque así se lee. Confundir
    los dos no da error de validación --los dos son ULEB-- sino un módulo que
    lee en la dirección equivocada.
    """
    desplazamiento = 0
    alineado = 0
    for palabra in resto:
        if palabra.startswith("offset="):
            desplazamiento = int(palabra.split("=", 1)[1])
        elif palabra.startswith("align="):
            alineado = int(palabra.split("=", 1)[1])
    if alineado == 0 and not any(p.startswith("align=") for p in resto):
        raise WatEnsamblarError(f"memarg sin `align=`: {' '.join(resto)}", numero)
    return (alineado, desplazamiento)


# ------------------------------------------------------------ serialización


def _serializar(estado: _Estado) -> bytes:
    """El módulo en bytes, en el orden de secciones que exige el formato.

    El orden no es libre: es el numérico de los identificadores de sección, y el
    encoder lo respeta (1..11). Emitir fuera de orden produce un módulo que
    algunos validadores aceptan y otros no, que es la peor variante.
    """
    salida = bytearray(w.MAGIC + w.VERSION)

    if estado.firmas:
        tipos = [bytes((0x60,))
                 + w.encode_vector([bytes((VALTIPO[t],)) for t in params])
                 + w.encode_vector([bytes((VALTIPO[t],)) for t in results])
                 for params, results in estado.firmas]
        salida += _seccion(w.SEC_TYPE, w.encode_vector(tipos))

    if estado.imports:
        imports = []
        for modulo, cosa, tipo in estado.imports:
            imports.append(w.encode_name(modulo) + w.encode_name(cosa)
                           + bytes((0x00,)) + w.uleb(tipo))
        salida += _seccion(w.SEC_IMPORT, w.encode_vector(imports))

    if estado.funciones:
        salida += _seccion(w.SEC_FUNCTION, w.encode_vector(
            [w.uleb(tipo) for tipo, _, _ in estado.funciones]))

    if estado.tablas:
        salida += _seccion(w.SEC_TABLE, w.encode_vector(
            [bytes((0x70, 0x00)) + w.uleb(minimo) for minimo in estado.tablas]))

    if estado.memoria:
        limites = [w.encode_limits(int(minimo))
                   for limites in estado.memoria
                              for minimo in [limites.split()[0]]]
        salida += _seccion(w.SEC_MEMORY, w.encode_vector(limites))

    if estado.globals_:
        variables = []
        for definicion in estado.globals_:
            palabras = definicion.split()
            mutable = palabras[0] == "mut"
            tipo = VALTIPO[palabras[1] if mutable else palabras[0]]
            valor = definicion.split("i32.const")[1].split(")")[0].strip()
            variables.append(bytes((tipo, 1 if mutable else 0))
                             + w.i32_const(int(valor)) + w.end())
        salida += _seccion(w.SEC_GLOBAL, w.encode_vector(variables))

    if estado.exports:
        exports = [w.encode_name(nombre) + bytes((kind,)) + w.uleb(indice)
                   for nombre, kind, indice in estado.exports]
        salida += _seccion(w.SEC_EXPORT, w.encode_vector(exports))

    if estado.inicio is not None:
        salida += _seccion(w.SEC_START, w.uleb(estado.inicio))

    if estado.funciones:
        cuerpos = []
        for _, locales, cuerpo in estado.funciones:
            grupos = _agrupar_locales(locales)
            cabeza = w.encode_vector(
                [w.uleb(cuenta) + bytes((tipo,)) for cuenta, tipo in grupos])
            codigo = b"".join(cuerpo) + w.end()
            cuerpos.append(w.uleb(len(cabeza) + len(codigo)) + cabeza + codigo)
        salida += _seccion(w.SEC_CODE, w.encode_vector(cuerpos))

    if estado.datos:
        segmentos = [bytes((0x00,)) + w.i32_const(offset) + w.end()
                     + w.encode_vector([bytes((b,)) for b in crudo])
                     for offset, crudo in estado.datos]
        salida += _seccion(w.SEC_DATA, w.encode_vector(segmentos))

    return bytes(salida)


def _agrupar_locales(locales: list[str]) -> list[tuple[int, int]]:
    """Los locals en run-length: contiguos del mismo tipo, en un grupo.

    Es la regla del encoder, y el round trip la comprueba en las dos
    direcciones: el emisor la aplica al escribir, el ensamblador al leer el
    texto, y si difieren los bytes difieren.

    **Lo que esto NO comprueba** es que la regla sea la correcta. Si el encoder
    reordenara los locals por tipo --que fue un error real-- las dos mitades lo
    harían igual y el round trip pasaría. Eso lo cazan la paridad con el
    intérprete y el validador de Node, no el round trip.
    """
    grupos: list[tuple[int, int]] = []
    for tipo in locales:
        valor = VALTIPO[tipo]
        if grupos and grupos[-1][1] == valor:
            grupos[-1] = (grupos[-1][0] + 1, valor)
        else:
            grupos.append((1, valor))
    return grupos


def _seccion(section_id: int, payload: bytes) -> bytes:
    if not payload:
        return b""
    return bytes((section_id,)) + w.uleb(len(payload)) + payload
