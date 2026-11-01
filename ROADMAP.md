# ROADMAP — Source / Sourc

Plan de desarrollo por fases. Cada fase tiene objetivos, entregables medibles,
criterios de éxito y riesgos propios. Este documento es normativo: cualquier
cambio requiere un ADR.

- Versión del lenguaje: `1.0` (ver `docs/adr/0001`)
- Estado actual: Fase 0 completada, Fase 1 completa, Fase 2 casi completa,
  Fase 8 arrancada (el lexador)
- Esfuerzo total estimado: ~117 person-semanas
- Hito "hello world end-to-end": mes 4
- Hito "self-hosted": mes 18-19 (3 personas)

## Principios transversales

No negociables. Se aplican desde el primer commit.

| # | Principio | Consecuencia práctica |
|---|---|---|
| P1 | Determinismo | IR canónico, orden de iteración estable, sin hash aleatorio, floats serializados por bits |
| P2 | Presupuesto de peso en CI | `budgets.toml` + `sourc size`, el build falla si se supera |
| P3 | Testing diferencial | Intérprete (oráculo) vs WASM vs JS sobre un único corpus |
| P4 | Runtime escrito en Source | Nada de C ni de lenguajes externos en el runtime; `runtime/native/` está retirado y vacío (ADR 0014) |
| P5 | Errores como producto | Diagnósticos con span, caret y sugerencia. Nunca "internal error" |
| P6 | Documentos como tests | Todo bloque ```src del repo se typechequea en CI |
| P7 | Cero dependencias del compilador | Stage 0 solo stdlib de Python |
| P8 | Contrato de subconjunto | Cada fase declara un *subset level*; runtime y compilador sólo usan features de su nivel |

## Métricas transversales

| Categoría | Cómo se mide | Gate |
|---|---|---|
| Peso transferido | brotli de index.html + glue + wasm/js + css + assets | `budgets.toml`, falla CI |
| Tamaño raw | bytes por artefacto | umbral informativo |
| Arranque | E2E por CDP en un Chromium, 4x CPU throttle | por fase |
| Frame | rAF y contador de reservas; `bench/` aún no existe: es de las fases 3 y 7 | por fase |
| Alocaciones | contador en build dev (`SOURC_DEV=1`) | 0 en game loop |
| Build | frío y con caché | por fase |
| Paridad | corpus contra oráculo | 100% |

## Fases

| Fase | Nombre | Esfuerzo (p-s) | Calendario | Predecesora |
|---|---|---|---|---|
| 0 | Documentos y decisiones congeladas | 3 | 2 semanas | — |
| 1 | Frontend en Python (lexer, parser, tipos, IR, intérprete) | 14 | 7 semanas | 0 |
| 2 | Backend WASM + pipeline + glue mínimo | 12 | 6 semanas | 1 |
| 3 | Gráficos 2D + game loop + laberinto | 12 | 6 semanas | 2 |
| 4 | Tipos avanzados + arenas + funciones de primera clase | 14 | 7 semanas | 2 |
| 5 | Modo PAGE + generación de HTML/CSS | 10 | 5 semanas | 2, 4 |
| 6 | Backend JS + `--target=auto` | 10 | 6 semanas | 4 |
| 7 | 3D + assets glTF | 22 | 11 semanas | 4, 3 |
| 8 | Self-hosting | 21 | 11 semanas | 4 |

Detalle de cada fase: `docs/ROADMAP-FASES.md`.

## Cronograma (3 personas)

```
Mes  1  2  3  4  5  6  7  8  9  10 11 12 13 14 15 16 17 18 19
F0   ## ##
F1      #######
F2          ######      -> hello world end-to-end (mes 4)
F3              ######  -> laberinto 2D (mes 9)
F4               #######-> arenas, closures, optimizaciones
F5                    ###-> chat PAGE (mes 11)
F6                       ### -> --target=auto (mes 13)
F7                          ###### -> 3D (mes 16)
F8                             ##### -> self-hosting (mes 18-19)
```

## Reparto sugerido

| Persona | Responsabilidad |
|---|---|
| P1 — Compilador | Frontend, backend WASM, optimizaciones, lenguaje de PAGE, `auto`, lenguaje 3D, Stage 1a/1b |
| P2 — Runtime y web | `source_core`, `source_2d` + glue, PAGE/APP + CSS, glue JS, `source_3d`, Stage 1c |
| P3 — Calidad y producto | CI, corpus, fuzzing, benchmarks, presupuestos, ejemplos, docs-as-tests, E2E visual, assets, LSP |

## Definition of Done

Aplica a todas las fases:

1. Criterios de éxito cumplidos y medidos, no estimados.
2. `budgets.toml` actualizado y en verde.
3. Corpus completo verde en todos los backends existentes.
4. `bench/results.md` actualizado frente a la fase anterior. **`bench/` no
   existe**: depende de las fases 3 y 7, así que esta línea del Definition of
   Done no se puede cumplir hoy y no es un olvido, es una dependencia.
5. ADRs escritos para toda decisión no obvia.
6. Bloques de código de la documentación compilando.
7. Cero TODOs sin issue asignado.
8. Un capítulo de `docs/TUTORIAL.md` que funciona de principio a fin.
   **Tampoco existe todavía.** La página (`site/pagina.src`) cubre qué es el
   lenguaje, cómo se instala y un ejemplo que se ejecuta, pero un tutorial de
   principio a fin es un documento largo y no se escribe a medias: o teaches
   todo el lenguaje o no enseñas, y medio tutorial es peor que ninguno.

## Decisiones tomadas en la revisión de diseño

| Decisión | Elección |
|---|---|
| Lenguaje del runtime | Source desde el inicio |
| Backend gráfico 2D inicial | WebGL2 |
| Exigencia del bootstrap | Equivalencia funcional (no byte-idéntico) |
| Forma de distribuir | Un fichero, sin gestor de paquetes. Ver `DISTRIBUCION.md` |
| Primer entregable | ROADMAP + documentos de Fase 0 |

Ver `docs/adr/` para el detalle y `docs/adr/0016` para el bootstrap.