"""El `dist/` tiene que ser lo que dice ser: tres ficheros, un manifiesto, y nada fuera.

La prueba de que un `dist/` funciona no es abrirlo, es **no poder abrir nada más**.
Un artefacto que necesita un fichero del repo para arrancar no es portable, y
uno que trae un cuarto fichero que nadie usa tiene un byte que alguien va a
usar mal. Estas comprobaciones miran la lista de ficheros exactamente, porque
enumerar "lo que debería estar" y no comprobar "lo que hay" deja pasar de todo.

## Lo que hay que mirar y por qué

- **El presupuesto bloquea.** El caso más importante del fichero: se baja un
  techo, se espera que no se escriba nada, y se restaura. Un `--release` que
  empaqueta siempre y avisa después es una copia con pasos extra.
- **Un `dist/` a medias no existe.** Se prueba dejando un `dist/` viejo y
  haciendo fallar el empaquetado: el viejo tiene que seguir intacto. El peor
  estado posible de un artefacto es uno que parece bueno y no lo está.
- **La página reescribe el import y nada más.** Si además se tocara el módulo o
  el pegamento, el peso medido dejaría de ser el peso que se descarga.
- **El manifiesto es reproducible.** Dos `dist/` de la misma fuente tienen que
  dar el mismo `sourc-manifest.json` byte a byte, o no se pueden comparar.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compiler"))

from sourc import budget, paquete            # noqa: E402


def _compilar(fuente: Path, dest: Path,
              presupuestos: Path | None = None) -> subprocess.CompletedProcess:
    """Compila con `--release` escribiendo en `dest`.

    `presupuestos` apunta `SOURC_BUDGETS` a otro fichero, que es la única forma
    de comprobarlo desde fuera: un `mock.patch` no llega a un subproceso, y este
    test lanza uno.
    """
    import os

    entorno = dict(os.environ, SOURC_DIST=str(dest))
    if presupuestos is not None:
        entorno["SOURC_BUDGETS"] = str(presupuestos)
    return subprocess.run(
        [sys.executable, "compiler/sourc.py", "build", str(fuente),
         "--release"],
        cwd=ROOT, capture_output=True, text=True, timeout=300, env=entorno)


class TestElContenidoDelDist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dist = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _hola(self):
        resultado = _compilar(ROOT / "examples" / "hola.src", self.dist)
        self.assertEqual(resultado.returncode, 0, resultado.stdout
                         + resultado.stderr)
        return self.dist / "hola"

    def test_son_cuatro_ficheros_y_solo_otros_cuatro(self):
        """Ni uno más. El cuarto es el manifiesto, y se cuenta aparte."""
        dist = self._hola()
        self.assertEqual(sorted(p.name for p in dist.iterdir()),
                         ["glue_core.js", "hola.wasm", "index.html",
                          "sourc-manifest.json"])

    def test_no_hay_ningun_otro_fichero_cerca(self):
        """El `dist/` no puede depender de nada que esté fuera.

        Se comprueba que `glue_core.js` está **dentro** y no en el repo: es el
        fichero que el navegador carga, y si se sirviera de fuera el `dist/` no
        sería portable aunque no se pudiera ver.
        """
        dist = self._hola()
        pegamento = (dist / "glue_core.js").read_bytes()
        self.assertEqual(
            pegamento,
            (ROOT / "runtime" / "glue" / "glue_core.js").read_bytes(),
            "el pegamento del dist/ tiene que ser el del repo, byte a byte")

    def test_la_pagina_importa_el_pegamento_de_al_lado(self):
        dist = self._hola()
        pagina = (dist / "index.html").read_text(encoding="utf-8")
        self.assertIn('from "./glue_core.js"', pagina)
        self.assertNotIn("../glue/glue_core.js", pagina,
                         "una ruta al repo en el dist/ significa que el dist/ "
                         "no funciona solo")

    def test_la_pagina_solo_cambia_el_import_y_el_nombre_del_modulo(self):
        """Dos cambios y ni uno más. El peso del `dist/` es el que se descarga.

        Si al reescribir se quitara un comentario, el `index.html` del `dist/`
        mediría menos que el del repo y el presupuesto estaría mintiendo. Por eso
        la comparación es exacta: la página del `dist/` tiene que ser la del repo
        con el import apuntando al lado y el nombre del módulo siendo el suyo.
        """
        dist = self._hola()
        del_repo = (ROOT / "runtime" / "page" / "index.html").read_text(
            encoding="utf-8")
        en_dist = (dist / "index.html").read_text(encoding="utf-8")
        self.assertEqual(
            del_repo.replace("../glue/glue_core.js", "./glue_core.js")
                     .replace('"main.wasm"', '"hola.wasm"'),
            en_dist,
            "la página del dist/ difiere del repo en algo más que el import y "
            "el nombre del módulo")

    def test_el_manifesto_dice_los_bytes_y_el_hash(self):
        import json

        dist = self._hola()
        datos = json.loads((dist / "sourc-manifest.json").read_text(
            encoding="utf-8"))
        self.assertEqual(datos["modulo"], "hola.wasm")
        self.assertIn("glue_core.js", datos["artefactos"])
        for nombre, entrada in datos["artefactos"].items():
            with self.subTest(artefacto=nombre):
                crudo = (dist / nombre).read_bytes()
                self.assertEqual(entrada["bytes"], len(crudo))
                import hashlib
                self.assertEqual(entrada["sha256"],
                                 hashlib.sha256(crudo).hexdigest())


class TestElPresupuestoManda(unittest.TestCase):
    """El techo es condición para escribir, no un informe que sale después."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dist = Path(self.tmp.name)
        self.presupuestos = self.dist / "budgets.toml"
        self.addCleanup(self.tmp.cleanup)

    def _bajar_el_techo_del_pegamento(self):
        """Escribe unos presupuestos con el pegamento estrangulado.

        No se toca el `budgets.toml` del repo: un test que edita un fichero
        versionado para poder fallar deja el repo modificado si algo va mal, y
        además el fallo dependería de un fichero que otro test puede estar
        tocando a la vez.
        """
        self.presupuestos.write_text(
            'tolerance = 0.02\n\n'
            '[[budget]]\n'
            'artifact = "source_glue.js"\n'
            'phase = 2\n'
            'gzip = 40\n'
            'hard_gzip = 40\n'
            'raw = 40\n', encoding="utf-8")

    def test_no_se_escribe_cuando_no_cabe(self):
        self._bajar_el_techo_del_pegamento()
        resultado = _compilar(ROOT / "examples" / "hola.src", self.dist,
                              presupuestos=self.presupuestos)
        self.assertEqual(resultado.returncode, 1,
                         "un pegamento que no cabe tiene que hacer fallar el "
                         "build de release")
        # `printer.bad` va a stderr: el motivo de que no se empaqueta es un
        # error del build, no una nota del progreso.
        self.assertIn("no se empaqueta", resultado.stdout + resultado.stderr)
        self.assertIn("supera el techo",
                      resultado.stdout + resultado.stderr,
                      "el motivo tiene que decir qué techo y cuánto se pasó")
        self.assertFalse((self.dist / "hola").exists(),
                         "no se puede dejar un dist/ a medias")


