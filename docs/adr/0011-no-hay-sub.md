# ADR 0011 — No existe SUB

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

BASIC tiene dos construcciones para subrutinas: `SUB ... END SUB` y
`FUNCTION ... END FUNCTION`. El proyecto ya usa `FUNCTION` con tipos de retorno,
así que la asimetría es heredada, no necesaria.

## Decisión

1. No hay `SUB`. Un procedimiento es una `FUNCTION` sin `AS`, que no devuelve
   nada.
2. `RETURN` sin expresión termina la función y devuelve el valor cero del tipo
   de retorno.
3. Llegar a `END FUNCTION` sin `RETURN` no es error: devuelve el valor cero.
4. `SUB` se acepta como error `E0052` con el mensaje exacto que escribir.

## Consecuencias

- Un solo camino de llamada, un solo tipo de función, una sola tabla de llamadas.
- El `RETURN` sin valor es un error `E0053` en una `FUNCTION` que declara tipo.
- Quien viene de QBasic recibe un mensaje que dice qué escribir, no un error seco.