# ARCHITECTURE

Diseño técnico de Source. Normativo: cualquier cambio requiere un ADR.

## 1. Vista general

```
   hola.src
      │
      ▼
  ┌───────────────────────────────────────────────┐
  │  FRONTEND (compartido por todos los backends) │
  │                                               │
  │  lexer → parser → typechecker → SIR           │
  │                  (diagnósticos con span)       │
  └───────────────────┬───────────────────────────┘
                      │  SIR canónico, determinista
          ┌───────────┴───────────┐
          ▼                       ▼
   ┌─────────────┐        ┌─────────────┐
   │  WASM       │        │  JS         │
   │  backend    │        │  backend    │
   └──────┬──────┘        └──────┬──────┘
          │  main.wasm            │  main.js
          ▼                      ▼
   wasm-snip → wasm-opt → strip → brotli
          │                      │
          └──────────┬───────────┘
                     ▼
        source_glue.js + source_core.wasm
                     ▼
                  dist/
```

El frontend es único. El SIR es un contrato: todo lo que el lenguaje puede hacer
tiene que ser representable en él, porque los dos backends deben poder generarlo.
Si el IR no sirve para ambos, el IR está mal — no se añaden banderas.

## 2. Estructura del repositorio

```
compiler/sourc/          Frontend en Python (Stage 0), sin dependencias
  diagnostics.py         Errores, warnings, spans, renderizado con caret
  lexer.py               Tokens con span
  ast.py                 Nodos del AST
  parser.py              Parser descendente recursivo
  types.py               Sistema de tipos e inferencia
  sir.py                 IR canónico + serializador determinista
  interp.py              Intérprete del IR (oráculo de tests)
  cli.py                 sourc check | run | build | test | bench | size

src/sourc/               El mismo compilador, escrito en Source (Fase 8)

runtime/
  core/                  source_core.wasm — allocator, textos, math
  two_d/                 source_2d.wasm — DisplayList 2D
  three_d/               source_3d.wasm — escena, cámara, luces
  web/                   source_web.wasm — DOM, routing
  sound/                 source_sound.wasm
  net/                   source_net.wasm
  native/                Módulos WAT mínimos (allocator de bump)
  glue/
    glue_core.js         Siempre presente. ~300 líneas
    glue_2d.js           WebGL2 + Canvas2D. Bajo demanda
    glue_3d.js           WebGPU + WebGL2. Bajo demanda
    glue_web.js          DOM + routing. Bajo demanda
    glue_sound.js        WebAudio. Bajo demanda
    glue_net.js          WebSocket + fetch. Bajo demanda

tools/
  asset_pipeline.py      glTF/GLB → sourcpack, encoder Meshopt
  budget.py              Comprobación de presupuestos de peso
  testharness.py         Corpus, goldens, diferenciales
  host_node.mjs          Runner de Node para CI

tests/
  corpus/                Programas fuente del corpus
  golden/sir/            IR esperado
  golden/out/            Salida esperada
  errors/                Snapshots de diagnósticos
  parity/                WASM vs JS
  e2e/                   CDP + un Chromium, sin Playwright (ADR 0019)

bench/                   Microbenchmarks y registros
examples/                Programas de ejemplo que deben compilar
docs/adr/                Decisiones
budgets.toml             Presupuestos de peso (gate de CI)
```

## 3. SIR — el IR canónico

Nombre: **SIR** (Source IR). Forma SSA-ish con control estructurado y memoria
explícita. Serializable de forma determinista.

### 3.1 Módulo

```
Module {
    name: str
    lang_version: str
    types:    [Type]           # structs, arrays, refs a función
    funcs:    [Func]           # orden determinista: por índice de definición
    globals:  [Global]
    strings:  [DataSegment]    # constantes de texto y arrays literales
    imports:  [HostImport]
    exports:  [Export]
    runtime:  [str]            # módulos del runtime usados: ["SOURCE_2D"]
}
```

### 3.2 Función

```
Func {
    index: int
    name: str
    sig: FuncSig                 # params, result
    locals: [Local]              # todas tipadas; slots de memoria
    blocks: [Block]
    arenas: [ArenaDecl]
}
```