class TestUnDistAMediasNoExiste(unittest.TestCase):
    def test_un_dist_anterior_se_queda_cuando_el_nuevo_falla(self):
        """Si el empaquetado falla, el `dist/` de antes sigue siendo válido.

        El estado peor posible de un artefacto es uno que parece bueno y no lo
        está: a medias, con tres ficheros de cuatro. Montar en un temporal y
        mover entero lo evita, y este test es lo que lo fija.
        """
        import os

        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            destino = raiz / "dist" / "hola"
            destino.mkdir(parents=True)
            (destino / "viejo.txt").write_text("el bueno", encoding="utf-8")

            ficheros = paquete.preparar(b"\x00asm\x01\x00\x00\x00", "hola.wasm")
            resultado = paquete.escribir(destino, ficheros,
                                         ["un techo que se pasa"])
            self.assertFalse(resultado.ok)
            self.assertTrue((destino / "viejo.txt").exists(),
                            "un fallo tiene que dejar el dist/ anterior intacto")

    def test_al_escribir_se_mueven_los_tres_y_el_manifiesto(self):
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "dist" / "hola"
            ficheros = paquete.preparar(b"\x00asm\x01\x00\x00\x00", "hola.wasm")
            resultado = paquete.escribir(destino, ficheros, [])
            self.assertTrue(resultado.ok)
            self.assertEqual(
                sorted(p.name for p in destino.iterdir()),
                ["glue_core.js", "hola.wasm", "index.html",
                 "sourc-manifest.json"])

    def test_no_deja_directorios_de_trabajo(self):
        """El temporal tiene que desaparecer pase lo que pase.

        Un `dist.en-empaquetado` que se queda es un directorio que el siguiente
        `--release` puede encontrar, y cuyo contenido es de una build anterior.
        """
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "dist" / "hola"
            ficheros = paquete.preparar(b"\x00asm\x01\x00\x00\x00", "hola.wasm")
            paquete.escribir(destino, ficheros, [])
            self.assertEqual([p.name for p in (Path(tmp) / "dist").iterdir()],
                             ["hola"])


class TestElManifiestoEsReproducible(unittest.TestCase):
    def test_dos_veces_los_mismos_bytes(self):
        """Sin esto, dos `dist/` no se pueden comparar y el manifiesto sobra."""
        uno = paquete.manifiesto(
            paquete.preparar(b"\x00asm\x01\x00\x00\x00", "hola.wasm"), "hola.wasm")
        otro = paquete.manifiesto(
            paquete.preparar(b"\x00asm\x01\x00\x00\x00", "hola.wasm"), "hola.wasm")
        self.assertEqual(uno, otro)


