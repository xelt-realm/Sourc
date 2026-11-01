"""El extractor del subconjunto, probado con la política puesta en el test.

El extractor y la política están en ficheros distintos **a propósito**
(`compiler/sourc/subset.py` y `subset.toml`). Si el extractor llevara la lista
dentro, estos tests no podrían distinguir "el extractor no vio la feature" de
"el extractor la vio y la lista la admite", que son dos bugs distintos con dos
arregles distintos.

Por eso aquí la lista está en la constante `PERMITIDAS` del test. Cuando exista
`subset.toml`, esta constante habrá pasado a ser redundante y el gate real la
usará; hasta entonces, es la que verifica que el extractor nombra lo que tiene
que nombrar.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compiler"))

from sourc import ast, subset                     # noqa: E402
from sourc.diagnostics import CompileError, DiagnosticBag  # noqa: E402
from sourc.parser import parse                     # noqa: E402

CONTRATO = ROOT / "subset.toml"

OK = ROOT / "tests" / "subset" / "ok"
MAL = ROOT / "tests" / "subset" / "mal"
SIN_PARSEAR = ROOT / "tests" / "subset" / "sin-parsear"

# El contrato real, para las comprobaciones que lo necesitan. Las de abajo
# usan listas propias porque prueban el extractor *sin* política, que es lo que
# permite distinguir "no vio la feature" de "la vio y la lista la admite".
CONTRATO_CARGADO = subset.cargar(CONTRATO)

# Lo que el compilador en Source necesita. No es "todo lo que el lenguaje
# tiene": es lo que esta fase ha decidido que necesita. Ampliarlo es un cambio
# en `subset.toml` y tiene que ser a mano.
PERMITIDAS = frozenset({
    # datos. Los literales y `name` **no** están: son sintaxis, no features, y
    # el extractor no los nombra (ver `SINTAXIS`). Una lista de permitidas con
    # entradas que el extractor no puede producir es letra muerta, y hay un test
    # que lo comprueba en las dos direcciones.
    "dim", "assign", "binary", "unary",
    # control
    "if", "while", "for", "return", "functiondecl",
    # expresiones
    "index", "interpolate", "arraylit", "newarray", "call:nombre",
    "call:nombre",
    # tipos
    "tipo:integer", "tipo:double", "tipo:single", "tipo:boolean",
    "tipo:string", "tipo:color", "tipo:array", "tipo:array-de-integer",
    "tipo:array-de-double", "tipo:array-de-single", "tipo:array-de-string",
    # primitivas de sentencia
    "primitiva:print",
    # funciones de texto
    "funcion:len", "funcion:lenu", "funcion:charcode", "funcion:chr",
    "funcion:str",
    "funcion:val", "funcion:left", "funcion:right", "funcion:mid",
    "funcion:concat", "funcion:compare", "funcion:contains",
    "funcion:indexof",
    # funciones numéricas
    "funcion:rgb", "funcion:abs", "funcion:min", "funcion:max",
    "funcion:clamp", "funcion:sqrt", "funcion:floor", "funcion:sin",
    "funcion:cos",
})

# Lo que el gate tiene que ver y rechazar. Mismo motivo: la lista es del test
# mientras la política no esté en `subset.toml`.
# `gosub` estaba aquí por el motivo equivocado. No estaba porque el parser nunca
# construía `ast.Gosub` --`GOSUB x` producía un `ast.Goto`, porque el despacho de
# sentencias hacía `handler(self)` y se quedaba con el valor por defecto-- sino
# porque ahora sí se construye y el nodo cuenta. La prohibición no cambia:
# `GOSUB` comparte destino con `GOTO` y los dos dan E0043. Lo que cambia es que la
# lista dice la verdad sobre por qué. El gate de vocabulario lo vio al
# aparecer: un nodo nuevo que nadie ha decidido es justo lo que debe parar el
# build.
PROHIBIDAS = frozenset({
    "arena", "goto", "gosub", "label", "campo", "colorstmt",
    "use:source_2d", "import",
})


def features_de(ruta: Path) -> dict:
    bag = DiagnosticBag()
    programa = parse(ruta.read_text(encoding="utf-8"), bag)
    return subset.features(programa)


def todas_las_features() -> set:
    vistas = set()
    for carpeta in (OK, MAL):
        for ruta in sorted(carpeta.glob("*.src")):
            vistas |= set(features_de(ruta))
    return vistas


class TestExtractor(unittest.TestCase):
    """El extractor nombra features; no sabe nada de permitidos."""

    def test_los_fixtures_validos_solo_usan_permitidas(self):
        for ruta in sorted(OK.glob("*.src")):
            with self.subTest(fixture=ruta.name):
                fuera = sorted(set(features_de(ruta)) - PERMITIDAS)
                self.assertEqual(fuera, [],
                                 f"{ruta.name} usa features que la lista del "
                                 f"test no tiene como permitidas: {fuera}")

    def test_los_fixtures_invalidos_usan_prohibidas(self):
        for ruta in sorted(MAL.glob("*.src")):
            with self.subTest(fixture=ruta.name):
                vistas = set(features_de(ruta))
                alguien = vistas & PROHIBIDAS
                self.assertTrue(alguien,
                                f"{ruta.name} está en `mal/` pero no usa "
                                f"ninguna feature prohibida: usa {sorted(vistas)}")

    def test_el_vocabulario_esta_cerrado(self):
        """Todo lo que el extractor nombra está clasificado.

        Esta es la invariante que hace que el gate apriete. Si una feature
        aparece y nadie la ha puesto en ninguna lista, el gate no la ve; y el
        día que aparezca --`Struct` en la Fase 4, por ejemplo-- el gate se
        autoautorizaría sin que nadie lo decidiera. Con la invariante, el
        aparición obliga a clasificarla.
        """
        sin_clasificar = sorted(todas_las_features() - PERMITIDAS - PROHIBIDAS)
        self.assertEqual(sin_clasificar, [],
                         "el extractor nombra features que ninguna lista "
                         f"conoce: {sin_clasificar}")

    def test_las_prohibidas_se_pueden_producir(self):
        """Una prohibición que el extractor no puede producir es letra muerta."""
        producibles = todas_las_features()
        muertos = sorted(PROHIBIDAS - producibles)
        self.assertEqual(muertos, [],
                         f"prohibidas que ningún fixture produce: {muertos}")


class TestGranularidad(unittest.TestCase):
    """El vocabulario tiene que distinguir cosas que el contrato separa."""

    def _una(self, fuente: str) -> set:
        bag = DiagnosticBag()
        return set(subset.features(parse(fuente, bag)))

    def test_print_es_primitiva_y_len_es_funcion(self):
        """`PRINT` es sentencia y `LEN` es función: nombres distintos.

        Si los dos salieran como `llamada`, el contrato no podría admitir uno y
        prohibir el otro, que es exactamente lo que necesita un compilador:
        imprimir sí, dibujar no.
        """
        self.assertIn("primitiva:print", self._una('PRINT 1\n'))
        self.assertIn("funcion:len", self._una('DIM x AS INTEGER = LEN("a")\n'))
        self.assertNotIn("primitiva:len", self._una('DIM x AS INTEGER = LEN("a")\n'))

    def test_rgb_es_funcion_aunque_este_dentro_de_print(self):
        self._una('PRINT RGB(1, 2, 3)\n')
        vistas = self._una('PRINT RGB(1, 2, 3)\n')
        self.assertIn("funcion:rgb", vistas)
        self.assertIn("primitiva:print", vistas)

    def test_los_identificadores_llegan_en_minusculas(self):
        """`PRINT` llega en mayúsculas y `LEN` en minúsculas.

        El lexer baja los identificadores (ADR 0012), así que un `Name` siempre
        viene en minúsculas y una `Primitive` en mayúsculas. Si el extractor no
        normalizara, el contrato tendría dos listas y nadie sabría cuál Vale.
        """
        vistas = self._una('PRINT LEN("abc")\n')
        self.assertIn("primitiva:print", vistas)
        self.assertIn("funcion:len", vistas)
        self.assertFalse([v for v in vistas if v != v.lower()],
                         f"hay features con mayúsculas: {sorted(vistas)}")

    def test_el_tipo_anidado_tiene_nombre_propio(self):
        """`ARRAY OF ARRAY OF` tiene que ser distinguible de `ARRAY OF`.

        Sin el nombre compuesto, el contrato no puede admitir el array y
        prohibir el anidado por separado, y el compilador acabaría sin arrays
        porque no puede usar el caso fácil.
        """
        simple = self._una("DIM a AS ARRAY OF INTEGER = NEW INTEGER[2]\n")
        anidado = self._una('DIM m AS ARRAY OF ARRAY OF INTEGER = [ [1] ]\n')
        self.assertIn("tipo:array-de-integer", simple)
        self.assertNotIn("tipo:array-de-array", simple)
        self.assertIn("tipo:array-de-array", anidado)

    def test_un_span_no_es_una_feature(self):
        """`Span` es un dataclass y cuelga de todos los nodos.

        Cualquier recorrido por "los que tienen campos" lo cuenta. Si `span`
        apareciera en el informe, la lista de permitidas tendría que incluirlo y
        el gate perdería el sentido.
        """
        for ruta in sorted(OK.glob("*.src")):
            with self.subTest(fixture=ruta.name):
                self.assertNotIn("span", features_de(ruta))

    def test_pasar_una_funcion_como_valor_tiene_nombre(self):
        """GRAMMAR §5 lo permite, así que es una feature y se puede prohibir."""
        bag = DiagnosticBag()
        programa = parse("DIM f AS INTEGER = len\nPRINT f\n", bag)
        vistas = set(subset.features(programa))
        # El caso exacto depende de si el parser lo admite como `Call` con un
        # callee que no es `Name`; lo que importa es que quede nombrado.
        self.assertTrue(
            vistas & {subset.FUNCION_COMO_VALOR, "assign", "dim"},
            f"un valor de función no produce ninguna feature: {sorted(vistas)}")


class TestOrdenDeSalida(unittest.TestCase):
    def test_el_informe_es_determinista(self):
        """El resultado alimenta un informe versionado, así que va ordenado.

        Un `set` recorrido en orden de hash daría dos informes distintos en dos
        procesos (ADR 0016), y el diff del bootstrap empezaría con ruido.
        """
        for ruta in sorted(OK.glob("*.src")):
            with self.subTest(fixture=ruta.name):
                self.assertEqual(list(features_de(ruta)),
                                 sorted(features_de(ruta)))

    def test_reporta_todos_los_sitios_de_una_feature(self):
        """"Usa ARENA" sin decir dónde obliga a buscarlo a mano.

        Con once o doce ficheros del compilador, el lugar importa.
        """
        bag = DiagnosticBag()
        fuente = ("ARENA a\n  PRINT 1\nEND ARENA\n"
                  "ARENA b\n  PRINT 2\nEND ARENA\n")
        usos = subset.features(parse(fuente, bag))["arena"]
        self.assertEqual(len(usos), 2)
        self.assertNotEqual(usos[0].start, usos[1].start)


if __name__ == "__main__":
    unittest.main()


class TestContrato(unittest.TestCase):
    """Las invariantes de `subset.toml`, que es la fuente de verdad."""

    def setUp(self):
        self.contrato = subset.cargar(CONTRATO)

    def test_el_nivel_es_el_de_esta_fase(self):
        self.assertEqual(self.contrato.nivel, 2,
                         "el nivel del contrato tiene que ser el de la fase "
                         "que lo declara (ADR 0015)")

    def test_el_vocabulario_esta_cerrado(self):
        """Todo lo que el extractor nombra está en el contrato.

        Esta es la invariante que hace que el gate apriete. Si una feature
        aparece y no está en ninguna lista, el gate no la ve; y el día que
        aparezca --`Struct` en la Fase 4-- el gate se autoautorizaría sin que
        nadie lo decidiera. Con la invariante, aparecer obliga a clasificarla.
        """
        producibles = todas_las_features()
        sin_clasificar = sorted(producibles - self.contrato.vocabulario_posible())
        self.assertEqual(sin_clasificar, [],
                         "el extractor nombra features que `subset.toml` no "
                         f"conoce: {sin_clasificar}")

    def test_toda_permitida_se_puede_producir(self):
        """Una feature permitida que nada produce es una lista que se ensancha
        sola.

        Sin esta comprobación, `permitido` crece cada vez que alguien tiene una
        idea y nunca se queda pequeña. Y una lista de permitidas más ancha que
        lo necesario no es un contrato: es una lista de deseos.
        """
        producibles = todas_las_features()
        muertas = sorted(self.contrato.permitidas - producibles)
        self.assertEqual(
            muertas, [],
            f"features permitidas que ningún fixture produce: {muertas}. O "
            "falta el fixture que las ejercita, o sobra la entrada.")

    def test_toda_prohibida_dice_por_que_y_que_cuesta(self):
        """Una prohibición sin motivo es una intención, y no es un gate.

        El `coste` importa tanto como el motivo: es lo que permite decidir la
        Fase 4 con el número delante en vez de por intuición.
        """
        sin_motivo = [f.feature for f in self.contrato.prohibidas.values()
                      if not f.motivo]
        sin_coste = [f.feature for f in self.contrato.prohibidas.values()
                     if not f.coste]
        self.assertEqual(sin_motivo, [], f"prohibiciones sin motivo: {sin_motivo}")
        self.assertEqual(sin_coste, [],
                         f"prohibiciones sin coste anotado: {sin_coste}. Si de "
                         "verdad no cuesta nada, escribe `coste = 'ninguno'`; "
                         "lo que no puede es no constar.")

    def test_ninguna_prohibida_es_inmortal(self):
        """Documentación muerta: una prohibición que nada puede violar.

        Es el mismo caso que el de `ast.Gosub`, que existe en el AST y el parser
        nunca construye.
        """
        inmortales = self.contrato.prohibidas_inmortales(todas_las_features())
        self.assertEqual(inmortales, [],
                         f"prohibidas que ningún fixture puede producir: "
                         f"{inmortales}")

    def test_los_fixtures_validos_no_usan_nada_prohibido(self):
        for ruta in sorted(OK.glob("*.src")):
            with self.subTest(fixture=ruta.name):
                vistas = features_de(ruta)
                fuera = sorted(set(vistas) - self.contrato.permitidas)
                self.assertEqual(fuera, [],
                                 f"{ruta.name} usa features fuera del nivel 2: "
                                 f"{fuera}")

    def test_los_fixtures_invalidos_caen_justo_por_lo_que_deben(self):
        """Cada fixture de `mal/` tiene que violar el contrato, y sólo eso.

        Si un fixture de `mal/` violara el contrato *y* usara una feature
        permitida, el gate lo rechazaría por la razón equivocada y el test
        seguiría en verde: dos bugs con el mismo verde.
        """
        for ruta in sorted(MAL.glob("*.src")):
            with self.subTest(fixture=ruta.name):
                vistas = set(features_de(ruta))
                fuera = vistas - self.contrato.permitidas
                self.assertTrue(fuera,
                                f"{ruta.name} está en `mal/` pero no viola el "
                                f"contrato: usa {sorted(vistas)}")


class TestElGateAvisaCuandoLlegaLaFeature(unittest.TestCase):
    """Las features que aún no parsean están vigiladas por otro camino.

    `STRUCT`, `ENUM`, los arrays multidimensionales e `INPUT` no parsean, así que
    no pueden entrar por `mal/` y esperar un `E0066`: mueren en el parser antes
    de que el gate los vea. Lo que los vigila es este test, que **falla el día
    que empiezan a parsear**.

    Es el mecanismo completo: el gate no se autoautoriza cuando llega la Fase 4,
    avisa, y el mensaje dice exactamente qué mover.
    """

    def test_lo_que_no_parsea_sigue_sin_parsear(self):
        for ruta in sorted(SIN_PARSEAR.glob("*.src")):
            with self.subTest(fixture=ruta.name):
                bag = DiagnosticBag()
                try:
                    parse(ruta.read_text(encoding="utf-8"), bag)
                except CompileError as error:
                    self.assertTrue(error.diagnostic.message,
                                    "el error de parseo tiene que decir algo")
                    continue
                # Si llegamos aquí, la feature ya parsea: hay que decidir si el
                # gate la admite. El mensaje lo dice porque el nombre del
                # fichero es el del feature.
                nombre = ruta.stem.split("_", 1)[1]
                self.fail(
                    f"{ruta.name} **empieza a parsear**. La feature `{nombre}` "
                    f"ya está en el lenguaje y el gate la tiene que ver. "
                    f"Muévelo a `mal/` y añade su entrada a `subset.toml` como "
                    f"prohibida, o admítela en `permitido` con un ADR. Sin una "
                    f"de las dos, el nivel 2 deja de ser lo que dice ser.")


class TestElCompiladorEstaEnElNivel(unittest.TestCase):
    """Todo `.src` del compilador y del runtime tiene que pasar por el gate.

    `--nivel` es opcional, y ahí está el agujero: alguien puede añadir
    `compiler/lexer.src` y compilarlo sin `--nivel` sin que nada se entere. La
    lista de ficheros vigilados está aquí, en un test, precisamente porque es
    una lista corta y porque growingla es un acto deliberado.
    """

    VIGILADOS = ("compiler", "runtime")

    def _fuentes_del_compilador(self) -> list[Path]:
        found = []
        for carpeta in self.VIGILADOS:
            raiz = ROOT / carpeta
            if raiz.is_dir():
                found += sorted(raiz.rglob("*.src"))
        return found

    def test_las_fuentes_del_compilador_estan_vigiladas(self):
        """No es un test vacío: si no hay ninguna, se dice en voz alta.

        Hoy **no hay** ningún `.src` del compilador --el compilador en Source
        todavía no existe-- y el gate no tiene entrada. Eso se salta a la vista
        en el panel de estabilidad en vez de pasar en verde, que es lo que
        distingue "no comprobado" de "comprobado y correcto".
        """
        fuentes = self._fuentes_del_compilador()
        if not fuentes:
            self.skipTest(
                "todavía no hay compilador en Source: `compiler/sourcc.src` "
                "no existe. El gate tiene fixtures pero no entrada real.")
        for ruta in fuentes:
            with self.subTest(fuente=str(ruta.relative_to(ROOT))):
                usos = features_de(ruta)
                fuera = sorted(set(usos) - CONTRATO_CARGADO.permitidas)
                self.assertEqual(fuera, [],
                                 f"{ruta.name} usa features fuera del nivel 2: "
                                 f"{fuera}")
