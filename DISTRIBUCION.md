# Distribución

Qué se distribuye, por quién, con qué dependencias y en qué orden. Este
documento responde a una pregunta que el repositorio no tenía contestada: **por
qué hace falta Python**.

## La respuesta corta

**Para quien escribe Source, y sólo hasta la Fase 8. Para quien visita un
programa, nunca ha hecho falta ni lo hará.**

Es una consecuencia de la forma de un bootstrap, no una elección. Un compilador
no se puede escribir en un lenguaje que sólo ese compilador compila: la semilla
de la cadena de confianza tiene que estar en algo que ya existe. Python es eso,
y el principio P7 de [`ROADMAP.md`](ROADMAP.md) —stdlib y nada más— existe para
que esa semilla no arrastre dependencias.

| | Qué necesita | Cuándo |
|---|---|---|
| Quien **visita** un programa | nada | siempre |
| Quien **compila** un `.src` | Python 3.11+ | hasta la Fase 8 |

La promesa del manifiesto —"el usuario paga cero veces en cada visita"— se cumple
entera desde el principio. Lo que aún no se cumple es para el **desarrollador**.

## La escalera

Cuatro peldaños. Los dos primeros están hechos; el cuarto es el que quita
Python de las manos de quien programa.

### 1 · `sourc.pyz` — un fichero, sin clonar · **hecho**

```
python3 sourc.pyz run mi_programa.src
python3 sourc.pyz build mi_programa.src --release
```

Un `zipapp` de la biblioteca estándar: **173 KB**, veinte y cinco entradas, con
`budgets.toml`, `subset.toml` y `runtime/` dentro. Quita el `git clone`, que es
la fricción real. **No quita Python**, y no lo promete: el nombre del módulo que
lo construye (`tools/build_zipapp.py`) no dice lo que no hace.

Dentro de un zipapp `__file__` no existe en disco, así que subir desde él da una
ruta sin sentido. De eso se ocupa [`compiler/sourc/rutas.py`](../compiler/sourc/rutas.py),
y hay una puerta que compila y empaqueta en un directorio temporal vacío: si
algo dependiera del repo por una ruta construida a mano, ahí falla.

Es reproducible byte a byte, con fecha de modificación fija. La reproducibilidad
del empaquetado no es un detalle: es el mismo requisito duro del bootstrap
(ADR 0016) aplicado a la herramienta que hace el bootstrap.

### 2 · La página — el proyecto se sirve a sí mismo · **hecho**

[`site/pagina.src`](../site/pagina.src) es un programa Source que **calcula** el
documento. No hay plantillas ni sustituciones, y `tools/build_site.py` sólo lo
ejecuta, le pone el CSS dentro y comprueba que el presupuesto se cumple.

Sale **un solo fichero**. Se abre con doble clic, se manda por correo y se
sirve desde cualquier sitio sin configuración.

### 3 · Servicio de compilación — sin Python para el usuario · **pendiente**

Un `POST` con el fuente devuelve el `dist/`. Entonces el desarrollador **no toca
Python**: ni el visitante ni el programador, sólo quien opera el servicio.

Es defendible por una razón concreta que se verificó en el código:
**`sourc build` no ejecuta nada del programa del usuario**. El único
`subprocess` del compilador es el compresor `brotli` opcional, y además es
informativo. Es un servicio de compilación puro, no un servidor de ejecución
arbitraria, y esa diferencia es la que lo hace publicable.

Necesita un proceso vivo, así que **no** puede ir en un hosting estático: hace
falta un servidor propio o una función serverless.

### 4 · `sourc.wasm` — ni servidor, ni Python, ni instalación · **Fase 8**

El compilador en el navegador. Es lo único que quita la frontera, y depende de
la Fase 8, que depende de la Fase 4. Por eso el orden importa.

## Lo que no existe todavía, y conviene decir

Un barrido de todo el repositorio devuelve **cero** menciones de: publicar el
compilador, un canal de instalación, `npm`, `crates.io`, `pip`, binarios
precompilados, un gestor de versiones, notas de versión, o un `build.sh` —que
aparece como criterio futuro de la Fase 8, no como fichero.

Y cero menciones de: **playground**, REPL, sandbox, editor concreto, `VS Code`,
`gopls`, `language server`, resaltes de sintaxis, ni interoperabilidad con otros
lenguajes. El LSP está como entregable de la Fase 4 y el formateador sólo en un
ADR. `IMPORT FROM` es Source-a-Source y nada más.

## Qué canales tienen sentido, y cuáles no

| Canal | Veredicto |
|---|---|
| `git clone` + Python | **Hoy, y es lo único que hay.** El `README` lo dice en dos líneas. |
| `sourc.pyz` de un solo fichero | **Hecho.** Quita el clon. |
| Sitio estático | **Hecho.** `site/dist/index.html` es un fichero, sin build en el servidor. |
| `npm`, `crates.io`, `pip` | **No.** Un paquete para algo que no tiene dependencias y que se distribuye con `git clone` es una dependencia que se paga poremsa. Y `pip install` contradice P7. |
| Playground en el navegador | **Fase 8.** Hoy no hay compilador en WebAssembly, así que un playground tendría que llamar a un servidor --que es el peldaño 3, no el 4. |
| Extensión de editor, LSP, resaltes | **Fase 4** el LSP. Los resaltes son un `tmLanguage.json`, que es declarativo y no necesita build: es lo primero barato que se puede añadir sin tocar el lenguaje. |

## Una promesa que hay que corregir antes de publicarla

`MANIFESTO.md` dice que el resultado "se sirve desde cualquier sitio". Con
`file://` **no funciona**: la página carga el módulo con `fetch` y los
navegadores bloquean el esquema `file:`. No es un fallo de Sourc —cargar
WebAssembly desde el disco está bloqueado en todos los navegadores— pero la
frase se puede leer como "incluido abrirlo con doble clic", y eso **no es
cierto**.

Hay dos salidas y la segunda está a medio camino:

1. **Aclarar la promesa**: "cualquier sitio estático", y documentar que el
   desarrollo local es `python3 -m http.server`. Una línea.
2. **`--inline`**: que el módulo viaje dentro del HTML y entonces el doble clic
   funcione. Cuesta un 33% del peso del módulo —152 B se vuelven 204 en un
   hola mundo, que es nada; una escena de 2 MB se va a +700 KB— así que
   tendría que ser opt-in con un techo.

La segunda es la que convierte "mándame el HTML" en una propiedad de
distribución real, y hoy ningún lenguaje de WebAssembly la tiene.

## Lo que no se distribuye, y por qué

- **La suite, el corpus y los ejemplos** dentro del `.pyz`. Los tests comprueban
  el repo, no el empaquetado, y meterlos triplicaría el fichero para que no le
  sirviera a nadie.
- **Los datos de `budgets.toml` fuera del compilador.** Llevan, pero `subset.toml`
  no: es una puerta de desarrollo, no una feature del lenguaje, y `check --nivel`
  dice por qué si lo pides desde el `.pyz`.