### 3.3 Instrucciones

Valores: `ValueId` (enteros sequential, deterministas) y `TypeId`.

| Grupo | Instrucciones |
|---|---|
| Constantes | `const.i64`, `const.f64`, `const.f32`, `const.ptr(len)`, `const.null` |
| Aritmética | `add`, `sub`, `mul`, `div`, `rem`, `neg`, `abs`, `min`, `max`, `shl`, `shr`, `and`, `or`, `xor` |
| Conversión | `conv.i64.f64`, `conv.f64.f32`, `conv.u8`, `ext.s8`, `trunc.f64.i64`, … |
| Memoria | `load`, `store`, `load.8u`, `store.8`, `memcpy`, `memset` |
| Texto | `str.len`, `str.index`, `str.concat`, `str.compare`, `str.sub`, `str.byte` |
| Control | `block`, `loop`, `br`, `br_if`, `return`, `unreachable`, `select` |
| Llamada | `call`, `call_indirect`, `host.call` |
| Arena | `arena.new`, `arena.reset`, `arena.mark` |
| Comandos | `dl.push` (DisplayList), `dl.clear`, `flip` |

### 3.4 Reglas del IR

Estas reglas son la razón de que el compilador sea pequeño y el código generado
sea pequeño:

1. **No hay strings como objetos.** Un texto es `(ptr, len)`. Si el IR tuviera un
   tipo `Str`, cada backend tendría que inventar un objeto y el compilador
   dejaría de ser trivialmente portable.
2. **No hay conversiones implícitas.** `conv` es explícito. El typechecker resuelve
   todo en compilación; los runtime solo validan fronteras (`INPUT`, assets,
   `KEY`).
3. **No hay boxing.** Un `INTEGER` es un `i64` en un slot. Nada más.
4. **Los composites son memoria.** `VEC3` es un struct de 3 `f32` en un slot. El
   IR no tiene registros vectoriales salvo con `--simd`.
5. **Orden determinista.** Todos los mapas del compilador son ordenados. Sin
   iteración dependiente del hash.
6. **Los números se serializan por bits.** Un `DOUBLE` en el serializador va como
   su patrón de 64 bits en hexadecimal, para que el texto del IR sea idéntico en
   cualquier plataforma.

### 3.5 Ejemplo

El programa que cuesta 6 instrucciones y 10 bytes de datos:

```source
PRINT "hola mundo"
```

produce exactamente:

```
func @main() -> i32:
  %0 = const.i64 1
  %1 = const.i64 2
  %2 = add.i64 %0, %1
  store.i64 @slot_x, %2
  %3 = const.ptr @str_0, len=10
  host.call @print(%3)
  %4 = const.i32 0
  return %4

str_0: bytes = [104,111,108,97,32,109,117,110,100,111]
```

6 instrucciones y un segmento de datos de 10 bytes. Ese es el objetivo: **un
concepto entero se materializa en decenas de bytes de WASM**, y
`tests/test_docs.py` verifica esta cifra para que no se degrade en silencio.

Un programa con estado real, todavía más pequeño:

```source
DIM total AS INTEGER = 0
FOR i = 1 TO 100
    total = total + i
NEXT i
PRINT "total: " & STR(total)
```

## 4. Mapeo de tipos

| Source | WASM | Notas |
|---|---|---|
| `INTEGER` | `i64` | Rango completo con signo |
| `SINGLE` | `f32` | |
| `DOUBLE` | `f64` | |
| `BOOLEAN` | `i32` | 0 / 1, sin trampas: leer un booleano fuera de 0/1 es error de compilación |
| `STRING` | `i32 ptr`, `i32 len` | UTF-8 |
| `COLOR` | `i32` | `0xAARRGGBB` |
| `ARRAY OF T` | `i32 ptr`, `i32 len`, `i32 cap`, `i32 elem` | Descriptor de 4 palabras; la cuarta es el código de `T` |
| `VEC2/3/4` | `f32 × n` en memoria | |
| `MAT3/MAT4` | `f32 × 9/16` en memoria | |
| `FUNCTION ref` | `i32` (índice en tabla) | `call_indirect` con typecheck estático |
| `NIL` | `i32 0` | |

