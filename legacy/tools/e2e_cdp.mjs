// e2e_cdp.mjs — el E2E de verdad: un Chromium de verdad, hablando CDP.
//
// Qué es y qué no es. `tools/page_check.mjs` ejecuta el **mismo** script de la
// página con `document`, `location`, `window` y `fetch` de mentira. Pilla lo que
// un stub no pilla: que el import del pegamento resuelva, que el módulo se
// instancie en un motor real, que el CSS no tape el texto y que el orden de
// carga sea el de un navegador. No pilla nada de lo que un stub sepa simular, y
// por eso es un paso **distinto** y no uno mejor.
//
// ## Por qué CDP y no Playwright, ni Puppeteer, ni Selenium
//
// Porque son dependencias externas, y la regla del manifiesto es que el proyecto
// no las tenga. Con `WebSocket` y `fetch`, que son globales de Node desde la
// versión 22, este fichero no importa **nada**: se ejecuta con `node
// tools/e2e_cdp.mjs` y ya está. Nada que instalar, nada que actualizar, nada que
// se rompa al subir Node.
//
// ## El plan de cuatro mensajes
//
// 1. `Page.navigate` a la página del `dist/`, servida por el servidor de la línea
//    siguiente.
// 2. `Runtime.evaluate` de una expresión que devuelve
//    `window.sourcResultado`, que la página escribe cuando termina.
// 3. Se espera a que exista, con un tope de tiempo.
// 4. Se comparan `salida`, `display`, `frames` y `error` con lo que el módulo
//    debería haber dado.
//
// El paso 2 necesita `Runtime.evaluate` con `awaitPromise`, y por eso hace falta
// `Runtime.enable`. Sin él Chromium no evalúa.
//
// ## Si no hay Chromium
//
// Sale con código 3 y un mensaje que dice **cómo instalarlo**. No con código 0 y
// un "todo bien" en silencio: un E2E que no se ejecuta y parece que pasa es peor
// que no tenerlo, porque la puerta queda verde sin comprobar nada. El código 3
// es el que `tests/test_e2e.py` reconoce para saltarse, y está escrito aquí para
// que el número sea el mismo en los dos sitios.
//
//   node tools/e2e_cdp.mjs dist/hola hola.wasm
//   node tools/e2e_cdp.mjs --sin-navegador        # sólo comprueba el hallazgo
//   node tools/e2e_cdp.mjs --servidor dist/hola   # sólo prueba el servidor
//
// Los dos últimos modos existen para poder comprobar este fichero **sin** un
// Chromium. El hallazgo del navegador y el servidor de ficheros son la parte que
// se puede verificar en cualquier máquina, y son justo las dos cosas que un E2E
// sin navegador dejaría sin comprobar: el `--sin-navegador` dice dónde mira y el
// `--servidor` demuestra que el `dist/` sirve sus tres ficheros y no uno de más.

import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { readFileSync, existsSync, mkdtempSync } from "node:fs";
import { extname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { tmpdir } from "node:os";

const SIN_NAVEGADOR = 3;
const ERROR = 1;

const TIPOS = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".wasm": "application/wasm",
  ".json": "application/json",
};

// --------------------------------------------------------------- el navegador

// Dónde se mira, en orden. La variable de entorno va primera porque es como se
// señala un Chromium de una instalación que no está en un sitio estándar, y
// porque es lo que permite probar este fichero sin tener Chromium.
const CANDIDATOS = [
  process.env.CHROMIUM,
  process.env.CHROME,
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/usr/local/bin/chromium",
  "/snap/bin/chromium",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
];

export function hallar(candidatos = CANDIDATOS) {
  for (const ruta of candidatos) {
    if (ruta && existsSync(ruta)) {
      return ruta;
    }
  }
  return null;
}

// -------------------------------------------------------------- el servidor

export { servir };

// Un servidor de un fichero, en memoria. Sirve **sólo** el `dist/`, que es la
// claim que se quiere comprobar: si el `dist/` necesitara algo de fuera del
// `dist/`, aquí daría 404 y el E2E lo vería.
//
// El puerto lo elige el sistema (0) para que dos ejecuciones no choquen.
function servir(dist, puertoPedido = 0) {
  return new Promise((cumplir, fallar) => {
    const servidor = createServer((peticion, respuesta) => {
      const pedido = decodeURIComponent(peticion.url.split("?")[0]);
      const relativo = pedido === "/" ? "/index.html" : pedido;
      // La comprobación de `..` no es descuido: sin ella, servir el `dist/` es
      // también una forma de leer el disco desde el navegador.
      const destino = resolve(join(dist, relativo));
      if (!destino.startsWith(resolve(dist))) {
        respuesta.writeHead(403).end("fuera del dist");
        return;
      }
      let cuerpo;
      try {
        cuerpo = readFileSync(destino);
      } catch {
        respuesta.writeHead(404).end("no existe");
        return;
      }
      respuesta.writeHead(200, {
        "content-type": TIPOS[extname(destino)] || "application/octet-stream",
      }).end(cuerpo);
    });
    servidor.on("error", fallar);
    servidor.listen(puertoPedido, "127.0.0.1", () => {
      const { port } = servidor.address();
      cumplir({
        puerto: port,
        url: `http://127.0.0.1:${port}/index.html`,
        cerrar: () => new Promise((hecho) => servidor.close(hecho)),
      });
    });
  });
}

