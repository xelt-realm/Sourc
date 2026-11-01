# GRAMMAR

Especificación normativa del lenguaje Source, versión `1.0`.

- Fuente de verdad de la sintaxis: [`docs/grammar/sourc.ebnf`](docs/grammar/sourc.ebnf)
- Este documento añade la **semántica** de cada construcción, que la gramática
  por sí sola no expresa.
- Toda ambigüedad se resuelve con un ADR en `docs/adr/`.
- Los bloques de código de este documento se typechequean en CI.

## 0. Convenciones léxicas

### 0.1 Case

Las palabras clave no distinguen mayúsculas: `DIM`, `dim` y `Dim` son lo mismo.

Los identificadores **no** distinguen mayúsculas: `miVariable` y `MIVARIABLE` son
el mismo identificador. Se canonizan a minúsculas parabuscar y comparar. Si dos
identificadores sólo difieren en case, es un error `E0004`.

El case original se conserva para generar documentación y mensajes de error.

### 0.2 Comentarios

```source
' comentario de línea
REM comentario de línea
```

`'`, `REM` y `REM` inicial sólo. No hay comentarios de bloque en la versión 1.0.

### 0.3 Literales numéricos

```source
DIM a AS INTEGER = 42
DIM b AS DOUBLE = 4.2
DIM c AS DOUBLE = 1e3
DIM d AS DOUBLE = 1.5e-2
DIM e AS INTEGER = &HFF
DIM f AS INTEGER = &B1010
DIM g AS INTEGER = &O777
PRINT a ; b ; c ; d ; e ; f ; g
```

Un literal entero sin sufijo es `INTEGER` (i64). Un literal con parte fraccionaria
o exponente es `DOUBLE` (f64).

### 0.4 Literales de texto

```source
DIM nombre AS STRING = "mundo"
PRINT "texto plano"
PRINT "con \"escapes\" y \n"
PRINT "unicode: \u{1F600}"
PRINT "interpolado: hola {nombre} y {1 + 2}"
```

Las cadenas son **UTF-8** y en memoria son una secuencia de bytes con longitud.
`LEN` de un texto devuelve el número de bytes. Ver `docs/adr/0003`.

Los escapes válidos son `\n`, `\t`, `\r`, `\0`, `\\`, `\"`, `\'` y `\u{...}`.

### 0.5 Booleanos y vacío

`TRUE` y `FALSE` son palabras clave. `NIL` es el valor vacío de un puntero o de
una referencia de función.

Un BOOLEAN se imprime como `TRUE` o `FALSE` **siempre**, también cuando está
dentro de un array. El valor cero de un BOOLEAN es `FALSE`, no el entero `0`, y
`PRINT` de un `ARRAY OF BOOLEAN` sin inicializar da `[FALSE, FALSE]`.

### 0.6 Identificadores

`[A-Za-z_][A-Za-z0-9_]*`. Se permiten acentos en literales de texto, no en
identificadores.

## 1. Precedencia de operadores

De mayor a menor. Un mismo nivel se evalúa de izquierda a derecha.

