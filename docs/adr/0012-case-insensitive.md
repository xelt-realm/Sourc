# ADR 0012 — Identificadores y palabras clave no distinguen mayúsculas

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

BASIC no distingue mayúsculas. Fortran tampoco. La mayoría de lenguajes
modernos, sí. El equipo tiene background QBasic.

## Decisión

1. Palabras clave: no distinguen mayúsculas. `DIM`, `dim`, `Dim` son lo mismo.
2. Identificadores: no distinguen mayúsculas. `miVar` y `MIVAR` son el mismo
   identificador.
3. Los identificadores se canonizan a minúsculas para comparar y buscar.
4. Si dos identificadores del mismo ámbito sólo difieren en case, error `E0004`
   con las dos declaraciones en el mensaje.
5. El case original se conserva y se usa al generar documentación y mensajes, de
   modo que `miVariable` se siga viendo como `miVariable` en un diagnóstico.
6. El estilo canónico de la biblioteca estándar y de los ejemplos es `camelCase`
   para funciones y `PascalCase` para tipos. El compilador no lo exige; el
   formateador (`sourc fmt`) lo propone.

## Consecuencias

- El código puerto de BASIC funciona sin editar los nombres.
- El coste es que un error de case pasa desapercibido: `contador` y `Contador`
  son la misma variable. El error `E0004` cubre la colisión, no el uso accidental
  de un case distinto, que en BASIC también era la misma variable.
- La opción contraria (distintivo de case, estilo `snake_case`) habría sido más
  unfamiliar para el equipo y para el perfil de usuario objetivo.