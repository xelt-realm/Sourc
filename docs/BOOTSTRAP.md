# Bootstrap — cómo se demuestra que Sourc compila Sourc

Este documento se escribió **después** de construir las tres piezas que usa, no
antes. Un procedimiento escrito antes describe lo que alguien imaginó; uno
escrito después puede citar líneas concretas que existen y nombrar gates que ya
fallan cuando algo está mal. Es la regla que salió de escribirlo: el orden
correcto es ensamblar el instrumento y luego documentarlo.

La decisión de fondo está en [ADR 0016](adr/0016-bootstrap-equivalencia.md):
el objetivo de Stage 2 es **equivalencia funcional**, no identidad de bytes.
El punto fijo clásico (`stage1(stage1.src) == stage1(src)` byte a byte) es una
prueba más fuerte y cuesta tres o cuatro semanas persiguiendo diferencias
cosméticas. Aquí se comparan tres cosas, en este orden.

## 0. Las piezas que existen

| Pieza | Qué es | Dónde |
|---|---|---|
| IR canónico | El texto que Stage 0 y Stage 1 producen y se compara | `tests/golden/ir/*.sir`, `make corpus` |
| Tabla de paridad | Un programa verde no puede dejar de estarlo | `tests/parity.json`, `make parity` |
| `sourc diff` | Clasifica la diferencia: semántica, orden o formato | `compiler/sourc/diffir.py` |
| Contrato del subconjunto | Lo que el compilador en Source puede usar | `subset.toml`, `make check-subset` |
| Fuzzer diferencial | Genera programas y compara intérprete contra módulo | `tools/fuzz_source.py`, `make fuzz-source` |
| Flujo de tokens goldenizado | El texto que ambos lexadores tienen que producir | `tests/golden/lex/*.lex`, `sourc lex` |
| AST goldenizado | El texto que ambos parsers tienen que producir | `tests/golden/ast/*.ast`, `sourc parse` |
| Lexador en Sourc | La rebanada 1 del bootstrap | `compiler/lexador.src`, `make bootstrap` |

Sin el IR goldenizado no hay contra qué mirar: es lo primero que se ejecuta.

## 0.bis. La rebanada 1 ya existe: el lexer

Antes de nada, una corrección a la forma del plan. **La primera rebanada no es
el serializador del IR.** Serializar el IR exige construirlo, y construirlo
exige un lexer, un parser, un typechecker y un lowering: un serializador solo es
la última cuarta parte de un compilador con las otras tres sin escribir. La
rebanada que sí se puede hacer entera, y que es un juez real, es **el lexer**.

Y no necesita `ARENA`, que era la condición que se había puesto para empezar.
Mover un cursor por un texto y acumular caracteres son dos arrays y nada más.

```sh
make bootstrap
```

`compiler/lexador.src` es **el cuerpo** del lexador, no un programa: el
generador `tools/bootstrap.py` le antepone una línea con el texto a lexar y lo
compila. Esa es la solución al problema de entrada, que es real: **el lenguaje
no tiene E/S**, ni `INPUT` ni lectura de fichero, así que la fuente entra como
literal. Cuando exista el contrato de host --un `ejecutarCompilador(bytes,
fuente)`-- esto se simplifica, y esa es exactamente la parte que cambiará.

El lexer deja además los tokens en cinco arrays paralelos --tipo, valor, texto,
inicio y fin--, porque el parser los necesita para construir el AST. El bucle
final **imprime desde los arrays**, y por eso el formato del golden vive en un
solo sitio: antes había quince copias, una por punto de emisión, y quince
lugares donde equivocarse. Y el cambio se hizo bajo el golden que ya existía,
que es lo que demuestra que no alteró la semántica: la salida nueva es idéntica
a la vieja salvo la columna del fin, comprobado en los 20 programas antes de
regenerar nada.

El `fin` de cada token es el número que el parser más va a usar, así que tiene
puerta propia. Tres invariantes que un golden completo todavía no cubre, porque
el `fin` no lo lee nadie hasta que el parser exista: `inicio <= fin`, los tokens
de una línea no se solapan, y el `eof` acaba en el número de **caracteres** del
fichero --que es donde el byte count y el character count se separan, y 13 de
los 20 programas tienen acentos--.

