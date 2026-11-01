# Detalle de las fases

Cada fase tiene objetivos concretos, entregables medibles, criterios de éxito y
riesgos propios. Los números de peso son presupuestos verificados en CI
(`budgets.toml`), no estimaciones.

Las fases 3 a 8 están planificadas pero no implementadas. La **Fase 2 está casi
completa** --sus entregables van marcados uno a uno más abajo--, y cuando una fase
se cierre sus criterios de éxito pasan a ser los tests correspondientes.

Lo que falta para cerrar la Fase 2, y por qué:

1. **El E2E en un Chromium de verdad.** `tools/e2e_cdp.mjs` está escrito y el
   servidor de ficheros se prueba sin navegador, pero no se ha podido ejecutar
   con un Chromium: en la máquina de desarrollo faltan diez bibliotecas del
   sistema (`libatk`, `libgbm`, `libasound`…) y no hay permisos de instalación.
   El salto está declarado y sale con código 3.
2. **Gráficos 2D**, que son la Fase 3.

Y una corrección de orden que la primera rebanada demostró: **el bootstrap no
depende sólo de la Fase 8 sino de la Fase 4**, porque el compilador entero
necesita `ARENA`. Pero **la primera rebanada no lo necesita**, y está hecha:
`compiler/lexador.src` es el lexer completo escrito en Sourc, en verde contra
Python en los 20 programas del corpus y en 23 casos léxicos que ningún programa
puede tener. `make bootstrap`.

Lo que la rebanada corrigió del plan es el *qué* era la primera rebanada, no el
*cuándo*: no era el serializador del IR --que exige construir el IR, y construirlo
es el compilador entero-- sino el lexer. Y corrigió tres cosas del lenguaje que
no estaban previstas, la más importante de ellas que **`AND` no cortocircuita**
(ADR 0020).

---

## Fase 0 — Documentos y decisiones

**Esfuerzo** 3 person-semanas · **Calendario** 2 semanas · **Estado** completada

### Objetivos
1. Convertir el brief en documentos normativos antes de escribir codegen.
2. Congelar la gramática en `1.0`.
3. Decidir por escrito las 14 ambigüedades del brief.
4. Fijar el contrato del runtime modular y el protocolo glue↔WASM.

### Entregables
`MANIFESTO.md`, `GRAMMAR.md`, `docs/grammar/sourc.ebnf`, `ARCHITECTURE.md`,
16 ADRs, `budgets.toml`, `examples/`, este roadmap.

### Criterios de éxito
- 3 personas con background QBasic leen `examples/` y lo entienden sin
  explicación.
- 14/14 ambigüedades del brief resueltas con ADR.
- Los bloques de código de la documentación se typechequean (`tests/test_docs.py`).

### Riesgos
- **Grammar churn**: congelado en `1.0`; los cambios incompatibles requieren
  bump de `--lang`.
- **Doc-bloat**: límite de 3 documentos + ADRs cortos.
- **Decidir sin datos de usuario**: revisiones externas de los ejemplos antes de
  congelar.

---

## Fase 1 — Frontend en Python

**Esfuerzo** 14 person-semanas · **Calendario** 7 semanas · **Estado** en curso

### Objetivos
1. `sourc check` con diagnósticos de calidad production.
2. `sourc run`: intérprete del IR. Acelera el proyecto entero y será el oráculo
   del testing diferencial.
3. IR canónico, determinista y serializable.
4. Cerrar por escrito el *compiler subset* nivel 1.

### Entregables
`diagnostics.py`, `lexer.py`, `ast.py`, `parser.py`, `types.py`, `sir.py`,
`interp.py`, `cli.py`; corpus de 14 programas con goldens; suite de 178 tests.

Bloque 2.0 (puentes de IR, completado): `layout_of()` como fuente única de
anchos y códigos de tipo · `array.alloc` lleva un código entero en vez de un
objeto `Type` de Python · `const.bool`, para que `BOOLEAN` tenga una sola
representación (i32) · `host_imports` completo con la tabla de colocación
inline/host/runtime · `W0004` sólo si el programa usa `SOURCE_2D` · la cifra
real de datos (10 bytes, no 11) con un test que la fija ·
`tests/test_ir_contract.py` como guardián del andamiaje Python.

