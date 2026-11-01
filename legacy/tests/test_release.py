"""La puerta de release tiene que ser una puerta, no una trampa.

Un gate es código que se ejecuta cuando algo importante va a pasar, y por eso
sus fallos se pagan caros. Este módulo comprueba tres cosas de `tools/release.py`
que no son su resultado --que sean correctas-- sino su **forma**, que es lo que
hace que seadangeroso ejecutarlo sin mirarlo:

1. Ningún paso es el propio script. Un paso que se invoca a sí mismo con un flag
   se invoca otra vez con ese flag, y otra: la primera versión de este gate lo
   tenía, y congeló el codespace con una recursión de procesos antes de que nadie
   llegara a ver un solo paso en verde.
2. Todo lo que un paso necesita existir antes de ejecutarse.
3. Todo paso dice en cuánto tardó, porque un gate que no dice cuánto cuesta es un
   gate que nadie espera.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import release                                                   # noqa: E402


class TestPuertaDeRelease(unittest.TestCase):
    def test_ningun_paso_es_el_propio_script(self):
        """La comprobación que se pagó con un codespace congelado."""
        viciousos = [p.nombre for p in release.PASOS
                     if any(Path(parte).name == "release.py"
                            for parte in p.comando)]
        self.assertEqual(
            viciousos, [],
            f"pasos que se invocan a sí mismos: {viciousos}. "
            "Cada paso tiene que ser un comando externo: si un paso puede "
            "volver a entrar en el gate, la máquina se queda sin procesos.")

    def test_los_pasos_usan_el_interprete_actual(self):
        """`sys.executable`, no `python3` del PATH.

        El gate tiene que correr en el mismo intérprete que el resto del
        proyecto. Con `python3` a secas, un `make release` en una máquina con
        dos versiones comprueba una y construye con la otra.
        """
        for paso in release.PASOS:
            if paso.comando[0].endswith(".py"):
                self.assertEqual(paso.comando[0], release.PYTHON,
                                 f"{paso.nombre} no usa sys.executable")

    def test_todo_lo_que_necesitan_los_pasos_existe(self):
        faltan = []
        for paso in release.PASOS:
            for parte in paso.comando[1:]:
                if parte.endswith(".py"):
                    if not (ROOT / parte).exists():
                        faltan.append(f"{paso.nombre}: {parte}")
        self.assertEqual(faltan, [],
                         f"pasos que apuntan a ficheros que no existen: {faltan}")

    def test_la_suite_completa_es_un_paso(self):
        """El paso de `--fast` no vale: el gate tiene que ver los goldens."""
        comandos = [p.comando for p in release.PASOS]
        self.assertIn([release.PYTHON, "tools/testharness.py"], comandos,
                      "la puerta tiene que correr la suite entera, no la "
                      "de --fast")

    def test_cada_paso_dice_en_que_consiste(self):
        """Un paso sin explicación es un paso que nadie revisa."""
        sin_nota = [p.nombre for p in release.PASOS if not p.nota.strip()]
        self.assertEqual(sin_nota, [], f"pasos sin nota: {sin_nota}")

    def test_el_escalado_esta_en_la_puerta(self):
        """Un O(n²) en el pipeline no se ve en ningún test de corrección.

        El gate mide la razón de crecimiento, no los milisegundos: la máquina
        cambia el número y un test de tiempo acaba desactivándose, pero un
        cuadrado aparece como un salto de un orden de magnitud que no depende de
        quién lo corra. Sin él en la puerta, se descubre en la Fase 8.
        """
        comandos = [" ".join(p.comando) for p in release.PASOS]
        self.assertTrue(any("perf.py" in c for c in comandos),
                        "el gate de escalado no está en la puerta de release")

    def test_el_contrato_del_subconjunto_esta_en_la_puerta(self):
        """ADR 0015 lo llama "el riesgo número uno del proyecto".

        Un gate que no está en la puerta de release se desactiva el día que
        moleste, y el riesgo número uno es justamente el que va a molestar.
        """
        comandos = [" ".join(p.comando) for p in release.PASOS]
        self.assertTrue(any("test_subset" in c for c in comandos),
                        "el contrato del subconjunto no está en la puerta de "
                        "release")

    def test_el_presupuesto_de_peso_esta_en_la_puerta(self):
        """Es el gate que decide si el peso es aceptable (ADR P2).

        `budgets.toml` dice que el presupuesto es un gate y no un informe, y un
        gate que no está en la puerta de release es un informe.
        """
        nombres = " ".join(" ".join(p.comando) for p in release.PASOS)
        self.assertIn("size", nombres,
                      "el presupuesto de peso no está en la puerta de release")


if __name__ == "__main__":
    unittest.main()
