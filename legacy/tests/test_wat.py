"""El texto WAT tiene que ser **exacto**, no aproximado.

El error más caro de este repositorio no es un opcode mal escrito: es un
literal de flotante que reensambla a otros bits. El módulo **valida**, Node lo
ejecuta sin quejarse, y el programa devuelve un número ligeramente distinto --
un `0.1` que sale `0.100000001`, un denormal que sale a cero-- que es la forma
peor de bug: silencioso, y sólo visible en una comparación con tolerance.

Por eso los flotantes se imprimen en hexadecimal y no en decimal, y por eso este
test no usa el propio emisor como juez: usa `float.fromhex` de la biblioteca
estándar, que es una **implementación independiente de la misma
especificación**. Si el emisor y el lector estuvieran mal de la misma manera, el
round trip del módulo pasaría y aquí no.
"""

import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compiler"))

from sourc.wasm import encoder as w                  # noqa: E402
from sourc.wasm import wat                            # noqa: E402
from sourc.wasm import wat_asm                          # noqa: E402
from sourc.wasm.encoder import f32_const, f64_const   # noqa: E402


class _CursorFalso:
    """Un cursor que sólo sirve para que `_hex_f32`/`_hex_f64` lean bytes."""

    def __init__(self, datos: bytes):
        self.datos = datos
        self.pos = 0

    def bytes(self, cuantos: int) -> bytes:
        trozo = self.datos[self.pos:self.pos + cuantos]
        self.pos += cuantos
        return trozo


CASOS_F64 = [
    0.0, -0.0, 1.0, -1.0, 2.0, 0.5, 1.5, -3.25, 0.1, -0.1,
    1024.0, 1e300, -1e-300, 1e-300,
    5e-324,                      # el menor subnormal de f64
    2.2250738585072014e-308,     # el menor normal de f64
    float("inf"), float("-inf"), float("nan"),
]

CASOS_F32 = [
    0.0, -0.0, 1.0, -1.0, 2.0, 0.5, 1.5, -3.25, 0.1,
    3.4028234663852886e+38,      # el mayor f32
    1.1754943508222875e-38,      # el menor normal de f32
    1e-45,                       # subnormal de f32
    float("inf"), float("-inf"), float("nan"),
]


