# ADR 0003 — Representación de textos y semántica de LEN

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

Un texto necesita una representación única que ambos backends puedan honorar
sin inventar un objeto. Los candidatos: `STRING` de alto nivel (con longitud
implícita, posiblemente con buffer real detrás) o `(ptr, len)` sobre memoria
lineal.

## Decisión

1. `STRING` es una secuencia de bytes UTF-8 en memoria lineal, representado en el
   IR como un par `(ptr, len)`. Nunca hay un objeto `Str` en el IR.
2. `LEN(s)` devuelve el **número de bytes**.
3. `LENU(s)` devuelve el número de codepoints UTF-8.
4. `LEN` es polimórfico: sobre `ARRAY OF T` devuelve el número de elementos.
5. `s[i]` devuelve un `STRING` de un carácter (índice en bytes).
6. `CHARCODE(s, i)` devuelve el byte como `INTEGER` sin asignar nada.

## Consecuencias

- El IR sigue siendo trivialmente portable a WASM y a JS, que es lo que permite
  un único frontend para dos backends.
- `LEN` sobre bytes y `LEN(mapa)` sobre elementos son la misma palabra con dos
  significados. Se acepta porque BASIC siempre lo hizo así y porque el segundo
  uso es naturalmente `FOR i = 0 TO LEN(mapa) - 1`.
- `LENU` existe para que programs que muestran texto al usuario no corten
  caracteres multi-byte.
- Ver ADR 0004 para el caso especial que hace que el acceso por índice sea
  gratuito.