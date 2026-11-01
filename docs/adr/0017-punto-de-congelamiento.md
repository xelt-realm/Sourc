# ADR 0017 — Punto de Congelamiento del Lenguaje al final de la Fase 3

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

ADR 0001 congeló la gramática en `1.0` al final de la Fase 0. Pero eso es
aspiracional: `GRAMMAR.md` 1.0 documenta `ARENA`, `STRUCT`, acceso a campos,
`IMPORT`, `PAGE` y lowering de `GOTO`, y el compilador **rechaza** varios de
ellos hoy (`E0044` para ARENA, `E0025` para STRUCT y campos, `E0043` para GOTO;
PAGE ni se parsea).

Mientras el lenguaje se esté diseñando, cada bug admite una discusión de diseño
por delante: no está claro si el fallo es del compilador o si el lenguaje
todavía iba a cambiar. Eso es exactamente lo que encarece un proyecto: el coste
no es el bug, es la discusión sobre el bug.

## Decisión

Se define el **Punto de Congelamiento del Lenguaje (PCL) al final de la Fase 3**.

- Antes del PCL, `1.0` es el lenguaje **objetivo**. Cambiar la gramática es
  esperado y barato.
- A partir del PCL, `1.0` es un documento **cerrado**. Las Fases 4 a 7
  implementan lo que 1.0 ya promete; no lo redesignan. Cada bug pasa a ser un
  bug contra una especificación, y no hay nada que decidir.
- Cualquier cambio incompatible de sintaxis o de semántica requiere bump de
  `--lang` y un ADR.
- `v1.0.0` se corta en el PCL.

El PCL está donde está por una razón concreta: en ese momento existen en
Python las primitivas de texto, las de dibujo 2D y las de juego, que es lo que
un runtime y un compilador necesitan para no ser triviales. Congelar antes
congelaría primitivas cuya ergonomía aún no se ha puesto a prueba; congelar
después dejaría meses de lenguaje en movimiento.

## Consecuencias

- Fases 4 a 7: trabajo de implementación contra un texto, no de diseño.
- El compilador Python sigue creciendo (ADR 0018), pero su superficie de
  implementación cambia de todos modos al añadir nodos. El PCL protege la
  **semántica**, no esa superficie.
- `docs/ROADMAP-FASES.md` y `GRAMMAR.md` dejan de prometer cosas sin fecha: la
  brecha de 1.0 se cierra como trabajo planificado.
- Un hallazgo que pida cambiar el lenguaje en las Fases 4 a 7 no se resuelve
  cambiando el lenguaje: se resuelve dentro de 1.0 o se abre `2.0`. La presión
  es real y es intencionada: obliga a ajustar el diseño antes del PCL en vez de
  arrastrarlo.