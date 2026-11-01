# ADR 0006 — IF: forma corta y forma de bloque

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

`IF cond THEN stmt` y

```source
IF cond THEN
    stmt
ELSE
    ...
END IF
```

comparten prefijo. El parser tiene que decidir cuándo el cuerpo es una sola
sentencia y cuándo es un bloque, sin inventar un token introductorio que
BASIC no tiene.

## Decisión

1. Después de `THEN`, el cuerpo es un bloque si y sólo si la siguiente cosa es
   un salto de línea o un comentario.
2. En la forma de una línea, el cuerpo termina en el fin de línea o en `;`.
3. `ELSE` y `END IF` sólo son válidos en la forma de bloque. Aparecer después de
   un cuerpo de una línea es error `E0040` con el mensaje exacto que falta.
4. No hay `THEN` opcional, ni `IF ... THEN ... END IF` de una línea.

El caso 1 es implementable en el lexer con una sola bandera: `saw_newline_after_then`.

## Consecuencias

- Cero ambigüedad, cero token nuevo, cero coste de runtime.
- El error más probable de quien viene de BASIC (escribir un `ELSE` con cuerpo de
  una línea) produce un diagnóstico que dice exactamente qué añadir.
- `IF cond THEN stmt ELSE stmt` en una línea no existe. Escribir el bloque.