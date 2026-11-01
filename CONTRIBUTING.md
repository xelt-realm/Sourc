# Cómo trabajar en Source

## Requisitos

Python 3.11 o superior. Nada más.

El compilador no tiene dependencias externas a propósito: es vendorizable, y en
la Fase 8 tiene que compilarse a sí mismo sin nada instalado
(principio P7 de `ROADMAP.md`).

## Puesta en marcha

```
git clone ...
cd Sourc
make test        # suite completa
make hola        # la prueba de fuego
```

## Antes de abrir un pull request

```
make test        # 137 tests; debe estar todo en verde
make check       # los ejemplos typechequean
```

Si cambias la semántica del lenguaje de forma intencionada:

```
make corpus      # regenera los goldens
git diff tests/golden/     # revisa el diff con atención
```

El diff de goldens **es** la revisión de un cambio de semántica. Si un golden
cambia y no debería, el cambio no era intencionado.

## Convenciones

- Python: 4 espacios, anotaciones de tipo dondejes nítido, sin `Any`.
- Los identificadores de los tests están en inglés; los mensajes al usuario, en
  español.
- Sin emojis en el código.
- Comentarios que explican **por qué**, no **qué**. Si el código necesita un
  comentario para explicar lo que hace, el código está mal.
- Todo texto de cara al usuario es un literal en el módulo que lo emite, para
  que los tests de diagnóstico puedan comprobarlo.

## Reglas del proyecto

1. **Los diagnósticos son API.** Cambiar el texto de un error es un cambio de
   producto: actualiza el snapshot en `tests/test_diagnostics.py` y explica por
   qué en el commit.
2. **La gramática está congelada** en `1.0`. Un cambio de sintaxis requiere ADR
   (ADR 0001).
3. **El peso es un gate.** Si un cambio aumenta un artefacto por encima de
   `budgets.toml`, o rompe el presupuesto, no se merges sin un ADR que lo justifique.
4. **El determinismo es requisito del bootstrap** (ADR 0016). Nada de iteración
   dependiente del hash, nada de flotantes como claves.
5. **El IR es el contrato.** Si un backend necesita algo que el IR no expresa,
   se arregla el IR; no se añade una bandera por backend.
6. **Los ejemplos compilan.** `examples/` está en el CI.

## Dónde tocar según lo que quieres cambiar

| Quiero... | Tocar |
|---|---|
| Cambiar el lenguaje | `GRAMMAR.md`, `docs/grammar/sourc.ebnf`, un ADR, el lexer y el parser |
| Añadir una primitiva  | `lexer.py` (keywords), `types.py` (aridad), `sir.py` (bajada), `interp.py`, `GRAMMAR.md` |
| Añadir una regla de tipos | `types.py` + un test en `tests/test_diagnostics.py` |
| Cambiar el IR | `sir.py` + `ARCHITECTURE.md` + `tests/test_sir.py` |
| Añadir un test de semántica | `tests/corpus/` + `make corpus` |
| Cambiar la filosofía | `MANIFESTO.md` + ADR, y hay que argumentar en contra |

## Estado de la suite

| Fichero | Qué cubre |
|---|---|
| `test_lexer.py` | Tokens, números en tres bases, escapes, interpolación |
| `test_parser.py` | Estructura, precedencia, `IF` corto/bloque, `ELSE IF` |
| `test_diagnostics.py` | El texto exacto de cada error, con sugerencia y posición |
| `test_sir.py` | Determinismo, serialización canónica, optimizaciones |
| `test_interp.py` | Semántica: aritmética, control de flujo, textos, arrays, 2D |
| `test_corpus.py` | 14 programas completos contra su salida esperada |
| `test_docs.py` | Los bloques de código de la documentación compilan |

## Licencia

MIT. Al contribuir aceptas la misma licencia.