Contra qué se juzga:

| juez | qué cubre | puerta |
|---|---|---|
| `tests/golden/lex/*.lex` | 20 programas del corpus y los ejemplos | `tests/test_lex_golden.py` |
| `tests/test_lex_casos.py` | 23 casos que **ningún programa puede llegar a tener** | `tests/test_bootstrap.py` |

Lo segundo es lo que hace que el juez sea serio. Un golden sobre el corpus sólo
ve lo que el resto del lenguaje permite escribir, y hay caminos del lexador que
sólo se alcanzan con fuente que no compila: un `1e` son dos tokens y no aparece
en ninguna expresión válida; un `&H` sin dígitos es un error léxico; un texto
sin cerrar también. La regla que se sigue es que **toda ruta del lexador que el
lexador implemente tiene que aparecer en un golden o en un caso**, y las que no
están, están escritas como huecos en la cabecera del propio fichero.

Tres cosas que la rebanada descubrió y que no estaban previstas. Ninguna es un
detalle de implementación:

1. **`AND` no cortocircuita en Source.** `pos < largo AND CHARCODE(src, pos)`
   evalúa las dos partes, así que con `pos == largo` lee un byte que no existe.
   El síntoma es el peor posible: verde en toda la suite y rojo en el último
   carácter de los ficheros que no acaban en salto de línea. La regla que sale
   de ahí es general y no es del lexador: *un `AND` que protege un índice tiene
   que protegerlo en la rama, no en la expresión*. Hay una puerta que la
   comprueba sobre el texto del fichero.
2. **No hay `UPPER` ni `LOWER` ni `CHR`.** El plegado de caso, que el lexador
   necesita para canonicalizar identificadores y palabras clave, se hace con
   `CHARCODE` y una tabla de las 52 letras en el propio programa: cero peso en
   el pegamento, que está a veintinueve bytes de su techo. Y el hueco de `\u`
   viene de lo mismo: no hay forma de pasar de un entero a un carácter.
3. **No hay `SUB`, ni `GOTO` que baje a IR, ni `STRUCT` declarable.** No hay
   subrutina con estado compartido. Por eso el lexador son veinte `FUNCTION`
   puras --predicados-- y una máquina de estados en el nivel superior: es la
   forma que queda cuando E0067 impide que una función toque un global y los
   parámetros son por valor.

Los dos huecos que quedan, declarados en la cabecera del lexador y medidos por
`tests/test_lex_casos.py`:

- **Los escapes `\u{...}`.** El rango ASCII se podría tapar con una tabla de
  128 caracteres; el no-ASCII no cabe en ninguna tabla dentro de las premises.
  El test no pide que coincidan: pide que **se diferencien**, y que se
  diferencien de una forma concreta. Un hueco que no se mide acaba siendo un
   fallo sin dueño.
- **Los errores léxicos.** El de Python acumula diagnósticos y sigue; el de
  Sourc sólo emite tokens y trata un carácter inesperado como operador. Los
  `E0001` llegan con la rebanada 2, que es el parser.

## 0.bis.bis. La rebanada 2: el juez del parser ya existe

`sourc parse` vuelca el AST en forma canónica y `tests/golden/ast/*.ast` son sus
goldens. Está escrito **antes** que el parser en Source, que es el punto.

El formato, una línea por nodo en pre-orden:

```
idx kind span_ini span_fin | campo=valor campo=valor
```

Tres decisiones, y las tres para quitarle decisiones al serializador: *todos los
campos siempre* (un formato condicional mete al serializador en el sitio donde
una divergencia se esconde), *con nombre* (`name=x` no se confunde con el `name`
de otro nodo, que es la trampa que `subset.toml` ya señalaba), y *con tipo
declarado* en una tabla (`astdump.FORMATO`) que una puerta comprueba contra los
`dataclass` del AST. Si alguien añade un campo a un nodo y no lo vuelca, la
puerta falla en vez de que el campo aparezca como `-` en todos los goldens.

