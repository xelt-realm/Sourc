# ADR 0002 — No hay variables implícitas (salvo la variable de FOR)

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

BASIC clásico permite `x = 5` sin declarar `x`. Es cómodo y es una fuente
clásica de errores: un nombre mal escrito se convierte en una variable global
nueva y silenciosa. Además estorba al objetivo del proyecto: el compilador
resuelve tipos en build time, y un identificador no declarado no tiene tipo.

La alternativa habitual (Rust, TypeScript estricto) es exigir declaración.

## Decisión

1. `DIM` es obligatorio para toda variable. `y = 5` sin `DIM y` es error `E0022`
   con la sugerencia `¿quiso decir DIM y AS INTEGER = 5?`.
2. Excepción única: la variable de `FOR` se declara sola, con ámbito de bucle.
   `FOR i = 0 TO 10` no requiere `DIM i` y `i` no existe fuera del bucle.
3. `DIM` sin tipo ni valor es error `E0010`. No hay valores cero implícitos que
   confundan un `INTEGER` con un `SINGLE`.
4. `INPUT` exige tipo explícito: `DIM nombre AS STRING` antes de leer.

## Consecuencias

- El ejemplo canónico del proyecto se escribe con `DIM px AS SINGLE = 32` y
  `DIM py AS SINGLE = 32` explícitos.
- El mensaje de error es la mitad del trabajo: sin sugerencia, esta decisión
  frustraría a quien viene de QBasic.
- Las variables de `FOR` no se filtran, lo que elimina la clase de bug más
  frecuente al portar código BASIC.