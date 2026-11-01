"""La cobertura tiene que medirse por **feature**, no por operación del IR.

Este módulo existe por un agujero concreto que costó siete bugs. El tripwire
(`tests/test_wasm_tripwire.py`) mide operaciones del IR y funciona: avisó de que
el backend no bajaba `ABS`, `MIN`, `MAX` y `CLAMP`. Pero mide un nivel demasiado
somero, porque **`call.native` es una sola operación que esconde veintitrés
funciones de la lengua**. El tripwire veía `call.native` ejercitada --lo estaba,
por `RGB`-- y no tenía forma de saber que `ABS` no lo estaba. Con 392 tests en
verde, `sourc check` aceptaba `ABS(-5)` y `sourc build` lo rechazaba con E0058.

Y no era un caso perdido: de las 23 funciones nativas de `types.py`, **7 no las
demonstraba ningún programa** del corpus ni de los ejemplos. Cuatro eran las que
acabas de implementarse y tres (`SIN`, `COS`, `KEYDOWN`) llevaban meses
declaradas sin que nadie las usara.

## Por qué se mide en el AST y no en el IR

Medir en el IR obliga a mantener una tabla que diga qué operación baja cada
nativa, y esa tabla se desincroniza en silencio --es el mismo bug que
`decl_types` indexado por nombre, y salió el mismo día. Midiendo en el AST la
medición sale del propio fuente: si un programa llama a `ABS`, el nombre `ABS`
está en su árbol, y no hay nada que mantener.

## La reserva, y por qué no es una excusa

Una cobertura sin salida se incumple sola: el primer `--skip` y el resto se van
detrás. Por eso lo no cubierto tiene que estar **reservado con un motivo escrito**,
igual que `OPS_RESERVADOS` en el SIR: una reserva obliga a explicar, que es
justo lo que no se puede hacer a ojo. Y la reserva tiene que ser corta: si crece
de ocho a dieciséis, la puerta está midiendo mal y hay que arreglarla, no aceptarla.

Las dos direcciones importan. También falla si aparece un nodo o una nativa en
la reserva que ya no existe, porque una reserva caduca es ruido que esconde el
caso real.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compiler"))

from sourc import ast                                      # noqa: E402
from sourc.diagnostics import DiagnosticBag                # noqa: E402
from sourc.parser import parse                              # noqa: E402
from sourc.types import NATIVE_FUNCTIONS                    # noqa: E402


def fuentes():
    """El corpus completo más los ejemplos: es el contrato del frontend."""
    return (sorted((ROOT / "tests" / "corpus").glob("*.src"))
            + sorted((ROOT / "examples").glob("*.src")))


def recorrer(nodo):
    """Todos los nodos del AST.

    Ojo con el tipo del campo: `body` es **tuple**, no lista, porque los nodos
    son dataclasses congeladas. Recorrer sólo `list` encuentra el nodo raíz y
    nada más, y un test que pasa sin mirar nada es peor que no tener test.
    """
    yield nodo
    for campo in vars(nodo).values():
        if isinstance(campo, ast.Node):
            yield from recorrer(campo)
        elif isinstance(campo, (list, tuple)):
            for hijo in campo:
                if isinstance(hijo, ast.Node):
                    yield from recorrer(hijo)


# Nativas que la lengua declara y ningún programa demuestra, con el motivo.
#
# Corto a propósito: si esta lista crece, la puerta está mal y hay que arreglar
# la puerta. `SIN` y `COS` son las dos matematicas de host sin usar --`SQRT` y
# `FLOOR` sí están en el corpus--, y `KEYDOWN` es del lote de entrada del 2D.
NATIVAS_SIN_DEMOSTRAR = {
    "sin": "no la usa ningún programa; el corpus usa SQRT y FLOOR, que sí son "
           "instrucción de WASM. Es una matemática de host sin consumidor",
    "cos": "ídem `SIN`, y por el mismo motivo: sin consumidor",
    "keydown": "lote de entrada del 2D (Fase 3); la página lo ejercita en "
               "tests/test_page.py, no en el corpus",
}

# Nodos del EBNF que ningún programa demuestra, con el motivo.
#
# `Gosub` estaba aquí porque el parser no lo construía: `GOSUB x` salía como un
# nodo `Goto`. Es un bug que encontró el volcado del AST --el judge-first
# pagando--, y ya no aplica: ahora hay nodo, y lo que hace falta es un programa
# que lo demuestre, que es lo que dice el resto de la lista.
SENTENCIAS_SIN_DEMOSTRAR = {
    "Gosub": "E0043: GOSUB no baja a IR todavia; cuando baje, el corpus tiene que "
             "tener un programa con una llamada y un retorno por etiqueta. Es el "
             "mismo E0043 que `Goto` pero un nodo distinto, y por eso necesita su "
             "propia entrada: reservarlo en la lista sin programa que lo "
             "demuestre seria dejar algo que no puede pasar",
    "Goto": "E0043: GOTO no baja a IR todavia; cuando baje, el corpus tiene que "
            "tener un programa con salto hacia atras",
    "Label": "misma razon que `Goto`: la etiqueta solo tiene sentido junto al "
             "salto, y si el salto no baja a IR la etiqueta tampoco se ejercita",
    "Arena": "Fase 4 (`ARENA`); el subconjunto nivel 2 lo prohibe con E0066, "
             "asi que ningun programa del corpus puede usarlo todavia",
    "ImportFrom": "necesita un modulo importable, y el primer modulo escrito en "
                  "Source llega en la Fase 4 con `ARENA` (ADR 0015)",
    "Field": "Fase 4 (`campo` de un STRUCT); el subconjunto nivel 2 lo prohibe "
             "con E0066 y no hay ningun STRUCT en el corpus mientras tanto",
}


class TestCoberturaPorFeature(unittest.TestCase):
    """Lo que la lengua declara tiene que estar demostrado o reservado."""

    def _arboles(self):
        for path in fuentes():
            bag = DiagnosticBag()
            try:
                yield path, parse(path.read_text(encoding="utf-8"), bag)
            except Exception:                       # noqa: BLE001
                continue

    def _nativas_usadas(self) -> set[str]:
        usadas: set[str] = set()
        for _path, prog in self._arboles():
            for nodo in recorrer(prog):
                if isinstance(nodo, ast.Call) and isinstance(nodo.callee, ast.Name):
                    usadas.add(nodo.callee.value)
        return usadas

    def _sentencias_usadas(self) -> set[str]:
        usadas: set[str] = set()
        for _path, prog in self._arboles():
            for nodo in recorrer(prog):
                usadas.add(type(nodo).__name__)
        return usadas

    def test_toda_nativa_declarada_esta_demostrada_o_reservada(self):
        sin_cubrir = set(NATIVE_FUNCTIONS) - self._nativas_usadas()
        sin_reservar = sin_cubrir - set(NATIVAS_SIN_DEMOSTRAR)
        self.assertEqual(
            sin_reservar, set(),
            "funciones de la lengua que ningún programa del corpus demuestra:\n"
            f"  {sorted(sin_reservar)}\n"
            "O entran en el corpus, o se añaden a NATIVAS_SIN_DEMOSTRAR con el "
            "motivo por el que ningún programa puede llamarlas")

    def test_la_reserva_de_nativas_no_tiene_nadas_obsoletas(self):
        """Una reserva que ya no aplica es ruido que esconde el caso real."""
        declaradas = set(NATIVE_FUNCTIONS)
        obsoletas = set(NATIVAS_SIN_DEMOSTRAR) - declaradas
        self.assertEqual(
            obsoletas, set(),
            f"reservadas pero ya no declaradas: {sorted(obsoletas)}")

    def test_la_reserva_de_nativas_es_corta(self):
        """Tres de veintitrés. Si sube a seis, la puerta está midiendo mal.

        El número no es un capricho: una reserva larga es una lista de features
        que la lengua promete y nadie entrega, y a partir de cierto tamaño lo
        útil es arreglar la puerta en vez de ampliar la excusa.
        """
        self.assertLessEqual(
            len(NATIVAS_SIN_DEMOSTRAR), 4,
            "la reserva de nativas ha crecido demasiado: escribe un programa "
            "del corpus para las que puedas y deja sólo las de verdad "
            "imposibles")

    def test_toda_sentencia_del_ebnf_esta_demostrada_o_reservada(self):
        from sourc.parser import NODES_DE_SENTENCIA

        usadas = self._sentencias_usadas()
        huerfanas = (set(NODES_DE_SENTENCIA) - usadas) - set(
            SENTENCIAS_SIN_DEMOSTRAR)
        self.assertEqual(
            huerfanas, set(),
            "nodos del EBNF que ningún programa demuestra:\n"
            f"  {sorted(huerfanas)}\n"
            "O entran en el corpus, o se añaden a SENTENCIAS_SIN_DEMOSTRAR con "
            "el motivo por el que ningún programa puede producirlos")

    def test_la_reserva_de_sentencias_no_tiene_nadas_obsoletas(self):
        from sourc.parser import NODES_DE_SENTENCIA
        declaradas = set(NODES_DE_SENTENCIA)
        obsoletas = set(SENTENCIAS_SIN_DEMOSTRAR) - declaradas
        self.assertEqual(
            obsoletas, set(),
            f"reservadas pero ya no en el EBNF: {sorted(obsoletas)}")

    def test_cada_reserva_explica_el_motivo(self):
        """Un motivo vacío es un `skip` con pasos extra."""
        for nombre, motivo in (list(NATIVAS_SIN_DEMOSTRAR.items())
                               + list(SENTENCIAS_SIN_DEMOSTRAR.items())):
            with self.subTest(reserva=nombre):
                self.assertGreater(len(motivo), 30,
                                   f"la reserva de '{nombre}' no explica nada")

    def test_el_corpus_no_esta_vacio(self):
        """Si `parse` fallara en todos, estas pruebas pasarían sin mirar nada.

        Es el fallo más probable de una puerta de cobertura: que la puerta sea
        verde porque no ha Nada que medir. Por eso hay que comprobar que se
        midió.
        """
        usadas = self._nativas_usadas()
        self.assertGreaterEqual(
            len(usadas & set(NATIVE_FUNCTIONS)), 15,
            "el corpus debería demostrar la mayoría de las nativas; si no, la "
            "puerta está midiendo una lista vacía")


if __name__ == "__main__":
    unittest.main()