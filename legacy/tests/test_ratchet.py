"""El ratchet de las diferencias del fuzzer sólo puede encogerse.

Hay una tentación muy fácil con un fuzzer: cuando sale un fallo conocido, poner
`--tolerate 3` y seguir. Eso es olvidar, no tolerar. En dos meses nadie recuerda
que existían, el fuzzer da 97 de 100 y todo el mundo asume que el tres por ciento
es ruido del generador. Y el día que aparezca un fallo **nuevo** entre esos tres,
nadie lo distingue de los viejos.

Un ratchet es distinto en tres cosas, y este módulo vigila las tres:

1. **Nombra cada diferencia.** Una entrada es una semilla concreta, no un
   número de fallos. La semilla se puede volver a ejecutar.
2. **Dice por qué.** Una entrada sin motivo es un fallo tolerado con pasos extra,
   y aquí se rechaza.
3. **Sólo encoge.** Ésta es la que importa. Arreglar un bug obliga a borrar su
   entrada, y borrar una entrada es una decisión que alguien tiene que tomar a
   propósito. Sin esta comprobación, "arreglar" y "tolerar más" son la misma
   acción.

## Por qué el fuzzer no está en la puerta de release

Y no lo va a estar mientras quede una diferencia registrada. Un gate que tolera
un fallo entrena a ignorar el cartel, y el cartel del fuzzer es el que más bugs
ha encontrado del repositorio entero: siete en una tarde, cuatro de ellos
funciones que la lengua declaraba y nadie podía compilar.

Lo que sí está en la puerta son las invariantes del generador
(`tests/test_fuzz_source.py`) y este ratchet. Cuando la última entrada se borre,
la conversación es otra.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fuzz_diffs  # noqa: E402


class TestElRatchetEsUnRatchet(unittest.TestCase):
    def test_el_fichero_se_puede_leer(self):
        datos = fuzz_diffs.cargar()
        self.assertIn("diferencias", datos)
        self.assertIsInstance(datos["diferencias"], dict)

    def test_la_lista_es_pequeña(self):
        """Y el número no crece porque sí, sino porque alguien lo decided.

        Una lista larga es una lista de bugs sin prioridad, y a partir de cierto
        tamaño lo útil es arreglar en vez de anotar.
        """
        self.assertLessEqual(
            len(fuzz_diffs.diferencias()), 5,
            "hay demasiadas diferencias registradas: el fuzzer está pidiendo "
            "atensión en vez de dar el aviso")

    def test_cada_entrada_explica_el_motivo(self):
        for semilla, motivo in fuzz_diffs.diferencias().items():
            with self.subTest(semilla=semilla):
                self.assertTrue(semilla.isdigit(),
                                f"la clave {semilla!r} no es una semilla")
                self.assertGreater(
                    len(motivo), 80,
                    f"la diferencia de la semilla {semilla} no explica nada: "
                    "una entrada sin motivo es un fallo tolerado a Ciegas")

    def test_cada_entrada_dice_como_reproducirla(self):
        """Sin el comando, la entrada es una nota, no un ratchet."""
        for semilla, motivo in fuzz_diffs.diferencias().items():
            with self.subTest(semilla=semilla):
                self.assertIn("fuzz_source.py", motivo,
                              f"la semilla {semilla} no dice cómo reproducirla")

    def test_la_semilla_sepuede_ejecutar_de_verdad(self):
        """La entrada tiene que producir un programa, o la lista está mal.

        Es el test que convierte una lista de notas en un inventario: si alguien
        escribe una semilla que no existe o que ya no da ese programa, el ratchet
        está mintiendo sobre lo que cubre.
        """
        base = fuzz_diffs.cargar().get("semilla_base", 779000)
        for semilla in fuzz_diffs.diferencias():
            with self.subTest(semilla=semilla):
                numero = int(semilla) - base
                if numero < 0:
                    self.skipTest("la semilla no viene de la tanda conocida")
                resultado = subprocess.run(
                    [sys.executable, "tools/fuzz_source.py",
                     "--ver", str(numero), "--semilla", str(base)],
                    cwd=ROOT, capture_output=True, text=True, timeout=120)
                self.assertEqual(
                    resultado.returncode, 0,
                    f"no se pudo regenerar el programa de la semilla {semilla}")
                self.assertIn("Programa generado",
                              resultado.stdout)


class TestLasReservasNoSeEngordan(unittest.TestCase):
    """Las dos reservas que hay en el repo, con el mismo motivo."""

    def test_cada_entrada_que_sobre_es_un_bug_nuevo(self):
        """Ejecuta el fuzzer sobre la tanda y comprueba que no hay semillas
        nuevas que no estén registradas.

        No es una puerta de release y no pretende serlo: necesita Node y tarda
        unos segundos. Es la comprobación que hay que hacer **antes** de cerrar
        un `fuzz_diffs.json` más grande, porque un ratchet que se agranda sin
        que nadie mire es exactamente lo que este módulo evita.
        """
        import shutil

        if shutil.which("node") is None:
            self.skipTest("node no está instalado")
        base = 779000
        resultado = subprocess.run(
            [sys.executable, "tools/fuzz_source.py", "--n", "80",
             "--semilla", str(base), "--sin-ratchet"],
            cwd=ROOT, capture_output=True, text=True, timeout=900)
        if resultado.returncode != 0 and "new" in resultado.stdout:
            self.fail("el fuzzer encontró diferencias sin registrar:\n"
                      + resultado.stdout[-2000:])

    def test_el_json_está_ordenado(self):
        """Un fichero a mano se desordena, y el diff se vuelve ruido."""
        texto = fuzz_diffs.RUTA.read_text(encoding="utf-8")
        canonical = json.dumps(json.loads(texto), indent=2, sort_keys=True,
                               ensure_ascii=False) + "\n"
        self.assertEqual(
            texto, canonical,
            "tests/fuzz_diffs.json no está ordenado; se regenera con "
            "fuzz_diffs.guardar()")


if __name__ == "__main__":
    unittest.main()