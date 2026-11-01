"""Verificación de tipos del código emitido, antes de serializarlo.

Existe por una razón concreta: un backend produce módulos **inválidos** de
formas que ninguna otra comprobación ve. `validate()` de WASM mira la
estructura --números mágicos, orden de secciones-- y un módulo con un `i32.add`
donde tocaba un `i64.add` lo supera. El error sale al instanciar, en Node o en
el navegador, lejos del código que lo causó.

Comprobar la pila de cada función mientras se genera cuesta un recorrido por
cuerpo y convierte esos fallos en un error de compilación con nombre de
función, que es donde se puede arreglar.

No es un validador de WASM completo: cubre el MVP y las operaciones que este
backend emite. Ante una instrucción que no conoce, no dice nada en vez de
suponer: un verificador que adivina es peor que no tener ninguno.
"""

from __future__ import annotations

from . import encoder as w

I32, I64, F32, F64 = "i32", "i64", "f32", "f64"

_BY_TYPE = {0x7F: I32, 0x7E: I64, 0x7D: F32, 0x7C: F64}


def _build_table() -> dict[int, tuple[list[str], list[str]]]:
    """opcode -> (lo que saca de la pila, lo que deja).

    El espacio de opcodes del MVP está ordenado por tipo, así que casi todo se
    escribe como un rango en vez de como una entrada por instrucción. El orden
    es el de la especificación, y por eso los rangos salen bien.
    """
    tabla: dict[int, tuple[list[str], list[str]]] = {}

    def rango(desde, hasta, saca, deja):
        for code in range(desde, hasta + 1):
            tabla[code] = (saca, deja)

    rango(w.I32_EQZ, w.I32_EQZ, [I32], [I32])            # i32.eqz
    rango(w.I32_EQ, w.I32_GE_U, [I32, I32], [I32])        # comparaciones i32
    rango(w.I64_EQZ, w.I64_EQZ, [I64], [I32])
    rango(w.I64_EQ, w.I64_GE_U, [I64, I64], [I32])        # comparaciones i64
    rango(w.F32_EQ, w.F32_GE, [F32, F32], [I32])          # comparaciones f32
    rango(w.F64_EQ, w.F64_GE, [F64, F64], [I32])          # comparaciones f64
    rango(w.I32_CLZ, w.I32_POPCNT, [I32], [I32])
    rango(w.I32_ADD, w.I32_ROTR, [I32, I32], [I32])
    rango(w.I64_CLZ, w.I64_POPCNT, [I64], [I64])
    rango(w.I64_ADD, w.I64_ROTR, [I64, I64], [I64])
    rango(w.F32_ABS, w.F32_SQRT, [F32], [F32])
    rango(w.F32_ADD, w.F32_COPYSIGN, [F32, F32], [F32])
    rango(w.F64_ABS, w.F64_SQRT, [F64], [F64])
    rango(w.F64_ADD, w.F64_COPYSIGN, [F64, F64], [F64])

    # Conversiones: cada una con su entrada y su salida, y no se pueden
    # agrupar por rango porque los tipos no siguen el orden del opcode.
    tabla[w.I32_WRAP_I64] = ([I64], [I32])
    for code, entrada in ((w.I32_TRUNC_F32_S, F32), (w.I32_TRUNC_F32_U, F32),
                          (w.I32_TRUNC_F64_S, F64), (w.I32_TRUNC_F64_U, F64)):
        tabla[code] = ([entrada], [I32])
    tabla[w.I64_EXTEND_I32_S] = ([I32], [I64])
    tabla[w.I64_EXTEND_I32_U] = ([I32], [I64])
    for code, entrada in ((w.I64_TRUNC_F32_S, F32), (w.I64_TRUNC_F32_U, F32),
                          (w.I64_TRUNC_F64_S, F64), (w.I64_TRUNC_F64_U, F64)):
        tabla[code] = ([entrada], [I64])
    tabla[w.F32_CONVERT_I32_S] = ([I32], [F32])
    tabla[w.F32_CONVERT_I32_U] = ([I32], [F32])
    tabla[w.F32_CONVERT_I64_S] = ([I64], [F32])
    tabla[w.F32_CONVERT_I64_U] = ([I64], [F32])
    tabla[w.F32_DEMOTE_F64] = ([F64], [F32])
    tabla[w.F64_CONVERT_I32_S] = ([I32], [F64])
    tabla[w.F64_CONVERT_I32_U] = ([I32], [F64])
    tabla[w.F64_CONVERT_I64_S] = ([I64], [F64])
    tabla[w.F64_CONVERT_I64_U] = ([I64], [F64])
    tabla[w.F64_PROMOTE_F32] = ([F32], [F64])
    tabla[w.I32_REINTERPRET_F32] = ([F32], [I32])
    tabla[w.I64_REINTERPRET_F64] = ([F64], [I64])
    tabla[w.F32_REINTERPRET_I32] = ([I32], [F32])
    tabla[w.F64_REINTERPRET_I64] = ([I64], [F64])
    for code in (w.I32_EXTEND8_S, w.I32_EXTEND16_S):
        tabla[code] = ([I32], [I32])
    for code in (w.I64_EXTEND8_S, w.I64_EXTEND16_S, w.I64_EXTEND32_S):
        tabla[code] = ([I64], [I64])
    return tabla


