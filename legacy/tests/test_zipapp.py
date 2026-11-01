"""El compilador en un solo fichero, y que eso no sea una promesa.

## Qué se comprueba

Que `dist/sourc.pyz` sirve para **compilar y para empaquetar** sin el repo
alrededor. La prueba se hace en un directorio temporal vacío, con el `.pyz`
copiado dentro y **nada más**: si el empaquetado dependiera de `budgets.toml`
por ruta, o de `runtime/`, o de la raíz del repositorio, aquí falla.

Es la forma de comprobar una afirmación de distribución sin fiarse de ella:
"un fichero, en cualquier sitio". La afirmación se ejecuta en `/tmp` con un
fichero copiado, y si el resultado depende del entorno, el entorno está en el
camino y eso es un bug.

## Lo que NO comprueba, y está dicho arriba del todo

**Que no requiera Python.** No lo requiere: es Python. El `.pyz` quita el
`git clone`, no el intérprete, y el nombre del módulo (`build_zipapp`) no
promete lo que no hace. Quitar Python es la Fase 8, y esta clase es el
recordatorio de que eso todavía no está.

## Por qué el `.pyz` lleva datos dentro

Porque `sourc build --release` es el comando que ejecuta un usuario para
obtener el artefacto, y sin `budgets.toml` y `runtime/` dentro falla con un
`FileNotFoundError` sobre ficheros que sí están. De ahí `sourc.rutas`: dentro de
un zipapp `__file__` no existe en disco, y subir desde él da una ruta sin
sentido.

Y por eso **no** lleva los tests. Los tests comprueban el repo, no el
empaquetado, y meterlos triplicaría el fichero para que no le sirviera a nadie.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compiler"))

from sourc import rutas  # noqa: E402

FICHERO = "sourc.pyz"

DEMO = 'PRINT "hola desde un solo fichero"\nPRINT 2 + 3\n'


@unittest.skipUnless(sys.version_info >= (3, 11),
                     "zipapp necesita 3.11")
class TestElCompiladorEnUnFichero(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lugar = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.pyz = self.lugar / FICHERO
        resultado = subprocess.run(
            [sys.executable, "tools/build_zipapp.py", "--salida",
             str(self.pyz)],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(resultado.returncode, 0, resultado.stdout
                         + resultado.stderr)

    def _correr(self, *argumentos, entorno=None):
        base = dict(os.environ)
        base.pop("SOURC_BUDGETS", None)
        base.pop("SOURC_DIST", None)
        if entorno:
            base.update(entorno)
        return subprocess.run([sys.executable, str(self.pyz), *argumentos],
                              cwd=self.lugar, capture_output=True, text=True,
                              timeout=300, env=base)

    # ------------------------------------------------------------------ forma

    def test_ocupa_lo_suficiente_para_no_asustar(self):
        """Un compilador que se descarga y ocupa 3 MB no se descarga.

        El número es un techo de hecho, no un deseo: si alguien añade un
        fichero de datos sin mirar, esto salta.
        """
        tamano = self.pyz.stat().st_size
        self.assertLess(tamano, 400_000,
                        f"el .pyz pesa {tamano} B; el objetivo es estar bajo "
                        "400 KB porque es lo que alguien descarga")

    def test_dos_builds_dan_los_mismos_bytes(self):
        """El empaquetado también es reproducible, y por la misma razón.

        ADR 0016 exige que el artefacto sea reproducible como requisito duro del
        bootstrap. Eso aplica también a la herramienta que hace el bootstrap: un
        `.pyz` que cambia de bytes cada vez no se puede auditar, que es una de
        las seis promesas del manifiesto.
        """
        segundo = self.lugar / "otro.pyz"
        subprocess.run(
            [sys.executable, "tools/build_zipapp.py", "--salida", str(segundo)],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(
            self.pyz.read_bytes(), segundo.read_bytes(),
            "dos builds del .pyz dan bytes distintos; casi siempre es la fecha "
            "de modificación de algún fichero colándose en el ZIP")

    def test_no_trae_los_tests(self):
        """Los tests comprueban el repo. Meterlos triplica el fichero."""
        import zipfile

        with zipfile.ZipFile(self.pyz) as paquete:
            nombres = paquete.namelist()
        self.assertFalse(any(n.startswith("tests/") for n in nombres))
        self.assertFalse(any(n.startswith("docs/") for n in nombres))

    def test_trae_los_datos_que_hacen_falta(self):
        import zipfile

        with zipfile.ZipFile(self.pyz) as paquete:
            nombres = set(paquete.namelist())
        for necesario in ("budgets.toml", "subset.toml", "__main__.py",
                          "runtime/glue/glue_core.js",
                          "runtime/page/index.html"):
            self.assertIn(necesario, nombres)

    # ------------------------------------------------------------- funciona

    def test_check_y_run_funcionan_fuera_del_repo(self):
        """El caso mínimo, y el que más dice: no hay nada más alrededor.

        El directorio temporal no tiene `budgets.toml`, ni `runtime/`, ni
        `subset.toml`. Si algo de eso se colara por una ruta absoluta construida
        a mano, aquí lo delataría.
        """
        (self.lugar / "demo.src").write_text(DEMO, encoding="utf-8")
        resultado = self._correr("run", "demo.src")
        self.assertEqual(resultado.returncode, 0, resultado.stdout
                         + resultado.stderr)
        self.assertIn("hola desde un solo fichero", resultado.stdout)
        self.assertIn("5", resultado.stdout)

    def test_release_empaqueta_los_cuatro_ficheros(self):
        """`--release` con los presupuestos y el pegamento leídos de dentro.

        Este es el que importa: es el comando que ejecuta un usuario. Y lee dos
        ficheros de datos que dentro de un zipapp **no se pueden abrir por ruta**.
        """
        (self.lugar / "demo.src").write_text(DEMO, encoding="utf-8")
        resultado = self._correr("build", "demo.src", "--release")
        self.assertEqual(resultado.returncode, 0, resultado.stdout
                         + resultado.stderr)
        dist = self.lugar / "dist" / "demo"
        self.assertEqual(
            sorted(p.name for p in dist.iterdir()),
            ["demo.wasm", "glue_core.js", "index.html",
             "sourc-manifest.json"])

    def test_el_presupuesto_tambien_manda_desde_el_pyz(self):
        """Con un techo estrangulado, no escribe. Y dice por qué.

        Un `.pyz` que se salta los presupuestos porque no encuentra el fichero
        sería el peor fallo posible: empaquetaría en silencio lo que en el repo
        no se puede empaquetar.
        """
        (self.lugar / "demo.src").write_text(DEMO, encoding="utf-8")
        estricto = self.lugar / "estricto.toml"
        estricto.write_text('[[budget]]\nartifact = "source_glue.js"\n'
                            "gzip = 100\nraw = 100\n", encoding="utf-8")
        resultado = self._correr("build", "demo.src", "--release",
                                 entorno={"SOURC_BUDGETS": str(estricto)})
        self.assertEqual(resultado.returncode, 1)
        self.assertIn("no se empaqueta",
                      resultado.stdout + resultado.stderr)
        self.assertFalse((self.lugar / "dist" / "demo").exists())

    @unittest.skipUnless(shutil.which("node"), "node no está instalado")
    def test_el_dist_que_produce_corre(self):
        """El artefacto del `.pyz` se ejecuta con los ficheros de su propio `dist/`.

        Cierra el círculo: no basta con que se escriban cuatro ficheros, tienen
        que **arrancar**, y arrancan sin el repo porque el pegamento va dentro
        del `dist/` y el módulo también.
        """
        (self.lugar / "demo.src").write_text(DEMO, encoding="utf-8")
        self._correr("build", "demo.src", "--release")
        dist = self.lugar / "dist" / "demo"
        resultado = subprocess.run(
            [shutil.which("node"), "tools/page_check.mjs",
             str(dist / "demo.wasm"), "1", "main.wasm",
             "--pagina", str(dist / "index.html"),
             "--glue", str(dist / "glue_core.js")],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(resultado.returncode, 0, resultado.stderr[-400:])
        self.assertIn("hola desde un solo fichero", resultado.stdout)


class TestLasRutasDelPyz(unittest.TestCase):
    """`sourc.rutas` tiene que distinguir los dos mundos sin equivocarse."""

    def test_en_el_repo_la_raiz_existe(self):
        self.assertFalse(rutas.DENTRO_DE_ARCHIVO)
        self.assertIsNotNone(rutas.raiz())
        self.assertTrue((rutas.raiz() / "budgets.toml").is_file())

    def test_leer_un_recurso_da_error_que_dice_cuál(self):
        """El error tiene que nombrar el recurso, no sólo la ruta.

        Un `FileNotFoundError` pelado decía `/ruta/que/no/existe` y no decía qué
        se había pedido, que es la mitad de lo que hace falta para arreglarlo.
        """
        with self.assertRaises(rutas.SinRecurso) as capturado:
            rutas.leer_bytes("no/existe.toml")
        self.assertIn("no/existe.toml", str(capturado.exception))
        self.assertIn("se buscó en", str(capturado.exception))


if __name__ == "__main__":
    unittest.main()