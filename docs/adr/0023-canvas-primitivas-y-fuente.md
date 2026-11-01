# ADR 0023 — Primitivas de Canvas y fuente opcional

**Estado:** aceptada
**Fecha:** 2026-11

## Contexto

La API Canvas del compilador C sólo ofrece dibujo básico y `draw_text` no permite
seleccionar una familia tipográfica. La página de ejemplo necesita paneles,
contornos, líneas y texto con fuentes distintas.

## Decisión

1. Añadir `draw_rect_filled(x, y, w, h, color)`,
   `draw_rect_outline(x, y, w, h, color, grosor)` y
   `draw_line(x1, y1, x2, y2, color)` como primitivas de sentencia Web.
2. `draw_text(texto, x, y, tamaño, color[, fuente])` acepta una fuente opcional;
   si se omite, el runtime usa `sans-serif`.
3. Las primitivas gráficas siguen disponibles sólo en el target Web; el backend
   QBE las rechaza explícitamente.

## Consecuencias

- El glue de Canvas conserva el orden de dibujo y delega el rasterizado al
  Canvas 2D del navegador.
- La fuente es un nombre CSS suministrado en compilación/runtime, no un asset
  descargado por el compilador.