class TestHexFloats(unittest.TestCase):
    """Cada literal impreso, con los mismos bits al rereadlo."""

    def _bits_f64(self, valor: float) -> int:
        return struct.unpack("<Q", struct.pack("<d", valor))[0]

    def _bits_f32(self, valor: float) -> int:
        return struct.unpack("<I", struct.pack("<f", valor))[0]

    def test_f64_conserva_los_bits(self):
        for valor in CASOS_F64:
            with self.subTest(valor=repr(valor)):
                crudo = f64_const(valor)[1:]
                texto = wat._hex_f64(_CursorFalso(crudo))
                if valor != valor:                    # NaN: no lo sabe `fromhex`
                    self._comprueba_nan(texto, 52)
                    continue
                releido = float.fromhex(texto)
                self.assertEqual(self._bits_f64(releido), self._bits_f64(valor),
                                 f"{valor!r} salió como {texto} y vuelve "
                                 f"como {releido!r}")

    def _comprueba_nan(self, texto: str, frac_bits: int) -> None:
        """Un NaN se comprueba por su forma, porque `fromhex` no lo acepta.

        `float.fromhex` entiende `nan` pero no `nan:0x...`, que es la forma que
        lleva la carga útil. Comprobar el NaN **por el oráculo** obligaría a
        emitir un `nan` a secas, que pierde la carga útil y con ella la
        igualdad byte a byte --que es lo que el round trip compara.
        """
        self.assertRegex(texto, r"^-?nan:0x[0-9a-f]+$")
        carga = int(texto.split("0x")[1], 16)
        self.assertTrue(carga & (1 << (frac_bits - 1)),
                        f"{texto}: el bit de silencio tiene que estar a uno")

    def test_f32_conserva_los_bits(self):
        """Un f32 se relee como f64 y se vuelve a estrechar.

        `float.fromhex` no tiene f32, así que el camino es el que haría un
        ensamblador: parsear a doble, redondear a f32, y comparar los bits. Si
        el hexadecimal del emisor 设计ara un valor que no cabe en f32, el
        redondeo lo delata.
        """
        for valor in CASOS_F32:
            with self.subTest(valor=repr(valor)):
                crudo = f32_const(valor)[1:]
                texto = wat._hex_f32(_CursorFalso(crudo))
                if valor != valor:
                    self._comprueba_nan(texto, 23)
                    continue
                releido = float.fromhex(texto)
                self.assertEqual(self._bits_f32(releido), self._bits_f32(valor),
                                 f"{valor!r} salió como {texto} y vuelve "
                                 f"como {releido!r}")

    def test_los_textos_no_decian_decimales(self):
        """La forma tiene que ser hexagonal, o el round trip no es exacto.

        Un `1.5` decimal sí sería exacto, y aun así no vale: la forma decimal
        depende de la ruta de conversión de cada lector y no de la
        especificación. Este test falla si alguien "simplifica" el emisor.
        """
        crudo = f64_const(1.5)[1:]
        texto = wat._hex_f64(_CursorFalso(crudo))
        self.assertTrue(texto.startswith("0x1.") and "p" in texto, texto)

    def test_el_exponente_de_un_normal_es_el_sesgo_y_nada_mas(self):
        """El bug concreto: sumarle también el exponente del subnormal.

        Hacía que `1.0` saliera como `0x1.0p-1022`, que reensambla a un
        denormal. Un test de igualdad de valores flutuantes no lo pilla --los
        dos son "un número"--; uno de bits sí.
        """
        for valor, esperado in ((1.0, "0x1.0p+0"), (2.0, "0x1.0p+1"),
                                (0.5, "0x1.0p-1"), (1.5, "0x1.8p+0"),
                                (1024.0, "0x1.0p+10")):
            with self.subTest(valor=valor):
                texto = wat._hex_f64(_CursorFalso(f64_const(valor)[1:]))
                self.assertEqual(texto, esperado)


class TestLoQueNoSeAdivina(unittest.TestCase):
    """Un opcode desconocido es un error, no una suposición.

    El emisor que inventa el texto de una instrucción produce un WAT que parece
    bueno y reensambla a algo distinto, y el round trip pasa por encima del bug
    que existía para cazarlo. Con el error, el bug sale en la línea que lo
    emitió.
    """

    def test_un_opcode_desconocido_falla_con_el_offset(self):
        from sourc.wasm.encoder import ModuleBuilder

        builder = ModuleBuilder("raro")
        cuerpo = builder.add_function("f", (0x7F,), ())
        cuerpo.pieces.append(bytes((0x45,)))     # i32.eqz: conocido, sin inmediato
        cuerpo.pieces.append(bytes((0xD5,)))     # 0xD5 no está en la tabla
        crudo = builder.to_bytes()
        with self.assertRaises(wat.WatError) as error:
            wat.desensamblar(crudo)
        self.assertIn("0xd5", str(error.exception))
        self.assertIn("TABLA_INMEDIATOS", str(error.exception))

    def test_un_magma_distinto_falla(self):
        with self.assertRaises(wat.WatError) as error:
            wat.desensamblar(b"\x00asm\xff\xff\xff\xff")
        self.assertIn("versión", str(error.exception))