Bloque 2.1 (encoder binario, completado): `compiler/sourc/wasm/encoder.py`.
**159 opcodes verificados uno a uno contra Node**, porque escribir noventa
opcodes de memoria es una fuente de errores garantizada y uno mal escrito no
falla hasta el navegador. Más `tools/fuzz_encoder.py`, que genera 1000 módulos
(`make fuzz`).

Bloque 2.2 (estructuración del control de flujo, completada):
`compiler/sourc/wasm/control.py`. Convierte los labels planos del IR en el árbol
de `block`/`loop` que WASM exige, y resuelve las profundidades de salto.

La regla de profundidad cabe en una línea:

    profundidad = nivel_del_salto - nivel_del_destino

y cubre los dos sentidos sin casos especiales: en un `block`, `br 0` salta a su
final; en un `loop`, `br 0` salta a su principio. Contarla a mano en el emisor
es la fuente clásica de módulos que **validan y calculan otra cosa**, así que
aquí sólo se construye un plan de emisión y el emisor lo recorre.

Dos reglas de estructura que costaron descubrir. Son:

- **Un bucle ocupa `[cabecera, extensión)`**, donde la extensión es hasta donde
  acaban todos los saltos que nacen dentro, calculada por punto fijo. No es
  "hasta el siguiente label": dentro de un bucle puede haber un `if` con su
  propia etiqueta de salida, y esa etiqueta es del bucle.
- **El `block` de una salida va fuera del bucle, no dentro.** Si empieza dentro,
  `br 0` sale del bloque pero no del bucle.

El fuzzer del encoder encontró dos bugs reales que ningún test de ejemplo
habría cazado:

1. **Grupos de locales de tamaño cero.** El backend los genera sin darse cuenta
   y el módulo entero deja de validar.
2. **Reordenar los locals por tipo.** parecía una optimización, pero permuta
   los índices en silencio: `f64` (0x7C) ordena antes que `i32` (0x7F), así que
   el local 0 deja de ser el primero declarado. **El módulo sigue validando**:
   no hay error, sólo valores equivocados. Los índices que emite el backend
   tienen que ser estables; agrupar por tipo es trabajo de `wasm-opt`.

### Criterios de éxito
- `PRINT "hola mundo"` produce 6 instrucciones IR y 10 bytes de datos.
- IR determinista byte a byte, 20 ejecuciones seguidas.
- Corpus 100% verde y sin alocaciones dentro de bucles.
- Todo bloque `source` de la documentación compila.

### Métricas
| Métrica | Objetivo | Actual |
|---|---|---|
| Startup del CLI | < 80 ms | ~55 ms |
| `check` de 1000 líneas | < 60 ms | ~35 ms |
| IR de `hola.src` | ≤ 40 instr | 6 |
| Anchos y códigos de tipo | una sola fuente | `layout_of()` |
| Opcodes WASM verificados | todos los usados | 159 de 159 |
| Módulos del fuzzer validados | 1000 | 1000 |
| Programas que estructuran bien | 16/16 | 16/16 |
| Tamaño del compilador | ≤ 4500 líneas | 5118 (ver nota) |
| Tests | creciente | 197 |

El compilador se pasó de 4500 líneas. El exceso son funciones de texto que la
Fase 1 no tenía en el alcance (`STR`, `VAL`, `LEFT`, `RIGHT`, `MID`, `CONTAINS`,
`INDEXOF`, `COMPARE`), el rasterizador `headless` que sustituye al navegador en
los tests de dibujo, y el subcomando `size`. Ninguna es invención: todas sustituyen
trabajo que en otra versión habría que repetir a mano en cada test.

El número se revisa al cerrar la Fase 1, cuando el compilador se congele para el
puerto a Source. Si al congelarlo sigue por encima de 6000, el problema no es que
el compilador sea grande, sino que hace demasiado: habría que preguntarse qué
se le puede quitar.

### Riesgos
- **El compilador Python se vuelve trabajo desechable**: estructura de ficheros
  idéntica a la del compilador en Source, congelado en la Fase 4 como oráculo.
- **IR demasiado abstracto**: los textos son `(ptr, len)` desde el día uno.
- **Typechecker permisivo**: test que verifica `runtime_checks == 0`.

