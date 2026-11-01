# ADR 0019 — Ratchet de paridad y puertas de release

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

Hay dos riesgos que no los detecta ningún test unitario:

1. **Completitud por sorpresa.** El backend baja el 90% de las operaciones y
   parece terminado; el 10% restante falla en el navegador, tres fases después.
2. **Deriva entre compiladores.** Stage 0 y Stage 1 se separan poco a poco y la
   divergencia se descubre cuando ya cuesta volver atrás.

Node es hoy un oráculo de tests: 35 de 197 tests lo usan y **se saltan si no
está**. Eso es deliberado: el build no depende nunca de Node, así que un entorno
sin Node ejecuta 162 tests en verde.

## Decisión

1. **Tripwire de completitud**, antes de escribir el backend: un test recorre el
   corpus, enumera cada operación que el frontend emite y falla si el backend no
   la declara. El backend se escribe después, no antes, para no fiarnos de la
   memoria de qué cubre.
2. **Contrato antes que lowering.** Para cada operación, primero el test que
   define qué entra, qué sale y de qué tipo; después, el lowering. Una
   operación mal bajada produce un módulo que **valida** y calcula otra cosa:
   sin contrato escrito primero, el fallo aparece en un golden que cambia solo.
3. **Paridad como ratchet.** Una tabla de paridad Stage0↔Stage1 por programa que
   **sólo crece**. Perder un programa verde es un fallo del build, igual que
   ganar uno exige que el test nuevo exista.
4. **`make release`** no etiqueta si algo falla: suite, fuzz, paridad del corpus,
   presupuesto de peso y reproducibilidad del artefacto.
5. **Node sigue siendo opcional.** Si falta, sus tests se saltan. Convertirlo en
   paso de build ataría el proyecto a un ecosistema de JavaScript que el
   manifiesto no quiere.
6. **La cobertura se mide por feature, no por operación del IR, y todo código de
   diagnóstico tiene un programa que lo provoque.** Las dos son la misma idea
   aplicada a dos medidas que estaban un nivel demasiado someras. Añadir una
   función nativa obliga a añadir la fila que la demuestra en
   `tests/test_sonda_nativas.py` --`check` acepta, `build` emite, los dos
   ejecutan igual--, y añadir un código de diagnóstico obliga a añadir el
   programa que lo provoca en `tests/casos_de_diagnostico.py`, o la reserva que
   explica por qué no se puede provocar.

## Estado de la decisión (2026-10)

Las cinco están implementadas. Lo que se aprendió al implementarlas, porque no
era lo que la decisión anticipaba:

- **(5) el salto declarado no basta.** `@skipUnless` sobre una clase es fácil y
  se olvida: 20 tests usaban Node sin guardarse y **fallaban** en vez de
  saltarse, así que una máquina sin Node rompía el build en el entorno que la
  decisión dice soportado. El skip es correcto y hay que vigilarlo igual que
  cualquier otra cosa: `TestGuardiasDeNode` recorre `tests/*.py` y falla si una
  clase llama a `run_in_node` sin su decorador. Y el harness cuenta y muestra
  los saltados, porque "281 en verde" y "281 en verde · 48 saltados" no son el
  mismo resultado.
- **(3) la tabla tiene que poder mentir para que Sirva.** Guardar los hashes sin
  más no impide nada: quien Encuentra un bug puede marcar su programa como rojo,
  escribir un motivo y la cobertura encoge sin que nadie se entere. La
  comprobación de si un programa **realmente** pasa tiene que estar donde se
  ejecuta el módulo, o sea en `tools/parity.py`, y el test sin Node sólo puede
  mirar la forma de las entradas.
