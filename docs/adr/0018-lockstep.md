# ADR 0018 — Lockstep: Python sigue creciendo tras el PCL

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

1.0 promete constructos que el compilador todavía no tiene: `ARENA`, `STRUCT`,
funciones de primera clase (ADR 0004/0009 dependen de ellos). Hay dos caminos:

- **Congelar Python** en lo que tiene. Las features de la Fase 4 en adelante
  existirían sólo en Source.
- **Lockstep**: Python las implementa también, en paralelo con Source.

La tentación es congelar, porque parece más barato. Y es un error: Python es el
**oráculo**. El testing diferencial (ADR P3) compara dos implementaciones de la
misma semántica; si una de ellas no implementa la feature, no hay comparación, y
las fases con más bugs posibles (memoria, arenas, closures) se quedan sin red.

## Decisión

Tras el PCL, el compilador en Python **sigue creciendo** para cubrir todo lo que
1.0 promete. Cada feature de las Fases 4 a 7 se implementa dos veces, y el test
diferencial sale gratis.

## Consecuencias

- El compilador Python crece de unos 6585 a unos 9000 líneas. Es trabajo que no
  se tira: es a la vez el compilador de producción, el generador de bootstrap y
  el oráculo del bootstrap.
- El coste es ~2x por feature de las Fases 4 a 7. Se paga en capacidad, no en
  calendario, si se reparte en paralelo.
- Python se congela al llegar a Stage 1 (paridad funcional), momento en que ya
  no hace falta para construir nada.
- La deriva entre las dos implementaciones deja de ser un riesgo invisible: la
  tabla de paridad (ADR 0019) la mide por programa. Si Source se queda atrás, se
  ve como un porcentaje, no como un sorpresa en el mes 18.