---

## Fase 2 — Backend WASM + pipeline + glue mínimo

**Esfuerzo** 12 person-semanas · **Calendario** 6 semanas

> Prioridad absoluta: Node el día 3, navegador en la semana 2.

### Entregables
1. `compiler/wasm/encoder.py` — codificador binario WASM puro. Sin wabt, porque
   el auto-hospedaje necesita emitir binarios sin herramientas externas.
2. `compiler/wasm/emit.py` — lowering SIR→WASM.
3. `compiler/wasm/wat.py` — emisor de texto para diffs. **Hecho**, junto con
   el ensamblador: los dos son implementaciones independientes del mismo
   formato y que reenensamblen los 17 programas del corpus **byte a byte** es el
   oráculo estructural del que no hay herramienta externa. Cada uno de los dos
   lados encontró bugs que ningún test de ejemplo veía: el exponente de los
   flotantes normales, los dígitos de la mantisa de f32 y el `0x0.` del
   subnormal salían como números plausibles, y sólo el round trip los delató.
4. (retirado) `runtime/core` en Source: aplaza a la Fase 4.
5. ~~`runtime/native/*.wat`~~ — **el ensamblador está hecho y el directorio se
   queda vacío**. El reencuadre a `memory.copy` lo deja sin contenido: las tres
   cosas que justificaban el escape hatch --bump, `memcpy` y comparación de
   cadenas-- las baja el propio módulo. Emitir `memory.copy` sustituyó a
   `@__copy` y `06_textos.src` bajó de 1299 a 1249 B. La regla y el techo de
   1 KB se quedan para cuando `make check-subset` diga que algo no cabe.
   Ver las enmiendas 1 a 3 del ADR 0014.
6. `runtime/glue/glue_core.js` — ≤ 300 líneas.
7. `tools/host_node.mjs` — runner de Node.
8. Pipeline con pasos opcionales: sin binaryen el build sigue funcionando.
9. `sourc size` + gate contra `budgets.toml`. Las métricas del gate son `gzip`
   y `raw`, de la biblioteca estándar: con `brotli` como techo, un `make size`
   en una máquina sin ese binario **fallaba**, y un gate que no puede pasar en
   una máquina soportada no es un gate. `brotli` sigue en el informe como
   columna informativa.
10. **E2E por CDP en un Chromium. Hecho en un Chromium, no en tres.**
    `tools/e2e_cdp.mjs` habla CDP con `WebSocket` y `fetch`, que son globales
    de Node desde la versión 22, así que **no importa nada**: `node
    tools/e2e_cdp.mjs dist/hola hola.wasm` y ya está. Playwright y Puppeteer son
    dependencias, y la regla del manifiesto es no tenerlas; además traían tres
    navegadores por defecto y aquí uno basta, porque lo que se comprueba es el
    camino de carga y la paridad con el intérprete, no el motor de cada navegador.

    **Un solo Chromium y no tres** es una decisión que se debe poder defender:
    el riesgo real de no probar en tres es que el módulo use una instrucción que
    un motor no soporta, y eso lo cubre la validación del propio WASM y el
    `fuzzer del encoder`, que genera mil módulos y los enseña a Node. Lo que
    queda para tres navegadores es el texto y el layout, y para eso está el
    presupuesto de `index.html`.

    Si no hay Chromium, el script sale con **código 3** y lo dice. Un E2E que no
    se ejecuta y parece que pasa es peor que no tenerlo.

### Criterios de éxito
- `sourc build examples/hola.src --release` produce un `dist/` que funciona en
  navegador y da la misma salida que Node. **Hecho**: `dist/<programa>/` con
  cuatro ficheros --el módulo, el pegamento, la página y `sourc-manifest.json`--,
  y el presupuesto es **condición para escribir**, no un informe que sale
  después. Si el pegamento se pasa del techo, el `dist/` no se escribe y el build
  sale con 1. El `dist/` anterior se queda intacto: se monta en un temporal y
  se mueve entero.

  El criterio se comprueba **ejecutando la página del `dist/` con el pegamento
  del `dist/`**, que es lo único que convierte "se basta solo" en un hecho en vez
  de una frase del manual (`tests/test_dist.py`, con Node).
