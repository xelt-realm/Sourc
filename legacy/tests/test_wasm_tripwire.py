"""El backend no puede tener operaciones sin lowering.

Una lectura de "el backend baja 65 operaciones y compila" no significa nada: lo
que significa es "el backend baja 65 operaciones y el corpus sólo ejercita 65".
Las que falten fallan en el navegador, tres fases después, y sin un mensaje que
diga qué pasó. Este módulo cierra ese hueco.

Es el primer test de la Fase 2 y se escribe **antes** que el backend, por un
motivo concreto: si se escribe después, el backend queda escrito fiándose de la
memoria de qué cubre, y el tripwire sólo avisa cuando ya no queda nada que
arreglar.

Tres direcciones, y cada una falla por su cuenta:

1. **Toda operación que el frontend emite está declarada** en la tabla de
   colocación. Es el gate que bloquea el build.
2. **Toda declaración tiene una razón** (`POR_QUE`). Sin ella, una entrada
   puesta por intuición es deuda que nadie ve venir.
3. **Toda operación declarada se ejercita en algún sitio**, o aparece en la lista
   de las que sólo cubre la especificación. No es un fallo: es visibilidad. Una
   tabla con 30 operaciones sin ejercitar es una afirmación, no un hecho.

La diferencia entre (1) y (3) es el matiz que importa: lo que el corpus no toca
no está roto, pero tampoco está probado, y hay que poder distinguirlo de lo que
sí funciona.

Hay un cuarto módulo, `tests/test_optionalidad.py`, que vigila el mismo tipo de
agujero en el otro lado: que un test dependa de Node sin decirlo, y en una
máquina sin Node **error** en vez de saltarse. Un salto declarado es cobertura
ausente a la vista; un error es el build roto en el entorno que el proyecto dice
que tiene que seguir funcionando (ADR 0019, decisión 5).
"""

import re
import unittest
from pathlib import Path

from sourc import sir
from sourc.diagnostics import DiagnosticBag
from sourc.parser import parse
from sourc.types import check

ROOT = Path(__file__).resolve().parents[1]

# Control de flujo: lo emite el builder, no aparece como operación con tipo.
CONTROL_OPS = {"label", "br", "br_if.false", "ret", "call", "call.native",
               "newline"}


def fuentes():
    """El corpus completo más los ejemplos: es el contrato del frontend."""
    return (sorted((ROOT / "tests" / "corpus").glob("*.src"))
            + sorted((ROOT / "examples").glob("*.src")))


def operaciones_del_corpus() -> dict[str, int]:
    """Cada operación que el frontend emite de verdad, con cuántas veces."""
    usadas: dict[str, int] = {}
    for path in fuentes():
        source = path.read_text(encoding="utf-8")
        bag = DiagnosticBag()
        try:
            program = parse(source, bag)
        except Exception:
            continue
        if bag.has_errors():
            continue
        typing = check(program, bag)
        if bag.has_errors():
            continue
        try:
            module = sir.build(program, typing)
        except Exception:
            # GOTO y ARENA aún no bajan a IR; su error es intencionado.
            continue
        for func in module.funcs:
            for instr in func.instrs:
                usadas[instr.op] = usadas.get(instr.op, 0) + 1
    return usadas


def declaradas() -> set[str]:
    return (set(sir.INLINE_OPS) | set(sir.HOST_ABI) | set(sir.HOST_FORMATTING)
            | set(sir.OPS_DEL_MODULO) | CONTROL_OPS)