// --------------------------------------------------------------------- el CDP

class Sesion {
  constructor(ws) {
    this.ws = ws;
    this.siguiente = 1;
    this.pendientes = new Map();
    this.eventos = [];
    ws.addEventListener("message", (evento) => {
      const mensaje = JSON.parse(evento.data);
      if (mensaje.id && this.pendientes.has(mensaje.id)) {
        const { cumplir, fallar } = this.pendientes.get(mensaje.id);
        this.pendientes.delete(mensaje.id);
        if (mensaje.error) {
          fallar(new Error(`${mensaje.error.message} (CDP ${mensaje.error.code})`));
        } else {
          cumplir(mensaje.result);
        }
      } else if (mensaje.method) {
        this.eventos.push(mensaje);
      }
    });
  }

  enviar(method, params = {}) {
    const id = this.siguiente++;
    return new Promise((cumplir, fallar) => {
      this.pendientes.set(id, { cumplir, fallar });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pendientes.has(id)) {
          this.pendientes.delete(id);
          fallar(new Error(`${method} no contestó`));
        }
      }, 20000).unref?.();
    });
  }

  cerrar() {
    this.ws.close();
  }
}

function conectar(puerto) {
  return new Promise((cumplir, fallar) => {
    const ws = new WebSocket(`ws://127.0.0.1:${puerto}/devtools/browser/`);
    ws.addEventListener("error", () => fallar(new Error("no se pudo hablar CDP")));
    ws.addEventListener("open", () => cumplir(new Sesion(ws)));
  });
}

// Una pestaña nueva, y su sesión de destino. La pestaña se cierra al terminar:
// abrir una por test y dejarla abierta llena el perfil del usuario de Firefox.
async function pestaña(sesion) {
  const { targetId } = await sesion.enviar("Target.createTarget",
                                             { url: "about:blank" });
  const { sessionId } = await sesion.enviar("Target.attachToTarget",
                                             { targetId, flatten: true });
  return { targetId, sessionId };
}

function enviarConSession(sesion, sessionId, method, params = {}) {
  // `flatten: true` deja las sesiones en el mismo socket, con `sessionId` en el
  // mensaje. Es lo que evita una segunda conexión.
  const id = sesion.siguiente++;
  return new Promise((cumplir, fallar) => {
    sesion.pendientes.set(id, { cumplir, fallar });
    sesion.ws.send(JSON.stringify({ id, method, params, sessionId }));
    setTimeout(() => {
      if (sesion.pendientes.has(id)) {
        sesion.pendientes.delete(id);
        fallar(new Error(`${method} no contestó`));
      }
    }, 20000).unref?.();
  });
}

// ------------------------------------------------------------------ el E2E

const LEER = `JSON.stringify(window.sourcResultado || null)`;

async function esperarResultado(sesion, sessionId, topeMs = 15000) {
  // Se pregunta en bucle en vez de esperar un evento: la página escribe
  // `sourcResultado` sin avisar, y no hay evento al que suscribirse. Un evento
  // inventado sería otro sitio donde el E2E puede mentir.
  const waited = Date.now();
  while (Date.now() - waited < topeMs) {
    const respuesta = await enviarConSession(sesion, sessionId,
                                             "Runtime.evaluate",
                                             { expression: LEER,
                                               returnByValue: true });
    const valor = respuesta.result?.value;
    if (valor && valor !== "null") {
      return JSON.parse(valor);
    }
    await new Promise((hecho) => setTimeout(hecho, 100));
  }
  return null;
}

export async function correr({ dist, programa, frames = 1, timeoutMs = 30000 }) {
  const navegador = hallar();
  if (!navegador) {
    return { sinNavegador: true };
  }
  const perfil = mkdtempSync(join(tmpdir(), "sourc-cdp-"));
  const proceso = spawn(navegador, [
    "--headless",
    "--remote-debugging-port=0",
    `--user-data-dir=${perfil}`,
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });

  try {
    const puerto = await puertoDeDebug(proceso, timeoutMs);
    const sesion = await conectar(puerto);
    const { targetId, sessionId } = await pestaña(sesion);
    const servidor = await servir(dist);
    try {
      await enviarConSession(sesion, sessionId, "Page.enable");
      await enviarConSession(sesion, sessionId, "Runtime.enable");
      await enviarConSession(sesion, sessionId, "Page.navigate",
                             { url: `${servidor.url}?p=${programa}&frames=${frames}` });
      const resultado = await esperarResultado(sesion, sessionId);
      return { resultado, url: servidor.url };
    } finally {
      await servidor.cerrar();
      sesion.enviar("Target.closeTarget", { targetId }).catch(() => {});
      sesion.cerrar();
    }
  } finally {
    proceso.kill();
  }
}

