"""El gate de pesos: que apriete, y que no mienta cuando no puede medir.

Un presupuesto que se informa pero no se comprueba no aprieta nada, y uno que
suma ceros de los artefactos que faltan aprueba una página a la que le falta la
mitad. Los dos fallos son silenciosos, y por eso tienen test.
"""

import tempfile
import unittest
from pathlib import Path

from sourc import budget

RAIZ = Path(__file__).resolve().parent.parent

PRESUPUESTO = """
schema = 1
tolerance = 0.02

[[budget]]
artifact = "main.wasm"
phase = 2
gzip = 100
raw = 400

[[budget]]
name = "pagina-wasm"
phase = 2
target = "wasm"
mode = "page"
example = "hola.src"
gzip = 300

# Muro por debajo del margen del objetivo: es la única configuración en la que
# se puede distinguir "pasó del objetivo" de "pasó del muro".
[[budget]]
name = "con-muro"
phase = 2
target = "wasm"
mode = "page"
example = "muro.src"
gzip = 400
hard_gzip = 405

[[budget]]
artifact = "source_glue.js"
phase = 0
note = "limite de lineas"
max_source_lines = 300
"""


def _presupuestos():
    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / "budgets.toml"
        ruta.write_text(PRESUPUESTO, encoding="utf-8")
        return budget.cargar(ruta)


class TestCarga(unittest.TestCase):
    def test_lee_las_dos_clases(self):
        tolerancia, presupuestos = _presupuestos()
        self.assertAlmostEqual(tolerancia, 0.02)
        clases = sorted({p.clase for p in presupuestos})
        self.assertEqual(clases, ["artefacto", "pagina"])

    def test_el_techo_de_lineas_no_es_un_peso(self):
        """`max_source_lines` mide líneas, no bytes: confundirlos daría un
        informe que dice 'bytes' sobre un número de líneas."""
        _, presupuestos = _presupuestos()
        lineas = next(p for p in presupuestos if p.max_source_lines)
        self.assertIsNone(lineas.gzip)
        self.assertEqual(lineas.max_source_lines, 300)

    def test_el_repositorio_tiene_un_gate_que_aprieta(self):
        """Los presupuestos del repo tienen que ser legibles y tener techo."""
        _, presupuestos = budget.cargar(RAIZ / "budgets.toml")
        self.assertTrue(presupuestos)
        for presupuesto in presupuestos:
            self.assertTrue(
                presupuesto.gzip or presupuesto.hard_gzip
                or presupuesto.raw or presupuesto.max_raw
                or presupuesto.max_source_lines,
                f"{presupuesto.nombre} no tiene ningún techo")


class TestTolerancia(unittest.TestCase):
    def test_dentro_de_la_tolerancia_no_falla(self):
        _, presupuestos = _presupuestos()
        main = next(p for p in presupuestos if p.artifact == "main.wasm")
        informe = budget.Informe()
        # 102 es un 2% por encima de 100: dentro, y con razón, porque la
        # tolerancia existe para el ruido de la compresión.
        budget._comprueba_peso(main, "x", budget.Size(400, 102), 0.02)
        self.assertEqual(informe.incumplimientos, [])

    def test_fuera_de_la_tolerancia_falla(self):
        _, presupuestos = _presupuestos()
        main = next(p for p in presupuestos if p.artifact == "main.wasm")
        fallo = budget._comprueba_peso(main, "x", budget.Size(400, 150), 0.02)
        self.assertIsNotNone(fallo)

    def test_el_muro_ignora_la_tolerancia(self):
        """`hard_gzip` es un muro, no un objetivo: pasarse no se tolera.

        402 está dentro del margen del objetivo (400 con un 2%) pero por encima
        del muro (405 no, 405 queda arriba)... concretamente: 404 pasa el
        objetivo con margen y falla por el muro.
        """
        _, presupuestos = _presupuestos()
        muro = next(p for p in presupuestos if p.nombre == "con-muro")
        ok = budget._comprueba_peso(muro, "x", budget.Size(999, 404), 0.02)
        self.assertIsNone(ok)
        fallo = budget._comprueba_peso(muro, "x", budget.Size(999, 406), 0.02)
        self.assertIsNotNone(fallo)
        self.assertIn("muro", str(fallo))