- Reducción del pipeline ≥ 45% sin binaryen, ≥ 70% con él.

### Presupuesto
| Artefacto | brotli | techo |
|---|---|---|
| `main.wasm` | 0.8 KB | 1 KB |
| `source_glue.js` | 2.5 KB | 3 KB |
| `index.html` | 1 KB | 1.5 KB |
| **TOTAL** | **12 KB** | **20 KB** |

`source_core.wasm` no entra: el primer módulo de runtime escrito en Source llega
en la Fase 4, con `ARENA`. Ver ARCHITECTURE.md §6.1.

### Riesgos
- **Bugs del encoder**: validar 1000 módulos con `WebAssembly.validate` y
  fuzzer propio.
- **binaryen ausente**: pipeline funcional sin él; CI con dos jobs.
- **El glue crece hacia framework**: presupuesto de líneas con test.

---

## Fase 3 — Gráficos 2D + game loop

**Esfuerzo** 12 person-semanas · **Calendario** 6 semanas

### Entregables
0. `source_core.wasm` en Source: allocator de bump, textos, math. Es el primer
   módulo de runtime escrito en Source, y es viable en esta fase porque el
   bump allocator de la Fase 2 le da `NEW`.
1. `source_2d.wasm` en Source con DisplayList SoA escrito in-place.
2. Protocolo del DisplayList versionado en `ARCHITECTURE.md`.
3. `glue_2d.js` con WebGL2 (primario) y Canvas2D (fallback), carga dinámica.
4. `SCREEN`, `CLS`, `RECT`, `CIRCLE`, `KEY`, `KEYDOWN`, `FLIP`.
5. Lint `W0004` para bucles sin `FLIP`, sólo en bucles exteriores.
6. Contador de alocaciones por frame en modo dev.
7. `examples/laberinto.src` funcionando, con test visual de píxeles.
8. Backend `headless` que renderiza a PPM desde Node.

### Criterios de éxito
- 60 fps a 640×480; ≥ 30 fps en WebKit.
- ≥ 500 000 comandos/s de construcción del DisplayList.
- **0 alocaciones por frame** en el laberinto (ya verificado hoy: 1 reserva, la
  del mapa, y 5 frames sin ninguna más).
- Misma imagen en WebGL2, Canvas2D y `headless`.

### Presupuesto
| Artefacto | brotli |
|---|---|
| `source_2d.wasm` | 7 KB |
| `source_core.wasm` | 7 KB |
| `glue_2d.js` | 6 KB |
| resto | 2 KB |
| **TOTAL** | **24 KB** (techo 28 KB) |

### Riesgos
- **Canvas2D lento**: el DisplayList es la abstracción correcta; el hint del
  compilador sugiere WebGL2 si detecta muchos `RECT`.
- **Divergencia entre ejecutores**: un ejecutor de referencia (`headless`) para
  los goldens.
- **Comparación de carácter en el bucle**: resuelta con el peephole del
  ADR 0004, ya implementado y medido.

---

## Fase 4 — Tipos avanzados, arenas, funciones de primera clase

**Esfuerzo** 14 person-semanas · **Calendario** 7 semanas

### Entregables
1. `STRUCT`, `ENUM`, arrays multidimensionales, `VEC*`/`MAT*` como structs de
   `f32`.
2. `ARENA ... END ARENA` con ámbito, checkpoints y prohibición estática de
   escapes (`E0031`).
3. `--gc=off` (por defecto), `--gc=wasm-gc`, `--gc=mark-sweep`.
4. `TYPE CALLBACK`, lambdas con environment en arena, tabla preasignada.
5. `USE` / `IMPORT FROM` (ADR 0010).
6. Pases de optimización con reporte `--stats`.
7. LSP básico: completion, hover, diagnostics.

### Criterios de éxito
- `1e8` iteraciones ≤ 0.4 s en WASM.
- Overhead de closure ≤ 2× la llamada directa.
- `RESET ARENA` O(1) y memoria plana tras 1M de iteraciones.
- `runtime_checks == 0` en los 100 programas del corpus.

### Riesgos
- **Feature creep antes del auto-hospedaje**: la regla es "el compilador en
  Source necesita esta feature" o no entra.
