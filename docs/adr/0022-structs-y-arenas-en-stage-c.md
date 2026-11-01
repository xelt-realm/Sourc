# ADR 0022 — Structs y arenas en el compilador C

**Estado:** aceptada
**Fecha:** 2026-11

## Contexto

El frontend C acepta bloques `arena`, pero sólo los baja como scopes; además, no
hay tipos compuestos ni una operación `new`. Los juegos necesitan agrupar el
estado relacionado sin introducir un recolector ni asignaciones por frame.

## Decisión

1. `type Nombre ... end` declara un struct por valor. Sus campos son tipos
   primitivos o tipos definidos antes; no se permiten campos `void` ni arrays.
2. `p.campo` lee o asigna campos. Los structs fuera de `arena` son valores
   estáticos o locales normales.
3. Una variable struct declarada dentro de `arena ... end` se asigna
   implícitamente desde un bump allocator cuyo buffer dimensiona el compilador.
   Al salir, el puntero vuelve al checkpoint. Los valores no pueden escapar.
4. QBE representa structs como memoria con layout de campos y slots `allocN`;
   `arena` no necesita allocator dinámico en ese backend.

## Consecuencias

- Los structs no requieren una nueva operación de heap ni GC.
- La arena reserva sólo el máximo estático calculado para ese scope.
- Los dos backends usan el orden declarado y el layout C de alineación natural.
