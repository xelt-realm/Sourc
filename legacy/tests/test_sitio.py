"""La página del sitio, y sus tres puertas.

## Qué se comprueba aquí, y por qué son puertas y no[intencion]

Las tres cosas que se piden de una página --oscura por defecto, sin esquinas
redondeadas, y **diciendo la verdad**-- son afirmaciones. Una afirmación sin
comprobación no es un requisito, es un deseo, y en una página pública un deseo
que se rompe no se nota: nadie mira el `border-radius` de un `<pre>` y se queja,
se abre la página y se ve raro.

Las tres se comprueban sobre el **artefacto**, no sobre el fuente. El fuente es
donde uno escribe; el artefacto es lo que se descarga. Una hoja de estilo con un
`border-radius: 4px` en un comentario no rompe nada, y una con `4px` en una regla
sí.

## Y la cuarta, que no es un requisito sino una prueba

El documento **no puede contener una llave**. No porque una llave esté
prohibida en el documento --el CSS tiene muchas y van en su `<style>`-- sino
porque Source no puede emitirla: `{` abre interpolación dentro de un literal, y
no hay forma de producir el carácter con las nativas que hay. Así que una llave
en el markup no puede haber salido del programa: alguien la escribió después.

Eso convierte una limitación del lenguaje en una **prueba de autenticidad** del
documento. Y ya ha servido: la primera versión de esta página escribía
`<code>{</code>` para *hablar* de la llave, y el compilador la leyó como una
interpolación. El lenguaje avisó de que el texto era lo que no se puede
escribir, y el compilador tenía razón.

## Lo que no se comprueba

Que la página se vea bien. Eso necesita un navegador, y el E2E de Chromium está
pendiente por falta de él en esta máquina (`tests/test_e2e.py` salta declarado).
Lo que sí se comprueba aquí es que el artefacto es válido y que sus números son
ciertos, que es la parte que se puede romper en silencio.
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compiler"))
sys.path.insert(0, str(ROOT / "tools"))

from build_site import comprobar, sin_comentarios   # noqa: E402

SITE = ROOT / "site"
FUENTE = SITE / "pagina.src"
CSS = SITE / "styles.css"
ARTEFACTO = SITE / "dist" / "index.html"


def _construir() -> str:
    """Construye el sitio en un temporal y devuelve el documento."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        destino = Path(tmp) / "index.html"
        resultado = subprocess.run(
            [sys.executable, "tools/build_site.py", "--salida", str(destino)],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
        if resultado.returncode != 0:
            raise AssertionError("el sitio no se genera:\n"
                                 + resultado.stdout + resultado.stderr)
        return destino.read_text(encoding="utf-8")


class TestLaPaginaSeGenera(unittest.TestCase):
    def test_el_programa_typecheckea(self):
        """Antes de nada: el programa tiene que ser Source válido.

        La puerta de `make check` cubre `examples/`; esta cubre `site/`, que no
        está en ese directorio. Un programa que no compila produciría un sitio
        viejo en lugar de un error, que es la peor forma de fallo: verde y
        mintiendo.
        """
        resultado = subprocess.run(
            [sys.executable, "compiler/sourc.py", "check", str(FUENTE)],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(resultado.returncode, 0, resultado.stdout
                         + resultado.stderr)

    def test_el_documento_es_valido(self):
        fallos = comprobar(_construir())
        self.assertEqual(fallos, [], f"el documento no vale:\n{fallos}")

    def test_un_fichero_solo(self):
        """El objetivo de distribución: un HTML, sin nada al lado.

        Con un `styles.css` al lado son dos ficheros, y "mándame el HTML" deja de
        ser cierto. El CSS va dentro, y eso sólo es posible porque `build_site`
        lo embebe y no el programa Source --que no puede emitir una llave--.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "index.html"
            subprocess.run(
                [sys.executable, "tools/build_site.py", "--salida",
                 str(destino)], cwd=ROOT, capture_output=True, timeout=300)
            self.assertEqual([p.name for p in Path(tmp).iterdir()],
                             ["index.html"],
                             "el sitio tiene que ser un fichero y nada más")
            self.assertIn("<style>", destino.read_text(encoding="utf-8"))


class TestSinEsquinasRedondeadas(unittest.TestCase):
    """La puerta del `border-radius`, sobre el artefacto y sobre el fuente."""

    def _valores(self, css: str) -> list[str]:
        """Los valores declarados, sin comentarios.

        Los comentarios se quitan porque la hoja tiene explanatory comments que
        **mencionan** `border-radius` al explicar la regla. Buscar la palabra en
        el texto entero daría cuatro falsos positivos y el test passers por
        error, que es peor que no tenerlo.
        """
        limpio = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        return re.findall(r"border-radius\s*:\s*([^;}]+)", limpio)

    def test_el_fuente_no_declara_ninguno(self):
        valores = self._valores(CSS.read_text(encoding="utf-8"))
        self.assertEqual(valores, [],
                         f"la hoja declara border-radius: {valores}. El "
                         "requisito es esquinas rectas, y si alguna vez hace "
                         "falta una esquina redondeada es a propósito: entonces "
                         "esta puerta se cambia aquí, no en la CSS.")

    def test_el_artefacto_no_trae_ninguno(self):
        """El que de verdad importa: lo que se descarga.

        Con los comentarios dentro del CSS, la palabra `border-radius` viaja en
        el fichero aunque no haya ninguna regla, porque se explica la regla.
        """
        documento = _construir()
        bloque = re.search(r"<style>(.*?)</style>", documento, re.S)
        self.assertIsNotNone(bloque, "el documento no lleva <style>")
        self.assertEqual(self._valores(bloque.group(1)), [],
                         "el CSS que se descarga declara un border-radius")

    def test_el_artefacto_no_trae_comentarios_del_css(self):
        """Un comentario en la hoja es un byte que el visitante paga por nada.

        Y la regla está escrita en `budgets.toml` para el pegamento: un techo
        sin margen no admite comentarios que el usuario final carga. Aquí vale
        exactamente lo mismo, y ya costó 751 B gzip.
        """
        documento = _construir()
        bloque = re.search(r"<style>(.*?)</style>", documento, re.S)
        self.assertNotIn("/*", bloque.group(1),
                         "el CSS entregado lleva comentarios; se quedan en el "
                         "fuente, que es donde explican el porqué")

    def test_la_hoja_sin_comentarios_es_mas_pequena(self):
        """La comprobación de que quitar comentarios no rompe la hoja.

        Sin esto, un `sin_comentarios` demasiado agresivo --por ejemplo un regex
        que se comiera media regla-- seguiría dando verde en las puertas de
        arriba, porque lo que se comprueba es la ausencia de `border-radius`.
        """
        css = CSS.read_text(encoding="utf-8")
        limpio = sin_comentarios(css)
        self.assertLess(len(limpio), len(css))
        for regla in ("color-scheme", "background", "font", "@media"):
            self.assertIn(regla, limpio,
                          f"sin_comentarios se ha comido '{regla}'")


class TestModoOscuroPorDefecto(unittest.TestCase):
    def test_declara_modo_oscuro(self):
        """`color-scheme: dark` en `:root`.

        Sin esto el navegador pinta los controles nativos --la barra de scroll,
        un `<select>`-- en claro sobre un fondo oscuro, que es lo primero que se
        ve al abrir la página.
        """
        limpio = sin_comentarios(CSS.read_text(encoding="utf-8"))
        self.assertIn("color-scheme: dark", limpio)
        raiz = re.search(r":root\s*\{([^}]*)\}", limpio)
        self.assertIsNotNone(raiz, "no hay regla :root")
        self.assertIn("color-scheme", raiz.group(1),
                      "color-scheme tiene que estar en :root, no en una regla "
                      "suelta: si está en body, los controles nativos se pintan "
                      "antes de que exista el body")

    def test_el_artefacto_lo_declara(self):
        self.assertIn("color-scheme: dark", _construir())

    def test_el_tema_no_se_invierte(self):
        """Este sitio tiene **un** tema, y no se invierte con la preferencia del
        sistema.

        La tentación clásica es poner `prefers-color-scheme: light` como
        alternativa "por accesibilidad", y el resultado es un sitio que en una
        mitad de los máquinas sale oscuro y en la otra mitad medio claro. Un
        proyecto cuya promesa es la predictibilidad no puede tener dos
        apariencias.

        Se busca `@media` junto a `prefers-color-scheme` y **no** la palabra
        suelta, porque la página menciona el término al explicar esta misma
        regla. Buscarlo entero hacía fallar la puerta por su propia
        documentación, que es el peor sitio donde puede estar una puerta.
        """
        limpio = sin_comentarios(CSS.read_text(encoding="utf-8"))
        self.assertNotRegex(
            limpio, r"@media[^}]*prefers-color-scheme",
            "el tema no se invierte; este sitio tiene un tema")

        estilo = re.search(r"<style>(.*?)</style>", _construir(), re.S).group(1)
        self.assertNotRegex(
            estilo, r"@media[^}]*prefers-color-scheme",
            "el CSS que se descarga invierte el tema")

    def test_no_html_blanco(self):
        """El fondo por defecto es oscuro, no "el que ponga el navegador".

        `background` está en `body`, y `body` es lo que el navegador pinta antes
        de que llegue el CSS si el CSS tarda.
        """
        limpio = sin_comentarios(CSS.read_text(encoding="utf-8"))
        cuerpo = re.search(r"body\s*\{([^}]*)\}", limpio)
        self.assertIsNotNone(cuerpo)
        self.assertIn("background:", cuerpo.group(1))
        self.assertNotIn("background: #fff", limpio)


class TestLoQueDiceEsVerdad(unittest.TestCase):
    """Las cifras de la página se contrastan con el repo.

    Una página que dice "459 tests" y un día hay 460 no está desactualizada: está
    **mintiendo**, y quien la lee no tiene forma de saberlo. Este módulo lo
    comprueba extrayendo el número del documento y comparándolo con el repo.
    """

    def _tests_en_verde(self) -> int:
        """Cuántos tests hay, contados con la función del harness en un proceso
        limpio.

        Las dos soluciones obvias fallan, y las dos vale la pena documentar:

        - **Lanzar el harness y leer su salida** se cuelga. El harness incluye
          este fichero, así que el test espera a una suite que está esperando al
          test. Referencia circular, y se ve colgándose en lugar de leyéndose.
        - **`discover` dentro del proceso** da un número **distinto** del que
          imprime el harness, porque el módulo que se está ejecutando ya está
          importado. Contó 456 donde el harness contaba 475.

        Un subproceso limpio que llama a `build_suite` --la función del propio
        harness-- da el número correcto sin ejecutar nada.
        """
        import subprocess as sp

        programa = (
            "import sys;"
            f"sys.path.insert(0, {str(ROOT / 'tools')!r});"
            "from testharness import build_suite;"
            "print(build_suite().countTestCases())"
        )
        salida = sp.run([sys.executable, "-c", programa], cwd=ROOT,
                        capture_output=True, text=True, timeout=300)
        self.assertEqual(salida.returncode, 0, salida.stderr[-300:])
        return int(salida.stdout.strip())

    def _pasos_de_release(self) -> int:
        sys.path.insert(0, str(ROOT / "tools"))
        import release
        return len(release.PASOS)

    def _codigos_de_diagnostico(self) -> int:
        """Cuántos códigos emite el compilador, y no cuántos dice nadie.

        El recuento va al compilador entero, `wasm/` incluido: una etiqueta puede
        emitir un código desde el backend, y contar sólo el directorio de arriba
        daba un número más bajo que el de verdad.
        """
        vistos = set()
        for f in (ROOT / "compiler" / "sourc").rglob("*.py"):
            vistos |= set(re.findall(r'"(E0\d{3})"', f.read_text(encoding="utf-8")))
        return len(vistos)

    def _kbs_del_pyz(self) -> int:
        """El peso del compilador en un fichero, en KB, como lo muestra la página.

        Se mide el artefacto de verdad y no el número que dice nadie, que es
        justo lo que hace esta puerta con las otras cifras.
        """
        pyz = ROOT / "dist" / "sourc.pyz"
        self.assertTrue(pyz.is_file(),
                        "dist/sourc.pyz no está: corre `make pyz` antes de "
                        "mirar lo que dice la página")
        return pyz.stat().st_size // 1024

    def _bytes_del_pegamento(self) -> int:
        """El pegamento medido con la misma métrica que el techo del gate."""
        sys.path.insert(0, str(ROOT / "compiler"))
        from sourc.budget import comprimir_gzip
        pegamento = ROOT / "runtime" / "glue" / "glue_core.js"
        return comprimir_gzip(pegamento.read_bytes())

    def _programas_con_paridad(self) -> int:
        import json
        with (ROOT / "tests" / "parity.json").open(encoding="utf-8") as f:
            return len(json.load(f)["programas"])

    def test_cada_cifra_de_la_pagina_existe(self):
        """Una cifra en la página tiene que corresponded con algo del repo."""
        reales = {
            "tests": self._tests_en_verde(),
            "puertas antes de etiquetar": self._pasos_de_release(),
            "programas en el corpus":
                len(list((ROOT / "tests" / "corpus").glob("*.src"))),
            "programas con paridad": self._programas_con_paridad(),
            "códigos de diagnóstico": self._codigos_de_diagnostico(),
            "KB del compilador en un fichero": self._kbs_del_pyz(),
            "B gzip del pegamento": self._bytes_del_pegamento(),
        }
        valores = {v for v in reales.values()}
        documento = _construir()
        mostradas = [int(n) for n in re.findall(
            r'<span class="cifra">(\d+)</span>', documento)]
        self.assertTrue(mostradas, "la página no muestra ninguna cifra")
        for numero in mostradas:
            with self.subTest(cifra=numero):
                self.assertIn(
                    numero, valores,
                    f"la página dice {numero} y el repo tiene {reales}. "
                    f"Actualiza el DIM que corresponda en site/pagina.src. "
                    "Esta puerta está para eso: que la cifra que se publica "
                    "siga siendo verdad, y va a saltar cada vez que se añada "
                    "un test o una puerta, que es justo lo que significa.")

    def test_el_ejemplo_de_la_pagina_dice_lo_que_produce(self):
        """El bloque de ejemplo se ejecuta y tiene que dar lo que dice.

        Este es el caso que ya pasó: la página decía `3` y `4` donde el programa
        da `3.5` y `4`, porque `PRINT STR(...)` no imprime comillas y `/` es
        división real. Se inventó la salida de memoria, que es justo lo que hace
        mal una página.
        """
        documento = _construir()
        bloques = re.findall(r"<pre><code>(.*?)</code></pre>", documento, re.S)
        # El primero es el ejemplo, el segundo su salida.
        self.assertGreaterEqual(len(bloques), 2, "no hay ejemplo en la página")
        ejemplo = bloques[0].replace("&lt;", "<").replace("&gt;", ">")
        salida_demostrada = bloques[1].strip()

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "ejemplo.src"
            ruta.write_text(ejemplo + "\n", encoding="utf-8")
            resultado = subprocess.run(
                [sys.executable, "compiler/sourc.py", "run", str(ruta),
                 "--salida"],
                cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(resultado.returncode, 0, resultado.stderr[-400:])
        self.assertEqual(
            resultado.stdout.strip(), salida_demostrada,
            "la salida que muestra la página no es la que da el ejemplo. "
            "Ejecuta el bloque y copia lo que sale, no lo que te suena.")


class TestElProgramaNoSaleDeSource(unittest.TestCase):
    """La autenticidad del documento, y una consecuencia de la regla de la llave.

    ## Por qué esto es una puerta y no una curiosidad

    `{` abre interpolación en un literal de Source, y no hay `chr` ni ninguna
    nativa que produzca el carácter. Un programa Source, por tanto, **no puede
    emitir una llave**: no es que este programa decida no hacerlo, es que no
    puede.

    Así que si el documento tiene una llave fuera del CSS, alguien la escribió
    después de generarlo. Eso convierte una limitación del lenguaje en una prueba
    de que el HTML salió del programa y no de un editor de texto, y no cuesta
    nada: es un `count`.
    """

    def test_no_hay_una_llave_en_el_markup(self):
        documento = _construir()
        fuera = re.sub(r"<style>.*?</style>", "", documento, flags=re.S)
        self.assertNotIn("{", fuera,
                         "hay una llave fuera del CSS. Un literal de Source no "
                         "puede contenerla, así que el documento se editó a "
                         "mano después de generarse")

    def test_el_documento_no_trae_una_plantilla(self):
        """No hay `{{ }}`, ni `${}`, ni `%s`: no hay motor de plantillas.

        El documento lo calcula un programa. Si aparece un marcador de
        sustitución, es que alguien ha metido una capa que no debería estar, y
        esa capa es justo lo que el manifiesto dice que no hay.
        """
        documento = _construir()
        for marcador in ("{{", "}}", "${", "%s", "%d"):
            self.assertNotIn(marcador, documento,
                             f"aparece {marcador!r}: hay una plantilla donde "
                             "no debería haberla")

    def test_no_hay_script_en_la_pagina(self):
        """Ni un `<script>`, ni `onclick`.

        La página es un documento. Si algún día necesita interacción, eso es una
        decisión con su ADR, no algo que se cuela en un fichero generado -- y
        además `budgets.toml` no tiene ni una línea de presupuesto para código
        ejecutable de terceros.
        """
        documento = _construir()
        limpio = re.sub(r"<style>.*?</style>", "", documento, flags=re.S)
        self.assertNotIn("<script", limpio)
        self.assertNotIn("onclick", limpio)
        self.assertNotIn("onload", limpio)


if __name__ == "__main__":
    unittest.main()