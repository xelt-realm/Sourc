# ADR 0001 — Versión del lenguaje y congelación de gramática

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

El proyecto empieza con una especificación en prosa. La gramática cambia más
caro cuando ya hay código de usuario que depende de ella: cada decisión tardía
equivale a un aviso de deprecación, un flag de versión y una migración.

## Decisión

La gramática se congela en la versión `1.0` al final de la Fase 0.

- Todo cambio incompatible crea una versión nueva (`1.1`, `2.0`).
- Un fuente puede declarar su versión: `--lang=1.0`. Sin declaración, el
  compilador asume la última soportada y avisa.
- Los cambios compatibles (nuevas primitivas, nuevos nombres de campo) no
  requieren bump.

## Consecuencias

- La especificación en prosa se convierte en `GRAMMAR.md` + `docs/grammar/sourc.ebnf`.
- Toda decisión dudosa pasa por un ADR antes de codificarse.
- Los bloques de código de la documentación se typechequean en CI, así que la
  gramática no puede romperse sin que alguien lo note en el build.