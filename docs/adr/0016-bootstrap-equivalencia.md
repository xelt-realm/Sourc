# ADR 0016 — El bootstrap exige equivalencia funcional, no identidad de bytes

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

La forma clásica de demostrar que un compilador está auto-hospedado es el punto
fijo: `stage1(stage1.src) == stage1(src)`, byte a byte. Es una prueba fuerte,
difícil de discutir.

Cuesta unas 3 o 4 semanas más que una prueba de equivalencia funcional, porque
hay que perseguir diferencias cosméticas: orden de secciones, padding de la
tabla de nombres, diferencia entre una constantematerializada y una calculada,
representación de un literal flotante.

## Decisión

El objetivo de Stage 2 es **equivalencia funcional**, verificado con tres
pruebas:

1. **Semántica**: Stage 1 y Stage 0 coinciden en stdout, en IR canónico y en
   diagnósticos, sobre un corpus de al menos 300 programas, incluido el código
   del propio compilador.
2. **Idempotencia**: Stage 1(Stage 1) se comporta igual que Stage 1 sobre el
   corpus.
3. **Determinismo intra-compilador**: el mismo Stage 1 produce los mismos bytes
   dos veces seguidas. Este requisito sí es duro.

## Estado de la decisión (2026-10)

El juez de (1) y (2) existe ya: `tests/golden/ir/` tiene el IR canónico de los
17 programas del corpus y de los ejemplos, y `make corpus` lo regenera junto a
las salidas. El motivo de generarlos en la misma pasada es la revisión: el diff
enseña **qué instrucción** cambió al lado de **qué línea imprime**.

Dos cosas que la decisión daba por hechas y no eran ciertas:

- **`tests/goldens/` no existía.** La comparación de IR canónico no tenía
  contra qué mirar, así que la parte 1 de la decisión no se podía cumplir. Ahora
  es lo primero que se ejecuta después de F1.
- **`.gitignore` tenía `*.sir`**, pensado para el IR que deja un build, y con esa
  línea los 17 goldens quedaban fuera del control de versiones. Los tests pasaban
  en verde y en otra máquina no había nada: un fichero de gate que sólo existe en
  la máquina de quien lo generó. Está corregido con una negación explícita, y
  `test_los_goldens_de_ir_no_estan_ignorados` pregunta a `git` para que no
  vuelva a pasar.

**Hecho desde entonces:** `sourc diff` con las tres clases y sus tests, y
`docs/BOOTSTRAP.md` con el procedimiento paso a paso.

Sobre escribir el procedimiento antes de haberlo ejecutado: se escribió
después de construir las tres piezas que usa, no antes. La regla que sale de
ahí es que **el orden correcto es al revés** --ensamblar el instrumento, luego
documentarlo-- porque un procedimiento escrito antes describe lo que alguien
imaginó, y el que se escribe después puede citar líneas concretas que existen.

Lo que sigue pendiente y es de la Fase 8: los 300 programas del criterio (hoy hay
17 versionados más el fuzzer diferencial, que es donde saldrán los otros) y el
`stage1(stage1.src)` del idempotencia.

## Consecuencias

- El determinismo se mantiene como requisito duro, porque sin él no hay caché
  de build, ni reproducibilidad, ni forma de bisectar un bug.
- La búsqueda de diferencias se hace sobre el IR canónico, que es texto legible,
  en vez de sobre binarios. Un bug de bootstrap se localiza en minutos en vez de
  horas.
- `sourc diff <stage0.sir> <stage1.sir>` clasifica cada diferencia como
  **semántica**, de **orden** o de **formato**, y sale con código 1 sólo si hay
  semántica. El ADR nombra `--explain-stage-diff`; se llama `sourc diff` porque
  el CLI es de subcomandos y no tiene ningún otro flag en la raíz, así que un
  flag suelto ahí sería el único de su clase. La clasificación es lo que hace
  manejable esta decisión: sin ella, una diferencia que puede ser "se reordenó
  una línea" o "Stage 1 calcula otra cosa" se investiga igual de lento en los
  dos casos.
- El compilador Python sigue como oráculo durante seis meses después del
  bootstrap, precisamente porque ahora es el único juez de la equivalencia.
- `docs/BOOTSTRAP.md` documenta el procedimiento completo.