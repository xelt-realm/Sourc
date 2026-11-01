# ADR 0010 — Módulos: USE e IMPORT FROM

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

El runtime es modular a propósito: `source_core`, `source_2d`, `source_3d`,
`source_web`, `source_sound`, `source_net`. La mecánica de pesos del proyecto
depende de que un programa que sólo hace `PRINT` no descargue código de WebGPU.

Eso exige que la dependencia sea explícita en el lenguaje, y que el compilador
pueda ver qué módulos usa un programa para elegir qué glue escribir.

## Decisión

```source
USE SOURCE_2D          ' módulo del runtime, por nombre
IMPORT FROM "matematico.src"   ' fuente del usuario, relativo a este archivo
IMPORT FROM "juego/nivel.src" AS NIVEL
```

1. `USE` nombra un módulo del runtime. El nombre es un identificador en
   mayúsculas: `SOURCE_2D`, `SOURCE_3D`, `SOURCE_WEB`, `SOURCE_SOUND`,
   `SOURCE_NET`, `SOURCE_CORE`.
2. `IMPORT FROM` trae código fuente del usuario. El path es relativo al archivo
   que importa, con `/` como separador.
3. `USE` es transitivo y no se repite: dos archivos que usan `SOURCE_2D` lo
   comparten.
4. La resolución de módulos ocurre en el compilador. Si `SOURCE_2D` se usa en
   alguna parte del programa, el compilador lo incluye **una vez** y registra su
   dependencia para la selección de glue y de runtime.

## Consecuencias

- El peso se decide en build time, con información completa, que es la
  filosofía del proyecto.
- `USE SOURCE_2D` habilita las primitivas 2D. Usar `RECT` sin el `USE` es error
  `E0060` que nombra el módulo que falta.
- Los nombres de los módulos del runtime son API pública. Añadir uno es un
  cambio de versión del runtime y un ADR.