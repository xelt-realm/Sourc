# ADR 0009 — Sin recolector de basura por defecto

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

El manifiesto promete cero asignaciones en el game loop. Un recolector de basura
contradice eso de forma directa: una recolección de la generación joven puede
pausar el frame en cualquier momento, y el coste es invisible en el código
fuente.

El proyecto también contempla `WasmGC`, que no está en todos los navegadores.

## Decisión

1. El camino por defecto no tiene recolector de basura. La memoria se organiza
   en arenas con ámbito y bump allocation.
2. Un valor no puede sobrevivir a su arena: referencia a un valor de una arena
   cerrada desde fuera es error de compilación `E0031`.
3. `RESET ARENA` marca un checkpoint y libera en O(1). Es la herramienta para
   memoria estable dentro de un bucle.
4. `--gc=mark-sweep` ofrece un recolector opcional implementado en Source, para
   programas que no pueden usar arenas.
5. `--gc=wasm-gc` usa WasmGC cuando está disponible. Es un target
   (`wasm/full`), no un requisito.

## Consecuencias

- El tiempo de frame es predecible, que es lo que promete el manifiesto.
- La restricción de escapes es la parte incómoda: obliga a un estilo de
  programación que no es libre. A cambio, el error aparece en compilación.
- Con WasmGC disponible, el target `full` puede huelir un poco mejor en
  programas con mucha estructura de datos. Los dos targets deben producir el
  mismo comportamiento observable, y ambos se testean.
- El perfil es más cercano al de C que al de Python. Para quien viene de
  QBasic, la diferencia práctica es que "el programa se ralentiza" pasa a ser
  "el programa seallocate cada frame", y el compilador lo puede avisar.