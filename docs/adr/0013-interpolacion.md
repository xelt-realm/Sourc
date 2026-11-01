# ADR 0013 — Interpolación de cadenas en tiempo de compilación

**Estado:** aceptada
**fecha:** 2026-10

## Contexto

`"hola " & nombre & "!"` es la forma BASIC. Es explícita y no tiene coste de
runtime más allá de la concatenación. La interpolación (`"hola {nombre}"`) es
más legible y más moderna.

## Decisión

1. La interpolación existe: `"hola {nombre}"`.
2. Si la expresión es una **constante de compilación**, se resuelve en build
   time. `"x = " & 1 + 2` con literales se convierte en un texto constante sin
   ninguna operación en runtime.
3. Si la expresión no es constante, el compilador genera la concatenación
   explícita. El resultado es idéntico a escribirla a mano.
4. `{{` y `}}` son el escape para una llave literal.
5. La concatenación es `&`. `+` entre dos `STRING` es error `E0021`: exigir el
   operador explícito evita la ambigüedad con la aritmética.

## Consecuencias

- El caso constante se convierte en un único `data segment`, lo que sirve
  directamente a la mecánica de presupuesto de peso: los textos constantes de
  un programa se pueden medir y predecir.
- `PRINT "hola mundo"` no tiene ninguna concatenación: un `data segment` de 11
  bytes y una llamada a `print`. Ese es el objetivo de la Fase 2.