En el backend JS, `INTEGER` mapea a `Number` con rango seguro 2^53-1 y
comprobación en builds debug. `--i64=strict` usa `BigInt`. Ver `docs/adr/0007`.

## 5. Layout de memoria

Un solo `memory` WASM con regiones asignadas estáticamente por el compilador:

```
0x00000  ┌──────────────────────────┐
         │  estáticos: textos, arrays literales, tablas de función │
         ├──────────────────────────┤
         │  shadow stack / frames   │   16 bytes por frame
         ├──────────────────────────┤
         │  globals                 │
         ├──────────────────────────┤
         │  arenas (por arena)      │   bump, tamaño calculado en compilación
         ├──────────────────────────┤
         │  heap opcional           │   sólo si --gc
         ├──────────────────────────┤
         │  DisplayList             │   SoA, escrito in-place
         ├──────────────────────────┤
         │  pila de evaluación       │
0x100000 └──────────────────────────┘  paginado por `memory.grow`
```

Una arena es bump allocation: un cursor sube, `RESET ARENA` lo baja a un
checkpoint. Sin malloc, sin free, sin recolector. Las fugas dentro de un bucle se
evitan con checkpoints, y el compilador sabe cuántas celdas necesita cada bucle
porque toda forma de dato tiene tamaño conocido en compilación.

### 5.1 Disposición de cada tipo

Una sola tabla, `layout_of()` en `types.py`, decide el tamaño, la alineación y
el número de palabras de cada tipo. El backend WASM, el futuro backend JS y el
allocator la consultan; nadie mide por su cuenta.

| Tipo | Bytes | Alineación | Palabras | Anotación |
|---|---|---|---|---|
| `BOOLEAN` | 4 | 4 | 1 | i32, 0 o 1. **Una sola representación**, sin excepciones |
| `COLOR` | 4 | 4 | 1 | i32 `0xAARRGGBB` |
| `INTEGER` | 8 | 8 | 1 | i64 |
| `SINGLE` | 4 | 4 | 1 | f32 |
| `DOUBLE` | 8 | 8 | 1 | f64 |
| `STRING` | 8 | 4 | 2 | ptr, len |
| `ARRAY` | 16 | 4 | 4 | ptr, len, cap, tipo del elemento |
| `VEC2` … `MAT4` | 8 … 64 | 4 | 2 … 16 | structs de f32 en memoria (ADR 0008) |
| `VOID` | 0 | 1 | 0 | no ocupa memoria |

**`BOOLEAN` tiene una representación única y es una regla, no una casualidad.**
`DIM b AS BOOLEAN = TRUE` produce `const.bool` (i32), no `const.i64`; un entero
convertido a `BOOLEAN` pasa por `to.bool`. Si un valor `BOOLEAN` pudiera ser i64
en un sitio y i32 en otro, el backend no podría elegir el tipo de un local: o
generaría un módulo inválido, o leería 4 bytes donde hay 8. La lista de
operaciones que producen i32 está en `tests/test_ir_contract.py`, escrita al
revés: es el contrato, no un comentario.

### 5.2 Códigos de tipo en el IR

El IR lleva **números**, no objetos de tipo. `array.alloc` lleva el código entero
del elemento (`T_INT`, `T_F64`, `T_STR`…), y quien ejecuta decide qué significa
cada código: el intérprete hace listas de Python, WASM hace memoria lineal.

No es purismo. El compilador en Python es andamiaje temporal (ADR 0016) y en la
Fase 8 estará escrito en Source, donde no hay clases. `array.alloc` llevaba un
`Type` de Python como operando durante toda la Fase 1; se printaba como
`INTEGER` por casualidad de `Type.__str__`, así que el bug era invisible en los
goldens y habría estallado en el backend al pedir el tamaño del elemento.
`tests/test_ir_contract.py` recorre el IR de todo el corpus y falla ante
cualquier operando que no sea un valor portable.

## 6. ABI del host