Los literales enteros y flotantes conservan **el texto tal como se escribió**, y
no su valor. No es estética: un compilador escrito en Sourc no puede volver a
imprimir `&HFFFFFFFFFFFFFFFF` --no cabe en un `SINGLE` y `VAL()` devuelve
`DOUBLE`--, así que sin el texto el golden mediría dos formateadores en vez de
un parser. Convertir a número es trabajo del typechecker.

**Y el juez encontró un bug de Stage 0 antes de que existiera una línea de
parser en Source.** `GOSUB x` producía un nodo `Goto`: el despacho de sentencias
hacía `handler(self)` y `parse_goto` se quedaba con su valor por defecto. Dos
puertas lo documentaban como rareza conocida, y estaba enmascarado porque los dos
dan E0043. Es el argumento de escribir el juez primero, con un ejemplo: **un
defecto de Stage 0 que ningún test de Stage 0 podía ver**, porque verlo exige
mirar la forma del árbol y nadie miraba la forma del árbol.

La cobertura también es puerta: `tests/casos_ast/` son seis programas de **sólo
parseo** para los nodos que el corpus no produce --`ARENA`, `GOTO`, `GOSUB`,
`Label`, `COLOR` y el acceso a campo, que parsean y no compilan-- porque un nodo
sin golden es código que nadie ha ejecutado nunca.

## 0.bis.bis.bis. La rebanada 2a: el parser, a medias y en verde

El parser escrito en Sourc existe y da el mismo AST que el de Python, byte a
byte, en los seis casos de `tests/casos_parser/`. `make parser`.

**No está entero, y eso hay que decirlo primero.** 2a cubre el esqueleto del
árbol, la pila de cuerpos abiertos, las sentencias --`DIM`, asignación, `PRINT`,
`RETURN`, `WHILE`, `FOR`, `IF`/`ELSE IF`/`ELSE` y `FUNCTION`-- y las expresiones
hoja: un literal o un nombre. Veintitrés de los treinta y un nodos del AST. Faltan
los operadores, la precedencia, el acceso a array y a campo, las llamadas, la
interpolación y los diagnósticos, que son 2b, 2c y 2d.

### Cinco cosas que 2a costó, y que no se sabían

1. **No hay ni una FUNCTION en el parser.** E0067 prohíbe tocar un global, y
   **leer** un global también cuenta. Así que tampoco hay accesores de token, y
   el bloque de "una expresión hoja" está **copiado** en los cuatro sitios donde
   hace falta. Se acepta porque el golden cazaría una copia desviada igual que
   a una función equivocada.
2. **Una lista de hijos no es un tramo contiguo de índices.** Era la hipótesis
   con la que se diseñó el árbol y es falsa: el golden dice `body=1,4,7`, y
   entre el 1 y el 4 están el `TypeRef` y el `IntLit` de la primera sentencia.
   Ha vuelto `nSiguiente` como eslabón, y las listas se construyen como texto al
   añadir en vez de recorrerse al cerrar.
3. **El `Span` de un `StringLit` estaba mal en el parser de Python.** Salió en el
   golden del caso `dim` y resultó ser el span del token *siguiente*: en
   `PRINT "texto"` era el salto de línea. `parser.py` escribía
   `self.advance().value` y **después** leía `self.current.span`, así que el
   literal se quedaba con el span de lo que venía detrás. Cualquier diagnóstico
   que apuntara a un literal de texto subrayaba el sitio equivocado. Es el
   **segundo** defecto de Stage 0 que encuentra el juez del AST, después del
   `GOSUB` que salía `Goto`, y los dos estaban en sitios donde ningún test de
   Stage 0 podía mirar: los dos hacen falta mirar la *forma*, no la salida.
