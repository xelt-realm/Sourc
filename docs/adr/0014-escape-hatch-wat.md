# ADR 0014 — Escape hatch WAT mínimo en runtime/native

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

Hay unas pocas operaciones donde el control exacto sobre los bytes importa más
que la elegancia: el cursor de bump allocation, la comparación rápida de
cadenas, el `memcpy`. Escribirlas en Source funciona, pero cuesta más bytes y
más ciclos.

La alternativa habitual es escribirlas en C y compilarlas con clang. Eso rompe
dos promesas: no hay toolchain en el proyecto, y el auto-hospedaje necesita que
el runtime se pueda compilar con el propio compilador.

## Decisión

1. Existe `runtime/native/*.wat`, limitado a un subconjunto mínimo de WAT.
2. El compilador incluye un ensamblador de ese subconjunto, de unas 300 líneas.
3. El subconjunto permite: tipos `i32`/`i64`/`f32`/`f64`, funciones sin
   parámetros o con escalares, `local`/`global`, `load`/`store`, `i32.const`,
   arithmetic, `call`, `return`. Ni bloques, ni tablas, ni GC, ni control
   estructurado.
4. Cualquier cosa fuera del subconjunto es un error de ensamblado claro.
5. Las reglas del presupuesto: un módulo `native` no puede superar 1 KB raw. Si lo
   supera, es señal de que la lógica debe estar en Source.

## Enmienda 1 (2026-10) — el escape hatch se queda sin contenido en la Fase 2

La justificación original de este ADR era que tres cosas concretas costaban más
bytes de los que iban a caber en Source: **el cursor de bump allocation, la
comparación rápida de cadenas y el `memcpy`**. Las tres están resueltas, y no
por un módulo nativo:

- El bump allocator lo genera el backend como `@__bump`, dentro del módulo.
- La comparación de cadenas la bajan `str.eq` y `str.compare`, que son
  instrucciones del módulo.
- El `memcpy` es **`memory.copy`**, que es una instrucción de *bulk memory*:
  cuatro bytes donde el bucle de `@__copy` eran cuarenta más la función interna.

`memory.copy` es post-MVP y ése era el motivo de la lista de "no control
estructurado". Se usa porque todos los navegadores y todas las versiones de
Node la tienen, y porque su alternativa pesa igual en un programa pequeño que
en uno grande. Emitirla 교체ó a `@__copy` y `06_textos.src` bajó de 1299 a 1249
bytes en disco.

**Consecuencia: `runtime/native/` no tiene contenido, y no se le va a dar.** Un
directorio con un `.wat` inventado para no verse vacío es peor que no tenerlo:
el techo de 1 KB por módulo dejaría de ser una regla y sería una decoración.

Lo que **no** cambia: la regla, el techo y el ensamblador. Existen para el
momento en que una feature no quepa en Source, que es lo que ADR 0015 llama
"si no cabe aquí, va en Source". Ese momento todavía no ha llegado; el
`make check-subset` es lo que lo dirá, y es lo que decidirá si hace falta.

## Enmienda 2 (2026-10) — el subconjunto crece, y por qué

El subconjunto de este ADR excluye bloques y control estructurado. Eso se
escribió cuando el ensamblador sólo servía para el runtime. Ahora el
ensamblador sirve también para el **round trip** --el emisor de `wat.py` y él
son dos implementaciones del mismo formato, y que reensamblen los 17 programas
del corpus byte a byte es el oráculo estructural del que no hay herramienta
externa--, y el round trip necesita bloques: todos los programas del corpus
tienen `IF` y `WHILE`.

La ampliación no relaja la regla, la separa en dos:

- **Lo que puede emitir el ensamblador**: todo el formato.
- **Lo que puede contener un módulo de `runtime/native/`** (`rizado=True` en
  `wat_asm.ensamblar`): la lista original, sin control estructurado.

Y el motivo de mantener la línea no es el tamaño --que lo acota el techo-- sino
que corregir un bloque exige mantener el estado de la pila en la cabeza, y el
módulo nativo es el sitio donde eso no compensa.

## Enmienda 3 (2026-10) — un opcode que el verificador no conoce

`memory.copy` es post-MVP, y el verificador de pila **ignoraba** las
instrucciones prefijadas con 0xFC. Para una instrucción normal eso sólo
significa "sin comprobar"; para una prefijada es peor: sus tres operandos se
quedan en la pila y todo el resto del cuerpo se comprueba contra un estado que
ya no es real. El verificador daba verde **por razones equivocadas**.

Se añadió `TABLA_FC` con las ocho conversiones saturadas y `memory.copy` /
`memory.fill`, con tests que demuestran que la pila se consume. La regla que sale
de aquí: **una instrucción que el backend emite tiene que estar en el
verificador**, porque el verificador es lo único entre el backend y el error de
Node, y un gate que no mira no está mirando.

## Consecuencias

- Sin clang, sin Rust, sin node_modules en el runtime.
- El compilador puede ensamblar su propio runtime cuando se auto-hospeda, que
  es un requisito para el bootstrap.
- El coste es un componente extra que hay que mantener. Se acota con el límite de
  1 KB y con la regla de que "si no cabe aquí, va en Source".