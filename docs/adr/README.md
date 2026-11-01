---
title: "Indice de ADRs"
---

Cada decisión que no sea obvia tiene un registro. Son cortos a propósito:
contexto, decisión, consecuencias.

Para cambiar una decisión aceptada se escribe un ADR nuevo que la sustituya.
Los ADRs aceptados no se editan.

| # | Título | Estado | Efecto principal |
|---|---|---|---|
| [0001](0001-version-del-lenguaje.md) | Versión del lenguaje y congelación de gramática | aceptada | La gramática se congela en `1.0` |
| [0002](0002-variables-implicitas.md) | No hay variables implícitas | aceptada | `DIM` obligatorio; `FOR` sí declara su variable |
| [0003](0003-texto-y-len.md) | Representación de textos y `LEN` | aceptada | Texto = `(ptr, len)` sobre memoria lineal |
| [0004](0004-comparacion-caracter.md) | Comparación de carácter sin asignación | aceptada | Cero alocaciones al indexar un mapa |
| [0005](0005-color-y-rgb.md) | `COLOR` sentencia, `RGB` función | aceptada | Fin de la ambigüedad de `COLOR` |
| [0006](0006-if-corto-y-bloque.md) | `IF`: forma corta y de bloque | aceptada | Sin token nuevo, sin ambigüedad |
| [0007](0007-integer-i64.md) | `INTEGER` es i64 | aceptada | JS usa `Number` con rango seguro 2^53-1 |
| [0008](0008-simd-opt-in.md) | Vectores escalares, SIMD opt-in | aceptada | Un solo camino de codegen |
| [0009](0009-sin-gc.md) | Sin recolector de basura por defecto | aceptada | Arenas con ámbito; frame predecible |
| [0010](0010-modulos.md) | Módulos `USE` e `IMPORT FROM` | aceptada | La mecánica de pesos es posible |
| [0011](0011-no-hay-sub.md) | No existe `SUB` | aceptada | Un solo camino de llamada |
| [0012](0012-case-insensitive.md) | Case-insensitive | aceptada | El código BASIC existente funciona tal cual |
| [0013](0013-interpolacion.md) | Interpolación en build time | aceptada | Textos constantes en un solo segmento de datos |
| [0014](0014-escape-hatch-wat.md) | Escape hatch WAT mínimo | aceptada | Sin clang; el runtime es auto-hospedable |
| [0015](0015-runtime-en-source.md) | El runtime se escribe en Source | aceptada | Fin del doble mantenimiento |
| [0016](0016-bootstrap-equivalencia.md) | Bootstrap por equivalencia funcional | aceptada | Ahorra 3-4 semanas; el determinismo sigue duro || [0017](0017-punto-de-congelamiento.md) | Punto de congelamiento del lenguaje | aceptada | La gramática se congela al final de la Fase 3 |
| [0018](0018-lockstep.md) | Python y Source en lockstep | aceptada | El compilador temporal nunca se queda atrás solo |
| [0019](0019-ratchet-y-release.md) | Ratchet de completitud y releases | aceptada | "Casi funciona" no es un estado que se pueda medir |
| [0020](0020-and-no-cortocircuita.md) | `AND` no cortocircuita | aceptada | El índice se protege en la rama, no en la expresión |
| [0022](0022-structs-y-arenas-en-stage-c.md) | Structs y arenas en el compilador C | aceptada | Tipos compuestos y memoria con ámbito |
| [0023](0023-canvas-primitivas-y-fuente.md) | Primitivas de Canvas y fuente opcional | aceptada | Dibujo de landing pages en el target Web |
| [0024](0024-ui-dom-overlay.md) | UI DOM superpuesta al Canvas | aceptada | Controles y texto HTML nativos sobre Canvas |
