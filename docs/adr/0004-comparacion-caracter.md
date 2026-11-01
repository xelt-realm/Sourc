# ADR 0004 — Comparación de carácter sin asignación

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

El ejemplo canónico del proyecto recorre un mapa de cadenas comparando un
carácter:

```source
IF mapa[fila][col] = "#" THEN RECT col * 32, fila * 32, 32, 32, RGB(40, 40, 60)
```

Según ADR 0003, `s[i]` devuelve un `STRING` de un carácter. Una implementación
literal asigna un descriptor de dos palabras y posiblemente un byte de heap por
cada celda del mapa, en cada frame. Con un mapa de 15×16 y 60 fps son 14 400
asignaciones por segundo. Eso viola la regla de oro del proyecto: cero
asignaciones en el game loop.

## Decisión

El codegen reconoce el patrón `s[i] = "literal de un carácter"` y lo baja a una
comparación de bytes en la posición constante o variable, sin materializar nada.

Además, la regla se hace explícita en el lenguaje con `CHARCODE(s, i)`, que
devuelve el byte como `INTEGER` y no asigna jamás.

## Consecuencias

- `bench/2d.mjs` verifica que el contador de asignaciones del frame es 0 en
  `laberinto.src`. La verificación es mecánica, no una promesa.
- El patrón es un caso particular de una optimización general: **las
  optimizaciones que mueven trabajo a build time se miden**. El compilador
  reporta cuántos casos de este tipo encontró (`--stats`), porque una
  optimización que no es visible es una optimización que nadie mantiene.
- En código sensible al rendimiento, `CHARCODE` es la forma explícita de
  pedir el comportamiento barato.