TABLA = _build_table()

# Las instrucciones con prefijo 0xFC: subopcode -> (lo que saca, lo que deja).
#
# Los truncamientos saturados son la razón por la que el prefijo existe en el
# MVP; `memory.copy` y `memory.fill` son de *bulk memory*, que es posterior.
# El backend las emite para copiar y para poner a cero en vez de escribir un
# bucle, y por eso están aquí: un opcode que el verificador no conoce deja la
# pila desincronizada a partir de ese punto.
TABLA_FC: dict[int, tuple[list[str], list[str]]] = {
    0: ([F32], [I32]), 1: ([F32], [I32]),
    2: ([F64], [I32]), 3: ([F64], [I32]),
    4: ([F32], [I64]), 5: ([F32], [I64]),
    6: ([F64], [I64]), 7: ([F64], [I64]),
    10: ([I32, I32, I32], []),      # memory.copy: destino, origen, bytes
    11: ([I32, I32, I32], []),      # memory.fill: destino, byte, bytes
}

# Instrucciones que no tocan la pila o cuya pila depende del contexto. Se
# saltan: comprobarlas bien exige implementing el modelo de controles, y
# adivinar es peor que no comprobar.
NEUTRAS = frozenset({
    w.NOP, w.BLOCK, w.LOOP, w.IF, w.ELSE, w.END, w.SELECT,
})


