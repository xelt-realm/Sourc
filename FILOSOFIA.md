# La filosofía, comprimida

Este fichero es la respuesta a "¿en qué cree este proyecto?" en el mínimo número
de palabras que no pierde el sentido. La versión larga está en
[`MANIFESTO.md`](../MANIFESTO.md), y este es un resumen, no una fuente paralela:
cuando discrepan, **`MANIFESTO.md` manda**.

## Qué es, en cinco líneas

1. Un lenguaje compilado, con sintaxis BASIC moderna, que produce WebAssembly. Un
   `.src` son cuatro ficheros que se sirven desde cualquier sitio.
2. **El coste se paga una vez, en la compilación. El usuario lo paga cero veces,
   en cada visita.**
3. Seis promesas: predecible, pequeño, explícito, sin recolector obligatorio,
   auditable, portable.
4. Una lista **normativa** de "no": virtual DOM, frameworks, gestor de paquetes,
   FFI dinámico, metaprogramación en runtime, recolector obligatorio, IDE,
   motor 3D completo. Añadir uno requiere un ADR.
5. El peso es un compromiso con el usuario final. Si ningún ejemplo del repo usa
   una función del runtime, esa función se borra.

## Cómo se sostiene — esto es lo no obvio

Lo de arriba es lo que el lenguaje promete. Esto es lo que evita que las
promesas se queden en palabras, y es la parte que no se deduce del manifiesto.

| Regla | Por qué existe |
|---|---|
| **Puerta, no informe** | Un presupuesto que sólo informa no aprieta nada. Si no cabe, el build falla y **el artefacto no se escribe**. |
| **Ratchet, no tolerancia** | Un `--tolerate 3` no sabe cuáles son ni por qué. En dos meses nadie lo recuerda y parece ruido. Una lista con semilla y motivo sólo puede encogerse. |
| **Oráculo independiente** | Lo que valida puede calcular otra cosa. Cinco de los bugs más caros los encontró una *segunda implementación de la misma especificación*: el intérprete frente al módulo, el emisor WAT frente al ensamblador. |
| **Medir donde vive la feature** | Una puerta que mide la unidad equivocada pasa en verde mientras la cosa está rota. `call.native` es **una** operación que escondía veintitrés funciones, y cuatro no compilaban. |
| **Un diagnóstico que se cae no es un diagnóstico** | Código estable, posición, corrección, sugerencia. Y probarlo **ejecutándolo**, porque existir en el fichero no prueba que su camino funcione. |
| **El presupuesto manda sobre la comodidad** | Subir un techo tres veces en un día es dejar de notar regresiones. Lo que no cabía se fue a otro programa, con su propio techo. |
| **Una reserva caduca es ruido** | Lo reservado se comprueba en las dos direcciones: nada sin motivo, y nada que ya no exista. |

## Las tres frases que explican el resto

- **"Predecible" significa reproducible, no "sin errores".** Un
  compilador determinista es el que se puede depurar: mismo fuente, mismos
  bytes, y un fallo que aparece una vez aparece siempre.
- **"Portable" significa un directorio estático**, no "funciona en todas partes
  sin condiciones". `file://` no funciona, porque cargar WebAssembly desde el
  disco está bloqueado en todos los navegadores. Cualquier sitio estático sí.
- **"Auditable" significa que se puede leer entero.** El pegamento completo
  cabe en menos de 300 líneas y está a la vista. Un runtime que el usuario no
  puede leer no es auditable, por mucho que se publique su código.

## Lo que esta compresión esconde a propósito

No está aquí, y está bien que no esté:

- Los números de peso. Viven en `budgets.toml`, se miden, y cambian cada semana.
  Ponerlos aquí los convierte en el sitio que hay que actualizar cada vez que se
  toca una línea.
- El cronograma. Vive en `ROADMAP.md`, y las estimaciones se revisan.
- La lista de no-goals completa, con sus excepciones. Está en `MANIFESTO.md`,
  que es normativa.

Una compresión que se queda al día es más útil que una compresión completa que
se queda vieja.