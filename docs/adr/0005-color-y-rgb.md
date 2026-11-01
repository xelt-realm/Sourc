# ADR 0005 — COLOR es sentencia, RGB es función

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

El proyecto usa `COLOR(40, 40, 60)` como si fuera una función que devuelve un
color. En BASIC, `COLOR` es una sentencia que selecciona un índice de paleta, y
`COLOR(40, 40, 60)` no es sintaxis válida.

Tener la misma palabra como sentencia y como función es una trampa de lectura y
un problema para el parser: `COLOR 5` y `COLOR(40,40,60)` divergen sólo en un
carácter.

## Decisión

1. `COLOR` es sólo sentencia: `COLOR 5` fija el índice de paleta actual.
2. `RGB(r, g, b)` es la función que construye un valor `COLOR`
   (`0xAARRGGBB`).
3. `COLOR(r, g, b)` se acepta como alias deprecado de `RGB` y emite warning
   `W0002` con la corrección en el mensaje.
4. `COLOR` sigue funcionando sin paréntesis en los ejemplos existentes, con
   warning, durante toda la versión 1.x.

## Consecuencias

- El parser no necesita contexto para distinguir dos construcciones.
- El código del manifiesto y los ejemplos se actualizan a `RGB(...)`.
- El alias mantiene el coste de migración casi nulo para quien ya escribió
  `COLOR(...)`, que es el objetivo.