- **(6) las dos medidas estaban un nivel demasiado someras, y ninguna puerta
  podía verlo.** El tripwire mide operaciones del IR y funciona: avisó de que el
  backend no bajaba `ABS`, `MIN`, `MAX` y `CLAMP`. Pero `call.native` es **una**
  operación, ya ejercitada por `RGB`, y detrás había veintitrés features. De
  esas, **7 no las demostraba ningún programa** del corpus ni de los ejemplos,
  cuatro de ellas recién implementadas. Con 392 tests en verde, `sourc check`
  aceptaba `ABS(-5)` y `sourc build` lo rechazaba con `E0058`.
  Con los diagnósticos pasaba igual: **49 códigos** que el compilador puede emitir
  y **15** aparecían en algún test, incluidos `E0058` y `E0065`, que son
  precisamente los dos que dispararon ese día. Y el hueco no era sólo de
  números: `Printer` no tenía `diagnostic`, así que el `except CompileError` del
  CLI se caía con un `AttributeError` en vez de imprimir el código. Un código
  que existe en el fuente no prueba que su camino funcione; sólo ejecutarlo lo
  prueba, y por eso los casos son programas que se compilan de verdad.
- **(6) una reserva sin caducidad es ruido que tapa el caso real.** Las dos
  puertas nuevas tienen reservas con motivo, y las reservas tienen dos
  propiedades: no pueden contener nada que ya no exista, y su número está
  acotado. Una nativa que se ha movido al corpus, o un código que ha cambiado de
  número, se quitan el mismo día; si no, la lista se llena de entradas muertas
  que se leen, no dicen nada, y esconden la nueva.
- **(6) un techo de peso que se sube cada vez que se añade cobertura ha dejado
  de notar regresiones.** El de `15_operaciones.src` hubo que subirlo tres veces
  en un día, una por bloque nuevo. Los bloques que no cabían en su presupuesto se
  fueron a programas con nombre propio --`16_nativas.src` y
  `17_truncamiento.src`-- con techo propio, y el original quedó donde estaba.
  Recortar la cobertura para cumplir un número invierte las dos cosas; subirlo
  cada vez es la tercera opción, y es la que no funciona.
- **(6) un artefacto que se puede empaquetar aunque no quepa es una copia con
  pasos extra.** `--release` existed, no hacía nada y salía con 0. Ahora el
  presupuesto es **condición para escribir**: si el pegamento se pasa, el `dist/`
  no se escribe y el build falla. Y al probarlo apareció el fallo que importaba:
  el pegamento se buscaba en `budgets.toml` como `glue_core.js` cuando ahí se
  llama `source_glue.js`, así que **no se comprobaba ningún techo** y se
  empaquetaba en silencio. Era el techo que aprieta más de todos --lo descarga el
  100% de las páginas-- y era el único que no miraba. Un mapa de alias entre el
  nombre del fichero y el del presupuesto, con un test que lo comprueba, es lo
  que evita la segunda vez.
- **(6) "se basta solo" tiene que ser ejecutable, no una frase.** Un `dist/`
  puede tener cuatro ficheros correctos y aun así no arrancar si el import del
  pegamento apunta al repo en vez de al lado. La comprobación es ejecutar **la
  página del `dist/` con el pegamento del `dist/`** y comparar con lo que da la
  del repo. Eso costó dos banderas en `tools/page_check.mjs` y una regla en el
  empaquetado: **el import se reescribe y nada más**. Reescribir más --meter el
  módulo en base64, quitar comentarios-- haría que el peso medido dejara de ser
  el peso que se descarga.
- **(4) un gate tiene que decir cuánto tarda y por dónde va.** La primera
  versión de `tools/release.py` no imprimía nada hasta terminar el primer paso,
  por `print` sin `flush` en una tubería: parecía colgada. Y un paso que sea el
  propio script con un flag se invoca a sí mismo para siempre; congeló una
  máquina. `tests/test_release.py` vigila las dos cosas, y el motivo está
  escrito en el sitio donde se vuelve a escribir el código.

## Consecuencias

- "El backend está completo" pasa a ser un hecho medido, no una promesa.
- El artefacto reproducible es requisito del bootstrap (ADR 0016), así que entra
  ahora y no al final: mismo fuente → mismos bytes de `main.wasm`.
- Un entorno sin Node da menos garantías sobre el WASM, y eso se acepta de forma
  explícita en lugar de ocultarlo. El panel de estabilidad muestra cuántos tests
  se han saltado.