# ADR 0020 — `AND` no cortocircuita, y es una decisión

**Estado:** aceptada
**Fecha:** 2026-10

## Contexto

Escribiendo el lexador en Sourc --la rebanada 1 del bootstrap-- apareció esto:

```
WHILE pos < largo AND EsSigueIdent(CHARCODE(src, pos))
```

Con `pos == largo` --el último carácter del fichero-- esto lee un byte que no
existe y el intérprete para con `índice N fuera de un texto de N bytes`.

La suposición de partida, razonable, es que se arregla con cortocircuito. Pero
esa suposición es una decisión de diseño, no un defecto: **no cortocircuita, y
no se va a cambiar.** Este ADR lo deja escrito, con el motivo, porque es el tipo
de cosa que se "arregla" sin querer dentro de seis meses y cuesta un rato
descubrir que el arreglo anterior era lo que sujetaba algo.

## La decisión

1. **Los operadores lógicos evaluan las dos partes.** `AND`, `OR` y `NOT` no
   cortocircuitan.
2. **La regla de escritura que sale:** *un `AND` que protege un índice tiene que
   protegerlo en la rama, no en la expresión.*

   ```
   ' mal: lee src[pos] aunque pos >= largo
   IF pos < largo AND CHARCODE(src, pos) = 46 THEN

   ' bien: la rama decide, y la llamada sólo se evalúa si puede
   IF ActualEs(src, pos, largo, 46) THEN
   ```

   Los predicados que indexan llevan el límite dentro, con su `RETURN` pronto:
   `ActualEs`, `SiguienteEs`, `SigueIdent`, `EsDigitoEn`. La razón de que
   sea un `RETURN` pronto y no un `AND` dentro de la FUNCTION es la misma: no
   hay cortocircuito, y dentro de una FUNCTION tampoco hay donde esconderlo.

## Por qué no cortocircuita

Tres razones, y las tres son del mismo tipo.

**Determinismo y medida.** Con cortocircuito, `f() AND g()` depende de `f()`. Sin
cortocircuito, la evaluación de una expresión es una función de sus subexpresiones
y nada más, y por eso el mismo fuente da el mismo resultado sin depender del
orden en que se miren las cosas. ADR 0016 pide determinismo para poder
auto-hospedarse; esto es la misma propiedad aplicada un nivel más abajo.

**Coste.** En WASM no hay cortocircuito en `i32.and`. Para emularlo hacen falta
dos ramas, una etiqueta y un salto por cada `AND` de cada expresión, en un
lenguaje cuyo presupuesto entero son 2880 bytes de pegamento. La versión sin
cortocircuito es una instrucción.

**Legibilidad del compilador.** El que escribe `x = f() AND g()` no tiene por
qué saber si `f()` devuelve un booleano. Con la regla de este ADR tampoco hace
falta: si `f()` devuelve un entero, `AND` devuelve un entero, y si la expresión
se usa como condición, el typechecker se encarga. Sin cortocircuito **no puede
haber una diferencia observable entre "devuelve booleano" y "devuelve entero"**,
que es justo lo que hace que `AND = 1` y `AND = TRUE` se comporten igual.

El coste de esta decisión es que hay una trampa, y la trampa es real: es la que
costó una hora de depuración. Se paga con la regla de escritura y con una
puerta, no escondiéndola.

## Consecuencias

- El compilador tiene que **documentarlo** junto a la gramática, no dejarlo en
  un ADR que nadie lee. El mensaje de `E0010` y la nota de la puerta que
  comprueba `compiler/lexador.src` lo dicen.
- Una puerta comprueba que no haya ningún `AND` junto a un índice en el lexador.
  Es un chequeo de texto y es incompleto a propósito: detecta lo que se escribe,
  que es lo que se escribe, y el comportamiento lo cubre
  `tests/test_lex_casos.py` con ficheros que no acaban en salto de línea.
- CualquierUNDS futura que escriba un compilador en Sourc se va a encontrar con
  esto. Que esté escrito aquí es la diferencia entre que lo lea y que lo
  descubra.

## Lo que no se decide aquí

No se decide si `AND` debería cortocircuitar. Se decide que **no lo hace**, y
que la consecuencia es una regla de escritura. Si algún día el presupuesto del
pegamento deja de ser el problema, esta decisión se revisa como una más, y el
ADR se reescribe; no se deja "para cuando haya tiempo", que es como se cuelan
las cosas.