class TestTripsCompleto(unittest.TestCase):
    """Gate: ninguna operación del corpus puede quedar sin lowering."""

    def test_corpus_is_worth_measuring(self):
        # Si el corpus no emitiera nada, este módulo pasaría en verde sin
        # comprobar nada, que es la forma más común de un test inútil.
        usadas = operaciones_del_corpus()
        self.assertGreaterEqual(len(usadas), 40,
                                "el corpus debería ejercitar una parte "
                                "sustancial de las operaciones")
        self.assertEqual(usadas, operaciones_del_corpus(),
                         "debe ser determinista")

    def test_every_emitted_op_is_declared(self):
        usadas = set(operaciones_del_corpus())
        declarados = declaradas()
        sin_declarar = usadas - declarados
        self.assertEqual(
            sin_declarar, set(),
            "operaciones que el frontend emite y el backend no declara: "
            f"{sorted(sin_declarar)}.\n"
            "El backend no sabría ni que bajarlas. Añádelas a INLINE_OPS, "
            "HOST_ABI o HOST_FORMATTING en sourc/sir.py.")

    def test_every_op_defines_a_placement(self):
        """Cada operación dice de dónde sale: inline, host o módulo.

        Sin esta información, el backend no se puede escribir: no se sabe si hay
        que emitir una instrucción, declarar un import o generar una función.
        """
        declarados = declaradas()
        sin_colocacion = declarados - set(sir.INLINE_OPS) - set(sir.HOST_ABI) \
            - set(sir.HOST_FORMATTING) - set(sir.OPS_DEL_MODULO) - CONTROL_OPS
        self.assertEqual(sin_colocacion, set(),
                         f"declaradas sin colocación: {sorted(sin_colocacion)}")


class TestTripsInforme(unittest.TestCase):
    """Visibilidad, no gate: lo que la especificación declara y nadie ejercita."""

    def test_declared_but_unexercised_is_small(self):
        """Operaciones declaradas que ningún programa toca.

        No es un fallo: la especificación del IR es más ancha que el corpus, y
        tiene que serlo, porque las features de la Fase 4 van a emitir
        operaciones que hoy nadie usa. Lo que sí es un fallo es que aparezcan
        sin que nadie mire: un backend "completo" con 40 operaciones sin probar
        es una afirmación, no un hecho.

        Por eso no hay un número sino una **reserva con motivo**: cada
        operación sin ejercitar tiene que estar en `OPS_RESERVADOS` diciendo por
        qué no hay forma de emitirla. Un número se sube cuando molesta; una
        reserva obliga a escribir la razón, que es justo lo que no se puede
        hacer a ojo.
        """
        sin_ejercitar = declaradas() - set(operaciones_del_corpus())
        sin_reservar = sin_ejercitar - set(sir.OPS_RESERVADOS)
        self.assertEqual(
            sin_reservar, set(),
            f"operaciones declaradas, sin ejercitar y sin motivo:\n"
            f"  {sorted(sin_reservar)}\n"
            "O entran en el corpus, o se añaden a OPS_RESERVADOS en "
            "sourc/sir.py con la razón por la que ningún programa puede "
            "emitirlas.")

    def test_toda_reserva_dice_por_que(self):
        """Una reserva sin motivo es una lista de deseos, no una reserva."""
        sin_motivo = [op for op, motivo in sir.OPS_RESERVADOS.items()
                      if not motivo.strip()]
        self.assertEqual(sin_motivo, [],
                         f"reservas sin motivo: {sorted(sin_motivo)}")

    def test_una_reserva_que_el_corpus_ya_toca_se_borra(self):
        """La reserva se comprueba en las dos direcciones.

        Si un programa del corpus ya emite la operación, la reserva está
        caducada: alguien añadió la feature y no quitó la nota. Dejarla puesta
        esconde una operación que sí tiene productor, que es justo lo que el
        censo sirve para ver.
        """
        usadas = set(operaciones_del_corpus())
        caducadas = set(sir.OPS_RESERVADOS) & usadas
        self.assertEqual(caducadas, set(),
                         f"reservadas y ya ejercitadas: {sorted(caducadas)}. "
                         "Bórralas de OPS_RESERVADOS")

    def test_una_reserva_tiene_que_estar_declarada(self):
        """Reservar una operación que el IR no declara no la protege de nada."""
        huerfanas = set(sir.OPS_RESERVADOS) - declaradas()
        self.assertEqual(huerfanas, set(),
                         f"reservadas pero no declaradas: {sorted(huerfanas)}")

    def test_declared_ops_are_real_names(self):
        """Que no haya erratas en la tabla.

        Una tabla de colocación con un nombre mal escrito es peor que una tabla
        incompleta: parece completa.
        """
        validas = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
        for op in declaradas():
            with self.subTest(operacion=op):
                self.assertRegex(op, validas)


