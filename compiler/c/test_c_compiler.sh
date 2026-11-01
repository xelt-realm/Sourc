#!/bin/sh
set -eu

mock_bin=$(mktemp -d)
trap 'rm -rf "$mock_bin"; rm -f dist/main.c dist/index.html dist/glue.js dist/app.wasm dist/assets/player.svg dist/assets/bounce.wav; rmdir dist/assets 2>/dev/null || true' EXIT HUP INT TERM

cat >"$mock_bin/clang" <<'MOCK'
#!/bin/sh
while [ "$#" -gt 0 ]; do
    if [ "$1" = "-o" ]; then
        shift
        : >"$1"
        exit 0
    fi
    shift
done
exit 2
MOCK
chmod +x "$mock_bin/clang"

PATH="$mock_bin:$PATH" ./sourc build examples/landing.src --target=web
test -f dist/app.wasm
grep -qF 'js_draw_rect_filled(' dist/main.c
grep -qF 'js_draw_rect_outline(' dist/main.c
grep -qF 'js_draw_line(' dist/main.c
grep -qF 'js_draw_gradient_rect(' dist/main.c
grep -qF 'js_ui_text(' dist/main.c
grep -qF 'js_ui_panel(' dist/main.c
grep -qF 'js_ui_code(' dist/main.c
grep -qF 'js_ui_button(' dist/main.c
grep -qF 'js_copy_to_clipboard(' dist/main.c
grep -qF '"Times New Roman"' dist/main.c
grep -qF 'pages_build_output_dir' wrangler.toml
grep -qF 'ctx.strokeRect(x, y, w, h)' dist/glue.js
grep -qF 'ctx.lineTo(x2, y2)' dist/glue.js
grep -qF 'ctx.createLinearGradient(x, y, x, y + h)' dist/glue.js
grep -qF 'new ResizeObserver(resizeOverlay)' dist/glue.js
grep -qF "node.dataset.sourcClicked = 'true'" dist/glue.js
grep -qF "document.createElement('code')" dist/glue.js
grep -qF 'navigator.clipboard.writeText(text)' dist/glue.js
grep -qF 'ctx.fillText(text, x, y)' dist/glue.js
cc -std=c11 -Wall -Wextra -Wno-attributes -fsyntax-only dist/main.c
if command -v node >/dev/null 2>&1; then
    node --check dist/glue.js
fi

PATH="$mock_bin:$PATH" ./sourc build examples/pong.src --target=web
test -f dist/app.wasm
test -f dist/assets/bounce.wav
grep -qF 'void update(void)' dist/main.c
grep -qF 'requestAnimationFrame(frame)' dist/glue.js
cc -std=c11 -Wall -Wextra -Wno-attributes -fsyntax-only dist/main.c