| Nivel | Operadores | Tipo de resultado |
|---|---|---|
| 1 | `.` (miembro), `(` (llamada), `[` (índice) | — |
| 2 | `-` (unario), `NOT` | numérico / booleano |
| 3 | `^` | `DOUBLE` |
| 4 | `*`, `/`, `\`, `MOD`, `DIV` | numérico |
| 5 | `+`, `-`, `&` (concatena) | numérico / `STRING` |
| 6 | `=`, `<>`, `<`, `<=`, `>`, `>=` | `BOOLEAN` |
| 7 | `AND` | `BOOLEAN` |
| 8 | `OR` | `BOOLEAN` |
| 9 | `XOR` | `BOOLEAN` |

Notas:

- `^` es potencia y siempre produce `DOUBLE`.
- `/` es división verdadera y **siempre** produce `DOUBLE`: `7 / 2` da `3.5`.
- `\` es división entera que trunca hacia cero.
- `DIV` es división entera que trunca hacia menos infinito.
- `MOD` es el resto con el signo del dividendo (comportamiento de BASIC).
- `&` concatena textos. `+` entre dos `STRING` es error `E0021`: hay que ser
  explícito, porque `+` con `INTEGER` es aritmética y no queremos adivinar.
- `AND` y `OR` **no** hacen cortocircuito. Se evalúan ambos lados. Para
  cortocircuito existe `IF ... THEN` o la forma `IFNOT` de la sección 4.

## 2. Declaraciones

### 2.1 DIM

```source
DIM x AS INTEGER = 10
DIM nombre = "hola"                  ' STRING, inferido
DIM vida AS SINGLE = 100.0
DIM activo AS BOOLEAN = TRUE
DIM mapa AS ARRAY OF STRING = [ "aa", "bb" ]
DIM buffer AS ARRAY OF SINGLE = NEW SINGLE[1024]
```

Reglas:

1. `DIM` **debe** incluir valor inicial o tipo de tamaño. `DIM x` a secas es
   error `E0010`: no hay valor por defecto en Source, ni siquiera `0`.
2. Sin `AS`, el tipo se infiere del valor inicial y es exacto.
3. Con `AS` y sin `=`, el valor se pone a cero del tipo (`NIL` para punteros y
   funciones). Útil para `ARRAY`.
4. Una variable no declarada no existe. `y = 5` sin `DIM y` es error `E0022` con
   sugerencia `¿quiso decir DIM y AS INTEGER = 5?`. Ver `docs/adr/0001`.
5. El ámbito de un `DIM` a nivel de sentencia es el bloque (sentencia, cuerpo de
   `IF`, cuerpo de bucle, cuerpo de `FUNCTION`, bloque de `ARENA`).
6. `DIM` repetido en el mismo ámbito es error `E0023`.
7. El rango de `INTEGER` es i64 con signo: -2^63 .. 2^63-1. El desbordamiento en
   una constante es error de compilación.
8. El tipo del `AS` manda sobre el que se deduciría del valor, y baja **un
   nivel** a los arrays anidados: `DIM m AS ARRAY OF ARRAY OF SINGLE = [[1.5]]`
   reserva un array de SINGLE, no de DOUBLE. El elemento del literal se convierte
   al tipo declarado.
9. `PRINT` de un array imprime sus elementos en notación del lenguaje:
   `[1, 2, 0]`, `["uno", "dos"]`, `[[1, 2], [3]]`. Un texto dentro va entre
   comillas dobles, como se escribe en el fuente, para que lo que sale se pueda
   volver a leer como un literal.

### 2.2 FUNCTION

```source
FUNCTION sumar(a AS INTEGER, b AS INTEGER) AS INTEGER
    RETURN a + b
END FUNCTION

FUNCTION saludar(nombre AS STRING) AS STRING
    ' sin RETURN devuelve el valor cero del tipo de retorno
END FUNCTION
```

Reglas:

1. No existe `SUB`. Un procedimiento es una `FUNCTION` sin `AS`.
2. `RETURN expr` devuelve; `RETURN` solo termina y devuelve el valor cero del tipo.
3. Reaching `END FUNCTION` sin `RETURN` devuelve el valor cero. No es error.
4. Los parámetros son por valor. Las referencias se pasan explícitamente.
5. Una `FUNCTION` es un valor de primera clase: `TYPE` de función, tabla de
   llamadas, `call_indirect`.

### 2.3 ARENA

```source,skip
arena
    var scratch: Datos
    scratch.valor = 1.0
end
```

Reglas:

1. Una arena es bump allocation con ámbito. En el compilador C, las variables
   struct declaradas en el bloque se asignan desde un buffer dimensionado en
   compilación. Al salir, el puntero vuelve al checkpoint y el buffer termina
   su ámbito.
2. `RESET ARENA name` marca un checkpoint y libera lo allocated desde el
   checkpoint. Es O(1).
3. Un valor no puede sobrevivir a su arena. Referenciar una variable struct
   después del bloque es error de compilación.
4. El buffer reserva el tamaño que el compilador calcula; si el allocator no
   puede satisfacer una solicitud, el programa falla explícitamente.

### 2.4 TYPE y campos

```source,skip
type Jugador
    x: float
    y: float
    vidas: int
end

var jugador: Jugador
jugador.x = 100.0
```

Los campos conservan el orden de declaración y se acceden mediante `.`. Los
tipos personalizados se pasan por valor; sus campos pueden ser primitivos o un
tipo personalizado definido antes. No se admiten campos array ni `void`.

## 3. Control de flujo

### 3.1 IF

```source
DIM a AS INTEGER = 2
DIM b AS INTEGER = 1
IF a > b THEN PRINT "mayor"

IF a > b THEN
    PRINT "mayor"
ELSE IF a = b THEN
    PRINT "igual"
ELSE
    PRINT "menor"
