# ADR 0007 — INTEGER es i64; su mapeo en el backend JS

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

`INTEGER` mapea a `i64` en WASM. En JavaScript no existe un entero de 64 bits
nativo; existen `BigInt` (correcto pero lento, ~10x más lento que `Number`) y
`Number` (rápido pero limitado a 2^53-1).

## Decisión

1. `INTEGER` es `i64` en WASM, sin discusión.
2. En el backend JS, `INTEGER` es `Number` con rango seguro
   [-2^53+1, 2^53-1]. El compilador emite una comprobación de desbordamiento en
   builds debug y la omite en release, documentando la garantía.
3. `--i64=strict` mapea a `BigInt`. Es correcto para todo, y lento. Existe para
   programas que necesitan todo el rango.
4. `SINGLE` y `DOUBLE` son `Number` en JS. Una operación de punto flotante en
   WASM y la misma en JS pueden diferir en el último bit. Los tests de paridad
   comparan flotantes por patrón de bits y, cuando difieren, se reporta como
   diferencia conocida y no como fallo.

## Consecuencias

- El backend JS cumple su objetivo (tamaño) sin arrastrar `BigInt` al código
  generado.
- El riesgo es el desbordamiento silencioso. Mitigación: el aviso de compilación
  menciona el límite en cuanto un literal entero lo supera, y el check de debug
  lo atrapa.
- La paridad de flotantes entre backends no es bit a bit en general. Está
  documentado como diferencia conocida, con un corpus de paridad que mide
  cuántas operaciones difieren.