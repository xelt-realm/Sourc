r"""Texto canónico: cómo se escribe una cadena dentro de una línea de salida.

Existe su propio módulo porque lo necesitan dos volcados --`sourc lex` y
`sourc parse`-- y porque los dos tienen que escaparlo **igual**. Si cada uno
tuviera el suyo, el día que uno añadiera un carácter al conjunto el otro
seguiría en verde: dos formatos que se parecen mucho y no coinciden son
peores que dos formatos que se ven distintos a ojo.

## Qué problema resuelve

Un golden es texto, y el texto lleva dentro datos que pueden llevar saltos de
línea, tabuladores y el propio separador de campos. Sin escapar, dos árboles
distintos pueden serializarse en la misma cadena, y **un golden que no puede
fallar no es un golden**: la puerta pasa sin comprobar nada.

## Por qué estos escapes

- `\\`, `\n`, `\r`, `\t`: los que un programa Source puede escribir, porque son
  los del propio lenguaje (ADR 0013). Un golden con un salto de línea dentro es
  un golden que ocupa dos líneas y corta la comparación por la mitad.
- `\p` para la barra vertical: **no es un escape del lenguaje**, y esa es la
  gracia. Los habituales los puede escribir cualquiera; uno raro casi nadie. Y
  es el separador de campos de los dos volcados.

## El orden importa

La barra invertida va **antes** que la barra vertical. Al revés, la barra
invertida del escape recién escrito se convertiría en `\\` y el resultado dejaría
de deshacerse de una sola manera. Es lo que hace que `visible` sea invertible, y
por eso el orden está escrito en el código y no se deduce.
"""

from __future__ import annotations


def visible(texto: str) -> str:
    """Un texto en una línea, y sin que se confunda con el separador."""
    return (texto.replace("\\", "\\\\")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t")
                .replace("|", "\\p"))

