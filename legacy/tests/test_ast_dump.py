"""La puerta del juez del parser: `sourc parse` y los goldens del AST.

Cuatro comprobaciones y las cuatro hacen falta, porque fallan de forma distinta:

1. **La tabla del volcado está completa.** `astdump.FORMATO` tiene que mencionar
   exactamente los campos de cada dataclass del AST. Sin esto, añadir un campo
   a un nodo y no volcarlo no falla nada: el campo sale como `-` en todos los
   goldens y nadie lo nota hasta que falta algo. Es la puerta más barata de las
   cuatro y la que más bugs futuros tapa.
2. **Ningún golden falta y ninguno sobra.** Un juez que crece a medias deja
   programas sin cubrir, y un golden sin programa sigue pasando porque ya nadie
   lo regenera.
3. **`sourc parse` reproduce el golden por el camino del comando.** El golden lo
   escribe una función interna; si se comparara contra ella, se compararía una
   cosa consigo misma. Por el comando, que es el que usa una persona.
4. **Cada nodo del AST aparece en algún golden.** Un nodo que el corpus no
   ejercita tiene un volcado que nadie ha visto nunca: funciona en el papel y no
   se ha ejecutado jamás. Los que faltaban están en `tests/casos_ast/`, que son
   de **sólo parseo**, porque hay nodos que parsean y no compilan --`ARENA` es de
   la Fase 4, `GOTO` y `GOSUB` son E0043, el acceso a campo es E0025-- y su AST
   necesita golden igual que el de los demás.

## Lo que este juez ya encontró

Escribiendo el volcado, antes de que existiera una línea de parser en Source,
apareció un bug real: **`GOSUB x` producía un nodo `Goto`**, porque el despacho
de sentencias hacía `handler(self)` y `parse_goto` se quedaba con su valor por
defecto `"GOTO"`. Dos puertas lo documentaban como rareza conocida. Estaba
enmascarado porque los dos dan E0043, y porque el nodo `Goto` ya estaba
reservado para E0043 de todos modos.

Es exactamente el argumento de escribir el juez primero: **un defecto del
Stage 0 que ningún test de Stage 0 podía ver**, porque para verlo hay que
mirar la forma del árbol y nadie miraba la forma del árbol.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compiler"))

CORPUS = ROOT / "tests" / "corpus"
EJEMPLOS = ROOT / "examples"
CASOS = ROOT / "tests" / "casos_ast"
GOLDEN = ROOT / "tests" / "golden" / "ast"


class TestElJuezDelAST(unittest.TestCase):
    def _programas(self) -> list[Path]:
        """Los que compilan y también los que sólo parsean.

        Los segundos existen porque hay nodos que no bajan a IR. Si esta puerta
        mirara sólo el corpus, el AST de `ARENA`, `GOTO`, `GOSUB`, `Label`,
        `COLOR` y el acceso a campo no lo miraría nadie.
        """
        return (sorted(CORPUS.glob("*.src")) + sorted(EJEMPLOS.glob("*.src"))
                + sorted(CASOS.glob("*.src")))

    def _parse(self, path: Path) -> str:
        """`sourc parse` por el camino del comando, con los flujos redirigidos.

        Se llama a `main` con `argv` en vez de fabricar un `args`: el parser de
        argumentos es parte de lo que puede romperse, y un golden comprobado
        contra una llamada directa no comprueba que el subcomando exista ni que
        su bandera se llame como uno cree.
        """
        from sourc import cli

        salida = io.StringIO()
        with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(io.StringIO()):
            codigo = cli.main(["parse", str(path), "--salida"])
        self.assertEqual(codigo, 0, f"sourc parse sale con {codigo} en {path.name}")
        return salida.getvalue()

    # ------------------------------------------------------------- la tabla

    def test_la_tabla_del_volcado_esta_completa(self):
        from sourc import astdump

        problemas = astdump.comprobar_tabla()
        self.assertEqual(
            problemas, [],
            "FORMATO y el AST han divergido:\n  " + "\n  ".join(problemas)
            + "\nSi has añadido un campo a un nodo, añádelo a `astdump.FORMATO`. "
            "Si no lo vuelvas, no es que no valga: es que valga y no lo ves.")

    # ------------------------------------------------------------- goldens

    def test_no_falta_ningun_golden(self):
        faltan = [p.stem for p in self._programas()
                  if not (GOLDEN / (p.stem + ".ast")).is_file()]
        self.assertEqual(
            faltan, [],
            f"estos programas no tienen golden de AST: {faltan}. `make corpus` "
            "los escribe. Un programa sin golden de AST no falla: no lo "
            "compara nadie.")

    def test_no_sobra_ningun_golden(self):
        sobras = sorted(p.stem for p in GOLDEN.glob("*.ast")
                        if not any((base / (p.stem + ".src")).is_file()
                                   for base in (CORPUS, EJEMPLOS, CASOS)))
        self.assertEqual(
            sobras, [],
            f"estos goldens no tienen programa: {sobras}. Un programa borrado "
            "deja un golden que sigue pasando porque ya nadie lo regenera.")

    def test_sourc_parse_reproduce_el_golden(self):
        for path in self._programas():
            with self.subTest(programa=path.stem):
                esperado = (GOLDEN / (path.stem + ".ast")).read_text(
                    encoding="utf-8")
                self.assertEqual(self._parse(path), esperado)

    # ------------------------------------------------------------- cobertura

    def test_cada_nodo_del_ast_aparece_en_algun_golden(self):
        """Todos. Uno sin ejecutar es uno que nadie sabe si funciona.

        No es de forma: es de *ejecución*. Un `Field` o un `NilLit` que el
        corpus no produce son veinte líneas de serializador y de construcción de
        nodo que no se han ejecutado ni una vez, y un volcado que no se ha
        ejecutado no se sabe si vuelca bien.
        """
        from sourc import astdump

        vistos: set[str] = set()
        for golden in GOLDEN.glob("*.ast"):
            for linea in golden.read_text(encoding="utf-8").splitlines():
                encontrada = re.match(r"\s*\d+ (\w+)\s", linea)
                if encontrada:
                    vistos.add(encontrada.group(1))

        sin_ver = sorted(set(astdump.FORMATO) - vistos)
        self.assertEqual(
            sin_ver, [],
            f"nodos que ningún golden ejecuta: {sin_ver}. O entra un programa "
            "en `tests/corpus/` o en `tests/casos_ast/`, o se admite que el "
            "nodo no tiene judge --y entonces eso va escrito aquí, no en la "
            "cabeza de quien lee.")

    # --------------------------------------------------------- invariantes

    def test_el_arbol_de_cada_golden_es_un_arbol(self):
        """Los índices de los goldens son un árbol, y no una lista con punteros.

        Ésta es la puerta que protege el árbol arena del parser escrito en Sourc,
        y está escrita **antes** que él a propósito: un invariante que se
        escribe después de ver fallar la implementación se ajusta a ella. Contra
        un golden generado por Python sólo comprueba que el formato es sano, y
        eso ya tiene valor, porque un golden degenerado no puede bendecir a una
        implementación posterior.

        Las cinco propiedades:

        1. Los índices son `0..n-1` y en orden. Un pre-orden con huecos significa
           que algo se saltó un número, y ese número falta en el golden.
        2. Todo índice referenciado existe, y es **mayor** que el del padre. Es
           la propiedad que define el pre-orden, y la que detecta un índice
           escrito al revés --el fallo más probable de una arena, porque un
           índice es un `INTEGER` y un `INTEGER` equivocado no da error.
        3. Se llega a todos los nodos desde la raíz. Un nodo al que nadie
           apunta está construido y no se ve: el golden lo omite y el programa
           se ejecuta sin él.
        4. **Todos** los spans caben dentro del del `Program`. Y aquí hay que
           decir lo que **no** es un invariante, porque se asume lo contrario y
           es falso: el span del hijo **no** está dentro del del padre.
           `ARRAY OF INTEGER` produce un `TypeRef` de 62 a 67 y su elemento, un
           `TypeRef` de 71 a 78, que se sale por detrás. El `TypeRef` exterior
           cubre la palabra `ARRAY` y no lo que viene detrás del `OF`. Es del
           parser de Python y no se va a cambiar, así que la puerta tiene que
           medir lo que es cierto y no lo que sería más bonito.
        5. El nodo 0 es `Program`.
        """
        for golden in sorted(GOLDEN.glob("*.ast")):
            with self.subTest(golden=golden.stem):
                nodos = _leer_golden(golden)

                esperados = list(range(len(nodos)))
                self.assertEqual([n[0] for n in nodos], esperados,
                                 f"{golden.stem}: los índices no son 0..n-1")

                for indice, kind, ini, fin, hijos in nodos:
                    for etiqueta, hijo in hijos:
                        for referencia in _indices(hijo):
                            with self.subTest(nodo=indice, campo=etiqueta):
                                self.assertLess(
                                    referencia, len(nodos),
                                    f"{golden.stem}: el nodo {indice} ({kind}) "
                                    f"apunta a {etiqueta}={referencia}, que no "
                                    "existe")
                                self.assertGreater(
                                    referencia, indice,
                                    f"{golden.stem}: el nodo {indice} ({kind}) "
                                    f"apunta a {etiqueta}={referencia}, que es "
                                    "anterior: un índice al revés")

                raiz_ini, raiz_fin = nodos[0][2], nodos[0][3]
                for indice, kind, ini, fin, hijos in nodos:
                    self.assertLessEqual(
                        ini, fin,
                        f"{golden.stem}: el nodo {indice} ({kind}) acaba en "
                        f"{fin} antes de empezar en {ini}")
                    self.assertGreaterEqual(
                        ini, raiz_ini,
                        f"{golden.stem}: el nodo {indice} ({kind}) empieza en "
                        f"{ini}, antes que el Program en {raiz_ini}")
                    self.assertLessEqual(
                        fin, raiz_fin,
                        f"{golden.stem}: el nodo {indice} ({kind}) acaba en "
                        f"{fin}, después que el Program en {raiz_fin}")

                alcanzables = _alcanzables(nodos)
                sueltos = sorted(set(range(len(nodos))) - alcanzables)
                self.assertEqual(
                    sueltos, [],
                    f"{golden.stem}: nodos que no cuelgan de la raíz: {sueltos}")

                self.assertEqual(nodos[0][1], "Program",
                                 f"{golden.stem}: la raíz no es Program")

    def test_el_span_de_la_raiz_llega_al_ultimo_token(self):
        """El span del `Program` termina donde termina el último token.

        Es la comprobación que une las dos capas: si el `Program` termina antes,
        hay tokens que el parser no se comió y que el programa no ejecuta; si
        termina después, se comió de más. Y compara el AST contra el **lexador**,
        que es el juez del otro lado de la frontera.
        """
        for golden in sorted(GOLDEN.glob("*.ast")):
            lex = GOLDEN.parent / "lex" / (golden.stem + ".lex")
            if not lex.is_file():
                continue
            with self.subTest(golden=golden.stem):
                nodos = _leer_golden(golden)
                eof = [l for l in lex.read_text(encoding="utf-8").splitlines()
                       if " eof " in l]
                if not eof:
                    continue
                fin_fuente = int(eof[0].split()[0])
                self.assertEqual(
                    nodos[0][3], fin_fuente,
                    f"{golden.stem}: el Program acaba en {nodos[0][3]} y el "
                    f"eof del lexador en {fin_fuente}")

    # -------------------------------------------------------------- formato

    def test_el_volcado_no_tiene_una_linea_que_se_pueda_confundir(self):
        r"""El separador de campos no puede aparecer sin escapar dentro.

        Comparar cadenas es comparar texto, y si un valor puede llevar el
        separador, dos AST distintos pueden dar la misma cadena y la puerta pasa
        sin comprobar nada. Se comprueba con el caso que de verdad lo tiene: un
        texto con barra vertical y con salto de línea, que salen `p` y `n`
        precedidas de una barra invertida.
        """
        temporal = CASOS / "_separador.src"
        temporal.write_text('PRINT "a|b\\nc"\n', encoding="utf-8")
        try:
            salida = self._parse(temporal)
        finally:
            temporal.unlink(missing_ok=True)
        # El texto sale con la barra escapada como `\p` y el salto como `\n`:
        # dos caracteres cada uno, y ninguna línea del volcado lleva un salto
        # dentro. Si lo llevara, el número de nodos y el de líneas dejarían de
        # cuadrar, y por eso se comparan los dos.
        self.assertIn(r"value=a\pb\nc", salida)
        lineas = [l for l in salida.splitlines() if re.match(r"\s*\d+ ", l)]
        self.assertEqual(len(lineas), 3, salida)
        self.assertIn("; 3 nodo(s)", salida)
        self.assertNotIn("|", salida.split("value=")[1].split(" |")[0][1:],
                         "un campo de texto lleva la barra sin escapar")


def _leer_golden(ruta: Path):
    """Los nodos de un golden de AST, con sus hijos ya resueltos a índices.

    Se parsea el golden y no el AST, porque la puerta tiene que poder mirar un
    golden que no venga de Python --el del parser escrito en Sourc-- y mirar
    exactamente lo mismo en los dos casos.

    **Qué campo es un hijo se decide con `astdump.FORMATO`**, no adivinando por
    la forma del valor. Adivinar fue el primer intento y falla de dos maneras a
    la vez: `inline=0` --un booleano-- parecía un índice de nodo, y `value=42`
    de un `IntLit` parecía un hijo y no existe. Las dos cosas coinciden, es decir:
    lo que decide es el tipo del nodo, no el aspecto del texto.
    """
    from sourc import astdump

    nodos = []
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        if not linea.strip() or linea.lstrip().startswith(";"):
            continue
        cabeza, _, detalle = linea.partition("|")
        partes = cabeza.split()
        indice, kind, ini, fin = (int(partes[0]), partes[1],
                                  int(partes[2]), int(partes[3]))
        formatos = dict(astdump.FORMATO[kind])
        declarados = [campo for campo in detalle.split()]
        hijos = []
        for campo in declarados:
            etiqueta, _, valor = campo.partition("=")
            formato = formatos.get(etiqueta)
            if formato == astdump.PARAM:
                # `params=n:2` es un índice por parámetro, con el tipo de cada
                # uno detrás de los dos puntos. Los tipos son nodos del árbol
                # como cualquier otro, y sin esto quedaban colgando: el golden
                # los escribía y la puerta de huérfanos los echaba de menos,
                # que es exactamente lo que pasó.
                #
                # `indiceTipo` y no `indice`, porque `indice` es el del nodo que
                # se está leyendo. Sombrearlo con un trozo de texto convertía el
                # índice del nodo en la cadena `"2"`, y el síntoma --una
                # comparación de listas que no cuadra-- sale a dos pantallas del
                # sitio del error. En un lector de formatos, el sombreado es el
                # fallo más caro que hay.
                # `params=` vacío es una función sin parámetros, no un
                # parámetro sin tipo: `"-".split(",")` daría `["-"]` y el -1
                # colgaría del último nodo del árbol, que es justo el nodo
                # equivocado y en silencio.
                piezas = []
                for par in valor.split(",") if valor else []:
                    _, _, indiceTipo = par.rpartition(":")
                    piezas.append(int(indiceTipo) if indiceTipo else -1)
                hijos.append((etiqueta, piezas))
                continue
            if formato not in (astdump.NODO, astdump.NODOS):
                continue
            if valor == "-":
                hijos.append((etiqueta, None))
                continue
            piezas = [int(x) for x in valor.split(",")] if valor else []
            if formato == astdump.NODO:
                hijos.append((etiqueta, piezas[0]))
            else:
                hijos.append((etiqueta, piezas))
        nodos.append((indice, kind, ini, fin, hijos))
    return nodos


def _indices(hijo) -> list[int]:
    """Un hijo como lista, esté el campo como un índice o como varios.

    Sin esto el recorrido tiene el `isinstance` repartido por tres sitios, y
    uno de ellos se olvidó y comparaba una lista con un entero. Un hijo es
    siempre un índice o una lista de índices según su formato, y la diferencia
    no la debe comprobar cada quien.
    """
    if hijo is None:
        return []
    return hijo if isinstance(hijo, list) else [hijo]


def _alcanzables(nodos) -> set[int]:
    vistos: set[int] = set()
    pila = [0]
    while pila:
        indice = pila.pop()
        if indice in vistos:
            continue
        vistos.add(indice)
        for _, hijo in nodos[indice][4]:
            pila.extend(_indices(hijo))
    return vistos


if __name__ == "__main__":
    unittest.main()