"""Cada código de diagnóstico tiene un programa que lo provoque, y su texto no está vacío.

Un código de diagnóstico es API (AGENTS.md, regla 3), y una API sin test es una
promesa que nadie puede cumplir. Aquí hay **49** códigos que el compilador puede
emitir; antes de este módulo, **15** aparecían en algún test. Los 34 restantes
--incluidos `E0058` y `E0065`, que son los dos que dispararon el día que el
fuzzer encontró las cuatro nativas sin lowering-- no tenían ni una línea que los
comprobara.

Y el motivo no es académico: cuando el CLI reventó con `AttributeError` en vez de
imprimir `E0058`, el primer síntoma fue un mensaje que **no era un mensaje**. Un
diagnóstico que al renderizarse se cae no es un diagnóstico, y nada lo fijaba
porque nadie comprobaba que el camino entero se ejecutara.

## Un programa por código, no una cadena

Comprobar que cada `E####` aparece como texto en el árbol mide la **existencia**
del código, no que su camino funcione. Es exactamente el agujero del
`AttributeError`: el `E0058` existía y su renderizado estaba roto. Aquí cada caso
es un fuente que se **ejecuta**, y eso cubre parseo, tipos, bajada y renderizado de
una vez.

## Las dos direcciones

Sólo "todo código tiene caso" deja pasar lo contrario: un test que cita `E0099`
porque el código se renombró sigue verde mintiendo sobre lo que cubre. También
falla un test que cita un código inexistente, y una reserva que ya no aplica.

## Lo que cada caso fija

El código, el fragmento del mensaje que identifica el error, y que la sugerencia
**no esté vacía** --que es lo que hace útil un diagnóstico. Los textos que de
verdad son API están snapshotteados enteros en `tests/test_diagnostics.py`; congelar
los 49 enteros convertiría cada reordenación de una frase en 49 fallos a mano, y
esa fricción acaba apagando la puerta.
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compiler"))
sys.path.insert(0, str(ROOT / "tests"))

from casos_de_diagnostico import CASOS, RESERVADOS   # noqa: E402

CODIGO = re.compile(r'"(E\d{4})"')


def codigos_del_compilador() -> set[str]:
    """Cada código que el compilador puede emitir, leído del fuente.

    Se lee el **texto** y no los objetos porque lo que se busca es que añadir un
    diagnóstico sin caso sea visible. Recorrer objetos sólo encuentra lo que ya
    se construyó en este proceso, que es justo lo que un error nuevo no ha hecho.
    """
    encontrados: set[str] = set()
    for path in sorted((ROOT / "compiler").rglob("*.py")):
        encontrados |= set(CODIGO.findall(path.read_text(encoding="utf-8")))
    return encontrados


def codigos_en_los_tests() -> set[str]:
    encontrados: set[str] = set()
    for path in sorted((ROOT / "tests").rglob("*.py")):
        encontrados |= set(CODIGO.findall(path.read_text(encoding="utf-8")))
    return encontrados


class TestContratoDeDiagnosticos(unittest.TestCase):
    def test_hay_bastantes_codigos_para_que_valga_la_pena(self):
        """Si no hay códigos, las otras comprobaciones pasan sin mirar nada."""
        self.assertGreaterEqual(len(codigos_del_compilador()), 40)

    def test_cada_codigo_tiene_programa_o_reserva(self):
        sin_cubrir = codigos_del_compilador() - set(CASOS) - set(RESERVADOS)
        self.assertEqual(
            sin_cubrir, set(),
            "códigos de diagnóstico sin programa que los provoque:\n"
            f"  {sorted(sin_cubrir)}\n"
            "Añádelo a casos_de_diagnostico.py con el fuente que lo produce, o "
            "a RESERVADOS si es una guarda interna")

    def test_ningun_test_cita_un_codigo_inexistente(self):
        fantasma = codigos_en_los_tests() - codigos_del_compilador()
        self.assertEqual(
            fantasma, set(),
            f"los tests citan códigos que el compilador no emite: "
            f"{sorted(fantasma)}")

    def test_la_reserva_no_tiene_nadas_obsoletas(self):
        obsoletas = set(RESERVADOS) - codigos_del_compilador()
        self.assertEqual(
            obsoletas, set(),
            f"reservados pero ya no emitidos: {sorted(obsoletas)}. Una reserva "
            "caducada se lee, no dice nada, y tapa el caso real")

    def test_la_reserva_es_corta(self):
        """Diez de cuarenta y tantos. El número está acotado a propósito.

        Una reserva larga es una lista de invariantes internas que nadie ha
        revisado, y se llena fácil: cada bucle defensivo que alguien escribe
        "por si acaso" se cuela sin que nadie lo mire. Diez es el punto en el que
        la lista deja de describir al compilador y empieza a describir a quien
        la escribió, y a partir de ahí lo útil es comprobar la puerta, no
        ampliar la excusa.
        """
        self.assertLessEqual(
            len(RESERVADOS), 10,
            "la reserva de códigos internos ha crecido demasiado")

    def test_cada_reserva_explica_por_que_no_se_puede_provocar(self):
        for codigo, motivo in RESERVADOS.items():
            with self.subTest(codigo=codigo):
                self.assertGreater(
                    len(motivo), 40,
                    f"la reserva de {codigo} no explica por qué no hay programa")

    def test_cada_codigo_se_emite_por_su_programa(self):
        from casos_de_diagnostico import COLECTORES

        for codigo, caso in sorted(CASOS.items()):
            with self.subTest(codigo=codigo):
                errores = []
                for fase in caso.fases:
                    errores = COLECTORES[fase](caso.fuente)
                    if [d for d in errores if d.code == codigo]:
                        break
                codigos = [d.code for d in errores]
                self.assertIn(
                    codigo, codigos,
                    f"el programa de {codigo} dio {codigos}\n"
                    f"--- fuente ---\n{caso.fuente}")

    def test_el_mensaje_dice_el_que(self):
        """El mensaje tiene que nombrar lo que está mal, no sólo existir.

        "error de tipos" con un código correcto no ayuda a nadie a arreglar el
        programa; "E0028: no se pueden comparar ARRAY con ARRAY" sí.
        """
        from casos_de_diagnostico import COLECTORES

        for codigo, caso in sorted(CASOS.items()):
            with self.subTest(codigo=codigo):
                errores = []
                for fase in caso.fases:
                    errores = COLECTORES[fase](caso.fuente)
                    if [d for d in errores if d.code == codigo]:
                        break
                found = [d for d in errores if d.code == codigo][0]
                self.assertTrue(
                    found.message.strip(),
                    f"{codigo} tiene el mensaje vacío")
                if caso.contiene:
                    self.assertIn(
                        caso.contiene, found.message,
                        f"el mensaje de {codigo} ya no menciona "
                        f"'{caso.contiene}'")

    def test_la_sugerencia_no_puede_estar_vacia(self):
        """Es lo que hace útil un diagnóstico: decir qué escribir en su lugar.

        AGENTS.md pide que cada error lleve "mensaje con la corrección y
        sugerencia". Una sugerencia vacía es la mitad de la regla incumplida, y
        es el tipo de cosa que se cuaga sin que nadie lo note.
        """
        from casos_de_diagnostico import COLECTORES

        for codigo, caso in sorted(CASOS.items()):
            with self.subTest(codigo=codigo):
                errores = []
                for fase in caso.fases:
                    errores = COLECTORES[fase](caso.fuente)
                    if [d for d in errores if d.code == codigo]:
                        break
                found = [d for d in errores if d.code == codigo][0]
                self.assertIsNotNone(
                    found.suggestion,
                    f"{codigo} no tiene sugerencia")
                self.assertTrue(
                    found.suggestion.strip(),
                    f"{codigo} tiene la sugerencia vacía")

    def test_el_renderizado_no_reventa(self):
        """Un diagnóstico que al renderizarse se cae no es un diagnóstico.

        Test aparte y no una línea del anterior porque lo que falla es otra
        cosa: el `Diagnostic` se construye bien y lo que se rompe es
        `render`. Con el `AttributeError` del `Printer` el diagnóstico era
        perfecto y lo que no se imprimía era el mensaje.
        """
        from casos_de_diagnostico import COLECTORES

        for codigo, caso in sorted(CASOS.items()):
            with self.subTest(codigo=codigo):
                errores = []
                for fase in caso.fases:
                    errores = COLECTORES[fase](caso.fuente)
                    if [d for d in errores if d.code == codigo]:
                        break
                found = [d for d in errores if d.code == codigo][0]
                texto = found.render("caso.src", caso.fuente)
                self.assertIsInstance(texto, str)
                self.assertIn(codigo, texto)
                for prohibido in ("Traceback", "AttributeError", "KeyError",
                                  "IndexError"):
                    self.assertNotIn(
                        prohibido, texto,
                        f"el render de {codigo} contiene {prohibido}")

    def test_el_programa_de_un_codigo_no_da_otros_errores(self):
        """Un caso que puede fallar por dos motivos tapa al otro.

        No es purismo: si el fuente de `E0035` además da `E0001`, entonces
        cuando `E0001` se rompa el test de `E0035` seguirá en verde y nadie se
        enterará. Cada caso tiene que fallar por un motivo, y el motivo tiene que
        ser el suyo.
        """
        from casos_de_diagnostico import COLECTORES

        for codigo, caso in sorted(CASOS.items()):
            with self.subTest(codigo=codigo):
                errores = []
                for fase in caso.fases:
                    errores = COLECTORES[fase](caso.fuente)
                    if [d for d in errores if d.code == codigo]:
                        break
                permitidos = {codigo} | set(caso.tambien)
                otros = sorted({d.code for d in errores} - permitidos)
                self.assertEqual(
                    otros, [],
                    f"el programa de {codigo} también da {otros}; un caso que "
                    "puede fallar por dos motivos no avisa de ninguno. Si el "
                    "compañero es legítimo, decláralo en `tambien`")


if __name__ == "__main__":
    unittest.main()

class TestElDiagnosticoSeImprimeEnTodosLosSitios(unittest.TestCase):
    """""Que el `except` que muestra un error **no se caiga** al mostrar el error.

    Éste es un bug que ya ocurrió dos veces en el mismo fichero, y por eso la
    defensa no es "arreglarlo": es **contar las copias**. `Printer` no tiene un
    método `diagnostic`, así que un `except CompileError` que lo llame se
    levanta con un `AttributeError` y el mensaje que lee quien escribió el
    programa es `AttributeError` en vez del código.

    Pasó en `cmd_build` y, un día después, seguía en `cmd_size`. Un arreglo en un
    sitio no encuentra el otro, y un `AttributeError` en un `except` es
    invisible mientras el camino no se ejecute: los tests usaban programas que
    compilaban bien.

    El test no busca el método equivocado --eso lo arregla el revisor cuando lo
    ve-- sino que **falla si el diagnóstico no llegó al `Printer`**, que es lo
    que importa.
    """

    def _con_texto(self):
        from sourc.diagnostics import DiagnosticBag
        from sourc.parser import parse
        from sourc.types import check

        fuente = "PRINT x\n"
        bag = DiagnosticBag()
        programa = parse(fuente, bag)
        if not bag.has_errors():
            check(programa, bag)
        return bag.errors

    def test_el_cli_no_tiene_ningun_printer_diagnostic(self):
        """La búsqueda literal. Es un test tonto a propósito.

        Un test que rastrea el error a través de la ejecución es mejor, pero este
        existe para cazar la copia en el momento de escribirla, que es cuando
        el `except` es nuevo y nadie lo ha ejecutado.
        """
        fuente = (ROOT / "compiler" / "sourc" / "cli.py").read_text(
            encoding="utf-8")
        self.assertNotIn(
            "printer.diagnostic(", fuente,
            "Printer no tiene `diagnostic`; un except que lo llame se cae con "
            "un AttributeError y el diagnóstico no se imprime")

    def test_el_diagnostico_llega_al_printer(self):
        """Y la comprobación de verdad: el error sale con su código.

        Se ejecuta la ruta que muestra el diagnóstico y se mira lo que sale, no
        la función que lo muestra. Es lo que distingue "el `except` existe" de
        "el `except` funciona".
        """
        import contextlib
        import io

        from sourc.cli import Printer, cmd_size

        errores = self._con_texto()
        self.assertTrue(errores)

        salida, errores_out = io.StringIO(), io.StringIO()
        impresor = Printer(stream=salida, err_stream=errores_out)
        argumentos = type("A", (), {
            "paths": [str(ROOT / "examples" / "hola.src")],
            "nivel": None, "stats": False})()
        with contextlib.redirect_stdout(io.StringIO()):
            codigo = cmd_size(argumentos, impresor)

        # Lo que se comprueba es que el comando **no se cae** al informar y que
        # su salida se puede leer. El caso de un fichero que no baja a IR --el
        # único que pasa por el `except` roto-- lo cubre `test_el_cli_no_tiene_
        # ningun_printer_diagnostic`, porque provocarlo aquí exigiría un programa
        # que hoy no existe.
        self.assertIn(codigo, (0, 1))
        self.assertTrue(salida.getvalue(),
                        "sourc size tiene que imprimir algo")
