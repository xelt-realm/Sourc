# Desplegar Sourc en Cloudflare Pages

Para el primer despliegue:

1. Instala Wrangler globalmente: `npm install -g wrangler`.
2. Autoriza tu cuenta de Cloudflare: `wrangler login`.
3. Desde la raíz del repositorio, ejecuta: `make deploy`.

El comando limpia `dist/`, compila `examples/landing.src`, verifica que se haya
generado `dist/index.html` y publica el paquete como `sourc`. Si falla
la compilación o falta el archivo de entrada, el despliegue se detiene.

Al terminar, Wrangler mostrará la URL pública del sitio:
<https://sourc.pages.dev>.
