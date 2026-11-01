# ADR 0024 — UI DOM superpuesto al Canvas

**Estado:** aceptada
**Fecha:** 2026-11

## Contexto

La UI dibujada exclusivamente en Canvas no permite seleccionar texto, copiar
código ni usar controles HTML accesibles. Se necesita una interfaz de documento
sin convertir el paquete web en una aplicación con framework.

## Decisión

1. El target Web expone `ui_text`, `ui_panel`, `ui_code`, `ui_button` y
   `copy_to_clipboard` como primitivas del host DOM.
2. Los elementos se posicionan en coordenadas lógicas del Canvas dentro de un
   overlay DOM escalado junto al Canvas. El texto y el código son seleccionables;
   los bloques de código usan `pre/code`.
3. `ui_button` identifica controles por ID estable. El listener DOM guarda el
   clic y la importación devuelve y consume ese estado en la siguiente llamada
   de Sourc.
4. Estas primitivas y el gradiente de Canvas son Web-only; QBE las rechaza.

## Consecuencias

- Los controles HTML conservan hover, foco y selección nativos sin framework.
- El navegador controla el acceso al portapapeles y puede rechazarlo según el
  contexto de seguridad o la activación del usuario.