// El puerto sale en la primera línea de stderr, y es 0 en la bandera.
//
// Se lee de stderr y **no** se elige un puerto fijo a propósito: dos E2E a la vez
// —que es lo que pasa en cuanto la puerta crece— no pueden pelearse por un
// puerto, y elegir uno fijo obliga a reintentar con números.
function puertoDeDebug(proceso, timeoutMs) {
  return new Promise((cumplir, fallar) => {
    let buffer = "";
    const reloj = setTimeout(
      () => fallar(new Error("Chromium no anunció su puerto de depuración")),
      timeoutMs);
    proceso.stderr.on("data", (trozo) => {
      buffer += trozo.toString();
      const encontrado = buffer.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/);
      if (encontrado) {
        clearTimeout(reloj);
        cumplir(Number(encontrado[1]));
      }
    });
    proceso.on("exit", (codigo) => {
      clearTimeout(reloj);
      fallar(new Error(`Chromium salió con código ${codigo} sin abrir puerto`));
    });
  });
}

// ------------------------------------------------------------------ el main

function bandera(nombre, porDefecto) {
  const indice = process.argv.indexOf(`--${nombre}`);
  return indice >= 0 && process.argv[indice + 1] ? process.argv[indice + 1]
                                                  : porDefecto;
}

// El `main` va en una función y se llama **sólo si este fichero es el punto de
// entrada**. Importado para probarlo, un script que ejecuta su `main` al ser
// importado no se puede probar: el test se encontraría launching Chromium.
async function principal() {
  // Los posicionales se leen **después** de quitar los flags. Sin este filtro,
  // `--servidor dist/hola` ponía `--servidor` donde va la ruta y todo daba 404:
  // un fallo de parsing que se lee como un fallo del servidor, que es peor.
  const posicionales = process.argv.slice(2).filter((argumento) => {
    if (!argumento.startsWith("--")) {
      return true;
    }
    // Un flag con valor se come el siguiente; uno sin valor, no.
    return !["--servidor", "--sin-navegador"].includes(argumento);
  });
  // `bandera()` lee el valor que sigue al flag, así que hay que ocultarlo del
  // filtro para que no cuente como posicional.
  const distArg = posicionales[0];
  const programaArg = posicionales[1];
  const programaDe = posicionales[1];

  if (process.argv.includes("--sin-navegador")) {
    const donde = hallar();
    console.log(donde ? `navegador: ${donde}` : "navegador: no está");
    return donde ? 0 : SIN_NAVEGADOR;
  }

  if (process.argv.includes("--servidor")) {
    // Autocomprobación del servidor contra un `dist/` real. Devuelve JSON para
    // que el test de Python lo lea y no tenga que adivinar por el texto.
    const raiz = resolve(distArg || "dist/hola");
    const servidor = await servir(raiz);
    const base = servidor.url.replace(/\/index\.html$/, "");
    const pedir = async (ruta) => {
      const r = await fetch(base + ruta);
      return {
        estado: r.status,
        bytes: (await r.arrayBuffer()).byteLength,
        tipo: r.headers.get("content-type"),
      };
    };
    const salida = {
      ausente: await pedir("/no-existe.wasm"),
      escapes: await pedir("/../../etc/passwd"),
      escapes2: await pedir("/..%2f..%2fetc/passwd"),
      glue: await pedir("/glue_core.js"),
      indice: await pedir("/index.html"),
      programa: await pedir("/" + (programaDe || "main.wasm")),
    };
    await servidor.cerrar();
    console.log(JSON.stringify(salida));
    return 0;
  }

  const dist = resolve(distArg || "dist/hola");
  const programa = programaArg || "main.wasm";

  if (!existsSync(join(dist, "index.html"))) {
    process.stderr.write(`no hay dist/ en ${dist}; compila con --release\n`);
    return ERROR;
  }

  try {
    const { sinNavegador, resultado } = await correr({
      dist,
      programa,
      frames: Number(bandera("frames", "1")),
    });
    if (sinNavegador) {
      process.stderr.write(
        "no hay Chromium: instálalo o pon CHROMIUM=/ruta/a/chromium\n");
      return SIN_NAVEGADOR;
    }
    console.log(JSON.stringify(resultado));
    return resultado && resultado.error === null ? 0 : ERROR;
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    return ERROR;
  }
}

// `import.meta.url` contra el argv es la forma estándar de saber si un módulo de
// ESM es el entry point. Sin esto, importar este fichero desde un test lanzaría
// un Chromium sin querer.
const invocadoDirecto = process.argv[1]
  && import.meta.url === pathToFileURL(process.argv[1]).href;

if (invocadoDirecto) {
  process.exit(await principal());
}