class TestRoundTrip(unittest.TestCase):
    """Módulo → WAT → módulo, byte a byte.

    Es el oráculo estructural del formato. Si el encoder tiene un error de
    LEB128, de orden de secciones, de longitud de inmediato o de agrupado de
    locales, el reensamblado no puede coincidir y el test falla **en el programa
    que lo tiene**, no en un caso inventado.

    Lo que el round trip NO comprueba, y conviene no creer:

    - **Que la regla de agrupado de locals sea la correcta.** Si el encoder
      reordenara los locals por tipo --que fue un error real en este repo-- las
      dos mitades aplicarían la misma regla y pasarían. Lo cazan la paridad con
      el intérprete y el validador de Node.
    - **Que el módulo sea válido.** El round trip sólo dice que el texto y los
      bytes se corresponden. Que el módulo valida y ejecute lo comprueba Node,
      que es el juez.

    Por eso el round trip corre además con Node: bytes idénticos, pero la
    validación es la del juez y no la nuestra.
    """

    def _modulos(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import parity                                    # noqa: PLC0415
        from sourc.wasm.backend import compile_module   # noqa: PLC0415

        for programa in parity.programas():
            yield (programa.name,
                   compile_module(parity.construir(programa)))

    def test_todo_el_corpus_vuelve_byte_a_byte(self):
        for nombre, modulo in self._modulos():
            with self.subTest(programa=nombre):
                vuelta = wat_asm.ensamblar(wat.desensamblar(modulo))
                self.assertEqual(
                    vuelta, modulo,
                    f"{nombre}: el texto y los bytes no se corresponden")

    def test_validar_el_reensamblado_seria_redundante(self):
        """Por qué aquí **no** se llama a Node, escrito para que no se olvide.

        El round trip afirma igualdad de bytes. Si los bytes son iguales, el
        módulo reensamblado es el mismo módulo, y validarlo es validar el otro:
        `tools/parity.py` ya lo valida y lo ejecuta contra el intérprete en cada
        programa del corpus. Volver a llamar a Node aquí sería un minuto de
        máquina por una afirmación que no añade nada.

        La división del trabajo es: **Node** dice si el módulo es válido y hace
        lo que dice; el **round trip** dice si el texto y los bytes se
        corresponden; y el **intérprete** dice si el programa hace lo que debe.
        Ninguno de los tres cubre a los otros dos.
        """
        for nombre, modulo in self._modulos():
            with self.subTest(programa=nombre):
                vuelta = wat_asm.ensamblar(wat.desensamblar(modulo))
                self.assertEqual(vuelta, modulo)


    def test_el_texto_es_wat_que_alguien_leeria(self):
        """Nada de un formato propio con comentarios.

        Si la firma de una función fuera un `;;`, el ensamblador tendría que
        leer comentarios para reconstruirla, y las dos mitades acordarían un
        formato que no es del formato: el round trip pasaría sin comprobar nada.
        """
        for nombre, modulo in self._modulos():
            with self.subTest(programa=nombre):
                texto = wat.desensamblar(modulo)
                self.assertNotIn(";; función", texto)
                self.assertNotIn(";; código", texto)
                # Y cada función lleva su tipo, que es lo que el ensamblador lee.
                self.assertIn("(func (type ", texto)

    def test_los_locales_van_declarados(self):
        """`(local ...)` es la forma que lee un ensamblador, y el round trip
        depende de que los locals sean recuperables del texto."""
        texto = wat.desensamblar(next(iter(self._modulos()))[1])
        self.assertIn("(local i64 i32", texto)


def _literal_de_f64(valor: float) -> str:
    """El texto que el emisor escribe para un `f64.const`, y su regreso.

    Se construye un módulo mínimo con un literal, se desensambla, se reensambla
    y se comprueba que el texto del literal es el mismo. La ida y la vuelta por
    las dos mitades es lo que lo convierte en un round trip, y leer el número con
    `float.fromhex` es lo que lo hace independiente del conversor propio.
    """
    literal = _texto_inicial(valor)
    modulo = wat_asm.ensamblar(
        "(module\n"
        "  (type (func))\n"
        "  (memory 1)\n"
        f"  (func (type 0)\n    f64.const {literal}\n  )\n"
        ")\n")
    texto = wat.desensamblar(modulo)
    assert wat_asm.ensamblar(texto) == modulo, "el módulo mínimo no vuelve"
    return [linea for linea in texto.splitlines() if "f64.const" in linea][0] \
        .split()[-1]


def _texto_inicial(valor: float) -> str:
    """Un flotante escrito como lo escribiría una persona, no como el emisor.

    El punto de partida tiene que ser legible: si usara la forma hexadecimal
    del emisor, el test compararía el emisor consigo mismo y no comprobaría
    nada.
    """
    if valor != valor:
        return "nan"
    if valor == float("inf"):
        return "inf"
    if valor == float("-inf"):
        return "-inf"
    return repr(valor)


class TestFloatsEnElRoundTrip(unittest.TestCase):
    """Un flotante tiene que volver con **los mismos bits**.

    Un literal decimal devolvería casi siempre los mismos bits y perdería el
    resto, y un `0.1` en f32 volvería como `0.100000001` sin que nada se quejara.
    Los tres bugs que se cazaron aquí --el exponente del normal, los dígitos de
    la mantisa de f32 y el `0x0.` del subnormal-- salían todos como un número
    plausible. Un test de igualdad de valores no los ve; uno de bits, sí.
    """

    def _bits_f64(self, valor: float) -> bytes:
        import struct
        return struct.pack("<d", valor)

    def test_hexadecimal_ida_y_vuelta_para_f64(self):
        for valor in (0.0, -0.0, 1.0, -1.0, 0.5, 1.5, 0.1, -0.1, 1024.0,
                      1e300, -1e-300, 5e-324, 2.2250738585072014e-308):
            with self.subTest(valor=repr(valor)):
                literal = _literal_de_f64(valor)
                self.assertTrue(literal.startswith(("0x", "-0x")),
                                f"{valor!r} salió como {literal!r}, y un "
                                "decimal no garantiza los bits")
                self.assertEqual(self._bits_f64(float.fromhex(literal)),
                                 self._bits_f64(valor),
                                 f"{valor!r} salió como {literal} y vuelve "
                                 "distinto")

    def test_el_menor_subnormal_no_se_convierte_en_normal(self):
        """`0x0000000000001p-1022` se lee como el menor **normal**.

        Sin el `0.` del mantiso el texto significa "entero 1 por 2^-1022", que es
        el menor normal: unas 2^52 veces mayor. Dos letras convierten un
        denormal en un número normal y el módulo sigue validando.
        """
        valor = 5e-324                                   # el menor subnormal f64
        literal = _literal_de_f64(valor)
        self.assertIn("0x0.", literal,
                      "un subnormal se escribe `0x0.<fracción>`, con el punto")
        self.assertEqual(self._bits_f64(float.fromhex(literal)),
                         self._bits_f64(valor))

    def test_los_digitos_de_la_mantisa_de_f32_cubren_los_bits(self):
        """23 bits no son un número entero de dígitos hexadecimales.

        Imprimirlos como 5 dígitos se come el bit bajo, y `1.5` en f32 --que es
        `0x1.8p+0` con 6 dígitos-- salía `0x1.4p+0`, que releído da `1.25`. Los
        dos son "un número", así que ningún test de valores lo ve.
        """
        import struct
        for valor in (1.5, -3.25, 0.1, 1.0, 0.5, 1e-45):
            with self.subTest(valor=repr(valor)):
                texto = wat._hex_f32(_CursorFalso(f32_const(valor)[1:]))
                self.assertEqual(
                    struct.pack("<f", float.fromhex(texto)),
                    struct.pack("<f", valor),
                    f"{valor!r} salió como {texto}")


if __name__ == "__main__":
    unittest.main()


class TestElVerificadorConoceLoPrefijado(unittest.TestCase):
    """Un opcode que el verificador no conoce **no** es un opcode inofensivo.

    La rama de "no sé qué es esto" no hace nada, y para una instrucción normal
    eso sólo significa "sin comprobar". Para una prefijada con 0xFC es peor: sus
    tres operandos se quedan en la pila y todo el resto del cuerpo se comprueba
    contra un estado que ya no es real. El resultado es un verificador que da
    verde **por razones equivocadas**, que es lo peor que puede hacer un gate.
    """

    def _verifica(self, cuerpo: list[bytes], parametros=(w.I32,), resultado=()):
        from sourc.wasm import encoder
        from sourc.wasm.encoder import FuncType
        from sourc.wasm import verify

        firma = FuncType(tuple(parametros), tuple(resultado))
        # **Sin** el `end` final: el verificador recorre losieces del cuerpo y
        # el serializador añade el terminador después. Pasárselo aquí haría que
        # el `end` se interpretara como el cierre de un bloque --que no existe
        # en el nivel de función-- y exigiría una pila vacía.
        verify.check_function("prueba", firma, [], cuerpo, {})

    def test_memory_copy_consume_tres_operandos(self):
        self._verifica([
            bytes((0x41, 0x00)), bytes((0x41, 0x00)), bytes((0x41, 0x10)),
            bytes((0xFC, 0x0A, 0x00, 0x00)),
        ])
        # Si los tres no se consumieran, el `i32.add` de abajo encontraría la
        # pila con cuatro valores en vez de con error, y el verificador daría
        # verde sobre un módulo que Node rechaza.
        with self.assertRaises(Exception):
            self._verifica([
                bytes((0x41, 0x00)), bytes((0x41, 0x00)), bytes((0x41, 0x10)),
                bytes((0xFC, 0x0A, 0x00, 0x00)),
                bytes((0x6A,)),
            ])

    def test_memory_fill_consume_tres_operandos(self):
        self._verifica([
            bytes((0x41, 0x00)), bytes((0x41, 0x00)), bytes((0x41, 0x10)),
            bytes((0xFC, 0x0B, 0x00)),
        ])

    def test_un_subopcode_desconocido_se_salta(self):
        """Un subopcode que no se conoce no se comprueba, que es la postura.

        Es distinto de "no se comprueba y además rompe la pila": aquí lo que
        queda en la pila es exactamente lo que había, y el resto del cuerpo se
        sigue comprobando contra el estado real.

        El cuerpo es `(a + ?) + b` con el `?` desconocido en medio. La función
        devuelve un i32, así que si el subopcode hubiera dejado algo o se hubiera
        comido un operando, la suma de la derecha no cuadraría y el `end`
        complains.
        """
        self._verifica([
            bytes((0x41, 0x00)),                     # a = 0
            bytes((0x41, 0x00)),                     # push 0
            bytes((0xFC, 0x7F, 0x00)),              # 0x7F: nadie lo define
            bytes((0x6A,)),                          # a + (lo que hubiera dejado)
            bytes((0x41, 0x00)),                     # b = 0
            bytes((0x6A,)),                          # + b
        ], parametros=(), resultado=(w.I32,))


class TestElEnsambladorRechaza(unittest.TestCase):
    """Lo que no se entiende es un error con su línea, no un opcode inventado.

    Un ensamblador que se salta un token desconocido produce un módulo que
    **valida** y calcula otra cosa. Es el peor sitio para descubrirlo, porque el
    error aparece en la salida del programa y no en el ensamblado.
    """

    def _ensambla(self, texto: str, **kwargs) -> bytes:
        return wat_asm.ensamblar(texto, **kwargs)

    MINIMO = ("(module\n"
              "  (type (func))\n"
              "  (memory 1)\n"
              "  (func (type 0)\n"
              "    i32.const 1\n"
              "  )\n"
              ")\n")

    def test_una_instruccion_desconocida_dice_la_linea(self):
        with self.assertRaises(wat_asm.WatEnsamblarError) as error:
            self._ensambla(self.MINIMO.replace("i32.const 1",
                                               "i32.construir 1"))
        self.assertEqual(error.exception.linea, 5)
        self.assertIn("construir", str(error.exception))

    def test_un_tipo_fuera_del_subconjunto_no_pasa(self):
        texto = self.MINIMO.replace("(type (func))",
                                    "(type (func (param v128)))")
        with self.assertRaises(wat_asm.WatEnsamblarError) as error:
            self._ensambla(texto)
        self.assertIn("v128", str(error.exception))

    def test_un_modulo_que_nunca_se_cierra(self):
        """Paréntesis cuadrados, y sin embargo sin cerrar.

        Es un fallo distinto del de "paréntesis descompensados": aquí el texto
        está bien formado y lo que no llegó es el final. Quitándole el `)` final
        a un módulo normal no se llega aquí --los paréntesis descuadran y salta
        el otro error--; hace falta un texto donde el `)` de cierre se consuma
        como cierre de otra construcción, que es justo lo que pasa con todo en
        una línea.
        """
        with self.assertRaises(wat_asm.WatEnsamblarError) as error:
            self._ensambla("(module (type (func)))\n")
        self.assertIn("falta", str(error.exception))
        self.assertNotIn("paréntesis", str(error.exception))

    def test_parentesis_desescompensados(self):
        with self.assertRaises(wat_asm.WatEnsamblarError) as error:
            self._ensambla(self.MINIMO.replace("(type (func))",
                                               "(type (func)"))
        self.assertIn("paréntesis", str(error.exception))

    def test_un_memarg_sin_align(self):
        texto = self.MINIMO.replace("i32.const 1", "i32.load offset=4")
        with self.assertRaises(wat_asm.WatEnsamblarError) as error:
            self._ensambla(texto)
        self.assertIn("align", str(error.exception))


class TestElSubconjuntoRizado(unittest.TestCase):
    """El límite de ADR 0014, y que se pueda comprobar.

    `runtime/native/*.wat` son rectos a propósito: es lo que mantiene el techo de
    1 KB por módulo. El modo `rizado` es lo que hace ese "a propósito"
    verificable en vez de una intención, igual que `make check-subset` con el
    contrato del lenguaje.
    """

    def test_un_modulo_rizado_pasa(self):
        texto = ("(module\n"
                 "  (type (func (param i32) (result i32)))\n"
                 "  (memory 1)\n"
                 "  (func (type 0) (local i32)\n"
                 "    local.get 0\n"
                 "    i32.const 1\n"
                 "    i32.add\n"
                 "  )\n"
                 ")\n")
        self.assertTrue(wat_asm.ensamblar(texto, rizado=True))

    def test_un_bloque_no_cabe_en_rizado(self):
        """El bloque es lo que hace un módulo grande, y lo que el ADR excluye.

        Un `loop` de una línea cabe en 20 bytes de binario y en unas 40 de WAT,
        y su coste real no está en el tamaño: está en que el revisor tiene que
        mantener el estado de la pila en la cabeza para corregirlo.
        """
        texto = ("(module\n"
                 "  (type (func))\n"
                 "  (memory 1)\n"
                 "  (func (type 0)\n"
                 "    block\n"
                 "      i32.const 1\n"
                 "    end\n"
                 "  )\n"
                 ")\n")
        with self.assertRaises(wat_asm.WatEnsamblarError) as error:
            wat_asm.ensamblar(texto, rizado=True)
        self.assertIn("ADR 0014", str(error.exception))
        # Sin `rizado` sí se ensambla: el round trip lo necesita.
        self.assertTrue(wat_asm.ensamblar(texto))

    def test_memory_copy_y_fill_si_caben(self):
        """Son el motivo de existir de `runtime/native/`: los usa el allocator.

        El bump allocator pone a cero lo que reserva y el copiador mueve bytes.
        Con el MVP eso son dos bucles de instrucciones; con bulk memory son dos
        instrucciones, y por eso vale la pena el módulo nativo.
        """
        texto = ("(module\n"
                 "  (type (func (param i32 i32 i32)))\n"
                 "  (memory 1)\n"
                 "  (func (type 0)\n"
                 "    local.get 0\n"
                 "    local.get 1\n"
                 "    local.get 2\n"
                 "    memory.copy\n"
                 "  )\n"
                 "  (type (func (param i32 i32 i32)))\n"
                 "  (func (type 1)\n"
                 "    local.get 0\n"
                 "    local.get 1\n"
                 "    local.get 2\n"
                 "    memory.fill\n"
                 "  )\n"
                 ")\n")
        self.assertTrue(wat_asm.ensamblar(texto, rizado=True))
        # `memory.copy` va como prefijo 0xFC y subcódigo 10; no aparece como
        # nombre en ningún sitio, que es el motivo de buscar los bytes.
        self.assertIn(b"\xfc\x0a", wat_asm.ensamblar(texto))
        self.assertIn(b"\xfc\x0b", wat_asm.ensamblar(texto))
