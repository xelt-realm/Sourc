# ADR 0008 — VEC3 y MAT4 escalares por defecto; SIMD opt-in

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

El lenguaje expone `VEC3` y `MAT4` con la expectativa de SIMD. WASM tiene `v128`
pero requiere `--enable-simd` al compilar y no está en todos los navegadores. Si
los vectores se definen en el IR como registros de 128 bits, el compilador
necesita dos lowers (v128 y escalar), el IR se complica, y el código generado se
divide en dos familias que hay que mantener y testear.

## Decisión

1. `VEC2`, `VEC3`, `VEC4`, `MAT3` y `MAT4` son **structs de `f32` en memoria** en
   la versión 1.0. `VEC3` ocupa 3 palabras, `MAT4` ocupa 16.
2. El azúcar de operadores (`a + b`, `v * s`, `MUL(M, v)`) se expande a esas
   operaciones escalares en el compilador.
3. `--simd` activa un lower que usa `v128` para `MAT4 * VEC4` y operaciones de
   color. Es una optimización opcional, medida, nunca un requisito.
4. El target `wasm/fallback` no usa `v128` en absoluto.

## Consecuencias

- Un solo camino de codegen para los vectores: el del 90% de los casos.
- El layout es estable y compatible entre los tres targets.
- El coste es renunciar a SIMD automático. A cambio: el compilador sigue siendo
  pequeño, que es la condición para poder auto-hospedarse.
- `MAT4 * VEC4` escalar son 64 multiplicaciones. Si el profiling de una escena
  3D lo señala, `--simd` es el mecanismo de arreglo, y existe desde el día uno.