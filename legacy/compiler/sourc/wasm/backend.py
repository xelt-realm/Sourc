"""Backend WASM: baja el SIR a un módulo binario.

Es la mitad del trabajo de la Fase 2, y la mitad donde un error no da error: da
un módulo que **valida** y calcula otra cosa. Por eso el orden es el de ADR
0019: primero el tripwire (`tests/test_wasm_tripwire.py`) declara qué
operaciones existen, luego cada una baja con su contrato escrito, y nada se da
por bueno hasta que el módulo corre en Node con la misma salida que el
intérprete.

Cuatro decisiones que no son obvias:

**Los slots son locales.** El IR tiene `load.slot` y `store.slot`, y en WASM no
son instrucciones: cada slot es un local, y un acceso es una copia. Ahorra dos
instrucciones por acceso, y en un bucle son decenas de miles.

**Un valor ocupa varios locales.** Un texto son dos (ptr, len) y un array tres
(ptr, len, cap). Los locales de WASM tienen tipo fijo, así que cada valor del IR
reserva `layout.words` locales contiguos. Toda la aritmética de índices sale de
ahí, y por eso vive aquí y no repartida por el emisor.

**Los locales se declaran al final.** El código se genera antes de saber cuántos
locales hacen falta, porque es el código quien los reserva. Los valores se
reservan en un primer recorrido para que uno usado antes de definirse no
desplace los índices ya emitidos.

**`store` consume de arriba abajo.** Para un valor de dos palabras, la primera
palabra está debajo en la pila: hay que guardar la última primero. Al revés se
guardan los números cambiados y el módulo sigue validando.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import sir
from ..diagnostics import CompileError, Diagnostic, Span
from ..types import layout_by_code, layout_of
from ..wasm import control
from ..wasm import verify
from ..wasm import encoder as w

# La dirección 0 se deja sin escribir para que `NIL` sea (ptr 0, len 0) y una
# cadena vacía pueda compartir dirección sin ambigüedad con ella.
DATA_BASE = 16
HEAP_ALIGN = 8

WASM_TYPE = {
    "INTEGER": w.I64, "SINGLE": w.F32, "DOUBLE": w.F64,
    "BOOLEAN": w.I32, "COLOR": w.I32,
}


def valtype(type_) -> int:
    """El tipo de local que representa un valor del IR.

    Texto y array son `i32` (puntero y longitud), aunque el IR los tipa como
    STRING y ARRAY: no hay un tipo `str` en WASM, hay direcciones. Los vectores
    también son punteros en esta fase.
    """
    kind = type_ if isinstance(type_, str) else type_.kind
    if kind in ("STRING", "ARRAY", "VOID", "PTR", "VEC2", "VEC3", "VEC4",
                "MAT3", "MAT4"):
        return w.I32
    return WASM_TYPE.get(kind, w.I64)


def words_of(type_) -> int:
    return layout_of(type_).words


def _expand(groups: list[tuple[int, int]]) -> list[int]:
    """Los grupos de locales, aplanados a un tipo por índice."""
    flat: list[int] = []
    for count, value_type in groups:
        flat.extend([value_type] * count)
    return flat


def unsupported(op: str, span) -> CompileError:
    return CompileError(Diagnostic(
        "E0058",
        f"el backend todavía no baja '{op}'",
        span or Span(0, 0),
        f"la operación está declarada en el contrato del tripwire pero su "
        f"emisión no existe. Añádela a la tabla de backend.py y su contrato a "
        f"tests/test_wasm_backend.py",
    ))


# --------------------------------------------------------------- firma del host
#
# El glue implementa exactamente estas firmas. Si aquí cambia una, cambia el
# glue, y el compilador no se entera: por eso están en un solo sitio y con un
# formato que se puede volcar a la documentación.

HOST_SIGNATURES = {
    "source.print_str":     ((w.I32, w.I32), ()),
    "source.print_int":     ((w.I64,), ()),
    "source.print_num":     ((w.F64,), ()),
    "source.print_bool":    ((w.I32,), ()),
    "source.newline":       ((), ()),
    # El formateo de números vive en el glue durante la Fase 2: un formateador
    # de DOUBLE a texto son ~200 instrucciones de WASM que no vale la pena
    # escribir en el backend, y unas pocas líneas de JavaScript que sí.
    #
    # **Una función por tipo de entrada**, y no una sola con un `f64` y que el
    # backend decida: `STR(n)` con un INTEGER de ocho bytes perdería precisión
    # al pasar por f64, y un entero grande saldría cambiado.
    "source.int2str":       ((w.I32, w.I64), (w.I32,)),
    "source.num2str":       ((w.I32, w.F64, w.I32), (w.I32,)),
    "source.bool2str":      ((w.I32, w.I32), (w.I32,)),
    "source.str_len":       ((w.I32,), (w.I32,)),
    "source.str2num":       ((w.I32, w.I32), (w.F64,)),
    # El MVP de WASM no tiene seno ni coseno. El glue los calcula con
    # `Math.sin`/`Math.cos` en la Fase 2 y con el runtime en la Fase 4.
    "source.sin":           ((w.F64,), (w.F64,)),
    "source.cos":           ((w.F64,), (w.F64,)),
    "source.pow":           ((w.F64, w.F64), (w.F64,)),
    # Estas firmas no son "lo que parece lógico": son lo que el IR emite. Un
    # `SCREEN` pasa el título como texto y las medidas como INTEGER, así que la
    # firma es (i64, i64, i32, i32) y no cuatro enteros de 32 bits. Inventar la
    # firma delante hacía que el módulo no validara, y el verificador lo.signaló
    # nombrando la instrucción.
    "source.key":           ((w.I32, w.I32), (w.I32,)),
    "source.keydown":       ((w.I32, w.I32), (w.I32,)),
    "source.flip":          ((), ()),
    "source.dl_cls":        ((w.I32,), ()),
    "source.dl_screen":     ((w.I64, w.I64, w.I32, w.I32), ()),
    "source.dl_screen_off": ((), ()),
    "source.dl_rect":       ((w.I64, w.I64, w.I64, w.I64, w.I32), ()),
    "source.dl_circle":     ((w.F32, w.F32, w.I64, w.I32), ()),
    "source.dl_pset":       ((w.I64, w.I64, w.I32), ()),
    "source.dl_line":       ((w.I64, w.I64, w.I64, w.I64, w.I32), ()),
    "source.dl_triangle":   ((w.I64, w.I64, w.I64, w.I64, w.I64, w.I64, w.I32), ()),
    "source.dl_text":       ((w.I64, w.I64, w.I32, w.I32, w.I32), ()),
    "source.dl_color":      ((w.I32,), ()),
}

# Las operaciones de host y el import que usan.
HOST_OPS = dict(sir.HOST_ABI)
HOST_OPS.update(sir.HOST_FORMATTING)
# `to.str` no entra aquí: su import depende del tipo del operando y por eso lo
# elige `to_str`, no la tabla. Dejarlo producía una llamada con el import de
# otro tipo, y el módulo no validaba.
HOST_OPS.pop("to.str", None)
HOST_OPS["str_len"] = "source.str_len"
HOST_OPS["newline"] = "source.newline"

# Nativas del lenguaje: lo que va al host y lo que es instrucción de WASM.
NATIVE_IMPORT = {
    "key": "source.key", "keydown": "source.keydown",
    "cls": "source.dl_cls", "cls_default": "source.dl_cls",
    "screen": "source.dl_screen", "screen_off": "source.dl_screen_off",
    "rect": "source.dl_rect", "circle": "source.dl_circle",
    "pset": "source.dl_pset", "line": "source.dl_line",
    "triangle": "source.dl_triangle", "text": "source.dl_text",
    "color": "source.dl_color",
    "sin": "source.sin", "cos": "source.cos", "pow": "source.pow",
    "rgb": None,
}


# Operaciones que reservan memoria del heap y por tanto necesitan `@__bump`.
OPS_QUE_RESERVAN = frozenset({"array.alloc", "str.concat", "to.str",
                              "host.print_arr", "str.from_code"})

# Los caracteres que necesita el formateador de arrays del módulo. Una sola
# cadena: cuatro constantes serían cuatro segmentos de datos por cinco bytes.
SIGUIES_ARRAY = '"[], '
COMILLA, LLAVETA, CIERRE, COMA = range(4)
# Un f64 con seis cifras significativas más el signo cabe de sobra en 32 bytes.
FORMAT_BUFFER = 32


# ============================================================ compilador de función


class FunctionCompiler:
    """Baja una función del SIR a código WASM."""

    def __init__(self, backend: "WasmBackend", func: sir.Func):
        self.backend = backend
        self.func = func
        self.pieces: list[bytes] = []
        self.local_of: dict[int, int] = {}
        self.slot_local: dict[int, int] = {}
        self.next_local = 0
        self.locals: list[tuple[int, int]] = []
        self.emitters = _build_emitters()
        self._reserve()

    # --------------------------------------------------------------- locales

    def _alloc(self, count: int, wasm_type: int, declare: bool = True) -> int:
        """Reserva `count` locales y devuelve el primero.

        `declare=False` avanza el contador sin añadir nada al vector de locales:
        es lo que necesitan los parámetros, que ya vienen declarados en la
        firma y sólo tienen que ocupar sus índices.
        """
        base = self.next_local
        self.next_local += count
        if count and declare:
            self.locals.append((count, wasm_type))
        return base

    def _reserve(self) -> None:
        """Reserva locales de slots y de cada valor del IR.

        Los parámetros **no** se declaran: ocupan los índices 0..n-1 del
        espacio de locales porque vienen en la firma. Declararlos otra vez en
        el vector de locales no rompe los índices --sólo si los primeros slots
        son exactamente los parámetros, que es lo que comprueba E0061-- pero
        gasta una entrada por parámetro y obliga a corregir el índice→tipo, que
        es donde se cueven los errores.

        Los valores se reservan antes de generar el código porque el código es
        quien los usa: reservarlos al encontrar la definición haría que un valor
        referenciado antes de definirse desplazara todos los índices ya
        emitidos.
        """
        params = list(self.func.params)
        declared = self.func.locals[:len(params)]
        if declared != params:
            raise CompileError(Diagnostic(
                "E0061",
                f"en @{self.func.name} los parámetros no son los primeros slots",
                self.func.span or Span(0, 0),
                "los parámetros de WASM ocupan los índices 0..n-1 del espacio "
                "de locales; si un slot declarado va antes, los índices no "
                "cuadran con la firma",
            ))

        # Los parámetros van primero y no se reservan -- `_alloc` ya dejó
        # `next_local` justo donde empiezan los slots declarados. No se reinicia
        # el contador aquí: eso rebotaba por encima de los slots ya reservados
        # y el primer valor del IR compartía local con el primer slot.
        for index, (_, type_) in enumerate(self.func.locals):
            self.slot_local[index] = self._alloc(
                words_of(type_), valtype(type_), declare=index >= len(params))

        for instr in self.func.instrs:
            value = sir.result_of(instr)
            if value is not None and value.id not in self.local_of:
                self.local_of[value.id] = self._alloc(words_of(value.type),
                                                      valtype(value.type))
        # Los temporales se reservan al final, y en número fijo, para que los
        # índices de los valores no dependan de cuántas operaciones de array
        # tenga la función. Si se reservaran bajo demanda, el mismo programa
        # daría índices distintos según dónde estuviera el `array.alloc`.
        self.scratch_bytes = self._alloc(1, w.I32)
        self.scratch_addr = self._alloc(1, w.I32)
        self.scratch_loop = self._alloc(1, w.I32)
        self.scratch_quedan = self._alloc(1, w.I32)
        # `rem.f64` necesita el cociente en un temporal f64: lo usa dos veces y
        # en la pila no se puede releer.
        self.scratch_f64 = self._alloc(1, w.F64)
        # El mapa índice→tipo tiene que empezar por los parámetros, porque los
        # locales del IR se numeran en el espacio global (0..n-1 son los
        # parámetros) y `self.locals` sólo contiene los declarados. Sin el
        # prefijo, `push_local` consultaba el tipo del local equivocado y
        # decidía que no hacía falta ninguna conversión.
        tipos: list[int] = []
        for _, type_ in params:
            tipos.extend([valtype(type_)] * words_of(type_))
        self.local_types = tipos + _expand(self.locals)

    # --------------------------------------------------------------- operands

    def push(self, operand) -> None:
        """Deja el valor de un operando del IR en la pila.

        Un operando puede ser un valor (`Value`, referencia a un `%id`) o un
        inmediato escrito en la instrucción: el compilador elige la forma más
        corta cuando el valor es constante, y el emisor tiene que aceptar las
        dos sin distinguirlas.
        """
        if isinstance(operand, sir.Value):
            base = self.local_of[operand.id]
            for offset in range(words_of(operand.type)):
                self.pieces.append(w.local_get(base + offset))
        elif isinstance(operand, bool):
            self.pieces.append(w.i32_const(1 if operand else 0))
        elif isinstance(operand, float):
            self.pieces.append(w.f64_const(operand))
        else:
            self.pieces.append(w.i64_const(int(operand)))

    def constant(self, type_, value) -> bytes:
        """La constante del tipo que el IR declara para ese valor.

        El tipo declarado manda y no el literal de Python: un puntero de
        `const.strref` llega tipado i64 y un `i32.const` en su sitio hace que el
        módulo no valide. Tampoco se puede deducir del literal, porque el mismo
        número es i64 en un sitio y i32 en otro según dónde va.
        """
        kind = type_ if isinstance(type_, str) else type_.kind
        if kind == "SINGLE":
            return w.f32_const(value)
        if kind == "DOUBLE":
            return w.f64_const(value)
        if valtype(type_) == w.I32:
            return w.i32_const(int(value))
        return w.i64_const(int(value))

    def push_local(self, local: int, want_kind: str) -> None:
        """`local.get` de un local, convertido al tipo que pide el destino.

        Hace falta porque una palabra suelta de un valor multi-palabra tiene el
        ancho del array, no el del resultado: la longitud de un array son
        cuatro bytes y `LEN` devuelve un INTEGER de ocho. Sin convertir, el
        módulo no valida; y si se convierte en el sitio equivocado, valida y
        calcula otra cosa.
        """
        self.pieces.append(w.local_get(local))
        tiene = self.local_types[local]
        quiere = valtype(want_kind)
        if tiene == quiere:
            return
        if tiene == w.I32 and quiere == w.I64:
            self.pieces.append(w.op_unary(w.I64_EXTEND_I32_S))
        elif tiene == w.I64 and quiere == w.I32:
            self.pieces.append(w.op_unary(w.I32_WRAP_I64))

    def store(self, value: sir.Value) -> None:
        """Guarda en los locales de `value` lo que hay en la pila.

        La última palabra es la que está arriba, así que se guarda de arriba
        abajo. En sentido contrario se guardan las palabras cambiadas y el
        módulo sigue validando.
        """
        base = self.local_of[value.id]
        for offset in reversed(range(words_of(value.type))):
            self.pieces.append(w.local_set(base + offset))

    # --------------------------------------------------------------- memoria

    @staticmethod
    def words_of_operand(value: sir.Value) -> int:
        return words_of(value.type)

    def min_i32(self, a_local: int, b_local: int) -> None:
        """`min(a, b)` con dos locales ya en la pila, como `a b a<b select`.

        El intérprete recorta las subcadenas al final del texto --`"hola
        mundo"[6..]` son cuatro bytes, no cinco-- así que un puntero y una
        longitud sin recortar dan de más y salen caracteres del heap
        siguiente.

        Los locales se leen con conversión a i32: `count` es un INTEGER de ocho
        bytes y `local.get` de un i64 encima de un `i32.lt_u` no valida.
        """
        pieces = self.pieces
        self.push_local(a_local, "COLOR")
        self.push_local(b_local, "COLOR")
        self.push_local(a_local, "COLOR")
        self.push_local(b_local, "COLOR")
        pieces.append(w.op_unary(w.I32_LT_U))
        pieces.append(w.op_unary(w.SELECT))

    def element_size(self, type_) -> int:
        return layout_of(type_).size

    def load_at(self, addr_local: int, type_) -> None:
        """Carga de `addr_local` el valor del tipo indicado.

        El destino de `load.index` es un valor del IR con tipo, y de ese tipo
        sale el tamaño del elemento. Un array de textos guarda un descriptor de
        ocho bytes por elemento, no el texto, así que son dos cargas.
        """
        kind = type_ if isinstance(type_, str) else type_.kind
        pieces = self.pieces
        if kind == "ARRAY":
            # Un array son cuatro i32 --puntero, longitud, capacidad y tipo
            # del elemento-- y no un i64. Cargar uno solo daba un módulo con la
            # pila mal, que es como se manifiesta `ARRAY OF ARRAY`, que el
            # intérprete sí soporta.
            for offset in (0, 4, 8, 12):
                pieces.append(w.local_get(addr_local))
                pieces.append(w.memarg(w.I32_LOAD, 2, offset))
            return
        if kind == "STRING":
            pieces.append(w.local_get(addr_local))
            pieces.append(w.memarg(w.I32_LOAD, 2, 0))
            pieces.append(w.local_get(addr_local))
            pieces.append(w.memarg(w.I32_LOAD, 2, 4))
        else:
            code, align = _LOAD_OF_KIND.get(kind, (w.I64_LOAD, 3))
            pieces.append(w.local_get(addr_local))
            pieces.append(w.memarg(code, align, 0))

    def store_at(self, addr_local: int, value: sir.Value) -> None:
        """Escribe `value` en la dirección de `addr_local`.

        Para un texto son dos escrituras separadas: el descriptor son ocho
        bytes, el puntero primero y la longitud después.
        """
        pieces = self.pieces
        base = self.local_of[value.id]
        kind = value.type.kind
        if kind == "ARRAY":
            # Cuatro escrituras, por el mismo motivo que las cuatro cargas: el
            # descriptor de un array ocupa dieciséis bytes.
            for offset in (0, 4, 8, 12):
                pieces.append(w.local_get(addr_local))
                pieces.append(w.local_get(base + offset // 4))
                pieces.append(w.memarg(w.I32_STORE, 2, offset))
            return
        if kind == "STRING":
            pieces.append(w.local_get(addr_local))
            pieces.append(w.local_get(base))
            pieces.append(w.memarg(w.I32_STORE, 2, 0))
            pieces.append(w.local_get(addr_local))
            pieces.append(w.local_get(base + 1))
            pieces.append(w.memarg(w.I32_STORE, 2, 4))
            return
        code, align = _STORE_OF_KIND.get(kind, (w.I64_STORE, 3))
        pieces.append(w.local_get(addr_local))
        pieces.append(w.local_get(base))
        pieces.append(w.memarg(code, align, 0))

    def index_address(self, array, index_operand, size):
        """Deja el índice i32 en `scratch_bytes` y la dirección en `scratch_addr`.

        El índice se envuelve a i32 *antes* de multiplicar: multiplicar en i64 y
        envolver después daría direcciones truncadas sin ningún error. Y hace
        falta en un local porque la comprobación de límites, que va después, no
        puede releer la pila.
        """
        self.push(index_operand)
        self.pieces.append(w.op_unary(w.I32_WRAP_I64))
        self.pieces.append(w.local_set(self.scratch_bytes))
        self.pieces.append(w.local_get(self.scratch_bytes))
        self.pieces.append(self.constant("COLOR", size))
        self.pieces.append(w.op_unary(w.I32_MUL))
        self.pieces.append(w.local_get(self.local_of[array.id]))
        self.pieces.append(w.op_unary(w.I32_ADD))
        self.pieces.append(w.local_set(self.scratch_addr))
        return self.scratch_addr

    def copy_bytes(self, dest_local, src_local, length_local):
        """Copia `length_local` bytes de `src_local` a `dest_local`.

        Es un `memory.copy`, que son cuatro bytes, y antes era una llamada a
        `@__copy`: una función interna con un bucle de bytes que el módulo
        declaraba una vez y pagaban todos los programas que concatenan.

        `memory.copy` es de *bulk memory*, posterior al MVP. Se usa porque todos
        los navegadores y todas las versiones de Node la tienen desde 2020, y
        porque su alternativa --un bucle de instrucciones-- pesa lo mismo en un
        programa pequeño y en uno grande. El riesgo real de una instrucción
        post-MVP no es que no exista: es que **alguien** tenga que enterarse, y
        por eso está en `TABLA_FC` del verificador y en las tablas del round
        trip. Una instrucción que el verificador no modela deja la pila
        desincronizada a partir de ese punto, y da verde por razones
        equivocadas.
        """
        self.push_local(dest_local, "COLOR")
        self.push_local(src_local, "COLOR")
        self.push_local(length_local, "COLOR")
        self.pieces.append(w.memory_copy())

    def check_bounds(self, array_base: int, index_local: int) -> None:
        """Trampa si el índice se sale, como hace el intérprete.

        El intérprete falla con `índice fuera de un array`, y un módulo que no
        comprueba lo que sale no calcula otra cosa: **escribe** en la memoria
        que sigue al array y corrompe el heap sin avisar. La paridad con el
        intérprete manda sobre los bytes.
        """
        pieces = self.pieces
        # i32: el índice es i64 y un negativo, envuelto, es enorme sin signo.
        pieces.append(w.local_get(index_local))
        pieces.append(w.local_get(array_base + 1))
        pieces.append(w.op_unary(w.I32_GE_U))
        pieces.append(w.if_())
        pieces.append(w.unreachable())
        pieces.append(w.end())

    def copy(self, source: int, target: sir.Value) -> None:
        for offset in range(words_of(target.type)):
            self.pieces.append(w.local_get(source + offset))
            self.pieces.append(w.local_set(self.local_of[target.id] + offset))

    def signature(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """La firma en WASM.

        Cada parámetro ocupa `layout.words` locales, así que un parámetro
        `STRING` son **dos** parámetros `i32` (puntero y longitud). Declarar
        uno solo dejaba el segundo local sin tipo: el módulo no validaba, y el
        verificador no lo veía porque su modelo de los parámetros venía de la
        misma función equivocada.
        """
        params: list[int] = []
        for _, type_ in self.func.params:
            params.extend([valtype(type_)] * words_of(type_))
        if self.func.return_type.kind == "VOID":
            return (tuple(params), ())
        # Devolver un texto son dos resultados, y eso es *valores múltiples*:
        # no está en el MVP de 2017, pero lo tienen todos los navegadores desde
        # 2020 y Node. Prohibirlo significa prohibir `FUNCTION` que devuelva
        # texto, que es la mitad de las funciones de BASIC. Se usa y se
        # documenta como requisito, no se esconde.
        words = words_of(self.func.return_type)
        return (tuple(params), (valtype(self.func.return_type),) * words)

    # ------------------------------------------------------------ compilación

    def compile(self) -> None:
        plan, jumps = control.build_plan(self.func.instrs)
        self._emit_node(plan, jumps, len(self.func.instrs))

    @staticmethod
    def _extent(node: control.Node, total: int) -> tuple[int, int]:
        """El rango de posiciones que cubre un nodo.

        La raíz no tiene rango cerrado en el plan --abarca el cuerpo entero-- y
        por eso se le da `[0, total)`.
        """
        if node.kind == "func":
            return (0, total)
        return (node.start, node.end)

    def _emit_node(self, node: control.Node, jumps, total: int) -> None:
        """Abre las construcciones y emite su cuerpo **en orden de posición**.

        El orden no es un detalle. Las instrucciones propias de un nodo están
        entre las de sus hijos, así que emitirlas todas primero --o todas
        después-- produce un módulo que valida y ejecuta las partes del
        programa como si estuvieran en otro sitio, sin ningún error.

        Con el orden equivocado, `PRINT numeros[0]` después de un bucle salía
        antes que el bucle: el módulo imprimía la primera línea del bucle y
        terminaba. Validaba, ejecutaba y no daba error, que es la peor forma de
        romperse.
        """
        inicio, fin = self._extent(node, total)
        cursor = inicio
        for child in node.children:
            for position in range(cursor, child.start):
                self._emit_one(position, jumps)
            cursor = child.end
            if child.kind == control.LOOP:
                self.pieces.append(w.loop())
            elif child.kind == control.BLOCK:
                self.pieces.append(w.block())
            self._emit_node(child, jumps, total)
            if child.kind in (control.LOOP, control.BLOCK):
                self.pieces.append(w.end())
        for position in range(cursor, fin):
            self._emit_one(position, jumps)

    def _emit_one(self, position: int, jumps) -> None:
        instr = self.func.instrs[position]
        if instr.op == "br_if.false":
            # El IR salta cuando la condición es falsa; WASM salta cuando es
            # distinta de cero. Hay que invertir, y un BOOLEAN es i32, así que
            # `i32.eqz` da exactamente lo que hace falta.
            self.push(instr.args[0])
            self.pieces.append(w.op_unary(w.I32_EQZ))
            self.pieces.append(w.br_if(jumps[position].depth))
            return
        if instr.op == "br":
            self.pieces.append(w.br(jumps[position].depth))
            return
        if instr.op == "label":
            return
        emitter = self.emitters.get(instr.op)
        if emitter is None:
            raise unsupported(instr.op, instr.span)
        emitter(self, instr)


# ============================================================ tabla de emisión


# Nativas que el backend emite con sus propias instrucciones. Se rellena al
# construir la tabla porque los emisores se definen después.
INLINE_NATIVES: dict = {}


def _build_emitters() -> dict:
    """El único sitio donde vive el lowering de cada operación."""
    table: dict = {}

    # ---- literales
    def const_of_type(c, instr):
        """`const.i64` y `const.f64` sólo se diferencian en quién fija el tipo."""
        c.pieces.append(c.constant(instr.args[0].type, instr.args[1]))
        c.store(instr.args[0])
    table["const.i64"] = const_of_type
    table["const.f64"] = const_of_type

    def const_bool(c, instr):
        c.pieces.append(w.i32_const(1 if instr.args[1] else 0))
        c.store(instr.args[0])
    table["const.bool"] = const_bool

    def const_strref(c, instr):
        offset = c.backend.string_offset(instr.args[1])
        c.pieces.append(c.constant(instr.args[0].type, offset))
        c.store(instr.args[0])
    table["const.strref"] = const_strref

    def const_str(c, instr):
        """(ptr, len): `const.strref` ya dejó el puntero en un local."""
        ptr = instr.args[1]
        length = instr.args[2]
        # La palabra del puntero es i32; si el `strref` venía tipado i64 hay
        # que estrecharlo, porque un local i64 no es un local i32 aunque valgan
        # lo mismo.
        c.push_local(c.local_of[ptr.id], instr.args[0].type.kind)
        c.pieces.append(w.local_set(c.local_of[instr.args[0].id]))
        c.pieces.append(w.i32_const(length))
        c.pieces.append(w.local_set(c.local_of[instr.args[0].id] + 1))
    table["const.str"] = const_str

    def const_zero(c, instr):
        target = instr.args[0]
        kind = target.type.kind
        if kind == "SINGLE":
            c.pieces.append(w.f32_const(0.0))
        elif kind == "DOUBLE":
            c.pieces.append(w.f64_const(0.0))
        elif valtype(target.type) == w.I32:
            c.pieces.append(w.i32_const(0))
        else:
            c.pieces.append(w.i64_const(0))
        c.store(target)
    table["const.zero"] = const_zero

    # ---- slots: ya son locales, así que un acceso es una copia
    def store_slot(c, instr):
        slot, value = instr.args[0], instr.args[1]
        base = c.slot_local[slot]
        if isinstance(value, sir.Value):
            source = c.local_of[value.id]
            for offset in range(words_of(value.type)):
                c.pieces.append(w.local_get(source + offset))
                c.pieces.append(w.local_set(base + offset))
        else:
            c.push(value)
            c.pieces.append(w.local_set(base))
    table["store.slot"] = store_slot

    def load_slot(c, instr):
        target, slot = instr.args[0], instr.args[1]
        base = c.slot_local[slot]
        for offset in range(words_of(target.type)):
            c.pieces.append(w.local_get(base + offset))
        c.store(target)
    table["load.slot"] = load_slot

    # ---- llamadas
    def call(c, instr):
        target, operands = instr.args[0], instr.args[1]
        returns = instr.type is not None and instr.type.kind != "VOID"
        destination = operands[0] if returns else None
        arguments = operands[1:] if returns else operands
        for argument in arguments:
            c.push(argument)
        c.pieces.append(w.call(c.backend.func_index(target)))
        if destination is not None:
            c.store(destination)
    table["call"] = call

    def call_native(c, instr):
        name, operands = instr.args[0], instr.args[1]
        # `NATIVE_IMPORT` con valor None significa "se emite aquí", no "no se
        # sabe": confundirlas hacía fallar `RGB` como si fuera una nativa
        # desconocida.
        if name in INLINE_NATIVES:
            INLINE_NATIVES[name](c, instr)
            return
        import_name = NATIVE_IMPORT.get(name)
        if import_name is None:
            raise unsupported(f"call.native '{name}'", instr.span)
        returns = bool(HOST_SIGNATURES[import_name][1])
        destination = operands[0] if returns else None
        arguments = operands[1:] if returns else operands
        for argument in arguments:
            c.push(argument)
        c.pieces.append(w.call(c.backend.host_index(import_name)))
        if destination is not None:
            c.store(destination)
    table["call.native"] = call_native

    def ret(c, instr):
        # Una FUNCTION sin valor devuelve sin operandos: leer args[0] a ciegas
        # daba IndexError en la primera función VOID del corpus.
        if instr.args and isinstance(instr.args[0], sir.Value):
            c.push(instr.args[0])
        c.pieces.append(w.op(0x0F))     # RETURN
    table["ret"] = ret

    # ---- arrays
    #
    # Un array del IR son tres palabras: (ptr, len, cap). Los elementos van
    # seguidos en memoria lineal desde `ptr`, con el tamaño que dicta el tipo
    # del elemento. El crecimiento es del bump allocator y no reubica: por eso
    # `cap` y `len` empiezan iguales y el compilador tiene que prohibido
    # crecer un array en la Fase 2.

    def array_alloc(c, instr):
        target, code, count = instr.args
        size = layout_by_code(code).size
        # bytes = count * size, y `@__bump` devuelve el puntero ya en la pila.
        # El tamaño se multiplica en i64 y se estrecha después: al revés, un
        # array de más de 2^31 elementos daría una reserva de un byte.
        c.push(count)
        c.pieces.append(c.constant("INTEGER", size))
        c.pieces.append(w.op_unary(w.I64_MUL))
        c.pieces.append(w.op_unary(w.I32_WRAP_I64))
        c.pieces.append(w.call(c.backend.bump_index))
        c.pieces.append(w.local_set(c.local_of[target.id]))
        # len y cap empiezan iguales: el bump allocator no reubica nunca.
        for offset in (1, 2):
            c.push(count)
            c.pieces.append(w.op_unary(w.I32_WRAP_I64))
            c.pieces.append(w.local_set(c.local_of[target.id] + offset))
        # La cuarta palabra del descriptor es el tipo del elemento. La lee el
        # host al imprimir un array de arrays, y no se puede deducir de los
        # datos: un array de ceros es indistinguible de uno de enteros.
        c.pieces.append(w.i32_const(code))
        c.pieces.append(w.local_set(c.local_of[target.id] + 3))
    table["array.alloc"] = array_alloc

    def array_len(c, instr):
        target, array = instr.args[0], instr.args[1]
        # La longitud es la segunda palabra del array y es i32; `LEN` devuelve
        # INTEGER, que es i64. Sin ensanchar, el módulo no valida.
        c.push_local(c.local_of[array.id] + 1, "INTEGER")
        c.store(target)
    table["array.len"] = array_len

    def load_index(c, instr):
        target, array, index = instr.args
        addr = c.index_address(array, index, c.element_size(target.type))
        c.check_bounds(c.local_of[array.id], c.scratch_bytes)
        c.load_at(addr, target.type)
        c.store(target)
    table["load.index"] = load_index

    def store_index(c, instr):
        array, index, value = instr.args
        addr = c.index_address(array, index, c.element_size(value.type))
        c.check_bounds(c.local_of[array.id], c.scratch_bytes)
        c.store_at(addr, value)
    table["store.index"] = store_index

    def array_init(c, instr):
        array, offset, item = instr.args
        # El desplazamiento es un inmediato, pero el camino es el mismo que el
        # de un índice variable: comprobarlo cuesta lo mismo y evita que un
        # literal se salga sin que nadie lo note.
        addr = c.index_address(array, offset, c.element_size(item.type))
        c.check_bounds(c.local_of[array.id], c.scratch_bytes)
        c.store_at(addr, item)
    table["array.init"] = array_init



    # ---- host
    def host_op(c, instr):
        import_name = HOST_OPS[instr.op]
        # `flip` no lleva operandos: no hay nada que empujar. Leerlos siempre
        # daba `IndexError` con una operación que el IR sí emite bien.
        if instr.args:
            target = instr.args[0]
            c.push(target)
            # El operando se convierte al tipo que pide la firma, no al que
            # resulte de su tipo en el IR. `PRINT` de un COLOR emite
            # `print_int`, que toma i64, y el COLOR son cuatro bytes: sin
            # ensanchar, el módulo no valida.
            #
            # Un texto son dos palabras y su firma son dos i32: encaja. Un
            # SINGLE es f32 y `print_num` pide f64: `f64.promote_f32`.
            wanting = c.backend.host_signature(import_name)[0]
            tiene = valtype(target.type)
            if len(wanting) == 1 and words_of(target.type) == 1:
                if tiene == w.I32 and wanting[0] == w.I64:
                    # Un COLOR es un patrón de cuatro bytes, no un número con
                    # signo: `RGB(255,200,0)` es 4294952960, y ensanchar con
                    # signo daría -14336. El intérprete lo trata como sin signo
                    # y el módulo tiene que hacer lo mismo.
                    cero_o_signo = (w.I64_EXTEND_I32_U
                                    if target.type.kind == "COLOR"
                                    else w.I64_EXTEND_I32_S)
                    c.pieces.append(w.op_unary(cero_o_signo))
                elif tiene == w.I64 and wanting[0] == w.I32:
                    c.pieces.append(w.op_unary(w.I32_WRAP_I64))
                elif tiene == w.F32 and wanting[0] == w.F64:
                    c.pieces.append(w.op_unary(w.F64_PROMOTE_F32))
        c.pieces.append(w.call(c.backend.host_index(import_name)))
    for op_name in HOST_OPS:
        table[op_name] = host_op

    def print_arr(c, instr):
        """`PRINT` de un array: una llamada a `@__printArray`.

        Las cuatro palabras del descriptor --puntero, longitud, capacidad y
        tipo del elemento-- son los cuatro argumentos, y la función es del
        módulo, no un import: el formateo es semántica del lenguaje.
        """
        c.push(instr.args[0])
        c.pieces.append(w.call(c.backend.print_array_index))
    table["host.print_arr"] = print_arr

    def newline(c, instr):
        c.pieces.append(w.call(c.backend.host_index("source.newline")))
    table["newline"] = newline

    # ---- aritmética entera. WASM usa `div_s`/`rem_s` con truncamiento hacia
    # cero; `div.i64` en BASIC es división entera también, así que coinciden.
    def make_binop(code):
        def binop(c, instr):
            c.push(instr.args[1])
            c.push(instr.args[2])
            c.pieces.append(w.op_unary(code))
            c.store(instr.args[0])
        return binop

    table["add.i64"] = make_binop(w.I64_ADD)
    table["sub.i64"] = make_binop(w.I64_SUB)
    table["mul.i64"] = make_binop(w.I64_MUL)
    table["div.i64"] = make_binop(w.I64_DIV_S)
    table["rem.i64"] = make_binop(w.I64_REM_S)
    table["shl.i64"] = make_binop(w.I64_SHL)
    table["shr.i64"] = make_binop(w.I64_SHR_S)
    table["and.i64"] = make_binop(w.I64_AND)
    table["or.i64"] = make_binop(w.I64_OR)
    table["xor.i64"] = make_binop(w.I64_XOR)
    table["and.bool"] = make_binop(w.I32_AND)
    table["or.bool"] = make_binop(w.I32_OR)
    table["xor.bool"] = make_binop(w.I32_XOR)

    def neg_i64(c, instr):
        c.pieces.append(w.i64_const(0))
        c.push(instr.args[1])
        c.pieces.append(w.op_unary(w.I64_SUB))
        c.store(instr.args[0])
    table["neg.i64"] = neg_i64

    # ---- comparación
    def make_compare(code):
        def compare(c, instr):
            c.push(instr.args[1])
            c.push(instr.args[2])
            c.pieces.append(w.op_unary(code))
            c.store(instr.args[0])
        return compare

    table["lt.i64"] = make_compare(w.I64_LT_S)
    table["le.i64"] = make_compare(w.I64_LE_S)
    table["gt.i64"] = make_compare(w.I64_GT_S)
    table["ge.i64"] = make_compare(w.I64_GE_S)
    table["not.bool"] = lambda c, i: (
        c.push(i.args[1]), c.pieces.append(w.op_unary(w.I32_EQZ)),
        c.store(i.args[0]))

    def eq(c, instr):
        # `eq` llega con el tipo en la propia instrucción, porque compara
        # enteros, booleanos y textos con la misma palabra.
        left = instr.args[1]
        kind = (left.type.kind if isinstance(left, sir.Value)
                else instr.type.kind)
        code = {w.I64: w.I64_EQ, w.I32: w.I32_EQ}.get(
            wasm_type_of_kind(kind), w.I64_EQ)
        c.push(instr.args[1])
        c.push(instr.args[2])
        c.pieces.append(w.op_unary(code))
        c.store(instr.args[0])
    table["eq"] = eq

    def select(c, instr):
        # El IR es `select destino, condición, si, no` y WASM deja la pila como
        # (si, no, condición). El orden importa y no se parece: empujarlo igual
        # que en el IR hace que `FOR` salte siempre a la salida, porque se está
        # evaluando la condición en el bucle equivocado.
        _, condition, si, no = instr.args
        c.push(si)
        c.push(no)
        c.push(condition)
        c.pieces.append(w.op_unary(w.SELECT))
        c.store(instr.args[0])
    table["select"] = select

    def to_bool(c, instr):
        # "Falsy" es cero o NIL; para un entero, distinto de cero.
        c.push(instr.args[1])
        c.pieces.append(w.i64_const(0))
        c.pieces.append(w.op_unary(w.I64_NE))
        c.store(instr.args[0])
    table["to.bool"] = to_bool

    # ---- conversiones
    #
    # El opcode depende del tipo del **operando**, no del del resultado: pasar
    # un SINGLE a un DOUBLE es `f64.promote_f32` y pasar un INTEGER a un DOUBLE
    # es `f64.convert_i64_s`. Elegir por el resultado daba un módulo que no
    # valida, porque la instrucción espera el operando que el destino no es.

    def conversion(c, instr, destino_kind):
        origen = instr.args[1]
        origen_kind = (origen.type.kind if isinstance(origen, sir.Value)
                       else destino_kind)
        code = _CONVERSION.get((origen_kind, destino_kind))
        if code is None:
            raise unsupported(f"conv de {origen_kind} a {destino_kind}",
                              instr.span)
        c.push(origen)
        c.pieces.append(w.op_unary(code))
        c.store(instr.args[0])
    table["conv.i64"] = lambda c, i: conversion(c, i, "INTEGER")
    table["conv.f32"] = lambda c, i: conversion(c, i, "SINGLE")
    table["conv.f64"] = lambda c, i: conversion(c, i, "DOUBLE")


    # ---- aritmética flotante y matemática
    #
    # Un SINGLE es f32 de verdad y un DOUBLE es f64 de verdad: no son un f32
    # disfrazado dentro de un i64, así que cada uno usa sus instrucciones.
    table["add.f32"] = make_binop(w.F32_ADD)
    table["sub.f32"] = make_binop(w.F32_SUB)
    table["mul.f32"] = make_binop(w.F32_MUL)
    table["div.f32"] = make_binop(w.F32_DIV)
    table["add.f64"] = make_binop(w.F64_ADD)
    table["sub.f64"] = make_binop(w.F64_SUB)
    table["mul.f64"] = make_binop(w.F64_MUL)
    table["div.f64"] = make_binop(w.F64_DIV)
    table["lt.f32"] = make_compare(w.F32_LT)
    table["le.f32"] = make_compare(w.F32_LE)
    table["gt.f32"] = make_compare(w.F32_GT)
    table["ge.f32"] = make_compare(w.F32_GE)
    table["lt.f64"] = make_compare(w.F64_LT)
    table["le.f64"] = make_compare(w.F64_LE)
    table["gt.f64"] = make_compare(w.F64_GT)
    table["ge.f64"] = make_compare(w.F64_GE)

    def neg_f32(c, instr):
        # `f32.neg` es una instrucción propia; restar de cero obligaría a tener
        # un cero f32 en un local.
        c.push(instr.args[1])
        c.pieces.append(w.op_unary(w.F32_NEG))
        c.store(instr.args[0])
    table["neg.f32"] = neg_f32

    def neg_f64(c, instr):
        c.push(instr.args[1])
        c.pieces.append(w.op_unary(w.F64_NEG))
        c.store(instr.args[0])
    table["neg.f64"] = neg_f64

    def sqrt_f64(c, instr):
        c.push(instr.args[1])
        c.pieces.append(w.op_unary(w.F64_SQRT))
        c.store(instr.args[0])
    table["sqrt.f64"] = sqrt_f64

    def floor_f64(c, instr):
        c.push(instr.args[1])
        c.pieces.append(w.op_unary(w.F64_FLOOR))
        c.store(instr.args[0])
    table["floor.f64"] = floor_f64
    table["pow.f64"] = lambda c, i: host_math(c, i, "pow")

    def divfloor_i64(c, instr):
        target, left, right = instr.args
        # `i64.div_s` trunca hacia cero y `DIV` redondea hacia menos infinito:
        # -7/2 da -3 con div_s y -4 con divfloor. Se corrige restando uno cuando
        # el resto no es cero y los signos no coinciden.
        c.push(left)
        c.push(right)
        c.pieces.append(w.op_unary(w.I64_DIV_S))
        c.store(target)
        c.push(left)
        c.push(right)
        c.pieces.append(w.op_unary(w.I64_REM_S))
        c.pieces.append(w.i64_const(0))
        c.pieces.append(w.op_unary(w.I64_NE))
        c.push(left)
        c.pieces.append(w.i64_const(63))
        c.pieces.append(w.op_unary(w.I64_SHR_S))
        c.push(right)
        c.pieces.append(w.i64_const(63))
        c.pieces.append(w.op_unary(w.I64_SHR_S))
        c.pieces.append(w.op_unary(w.I64_NE))
        # Las dos condiciones son i32 porque las comparaciones lo son, y el
        # `and` tiene que ser el de i32.
        c.pieces.append(w.op_unary(w.I32_AND))
        c.pieces.append(w.if_())
        c.pieces.append(w.local_get(c.local_of[target.id]))
        c.pieces.append(w.i64_const(1))
        c.pieces.append(w.op_unary(w.I64_SUB))
        c.pieces.append(w.local_set(c.local_of[target.id]))
        c.pieces.append(w.end())
    table["divfloor.i64"] = divfloor_i64

    def idiv_i64(c, instr):
        # El intérprete usa la misma división entera para `DIV` y para `\`, y las
        # dos truncan hacia menos infinito; por eso aquí es el mismo código que
        # `divfloor.i64` y no un `i64.div_s`. Si el intérprete llegara a
        # separarlas, este es el sitio donde se rompen las dos.
        divfloor_i64(c, instr)
    table["idiv.i64"] = idiv_i64

    def host_math(c, instr, name):
        """`sin`, `cos` y `pow` no son instrucciones del MVP: van al host."""
        target = instr.args[0]
        for argument in instr.args[1:]:
            c.push(argument)
        c.pieces.append(w.call(c.backend.host_index(f"source.{name}")))
        c.store(target)
    table["sin.f64"] = lambda c, i: host_math(c, i, "sin")
    table["cos.f64"] = lambda c, i: host_math(c, i, "cos")

    def rgb(c, instr):
        destination, red, green, blue = instr.args[1]
        # Un COLOR son tres bytes --rojo alto, verde, azul-- y empaquetarlos son
        # cuatro instrucciones, así que es inline y no del host. Los tres
        # componentes son INTEGER (i64) y hay que estrechar antes de desplazar;
        # el `or` va **entre** componentes, porque el primero no tiene con qué
        # combinarse todavía.
        # Cuatro bytes, con alfa a 255: es como lo empaqueta el intérprete
        # (`(a << 24) | (r << 16) | (g << 8) | b`) y es lo que espera el canvas.
        c.pieces.append(w.i32_const(255))
        c.pieces.append(w.i32_const(24))
        c.pieces.append(w.op_unary(w.I32_SHL))
        # **Cada** componente se combina con `or`, incluido el primero: el alfa
        # ya está en la pila, así que el rojo también tiene con qué. Omitir ese
        # primer `or` daba 0x00FFC800 en vez de 0xFFFFC800 -- el mismo número en
        # decimal con los dos últimos bytes cambiados, que es como se cuela un
        # error de empaquetado de color.
        for component, shift in ((red, 16), (green, 8), (blue, 0)):
            c.push(component)
            c.pieces.append(w.op_unary(w.I32_WRAP_I64))
            if shift:
                c.pieces.append(w.i32_const(shift))
                c.pieces.append(w.op_unary(w.I32_SHL))
            c.pieces.append(w.op_unary(w.I32_OR))
        c.store(destination)
    INLINE_NATIVES["rgb"] = rgb

    # ---- textos
    def str_len(c, instr):
        target, text = instr.args[0], instr.args[1]
        # La longitud es la segunda palabra del texto, y es i32; `LEN` devuelve
        # un INTEGER de ocho bytes.
        c.push_local(c.local_of[text.id] + 1, "INTEGER")
        c.store(target)
    table["str.len"] = str_len

    def str_byte(c, instr):
        target, text, index = instr.args
        base = c.local_of[text.id]
        c.push(index)
        c.pieces.append(w.op_unary(w.I32_WRAP_I64))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.check_bounds(base, c.scratch_bytes)
        c.pieces.append(w.local_get(base))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))
        # El byte es i32 y `ASC` devuelve un INTEGER: hay que ensanchar.
        c.pieces.append(w.op_unary(w.I64_EXTEND_I32_S))
        c.store(target)
    table["str.byte"] = str_byte

    def str_left(c, instr):
        # `LEFT$(s, n)`: los primeros `n` bytes. El puntero no se mueve, así que
        # no hay que copiar nada: el texto origen no se modifica.
        target, text, count = instr.args
        base = c.local_of[text.id]
        # El puntero no se mueve; la longitud se recorta al texto. Empujar el
        # contador sin recortar y luego el `min` dejaba tres valores en la pila
        # y `store` consumía los dos últimos.
        c.push_local(base, "STRING")
        c.min_i32(c.local_of[count.id], base + 1)
        c.store(target)
    table["str.left"] = str_left

    def str_right(c, instr):
        # `RIGHT$(s, n)`: los últimos `n` bytes. El puntero se avanza hasta el
        # final menos `n` y la longitud se queda; no hay que copiar.
        #
        # Las dos palabras se apilan y las guarda `store` de una vez. Guardar la
        # primera a mano y luego llamar a `store` deja la pila con un valor
        # menos de los que `store` saca, y el módulo no valida.
        target, text, count = instr.args
        base = c.local_of[text.id]
        largo = c.local_of[count.id]
        # `n` se recorta a la longitud del texto: `RIGHT$(s, 99)` son todos los
        # bytes, no 99. Con `n` recortado, el puntero es `base + (len - n)`.
        c.min_i32(largo, base + 1)
        c.pieces.append(w.local_set(c.scratch_bytes))       # n recortado
        n = c.scratch_bytes
        c.push_local(base, "COLOR")
        c.push_local(base + 1, "COLOR")
        c.push_local(n, "COLOR")
        c.pieces.append(w.op_unary(w.I32_SUB))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.push_local(n, "COLOR")
        c.store(target)
    table["str.right"] = str_right

    def str_mid(c, instr):
        # `MID$(s, desde, cuantos)`: una subcadena, que es un puntero al byte
        # `desde` y una longitud. No hay que copiar nada.
        #
        # Las dos palabras se apilan y las guarda `store` de una vez. Guardar la
        # primera a mano y luego llamar a `store` deja la pila con un valor
        # menos de los que `store` saca, y el módulo no valida.
        target, text, start, count = instr.args
        base = c.local_of[text.id]
        # Lo que queda desde `start` es `len - start`, y la longitud pedida se
        # recorta a eso: pedir 5 bytes desde la posición 6 de un texto de 10 da
        # 4, no 5.
        c.push_local(c.local_of[start.id], "COLOR")
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.pieces.append(w.local_get(base + 1))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.op_unary(w.I32_SUB))
        c.pieces.append(w.local_set(c.scratch_addr))       # len - start
        c.push_local(base, "COLOR")
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.op_unary(w.I32_ADD))             # puntero
        c.min_i32(c.local_of[count.id], c.scratch_addr)
        c.store(target)
    table["str.mid"] = str_mid

    def str_sub(c, instr):
        # `MID$(s, i, 1)`: un texto de un byte. Se reserva un byte en el heap y
        # se copia, en vez de apuntar dentro del original: el intérprete cuenta
        # una alocación aquí, y un texto que se solapa con otro haría que
        # escribir en uno cambiara el otro.
        target, text, index = instr.args
        base = c.local_of[text.id]
        c.push(index)
        c.pieces.append(w.op_unary(w.I32_WRAP_I64))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.check_bounds(base, c.scratch_bytes)
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.call(c.backend.bump_index))
        c.pieces.append(w.local_set(c.local_of[target.id]))
        c.pieces.append(w.local_get(c.local_of[target.id]))
        c.pieces.append(w.local_get(base))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))
        c.pieces.append(w.memarg(w.I32_STORE8, 0, 0))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.local_set(c.local_of[target.id] + 1))
    table["str.sub"] = str_sub

    def str_from_code(c, instr):
        # ADR 0021: `CHR(n)`. La forma es la de `str.sub` sin el texto origen:
        # reservar un byte en el heap, escribirlo, y decir que el texto mide uno.
        # Sin la linea de `check_bounds` que tiene `str.sub` porque aqui no hay
        # indice que acotar: el byte es el valor, no una posicion dentro de otro.
        target, code = instr.args
        c.push(code)
        c.pieces.append(w.op_unary(w.I32_WRAP_I64))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.call(c.backend.bump_index))
        c.pieces.append(w.local_set(c.local_of[target.id]))
        c.pieces.append(w.local_get(c.local_of[target.id]))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.memarg(w.I32_STORE8, 0, 0))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.local_set(c.local_of[target.id] + 1))
    table["str.from_code"] = str_from_code

    def str_concat(c, instr):
        target, left, right = instr.args
        left_base, right_base = c.local_of[left.id], c.local_of[right.id]
        # El destino se reserva una vez, con la suma de las dos longitudes.
        # Pedir dos reservas y pegarlas en la memoria contigua sería un
        # `memmove` a mano y no un concatenador.
        c.push_local(left_base + 1, "COLOR")
        c.push_local(right_base + 1, "COLOR")
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.call(c.backend.bump_index))
        c.pieces.append(w.local_set(c.local_of[target.id]))
        c.copy_bytes(c.local_of[target.id], left_base, left_base + 1)
        # El segundo trozo va justo detrás del primero.
        c.pieces.append(w.local_get(c.local_of[target.id]))
        c.pieces.append(w.local_get(left_base + 1))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.copy_bytes(c.scratch_bytes, right_base, right_base + 1)
        c.push_local(c.local_of[target.id], "STRING")
        c.push_local(left_base + 1, "COLOR")
        c.push_local(right_base + 1, "COLOR")
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.store(target)
    table["str.concat"] = str_concat

    def str_eq_byte(c, instr):
        target, text, index, needle = instr.args
        base = c.local_of[text.id]
        needle_base = c.local_of[needle.id]
        # Sólo hace falta `índice < longitud`: el índice viene envuelto a i32, así
        # que uno negativo es enorme sin signo y falla la comparación sola.
        #
        # La versión anterior añadía un segundo predicado, `índice >= 0` **sin
        # signo**, que es `0 <u 0` -- falso. Eso hacía que la columna 0 de
        # cualquier texto no coincidiera nunca, y el laberinto no dibujaba ni una
        # pared.
        c.push(index)
        c.pieces.append(w.op_unary(w.I32_WRAP_I64))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.local_get(base + 1))
        c.pieces.append(w.op_unary(w.I32_LT_U))
        # El `if` **devuelve** el booleano: las dos ramas dejan un i32, así que
        # el bloque no puede ser de tipo vacío. Con `if_()` a secas el módulo no
        # valida, y el error de Node ("type mismatch") no dice qué bloque es.
        c.pieces.append(w.if_(w.I32))
        c.pieces.append(w.local_get(base))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))
        c.pieces.append(w.local_get(needle_base))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))
        c.pieces.append(w.op_unary(w.I32_EQ))
        c.pieces.append(w.else_())
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.end())
        c.store(target)
    table["str.eq_byte"] = str_eq_byte

    # ---- división y módulo de flotantes
    #
    # WASM tiene `f64.div`, `f64.floor` y `f64.trunc`, así que las tres
    # operaciones son dos instrucciones cada una. `rem.f64` no tiene equivalente:
    # no hay `f64.rem` en el MVP, y se calcula como `a - trunc(a/b) * b`, que es
    # el resto con el signo del dividendo, que es lo que quiere `MOD` en BASIC.
    def divfloor_f64(c, instr):
        c.push(instr.args[1])
        c.push(instr.args[2])
        c.pieces.append(w.op_unary(w.F64_DIV))
        c.pieces.append(w.op_unary(w.F64_FLOOR))
        c.store(instr.args[0])
    table["divfloor.f64"] = divfloor_f64

    def idiv_f64(c, instr):
        c.push(instr.args[1])
        c.push(instr.args[2])
        c.pieces.append(w.op_unary(w.F64_DIV))
        c.pieces.append(w.op_unary(w.F64_TRUNC))
        c.store(instr.args[0])
    table["idiv.f64"] = idiv_f64

    def rem_f64(c, instr):
        target, left, right = instr.args
        cociente = c.scratch_f64
        c.push(left)
        c.push(right)
        c.pieces.append(w.op_unary(w.F64_DIV))
        c.pieces.append(w.op_unary(w.F64_TRUNC))
        c.pieces.append(w.local_set(cociente))
        c.push(left)
        c.pieces.append(w.local_get(cociente))
        c.push(right)
        c.pieces.append(w.op_unary(w.F64_MUL))
        c.pieces.append(w.op_unary(w.F64_SUB))
        c.store(target)
    table["rem.f64"] = rem_f64

    # ---- comparación de textos
    #
    # `=` sobre dos textos es igualdad byte a byte; `COMPARE` es la de tres vías
    # de BASIC. Los dos son un recorrido con salida temprana, y la salida desde
    # dentro de un bucle se hace con `br` a la construcción de fuera: no hay
    # `break` en WASM y el `if` de la comparación se emite como bloque de tipo
    # vacío.

    def _byte_de(c, base_local, indice_local):
        c.pieces.append(w.local_get(base_local))
        c.pieces.append(w.local_get(indice_local))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))

    def str_eq(c, instr):
        target, left, right = instr.args
        base, aguja = c.local_of[left.id], c.local_of[right.id]
        largo, largo_aguja = base + 1, aguja + 1
        # resultado = 1 (iguales), y se va a 0 en cuanto se sabe que no.
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.local_set(c.scratch_addr))
        c.pieces.append(w.local_get(largo))
        c.pieces.append(w.local_get(largo_aguja))
        c.pieces.append(w.op_unary(w.I32_NE))
        c.pieces.append(w.if_())
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(c.scratch_addr))
        c.pieces.append(w.else_())
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.pieces.append(w.block())
        c.pieces.append(w.loop())
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.local_get(largo))
        c.pieces.append(w.op_unary(w.I32_GE_U))
        c.pieces.append(w.br_if(1))
        _byte_de(c, base, c.scratch_bytes)
        _byte_de(c, aguja, c.scratch_bytes)
        c.pieces.append(w.op_unary(w.I32_NE))
        c.pieces.append(w.if_())
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(c.scratch_addr))
        c.pieces.append(w.br(2))
        c.pieces.append(w.end())
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.pieces.append(w.br(0))
        c.pieces.append(w.end())
        c.pieces.append(w.end())
        c.pieces.append(w.end())
        c.pieces.append(w.local_get(c.scratch_addr))
        if valtype(target.type) == w.I64:
            c.pieces.append(w.op_unary(w.I64_EXTEND_I32_S))
        c.store(target)
    table["str.eq"] = str_eq

    def str_compare(c, instr):
        target, left, right = instr.args
        base, aguja = c.local_of[left.id], c.local_of[right.id]
        largo, largo_aguja = base + 1, aguja + 1
        i, resultado = c.scratch_bytes, c.scratch_addr
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(resultado))          # iguales
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(i))
        # Dos bloques, no uno. El bucle tiene que salir **antes** de la
        # comparación de longitudes --que sólo tiene sentido si los prefijos
        # coinciden-- y la salida por diferencia tiene que **saltársela**.
        #
        # Con un solo bloque, `br_if` al salir del bucle se iba también de la
        # comparación de longitudes y `COMPARE("ab", "abc")` daba 0.
        # Profundidades desde dentro del bucle: B(0), D(1), A(2).
        c.pieces.append(w.block())        # A
        c.pieces.append(w.block())        # D
        c.pieces.append(w.loop())         # B
        c.pieces.append(w.local_get(i))
        c.pieces.append(w.local_get(largo))
        c.pieces.append(w.op_unary(w.I32_GE_U))
        c.pieces.append(w.br_if(1))
        c.pieces.append(w.local_get(i))
        c.pieces.append(w.local_get(largo_aguja))
        c.pieces.append(w.op_unary(w.I32_GE_U))
        c.pieces.append(w.br_if(1))
        _byte_de(c, base, i)
        c.pieces.append(w.local_set(c.scratch_loop))
        _byte_de(c, aguja, i)
        c.pieces.append(w.local_set(c.scratch_quedan))
        c.pieces.append(w.local_get(c.scratch_loop))
        c.pieces.append(w.local_get(c.scratch_quedan))
        c.pieces.append(w.op_unary(w.I32_LT_U))
        c.pieces.append(w.if_())
        c.pieces.append(w.i32_const(-1))
        c.pieces.append(w.local_set(resultado))
        c.pieces.append(w.br(3))          # if(0), B(1), D(2), A(3)
        c.pieces.append(w.end())
        c.pieces.append(w.local_get(c.scratch_quedan))
        c.pieces.append(w.local_get(c.scratch_loop))
        c.pieces.append(w.op_unary(w.I32_LT_U))
        c.pieces.append(w.if_())
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.local_set(resultado))
        c.pieces.append(w.br(3))
        c.pieces.append(w.end())
        c.pieces.append(w.local_get(i))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_set(i))
        c.pieces.append(w.br(0))
        c.pieces.append(w.end())          # B
        c.pieces.append(w.end())          # D: aquí cae el bucle al agotarse
        # Los prefijos son iguales: gana el más largo.
        c.pieces.append(w.local_get(largo))
        c.pieces.append(w.local_get(largo_aguja))
        c.pieces.append(w.op_unary(w.I32_GT_U))
        c.pieces.append(w.if_())
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.local_set(resultado))
        c.pieces.append(w.else_())
        c.pieces.append(w.local_get(largo))
        c.pieces.append(w.local_get(largo_aguja))
        c.pieces.append(w.op_unary(w.I32_LT_U))
        c.pieces.append(w.if_())
        c.pieces.append(w.i32_const(-1))
        c.pieces.append(w.local_set(resultado))
        c.pieces.append(w.end())          # cierra el `if` de la longitud
        c.pieces.append(w.end())          # cierra el `if`/`else` de la longitud
        c.pieces.append(w.end())          # cierra A
        c.pieces.append(w.local_get(resultado))
        if valtype(target.type) == w.I64:
            c.pieces.append(w.op_unary(w.I64_EXTEND_I32_S))
        c.store(target)
    table["str.compare"] = str_compare

    def str_lenu(c, instr):
        target, text = instr.args[0], instr.args[1]
        # `LENU` cuenta caracteres y `LEN` cuenta bytes. Contar caracteres es
        # recorrer el texto saltando los bytes de continuación de UTF-8, que son
        # los que tienen los tres bits altos a uno: si se contasen como
        # caracteres, "ñ" valdría dos.
        base = c.local_of[text.id]
        # El índice arranca en **cero**, no en el puntero. Poner el puntero
        # hacía que el bucle saliera en la primera vuelta y `LENU` devolvía 0
        # para cualquier texto.
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(c.scratch_addr))
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(c.scratch_loop))
        c.pieces.append(w.block())
        c.pieces.append(w.loop())
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.local_get(base + 1))
        c.pieces.append(w.op_unary(w.I32_GE_U))
        c.pieces.append(w.br_if(1))
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))
        c.pieces.append(w.i32_const(0xC0))
        c.pieces.append(w.op_unary(w.I32_AND))
        c.pieces.append(w.i32_const(0x80))
        c.pieces.append(w.op_unary(w.I32_NE))
        c.pieces.append(w.if_())
        c.pieces.append(w.local_get(c.scratch_loop))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_set(c.scratch_loop))
        c.pieces.append(w.end())
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_set(c.scratch_addr))
        c.pieces.append(w.br(0))
        c.pieces.append(w.end())
        c.pieces.append(w.end())
        c.pieces.append(w.local_get(c.scratch_loop))
        # El contador va en un temporal i32 porque comparar direcciones es más
        # barato en 32 bits, pero `LENU` devuelve un INTEGER de ocho.
        if valtype(target.type) == w.I64:
            c.pieces.append(w.op_unary(w.I64_EXTEND_I32_S))
        c.store(target)
    table["str.lenu"] = str_lenu

    # ---- `STR(x)` y `VAL(x)`
    #
    # El formateo está en el glue porque un formateador de DOUBLE a texto son
    # unas doscientas instrucciones de WASM, y en JavaScript son dos. Lo que sí
    # es del backend es decidir a quién llamar: `to.str` puede recibir un
    # INTEGER, un SINGLE, un DOUBLE, un BOOLEAN o un COLOR, y pasar un entero
    # por f64 cambiaría sus últimos dígitos.
    def to_str(c, instr):
        target, operand = instr.args[0], instr.args[1]
        kind = operand.type.kind
        import_name = sir.HOST_FORMATTING["to.str"].get(kind)
        if import_name is None:
            raise unsupported(f"to.str de {kind}", instr.span)
        # Un buffer del heap donde el glue escribe el texto. 32 bytes bastan
        # para un f64 con seis decimales significativos más el signo.
        c.pieces.append(w.i32_const(32))
        c.pieces.append(w.call(c.backend.bump_index))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.push(operand)
        if kind == "SINGLE":
            c.pieces.append(w.op_unary(w.F64_PROMOTE_F32))
        if kind == "COLOR":
            # Un COLOR son cuatro bytes y `int2str` toma un INTEGER de ocho.
            # Sin signo, porque es un patrón de bits: igual que en `print_int`.
            c.pieces.append(w.op_unary(w.I64_EXTEND_I32_U))
        if kind in ("DOUBLE", "SINGLE"):
            # El modo 0 es el formato de `PRINT` y `STR`: enteros sin decimales
            # y seis cifras significativas en los demás. El 1 son decimales
            # fijos, para `FORMAT`.
            c.pieces.append(w.i32_const(0))
        c.pieces.append(w.call(c.backend.host_index(import_name)))
        c.pieces.append(w.local_set(c.local_of[target.id]))
        c.pieces.append(w.local_get(c.local_of[target.id]))
        c.pieces.append(w.call(c.backend.host_index("source.str_len")))
        c.pieces.append(w.local_set(c.local_of[target.id] + 1))
    table["to.str"] = to_str

    def to_num(c, instr):
        target, text = instr.args[0], instr.args[1]
        base = c.local_of[text.id]
        c.pieces.append(w.local_get(base))
        c.pieces.append(w.local_get(base + 1))
        c.pieces.append(w.call(c.backend.host_index("source.str2num")))
        # `VAL("42")` es un INTEGER si el destino lo es: el host devuelve f64
        # porque es lo único que sabe leer, y el truncado es una instrucción.
        if target.type.kind == "INTEGER":
            c.pieces.append(w.op_unary(w.I64_TRUNC_F64_S))
        elif target.type.kind == "SINGLE":
            c.pieces.append(w.op_unary(w.F32_DEMOTE_F64))
        c.store(target)
    table["to.num"] = to_num

    def buscar(c, instr, primer_match):
        """`CONTAINS` e `INDEXOF`: el mismo recorrido, distinto final.

        Se busca la aguja comparando byte a byte desde cada posición. Es lo
        más corto que funciona en el MVP: `memory.match` no existe y un
        polinomio de Rolling Hash dentro del backend sería otra aritmética que
        mantener.

        La estructura, en orden:

            i = 0
            resultado = -1            (0 para CONTAINS)
            mientras i + n <= m:
                j = 0
                mientras j < n y texto[i+j] == aguja[j]: j++
                si j == n: resultado = i; salir
                i = 1 + i

        Los cuatro temporales son `scratch_bytes` (i), `scratch_addr` (j),
        `scratch_loop` (la longitud de la aguja) y `scratch_quedan` (el
        resultado).
        """
        target, text, needle = instr.args[0], instr.args[1], instr.args[2]
        base, aguja = c.local_of[text.id], c.local_of[needle.id]
        hay_len = base + 1

        # El temporal guarda la **posición** del match, y -1 si no aparece.
        # Guardar el resultado final aquí mezclaba las dos cosas: un match en la
        # posición 0 era indistinguible de "no encontrado", y `CONTAINS` salía
        # con la posición en vez de con un booleano.
        c.pieces.append(w.i32_const(-1))
        c.pieces.append(w.local_set(c.scratch_quedan))       # posición
        c.push_local(aguja + 1, "COLOR")
        c.pieces.append(w.local_set(c.scratch_loop))         # n
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(c.scratch_bytes))        # i

        c.pieces.append(w.block())                           # A
        c.pieces.append(w.loop())                            # B
        # Sale si `i` se sale del texto, o si lo que queda es **más corto** que
        # la aguja. Es `<` y no `<=`: con `<=` el texto entero se saltaba la
        # posición del final, y `INDEXOF("mundo")` en "hola mundo" daba -1
        # porque la aguja acaba justo donde el texto acaba.
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.local_get(hay_len))
        c.pieces.append(w.op_unary(w.I32_GE_U))
        c.pieces.append(w.br_if(1))
        c.pieces.append(w.local_get(hay_len))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.op_unary(w.I32_SUB))
        c.pieces.append(w.local_get(c.scratch_loop))
        c.pieces.append(w.op_unary(w.I32_LT_U))
        c.pieces.append(w.br_if(1))
        # Compara la aguja entera desde `i`.
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(c.scratch_addr))         # j = 0
        c.pieces.append(w.block())                           # C
        c.pieces.append(w.loop())                            # D
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.local_get(c.scratch_loop))
        c.pieces.append(w.op_unary(w.I32_GE_U))
        c.pieces.append(w.br_if(1))
        c.pieces.append(w.local_get(base))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))
        c.pieces.append(w.local_get(aguja))
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))
        c.pieces.append(w.op_unary(w.I32_NE))
        c.pieces.append(w.br_if(1))
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_set(c.scratch_addr))
        c.pieces.append(w.br(0))
        c.pieces.append(w.end())                             # D
        c.pieces.append(w.end())                             # C
        # `j >= n` significa que todos los bytes comparados coincidían.
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.local_get(c.scratch_loop))
        c.pieces.append(w.op_unary(w.I32_GE_U))
        c.pieces.append(w.if_())
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.local_set(c.scratch_quedan))
        # En este punto C y D ya están cerrados, así que desde dentro del `if` lo
        # abierto es: if(0), B(1), A(2). Contar también los bucles internos
        # --que ya se cerraron-- salta a una construcción inexistente.
        c.pieces.append(w.br(2))
        c.pieces.append(w.end())
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.pieces.append(w.br(0))
        c.pieces.append(w.end())                             # B
        c.pieces.append(w.end())                             # A

        c.pieces.append(w.local_get(c.scratch_quedan))
        if primer_match:
            # `INDEXOF` devuelve la posición, pero en 0 significa "no
            # encontrado" y BASIC usa 1 para "sí". La convención del IR es
            # -1 para no encontrado y 0 es un match legítimo, así que el 0 se
            # convierte en 1.
            c.pieces.append(w.i32_const(0))
            c.pieces.append(w.op_unary(w.I32_EQ))
            c.pieces.append(w.if_(w.I32))
            c.pieces.append(w.i32_const(1))
            c.pieces.append(w.else_())
            c.pieces.append(w.local_get(c.scratch_quedan))
            c.pieces.append(w.end())
        else:
            # `CONTAINS` sólo quiere saber si apareció.
            c.pieces.append(w.local_get(c.scratch_quedan))
            c.pieces.append(w.i32_const(0))
            c.pieces.append(w.op_unary(w.I32_LT_S))
            c.pieces.append(w.if_(w.I32))
            c.pieces.append(w.i32_const(0))
            c.pieces.append(w.else_())
            c.pieces.append(w.i32_const(1))
            c.pieces.append(w.end())
        if valtype(target.type) == w.I64:
            c.pieces.append(w.op_unary(w.I64_EXTEND_I32_S))
        c.store(target)
    table["str.contains"] = lambda c, i: buscar(c, i, False)
    table["str.indexof"] = lambda c, i: buscar(c, i, True)

    return table


# (tipo de origen, tipo de destino) -> opcode. El MVP no tiene conversión
# directa entre todos los pares: los que faltan no se emiten, y el error lo
# nombra en vez de elegir el opcode más parecido.
_CONVERSION = {
    ("INTEGER", "SINGLE"): w.F32_CONVERT_I64_S,
    ("INTEGER", "DOUBLE"): w.F64_CONVERT_I64_S,
    ("BOOLEAN", "INTEGER"): w.I64_EXTEND_I32_S,
    ("COLOR", "INTEGER"): w.I64_EXTEND_I32_S,
    ("SINGLE", "DOUBLE"): w.F64_PROMOTE_F32,
    ("SINGLE", "INTEGER"): w.I64_TRUNC_F32_S,
    ("DOUBLE", "SINGLE"): w.F32_DEMOTE_F64,
    ("DOUBLE", "INTEGER"): w.I64_TRUNC_F64_S,
}


def wasm_type_of_kind(kind: str) -> int:
    return valtype(kind)


# Cómo se lee y se escribe un elemento de cada tipo en memoria lineal. El
# `align` es log2(bytes), que es lo que pide el memarg de WASM.
_LOAD_OF_KIND = {
    "INTEGER": (w.I64_LOAD, 3), "DOUBLE": (w.F64_LOAD, 3),
    "SINGLE": (w.F32_LOAD, 2), "BOOLEAN": (w.I32_LOAD, 2),
    "COLOR": (w.I32_LOAD, 2), "PTR": (w.I32_LOAD, 2),
}
_STORE_OF_KIND = {
    "INTEGER": (w.I64_STORE, 3), "DOUBLE": (w.F64_STORE, 3),
    "SINGLE": (w.F32_STORE, 2), "BOOLEAN": (w.I32_STORE, 2),
    "COLOR": (w.I32_STORE, 2), "PTR": (w.I32_STORE, 2),
}


# ============================================================ módulo entero


class WasmBackend:
    """Compila un `sir.Module` completo a bytes WASM."""

    def __init__(self, module: sir.Module):
        self.module = module
        self.builder = w.ModuleBuilder(module.name)
        self.string_offsets: dict[int, int] = {}
        self.host_indices: dict[str, int] = {}
        self.func_indices: dict[int, int] = {}
        self.heap_base = DATA_BASE
        self.bump_index: int | None = None
        self.print_array_index: int | None = None
        # Imports que usa una función interna del módulo y no ninguna
        # instrucción del IR. Van aparte de `module.host_imports` para no
        # mutar el módulo, que es del llamante.
        self.extra_imports: set[str] = set()
        self.pies_base = 0
        self._pies_index: int | None = None
        # Un array impreso necesita sus caracteres --corchetes, coma y comilla--.
        # Va una sola cadena y no cuatro constantes: cada una sería su propio
        # segmento de datos, y son cinco bytes.
        if any(instr.op == "host.print_arr"
               for func in module.funcs for instr in func.instrs):
            self.extra_imports |= {"source.print_str", "source.print_bool",
                                   "source.str_len", "source.int2str",
                                   "source.num2str"}
            self._pies_index = module.add_string(SIGUIES_ARRAY)
        # Ancho en locales de cada parámetro, por cuerpo. El verificador lo
        # necesita porque la firma no lo dice: `i32` puede ser un entero o media
        # cadena.
        self._param_words: list = []
        self.layout_strings()
        if self._pies_index is not None:
            self.pies_base = self.string_offset(self._pies_index)
        self.declare_imports()
        self.declare_bump()
        if self._pies_index is not None:
            self._declare_print_array()

    # --------------------------------------------------------------- memoria

    def layout_strings(self) -> None:
        cursor = DATA_BASE
        for index, raw in enumerate(self.module.strings):
            self.string_offsets[index] = cursor
            cursor += len(raw)
        self.heap_base = _align(cursor, HEAP_ALIGN)

    def string_offset(self, index: int) -> int:
        return self.string_offsets[index]

    # --------------------------------------------------------------- imports

    def declare_imports(self) -> None:
        """Importa en orden alfabético, no en el orden de aparición.

        El orden de los imports fija los índices de función, y esos índices
        aparecen en el código. Importar en orden de aparición haría que un
        programa distinto del suyo produjera el mismo binario, y rompería la
        caché por contenido.
        """
        for name in sorted(set(self.module.host_imports) | self.extra_imports):
            signature = HOST_SIGNATURES.get(name)
            if signature is None:
                raise CompileError(Diagnostic(
                    "E0060",
                    f"el host expone '{name}' y no hay firma declarada",
                    Span(0, 0),
                    "añádela a HOST_SIGNATURES en backend.py: el glue la "
                    "implementa y los dos lados tienen que coincidir",
                ))
            params, results = signature
            self.host_indices[name] = self.builder.import_function(
                "source", name.removeprefix("source."), params, results)

    def host_signature(self, name: str) -> tuple:
        return HOST_SIGNATURES[name]

    def host_index(self, name: str) -> int:
        try:
            return self.host_indices[name]
        except KeyError:
            raise CompileError(Diagnostic(
                "E0060", f"falta el import '{name}'", Span(0, 0),
                "el módulo declara un uso del host que no llegó a los imports; "
                "falta `uses_host` en el SIR",
            )) from None

    def func_index(self, sir_index: int) -> int:
        return self.func_indices[sir_index]

    # -------------------------------------------------------------- funciones

    def declare_bump(self) -> None:
        """El allocator del módulo, como función propia.

        Un bump allocator son un global y unas veinte instrucciones. Ponerlo en
        el glue costaría bytes en cada programa con arrays y metería lógica del
        runtime en JavaScript; en la Fase 4 lo sustituye el allocator escrito
        en Source (ADR 0015).

        Se declara **antes** que las funciones del programa porque su índice
        tiene que existir cuando `array.alloc` lo referencia. Los índices de
        función no son el orden del IR, sino el orden de declaración más los
        imports, y confundirlos es el error más habitual al escribir un backend.
        """
        # Cualquier operación que reserve memoria necesita el allocator. Antes
        # sólo miraba `array.alloc`, y un programa que concatenaba textos sin
        # arrays se quedaba con `bump_index = None` y un `AttributeError` en
        # vez de un módulo.
        if not any(instr.op in OPS_QUE_RESERVAN
                   for func in self.module.funcs for instr in func.instrs):
            return

        self.builder.add_global(w.I32, True, w.i32_const(self.heap_base))
        self.builder.add_memory(1)
        body = self.builder.add_function("__bump", params=(w.I32,),
                                         results=(w.I32,), locals=[(2, w.I32)])
        emit = body.emit
        emit(w.global_get(0))                     # local 1 = ptr = heap
        emit(w.local_set(1))
        # heap += align(bytes, 8)
        emit(w.global_get(0))
        emit(w.local_get(0))
        emit(w.i32_const(HEAP_ALIGN - 1))
        emit(w.op_unary(w.I32_ADD))
        emit(w.i32_const(-HEAP_ALIGN))
        emit(w.op_unary(w.I32_AND))
        emit(w.op_unary(w.I32_ADD))
        emit(w.global_set(0))
        # 2 = páginas necesarias = (heap + 65535) >> 16
        emit(w.global_get(0))
        emit(w.i32_const(w.PAGE_SIZE - 1))
        emit(w.op_unary(w.I32_ADD))
        emit(w.i32_const(16))
        emit(w.op_unary(w.I32_SHR_U))
        emit(w.local_set(2))
        # Crecer de una página en una mientras no quepa.
        #
        # No se puede escribir `memory.grow(lo que falte)`: el número de páginas
        # a crecer es un **inmediato literal**, y lo que falta depende del
        # tamaño del array, que es un valor de ejecución. Un solo `memory.grow`
        # con un delta fijo reservaría de más en cada programa con arrays, y uno
        # calculado en runtime no existe. Por eso el bucle.
        # El `block` exterior es lo que permite salir: dentro de un `loop`, un
        # `br_if` a la profundidad 0 salta a la **cabecera**, que es seguir
        # dando vueltas. Hay que saltar al `block`, que está en la profundidad 1.
        emit(w.block())
        emit(w.loop())
        emit(w.local_get(2))
        emit(w.memory_size())
        emit(w.op_unary(w.I32_LE_U))
        emit(w.br_if(1))
        emit(w.memory_size())
        emit(w.memory_grow(1))
        emit(w.drop())
        emit(w.br(0))
        emit(w.end())
        emit(w.end())
        emit(w.local_get(1))                      # return ptr
        self._param_words.append([1])              # un solo parámetro i32
        self.bump_index = self.builder.func_index(body)

    def _declare_print_array(self) -> None:
        """`@__printArray`: imprime un array con la notación del lenguaje.

        Vive en el módulo y no en el glue por dos razones. La primera es el
        peso: el glue se descarga en cada página, y un formateador de arrays son
        unos 450 B brotli que paga todo el mundo por un caso que casi ningún
        programa tiene. La segunda es de sitio: es semántica del lenguaje, y el
        runtime se reescribe en Source en la Fase 4 --un formateador en
        JavaScript es justo el tipo de cosa que luego hay que traducir.

        El módulo imprime elemento a elemento con las mismas primitivas de
        `PRINT`, y lo único que sigue haciendo el host es formatear un número,
        que ya hacía.
        """
        importes = {name: self.host_index(name) for name in self.extra_imports}

        # `@__printArray` se declara **antes** que `@__printElem` porque el
        # elemento lo llama --un array dentro de un array-- y el índice de una
        # función es el de su declaración. El cuerpo se emite después; al
        # builder le da igual, lo que importa es el orden de declaración.
        bucle = self.builder.add_function(
            "__printArray", params=(w.I32, w.I32, w.I32, w.I32), results=(),
            locals=[(2, w.I32)])               # 4 = i, 5 = at
        self._param_words.append([0, 0, 0, 0])
        self.print_array_index = self.builder.func_index(bucle)

        def carga(codigo, alineado, desplazamiento=0):
            return [w.local_get(0), w.memarg(codigo, alineado, desplazamiento)]

        def letra(cual, longitud=1):
            """`print_str` del carácter guardado en la cadena de datos."""
            return [w.i32_const(self.pies_base + cual),
                    w.i32_const(longitud),
                    w.call(importes["source.print_str"])]

        # ---- `@__printElem(at, elem) -> i32`: imprime uno y devuelve su tamaño.
        cuerpo = self.builder.add_function(
            "__printElem", params=(w.I32, w.I32), results=(w.I32,),
            locals=[(1, w.I32)])               # local 2 = buffer del heap
        emit = cuerpo.emit
        BUFFER = 2

        def salir(tamano):
            emit(w.i32_const(tamano))
            emit(w.op_unary(w.RETURN))

        def numero(importe, valor, sin_signo=False):
            """Formatea en un buffer del heap e imprime el resultado.

            Es `STR$` escribiendo por `print_str` en vez de por el valor de la
            expresión. El buffer se pide al allocator del módulo, que para esto
            ya está declarado.
            """
            emit(w.i32_const(FORMAT_BUFFER))
            emit(w.call(self.bump_index))
            emit(w.local_set(BUFFER))
            emit(w.local_get(BUFFER))
            for trozo in valor:
                emit(trozo)
            if sin_signo:
                emit(w.op_unary(w.I64_EXTEND_I32_U))
            if importe == "source.num2str":
                emit(w.i32_const(0))           # modo `PRINT`
            emit(w.call(importes[importe]))
            emit(w.local_get(BUFFER))
            emit(w.call(importes["source.str_len"]))
            emit(w.call(importes["source.print_str"]))

        # El orden va del tipo más frecuente al menos: `elem` es un i32 y cada
        # comparación son tres bytes.
        for code, tamano in ((2, 8), (5, 8), (4, 8), (3, 4), (1, 4), (0, 4)):
            emit(w.local_get(1))
            emit(w.i32_const(code))
            emit(w.op_unary(w.I32_EQ))
            emit(w.if_())
            if code == 5:
                # Un texto dentro va entre comillas dobles, como se escribe en
                # el fuente, para que lo que sale se pueda volver a leer.
                for trozo in letra(COMILLA):
                    emit(trozo)
                for trozo in carga(w.I32_LOAD, 2, 0) + carga(w.I32_LOAD, 2, 4):
                    emit(trozo)
                emit(w.call(importes["source.print_str"]))
                for trozo in letra(COMILLA):
                    emit(trozo)
            elif code == 0:
                for trozo in carga(w.I32_LOAD, 2, 0):
                    emit(trozo)
                emit(w.call(importes["source.print_bool"]))
            elif code == 3:
                numero("source.num2str",
                       carga(w.F32_LOAD, 2, 0) + [w.op_unary(w.F64_PROMOTE_F32)])
            elif code == 4:
                numero("source.num2str", carga(w.F64_LOAD, 3, 0))
            else:
                numero("source.int2str",
                       carga(w.I32_LOAD, 2, 0) if code == 1
                       else carga(w.I64_LOAD, 3, 0),
                       sin_signo=code == 1)
            salir(tamano)
            emit(w.end())
        # ARRAY es el último porque es el único que se llama a sí mismo.
        emit(w.local_get(1))
        emit(w.i32_const(6))
        emit(w.op_unary(w.I32_EQ))
        emit(w.if_())
        # Sin corchetes aquí: `@__printArray` ya cierra el suyo, y ponerlos
        # los dos metía un nivel de más --`[[[1, 2]], [[3]]]`.
        for desplazamiento in (0, 4, 8, 12):
            for trozo in carga(w.I32_LOAD, 2, desplazamiento):
                emit(trozo)
        emit(w.call(self.print_array_index))
        salir(16)
        emit(w.end())
        # Un tipo desconocido se imprimiría como un entero de ocho bytes. No
        # hay forma honesta de formatearlo, y no puede pasar: el código sale
        # del descriptor, que lo escribe este mismo módulo.
        numero("source.int2str", carga(w.I64_LOAD, 3, 0))
        salir(8)
        self._param_words.append([0, 0])
        elem_index = self.builder.func_index(cuerpo)

        # ---- `@__printArray(ptr, len, cap, elem)`: el bucle.
        emit = bucle.emit
        for trozo in letra(LLAVETA):
            emit(trozo)
        emit(w.i32_const(0))
        emit(w.local_set(4))                   # i = 0
        emit(w.local_get(0))
        emit(w.local_set(5))                   # at = ptr
        emit(w.block())
        emit(w.loop())
        emit(w.local_get(4))
        emit(w.local_get(1))
        emit(w.op_unary(w.I32_GE_U))
        emit(w.br_if(1))
        # La coma va antes del elemento, menos en el primero.
        emit(w.local_get(4))
        emit(w.if_())
        for trozo in letra(COMA, 2):
            emit(trozo)
        emit(w.end())
        emit(w.local_get(5))
        emit(w.local_get(3))
        # El tamaño que devuelve `@__printElem` es lo que avanza `at`: cada
        # tipo mide lo suyo --ocho bytes un texto, dieciséis un array-- y el
        # bucle no multiplica por nada.
        emit(w.call(elem_index))
        emit(w.local_get(5))
        emit(w.op_unary(w.I32_ADD))
        emit(w.local_set(5))
        emit(w.local_get(4))
        emit(w.i32_const(1))
        emit(w.op_unary(w.I32_ADD))
        emit(w.local_set(4))
        emit(w.br(0))
        emit(w.end())
        emit(w.end())
        for trozo in letra(CIERRE):
            emit(trozo)

    def compile(self) -> None:
        plan, jumps = control.build_plan(self.func.instrs)
        self._emit_node(plan, jumps, len(self.func.instrs))

    @staticmethod
    def _extent(node: control.Node, total: int) -> tuple[int, int]:
        """El rango de posiciones que cubre un nodo.

        La raíz no tiene rango cerrado en el plan --abarca el cuerpo entero-- y
        por eso se le da `[0, total)`.
        """
        if node.kind == "func":
            return (0, total)
        return (node.start, node.end)

    def _emit_node(self, node: control.Node, jumps, total: int) -> None:
        """Abre las construcciones y emite su cuerpo **en orden de posición**.

        El orden no es un detalle. Las instrucciones propias de un nodo están
        entre las de sus hijos, así que emitirlas todas primero --o todas
        después-- produce un módulo que valida y ejecuta las partes del
        programa como si estuvieran en otro sitio, sin ningún error.

        Con el orden equivocado, `PRINT numeros[0]` después de un bucle salía
        antes que el bucle: el módulo imprimía la primera línea del bucle y
        terminaba. Validaba, ejecutaba y no daba error, que es la peor forma de
        romperse.
        """
        inicio, fin = self._extent(node, total)
        cursor = inicio
        for child in node.children:
            for position in range(cursor, child.start):
                self._emit_one(position, jumps)
            cursor = child.end
            if child.kind == control.LOOP:
                self.pieces.append(w.loop())
            elif child.kind == control.BLOCK:
                self.pieces.append(w.block())
            self._emit_node(child, jumps, total)
            if child.kind in (control.LOOP, control.BLOCK):
                self.pieces.append(w.end())
        for position in range(cursor, fin):
            self._emit_one(position, jumps)

    def _emit_one(self, position: int, jumps) -> None:
        instr = self.func.instrs[position]
        if instr.op == "br_if.false":
            # El IR salta cuando la condición es falsa; WASM salta cuando es
            # distinta de cero. Hay que invertir, y un BOOLEAN es i32, así que
            # `i32.eqz` da exactamente lo que hace falta.
            self.push(instr.args[0])
            self.pieces.append(w.op_unary(w.I32_EQZ))
            self.pieces.append(w.br_if(jumps[position].depth))
            return
        if instr.op == "br":
            self.pieces.append(w.br(jumps[position].depth))
            return
        if instr.op == "label":
            return
        emitter = self.emitters.get(instr.op)
        if emitter is None:
            raise unsupported(instr.op, instr.span)
        emitter(self, instr)


# ============================================================ tabla de emisión


# Nativas que el backend emite con sus propias instrucciones. Se rellena al
# construir la tabla porque los emisores se definen después.
INLINE_NATIVES: dict = {}


def _build_emitters() -> dict:
    """El único sitio donde vive el lowering de cada operación."""
    table: dict = {}

    # ---- literales
    def const_of_type(c, instr):
        """`const.i64` y `const.f64` sólo se diferencian en quién fija el tipo."""
        c.pieces.append(c.constant(instr.args[0].type, instr.args[1]))
        c.store(instr.args[0])
    table["const.i64"] = const_of_type
    table["const.f64"] = const_of_type

    def const_bool(c, instr):
        c.pieces.append(w.i32_const(1 if instr.args[1] else 0))
        c.store(instr.args[0])
    table["const.bool"] = const_bool

    def const_strref(c, instr):
        offset = c.backend.string_offset(instr.args[1])
        c.pieces.append(c.constant(instr.args[0].type, offset))
        c.store(instr.args[0])
    table["const.strref"] = const_strref

    def const_str(c, instr):
        """(ptr, len): `const.strref` ya dejó el puntero en un local."""
        ptr = instr.args[1]
        length = instr.args[2]
        # La palabra del puntero es i32; si el `strref` venía tipado i64 hay
        # que estrecharlo, porque un local i64 no es un local i32 aunque valgan
        # lo mismo.
        c.push_local(c.local_of[ptr.id], instr.args[0].type.kind)
        c.pieces.append(w.local_set(c.local_of[instr.args[0].id]))
        c.pieces.append(w.i32_const(length))
        c.pieces.append(w.local_set(c.local_of[instr.args[0].id] + 1))
    table["const.str"] = const_str

    def const_zero(c, instr):
        target = instr.args[0]
        kind = target.type.kind
        if kind == "SINGLE":
            c.pieces.append(w.f32_const(0.0))
        elif kind == "DOUBLE":
            c.pieces.append(w.f64_const(0.0))
        elif valtype(target.type) == w.I32:
            c.pieces.append(w.i32_const(0))
        else:
            c.pieces.append(w.i64_const(0))
        c.store(target)
    table["const.zero"] = const_zero

    # ---- slots: ya son locales, así que un acceso es una copia
    def store_slot(c, instr):
        slot, value = instr.args[0], instr.args[1]
        base = c.slot_local[slot]
        if isinstance(value, sir.Value):
            source = c.local_of[value.id]
            for offset in range(words_of(value.type)):
                c.pieces.append(w.local_get(source + offset))
                c.pieces.append(w.local_set(base + offset))
        else:
            c.push(value)
            c.pieces.append(w.local_set(base))
    table["store.slot"] = store_slot

    def load_slot(c, instr):
        target, slot = instr.args[0], instr.args[1]
        base = c.slot_local[slot]
        for offset in range(words_of(target.type)):
            c.pieces.append(w.local_get(base + offset))
        c.store(target)
    table["load.slot"] = load_slot

    # ---- llamadas
    def call(c, instr):
        target, operands = instr.args[0], instr.args[1]
        returns = instr.type is not None and instr.type.kind != "VOID"
        destination = operands[0] if returns else None
        arguments = operands[1:] if returns else operands
        for argument in arguments:
            c.push(argument)
        c.pieces.append(w.call(c.backend.func_index(target)))
        if destination is not None:
            c.store(destination)
    table["call"] = call

    def call_native(c, instr):
        name, operands = instr.args[0], instr.args[1]
        # `NATIVE_IMPORT` con valor None significa "se emite aquí", no "no se
        # sabe": confundirlas hacía fallar `RGB` como si fuera una nativa
        # desconocida.
        if name in INLINE_NATIVES:
            INLINE_NATIVES[name](c, instr)
            return
        import_name = NATIVE_IMPORT.get(name)
        if import_name is None:
            raise unsupported(f"call.native '{name}'", instr.span)
        returns = bool(HOST_SIGNATURES[import_name][1])
        destination = operands[0] if returns else None
        arguments = operands[1:] if returns else operands
        for argument in arguments:
            c.push(argument)
        c.pieces.append(w.call(c.backend.host_index(import_name)))
        if destination is not None:
            c.store(destination)
    table["call.native"] = call_native

    def ret(c, instr):
        # Una FUNCTION sin valor devuelve sin operandos: leer args[0] a ciegas
        # daba IndexError en la primera función VOID del corpus.
        if instr.args and isinstance(instr.args[0], sir.Value):
            c.push(instr.args[0])
        c.pieces.append(w.op(0x0F))     # RETURN
    table["ret"] = ret

    # ---- arrays
    #
    # Un array del IR son tres palabras: (ptr, len, cap). Los elementos van
    # seguidos en memoria lineal desde `ptr`, con el tamaño que dicta el tipo
    # del elemento. El crecimiento es del bump allocator y no reubica: por eso
    # `cap` y `len` empiezan iguales y el compilador tiene que prohibido
    # crecer un array en la Fase 2.

    def array_alloc(c, instr):
        target, code, count = instr.args
        size = layout_by_code(code).size
        # bytes = count * size, y `@__bump` devuelve el puntero ya en la pila.
        # El tamaño se multiplica en i64 y se estrecha después: al revés, un
        # array de más de 2^31 elementos daría una reserva de un byte.
        c.push(count)
        c.pieces.append(c.constant("INTEGER", size))
        c.pieces.append(w.op_unary(w.I64_MUL))
        c.pieces.append(w.op_unary(w.I32_WRAP_I64))
        c.pieces.append(w.call(c.backend.bump_index))
        c.pieces.append(w.local_set(c.local_of[target.id]))
        # len y cap empiezan iguales: el bump allocator no reubica nunca.
        for offset in (1, 2):
            c.push(count)
            c.pieces.append(w.op_unary(w.I32_WRAP_I64))
            c.pieces.append(w.local_set(c.local_of[target.id] + offset))
        # La cuarta palabra del descriptor es el tipo del elemento. La lee el
        # host al imprimir un array de arrays, y no se puede deducir de los
        # datos: un array de ceros es indistinguible de uno de enteros.
        c.pieces.append(w.i32_const(code))
        c.pieces.append(w.local_set(c.local_of[target.id] + 3))
    table["array.alloc"] = array_alloc

    def array_len(c, instr):
        target, array = instr.args[0], instr.args[1]
        # La longitud es la segunda palabra del array y es i32; `LEN` devuelve
        # INTEGER, que es i64. Sin ensanchar, el módulo no valida.
        c.push_local(c.local_of[array.id] + 1, "INTEGER")
        c.store(target)
    table["array.len"] = array_len

    def load_index(c, instr):
        target, array, index = instr.args
        addr = c.index_address(array, index, c.element_size(target.type))
        c.check_bounds(c.local_of[array.id], c.scratch_bytes)
        c.load_at(addr, target.type)
        c.store(target)
    table["load.index"] = load_index

    def store_index(c, instr):
        array, index, value = instr.args
        addr = c.index_address(array, index, c.element_size(value.type))
        c.check_bounds(c.local_of[array.id], c.scratch_bytes)
        c.store_at(addr, value)
    table["store.index"] = store_index

    def array_init(c, instr):
        array, offset, item = instr.args
        # El desplazamiento es un inmediato, pero el camino es el mismo que el
        # de un índice variable: comprobarlo cuesta lo mismo y evita que un
        # literal se salga sin que nadie lo note.
        addr = c.index_address(array, offset, c.element_size(item.type))
        c.check_bounds(c.local_of[array.id], c.scratch_bytes)
        c.store_at(addr, item)
    table["array.init"] = array_init



    # ---- host
    def host_op(c, instr):
        import_name = HOST_OPS[instr.op]
        # `flip` no lleva operandos: no hay nada que empujar. Leerlos siempre
        # daba `IndexError` con una operación que el IR sí emite bien.
        if instr.args:
            target = instr.args[0]
            c.push(target)
            # El operando se convierte al tipo que pide la firma, no al que
            # resulte de su tipo en el IR. `PRINT` de un COLOR emite
            # `print_int`, que toma i64, y el COLOR son cuatro bytes: sin
            # ensanchar, el módulo no valida.
            #
            # Un texto son dos palabras y su firma son dos i32: encaja. Un
            # SINGLE es f32 y `print_num` pide f64: `f64.promote_f32`.
            wanting = c.backend.host_signature(import_name)[0]
            tiene = valtype(target.type)
            if len(wanting) == 1 and words_of(target.type) == 1:
                if tiene == w.I32 and wanting[0] == w.I64:
                    # Un COLOR es un patrón de cuatro bytes, no un número con
                    # signo: `RGB(255,200,0)` es 4294952960, y ensanchar con
                    # signo daría -14336. El intérprete lo trata como sin signo
                    # y el módulo tiene que hacer lo mismo.
                    cero_o_signo = (w.I64_EXTEND_I32_U
                                    if target.type.kind == "COLOR"
                                    else w.I64_EXTEND_I32_S)
                    c.pieces.append(w.op_unary(cero_o_signo))
                elif tiene == w.I64 and wanting[0] == w.I32:
                    c.pieces.append(w.op_unary(w.I32_WRAP_I64))
                elif tiene == w.F32 and wanting[0] == w.F64:
                    c.pieces.append(w.op_unary(w.F64_PROMOTE_F32))
        c.pieces.append(w.call(c.backend.host_index(import_name)))
    for op_name in HOST_OPS:
        table[op_name] = host_op

    def print_arr(c, instr):
        """`PRINT` de un array: una llamada a `@__printArray`.

        Las cuatro palabras del descriptor --puntero, longitud, capacidad y
        tipo del elemento-- son los cuatro argumentos, y la función es del
        módulo, no un import: el formateo es semántica del lenguaje.
        """
        c.push(instr.args[0])
        c.pieces.append(w.call(c.backend.print_array_index))
    table["host.print_arr"] = print_arr

    def newline(c, instr):
        c.pieces.append(w.call(c.backend.host_index("source.newline")))
    table["newline"] = newline

    # ---- aritmética entera. WASM usa `div_s`/`rem_s` con truncamiento hacia
    # cero; `div.i64` en BASIC es división entera también, así que coinciden.
    def make_binop(code):
        def binop(c, instr):
            c.push(instr.args[1])
            c.push(instr.args[2])
            c.pieces.append(w.op_unary(code))
            c.store(instr.args[0])
        return binop

    table["add.i64"] = make_binop(w.I64_ADD)
    table["sub.i64"] = make_binop(w.I64_SUB)
    table["mul.i64"] = make_binop(w.I64_MUL)
    table["div.i64"] = make_binop(w.I64_DIV_S)
    table["rem.i64"] = make_binop(w.I64_REM_S)
    table["shl.i64"] = make_binop(w.I64_SHL)
    table["shr.i64"] = make_binop(w.I64_SHR_S)
    table["and.i64"] = make_binop(w.I64_AND)
    table["or.i64"] = make_binop(w.I64_OR)
    table["xor.i64"] = make_binop(w.I64_XOR)
    table["and.bool"] = make_binop(w.I32_AND)
    table["or.bool"] = make_binop(w.I32_OR)
    table["xor.bool"] = make_binop(w.I32_XOR)

    def neg_i64(c, instr):
        c.pieces.append(w.i64_const(0))
        c.push(instr.args[1])
        c.pieces.append(w.op_unary(w.I64_SUB))
        c.store(instr.args[0])
    table["neg.i64"] = neg_i64

    # ---- comparación
    def make_compare(code):
        def compare(c, instr):
            c.push(instr.args[1])
            c.push(instr.args[2])
            c.pieces.append(w.op_unary(code))
            c.store(instr.args[0])
        return compare

    table["lt.i64"] = make_compare(w.I64_LT_S)
    table["le.i64"] = make_compare(w.I64_LE_S)
    table["gt.i64"] = make_compare(w.I64_GT_S)
    table["ge.i64"] = make_compare(w.I64_GE_S)
    table["not.bool"] = lambda c, i: (
        c.push(i.args[1]), c.pieces.append(w.op_unary(w.I32_EQZ)),
        c.store(i.args[0]))

    def eq(c, instr):
        # `eq` llega con el tipo en la propia instrucción, porque compara
        # enteros, booleanos y textos con la misma palabra.
        left = instr.args[1]
        kind = (left.type.kind if isinstance(left, sir.Value)
                else instr.type.kind)
        # Sin este `raise`, `eq` sobre dos `f64` caía en `i64.eq` **por el
        # `.get(..., I64_EQ)` de abajo** y comparaba los bits de los dos
        # flotantes como enteros. Da la respuesta correcta casi siempre --mismos
        # bits, mismo número-- y la incorrecta justo donde importa: `0.0 = -0.0`
        # son bits distintos, así que "no son iguales", cuando IEEE dice que sí.
        # Un `.get` con valor por defecto es una afirmación sobre lo que se sabe,
        # y aquí lo que se sabía era falso.
        if kind in ("SINGLE", "DOUBLE"):
            raise unsupported(f"eq sin sufijo sobre {kind}", instr.span)
        code = {w.I64: w.I64_EQ, w.I32: w.I32_EQ}.get(
            wasm_type_of_kind(kind), w.I64_EQ)
        c.push(instr.args[1])
        c.push(instr.args[2])
        c.pieces.append(w.op_unary(code))
        c.store(instr.args[0])
    table["eq"] = eq

    # Igualdad de flotantes. No se puede reutilizar `eq` porque el opcode depende
    # del **operando**, y `eq` sin sufijo no lo mira: mira el destino, que es
    # BOOLEAN y por tanto siempre i32.
    def eq_f64(c, instr):
        c.push(instr.args[1])
        c.push(instr.args[2])
        c.pieces.append(w.op_unary(w.F64_EQ))
        c.store(instr.args[0])
    table["eq.f64"] = eq_f64

    def eq_f32(c, instr):
        c.push(instr.args[1])
        c.push(instr.args[2])
        c.pieces.append(w.op_unary(w.F32_EQ))
        c.store(instr.args[0])
    table["eq.f32"] = eq_f32

    def select(c, instr):
        # El IR es `select destino, condición, si, no` y WASM deja la pila como
        # (si, no, condición). El orden importa y no se parece: empujarlo igual
        # que en el IR hace que `FOR` salte siempre a la salida, porque se está
        # evaluando la condición en el bucle equivocado.
        _, condition, si, no = instr.args
        c.push(si)
        c.push(no)
        c.push(condition)
        c.pieces.append(w.op_unary(w.SELECT))
        c.store(instr.args[0])
    table["select"] = select

    def to_bool(c, instr):
        # "Falsy" es cero o NIL; para un entero, distinto de cero.
        c.push(instr.args[1])
        c.pieces.append(w.i64_const(0))
        c.pieces.append(w.op_unary(w.I64_NE))
        c.store(instr.args[0])
    table["to.bool"] = to_bool

    # ---- conversiones
    #
    # El opcode depende del tipo del **operando**, no del del resultado: pasar
    # un SINGLE a un DOUBLE es `f64.promote_f32` y pasar un INTEGER a un DOUBLE
    # es `f64.convert_i64_s`. Elegir por el resultado daba un módulo que no
    # valida, porque la instrucción espera el operando que el destino no es.

    def conversion(c, instr, destino_kind):
        origen = instr.args[1]
        origen_kind = (origen.type.kind if isinstance(origen, sir.Value)
                       else destino_kind)
        code = _CONVERSION.get((origen_kind, destino_kind))
        if code is None:
            raise unsupported(f"conv de {origen_kind} a {destino_kind}",
                              instr.span)
        c.push(origen)
        c.pieces.append(w.op_unary(code))
        c.store(instr.args[0])
    table["conv.i64"] = lambda c, i: conversion(c, i, "INTEGER")
    table["conv.f32"] = lambda c, i: conversion(c, i, "SINGLE")
    table["conv.f64"] = lambda c, i: conversion(c, i, "DOUBLE")


    # ---- aritmética flotante y matemática
    #
    # Un SINGLE es f32 de verdad y un DOUBLE es f64 de verdad: no son un f32
    # disfrazado dentro de un i64, así que cada uno usa sus instrucciones.
    table["add.f32"] = make_binop(w.F32_ADD)
    table["sub.f32"] = make_binop(w.F32_SUB)
    table["mul.f32"] = make_binop(w.F32_MUL)
    table["div.f32"] = make_binop(w.F32_DIV)
    table["add.f64"] = make_binop(w.F64_ADD)
    table["sub.f64"] = make_binop(w.F64_SUB)
    table["mul.f64"] = make_binop(w.F64_MUL)
    table["div.f64"] = make_binop(w.F64_DIV)
    table["lt.f32"] = make_compare(w.F32_LT)
    table["le.f32"] = make_compare(w.F32_LE)
    table["gt.f32"] = make_compare(w.F32_GT)
    table["ge.f32"] = make_compare(w.F32_GE)
    table["lt.f64"] = make_compare(w.F64_LT)
    table["le.f64"] = make_compare(w.F64_LE)
    table["gt.f64"] = make_compare(w.F64_GT)
    table["ge.f64"] = make_compare(w.F64_GE)

    def neg_f32(c, instr):
        # `f32.neg` es una instrucción propia; restar de cero obligaría a tener
        # un cero f32 en un local.
        c.push(instr.args[1])
        c.pieces.append(w.op_unary(w.F32_NEG))
        c.store(instr.args[0])
    table["neg.f32"] = neg_f32

    def neg_f64(c, instr):
        c.push(instr.args[1])
        c.pieces.append(w.op_unary(w.F64_NEG))
        c.store(instr.args[0])
    table["neg.f64"] = neg_f64

    def sqrt_f64(c, instr):
        c.push(instr.args[1])
        c.pieces.append(w.op_unary(w.F64_SQRT))
        c.store(instr.args[0])
    table["sqrt.f64"] = sqrt_f64

    def floor_f64(c, instr):
        c.push(instr.args[1])
        c.pieces.append(w.op_unary(w.F64_FLOOR))
        c.store(instr.args[0])
    table["floor.f64"] = floor_f64
    table["pow.f64"] = lambda c, i: host_math(c, i, "pow")

    # ---- ABS, MIN, MAX y CLAMP
    #
    # La lengua los declara (`types.py`) y el intérprete los ejecuta, pero el
    # backend no los bajaba: `sourc check` aceptaba el programa y `sourc build`
    # lo rechazaba con E0058. Es la peor clase de bug posible aquí, porque las
    # dos mitades del mismo compilador se contradicen sobre el mismo fuente, y
    # el tripwire --que existe exactamente para esto-- avisaba en vez de dejar
    # pasar en silencio.
    #
    # No se pueden emitir con una instrucción porque **WASM no tiene `i64.abs`,
    # ni `i64.min`, ni `i64.max`**. De los tres sólo existen las versiones de
    # flotante (`f64.abs` = 0x99, `f64.min` = 0xA4, `f64.max` = 0xA5); en el MVP
    # los enteros se quedan sin instrucción. Para sintetizarlos sale más barato
    # `select` que la fórmula de bits con `xor` y desplazamiento: cinco
    # instrucciones y un comentario, frente a ocho y un acertijo.
    _COMPARAR = {"INTEGER": (w.I64_LT_S, w.I64_GT_S),
                 "DOUBLE": (w.F64_LT, w.F64_GT),
                 "SINGLE": (w.F32_LT, w.F32_GT)}

    def _tipo_de(valor):
        return valor.type.kind if isinstance(valor, sir.Value) else "DOUBLE"

    def _elige(c, cuando_si, cuando_no, izquierda, derecha, code):
        """Deja en la pila `izquierda <code> derecha ? cuando_si : cuando_no`.

        `select` saca tres cosas de la pila y devuelve la primera si la tercera
        no es cero, así que la condición va **encima**: es lo último que se
        evalúa, y por eso los dos valores se emiten antes que ella.

        No guarda en ningún destino a propósito. `clamp` necesita el mismo
        `select` dos veces en la pila y no puede declarar un local: los locales
        son parte de la firma de la función, que se decide antes de bajar el
        cuerpo. Quien llama guarda.
        """
        c.push(cuando_si)
        c.push(cuando_no)
        c.push(izquierda)
        c.push(derecha)
        c.pieces.append(w.op_unary(code))
        c.pieces.append(w.op_unary(w.SELECT))

    def _minimo(c, first, second, kind):
        lt, _gt = _COMPARAR[kind]
        # El intérprete escribe `min(a, b)` como `b si b < a si no a`, y ese
        # orden **se nota**: `min(1.0, NaN)` da `1.0` mientras que `f64.min`
        # daría NaN. Por eso no se usa `f64.min`, que pesa un byte menos y
        # cambia la semántica.
        #
        # `second` va **primero** en la pila porque es lo que se devuelve cuando
        # la comparación se cumple. Invertirlo da un `select` que siempre elige
        # el otro lado: `MIN(3, 4)` devolvía 4 y `ABS(5)` devolvía -5, que es un
        # módulo que valida, que el verificador acepta y que imprime el número
        # equivocado. Ningún gate lo ve, porque los tres están bien formados.
        _elige(c, second, first, second, first, lt)

    def _maximo(c, first, second, kind):
        _lt, gt = _COMPARAR[kind]
        _elige(c, second, first, second, first, gt)

    def abs(c, instr):
        # `call.native` guarda `(nombre, [operandos])`, no los operandos sueltos:
        # leer `args[0]` como destino entrega la cadena `"abs"`.
        target, value = instr.args[1]
        kind = _tipo_de(value)
        if kind != "INTEGER":
            # En flotante está la instrucción, y además hace falta: con `select`
            # el `-0.0` volvería como `-0.0`, y `f64.abs` lo devuelve `+0.0`.
            c.push(value)
            c.pieces.append(w.op_unary(
                w.F64_ABS if kind == "DOUBLE" else w.F32_ABS))
            c.store(target)
            return
        # `select(0 - x, x, x < 0)`: `0 - x` va primero porque es lo que sale
        # cuando `x` es negativo.
        c.pieces.append(w.i64_const(0))
        c.push(value)
        c.pieces.append(w.op_unary(w.I64_SUB))
        c.push(value)
        c.push(value)
        c.pieces.append(w.i64_const(0))
        c.pieces.append(w.op_unary(w.I64_LT_S))
        c.pieces.append(w.op_unary(w.SELECT))
        c.store(target)

    def minimo(c, instr):
        target, first, second = instr.args[1]
        _minimo(c, first, second, _tipo_de(second))
        c.store(target)

    def maximo(c, instr):
        target, first, second = instr.args[1]
        _maximo(c, first, second, _tipo_de(second))
        c.store(target)

    def clamp(c, instr):
        target, low, value, high = instr.args[1]
        kind = _tipo_de(value)
        # `CLAMP(low, value, high)` es `max(low, min(value, high))`, con el
        # orden exacto del intérprete, y sale como un `max` cuyo primer valor es
        # el `min` interno.
        #
        # El `min` se calcula **dos veces** porque hacen falta dos copias: una
        # para el valor y otra para la comparación. No se puede guardar en
        # ningún lado porque `select` se come sus tres operandos y no deja nada
        # en la pila, y a un emisor no se le puede declarar un local: los
        # locales son parte de la firma de la función, que se decide antes de
        # bajar el cuerpo. Son cinco instrucciones de código de programa, y el
        # código de programa no es lo que se mide.
        _minimo(c, value, high, kind)      # el valor, cuando se cumple
        c.push(low)                        # el valor, cuando no
        _minimo(c, value, high, kind)      # el comparando
        c.push(low)                        # el comparando
        c.pieces.append(w.op_unary(_COMPARAR[kind][1]))
        c.pieces.append(w.op_unary(w.SELECT))
        c.store(target)

    INLINE_NATIVES["abs"] = abs
    INLINE_NATIVES["min"] = minimo
    INLINE_NATIVES["max"] = maximo
    INLINE_NATIVES["clamp"] = clamp

    def divfloor_i64(c, instr):
        target, left, right = instr.args
        # `i64.div_s` trunca hacia cero y `DIV` redondea hacia menos infinito:
        # -7/2 da -3 con div_s y -4 con divfloor. Se corrige restando uno cuando
        # el resto no es cero y los signos no coinciden.
        c.push(left)
        c.push(right)
        c.pieces.append(w.op_unary(w.I64_DIV_S))
        c.store(target)
        c.push(left)
        c.push(right)
        c.pieces.append(w.op_unary(w.I64_REM_S))
        c.pieces.append(w.i64_const(0))
        c.pieces.append(w.op_unary(w.I64_NE))
        c.push(left)
        c.pieces.append(w.i64_const(63))
        c.pieces.append(w.op_unary(w.I64_SHR_S))
        c.push(right)
        c.pieces.append(w.i64_const(63))
        c.pieces.append(w.op_unary(w.I64_SHR_S))
        c.pieces.append(w.op_unary(w.I64_NE))
        # Las dos condiciones son i32 porque las comparaciones lo son, y el
        # `and` tiene que ser el de i32.
        c.pieces.append(w.op_unary(w.I32_AND))
        c.pieces.append(w.if_())
        c.pieces.append(w.local_get(c.local_of[target.id]))
        c.pieces.append(w.i64_const(1))
        c.pieces.append(w.op_unary(w.I64_SUB))
        c.pieces.append(w.local_set(c.local_of[target.id]))
        c.pieces.append(w.end())
    table["divfloor.i64"] = divfloor_i64

    def idiv_i64(c, instr):
        # `\` trunca hacia CERO y `DIV` hacia menos infinito (GRAMMAR.md 3.2), y
        # `i64.div_s` ya trunca hacia cero. Aquí se delegaba en `divfloor_i64`
        # porque el intérprete usaba la misma división para los dos, y el
        # comentario decía que si algún día se separaban, "este es el sitio
        # donde se rompen las dos".
        #
        # Llegaron a separarse. `PRINT -9 \ 2` daba -5 en el módulo y -4 en el
        # intérprete, y el corpus no lo veía porque `03_aritmetica` sólo divide
        # `7 \ 2`, con dividendo positivo, donde las dos truncaciones coinciden.
        c.push(instr.args[1])
        c.push(instr.args[2])
        c.pieces.append(w.op_unary(w.I64_DIV_S))
        c.store(instr.args[0])
    table["idiv.i64"] = idiv_i64

    def host_math(c, instr, name):
        """`sin`, `cos` y `pow` no son instrucciones del MVP: van al host."""
        target = instr.args[0]
        for argument in instr.args[1:]:
            c.push(argument)
        c.pieces.append(w.call(c.backend.host_index(f"source.{name}")))
        c.store(target)
    table["sin.f64"] = lambda c, i: host_math(c, i, "sin")
    table["cos.f64"] = lambda c, i: host_math(c, i, "cos")

    def rgb(c, instr):
        destination, red, green, blue = instr.args[1]
        # Un COLOR son tres bytes --rojo alto, verde, azul-- y empaquetarlos son
        # cuatro instrucciones, así que es inline y no del host. Los tres
        # componentes son INTEGER (i64) y hay que estrechar antes de desplazar;
        # el `or` va **entre** componentes, porque el primero no tiene con qué
        # combinarse todavía.
        # Cuatro bytes, con alfa a 255: es como lo empaqueta el intérprete
        # (`(a << 24) | (r << 16) | (g << 8) | b`) y es lo que espera el canvas.
        c.pieces.append(w.i32_const(255))
        c.pieces.append(w.i32_const(24))
        c.pieces.append(w.op_unary(w.I32_SHL))
        # **Cada** componente se combina con `or`, incluido el primero: el alfa
        # ya está en la pila, así que el rojo también tiene con qué. Omitir ese
        # primer `or` daba 0x00FFC800 en vez de 0xFFFFC800 -- el mismo número en
        # decimal con los dos últimos bytes cambiados, que es como se cuela un
        # error de empaquetado de color.
        for component, shift in ((red, 16), (green, 8), (blue, 0)):
            c.push(component)
            c.pieces.append(w.op_unary(w.I32_WRAP_I64))
            if shift:
                c.pieces.append(w.i32_const(shift))
                c.pieces.append(w.op_unary(w.I32_SHL))
            c.pieces.append(w.op_unary(w.I32_OR))
        c.store(destination)
    INLINE_NATIVES["rgb"] = rgb

    # ---- textos
    def str_len(c, instr):
        target, text = instr.args[0], instr.args[1]
        # La longitud es la segunda palabra del texto, y es i32; `LEN` devuelve
        # un INTEGER de ocho bytes.
        c.push_local(c.local_of[text.id] + 1, "INTEGER")
        c.store(target)
    table["str.len"] = str_len

    def str_byte(c, instr):
        target, text, index = instr.args
        base = c.local_of[text.id]
        c.push(index)
        c.pieces.append(w.op_unary(w.I32_WRAP_I64))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.check_bounds(base, c.scratch_bytes)
        c.pieces.append(w.local_get(base))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))
        # El byte es i32 y `ASC` devuelve un INTEGER: hay que ensanchar.
        c.pieces.append(w.op_unary(w.I64_EXTEND_I32_S))
        c.store(target)
    table["str.byte"] = str_byte

    def str_left(c, instr):
        # `LEFT$(s, n)`: los primeros `n` bytes. El puntero no se mueve, así que
        # no hay que copiar nada: el texto origen no se modifica.
        target, text, count = instr.args
        base = c.local_of[text.id]
        # El puntero no se mueve; la longitud se recorta al texto. Empujar el
        # contador sin recortar y luego el `min` dejaba tres valores en la pila
        # y `store` consumía los dos últimos.
        c.push_local(base, "STRING")
        c.min_i32(c.local_of[count.id], base + 1)
        c.store(target)
    table["str.left"] = str_left

    def str_right(c, instr):
        # `RIGHT$(s, n)`: los últimos `n` bytes. El puntero se avanza hasta el
        # final menos `n` y la longitud se queda; no hay que copiar.
        #
        # Las dos palabras se apilan y las guarda `store` de una vez. Guardar la
        # primera a mano y luego llamar a `store` deja la pila con un valor
        # menos de los que `store` saca, y el módulo no valida.
        target, text, count = instr.args
        base = c.local_of[text.id]
        largo = c.local_of[count.id]
        # `n` se recorta a la longitud del texto: `RIGHT$(s, 99)` son todos los
        # bytes, no 99. Con `n` recortado, el puntero es `base + (len - n)`.
        c.min_i32(largo, base + 1)
        c.pieces.append(w.local_set(c.scratch_bytes))       # n recortado
        n = c.scratch_bytes
        c.push_local(base, "COLOR")
        c.push_local(base + 1, "COLOR")
        c.push_local(n, "COLOR")
        c.pieces.append(w.op_unary(w.I32_SUB))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.push_local(n, "COLOR")
        c.store(target)
    table["str.right"] = str_right

    def str_mid(c, instr):
        # `MID$(s, desde, cuantos)`: una subcadena, que es un puntero al byte
        # `desde` y una longitud. No hay que copiar nada.
        #
        # Las dos palabras se apilan y las guarda `store` de una vez. Guardar la
        # primera a mano y luego llamar a `store` deja la pila con un valor
        # menos de los que `store` saca, y el módulo no valida.
        target, text, start, count = instr.args
        base = c.local_of[text.id]
        # Lo que queda desde `start` es `len - start`, y la longitud pedida se
        # recorta a eso: pedir 5 bytes desde la posición 6 de un texto de 10 da
        # 4, no 5.
        c.push_local(c.local_of[start.id], "COLOR")
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.pieces.append(w.local_get(base + 1))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.op_unary(w.I32_SUB))
        c.pieces.append(w.local_set(c.scratch_addr))       # len - start
        c.push_local(base, "COLOR")
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.op_unary(w.I32_ADD))             # puntero
        c.min_i32(c.local_of[count.id], c.scratch_addr)
        c.store(target)
    table["str.mid"] = str_mid

    def str_sub(c, instr):
        # `MID$(s, i, 1)`: un texto de un byte. Se reserva un byte en el heap y
        # se copia, en vez de apuntar dentro del original: el intérprete cuenta
        # una alocación aquí, y un texto que se solapa con otro haría que
        # escribir en uno cambiara el otro.
        target, text, index = instr.args
        base = c.local_of[text.id]
        c.push(index)
        c.pieces.append(w.op_unary(w.I32_WRAP_I64))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.check_bounds(base, c.scratch_bytes)
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.call(c.backend.bump_index))
        c.pieces.append(w.local_set(c.local_of[target.id]))
        c.pieces.append(w.local_get(c.local_of[target.id]))
        c.pieces.append(w.local_get(base))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))
        c.pieces.append(w.memarg(w.I32_STORE8, 0, 0))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.local_set(c.local_of[target.id] + 1))
    table["str.sub"] = str_sub

    def str_from_code(c, instr):
        # ADR 0021: `CHR(n)`. La forma es la de `str.sub` sin el texto origen:
        # reservar un byte en el heap, escribirlo, y decir que el texto mide uno.
        # Sin la linea de `check_bounds` que tiene `str.sub` porque aqui no hay
        # indice que acotar: el byte es el valor, no una posicion dentro de otro.
        target, code = instr.args
        c.push(code)
        c.pieces.append(w.op_unary(w.I32_WRAP_I64))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.call(c.backend.bump_index))
        c.pieces.append(w.local_set(c.local_of[target.id]))
        c.pieces.append(w.local_get(c.local_of[target.id]))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.memarg(w.I32_STORE8, 0, 0))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.local_set(c.local_of[target.id] + 1))
    table["str.from_code"] = str_from_code

    def str_concat(c, instr):
        target, left, right = instr.args
        left_base, right_base = c.local_of[left.id], c.local_of[right.id]
        # El destino se reserva una vez, con la suma de las dos longitudes.
        # Pedir dos reservas y pegarlas en la memoria contigua sería un
        # `memmove` a mano y no un concatenador.
        c.push_local(left_base + 1, "COLOR")
        c.push_local(right_base + 1, "COLOR")
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.call(c.backend.bump_index))
        c.pieces.append(w.local_set(c.local_of[target.id]))
        c.copy_bytes(c.local_of[target.id], left_base, left_base + 1)
        # El segundo trozo va justo detrás del primero.
        c.pieces.append(w.local_get(c.local_of[target.id]))
        c.pieces.append(w.local_get(left_base + 1))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.copy_bytes(c.scratch_bytes, right_base, right_base + 1)
        c.push_local(c.local_of[target.id], "STRING")
        c.push_local(left_base + 1, "COLOR")
        c.push_local(right_base + 1, "COLOR")
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.store(target)
    table["str.concat"] = str_concat

    def str_eq_byte(c, instr):
        target, text, index, needle = instr.args
        base = c.local_of[text.id]
        needle_base = c.local_of[needle.id]
        # Sólo hace falta `índice < longitud`: el índice viene envuelto a i32, así
        # que uno negativo es enorme sin signo y falla la comparación sola.
        #
        # La versión anterior añadía un segundo predicado, `índice >= 0` **sin
        # signo**, que es `0 <u 0` -- falso. Eso hacía que la columna 0 de
        # cualquier texto no coincidiera nunca, y el laberinto no dibujaba ni una
        # pared.
        c.push(index)
        c.pieces.append(w.op_unary(w.I32_WRAP_I64))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.local_get(base + 1))
        c.pieces.append(w.op_unary(w.I32_LT_U))
        # El `if` **devuelve** el booleano: las dos ramas dejan un i32, así que
        # el bloque no puede ser de tipo vacío. Con `if_()` a secas el módulo no
        # valida, y el error de Node ("type mismatch") no dice qué bloque es.
        c.pieces.append(w.if_(w.I32))
        c.pieces.append(w.local_get(base))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))
        c.pieces.append(w.local_get(needle_base))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))
        c.pieces.append(w.op_unary(w.I32_EQ))
        c.pieces.append(w.else_())
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.end())
        c.store(target)
    table["str.eq_byte"] = str_eq_byte

    # ---- división y módulo de flotantes
    #
    # WASM tiene `f64.div`, `f64.floor` y `f64.trunc`, así que las tres
    # operaciones son dos instrucciones cada una. `rem.f64` no tiene equivalente:
    # no hay `f64.rem` en el MVP, y se calcula como `a - trunc(a/b) * b`, que es
    # el resto con el signo del dividendo, que es lo que quiere `MOD` en BASIC.
    def divfloor_f64(c, instr):
        c.push(instr.args[1])
        c.push(instr.args[2])
        c.pieces.append(w.op_unary(w.F64_DIV))
        c.pieces.append(w.op_unary(w.F64_FLOOR))
        c.store(instr.args[0])
    table["divfloor.f64"] = divfloor_f64

    def idiv_f64(c, instr):
        c.push(instr.args[1])
        c.push(instr.args[2])
        c.pieces.append(w.op_unary(w.F64_DIV))
        c.pieces.append(w.op_unary(w.F64_TRUNC))
        c.store(instr.args[0])
    table["idiv.f64"] = idiv_f64

    def rem_f64(c, instr):
        target, left, right = instr.args
        cociente = c.scratch_f64
        c.push(left)
        c.push(right)
        c.pieces.append(w.op_unary(w.F64_DIV))
        c.pieces.append(w.op_unary(w.F64_TRUNC))
        c.pieces.append(w.local_set(cociente))
        c.push(left)
        c.pieces.append(w.local_get(cociente))
        c.push(right)
        c.pieces.append(w.op_unary(w.F64_MUL))
        c.pieces.append(w.op_unary(w.F64_SUB))
        c.store(target)
    table["rem.f64"] = rem_f64

    # ---- comparación de textos
    #
    # `=` sobre dos textos es igualdad byte a byte; `COMPARE` es la de tres vías
    # de BASIC. Los dos son un recorrido con salida temprana, y la salida desde
    # dentro de un bucle se hace con `br` a la construcción de fuera: no hay
    # `break` en WASM y el `if` de la comparación se emite como bloque de tipo
    # vacío.

    def _byte_de(c, base_local, indice_local):
        c.pieces.append(w.local_get(base_local))
        c.pieces.append(w.local_get(indice_local))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))

    def str_eq(c, instr):
        target, left, right = instr.args
        base, aguja = c.local_of[left.id], c.local_of[right.id]
        largo, largo_aguja = base + 1, aguja + 1
        # resultado = 1 (iguales), y se va a 0 en cuanto se sabe que no.
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.local_set(c.scratch_addr))
        c.pieces.append(w.local_get(largo))
        c.pieces.append(w.local_get(largo_aguja))
        c.pieces.append(w.op_unary(w.I32_NE))
        c.pieces.append(w.if_())
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(c.scratch_addr))
        c.pieces.append(w.else_())
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.pieces.append(w.block())
        c.pieces.append(w.loop())
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.local_get(largo))
        c.pieces.append(w.op_unary(w.I32_GE_U))
        c.pieces.append(w.br_if(1))
        _byte_de(c, base, c.scratch_bytes)
        _byte_de(c, aguja, c.scratch_bytes)
        c.pieces.append(w.op_unary(w.I32_NE))
        c.pieces.append(w.if_())
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(c.scratch_addr))
        c.pieces.append(w.br(2))
        c.pieces.append(w.end())
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.pieces.append(w.br(0))
        c.pieces.append(w.end())
        c.pieces.append(w.end())
        c.pieces.append(w.end())
        c.pieces.append(w.local_get(c.scratch_addr))
        if valtype(target.type) == w.I64:
            c.pieces.append(w.op_unary(w.I64_EXTEND_I32_S))
        c.store(target)
    table["str.eq"] = str_eq

    def str_compare(c, instr):
        target, left, right = instr.args
        base, aguja = c.local_of[left.id], c.local_of[right.id]
        largo, largo_aguja = base + 1, aguja + 1
        i, resultado = c.scratch_bytes, c.scratch_addr
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(resultado))          # iguales
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(i))
        # Dos bloques, no uno. El bucle tiene que salir **antes** de la
        # comparación de longitudes --que sólo tiene sentido si los prefijos
        # coinciden-- y la salida por diferencia tiene que **saltársela**.
        #
        # Con un solo bloque, `br_if` al salir del bucle se iba también de la
        # comparación de longitudes y `COMPARE("ab", "abc")` daba 0.
        # Profundidades desde dentro del bucle: B(0), D(1), A(2).
        c.pieces.append(w.block())        # A
        c.pieces.append(w.block())        # D
        c.pieces.append(w.loop())         # B
        c.pieces.append(w.local_get(i))
        c.pieces.append(w.local_get(largo))
        c.pieces.append(w.op_unary(w.I32_GE_U))
        c.pieces.append(w.br_if(1))
        c.pieces.append(w.local_get(i))
        c.pieces.append(w.local_get(largo_aguja))
        c.pieces.append(w.op_unary(w.I32_GE_U))
        c.pieces.append(w.br_if(1))
        _byte_de(c, base, i)
        c.pieces.append(w.local_set(c.scratch_loop))
        _byte_de(c, aguja, i)
        c.pieces.append(w.local_set(c.scratch_quedan))
        c.pieces.append(w.local_get(c.scratch_loop))
        c.pieces.append(w.local_get(c.scratch_quedan))
        c.pieces.append(w.op_unary(w.I32_LT_U))
        c.pieces.append(w.if_())
        c.pieces.append(w.i32_const(-1))
        c.pieces.append(w.local_set(resultado))
        c.pieces.append(w.br(3))          # if(0), B(1), D(2), A(3)
        c.pieces.append(w.end())
        c.pieces.append(w.local_get(c.scratch_quedan))
        c.pieces.append(w.local_get(c.scratch_loop))
        c.pieces.append(w.op_unary(w.I32_LT_U))
        c.pieces.append(w.if_())
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.local_set(resultado))
        c.pieces.append(w.br(3))
        c.pieces.append(w.end())
        c.pieces.append(w.local_get(i))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_set(i))
        c.pieces.append(w.br(0))
        c.pieces.append(w.end())          # B
        c.pieces.append(w.end())          # D: aquí cae el bucle al agotarse
        # Los prefijos son iguales: gana el más largo.
        c.pieces.append(w.local_get(largo))
        c.pieces.append(w.local_get(largo_aguja))
        c.pieces.append(w.op_unary(w.I32_GT_U))
        c.pieces.append(w.if_())
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.local_set(resultado))
        c.pieces.append(w.else_())
        c.pieces.append(w.local_get(largo))
        c.pieces.append(w.local_get(largo_aguja))
        c.pieces.append(w.op_unary(w.I32_LT_U))
        c.pieces.append(w.if_())
        c.pieces.append(w.i32_const(-1))
        c.pieces.append(w.local_set(resultado))
        c.pieces.append(w.end())          # cierra el `if` de la longitud
        c.pieces.append(w.end())          # cierra el `if`/`else` de la longitud
        c.pieces.append(w.end())          # cierra A
        c.pieces.append(w.local_get(resultado))
        if valtype(target.type) == w.I64:
            c.pieces.append(w.op_unary(w.I64_EXTEND_I32_S))
        c.store(target)
    table["str.compare"] = str_compare

    def str_lenu(c, instr):
        target, text = instr.args[0], instr.args[1]
        # `LENU` cuenta caracteres y `LEN` cuenta bytes. Contar caracteres es
        # recorrer el texto saltando los bytes de continuación de UTF-8, que son
        # los que tienen los tres bits altos a uno: si se contasen como
        # caracteres, "ñ" valdría dos.
        base = c.local_of[text.id]
        # El índice arranca en **cero**, no en el puntero. Poner el puntero
        # hacía que el bucle saliera en la primera vuelta y `LENU` devolvía 0
        # para cualquier texto.
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(c.scratch_addr))
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(c.scratch_loop))
        c.pieces.append(w.block())
        c.pieces.append(w.loop())
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.local_get(base + 1))
        c.pieces.append(w.op_unary(w.I32_GE_U))
        c.pieces.append(w.br_if(1))
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))
        c.pieces.append(w.i32_const(0xC0))
        c.pieces.append(w.op_unary(w.I32_AND))
        c.pieces.append(w.i32_const(0x80))
        c.pieces.append(w.op_unary(w.I32_NE))
        c.pieces.append(w.if_())
        c.pieces.append(w.local_get(c.scratch_loop))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_set(c.scratch_loop))
        c.pieces.append(w.end())
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_set(c.scratch_addr))
        c.pieces.append(w.br(0))
        c.pieces.append(w.end())
        c.pieces.append(w.end())
        c.pieces.append(w.local_get(c.scratch_loop))
        # El contador va en un temporal i32 porque comparar direcciones es más
        # barato en 32 bits, pero `LENU` devuelve un INTEGER de ocho.
        if valtype(target.type) == w.I64:
            c.pieces.append(w.op_unary(w.I64_EXTEND_I32_S))
        c.store(target)
    table["str.lenu"] = str_lenu

    # ---- `STR(x)` y `VAL(x)`
    #
    # El formateo está en el glue porque un formateador de DOUBLE a texto son
    # unas doscientas instrucciones de WASM, y en JavaScript son dos. Lo que sí
    # es del backend es decidir a quién llamar: `to.str` puede recibir un
    # INTEGER, un SINGLE, un DOUBLE, un BOOLEAN o un COLOR, y pasar un entero
    # por f64 cambiaría sus últimos dígitos.
    def to_str(c, instr):
        target, operand = instr.args[0], instr.args[1]
        kind = operand.type.kind
        import_name = sir.HOST_FORMATTING["to.str"].get(kind)
        if import_name is None:
            raise unsupported(f"to.str de {kind}", instr.span)
        # Un buffer del heap donde el glue escribe el texto. 32 bytes bastan
        # para un f64 con seis decimales significativos más el signo.
        c.pieces.append(w.i32_const(32))
        c.pieces.append(w.call(c.backend.bump_index))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.push(operand)
        if kind == "SINGLE":
            c.pieces.append(w.op_unary(w.F64_PROMOTE_F32))
        if kind == "COLOR":
            # Un COLOR son cuatro bytes y `int2str` toma un INTEGER de ocho.
            # Sin signo, porque es un patrón de bits: igual que en `print_int`.
            c.pieces.append(w.op_unary(w.I64_EXTEND_I32_U))
        if kind in ("DOUBLE", "SINGLE"):
            # El modo 0 es el formato de `PRINT` y `STR`: enteros sin decimales
            # y seis cifras significativas en los demás. El 1 son decimales
            # fijos, para `FORMAT`.
            c.pieces.append(w.i32_const(0))
        c.pieces.append(w.call(c.backend.host_index(import_name)))
        c.pieces.append(w.local_set(c.local_of[target.id]))
        c.pieces.append(w.local_get(c.local_of[target.id]))
        c.pieces.append(w.call(c.backend.host_index("source.str_len")))
        c.pieces.append(w.local_set(c.local_of[target.id] + 1))
    table["to.str"] = to_str

    def to_num(c, instr):
        target, text = instr.args[0], instr.args[1]
        base = c.local_of[text.id]
        c.pieces.append(w.local_get(base))
        c.pieces.append(w.local_get(base + 1))
        c.pieces.append(w.call(c.backend.host_index("source.str2num")))
        # `VAL("42")` es un INTEGER si el destino lo es: el host devuelve f64
        # porque es lo único que sabe leer, y el truncado es una instrucción.
        if target.type.kind == "INTEGER":
            c.pieces.append(w.op_unary(w.I64_TRUNC_F64_S))
        elif target.type.kind == "SINGLE":
            c.pieces.append(w.op_unary(w.F32_DEMOTE_F64))
        c.store(target)
    table["to.num"] = to_num

    def buscar(c, instr, primer_match):
        """`CONTAINS` e `INDEXOF`: el mismo recorrido, distinto final.

        Se busca la aguja comparando byte a byte desde cada posición. Es lo
        más corto que funciona en el MVP: `memory.match` no existe y un
        polinomio de Rolling Hash dentro del backend sería otra aritmética que
        mantener.

        La estructura, en orden:

            i = 0
            resultado = -1            (0 para CONTAINS)
            mientras i + n <= m:
                j = 0
                mientras j < n y texto[i+j] == aguja[j]: j++
                si j == n: resultado = i; salir
                i = 1 + i

        Los cuatro temporales son `scratch_bytes` (i), `scratch_addr` (j),
        `scratch_loop` (la longitud de la aguja) y `scratch_quedan` (el
        resultado).
        """
        target, text, needle = instr.args[0], instr.args[1], instr.args[2]
        base, aguja = c.local_of[text.id], c.local_of[needle.id]
        hay_len = base + 1

        # El temporal guarda la **posición** del match, y -1 si no aparece.
        # Guardar el resultado final aquí mezclaba las dos cosas: un match en la
        # posición 0 era indistinguible de "no encontrado", y `CONTAINS` salía
        # con la posición en vez de con un booleano.
        c.pieces.append(w.i32_const(-1))
        c.pieces.append(w.local_set(c.scratch_quedan))       # posición
        c.push_local(aguja + 1, "COLOR")
        c.pieces.append(w.local_set(c.scratch_loop))         # n
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(c.scratch_bytes))        # i

        c.pieces.append(w.block())                           # A
        c.pieces.append(w.loop())                            # B
        # Sale si `i` se sale del texto, o si lo que queda es **más corto** que
        # la aguja. Es `<` y no `<=`: con `<=` el texto entero se saltaba la
        # posición del final, y `INDEXOF("mundo")` en "hola mundo" daba -1
        # porque la aguja acaba justo donde el texto acaba.
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.local_get(hay_len))
        c.pieces.append(w.op_unary(w.I32_GE_U))
        c.pieces.append(w.br_if(1))
        c.pieces.append(w.local_get(hay_len))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.op_unary(w.I32_SUB))
        c.pieces.append(w.local_get(c.scratch_loop))
        c.pieces.append(w.op_unary(w.I32_LT_U))
        c.pieces.append(w.br_if(1))
        # Compara la aguja entera desde `i`.
        c.pieces.append(w.i32_const(0))
        c.pieces.append(w.local_set(c.scratch_addr))         # j = 0
        c.pieces.append(w.block())                           # C
        c.pieces.append(w.loop())                            # D
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.local_get(c.scratch_loop))
        c.pieces.append(w.op_unary(w.I32_GE_U))
        c.pieces.append(w.br_if(1))
        c.pieces.append(w.local_get(base))
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))
        c.pieces.append(w.local_get(aguja))
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.memarg(w.I32_LOAD8_U, 0, 0))
        c.pieces.append(w.op_unary(w.I32_NE))
        c.pieces.append(w.br_if(1))
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_set(c.scratch_addr))
        c.pieces.append(w.br(0))
        c.pieces.append(w.end())                             # D
        c.pieces.append(w.end())                             # C
        # `j >= n` significa que todos los bytes comparados coincidían.
        c.pieces.append(w.local_get(c.scratch_addr))
        c.pieces.append(w.local_get(c.scratch_loop))
        c.pieces.append(w.op_unary(w.I32_GE_U))
        c.pieces.append(w.if_())
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.local_set(c.scratch_quedan))
        # En este punto C y D ya están cerrados, así que desde dentro del `if` lo
        # abierto es: if(0), B(1), A(2). Contar también los bucles internos
        # --que ya se cerraron-- salta a una construcción inexistente.
        c.pieces.append(w.br(2))
        c.pieces.append(w.end())
        c.pieces.append(w.local_get(c.scratch_bytes))
        c.pieces.append(w.i32_const(1))
        c.pieces.append(w.op_unary(w.I32_ADD))
        c.pieces.append(w.local_set(c.scratch_bytes))
        c.pieces.append(w.br(0))
        c.pieces.append(w.end())                             # B
        c.pieces.append(w.end())                             # A

        c.pieces.append(w.local_get(c.scratch_quedan))
        if primer_match:
            # `INDEXOF` devuelve la posición, pero en 0 significa "no
            # encontrado" y BASIC usa 1 para "sí". La convención del IR es
            # -1 para no encontrado y 0 es un match legítimo, así que el 0 se
            # convierte en 1.
            c.pieces.append(w.i32_const(0))
            c.pieces.append(w.op_unary(w.I32_EQ))
            c.pieces.append(w.if_(w.I32))
            c.pieces.append(w.i32_const(1))
            c.pieces.append(w.else_())
            c.pieces.append(w.local_get(c.scratch_quedan))
            c.pieces.append(w.end())
        else:
            # `CONTAINS` sólo quiere saber si apareció.
            c.pieces.append(w.local_get(c.scratch_quedan))
            c.pieces.append(w.i32_const(0))
            c.pieces.append(w.op_unary(w.I32_LT_S))
            c.pieces.append(w.if_(w.I32))
            c.pieces.append(w.i32_const(0))
            c.pieces.append(w.else_())
            c.pieces.append(w.i32_const(1))
            c.pieces.append(w.end())
        if valtype(target.type) == w.I64:
            c.pieces.append(w.op_unary(w.I64_EXTEND_I32_S))
        c.store(target)
    table["str.contains"] = lambda c, i: buscar(c, i, False)
    table["str.indexof"] = lambda c, i: buscar(c, i, True)

    return table


# (tipo de origen, tipo de destino) -> opcode. El MVP no tiene conversión
# directa entre todos los pares: los que faltan no se emiten, y el error lo
# nombra en vez de elegir el opcode más parecido.
_CONVERSION = {
    ("INTEGER", "SINGLE"): w.F32_CONVERT_I64_S,
    ("INTEGER", "DOUBLE"): w.F64_CONVERT_I64_S,
    ("BOOLEAN", "INTEGER"): w.I64_EXTEND_I32_S,
    ("COLOR", "INTEGER"): w.I64_EXTEND_I32_S,
    ("SINGLE", "DOUBLE"): w.F64_PROMOTE_F32,
    ("SINGLE", "INTEGER"): w.I64_TRUNC_F32_S,
    ("DOUBLE", "SINGLE"): w.F32_DEMOTE_F64,
    ("DOUBLE", "INTEGER"): w.I64_TRUNC_F64_S,
}


def wasm_type_of_kind(kind: str) -> int:
    return valtype(kind)


# Cómo se lee y se escribe un elemento de cada tipo en memoria lineal. El
# `align` es log2(bytes), que es lo que pide el memarg de WASM.
_LOAD_OF_KIND = {
    "INTEGER": (w.I64_LOAD, 3), "DOUBLE": (w.F64_LOAD, 3),
    "SINGLE": (w.F32_LOAD, 2), "BOOLEAN": (w.I32_LOAD, 2),
    "COLOR": (w.I32_LOAD, 2), "PTR": (w.I32_LOAD, 2),
}
_STORE_OF_KIND = {
    "INTEGER": (w.I64_STORE, 3), "DOUBLE": (w.F64_STORE, 3),
    "SINGLE": (w.F32_STORE, 2), "BOOLEAN": (w.I32_STORE, 2),
    "COLOR": (w.I32_STORE, 2), "PTR": (w.I32_STORE, 2),
}


# ============================================================ módulo entero


class WasmBackend:
    """Compila un `sir.Module` completo a bytes WASM."""

    def __init__(self, module: sir.Module):
        self.module = module
        self.builder = w.ModuleBuilder(module.name)
        self.string_offsets: dict[int, int] = {}
        self.host_indices: dict[str, int] = {}
        self.func_indices: dict[int, int] = {}
        self.heap_base = DATA_BASE
        self.bump_index: int | None = None
        self.print_array_index: int | None = None
        # Imports que usa una función interna del módulo y no ninguna
        # instrucción del IR. Van aparte de `module.host_imports` para no
        # mutar el módulo, que es del llamante.
        self.extra_imports: set[str] = set()
        self.pies_base = 0
        self._pies_index: int | None = None
        # Un array impreso necesita sus caracteres --corchetes, coma y comilla--.
        # Va una sola cadena y no cuatro constantes: cada una sería su propio
        # segmento de datos, y son cinco bytes.
        if any(instr.op == "host.print_arr"
               for func in module.funcs for instr in func.instrs):
            self.extra_imports |= {"source.print_str", "source.print_bool",
                                   "source.str_len", "source.int2str",
                                   "source.num2str"}
            self._pies_index = module.add_string(SIGUIES_ARRAY)
        # Ancho en locales de cada parámetro, por cuerpo. El verificador lo
        # necesita porque la firma no lo dice: `i32` puede ser un entero o media
        # cadena.
        self._param_words: list = []
        self.layout_strings()
        if self._pies_index is not None:
            self.pies_base = self.string_offset(self._pies_index)
        self.declare_imports()
        self.declare_bump()
        if self._pies_index is not None:
            self._declare_print_array()

    # --------------------------------------------------------------- memoria

    def layout_strings(self) -> None:
        cursor = DATA_BASE
        for index, raw in enumerate(self.module.strings):
            self.string_offsets[index] = cursor
            cursor += len(raw)
        self.heap_base = _align(cursor, HEAP_ALIGN)

    def string_offset(self, index: int) -> int:
        return self.string_offsets[index]

    # --------------------------------------------------------------- imports

    def declare_imports(self) -> None:
        """Importa en orden alfabético, no en el orden de aparición.

        El orden de los imports fija los índices de función, y esos índices
        aparecen en el código. Importar en orden de aparición haría que un
        programa distinto del suyo produjera el mismo binario, y rompería la
        caché por contenido.
        """
        for name in sorted(set(self.module.host_imports) | self.extra_imports):
            signature = HOST_SIGNATURES.get(name)
            if signature is None:
                raise CompileError(Diagnostic(
                    "E0060",
                    f"el host expone '{name}' y no hay firma declarada",
                    Span(0, 0),
                    "añádela a HOST_SIGNATURES en backend.py: el glue la "
                    "implementa y los dos lados tienen que coincidir",
                ))
            params, results = signature
            self.host_indices[name] = self.builder.import_function(
                "source", name.removeprefix("source."), params, results)

    def host_signature(self, name: str) -> tuple:
        return HOST_SIGNATURES[name]

    def host_index(self, name: str) -> int:
        try:
            return self.host_indices[name]
        except KeyError:
            raise CompileError(Diagnostic(
                "E0060", f"falta el import '{name}'", Span(0, 0),
                "el módulo declara un uso del host que no llegó a los imports; "
                "falta `uses_host` en el SIR",
            )) from None

    def func_index(self, sir_index: int) -> int:
        return self.func_indices[sir_index]

    # -------------------------------------------------------------- funciones

    def declare_bump(self) -> None:
        """El allocator del módulo, como función propia.

        Un bump allocator son un global y unas veinte instrucciones. Ponerlo en
        el glue costaría bytes en cada programa con arrays y metería lógica del
        runtime en JavaScript; en la Fase 4 lo sustituye el allocator escrito
        en Source (ADR 0015).

        Se declara **antes** que las funciones del programa porque su índice
        tiene que existir cuando `array.alloc` lo referencia. Los índices de
        función no son el orden del IR, sino el orden de declaración más los
        imports, y confundirlos es el error más habitual al escribir un backend.
        """
        # Cualquier operación que reserve memoria necesita el allocator. Antes
        # sólo miraba `array.alloc`, y un programa que concatenaba textos sin
        # arrays se quedaba con `bump_index = None` y un `AttributeError` en
        # vez de un módulo.
        if not any(instr.op in OPS_QUE_RESERVAN
                   for func in self.module.funcs for instr in func.instrs):
            return

        self.builder.add_global(w.I32, True, w.i32_const(self.heap_base))
        self.builder.add_memory(1)
        body = self.builder.add_function("__bump", params=(w.I32,),
                                         results=(w.I32,), locals=[(2, w.I32)])
        emit = body.emit
        emit(w.global_get(0))                     # local 1 = ptr = heap
        emit(w.local_set(1))
        # heap += align(bytes, 8)
        emit(w.global_get(0))
        emit(w.local_get(0))
        emit(w.i32_const(HEAP_ALIGN - 1))
        emit(w.op_unary(w.I32_ADD))
        emit(w.i32_const(-HEAP_ALIGN))
        emit(w.op_unary(w.I32_AND))
        emit(w.op_unary(w.I32_ADD))
        emit(w.global_set(0))
        # 2 = páginas necesarias = (heap + 65535) >> 16
        emit(w.global_get(0))
        emit(w.i32_const(w.PAGE_SIZE - 1))
        emit(w.op_unary(w.I32_ADD))
        emit(w.i32_const(16))
        emit(w.op_unary(w.I32_SHR_U))
        emit(w.local_set(2))
        # Crecer de una página en una mientras no quepa.
        #
        # No se puede escribir `memory.grow(lo que falte)`: el número de páginas
        # a crecer es un **inmediato literal**, y lo que falta depende del
        # tamaño del array, que es un valor de ejecución. Un solo `memory.grow`
        # con un delta fijo reservaría de más en cada programa con arrays, y uno
        # calculado en runtime no existe. Por eso el bucle.
        # El `block` exterior es lo que permite salir: dentro de un `loop`, un
        # `br_if` a la profundidad 0 salta a la **cabecera**, que es seguir
        # dando vueltas. Hay que saltar al `block`, que está en la profundidad 1.
        emit(w.block())
        emit(w.loop())
        emit(w.local_get(2))
        emit(w.memory_size())
        emit(w.op_unary(w.I32_LE_U))
        emit(w.br_if(1))
        emit(w.memory_size())
        emit(w.memory_grow(1))
        emit(w.drop())
        emit(w.br(0))
        emit(w.end())
        emit(w.end())
        emit(w.local_get(1))                      # return ptr
        self._param_words.append([1])              # un solo parámetro i32
        self.bump_index = self.builder.func_index(body)

    def _declare_print_array(self) -> None:
        """`@__printArray`: imprime un array con la notación del lenguaje.

        Vive en el módulo y no en el glue por dos razones. La primera es el
        peso: el glue se descarga en cada página, y un formateador de arrays son
        unos 450 B brotli que paga todo el mundo por un caso que casi ningún
        programa tiene. La segunda es de sitio: es semántica del lenguaje, y el
        runtime se reescribe en Source en la Fase 4 --un formateador en
        JavaScript es justo el tipo de cosa que luego hay que traducir.

        El módulo imprime elemento a elemento con las mismas primitivas de
        `PRINT`, y lo único que sigue haciendo el host es formatear un número,
        que ya hacía.
        """
        importes = {name: self.host_index(name) for name in self.extra_imports}

        # `@__printArray` se declara **antes** que `@__printElem` porque el
        # elemento lo llama --un array dentro de un array-- y el índice de una
        # función es el de su declaración. El cuerpo se emite después; al
        # builder le da igual, lo que importa es el orden de declaración.
        bucle = self.builder.add_function(
            "__printArray", params=(w.I32, w.I32, w.I32, w.I32), results=(),
            locals=[(2, w.I32)])               # 4 = i, 5 = at
        self._param_words.append([0, 0, 0, 0])
        self.print_array_index = self.builder.func_index(bucle)

        def carga(codigo, alineado, desplazamiento=0):
            return [w.local_get(0), w.memarg(codigo, alineado, desplazamiento)]

        def letra(cual, longitud=1):
            """`print_str` del carácter guardado en la cadena de datos."""
            return [w.i32_const(self.pies_base + cual),
                    w.i32_const(longitud),
                    w.call(importes["source.print_str"])]

        # ---- `@__printElem(at, elem) -> i32`: imprime uno y devuelve su tamaño.
        cuerpo = self.builder.add_function(
            "__printElem", params=(w.I32, w.I32), results=(w.I32,),
            locals=[(1, w.I32)])               # local 2 = buffer del heap
        emit = cuerpo.emit
        BUFFER = 2

        def salir(tamano):
            emit(w.i32_const(tamano))
            emit(w.op_unary(w.RETURN))

        def numero(importe, valor, sin_signo=False):
            """Formatea en un buffer del heap e imprime el resultado.

            Es `STR$` escribiendo por `print_str` en vez de por el valor de la
            expresión. El buffer se pide al allocator del módulo, que para esto
            ya está declarado.
            """
            emit(w.i32_const(FORMAT_BUFFER))
            emit(w.call(self.bump_index))
            emit(w.local_set(BUFFER))
            emit(w.local_get(BUFFER))
            for trozo in valor:
                emit(trozo)
            if sin_signo:
                emit(w.op_unary(w.I64_EXTEND_I32_U))
            if importe == "source.num2str":
                emit(w.i32_const(0))           # modo `PRINT`
            emit(w.call(importes[importe]))
            emit(w.local_get(BUFFER))
            emit(w.call(importes["source.str_len"]))
            emit(w.call(importes["source.print_str"]))

        # El orden va del tipo más frecuente al menos: `elem` es un i32 y cada
        # comparación son tres bytes.
        for code, tamano in ((2, 8), (5, 8), (4, 8), (3, 4), (1, 4), (0, 4)):
            emit(w.local_get(1))
            emit(w.i32_const(code))
            emit(w.op_unary(w.I32_EQ))
            emit(w.if_())
            if code == 5:
                # Un texto dentro va entre comillas dobles, como se escribe en
                # el fuente, para que lo que sale se pueda volver a leer.
                for trozo in letra(COMILLA):
                    emit(trozo)
                for trozo in carga(w.I32_LOAD, 2, 0) + carga(w.I32_LOAD, 2, 4):
                    emit(trozo)
                emit(w.call(importes["source.print_str"]))
                for trozo in letra(COMILLA):
                    emit(trozo)
            elif code == 0:
                for trozo in carga(w.I32_LOAD, 2, 0):
                    emit(trozo)
                emit(w.call(importes["source.print_bool"]))
            elif code == 3:
                numero("source.num2str",
                       carga(w.F32_LOAD, 2, 0) + [w.op_unary(w.F64_PROMOTE_F32)])
            elif code == 4:
                numero("source.num2str", carga(w.F64_LOAD, 3, 0))
            else:
                numero("source.int2str",
                       carga(w.I32_LOAD, 2, 0) if code == 1
                       else carga(w.I64_LOAD, 3, 0),
                       sin_signo=code == 1)
            salir(tamano)
            emit(w.end())
        # ARRAY es el último porque es el único que se llama a sí mismo.
        emit(w.local_get(1))
        emit(w.i32_const(6))
        emit(w.op_unary(w.I32_EQ))
        emit(w.if_())
        # Sin corchetes aquí: `@__printArray` ya cierra el suyo, y ponerlos
        # los dos metía un nivel de más --`[[[1, 2]], [[3]]]`.
        for desplazamiento in (0, 4, 8, 12):
            for trozo in carga(w.I32_LOAD, 2, desplazamiento):
                emit(trozo)
        emit(w.call(self.print_array_index))
        salir(16)
        emit(w.end())
        # Un tipo desconocido se imprimiría como un entero de ocho bytes. No
        # hay forma honesta de formatearlo, y no puede pasar: el código sale
        # del descriptor, que lo escribe este mismo módulo.
        numero("source.int2str", carga(w.I64_LOAD, 3, 0))
        salir(8)
        self._param_words.append([0, 0])
        elem_index = self.builder.func_index(cuerpo)

        # ---- `@__printArray(ptr, len, cap, elem)`: el bucle.
        emit = bucle.emit
        for trozo in letra(LLAVETA):
            emit(trozo)
        emit(w.i32_const(0))
        emit(w.local_set(4))                   # i = 0
        emit(w.local_get(0))
        emit(w.local_set(5))                   # at = ptr
        emit(w.block())
        emit(w.loop())
        emit(w.local_get(4))
        emit(w.local_get(1))
        emit(w.op_unary(w.I32_GE_U))
        emit(w.br_if(1))
        # La coma va antes del elemento, menos en el primero.
        emit(w.local_get(4))
        emit(w.if_())
        for trozo in letra(COMA, 2):
            emit(trozo)
        emit(w.end())
        emit(w.local_get(5))
        emit(w.local_get(3))
        # El tamaño que devuelve `@__printElem` es lo que avanza `at`: cada
        # tipo mide lo suyo --ocho bytes un texto, dieciséis un array-- y el
        # bucle no multiplica por nada.
        emit(w.call(elem_index))
        emit(w.local_get(5))
        emit(w.op_unary(w.I32_ADD))
        emit(w.local_set(5))
        emit(w.local_get(4))
        emit(w.i32_const(1))
        emit(w.op_unary(w.I32_ADD))
        emit(w.local_set(4))
        emit(w.br(0))
        emit(w.end())
        emit(w.end())
        for trozo in letra(CIERRE):
            emit(trozo)

    def _declare_copy(self) -> None:
        """`@__copy(destino, origen, bytes)`: copia de memoria, byte a byte.

        La declara el módulo, no el glue: es lógica del lenguaje, y meterla en
        JavaScript pondría el runtime fuera del programa.
        """
        body = self.builder.add_function("__copy",
                                         params=(w.I32, w.I32, w.I32),
                                         results=(), locals=[(1, w.I32)])
        emit = body.emit
        emit(w.i32_const(0))
        emit(w.local_set(3))                      # 3 = i
        emit(w.block())
        emit(w.loop())
        emit(w.local_get(3))
        emit(w.local_get(2))                      # 2 = bytes
        emit(w.op_unary(w.I32_GE_U))
        emit(w.br_if(1))
        emit(w.local_get(0))
        emit(w.local_get(3))
        emit(w.op_unary(w.I32_ADD))
        emit(w.local_get(1))
        emit(w.local_get(3))
        emit(w.op_unary(w.I32_ADD))
        emit(w.memarg(w.I32_LOAD8_U, 0, 0))
        emit(w.memarg(w.I32_STORE8, 0, 0))
        emit(w.local_get(3))
        emit(w.i32_const(1))
        emit(w.op_unary(w.I32_ADD))
        emit(w.local_set(3))
        emit(w.br(0))
        emit(w.end())
        emit(w.end())
        self._param_words.append([0, 0, 0])
        self.copy_index = self.builder.func_index(body)

    def compile(self) -> None:
        """Baja todas las funciones. La llama `build()`, una sola vez.

        Reentrante no es lo mismo que idempotente: llamarla dos veces añade las
        funciones otra vez, `func_index` devuelve el índice de la copia, y el
        módulo valida con dos `main` y el glue llama a la segunda. Un error que
        no se ve.
        """
        if self.func_indices:
            raise CompileError(Diagnostic(
                "E0064", "el backend ya se compiló", Span(0, 0),
                "`compile()` se llama una vez desde `build()`; volver a "
                "llamarla duplica las funciones en el módulo",
            ))
        # Dos pasadas, y el orden importa. La primera declara todas las
        # funciones y les da su índice; la segunda genera el código.
        #
        # En una sola pasada, una función recursiva --o una que llama a otra
        # declarada más abajo, que el IR permite a propósito-- emitted `call`
        # con un índice que todavía no existía, y el fallo salía como
        # `KeyError` en vez de como un error de compilación con nombre.
        bodies = []
        for func in self.module.funcs:
            compiler = FunctionCompiler(self, func)
            params, results = compiler.signature()
            body = self.builder.add_function(func.name, params, results,
                                             compiler.locals)
            self.func_indices[func.index] = self.builder.func_index(body)
            self._param_words.append([words_of(t) for _, t in func.params])
            bodies.append((compiler, body))
        for compiler, body in bodies:
            compiler.compile()
            body.pieces = compiler.pieces

    # ----------------------------------------------------------------- salida

    def verify(self) -> None:
        """Comprueba los tipos de la pila de cada función antes de serializar.

        Sin esto, un `i32.add` donde tocaba un `i64.add` sale como un error de
        instanciación en Node, a cientos de líneas del código que lo causó. Con
        esto sale como un error de compilación que nombra la función y la
        instrucción.
        """
        calls = {indice: ([verify._BY_TYPE.get(t, "?") for t in signature.params],
                          [verify._BY_TYPE.get(t, "?") for t in signature.results])
                 for indice, signature in self._signatures().items()}
        for body in self.builder.funcs:
            try:
                verify.check_function(body.name, body.signature, body.locals,
                                      body.pieces, calls)
            except verify.SignatureError as error:
                raise CompileError(Diagnostic(
                    "E0065",
                    "el backend emitió código con los tipos equivocados",
                    Span(0, 0),
                    str(error.detail) + " -- mira la operación que la emite en "
                    "la tabla de backend.py",
                )) from None

    def _signatures(self) -> dict:
        firmas = {}
        for indice, import_ in enumerate(self.builder.imports):
            firmas[indice] = self.builder.types[import_.type_index]
        for posicion, body in enumerate(self.builder.funcs):
            firmas[self.builder.import_count + posicion] = body.signature
        return firmas

    def build(self) -> bytes:
        self.compile()
        self.verify()
        for index, raw in enumerate(self.module.strings):
            if raw:
                self.builder.add_data(self.string_offsets[index], raw)
        self.builder.add_memory(1)
        self.builder.export_memory("memory")

        main = next((f for f in self.module.funcs if f.name == "main"), None)
        if main is None:
            raise CompileError(Diagnostic(
                "E0063", "el módulo no tiene función main", Span(0, 0),
                "el glue llama a `main`: sin ella no hay programa",
            ))
        self.builder.export("main", w.KIND_FUNC, self.func_indices[main.index])
        return self.builder.to_bytes()


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def compile_module(module: sir.Module) -> bytes:
    """Punto de entrada: SIR a bytes WASM."""
    return WasmBackend(module).build()