class TestLoQueNoSePuedeMedir(unittest.TestCase):
    """El caso importante: lo que falta no se cuenta como cero."""

    def test_un_artefacto_ausente_no_se_da_por_cumplido(self):
        _, presupuestos = _presupuestos()
        pagina = next(p for p in presupuestos if p.nombre == "pagina-wasm")
        informe = budget.Informe()
        with tempfile.TemporaryDirectory() as tmp:
            vacio = Path(tmp)
            budget.comprobar_programa(Path("hola.src"), b"\x00asm\x01\x00\x00\x00",
                                      vacio, presupuestos, 0.02, informe)
        # El programa entra en la suma, pero el glue y la página no están.
        self.assertTrue(any("hola.src" in linea for linea, _, _ in informe.filas))
        self.assertTrue(informe.sin_medir)
        pagina_falsa = [p for p in informe.sin_medir if "pagina-wasm" in p]
        self.assertTrue(pagina_falsa)

    def test_sin_brotli_el_gate_no_se_enterra(self):
        """`brotli` era el techo y su ausencia **fallaba** el gate.

        Éste es el defecto que motivó el cambio: `Size(10, None)` significaba
        "10 B raw y no medido", y con `brotli` como techo eso era un
        incumplimiento. Un `make size` en una máquina sin el binario no podía
        pasar, y un gate que no puede pasar en una máquina soportada no es un
        gate.
        """
        _, presupuestos = _presupuestos()
        main = next(p for p in presupuestos if p.artifact == "main.wasm")
        sin_brotli = budget.Size(raw=10, gzip=10, brotli=None)
        self.assertIsNone(budget._comprueba_peso(main, "x", sin_brotli, 0.02),
                          "sin brotli el gate tiene que seguir cumpliendo")

    def test_brotli_no_es_techo_de_ninguna_entrada(self):
        """`brotli` es informativa. Si alguna vez vuelve a ser techo, el gate
        vuelve a depender de una herramienta del sistema.

        Es el mismo motivo por el que existe el campo: está en el dataclass para
        poder *informar*, no para poder *medir*. Un campo que sólo se lee es
        documentación; uno que además_aprieta es una dependencia.
        """
        _, presupuestos = budget.cargar(RAIZ / "budgets.toml")
        import tomllib
        datos = tomllib.loads((RAIZ / "budgets.toml").read_text(encoding="utf-8"))
        for entrada in datos.get("budget", []):
            for clave in ("brotli", "hard_brotli"):
                self.assertNotIn(clave, entrada,
                                 f"la entrada {entrada.get('artifact') or entrada.get('name')} "
                                 f"usa `{clave}`, y eso hace depender el gate de "
                                 "un binario del sistema")
        # Y el dataclass lo guarda, pero `_comprueba_peso` no lo mira.
        # `hard_brotli` ya **no existe** en el dataclass: se quitó al cambiar el
        # gate. Eso es mejor que comprobar que está inerte --no se puede usar por
        # accidente porque no se puede nombrar--, y por eso el test de arriba
        # mira el TOML y no el dataclass.
        self.assertFalse(hasattr(budget.Budget, "hard_brotli"),
                         "`hard_brotli` no debe volver al dataclass")
        base = next((p for p in presupuestos if p.artifact == "main.wasm"), None)
        if base is not None:
            main = budget.Budget(clase=base.clase, nombre=base.nombre,
                                 fase=base.fase, artifact=base.artifact,
                                 gzip=base.gzip, hard_gzip=base.hard_gzip,
                                 raw=base.raw, brotli=1)
            # Por debajo del techo de gzip y por encima del de brotli: si
            # `brotli` apretara, esto fallaría.
            medida = budget.Size(raw=200, gzip=200, brotli=99_999)
            self.assertIsNone(
                budget._comprueba_peso(main, "x", medida, 0.02),
                "un `brotli` absurdamente bajo tiene que ser inerte")

    def test_gzip_nunca_es_none(self):
        """La métrica del gate no puede faltar: `gzip` es de la stdlib.

        `Size.gzip` es `int`, no `int | None`, justamente para que no exista el
        camino en el que el gate se queda sin medir. `brotli` es el que puede
        faltar, y por eso es `int | None`.
        """
        medido = budget.medir(b"un artefacto cualquiera")
        self.assertIsInstance(medido.gzip, int)
        self.assertIsInstance(medido.raw, int)