- **WasmGC no disponible**: camino principal sin GC, WasmGC opcional.
- **LSP roba tiempo del compilador**: timebox de una semana.

---

## Fase 5 — Modo PAGE + HTML/CSS generados

**Esfuerzo** 10 person-semanas · **Calendario** 5 semanas

### Entregables
1. Capa web del lenguaje: `PAGE`, `HEAD`, `BODY`, `LINK`, `SCRIPT`, `META`,
   `INCLUDE HTML`.
2. Elementos `H1`, `H2`, `P`, `BUTTON`, `INPUT`, `LIST`, `TEXT`, `CANVAS`, `DIV`
   con `ON CLICK`, `ON ENTER`.
3. Render sin vdom: estático en build time, dinámico por bridge, una pasada por
   cambio.
4. Seguridad: escapado total, CSP por defecto, sin `eval`.
5. CSS generado con tokens. Presupuesto 2 KB brotli.
6. Modos APP y SCRIPT.
7. `examples/chat.src`.

### Criterios de éxito
- Chat ≤ 35 KB brotli; first interactive ≤ 400 ms con 4× throttle.
- **≤ 3 escrituras DOM por interacción**, medido con un contador del glue.
- CLS = 0.

### Riesgos
- **Terminar construyendo un framework**: los *no-goals* de `MANIFESTO.md` son
  la defensa; si hace falta más, se recorta el lenguaje.
- **XSS**: fuzzing de valores en la página como criterio de aceptación.

---

## Fase 6 — Backend JS + `--target=auto`

**Esfuerzo** 10 person-semanas · **Calendario** 6 semanas

### Entregables
1. `compiler/js/` — JS idiomático desde el mismo IR. Sin `wasm2js`.
2. Estrategia de textos: emulación de memoria lineal para conservar un único IR.
3. `--target=auto` con modelo de coste documentado y `--target=auto-stats`.
4. `tests/parity/` — 100 programas, salida byte a byte idéntica entre backends.

### Criterios de éxito
- Paridad 100% en el corpus.
- Hola mundo JS ≤ 8 KB brotli.
- `auto` elige el menor total en ≥ 90% de un corpus de 50 programas.
- Ratio JS/WASM ≤ 3×.

### Riesgos
- **JS se convierte en un segundo lenguaje con bugs**: un solo IR y un solo
  corpus. Si el IR no sirve para ambos, se arregla el IR.
- **i64 en JS**: `Number` con rango seguro 2^53−1 y check en debug; `--i64=strict`
  con `BigInt`.

---

## Fase 7 — 3D + assets glTF

**Esfuerzo** 22 person-semanas · **Calendario** 11 semanas

**Alcance recortado y escrito**: escena estática, cámara, una luz, shadow map,
glTF estático, HUD 2D. PBR, SSR y post-processing quedan fuera.

### Entregables
1. `source_3d.wasm`: escena, cámara, transformaciones, culling, shadow map,
   niebla.
2. Primitivas `SCENE`, `CAMERA`, `MODEL`, `LIGHT`, `MESH`, `MATERIAL`.
3. Encoder Meshopt en Python y **decoder Meshopt en Source** (~300 líneas), para
   no depender de `gltfpack`.
4. Contenedor `.sourcpack` versionado, una sola petición.
5. `sourc assets` con caché.
6. WebGPU (primario) y WebGL2 (paridad completa).

### Criterios de éxito
- 60 fps a 1080p con 50k triángulos.
- Modelo de 50k tris ≤ 200 KB brotli; escena ≤ 1 MB.
- Decoder ≤ 6 KB brotli y cargado sólo si hace falta.
- 0 alocaciones por frame.

### Riesgos
- **Dependencias C++ rompen la promesa**: son opcionales y cacheadas; el
  objetivo es sobre el output del usuario, no sobre el toolchain del lenguaje.
- **WebGPU no está en todas partes**: WebGL2 debe ser equivalente.
- **Scope**: el recorte está escrito arriba y se revisa cada dos semanas.

---

## Fase 8 — Self-hosting

**Esfuerzo** 21 person-semanas · **Calendario** 11 semanas