4. **En una cadena de `ELSE IF` hay un `If` con `nFin` y los demás no.** Cada
   `If` de la cadena toma el suyo de **su propio** último cuerpo; el del dueño se
   pone al final, ya con el `END` dentro. Por eso la pila guarda un `pDueno` por
   nivel: sin él, el `nFin` del `IF` de fuera se quedaba a cero y se le ponía al
   último de la cadena. Lo enseña el golden de `flujo`: `14 If ... 0` donde iba
   `329`.
5. **El cuerpo de una línea se cierra al final del bucle, no en la rama que lo
   abrió.** El cuerpo de un `IF x THEN PRINT "a"` lo cierra el `PRINT`, que va
   por otra rama del despacho. Y sólo si ya tiene alguna sentencia: sin esa
   comprobación el cuerpo se cerraba en la misma vuelta en que se abría, y el
   `then_body` salía vacío.

Y una corrección al plan, que es de las que cuestan un rediseño si no se ven
pronto. La contigüidad de los índices se dio por buena porque "el parser crea los
hijos de un nodo uno detrás de otro y sin nada en medio". Eso es cierto para los
hijos, y falso para una lista de hermanos, y la diferencia es toda: un tramo
contiguo puede contener los hijos de una sentencia si esa sentencia va en medio
de otra, y no puede excluirlos sin dejar de ser contiguo. El golden lo dice en la
primera línea: `body=1,4,7`.

### Y una de las formas salió bastante más cara de lo previsto

2a-1 --los cuerpos, `IF`/`WHILE`/`FOR`/`FUNCTION` y la pila de niveles-- **se
escribió y se revirtió**. La pila funcionaba: los niveles se abrían y cerraban,
el `ELSE IF` encadenado no consumía el `END`, y el cuerpo de una línea se
cerraba solo. Lo que falló fue la forma del fichero.

La razón: usé `ELSE` seguido de `IF` anidado donde el lenguaje tiene `ELSE IF`,
que es **un** bloque y no dos. Una cadena de cuatro condiciones pasaron de un
cierre a cuatro, y a partir de ahí contar `END IF` dejó de ser fiable: el
compilador informaba de uno de más y el que sobraba no era nunca el que yo
sospechaba. Sin un juez fiable, cualquier arreglo iba a ciegas.

La regla que sale de ahí, y que decide cómo se escribe el resto del parser:

> **Toda cadena de condiciones es `ELSE IF`. Nunca `ELSE` seguido de `IF`.**

Con `ELSE IF` la cadena es un bloque, el número de cierres es uno y se cuenta
mirando. Con `ELSE` + `IF` son *n* bloques y *n* cierres, y *n* es el número de
sitios donde se puede equivocar uno. En un fichero de mil y pico líneas, ésa es
la diferencia entre un cierre que se cuenta y uno que se busca.

Y para que no dependa del pulso, `tools/contar_bloques.py` tokeniza el fichero
como lo hace el lexer --literales, comentarios y escapes-- y devuelve la
profundidad y el primer cierre de más. Se escribió cuando ya era tarde; lo que
faltaba era la regla de arriba.

La segunda versión, con la regla aplicada, salió a la primera: los seis casos
verdes del tirón y en un orden --`funcion`, `flujo`, `anidado`-- que es el que
digiere el fichero, porque `anidado` es el que prueba la pila con tres niveles
abiertos por tres caminos distintos: un cuerpo nuevo, un `ELSE` y un cierre.

## 1. Equivalencia semántica

Stage 0 y Stage 1 tienen que coincidir en **stdout**, en **IR canónico** y en
**diagnósticos**, sobre el corpus.

```sh
# 1. Stage 0 (Python) sobre el corpus.
for f in tests/corpus/*.src; do
  python3 compiler/sourc.py sir "$f" > "/tmp/stage0/$(basename "$f" .src).sir"
done

# 2. Stage 1 (el compilador en Source, correndose a si mismo) sobre lo mismo.
#    Empieza por el lexer, que ya existe: `make bootstrap`.
#    El IR todavia no: falta el parser, que es la rebanada 2.

# 3. La diferencia, clasificada.
python3 compiler/sourc.py diff /tmp/stage0/01_hola.sir /tmp/stage1/01_hola.sir
```