if __name__ == "__main__":
    unittest.main()

class TestNativasInlineConRespaldo(unittest.TestCase):
    """Una nativa INLINE necesita una instrucción WASM que la respalde.

    Esta comprobación existe por un error concreto: `sin` y `cos` estaban
    marcadas INLINE, y el MVP de WASM no tiene ninguna de las dos instrucciones.
    Nada lo detectaba, porque la tabla de colocación no se cruzaba con la del
    encoder y el programa compilaba igual: el backend simplemente no tenía
    dónde mirar.

    La lista `A MANO` son las nativas que el backend emite con varias
    instrucciones suyas, sin una única que las cubra. Para esas la comprobación
    es de presencia: que estén anotadas, no que exista opcode.
    """

    A_MANOS = {
        # Empaquetar un color son cuatro `i32.shl`/`or`: no hay instrucción.
        "rgb",
        # `min`/`max` tienen opcode de f32/f64 pero no de i64, y `abs` tampoco
        # tiene de i64 en el MVP. La variante entera se emite a mano o pasa al
        # host cuando se bajen estas ops; hasta entonces, anotadas.
        "min", "max", "clamp", "abs",
        # ADR 0021: `CHR` son siete instrucciones suyas --un `bump`, un
        # `i32.store8` y dos locales-- y no hay ningún opcode que la cubra. La
        # alternativa sería una tabla de 256 caracteres en los datos, que son 256
        # bytes en cada programa que use `CHR`, y el pegamento tiene un techo de
        # 2880. Se anota con su motivo, que es lo que este test pide.
        "chr",
    }

    def setUp(self):
        from sourc.wasm import encoder

        self.encoder = encoder

    def opcode_existe(self, name: str) -> bool:
        return hasattr(self.encoder, name)

    def inline(self) -> set:
        return {name for name, (placement, _) in sir.NATIVE_PLACEMENT.items()
                if placement is sir.INLINE}

    def test_toda_inline_que_no_esta_anotada_tiene_opcode(self):
        """Lo que no está en `A_MANOS` tiene que tener una instrucción.

        El encoder nombra los opcodes por tipo primero: `F64_SQRT`.
        """
        for name in sorted(self.inline() - self.A_MANOS):
            candidatos = [f"{tipo}_{name.upper()}" for tipo in ("F64", "F32")]
            self.assertTrue(
                any(self.opcode_existe(c) for c in candidatos),
                f"'{name}' está marcada INLINE pero el encoder no tiene "
                f"ninguna instrucción que la cubra ({candidatos}); añádela a "
                f"A_MANOS con el motivo, o márcala HOST")

    def test_la_lista_de_manos_no_se_enseguida(self):
        """La lista se queda corta sin querer, que es como empieza el error."""
        self.assertEqual(
            self.inline() & self.A_MANOS - {
                # Estas sí tienen opcode de flotante; lo que se emite a mano es
                # su variante entera, que el MVP no cubre.
                "min", "max", "clamp", "abs"},
            self.A_MANOS & {"rgb", "chr"},
            "A_MANOS enumera las nativas INLINE que el backend tiene que "
            "escribir con sus propias instrucciones")

    def test_sin_y_cos_estan_en_el_host(self):
        """El caso que motivó el test, fijado para que no vuelva."""
        for name in ("sin", "cos"):
            placement, import_name = sir.NATIVE_PLACEMENT[name]
            self.assertIs(placement, sir.HOST)
            self.assertEqual(import_name, f"source.{name}")


