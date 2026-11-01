"""El backend: del SIR a un módulo que ejecuta lo mismo que el intérprete.

El encoder ya tenía sus propios tests. Éstos son de otra especie: no comprueban
bytes sino *comportamiento*. Un backend puede producir un módulo perfectamente
válido que calcula otra cosa, y ningún test de estructura lo detecta; sólo
comparar la salida del intérprete con la del módulo lo detecta.

El circuito mínimo es `PRINT doble(21)`. Todo lo que se puede verificar sobre él
se verifica aquí antes de bajar la siguiente tanda de operaciones.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sourc import interp, sir
from sourc.diagnostics import CompileError, DiagnosticBag
from sourc.parser import parse
from sourc.types import check
from sourc.wasm import encoder as w
from sourc.wasm.backend import HOST_SIGNATURES, WasmBackend, compile_module

NODE = shutil.which("node")
ROOT_COMPILER = Path(__file__).resolve().parents[1] / "compiler"
ROOT_TESTS = Path(__file__).resolve().parent


# El glue mínimo: implementa lo justo para ejecutar el circuito. Es el mismo
# contrato que el glue real de la Fase 2, y por eso los tests que lo usan están
# midiendo el ABI y no el backend.
# El pegamento de los tests es **el de verdad**, no una copia. Una copia se
# queda obsoleta --ya lo estaba: `print_arr` seguía con la firma de tres
# argumentos-- y entonces los tests pasan con un ABI que el navegador no
# soporta, que es el peor sitio para descubrirlo.
RUN_SCRIPT = r"""
import { readFileSync } from "node:fs";
import { ejecutar } from "RAIZ/runtime/glue/glue_core.js";
const bytes = readFileSync(process.argv[2]);
const { host, error } = await ejecutar(bytes, { frames: 1 });
if (error) {
  process.stderr.write(String(error.message || error));
  process.exit(1);
}
process.stdout.write(host.salida);
"""


def build_module(source: str) -> sir.Module:
    bag = DiagnosticBag()
    program = parse(source, bag)
    self_check(bag)
    typing = check(program, bag)
    self_check(bag)
    return sir.build(program, typing)


def self_check(bag: DiagnosticBag) -> None:
    if bag.has_errors():
        raise AssertionError("el programa de prueba no compila: "
                             + "; ".join(d.message for d in bag.diagnostics))


def run_in_node(module_bytes: bytes, timeout: int = 60) -> str:
    """Ejecuta el módulo en Node con el glue mínimo y devuelve lo impreso."""
    with tempfile.NamedTemporaryFile(suffix=".wasm", delete=False) as handle:
        handle.write(module_bytes)
        path = handle.name
    script_path = path + ".mjs"
    try:
        # El script vive en un temporal, así que el import del pegamento tiene
        # que ser absoluto: `RAIZ` se sustituye por la raíz del repositorio.
        Path(script_path).write_text(
            RUN_SCRIPT.replace("RAIZ", str(Path(__file__).resolve().parents[1])),
            encoding="utf-8")
        result = subprocess.run([NODE, script_path, path], capture_output=True,
                                text=True, timeout=timeout)
        if result.returncode != 0:
            raise AssertionError(f"node falló: {result.stderr[:400]}")
        return result.stdout
    finally:
        for leftover in (path, script_path):
            try:
                os.unlink(leftover)
            except OSError:
                pass


CIRCUITO = ('FUNCTION doble(n AS INTEGER) AS INTEGER\n'
            '  RETURN n * 2\n'
            'END FUNCTION\n'
            '\n'
            'PRINT doble(21)\n')


@unittest.skipUnless(NODE, "node no está instalado")
class TestCircuitoMinimo(unittest.TestCase):
    """Fuente → IR → WASM → Node. El circuito completo, sin recortes."""

    def test_el_modulo_valida(self):
        w.validate(compile_module(build_module(CIRCUITO)))

    def test_imprime_42_en_wasm(self):
        self.assertEqual(run_in_node(compile_module(build_module(CIRCUITO))),
                         "42\n")

    def test_el_interprete_imprime_lo_mismo(self):
        """El módulo no es una implementación distinta: es la misma.

        Comparar las dos salidas es lo que detecta un módulo que valida y
        calcula otra cosa, que ningún test de estructura ve.
        """
        from sourc import interp

        source = build_module(CIRCUITO)
        expected = interp.run(source).output
        self.assertEqual(run_in_node(compile_module(source)), expected)


class TestImportes(unittest.TestCase):
    """Los imports fijan los índices de función, así que su orden es sacred."""

    def test_los_imports_vienen_en_orden_alfabetico(self):
        source = ('PRINT 1\n'
                  'PRINT "a"\n'
                  'PRINT 1 > 2\n')
        module = build_module(source)
        backend = WasmBackend(module)
        self.assertEqual(sorted(backend.host_indices),
                         list(backend.host_indices))

    def test_toda_firma_usada_existe(self):
        """Cada import del IR tiene firma, o el módulo no valida.

        Es el fallo más caro del backend: sin firma no hay módulo, y el error
        aparece al compilar y no al ejecutar.
        """
        module = build_module('PRINT "hola"\n')
        for name in module.host_imports:
            self.assertIn(name, HOST_SIGNATURES)

    def test_sin_main_no_hay_modulo(self):
        module = build_module(CIRCUITO)
        module.funcs = [f for f in module.funcs if f.name != "main"]
        with self.assertRaises(CompileError) as caught:
            WasmBackend(module).build()
        self.assertIn("main", caught.exception.diagnostic.message)


class TestInvariantesDeLocales(unittest.TestCase):
    """Los parámetros de WASM ocupan los índices 0..n-1. Si se desalinean, el
    módulo sigue validando y cada función lee el argumento de otra."""

    def test_parametros_son_los_primeros_slots(self):
        module = build_module(CIRCUITO)
        doble = next(f for f in module.funcs if f.name == "doble")
        self.assertEqual(doble.locals[:len(doble.params)], doble.params)

    def test_un_slot_antes_de_los_parametros_se_rechaza(self):
        module = build_module(CIRCUITO)
        doble = next(f for f in module.funcs if f.name == "doble")
        doble.locals.insert(0, ("temporal", doble.locals[0][1]))
        with self.assertRaises(CompileError) as caught:
            WasmBackend(module).build()
        self.assertIn("parámetros", caught.exception.diagnostic.message)


class TestOperacionesSinBajar(unittest.TestCase):
    """Una operación declarada y no emitida es trabajo pendiente, no un bug.

    El error lo dice en el nombre de la operación y en dónde se arregla, para
    que un fallo aquí sea una lista de trabajo y no una avería.
    """

    def test_el_error_nombra_la_operacion(self):
        # Se inyecta una operación que el frontend no emite nunca, en vez de
        # depender de que exista alguna sin bajar: en cuanto se implementa una,
        # el test empezaría a fallar sin que cambie nada.
        module = build_module('PRINT "hola"\n')
        modulo = module.funcs[0]
        intr = sir.Instr(op="operacion.inventada", args=())
        modulo.instrs.insert(0, intr)
        with self.assertRaises(CompileError) as caught:
            compile_module(module)
        diagnostic = caught.exception.diagnostic
        # El mensaje nombra lo que falta y el arreglo dice dónde escribirlo: un
        # fallo aquí tiene que ser una lista de trabajo, no una avería.
        self.assertIn("todavía no baja", diagnostic.message)
        self.assertIn("'", diagnostic.message)
        self.assertIn("backend.py", diagnostic.suggestion)
        self.assertIn("tests/test_wasm_backend.py", diagnostic.suggestion)


@unittest.skipUnless(NODE, "node no está instalado")
class TestPrintDeCadaTipo(unittest.TestCase):
    """Cada tipo que `PRINT` sabe escribir, por su camino entero."""

    def assertPrints(self, source: str, expected: str) -> None:
        self.assertEqual(run_in_node(compile_module(build_module(source))),
                         expected)

    def test_entero(self):
        self.assertPrints('PRINT 42\n', "42\n")

    def test_texto(self):
        self.assertPrints('PRINT "hola"\n', "hola\n")

    def test_dos_argumentos_con_punto_y_coma(self):
        self.assertPrints('PRINT 1 ; 2\n', "12\n")

    def test_dos_argumentos_sin_punto_y_coma(self):
        self.assertPrints('PRINT 1, 2\n', "1\n2\n")

@unittest.skipUnless(NODE, "node no está instalado")
class TestArrays(unittest.TestCase):
    """Un array son tres palabras y sus elementos viven en memoria lineal."""

    def assertPrints(self, source: str, expected: str) -> None:
        self.assertEqual(run_in_node(compile_module(build_module(source))),
                         expected)

    def test_lectura_y_escritura(self):
        self.assertPrints(
            'DIM t AS ARRAY OF INTEGER = NEW INTEGER[4]\n'
            't[0] = 10\n'
            't[3] = 40\n'
            'PRINT t[0]; t[3]\n', "1040\n")

    def test_longitud(self):
        self.assertPrints(
            'DIM t AS ARRAY OF INTEGER = NEW INTEGER[4]\nPRINT LEN(t)\n',
            "4\n")

    def test_elemento_inicializado_a_cero(self):
        self.assertPrints(
            'DIM t AS ARRAY OF INTEGER = NEW INTEGER[3]\nPRINT t[1]\n',
            "0\n")

    def test_los_valores_no_se_pisan(self):
        """Dos arrays seguidos, cada uno con su memoria.

        Es el fallo del bump allocator mal alineado: si el segundo empieza
        encima del primero, los valores se leen de la fila equivocada.
        """
        self.assertPrints(
            'DIM a AS ARRAY OF INTEGER = NEW INTEGER[3]\n'
            'DIM b AS ARRAY OF INTEGER = NEW INTEGER[3]\n'
            'a[0] = 111\n'
            'a[2] = 113\n'
            'b[0] = 222\n'
            'b[2] = 224\n'
            'PRINT a[0]; a[2]; b[0]; b[2]\n', "111113222224\n")

    def test_singles_ocupan_cuatro_bytes(self):
        """Un SINGLE es f32 de verdad: si se guardara como i64, el siguiente
        elemento quedaría a mitad del anterior."""
        self.assertPrints(
            'DIM a AS ARRAY OF SINGLE = NEW SINGLE[4]\n'
            'a[0] = 1.5\n'
            'a[3] = 2.5\n'
            'PRINT a[0]; " "; a[3]\n', "1.5 2.5\n")

    def test_los_datos_estaticos_no_se_pisan_con_el_heap(self):
        """El heap empieza después del último dato, nunca dentro."""
        self.assertPrints(
            'DIM t AS ARRAY OF INTEGER = NEW INTEGER[8]\n'
            't[0] = 7\n'
            'PRINT "fin"; t[0]\n', "fin7\n")

    def test_un_indice_fuera_de_rango_trampa(self):
        """Como el intérprete: no es un error de compilación, es una trampa.

        Un módulo que no comprueba lo que sale no calcula otra cosa: escribe en
        la memoria que sigue al array y corrompe el heap sin avisar.
        """
        with self.assertRaises(Exception):
            self.assertPrints(
                'DIM t AS ARRAY OF INTEGER = NEW INTEGER[2]\n'
                't[5] = 1\nPRINT "no"\n', "no\n")

    def test_una_lista_literal(self):
        self.assertPrints('DIM t AS ARRAY OF INTEGER = [1, 2, 3]\n'
                          'PRINT t[0]; t[2]\n', "13\n")


@unittest.skipUnless(NODE, "node no está instalado")
class TestAllocator(unittest.TestCase):
    """`@__bump` es una función del propio módulo, no del glue."""

    def test_solo_existe_si_hay_arrays(self):
        """Un programa sin arrays no paga el allocator.

        La mecánica de pesos: lo que no se usa no se descarga, ni se pesa.
        """
        def nombres(source: str) -> list:
            backend = WasmBackend(build_module(source))
            backend.build()
            return [body.name for body in backend.builder.funcs]

        self.assertNotIn("__bump", nombres('PRINT "hola"\n'))
        self.assertIn("__bump", nombres(
            'DIM t AS ARRAY OF INTEGER = NEW INTEGER[2]\nPRINT t[0]\n'))

    def test_crece_la_memoria_cuando_no_cabe(self):
        """Un array que no cabe en una página obliga a `memory.grow`."""
        source = ('DIM t AS ARRAY OF INTEGER = NEW INTEGER[100000]\n'
                  't[99999] = 42\nPRINT t[99999]\n')
        self.assertEqual(run_in_node(compile_module(build_module(source))),
                         "42\n")

    def test_dos_reservas_no_se_solapan(self):
        source = ('DIM a AS ARRAY OF INTEGER = NEW INTEGER[16]\n'
                  'DIM b AS ARRAY OF SINGLE = NEW SINGLE[16]\n'
                  'a[15] = 9\nb[15] = 0.5\n'
                  'PRINT a[15]; " "; b[15]\n')
        self.assertEqual(run_in_node(compile_module(build_module(source))),
                         "9 0.5\n")

@unittest.skipUnless(NODE, "node no está instalado")
class TestImpresionDeArrays(unittest.TestCase):
    """`PRINT` de un array es una decisión del lenguaje, no un `str` de Python.

    El intérprete sacaba `str(lista)`, que imprimía `[True, False]` y
    `['uno']`. Eso es un accidente del oráculo, no una especificación, así que
    los dos lados imprimen notación del lenguaje: `[1, 2]`, `["uno"]`.
    """

    def _ambos(self, source):
        return (interp.run(build_module(source)).output,
                run_in_node(compile_module(build_module(source))))

    def test_enteros(self):
        source = ('DIM a AS ARRAY OF INTEGER = NEW INTEGER[3]\n'
                  'a[0] = 1\na[2] = 7\nPRINT a\n')
        self.assertEqual(self._ambos(source), ("[1, 0, 7]\n", "[1, 0, 7]\n"))

    def test_textos_entre_comillas(self):
        """Un texto dentro va entre comillas, como se escribe en el fuente."""
        source = 'DIM a AS ARRAY OF STRING = ["uno", "dos"]\nPRINT a\n'
        self.assertEqual(self._ambos(source),
                         ('["uno", "dos"]\n', '["uno", "dos"]\n'))

    def test_vacia(self):
        self.assertEqual(self._ambos("DIM a AS ARRAY OF INTEGER = "
                                     "NEW INTEGER[0]\nPRINT a\n"),
                         ("[]\n", "[]\n"))

    def test_anidado(self):
        """Un array de arrays: el host necesita el tipo de cada elemento."""
        source = ('DIM m AS ARRAY OF ARRAY OF INTEGER = [ [1, 2], [3] ]\n'
                  'PRINT m\nm[1][0] = 9\nPRINT m\nPRINT m[0][1]\n')
        self.assertEqual(self._ambos(source),
                         ("[[1, 2], [3]]\n[[1, 2], [9]]\n2\n",
                          "[[1, 2], [3]]\n[[1, 2], [9]]\n2\n"))

    def test_anidado_de_textos(self):
        """El tipo del array interior viaja en su descriptor, no se adivina."""
        source = 'DIM m AS ARRAY OF ARRAY OF STRING = [ ["a"], ["b", "c"] ]\nPRINT m\n'
        self.assertEqual(self._ambos(source),
                         ('[["a"], ["b", "c"]]\n', '[["a"], ["b", "c"]]\n'))

    def test_anidado_de_reales(self):
        source = 'DIM m AS ARRAY OF ARRAY OF SINGLE = [ [1.5], [2.0] ]\nPRINT m\n'
        self.assertEqual(self._ambos(source), ("[[1.5], [2]]\n", "[[1.5], [2]]\n"))


@unittest.skipUnless(NODE, "node no está instalado")
class TestCompararTextos(unittest.TestCase):
    """`COMPARE` es la de tres vías de BASIC.

    Los casos que importan son los que sólo difieren en la longitud o en un byte
    del final: el recorrido byte a byte termina antes y el resultado lo decide el
    prefijo, no los datos.
    """

    def _ambos(self, left, right):
        source = f'PRINT COMPARE("{left}", "{right}")\n'
        return (interp.run(build_module(source)).output,
                run_in_node(compile_module(build_module(source))))

    def test_primera_letra(self):
        self.assertEqual(self._ambos("hola", "adios"), ("1\n", "1\n"))
        self.assertEqual(self._ambos("adios", "hola"), ("-1\n", "-1\n"))

    def test_prefijo_mas_corto(self):
        """`"ab"` es prefijo de `"abc"`: gana el más largo, no el que tiene más
        bytes distintos."""
        self.assertEqual(self._ambos("ab", "abc"), ("-1\n", "-1\n"))
        self.assertEqual(self._ambos("abc", "ab"), ("1\n", "1\n"))

    def test_ultimo_byte(self):
        # "hola" y "hole" sólo difieren en el cuarto byte: 'a' (97) contra 'e'
        # (101). El recorrido entero no se corta antes, así que aquí decide el
        # byte y no la longitud.
        self.assertEqual(self._ambos("hola", "hole"), ("-1\n", "-1\n"))
        self.assertEqual(self._ambos("hole", "hola"), ("1\n", "1\n"))

    def test_iguales(self):
        self.assertEqual(self._ambos("hola", "hola"), ("0\n", "0\n"))

    def test_vacio(self):
        self.assertEqual(self._ambos("", "a"), ("-1\n", "-1\n"))
        self.assertEqual(self._ambos("a", ""), ("1\n", "1\n"))
        self.assertEqual(self._ambos("", ""), ("0\n", "0\n"))
class TestVerificadorDePila(unittest.TestCase):
    """El verificador es lo único entre el backend y el error de Node.

    Si él mira mal una instrucción, el error sale o como un rechazo de Node
    que no señala la causa, o --peor-- como un módulo que valida y hace otra
    cosa. Los dos fallos de aquí son de eso: un índice mal leído y un
    bloque sin cerrar.
    """

    def _cuerpo(self, source, name="main"):
        backend = WasmBackend(build_module(source))
        backend.compile()
        return next(f for f in backend.builder.funcs if f.name == name)

    def _firmas(self, backend):
        return backend._signatures()

    def test_indice_de_local_por_de_encima_de_127(self):
        """A partir del local 128 el índice va en LEB128, no en un byte.

        Leer el primer byte crudo pasaba el local 384 por el 128, y el
        verificador daba por buena una función con 128 locales o más --que es
        donde empiezan los programas que ya no son de juguete.
        """
        # 40 variables locales y 300 valores: el objetivo acaba muy por encima
        # de 127 y el índice real necesita dos bytes.
        source = "".join(f"DIM v{n} AS DOUBLE = {n}.5\n" for n in range(40))
        source += "".join(f"v{n} = v{n} + 0.25\n" for n in range(40))
        backend = WasmBackend(build_module(source))
        backend.compile()
        cuerpo = next(f for f in backend.builder.funcs if f.name == "main")
        indices = []
        for piece in cuerpo.pieces:
            if piece[0] in (0x20, 0x21, 0x22):
                valor, shift, i = 0, 0, 1
                while True:
                    byte = piece[i]
                    valor |= (byte & 0x7F) << shift
                    if not byte & 0x80:
                        break
                    shift += 7
                    i += 1
                indices.append(valor)
        self.assertGreater(max(indices), 127,
                           "el caso no llega al rango que queria probar")
        # Y el verificador tiene que estar de acuerdo con el módulo entero.
        backend.verify()

    def test_bloque_sin_cerrar_se_rechaza(self):
        """Un `end` de menos es el error que Node llama "must end with end"."""
        from sourc.wasm import verify as verificador
        from sourc.wasm.encoder import FuncType

        # `block` sin su `end`: el `end` que falta es justo lo que Node
        # complains de con "function body must end with end".
        cuerpo = [b"\x41\x00", b"\x02\x40"]
        with self.assertRaises(verificador.SignatureError) as error:
            verificador.check_function("main", FuncType((), ()), [], cuerpo, {})
        self.assertIn("sin cerrar", str(error.exception))

    def test_bloque_cerrado_no_se_rechaza(self):
        from sourc.wasm import verify as verificador
        from sourc.wasm.encoder import FuncType

        cuerpo = [b"\x41\x00", b"\x02\x40", b"\x0b", b"\x1a"]  # block + end + drop
        verificador.check_function("main", FuncType((), ()), [], cuerpo, {})


class TestReproducibilidad(unittest.TestCase):
    """Mismo fuente, mismos bytes.

    Es un requisito del bootstrap (ADR 0016): el compilador auto-hospedado
    tiene que producir el mismo `main.wasm` que el de Python, y sólo se puede
    comparar byte a byte si el de Python es determinista. Un artefacto que varía
    entre ejecuciones hace imposible la comparación y convierte el bootstrap en
    una lotería.

    Lo que se comprueba aquí es lo barato y lo que más muerde: dos compilaciones
    del mismo módulo, y el módulo entero con distintos programas, no comparten
    ni un índice. Un `dict` recorrido en orden de inserción o un `set` recorrido
    en orden de hash escapes en este test.
    """

    FUENTE = ('DIM t AS ARRAY OF INTEGER = NEW INTEGER[4]\n'
              'FOR i = 0 TO LEN(t) - 1\n'
              '  t[i] = i * i\n'
              'NEXT\n'
              'PRINT t\n'
              'PRINT "fin"\n')

    def test_dos_compilaciones_dan_los_mismos_bytes(self):
        bytes_ = compile_module(build_module(self.FUENTE))
        for _ in range(5):
            self.assertEqual(compile_module(build_module(self.FUENTE)), bytes_)

    def test_un_programa_determinista_da_los_mismos_bytes(self):
        """`hello world`: el caso mínimo, que es donde se nota un índice mal
        apartado porque no hay nada más que lo tape."""
        for _ in range(5):
            self.assertEqual(
                compile_module(build_module('PRINT "hola mundo"\n')),
                compile_module(build_module('PRINT "hola mundo"\n')))

    def test_el_orden_de_los_programas_no_altera_los_bytes(self):
        """Compilar A y luego B tiene que dar exactamente lo mismo que B solo.

        Los índices de función, de tipo y de import se asignan por orden de
        declaración, y ese orden depende de qué se ha compilado antes si algo
        guarda estado entre módulos. Con un `set` de imports sin ordenar, el
        orden de importación --y con él el binario-- dependería del **hash**,
        que cambia entre procesos.
        """
        uno = 'PRINT "uno"\nPRINT 1\n'
        dos = 'DIM a AS ARRAY OF SINGLE = NEW SINGLE[2]\na[0] = 1.5\nPRINT a\n'

        solo = compile_module(build_module(dos))
        compile_module(build_module(uno))
        despues = compile_module(build_module(dos))
        self.assertEqual(solo, despues,
                         "compilar otro programa antes cambió los bytes")

    def test_la_reproducibilidad_sobrevive_a_otro_proceso(self):
        """Dos procesos, porque el hash de un `set` sólo cambia entre procesos.

        Es el caso que el ratchet de paridad no cubre: dentro de un proceso todo
        es determinista aunque se recorra un `set`, y el bug sale al compilar
        desde la línea de órdenes.
        """
        import subprocess
        import sys
        script = (
            "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r);"
            "import test_wasm_backend as T;"
            "import hashlib;"
            "b = T.compile_module(T.build_module(%r));"
            "print(hashlib.sha256(b).hexdigest())"
            % (str(ROOT_COMPILER), str(ROOT_TESTS), self.FUENTE))
        digests = set()
        for semilla in ("0", "1", "12345"):
            import os
            entorno = dict(os.environ, PYTHONHASHSEED=semilla)
            salida = subprocess.run([sys.executable, "-c", script],
                                    capture_output=True, text=True,
                                    env=entorno, timeout=120)
            self.assertEqual(salida.returncode, 0,
                             f"subproceso fallo: {salida.stderr[:300]}")
            digests.add(salida.stdout.strip())
        self.assertEqual(len(digests), 1,
                         f"el mismo fuente dio {len(digests)} hashes distintos "
                         "según PYTHONHASHSEED: hay un orden que depende del "
                         "hash de un dict o de un set")


if __name__ == "__main__":
    unittest.main()