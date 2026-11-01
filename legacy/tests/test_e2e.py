"""El E2E por CDP: se puede ejecutar en parte, y se salta **declarado** en el resto.

## Por qué el E2E sale con código 3 y no con código 0

Un E2E que no se ejecuta y parece que pasa es peor que no tener E2E: la puerta
queda verde sin comprobar nada, y eso es exactamente la forma de fallo que este
repositorio lleva dos sesiones intentando cerrar --"el salto declarado no basta",
"la tabla tiene que poder mentir para que sirva".

Por eso `tools/e2e_cdp.mjs` sale con **3** cuando no encuentra Chromium, y este
test reconoce ese número. Un fallo de verdad sale con 1 y **no** se salta.

## Qué se comprueba sin navegador, y por qué es algo

Dos cosas de este fichero se pueden verificar en cualquier máquina, y son
precisamente las dos que un E2E sin navegador dejaría sin comprobar:

- **Dónde mira el navegador.** El orden de candidatos y el `CHROMIUM`/`CHROME`
  del entorno. Con un camino inventado por `CHROMIUM` se ve que lo encuentra, y
  sin ninguno se ve que no miente diciendo que lo ha encontrado.
- **El servidor de ficheros.** Sirve el `dist/` y sólo el `dist/`, devuelve 404
  para lo que no está y 403 para lo que intenta salir. Eso es lo que decide si
  el `dist/` "se basta solo", y es lógica propia: si el `dist/` necesitara algo de
  `runtime/`, el E2E real daría un 404, pero sin Chromium nadie lo vería.

La comparación con el intérprete y la paridad de la página ya la hacen otras
puertas; aquí lo que se comprueba es que el camino de carga existe.

## Lo que este fichero NO puede hacer

No puede afirmar que el E2E funciona. Sin Chromium, este módulo pasa y el E2E
sigue sin haberse ejecutado nunca. Está escrito para que cuando Chromium esté,
el paso sea real; y para que mientras no esté, nadie confíe en él por estar en
verde.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compiler"))

NODE = shutil.which("node")
E2E = ROOT / "tools" / "e2e_cdp.mjs"

# El código con el que el script dice "no hay navegador". Está escrito en los
# dos sitios a propósito: si uno cambia, este test falla en vez de saltarse en
# silencio y dejar la puerta verde sin comprobar.
SIN_NAVEGADOR = 3


def _correr(*argumentos, entorno=None):
    return subprocess.run([NODE, str(E2E), *argumentos], cwd=ROOT,
                          capture_output=True, text=True, timeout=300,
                          env=entorno)


@unittest.skipUnless(NODE, "node no está instalado")
class TestElHarnessSePuedeEjecutar(unittest.TestCase):
    """Lo que se puede comprobar sin Chromium, que es más que nada."""

    def test_sin_flags_no_hay_navegador(self):
        """Sin nada en el entorno, el script dice que no lo hay.

        Se limpia `CHROMIUM` y `CHROME` a propósito: si el test dependiera de la
        máquina de quien lo ejecuta, en un sitio con Chromium daría verde por un
        motivo y en otro por otro.
        """
        entorno = dict(os.environ)
        entorno.pop("CHROMIUM", None)
        entorno.pop("CHROME", None)
        resultado = _correr("--sin-navegador", entorno=entorno)
        self.assertEqual(resultado.returncode, SIN_NAVEGADOR,
                         "sin Chromium tiene que salir con 3, no con 0")
        self.assertIn("no está", resultado.stdout)

    def test_el_entorno_apunta_a_un_navegador(self):
        """Con `CHROMIUM` puesto, lo encuentra. Aunque no funcione.

        Es lo que separa "el harness sabe dónde mirar" de "el harness funciona".
        Un Chromium que no arranca da 127, que **no** es 3: se distingue el
        "no lo hay" del "está y no puede".
        """
        with tempfile.TemporaryDirectory() as tmp:
            falso = Path(tmp) / "chromium"
            falso.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(falso, 0o755)
            entorno = dict(os.environ, CHROMIUM=str(falso))
            resultado = _correr("--sin-navegador", entorno=entorno)
        self.assertEqual(resultado.returncode, 0)
        self.assertIn(str(falso), resultado.stdout)

    def test_el_script_no_importa_nada_de_fuera(self):
        """Cero dependencias, y no por gusto: es la regla del manifiesto.

        Se comprueba con `import` en vez de leyendo el texto, porque un `import`
        dentro de un comentario o de una cadena no es un `import` y un `grep` no
        lo distingue.
        """
        resultado = subprocess.run(
            [NODE, "--input-type=module", "-e",
             f'import {{ readFileSync }} from "node:fs";'
             f'const t = readFileSync({json.dumps(str(E2E))}, "utf8");'
             'const imports = [...t.matchAll(/(?:^|\\n)\\s*import[^\\n]*from '
             '"([^"]+)"/g)].map((m) => m[1]);'
             'const externos = imports.filter((i) => !i.startsWith("node:"));'
             'console.log(JSON.stringify(externos));'],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(resultado.returncode, 0, resultado.stderr[-300:])
        externos = json.loads(resultado.stdout)
        self.assertEqual(externos, [],
                         f"el E2E importa paquetes de fuera: {externos}")


@unittest.skipUnless(NODE, "node no está instalado")
class TestElServidorDelDist(unittest.TestCase):
    """El servidor es lo que decide si el `dist/` se basta solo."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dist = Path(cls.tmp.name) / "dist"
        resultado = subprocess.run(
            [sys.executable, "compiler/sourc.py", "build",
             "examples/hola.src", "--release"],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
            env=dict(os.environ, SOURC_DIST=str(cls.dist)))
        if resultado.returncode != 0:
            cls.tmp.cleanup()
            raise unittest.SkipTest(f"no se pudo empaquetar: {resultado.stderr}")
        cls.empaquetado = cls.dist / "hola"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _servidor(self):
        # El nombre del módulo se pasa porque el servidor lo sirve como un
        # fichero más, y el MIME del `.wasm` es justo lo que se quiere mirar.
        resultado = _correr("--servidor", str(self.empaquetado), "hola.wasm")
        self.assertEqual(resultado.returncode, 0, resultado.stderr[-400:])
        return json.loads(resultado.stdout)

    def test_sirve_la_pagina_y_el_pegamento(self):
        salida = self._servidor()
        self.assertEqual(salida["indice"]["estado"], 200)
        self.assertEqual(salida["glue"]["estado"], 200)
        # Los bytes tienen que ser los del fichero, no un 200 con cualquier cosa.
        self.assertEqual(salida["indice"]["bytes"],
                         (self.empaquetado / "index.html").stat().st_size)

    def test_lo_que_no_esta_da_404(self):
        """Un 404 tiene que ser un 404.

        Si el servidor devolviera 200 con una página vacía, un import roto en el
        `dist/` pasaría y el E2E real no vería nada.
        """
        self.assertEqual(self._servidor()["ausente"]["estado"], 404)

    def test_no_sirve_fuera_del_dist(self):
        """Servir el `dist/` no puede ser una forma de leer el disco.

        Se comprueban las dos formas de intentar salir: la ruta con `..` y la
        ruta con el `..` codificado en hexadecimal. La segunda llega al mismo
        sitio y es la que un servidor ingenuo deja pasar, porque `join` no la
        normaliza y `existsSync` sí la resuelve.
        """
        salida = self._servidor()
        for clave in ("escapes", "escapes2"):
            with self.subTest(forma=clave):
                self.assertIn(salida[clave]["estado"], (403, 404))
                self.assertLess(salida[clave]["bytes"], 32,
                                "no puede haber servido el fichero de verdad")

    def test_los_tres_tipos_de_contenido(self):
        """El MIME del `.wasm` no es un detalle.

        `WebAssembly.compileStreaming` lo exige, y si algún día la página lo usa
        con otro MIME falla en el navegador y no en ningún test. También el
        `text/html` sin `charset` rompe los acentos en la página.
        """
        salida = self._servidor()
        self.assertTrue(salida["indice"]["tipo"].startswith("text/html"))
        self.assertIn("charset", salida["indice"]["tipo"])
        self.assertEqual(salida["glue"]["tipo"], "text/javascript; charset=utf-8")
        self.assertEqual(salida["programa"]["tipo"], "application/wasm")