class TestElPegamentoEsElDelRepo(unittest.TestCase):
    def test_el_alias_del_techo_existe(self):
        """El pegamento del `dist/` tiene que encontrar su techo.

        Sin el alias se busca `glue_core.js` donde `budgets.toml` dice
        `source_glue.js`, no se encuentra nada, y el `--release` empaqueta un
        pegamento que se ha pasado del techo **sin decir nada**. Es el techo que
        aprieta más de todos --lo descarga el 100% de las páginas-- y era el
        único que no miraba.
        """
        _, presupuestos = budget.cargar(paquete.RUTA_PRESUPUESTOS)
        nombres = {p.artifact for p in presupuestos}
        self.assertIn(paquete.ALIAS_PRESUPUESTO[paquete.GLUE], nombres)
        self.assertIn(paquete.ALIAS_PRESUPUESTO[paquete.PAGINA], nombres)

    def test_el_techo_aprita_de_verdad(self):
        """El pegamento está dentro del presupuesto con poco margen.

        Si algún día alguien sube el objetivo sin mirar, esto se entera: el
        margen es la distancia entre lo que se descarga y lo que se permite.
        """
        _, presupuestos = budget.cargar(paquete.RUTA_PRESUPUESTOS)
        pegamento = [p for p in presupuestos
                     if p.artifact == paquete.ALIAS_PRESUPUESTO[paquete.GLUE]]
        self.assertTrue(pegamento)
        techo = pegamento[0]
        medida = budget.medir(
            (ROOT / "runtime" / "glue" / "glue_core.js").read_bytes())
        self.assertLess(medida.gzip, techo.gzip,
                        f"el pegamento mide {medida.gzip} B gzip y el techo es "
                        f"{techo.gzip} B: el objetivo va sin margen a propósito")


if __name__ == "__main__":
    unittest.main()

class TestElDistCorreSolo(unittest.TestCase):
    """La afirmación "el `dist/` se basta solo" tiene que ser ejecutable.

    Todo lo demás del fichero mira la lista de ficheros y la lista de tamaños.
    Esto ejecuta la **página del `dist/`** con el **pegamento del `dist/`** y
    compara con lo que da la página del repo. Si el `dist/` necesitara algo del
    árbol --un import con ruta relativa, un módulo que sólo existe en
    `runtime/`-- aquí saldría un 404 o un error de módulo, que es exactamente lo
    que esta clase existe para cazar.

    Va con Node y no con navegador a propósito: el navegador es el paso E y este
    es el anterior. Un paso que depende del anterior sólo se puede saltar entero
    cuando el anterior falla.
    """

    def setUp(self):
        if shutil.which("node") is None:
            self.skipTest("node no está instalado")
        self.tmp = tempfile.TemporaryDirectory()
        self.dist = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _empaquetar(self, fuente: str) -> Path:
        resultado = _compilar(ROOT / "examples" / fuente, self.dist)
        self.assertEqual(resultado.returncode, 0, resultado.stdout
                         + resultado.stderr)
        return self.dist / fuente[:-4]

    def _correr(self, pagina: Path, pegamento: Path, modulo: Path,
                nombre: str, frames: str = "1") -> dict:
        import json

        resultado = subprocess.run(
            [shutil.which("node"), "tools/page_check.mjs", str(modulo),
             frames, nombre,
             "--pagina", str(pagina), "--glue", str(pegamento)],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(resultado.returncode, 0, resultado.stderr[-600:])
        return json.loads(resultado.stdout)

    def test_el_dist_da_lo_mismo_que_la_pagina_del_repo(self):
        dist = self._empaquetar("laberinto.src")
        desde_dist = self._correr(dist / "index.html", dist / "glue_core.js",
                                  dist / "laberinto.wasm", "laberinto.wasm",
                                  frames="5")
        desde_repo = self._correr(ROOT / "runtime" / "page" / "index.html",
                                  ROOT / "runtime" / "glue" / "glue_core.js",
                                  dist / "laberinto.wasm", "main.wasm",
                                  frames="5")
        self.assertIsNone(desde_dist["error"],
                          f"el dist/ falló: {desde_dist['error']}")
        self.assertEqual(desde_dist["display"], desde_repo["display"])
        self.assertEqual(desde_dist["salida"], desde_repo["salida"])

    def test_hola_tambien(self):
        """Un programa que sólo imprime no tiene `display`, y también vale.

        Es el caso mínimo del `dist/`: si el pegamento no carga, esto falla; y
        como no hay dibujo, el fallo tiene que ser de verdad de carga y no de
        dibujo.
        """
        dist = self._empaquetar("hola.src")
        salida = self._correr(dist / "index.html", dist / "glue_core.js",
                              dist / "hola.wasm", "hola.wasm")
        self.assertIsNone(salida["error"], str(salida["error"]))
        self.assertEqual(salida["salida"], "hola mundo\n")
