# MANIFESTO

> Source (técnicamente **Sourc**, extensión `.src`) es un lenguaje de programación
> compilado, con sintaxis BASIC moderna, que compila a WebAssembly por defecto y
> opcionalmente a JavaScript.

## La idea en una frase

**El desarrollador paga el coste una vez, en la compilación. El usuario lo paga cero
veces, en cada visita.**

Todo el trabajo pesado —resolución de tipos, inlining, eliminación de código
muerto, optimización de assets, selección de backend— ocurre en build time. El
usuario final recibe un binario pequeño y predecible: sin frameworks, sin virtual
DOM, sin hydration, sin `node_modules`.

## Para quién

Para quien viene de QBasic o BASIC moderno y quiere algo que funcione en el
navegador hoy, sin aprender un ecosistema de cinco herramientas ni montar un
`node_modules` de 69 KB antes de escribir la primera línea.

Un programa de "hola mundo" en Source transfiere menos de un tercio de lo que
transfiere React con TypeScript antes de escribir nada.

## Qué prometemos

1. **Predecible.** El mismo código produce el mismo binario. El mismo binario
   produce el mismo comportamiento en el mismo navegador.
2. **Pequeño.** Hay un presupuesto de peso por artefacto, verificado en CI. Si un
   cambio lo rompe, el build falla. No es un informe: es un gate.
3. **Explícito.** Tipos resueltos en compilación. Si un tipo no se puede resolver,
   es un error de compilación con sugerencia, nunca una surpresa en runtime.
4. **Sin GC obligatorio.** Las arenas tienen scope. La regla de oro: cero
   asignaciones en el game loop, y se verifica con un contador, no con una
   promesa.
5. **Auditable.** El glue JavaScript completo cabe en 300 líneas. El runtime está
   escrito en el mismo lenguaje que el usuario.
6. **Portable.** Un `.src` es un proyecto. El resultado es un directorio estático
   que se sirve desde cualquier sitio.

## Qué NO hacemos

Esta lista es normativa. Cada elemento es un "no" deliberado, y añadir algo aquí
requiere un ADR.

- No virtual DOM, ni reconciliación, ni hydration.
- No frameworks de terceros en el output. Ni React, ni Vue, ni Svelte.
- No gestor de paquetes. No hay `node_modules` en el proyecto de un usuario.
- No FFI dinámico en runtime. Los límites con el host son explícitos y versionados.
- No metaprogramación en runtime. Las macros, si algún día existen, ocurren en
  compilación.
- No recolector de basura como requisito. `WasmGC` es un target opcional.
- No `GOTO` sin warning. Existe por compatibilidad, marca el código como legacy.
- No editor ni IDE. Hay LSP, coloración y un formateador. Nada más.
- No motor de juego 3D completo. La Fase 7 entrega escena estática, cámara, una
  luz y sombras. PBR, SSR y post-processing quedan fuera.

## Dónde gana

| Contra | Ganamos en | Perdemos en |
|---|---|---|
| JavaScript / TypeScript | peso consistente, portabilidad standalone, cero configuración | ecosistema, biblioteca, tooling maduro |
| Rust | sin borrow checker, sintaxis legible, WASM más pequeño | seguridad de memoria, concurrencia y ecosistema |
| Python | es nativo (WASM), no interpretador, viable en el navegador | ciencia de datos, notebooks, velocidad de iteración |
| QBasic | moderno, portable, seguro, gráficos vectoriales | compatibilidad con BASIC clásico (GOTO/GO SUB legacy) |

## Métricas de peso como contrato

Transferido (brotli), incluyendo todo lo que el navegador descarga antes de que
el programa funcione:

| Entregable | Objetivo | Techo duro |
|---|---|---|
| Hola mundo | 12 KB | 20 KB |
| Laberinto 2D | 24 KB | 28 KB |
| Chat (modo PAGE) | 32 KB | 35 KB |
| Hola mundo con backend JS | 7 KB | 10 KB |
| Escena 3D de referencia | 800 KB | 1 MB |

El mecanismo que sostiene estos números es la **carga condicional de módulos**: el
glue_core se carga siempre; `glue_2d`, `glue_3d`, `glue_web`, `glue_sound` y
`glue_net` sólo si el programa referencia sus primitivas. Un programa que sólo
hace `PRINT` no descarga código de WebGPU.

## Budget de responsabilidad

El peso es un compromiso con el usuario final, y eso significa que cada función
del runtime tiene que justifiable con un programa real. La regla: si ningún
ejemplo del repo usa una función del runtime, esa función se borra.

## Licencia

MIT. El compilador, el runtime y la biblioteca estándar son MIT. Las
herramientas externas del pipeline de assets son opcionales, se cachean, y sólo
se aceptan licencias MIT o Apache-2.0.