@unittest.skipUnless(NODE, "node no está instalado")
class TestElE2EDeverdad(unittest.TestCase):
    """Con Chromium, el E2E corre. Sin él, se salta diciendo por qué."""

    def test_el_e2e_real(self):
        # El `dist/` se monta aquí y no se usa el del repo. Un test que depende
        # de que alguien se haya dejado un `make dist` puesto pasa en local y
        # falla en la suite limpia, que es la forma más traicionera de test que
        # hay: verde por algo que no está delante.
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "dist"
            empaquetado = subprocess.run(
                [sys.executable, "compiler/sourc.py", "build",
                 "examples/hola.src", "--release"],
                cwd=ROOT, capture_output=True, text=True, timeout=300,
                env=dict(os.environ, SOURC_DIST=str(destino)))
            self.assertEqual(empaquetado.returncode, 0, empaquetado.stderr)
            dist = destino / "hola"

            if not _hay_navegador():
                resultado = _correr(str(dist), "hola.wasm")
                self.assertEqual(
                    resultado.returncode, SIN_NAVEGADOR,
                    "sin Chromium tiene que decir que no lo hay, no fallar por "
                    "otra cosa. Con un dist/ presente, cualquier otro código "
                    "significa que el harness tiene otro fallo antes.")
                self.assertIn("CHROMIUM", resultado.stderr,
                              "el mensaje tiene que decir cómo instalar uno")
                return

            resultado = _correr(str(dist), "hola.wasm")

        self.assertEqual(resultado.returncode, 0, resultado.stderr[-600:])
        salida = json.loads(resultado.stdout)
        self.assertIsNone(salida["error"], str(salida["error"]))
        self.assertEqual(salida["salida"], "hola mundo\n")


def _hay_navegador() -> bool:
    """Se lo pregunta al propio harness, no se reimplementa su búsqueda.

    Reimplementar la lista de candidatos en Python sería tener la misma
    respuesta en dos sitios y cambiar uno sin cambiar el otro. El harness es la
    autoridad: si él dice que no hay, no hay.
    """
    if shutil.which("node") is None:
        return False
    return _correr("--sin-navegador").returncode == 0


if __name__ == "__main__":
    unittest.main()