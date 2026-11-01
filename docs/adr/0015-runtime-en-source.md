# ADR 0015 — El runtime se escribe en Source

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

El runtime (`source_core`, `source_2d`, `source_3d`, `source_web`, `source_sound`,
`source_net`) podría escribirse en C, en Rust o en WebAssembly a mano.

La opción en C o Rust es cómoda al principio, y crea tres problemas a largo
plazo:

1. Hay dos implementaciones del runtime mientras dure el auto-hospedaje.
2. El auto-hospedaje requiere que el runtime se pueda compilar con el compilador
   del proyecto, es decir, con el propio Source.
3. La promesa del manifiesto incluye que el runtime es auditable por quien usa
   el lenguaje. C no lo es tanto.

Las desventajas de escribirlo en Source desde el inicio son la ruta de arranque:
el runtime necesita cosas (textos, arrays, arenas) que el lenguaje todavía no
tiene.

## Decisión

1. El runtime se escribe en Source, desde la Fase 2.
2. Lo que no cabe en Source, va a `runtime/native/*.wat` (ADR 0014), con un
   límite de 1 KB por módulo.
3. El glue JavaScript se queda en JavaScript: WebGPU, WebGL2, DOM y WebAudio no
   existen en Source, y nadie va a inventar un FFI para ellos.
4. Cada fase declara un **subset level**. El runtime sólo puede usar features del
   nivel de la fase en curso, y un test de CI (`make check-subset`) falla si usa
   algo más.

## Estado de la decisión (2026-10)

El punto 4 está implementado: `subset.toml` declara el nivel 2 y
`sourc check --nivel=2` lo aplica **durante la compilación**, con posición. El
chequeo va después del parseo y antes del typecheck, y el orden tiene un motivo:
si un programa usa `ARENA` y además tiene un error de tipos, quien lo escribe
quiere saber primero que está por encima de su nivel.

`make check-subset` corre `tests/test_subset.py`, que es donde vive la política
de verdad. No es un script aparte porque un gate que no está en el camino del
build se desactiva el día que moleste.

**El vocabulario se nombra por nodo del AST, no por lo que el parser acepta.**
Es la diferencia entre un gate y un adorno: `STRUCT`, `ENUM` y los arrays
multidimensionales no parsean todavía, y un vocabulario hecho con «las cosas que
hoy compilan» no los mencionaría. El día que la Fase 4 los traiga, ese gate se
autoautorizaría solo el día exacto en que tiene que apretar.

**Lo que no se puede comprobar todavía, se vigila por otro camino.** Esas cuatro
features no pueden entrar por `mal/` esperando un `E0066`: mueren en el parser
antes de que el gate las vea. Lo que las vigila es
`TestElGateAvisaCuandoLlegaLaFeature`, que **falla el día que empiezan a
parsear** y el mensaje dice exactamente qué mover. El gate completo es: para lo
que parsea, `E0066`; para lo que no, un test que avisa.

Dos cosas que la decisión daba por hechas y no eran ciertas:

- **El compilador en Source es un solo fichero** (`compiler/sourcc.src`), así que
  `USE` e `IMPORT` están fuera del contrato. No es una decisión permanente: se
  ensancha el día que el fichero se divida, y por eso su prohibición dice
  "antes de dividirlo hay que poder escribirlo entero".
- **`INPUT` está en GRAMMAR §5 y no parsea.** Documentación que promete una
  primitiva que no existe, y justo la que un compilador necesitaría para leer su
  propio fuente. Está en `sin-parsear/` con las otras tres, así que cuando se
  implemente el gate tendrá que decidir si el compilador puede usarla.

El `coste` de cada prohibición está escrito, y el más caro es `campo`: sin
`STRUCT`, un nodo del IR son arrays paralelos con índices enteros, y un índice
mal escrito lee el campo de otro nodo sin que el typechecker pueda verlo. Es la
familia de bug de la lista de trampas de `AGENTS.md`, y está escrita en
`subset.toml` para que la Fase 4 se decida con el número delante.

Lo que no existe todavía: `compiler/sourcc.src`. El gate no tiene entrada real, y
`TestElCompiladorEstaEnElNivel` se salta diciendo exactamente eso, que es lo que
hace el panel de estabilidad que se añadió con ADR 0019.

## Consecuencias

- No hay doble mantenimiento del runtime.
- El compilador tiene que ser utilizable para escribir el runtime antes de
  poder auto-hospedarse. Es una presión permanente sobre el subconjunto del
  lenguaje, y es exactamente la presión que hace falta.
- La regla del subconjunto es el riesgo número uno del proyecto. Por eso es un
  test de CI, no una intención.
- `source_core.wasm` es compilado por el compilador Python en las Fases 2 a 7, y
  por el compilador en Source a partir de la Fase 8.