END IF
```

Reglas:

1. `THEN` seguido de una sentencia en la misma línea es una forma corta. El
   cuerpo termina en el fin de línea o en un `;`.
2. `ELSE` y `END IF` sólo aparecen en la forma de bloque.
3. La forma de bloque se detecta por la presencia de una sentencia o por un
   salto de línea antes de `ELSE`/`END IF`. El analizador léxico resuelve esto
   sin ambigüedad (ver `docs/adr/0006`).
4. Las condiciones deben ser `BOOLEAN`. Un `INTEGER` no se convierte a `BOOLEAN`
   implícitamente: es error `E0024` con sugerencia `<> 0`.

### 3.2 WHILE

```source
DIM vida AS INTEGER = 3
WHILE vida > 0
    vida = vida - 1
WEND
```

`WHILE ... WEND`. No hay `WEND IF` en la versión 1.0.

### 3.3 FOR

```source
FOR i = 0 TO 10
    PRINT i
NEXT

FOR i = 10 TO 1 STEP -1
    PRINT i
NEXT i

DIM mapa AS ARRAY OF STRING = ["ab", "cd"]
FOR i = 0 TO LEN(mapa) - 1
    FOR j = 0 TO LEN(mapa[i]) - 1
        PRINT mapa[i][j]
    NEXT j
NEXT i
```

Reglas:

1. **La variable de `FOR` se declara sola**, con ámbito de bucle. No hace falta
   `DIM`, y no existe fuera del bucle. Es la excepción a la regla 4 de DIM, y es
   deliberada: evita la sentencia `FOR i = 0` que declara una global por accidente.
2. La variable es `INTEGER` si los tres valores (inicio, fin, paso) son enteros;
   en otro caso `SINGLE`.
3. `STEP` por defecto es `1`.
4. `NEXT` acepta opcionalmente el nombre de la variable, que debe coincidir con
   el del `FOR`. Si no coincide, error `E0041`.
5. `FOR` con condición booleana (`FOR WHILE cond`) no existe. Usar `WHILE`.

### 3.4 GOTO y GOSUB

Existen por compatibilidad con BASIC clásico. Ambos emiten warning `W0001`:

- `GOTO etiqueta` salta a una etiqueta `etiqueta:` del mismo bloque o de un
  bloque ancestro.
- `GOSUB etiqueta` / `RETURN` usa la pila de retorno.

Un `GOTO` hacia adelante, hacia dentro de un `FOR` o hacia fuera de una `FUNCTION`
es error `E0042`. No hay verificación en runtime: es el precio de mantenerlos.

## 4. Textos

```source
DIM s AS STRING = "hola"
DIM a AS STRING = "x"
DIM b AS STRING = "y"
PRINT LEN(s)         ' bytes: 4
LENU(s)               ' codepoints
CHARCODE(s, 0)        ' 104
CHR(104)              ' "h"   -- el inverso; ADR 0021
LEFT(s, 2)            ' "ho"
RIGHT(s, 3)           ' "ola"
MID(s, 1, 3)          ' "ola"   (0-based, ver nota)
COMPARE(s, "hola")    ' 0 si iguales, -1 o 1 si no
CONCAT(a, b)          ' a & b
CONTAINS(s, "la")     ' BOOLEAN
INDEXOF(s, "la")      ' INTEGER o -1
```

Los índices de texto son **0-based**, como el resto del lenguaje, no 1-based
como en QBasic. `MID(s, inicio, largo)` empieza a contar en 0. Quien portería
código clásico tiene que revisar los `MID` y `LEFT`; quien use `s[i]` no nota
diferencia porque en BASIC el índice también empieza en 0.

`s[i]` devuelve un `STRING` de un carácter. Cuando se compara contra un literal
de un solo carácter, el compilador lo baja a comparación de bytes y **no
genera ninguna asignación**. Ver `docs/adr/0004`. Para evitar cualquier duda en
código sensible al rendimiento, usar `CHARCODE`.

`CHR(n)` es el inverso de `CHARCODE`: construye la cadena de un byte con ese
código. El rango es **0 a 255**, porque en Source una cadena son bytes y no
caracteres Unicode —`CHR(256)` no se puede representar, y da `E0068` en vez de
truncarse. Ver `docs/adr/0021`.

`&` es el operador de concatenación: `"a" & "b"`.

## 5. Primitivas nativas

Son funciones del runtime, no sintaxis. Se pueden pasar como valores.

| Primitiva | Tipo | Notas |
|---|---|---|
| `PRINT` | sentencia | escribe en la salida estándar virtual |
| `SCREEN w, h` / `SCREEN w, h, "título"` | sentencia | crea el lienzo |
| `SCREEN OFF` | sentencia | destruye el lienzo |
| `CLS` / `CLS color` | sentencia | limpia el buffer de dibujo |
| `PSET x, y, color` | sentencia | un píxel |
| `LINE x1, y1, x2, y2, color` | sentencia | línea |
| `RECT x, y, w, h, color` | sentencia | contorno de rectángulo |
| `RECT FILLED x, y, w, h, color` | sentencia | rectángulo relleno |
| `CIRCLE x, y, r, color` | sentencia | círculo |
| `TRIANGLE x1, y1, x2, y2, x3, y3, color` | sentencia | triángulo |
| `TEXT x, y, "hola", color` | sentencia | texto en pantalla |
| `FLIP` | sentencia | fin de frame |
| `KEY("w")` | `BOOLEAN` | tecla mantenida |
| `KEYDOWN("w")` | `BOOLEAN` | flanco de bajada en este frame |
| `RGB(r, g, b)` | `COLOR` | empaqueta un color |
| `INPUT` | sentencia | lee una línea. **No parsea todavía**: el tipochecker la conoce y el parser la registra, pero `INPUT x` no está implementado. El `INPUT` de la capa web (§7) es otra cosa |
| `MOUSE_X`, `MOUSE_Y`, `MOUSE_DOWN` | valores | puntero |
| `DT` | valor | segundos desde el último `FLIP` |

Reglas:

1. `COLOR` es **sentencia**: `COLOR 5` fija el índice de paleta. Para construir un
   color se usa `RGB(r, g, b)`. `COLOR(r, g, b)` sigue funcionando como alias y
   emite warning `W0002`. Ver `docs/adr/0005`.
2. `FLIP` presenta el frame, cierra los flancos de entrada y calcula `DT`.
3. Un bucle sin `FLIP` emite warning `W0004` y quemaría la CPU. El aviso sólo
   aparece si el programa usa `SOURCE_2D`: un programa de cálculo con bucles no
   es un juego y no presenta nada. Sólo se miran los bucles exteriores, porque
   el `FLIP` del bucle exterior ya presenta lo que dibujan los interiores.
4. `FLIP` fuera de un bucle es error `E0051`.

## 6. Módulos

```source
USE SOURCE_2D
USE SOURCE_SOUND