| Paso | Contenido | Criterio |
|---|---|---|
| **1a** | Lexer, parser y tipos en Source | Mismo IR que Stage 0 en el corpus |
| **1b** | SIR y emitter WASM en Source | Hash tables deterministas, arenas, FFI mínimo de host |
| **1c** | Python fuera del camino crítico | Equivalencia funcional (ADR 0016) |

### Criterios de éxito
- 100% de coincidencia en stdout, IR canónico y diagnósticos sobre ≥ 300
  programas, incluido el propio compilador.
- `stage1(stage1.src)` se comporta como `stage1(src)`.
- Stage 1 produce los mismos bytes dos veces seguidas (requisito duro).
- Cero toolchain: `git clone && ./build.sh` sin Python, Node ni Rust.
- `sourc.wasm` compila 500 líneas en < 3 s en el navegador.

### Riesgos
- **Falta de features para escribir un compilador**: es el riesgo número uno del
  proyecto. El contrato de subconjunto (`make check-subset`) es un test de CI
  desde la Fase 1, no una intención.
- **Divergencia entre Stage 0 y Stage 1**: se compara IR canónico, que es texto,
  no binarios.
- **Long tail**: dos semanas de buffer para FFI, serialización y arenas.
## Deuda que dejó `tools/fuzz_source.py`

El fuzzer diferencial es la pieza que más bugs ha encontrado, y encontró **nueve**
en dos sesiones. La lista es corta porque la mayoría eran de la misma forma, y
esa forma tiene nombre: **el mensaje sonaba plausible y no señalaba el sitio**.

| Bug | Qué decía | Dónde estaba |
|---|---|---|
| `ABS`/`MIN`/`MAX`/`CLAMP` sin bajar | `E0058` | ninguna: faltaban de verdad |
| `select` con los dos valores al revés | nada, el módulo validaba | en el propio `select` |
| `eq` sobre `f64` comparando bits | nada | en un `.get(..., I64_EQ)` |
| `eq` de flotantes, `0.0 = -0.0` falso | `E0065` a cuatro líneas | dos variables con el mismo nombre |
| `-2^63` sin representar en i64 | `extra bits in varint` (de Node) | en el plegado de un literal |
| `AttributeError` en el CLI | `AttributeError` | un `Printer.diagnostic` inexistente |
| `STR(TRUE)` como `"1"` | nada | dos formateadores de booleano |
| `\` truncando hacia menos infinito | nada | un `idiv` delegado en `divfloor` |
| `-9 \ -9` dando `-1` | `E0058` de paridad | el signo del divisor, ignorado |

Cinco de los nueve los encontró **implementar una implementación independiente de
la misma especificación**, y cuatro los encontró una puerta escrita *antes* que
la feature. Ninguno lo encontró un test de estructura, y ése es el punto: una
puerta que comprueba que algo esté bien formado sólo encuentra cosas mal
formadas.

### Lo que queda

Una diferencia, y está en el ratchet (`tests/fuzz_diffs.json`) con su
reproducción y lo que ya se ha descartado:

- **Semilla 779000.** El intérprete produce 6751 líneas y el módulo 3891. Los
  bucles anidados coinciden por sí solos (126/126 en un caso mínimo), `FOR ...
  STEP` coincide, no hay `FLIP` y el módulo sale con código 0. Queda por mirar por
  qué algún bucle sale antes en el módulo.

Las puertas nuevas que vinieron de esta sesión están todas en `make release`, que
ahora tiene dieciocho pasos: las doce de antes --cobertura por feature, contrato
de diagnósticos, sonda de las tres mitades e invariantes del fuzzer-- más el
lexador escrito en Sourc, el AST del parser escrito en Sourc y las tres puertas
de la página. El fuzzer **no** está en la puerta mientras quede una diferencia
registrada, porque un gate que tolera un fallo entrena a ignorar el cartel.

### El patrón que sirve para lo que queda

Las diferencias que encuentra el fuzzer se corrigen **subiendo el caso al
corpus**, no sólo arreglando el código. El corpus es el juez de los goldens, así
que un programa que sale del fuzzer se convierte en regresión permanente con tres
ficheros mecánicos. Eso convierte el fuzzer en un motor de cobertura en vez de
una herramienta de una vez -- y es el camino corto a los **300 programas** que
pide el criterio de bootstrap, que no hay que escribir a mano.