El glue expone estas funciones al WASM. Los nombres están versionados; cambiarlos
es un cambio incompatible.

### 6.1 Dónde vive cada operación

Cada operación del IR tiene una colocación declarada, y el backend falla si
encuentra una que no esté en la tabla (`sir.py`: `INLINE_OPS`, `HOST_ABI`,
`NATIVE_PLACEMENT`, `OPS_DEL_MODULO`). "El backend está completo" nunca es una
sorpresa.

| Colocación | Qué es | Por qué |
|---|---|---|
| `INLINE` | El backend emite una instrucción WASM | Sin glue, sin import, cero peso |
| `HOST` | Import del host, implementado en `source_glue.js` | Necesita estado externo |
| `MODULO` | Función que genera el propio módulo | Es semántica del lenguaje, no del host |
| `RUNTIME` | Función de un módulo de runtime, en Source | Llega con `ARENA` en la Fase 4 |

**`MODULO` es `HOST` con el peso al revés.** El caso es `PRINT` de un array: el
formateo lo hace `@__printArray` dentro del WASM, y el host sólo formatea un
número suelto. En el pegamento eran unos 450 B brotli que pagaba **cada página**
por algo que casi ningún programa usa, y además era lógica del lenguaje escrita
en JavaScript, que es justo lo que la Fase 4 va a tener que traducir. La regla
que salió de ahí: si el host necesita conocer la disposición de un array o un
texto para hacer su trabajo, ese trabajo no es suyo.

Tres funciones internas genera hoy el backend, y sólo si el programa las pide:
`@__bump` (el allocator), `@__copy` (concatenar) y `@__printArray`.

La categoría de destino final está anotada en el código, y esa anotación no es
informativa: es la deuda visible. Cada `HOST` que debería ser `RUNTIME` es un
paso pendiente de la Fase 4, no trabajo olvidado.

**Por qué no hay runtime en Source en la Fase 2.** El lenguaje no tiene
superficie de memoria: no existe `buf[i] = x` sobre bytes, ni `PTR`, ni arenas.
Un `source_core.wasm` escrito hoy sólo podría contener math y un buffer de
`PRINT`: un artefacto a medias que se tiraría entero en la Fase 4. Los 7 KB que
`budgets.toml` reservaba para él salen del presupuesto, y el primer módulo de
runtime en Source llega con `ARENA`, que es cuando puede existir de verdad.

### 6.2 Imports de la Fase 2

```
source.print_str(ptr, len)            escribe un texto UTF-8
source.print_int(i64)                 escribe un entero
source.print_num(f64)                 escribe un flotante
source.print_bool(i32)                escribe TRUE o FALSE
source.num2str(ptr, f64, i32) -> i32     formatea un flotante en el heap
source.int2str(ptr, i64) -> i32          formatea un entero en el heap
source.str_len(ptr) -> i32               longitud del texto recién formateado
source.str2num(ptr, len) -> f64       analiza un texto a número
source.key(ptr, len) -> i32           tecla mantenida
source.keydown(ptr, len) -> i32       flanco de este frame
source.flip()                         fin de frame
source.dl_screen(x, y, title_ptr, title_len)
source.dl_cls(color)
source.dl_rect(x, y, w, h, color)
source.dl_circle(x, y, r, color)
source.dl_pset(x, y, color)
source.dl_line(x1, y1, x2, y2, color)
source.dl_triangle(...)
source.dl_text(x, y, ptr, len, color)
source.dl_color(index)
```

Imports separados por tipo en vez de uno solo con un byte discriminante: son más
autoexplicativos, y la sección de imports cuesta 6 bytes por función.

**La mecánica de pesos, medida antes de tener el backend:**

| Programa | Imports que arrastra |
|---|---|
| `hola.src` | **1**: `source.print_str` |
| `laberinto.src` | 6, todos de dibujo |
| 10 de 16 programas del corpus | necesitan `num2str` |

Un hola mundo no descarga ni una función de dibujo. Eso es la carga condicional
funcionando, y hay un test que lo fija.