class TestGateReal(unittest.TestCase):
    """El gate sobre el repo de verdad, no sobre un fichero inventado."""

    def test_el_glue_cumple_el_techo_de_lineas(self):
        _, presupuestos = budget.cargar(RAIZ / "budgets.toml")
        informe = budget.Informe()
        budget.comprobar_estructura(RAIZ, presupuestos, informe)
        lineas = [f for f in informe.incumplimientos if "línea" in str(f)]
        self.assertEqual(lineas, [],
                         f"el glue se pasó de líneas: {lineas}")

    def test_los_programas_del_corpus_cumplen(self):
        """El estado real del backend contra el presupuesto real.

        Es un test lento: compila y comprime todos los programas. Es el único
        sitio donde se comprueba que el tamaño de la Fase 2 no se ha ido, que es
        la métrica que el gate existe para vigilar.
        """
        import sys
        sys.path.insert(0, str(RAIZ / "tools"))
        import parity                                     # noqa: PLC0415

        _, presupuestos = budget.cargar(RAIZ / "budgets.toml")
        informe = budget.Informe()
        for path in parity.programas():
            modulo = parity.construir(path)
            wasm = parity.compile_module(modulo)
            budget.comprobar_programa(path, wasm, RAIZ, presupuestos, 0.02,
                                      informe)
        if __name__.endswith("test_budget.py"):
            self.assertEqual(
                [str(f) for f in informe.incumplimientos], [],
                "hay programas por encima del techo de pesos")

class TestPresupuestoPorPrograma(unittest.TestCase):
    """Un techo por programa, y el general como respaldo.

    No todos los programas del corpus son comparables: `hola-mundo` y un
    programa que existe para ejercitar veinte operaciones no pueden compartir
    número. Con un único techo global, cumplir el grande obligaba a recortarle
    cobertura al gate.
    """

    def _presupuestos(self, *artefactos):
        return [budget.Budget(clase="artefacto", nombre=a, fase=2, artifact=a,
                              gzip=1000, hard_gzip=2000)
                for a in artefactos]

    def _informe(self, presupuestos):
        """Un informe con el módulo de `hola-mundo` dentro.

        Se mide el módulo real; lo que se prueba es **qué presupuesto se
        elige**, así que el tamaño da igual.
        """
        import sys
        sys.path.insert(0, str(RAIZ / "tools"))
        import parity                                    # noqa: PLC0415

        programa = RAIZ / "tests" / "corpus" / "01_hola.src"
        wasm = parity.compile_module(parity.construir(programa))
        informe = budget.Informe()
        budget.comprobar_programa(programa, wasm, RAIZ, presupuestos, 0.02,
                                  informe)
        return informe

    def test_el_propio_manda_sobre_el_general(self):
        # Los dos techos miran lo mismo, así que sólo se puede distinguir cuál
        # manda si uno de los dos no se cumple: el propio está a 900 B.
        propios = self._presupuestos("main.wasm")
        propios.append(budget.Budget(
            clase="artefacto", nombre="propio", fase=2,
            artifact="main.wasm de 01_hola.src", gzip=1, hard_gzip=2000))
        # `hola-mundo` pesa unos 150 B: el general de 1000 se cumple y el
        # propio de 1 no. Si ganara el general no habría ninguno.
        propios[0] = budget.Budget(clase="artefacto", nombre="general", fase=2,
                                   artifact="main.wasm", gzip=1000,
                                   hard_gzip=2000)
        informe = self._informe(propios)
        self.assertEqual(len(informe.incumplimientos), 1,
                         "el techo propio de 900 B es el que tiene que mandar")
        self.assertIn("01_hola.src", str(informe.incumplimientos[0]))

    def test_sin_techo_propio_manda_el_general(self):
        """Sin entrada para el programa, el general es el que se aplica."""
        informe = self._informe(self._presupuestos("main.wasm"))
        # `hola-mundo` pesa 122 B gzip, así que el general de 1000 se
        # cumple y no hay nada que reportar.
        self.assertEqual(informe.incumplimientos, [], "el general de 1000 "
                         "no se incumple")


if __name__ == "__main__":
    unittest.main()