class SignatureError(Exception):
    """El código emitido no respeta los tipos de la pila."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def check_function(name: str, signature, groups, pieces,
                   calls: dict[int, tuple[list[str], list[str]]]) -> None:
    """Recorre el cuerpo de una función comprobando la pila.

    `calls` mapea índice de función a (parámetros, resultados). Los imports y
    las funciones internas entran por ahí; una llamada a un índice que no
    aparece no se comprueba, porque no es un error del backend sino un módulo
    con una firma desconocida.

    **Pila de fondo.** Después de un salto, un `return` o un `unreachable` la
    pila no tiene un tipo fijo: es lo que la especificación llama *bottom*, y
    desde ahí cualquier valor encaja. Sin esa regla, limpiar la pila y seguir
    comprobando daba falsos positivos en cada bucle -- el `br` de vuelta de un
    `loop` va seguido de más código-- y el verificador era inservible.
    """
    # La firma tiene **un tipo por local**, no uno por parámetro del fuente: un
    # parámetro STRING ya aparece como dos i32. Expandirlo otra vez aquí
    # desplazaba el índice→tipo en una posición y el verificador acababa
    # culpando a un local que era del tipo correcto.
    locales: list[str] = [_BY_TYPE.get(t, "?") for t in signature.params]
    for count, value_type in groups:
        locales.extend([_BY_TYPE.get(value_type, "?")] * count)

    pila: list[str] = []
    fondo = False
    niveles: list[tuple[list[str], bool, int, int]] = []

    def poner(tipo: str) -> None:
        pila.append("?" if fondo else tipo)

    def sacar(quiere: str, donde: str) -> None:
        if not pila:
            if fondo:
                return
            raise SignatureError(f"{donde}: la pila está vacía y hace "
                                 f"falta {quiere}")
        if pila[-1] != quiere and pila[-1] != "?":
            raise SignatureError(f"{donde}: esperaba {quiere} y la pila trae "
                                 f"{pila[-1]}")
        pila.pop()

    def resultados_de(signature_) -> list[str]:
        return [_BY_TYPE.get(t, "?") for t in signature_.results]

    def operando(piece, posicion=1):
        """El índice de un local o de una función, en LEB128.

        Por debajo de 128 --todo lo que cabe en un byte-- el índice *es* el
        primer byte, así que leerlo crudo funciona. A partir de ahí no: el
        índice 384 son los bytes `0x80 0x02`, y leer `0x80` miraba el local 128
        en vez del 384. El verificador daba por buena una función con 128
        locales, que es justo donde empiezan los programas que ya no son de
        juguete.
        """
        value = 0
        shift = 0
        for byte in piece[posicion:]:
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7
        return value

    for indice, piece in enumerate(pieces):
        code = piece[0]
        donde = f"@{name} instrucción {indice} (0x{code:02x})"

        VACIO = 0x40
        if code in (w.BLOCK, w.LOOP):
            # Cada bloque guarda la pila con la que entra. Sin esto, lo que
            # deja un bloque se acumulaba hasta el final de la función y el
            # verificador acababa culpando a la última instrucción de un
            # `main` que estaba bien.
            niveles.append((list(pila), fondo, piece[1] if len(piece) > 1
                            else VACIO, len(pila)))
            continue
        if code == w.IF:
            # La condición se saca **antes** de guardar el nivel, porque `if` se
            # la consume: la pila con la que continúa el bloque después del
            # `end` es la de entrada menos la condición. Guardar antes añadía un
            # i32 fantasma por cada `if` del programa, y el `end` lo reportaba
            # como "el bloque deja ['i32']".
            sacar(I32, donde)
            niveles.append((list(pila), fondo, piece[1] if len(piece) > 1
                            else VACIO, len(pila)))
            continue
        if code == w.END:
            # Un `block`, un `loop` y un `if` son de tipo vacío: al cerrarlos
            # no puede quedar nada en la pila. Sin esta comprobación, una
            # operación que deja un valor dentro de un bloque --una llamada mal
            # construida, un `select` con un operando de más-- pasaba el
            # verificador y Node lo rechazaba con "expected 0 elements on the
            # stack for fallthru", que no dice qué bloque es.
            # Un bloque deja exactamente lo que declara su tipo: nada si es
            # vacío, un valor si es `if (result i32)`. Comprobar sólo "vacío"
            # marcaba como roto un `if` que devolvía un booleano.
            if niveles:
                _, _, tipo, entrada = niveles[-1]
                esperados_bloque = ([] if tipo == 0x40
                                   else [_BY_TYPE.get(tipo, "?")])
            else:
                esperados_bloque, entrada = [], 0
            # La comparación es **relativa a la altura de entrada**: un bloque
            # anidado puede legítimamente tener valores debajo, y exigir la
            # pila vacía daba falsos positivos en cuanto dos `if` con resultado
            # se anidaban.
            if not fondo and pila[entrada:] != esperados_bloque:
                raise SignatureError(
                    f"{donde}: el bloque deja {pila[entrada:]} y declara "
                    f"{esperados_bloque}")
            if niveles:
                pila, fondo, _, _ = niveles.pop()
                # El valor que devuelve el bloque **sobrevive** al `end`: se
                # devuelve lo que había al entrar más lo que declara el tipo.
                # Descartarlo dejaba la pila vacía justo donde el consumidor
                # leía el resultado.
                pila.extend(esperados_bloque)
            continue
        if code == w.ELSE:
            # La rama `then` ya no sigue viva: se descarta lo que dejó y se
            # continúa desde la pila de entrada del `if`. Sin esta restauración
            # las dos ramas se sumaban y el `end` veía el doble de valores de
            # los que declara.
            if niveles:
                pila, _, _, _ = niveles[-1]
                pila = list(pila)
            continue
        if code == w.NOP:
            continue
        if code == 0x20:
            poner(locales[operando(piece)])
            continue
        if code in (0x21, 0x22):
            indice_local = operando(piece)
            quiere = locales[indice_local]
            if not pila and not fondo:
                raise SignatureError(f"{donde}: la pila está vacía y local "
                                     f"{indice_local} necesita un valor")
            if pila and pila[-1] != quiere and pila[-1] != "?":
                raise SignatureError(
                    f"{donde}: local {indice_local} es {quiere} y la pila trae "
                    f"{pila[-1]}")
            if pila:
                pila.pop()
            if code == 0x22:
                poner(quiere)
            continue
        if code == w.GLOBAL_GET:
            poner(I32)
            continue
        if code == w.GLOBAL_SET:
            sacar(I32, donde)
            continue
        if code == w.CALL:
            esperada = calls.get(operando(piece))
            if esperada is None:
                continue
            params, results = esperada
            for position in range(len(params) - 1, -1, -1):
                sacar(params[position], donde)
            for tipo in results:
                poner(tipo)
            continue
        if code == w.I32_CONST:
            poner(I32)
            continue
        if code == w.I64_CONST:
            poner(I64)
            continue
        if code == w.F32_CONST:
            poner(F32)
            continue
        if code == w.F64_CONST:
            poner(F64)
            continue
        if code == w.MEMORY_SIZE or code == w.MEMORY_GROW:
            poner(I32)
            continue
        if code == w.DROP:
            if not pila and not fondo:
                raise SignatureError(f"{donde}: `drop` con la pila vacía")
            if pila:
                pila.pop()
            continue
        if code == w.SELECT:
            # (a, b, cond) -> a si cond.
            #
            # Hay que sacar **los tres** y devolver uno. Comprobar sólo la
            # condición dejaba dos valores en la pila simulada, que se
            # consumían más adelante como si fueran de otro tipo: el modelo se
            # desviaba y el verificador daba verde a un módulo que Node rechazaba
            # con "expected 0 elements for fallthru".
            sacar(I32, donde)
            if len(pila) < 2:
                if not fondo:
                    raise SignatureError(f"{donde}: `select` con {len(pila)} "
                                         f"valores y necesita 3")
                continue
            b, a = pila.pop(), pila.pop()
            if a != b and a != "?" and b != "?":
                raise SignatureError(f"{donde}: `select` con {a} y {b}, que no "
                                     f"son del mismo tipo")
            poner(a if a != "?" else b)
            continue
        if code == w.BR_IF:
            sacar(I32, donde)
            continue
        if code == w.BR or code == w.UNREACHABLE:
            fondo = True
            continue
        if code == w.RETURN:
            for tipo in reversed(resultados_de(signature)):
                sacar(tipo, donde)
            fondo = True
            continue
        if code in (w.I32_LOAD, w.I32_LOAD8_S, w.I32_LOAD8_U, w.I32_LOAD16_S,
                    w.I32_LOAD16_U):
            sacar(I32, donde)
            poner(I32)
            continue
        if code in (w.I64_LOAD, w.I64_LOAD8_S, w.I64_LOAD8_U, w.I64_LOAD16_S,
                    w.I64_LOAD16_U, w.I64_LOAD32_S, w.I64_LOAD32_U):
            sacar(I32, donde)
            poner(I64)
            continue
        if code == w.F32_LOAD:
            sacar(I32, donde)
            poner(F32)
            continue
        if code == w.F64_LOAD:
            sacar(I32, donde)
            poner(F64)
            continue
        if code in (w.I32_STORE, w.I32_STORE8, w.I32_STORE16):
            sacar(I32, donde)
            sacar(I32, donde)
            continue
        if code in (w.I64_STORE, w.I64_STORE8, w.I64_STORE16, w.I64_STORE32):
            sacar(I64, donde)
            sacar(I32, donde)
            continue
        if code == w.F32_STORE:
            sacar(F32, donde)
            sacar(I32, donde)
            continue
        if code == w.F64_STORE:
            sacar(F64, donde)
            sacar(I32, donde)
            continue
        if code == w.FC_PREFIX:
            # Las prefijadas llevan el subopcode en ULEB y hay que contarlas
            # como una instrucción, no como cero.
            #
            # Sin esto el verificador las **ignoraba** --la rama de "no la
            # conozco" no hace nada-- y el efecto no era un falso positivo sino
            # peor: los tres operandos de un `memory.copy` se quedaban en la
            # pila, y a partir de ahí todo el resto del cuerpo se comprobaba
            # contra un estado que ya no era real. Un verificador que salta lo
            # que no conoce da verde por razones equivocadas.
            entrada = TABLA_FC.get(operando(piece))
            if entrada is None:
                continue
            saca_fc, deja_fc = entrada
            for posicion in range(len(saca_fc) - 1, -1, -1):
                sacar(saca_fc[posicion], donde)
            for tipo in deja_fc:
                poner(tipo)
            continue

        if code in TABLA:
            saca, deja = TABLA[code]
            for position in range(len(saca) - 1, -1, -1):
                sacar(saca[position], donde)
            for tipo in deja:
                poner(tipo)
            continue
        # Una instrucción desconocida no se comprueba. Decirlo es mejor que
        # suponer: el verificador no es el validador de la especificación.
        continue

    # Con la pila en fondo, el final de la función es inalcanzable y lo que
    # quede no importa.
    #
    # Si es alcanzable, la pila tiene que ser **exactamente** la lista de
    # resultados. Quedarse corto es un módulo que no valida; quedarse largo es
    # un `expected 0 elements on the stack for fallthru`, que es el mismo error
    # dicho de otra forma. Comprobar sólo "queda alguno" marcaba como roto a
    # `@__bump`, que devuelve su puntero cayendo al final.
    # Todas las construcciones tienen que estar cerradas. Es el mismo error que
    # Node llama "function body must end with end", y sin esta comprobación un
    # `end` de menos pasaba el verificador entero.
    if niveles:
        _, _, tipo, _ = niveles[-1]
        raise SignatureError(
            f"@{name}: quedan {len(niveles)} bloque(s) sin cerrar al final "
            f"(el más exterior declara {hex(tipo)})")
    if fondo:
        return
    esperados = resultados_de(signature)
    if pila != esperados:
        raise SignatureError(
            f"@{name}: al final de la función hay {pila} y la firma devuelve "
            f"{esperados}")


def _saca(pila: list[str], quiere: str, donde: str) -> None:
    if not pila:
        raise SignatureError(f"{donde}: la pila está vacía y hace falta {quiere}")
    if pila[-1] != quiere:
        raise SignatureError(f"{donde}: esperaba {quiere} y la pila trae "
                             f"{pila[-1]}")
    pila.pop()