IMPORT FROM "utilidades/matematica.src"
IMPORT FROM "juego/nivel.src" AS NIVEL
```

El runtime es modular: `source_core`, `source_2d`, `source_3d`, `source_web`,
`source_sound`, `source_net`. Un programa sólo descarga el runtime que usa, y el
glue correspondiente también. Ver `docs/adr/0010`.

## 7. Capa web (modo PAGE)

```source,skip
' La capa web no está implementada hasta la Fase 5; este bloque documenta la
' sintaxis futura, no un programa ejecutable hoy.
PAGE "Mi juego"

HEAD
    LINK "estilo.css"
    META name "description" content "Un juego"
    SCRIPT "analytics.js" ASYNC
END HEAD

BODY
    H1 "Puntuación"
    TEXT puntuacion
    BUTTON "Jugar" ON CLICK jugar
    INPUT "Tu nombre" ON ENTER enviar
    LIST mensajes
END BODY

END PAGE
```

Reglas:

1. `PAGE "título"` empieza un documento. El modo por defecto es `PAGE`: el
   usuario no escribe HTML.
2. `HEAD` y `BODY` generan `<head>` y `<body>`. `LINK`, `SCRIPT` y `META` sólo
   dentro de `HEAD`.
3. `INCLUDE HTML "fragmento.html"` incluye marcado externo sin processar. Es el
   escape hatch deliberado para HTML que no controlamos.
4. Los elementos `H1`, `H2`, `P`, `BUTTON`, `INPUT`, `LIST`, `TEXT`, `CANVAS` y
   `DIV` declaran estructura y comportamiento. `TEXT x` refleja una variable de
   WASM. `ON CLICK`, `ON ENTER` y `ON INPUT` reciben el nombre de una `FUNCTION`.
5. El estado vive en WASM. El DOM se refleja. No hay estado duplicado en el DOM.
6. `SCREEN` dentro de `BODY` inyecta un `<canvas>` en ese punto.

## 8. Gramática formal

Ver [`docs/grammar/sourc.ebnf`](docs/grammar/sourc.ebnf).

## 9. Compatibilidad

La versión del lenguaje se declara con `--lang=1.0`. Un cambio incompatible crea
una versión nueva y el compilador avisa si el fuente no declara la suya.