`num2str` es la única función de runtime que empieza en el glue: un formateador
de DOUBLE a texto son ~200 instrucciones de WASM que no vale la pena escribir en
el backend, y ~12 líneas de JavaScript que sí. Se mueve a Source en la Fase 4.

El WASM expone:

```
main() -> i32                              código de salida
memory                                    linear memory
```

Regla dura: el glue **no** importa nada fuera de APIs web. Un test falla si
`glue_core.js` supera 400 líneas o si usa una librería.

## 7. DisplayList

El WASM escribe comandos vectoriales en su propia memoria; el glue los ejecuta
con el backend disponible. Sin objetos, sin `GC`, sin copias intermedias.

```
Cabecera (32 bytes):
  magic:    u32  "SDL1"
  version:  u32  1
  frame:    u32
  width:    f32
  height:   f32
  dpr:      f32
  count:    u32   número de comandos
  _pad:     u32

Comando (32 bytes, SoA en 4 arrays de f32 paralelos):
  op:      u32   SRC_PUSH | SRC_LINE | SRC_RECT | ...
  x,y:     f32
  w,h:     f32
  r,g,b,a: u8 × 4
  flags:   u32   SRC_CLIP_BEGIN | SRC_CLIP_END | SRC_BATCH_BREAK
```

El pipeline por frame es: construir → `dl_submit` → el glue sube un solo buffer
preasignado → ejecuta → `FLIP` presenta. Cero asignaciones por frame.

## 8. Modos web

| Modo | Qué genera | Cuándo |
|---|---|---|
| `SCRIPT` | `.wasm` + glue, para incrustar en HTML existente | Hay una página y Source sólo aporta lógica |
| `APP` | `.wasm` + glue + `<source-app src="app.wasm">` | Isla de Source dentro de una página existente |
| `PAGE` | `index.html` completo + `.wasm` + glue + CSS | Por defecto. El usuario no escribe HTML |

El modo se declara con `--mode`, o dentro del fuente con `PAGE` / `APP` /
`SCRIPT`.

### 8.1 Modelo de render de PAGE (sin vdom)

1. Lo estático se resuelve en **build time**: `H1 "Puntuación"` produce HTML
   final. No hay hidratación.
2. `TEXT x` marca `x` como sucio cuando cambia.
3. Una pasada de render escribe **sólo** los nodos sucios, en un solo turno, sin
   lecturas intercaladas (sin *layout thrash*).
4. Los eventos van a WASM, que decide. El DOM no guarda estado.

Coste: ≤ 3 escrituras DOM por interacción de usuario. Es un criterio de
aceptación, medido con un contador expuesto por el glue.

### 8.2 Seguridad

- Todo valor interpolado se escapa. `innerHTML` nunca recibe datos.
- CSP por defecto: `script-src 'self' 'wasm-unsafe-eval'` (Safari no soporta
  `wasm-unsafe-eval`; se documenta el requisito alternativo).
- Sin `eval` en el glue.
- SRI opcional para recursos externos.
- Fuzzing de valores en la página como criterio de aceptación.

## 9. Target matrix

| Target | Requiere | Memoria | Notas |
|---|---|---|---|
| `wasm/full` | WasmGC, SIMD, bulk memory, tail calls | WasmGC | Por defecto en navegadores modernos |
| `wasm/fallback` | MVP + bulk memory | arenas | Semántica completa, GC manual opcional |
| `js` | cualquier navegador con ES2017 | heaps JS | Backend de primera clase, no un parche |

La detección de features ocurre en el glue y decide qué módulo de runtime
descargar. Un mismo `.src` compila a los tres.

## 10. `--target=auto`

Decide sin compilar dos veces, con un modelo de coste estático sobre el SIR.

**Gana WASM cuando:**

- El IR supera 12 KB de instrucciones.
- Hay bitwise sobre `i64`, `DIV`/`MOD` de enteros, o `call_indirect`.
- El programa asigna en un bucle que se repite mucho.
- Se usa SIMD (`--simd`).
- El trabajo es sostenido (más de N iteraciones por frame).

**Gana JS cuando:**

- El IR es pequeño (< 12 KB) y el arranque domina.
- No hay closures ni funciones de orden superior.
- El total brotli estimado de JS queda por debajo del de WASM + glue_core.