class TestLasPiezasDeLaPagina(unittest.TestCase):
    """Una página es su programa más **el glue que ese programa usa**.

    La lista era fija --`source_glue.js` y `index.html`-- y su docstring
    prometía lo contrario. El efecto era que el presupuesto de la Fase 3 para
    `laberinto.src` se comprobaba contra el glue del core: los números correctos
    para el programa equivocado, y `glue_2d.js` sin mirar por nadie. Eso es un
    gate que pasa midiendo otra cosa, que es peor que un gate que no pasa.
    """

    def test_el_core_siempre(self):
        self.assertEqual(budget._piezas_de_la_pagina([]),
                         ["source_glue.js", "index.html"])

    def test_un_modulo_suma_sus_artefactos(self):
        piezas = budget._piezas_de_la_pagina(["SOURCE_2D"])
        self.assertIn("glue_2d.js", piezas)
        self.assertIn("source_2d.wasm", piezas)
        self.assertIn("source_glue.js", piezas)

    def test_un_programa_sin_modulos_no_arrastra_pegs_remotos(self):
        """El core es 2824 B; `glue_2d.js` son ~6 KB. Que no entre por defecto es
        lo que sostiene que el hola mundo sea pequeño."""
        piezas = budget._piezas_de_la_pagina([])
        self.assertNotIn("glue_2d.js", piezas)
        self.assertNotIn("glue_3d.js", piezas)
        self.assertNotIn("glue_web.js", piezas)

    def test_la_lista_no_depende_del_orden_del_use(self):
        """Un informe cuya lista cambia con el orden del fuente no se puede
        comparar entre dos ejecuciones."""
        uno = budget._piezas_de_la_pagina(["SOURCE_2D"])
        dos = budget._piezas_de_la_pagina(["SOURCE_2D", "SOURCE_WEB"])
        tres = budget._piezas_de_la_pagina(["SOURCE_WEB", "SOURCE_2D"])
        self.assertEqual(dos, tres)
        self.assertLess(len(uno), len(dos))

    def test_no_hay_duplicados(self):
        piezas = budget._piezas_de_la_pagina(["SOURCE_2D", "SOURCE_2D"])
        self.assertEqual(len(piezas), len(set(piezas)))

    def test_laberintto_aun_no_se_puede_midir_y_se_dice(self):
        """El estado actual, comprobado de verdad.

        `laberinto.src` usa `SOURCE_2D`, `glue_2d.js` no existe, y por eso su
        página se anota como **no medida** en vez de medirse contra el glue del
        core. El día que la Fase 3 cree el fichero, este test falla y dice qué
        rebasar -- que es el aviso que el `note = "proyeccion"` de la entrada
        promete.
        """
        import sys
        sys.path.insert(0, str(RAIZ / "tools"))
        import parity                                    # noqa: PLC0415

        _, presupuestos = budget.cargar(RAIZ / "budgets.toml")
        programa = RAIZ / "examples" / "laberinto.src"
        modulo = parity.construir(programa)
        self.assertIn("SOURCE_2D", modulo.runtime_modules,
                      "el fixture depende de que laberinto use SOURCE_2D")
        from sourc.wasm.backend import compile_module     # noqa: PLC0415
        informe = budget.Informe()
        budget.comprobar_programa(
            programa, compile_module(modulo), RAIZ, presupuestos, 0.02,
            informe, modulo.runtime_modules)

        faltan = {n for n in ("glue_2d.js", "source_2d.wasm")
                  if not budget.ruta_de_artefacto(RAIZ, n).is_file()}
        if faltan:
            self.assertTrue(
                any("laberinto-2d" in nota for nota in informe.sin_medir),
                f"con {sorted(faltan)} sin existir, la pagina de laberinto "
                f"tiene que quedar sin medir; sin_medir={informe.sin_medir}")
            self.assertFalse(
                any(nombre == "laberinto-2d" for nombre, _, _ in informe.filas),
                "y no debe aparecer como medida: seria medir el programa "
                "equivocado")
        else:
            self.fail(
                f"ya existen {sorted(faltan)}. Rebasa el presupuesto de "
                "`laberinto-2d` con la medida real y quita el `note` de "
                "proyeccion de `glue_2d.js` y `source_2d.wasm`.")