`diff` sale con código 1 **sólo** si hay diferencias semánticas. Un exit 0 con
diferencias de orden o de formato es el resultado correcto: significan que las
dos etapas compilan lo mismo y lo escriben distinto.

Lo que **no** hace este paso y hay que hacer aparte: comparar diagnósticos. Un
`E0022` de Stage 0 y un `E0022` de Stage 1 tienen que decir **lo mismo**, con
el mismo código, la misma posición y la misma sugerencia. Los textos están
snapshotteados en `tests/test_diagnostics.py`, así que el juez ya existe y Stage 1
tiene que pasar contra él.

## 2. Idempotencia

Stage 1 sobre la salida de Stage 1 tiene que comportarse como Stage 1 sobre el
original:

```sh
python3 compiler/sourc.py sir /tmp/stage1/01_hola.sir > /tmp/stage2/01_hola.sir
python3 compiler/sourc.py diff /tmp/stage1/01_hola.sir /tmp/stage2/01_hola.sir
```

Aquí `diff` **tiene** que dar "sin diferencias": es el mismo frontend con la
misma entrada, y cualquier diferencia es un bug.

## 3. Determinismo intra-compilador

El mismo Stage 1 produce los mismos bytes dos veces seguidas. Este requisito sí
es duro, y sin él no hay caché de build ni forma de bisectar un bug.

```sh
make bootstrap-check          # cinco comprobaciones, incluida la de dos procesos
```

La comprobación entre **procesos** con `PYTHONHASHSEED` distinto es la que
importa: dentro de un proceso todo es determinista aunque se recorra un `set`,
y ese bug sale al compilar desde la línea de órdenes.

## Cómo se lee un fallo

El orden de la investigación es el que ahorra tiempo:

1. **`make test`** — si falla, Stage 1 ni ha empezado. Un fallo aquí es un bug
   de Stage 0, y no se busca en Stage 1.
2. **`make check-subset`** — si falla, Stage 1 está usando algo por encima de su
   nivel. El mensaje nombra la feature, la línea y lo que cuesta trabajar sin
   ella. **Esto se mira antes que nada**, porque un `campo` que se cuela produce
   un nodo del IR con los campos en arrays paralelos, y los errores que de ahí
   salen son errores de índice en todas partes: dos días buscando el bug donde
   la causa era una decisión de la Fase 4 tomada sin querer.
3. **`sourc diff`** con `--detallado`. Si dice `orden` o `formato`, el programa
   es el mismo y el fallo es de otro sitio: probablemente determinismo o
   idempotencia. Si dice `semantica`, la diferencia está en la línea que enseña.
4. **`make parity`** — si el módulo y el intérprete discrepan, el bug está en el
   lowering, no en Stage 1 todavía.

## Lo que este procedimiento **no** demuestra

- **Que Stage 1 sea correcto.** Demuestra que Stage 1 se parece a Stage 0. Si
  los dos tienen el mismo bug, el bootstrap pasa y el compilador está mal. Por
  eso Stage 0 sigue siendo el compilador de producción durante meses después.
- **Que el compilador sea rápido.** El criterio de "500 líneas en menos de 3 s en
  el navegador" es otro, y no lo cubre nada de aquí.
- **Que el lenguaje sirva para algo.** El bootstrap es una propiedad de la
  cadena de compilación, no del lenguaje.

## Criterios de éxito, y su estado

| Criterio de ADR 0016 | Estado |
|---|---|
| Coincidencia en stdout, IR y diagnósticos sobre ≥ 300 programas | 17 versionados + el fuzzer diferencial. Falta el slice 1 |
| Idempotencia `stage1(stage1.src)` | El instrumento existe; falta Stage 1 |
| Determinismo intra-compilador | Hecho y en la puerta de release |

Los 300 programas del criterio son un número de ADR, y el fuzzer diferencial es
la forma de llegar: genera programas válidos por construcción con semilla fija
y compara intérprete contra módulo. Un programa generado **no** se versiona --no
es un corpus-- pero cubre lo que el corpus no cubre.