`--target=auto-stats` imprime la decisión, el razonamiento y **ambos** tamaños
estimados, para que la decisión sea auditable.

Criterio de aceptación: `auto` elige el menor total brotli en ≥ 90% de un corpus
de 50 programas.

## 11. Determinismo

Requisito duro, no opcional:

1. Ningún mapa del compilador itera en orden de hash.
2. Los `DOUBLE` se serializan por patrón de bits.
3. La versión de binaryen está fijada en `tools/toolchain.lock`.
4. Un test verifica que el mismo fuente produce el mismo `.sir` 1000 veces, con
   hash seed aleatorio.
5. El build cache usa el hash del IR canónico, no el mtime del fuente.

## 12. Pipeline de build

```
sourc build hola.src --target=web --release
```

1. Lexer y parseo.
2. Typecheck.
3. Lowering a SIR.
4. Optimizaciones sobre el SIR (folding, DCE, inlining).
5. Decisión de backend (`--target=auto`).
6. Codegen.
7. `wasm-snip` propio (eliminar exports no usados, preprocesado).
8. `wasm-opt -Oz` — **opcional**, si binaryen está disponible.
9. Strip de nombres y debug.
10. Generación de HTML (si el modo es PAGE).
11. Generación/copia de CSS.
12. Copia y optimización de assets.
13. Escritura del glue (selección por imports del SIR).
14. Brotli.
15. `sourc size`: informe y gate contra `budgets.toml`.

Los pasos 7 a 14 son opcionales en el sentido de que el build **nunca falla** si
faltan herramientas externas. El informe dice explícitamente qué pasos se
aplicaron. Esto es obligatorio: el entorno de desarrollo puede no tener binaryen,
y un compilador que necesita `wasm-opt` para funcionar no cumple su premisa.

## 13. Bootstrap

| Stage | Qué es | Quién lo compila |
|---|---|---|
| 0 | El compilador en Python | Python |
| 1 | El compilador en Source | Stage 0 |
| 2 | El compilador en Source, compilado por sí mismo | Stage 1 |

El objetivo de Stage 2 es **equivalencia funcional**, no identidad de bytes:

1. **Semántica**: Stage 1 y Stage 0 coinciden en stdout, en IR canónico y en
   diagnósticos sobre un corpus de ≥ 300 programas, incluido el código del
   propio compilador.
2. **Idempotencia**: Stage 1(Stage 1) se comporta igual que Stage 1.
3. **Determinismo intra-compilador**: el mismo Stage 1 produce los mismos bytes
   dos veces. Este requisito sí es duro, porque sin él no hay caché, ni
   reproducibilidad, ni forma de bisectar un bug.

Ver `docs/adr/0016` y `docs/BOOTSTRAP.md`.

## 14. Presupuesto de peso

`budgets.toml` es la fuente de verdad. `sourc size` lo aplica con 2% de tolerancia
y devuelve código de salida 1 si se supera.

```toml
[[budget]]
artifact = "TOTAL"
mode     = "page"
target   = "wasm"
example  = "hola.src"
brotli   = 20480

[[budget]]
artifact = "glue_core.js"
brotli   = 2560
```

## 15. Extensión: cómo añadir un módulo de runtime

1. Se escribe en Source, usando sólo el subset level de la fase actual.
2. Se declara un `HostImport` por cada función que necesita del glue.
3. Se añade `USE SOURCE_X` al lenguaje si expone primitivas.
4. Se escribe el glue correspondiente, con carga dinámica.
5. Se añade un ejemplo que lo use. Sin ejemplo, el módulo no entra.

## 16. Glosario

| Término | Significado |
|---|---|
| SIR | Source IR. El contrato entre frontend y backends |
| Arena | Región de memoria bump con ámbito |
| DisplayList | Lista de comandos de dibujo SoA que produce WASM y ejecuta el glue |
| Subset level | Nivel de features del lenguaje permitido a un componente en una fase |
| Stage | Etapa del bootstrap del compilador |
| Budget | Techo de peso verificado en CI |