class TestProductoresYEmisiones(unittest.TestCase):
    """Lo que produce un valor tiene que estar en las dos tablas.

    `PRODUCING_OPS` dice qué instrucciones definen un valor, y es lo que hace
    que el backend le reserve un local. Si un op está en `INLINE_OPS` --que el
    backend tiene que bajar-- y no en `PRODUCING_OPS`, el backend lo emite, no le
    reserva sitio, y el fallo sale como un `KeyError` al escribir el resultado:
    a cientos de líneas de donde está la tabla.

    Pasó con `divfloor.f64`: `a DIV b` sobre DOUBLEs llegaba hasta el backend y
    moría con un error que nomentionaba la operación.
    """

    FAMILIA = ("add", "sub", "mul", "div", "rem", "idiv", "divfloor", "neg",
               "shl", "shr", "and", "or", "xor", "lt", "le", "gt", "ge",
               "conv")

    def test_la_familia_aritmetica_esta_en_las_dos_tablas(self):
        desde_inline = {op for op in sir.INLINE_OPS
                        if op.split(".")[0] in self.FAMILIA}
        sin_declarar = sorted(desde_inline - set(sir.PRODUCING_OPS))
        self.assertEqual(
            sin_declarar, [],
            "operaciones que el backend tiene que bajar y que no reservan "
            "local para su resultado:\n  " + "\n  ".join(sin_declarar))

    def test_todo_productor_del_ir_declara_donde_vive(self):
        """Y al revés: cada productor tiene que saber el backend si lo baja.

        `call.native` va al host y `to.str` también; lo que no puede pasar es un
        productor que no esté ni en `INLINE_OPS` ni en las tablas del host,
        porque entonces nadie sabe quién lo emite.
        """
        conocidos = set(sir.HOST_ABI) | set(sir.HOST_FORMATTING)
        # Una matemática que va al host se declara en NATIVE_PLACEMENT, no en
        # HOST_ABI: HOST_ABI es para operaciones del IR, y `sin.f64` sale como
        # una. Sin esta excepción el test las daría por huérfanas.
        nativas_al_host = {f"{nombre}.f64" for nombre, (plaza, _) in
                           sir.NATIVE_PLACEMENT.items() if plaza is sir.HOST}
        conocidos |= nativas_al_host
        sin_ruta = sorted(op for op in sir.PRODUCING_OPS
                          if op not in sir.INLINE_OPS
                          and op not in conocidos
                          and op != "call.native")
        self.assertEqual(sin_ruta, [], f"productores sin ruta de emisión: {sin_ruta}")


class TestGuardiasDeNode(unittest.TestCase):
    """Nada usa Node sin decirlo.

    Node es opcional por decisión (ADR 0019), y un test que lo use sin
    `@skipUnless` no se salta: **falla**. La diferencia no es cosmética, es la
    diferencia entre "esta máquina tiene 48 tests menos" y "el build se rompió
    en un entorno soportado", y el repo promises que lo segundo no pasa.
    """

    def test_ninguna_clase_usa_node_sin_guardia(self):
        sin_guardia = []
        for path in (ROOT / "tests").glob("test_*.py"):
            # Este fichero queda fuera: nombra a `run_in_node` en su propio
            # mensaje de fallo, y buscarse a sí mismo no encuentra nada.
            if path.name == Path(__file__).name:
                continue
            fuente = path.read_text(encoding="utf-8")
            # El decorador va **unido** a su clase: partir el fichero por
            # líneas que empiezan por `@` o por `class` los separaba, y la
            # guardia de una clase acababa protegiendo a la anterior.
            patron = re.compile(r"((?:^@[^\n]*\n)*)^class (\w+)", re.M)
            cortes = [(m.start(), m.end(), m.group(1), m.group(2))
                      for m in patron.finditer(fuente)]
            for indice, (_, corte, decoradores, nombre) in enumerate(cortes):
                fin_ = cortes[indice + 1][0] if indice + 1 < len(cortes) \
                    else len(fuente)
                if "run_in_node" not in fuente[corte:fin_]:
                    continue
                if "skipUnless" not in decoradores:
                    sin_guardia.append(f"{path.name}:{nombre}")
        self.assertEqual(
            sin_guardia, [],
            "clases que usan `run_in_node` sin `@skipUnless(NODE)`. Sin Node "
            "fallan en vez de saltarse:\n  "
            + "\n  ".join(sorted(sin_guardia)))
