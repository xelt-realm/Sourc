#define _POSIX_C_SOURCE 200809L
#include "codegen_c.h"
#include "compiler.h"

#include <errno.h>
#include <dirent.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

typedef enum {
    TARGET_IR,
    TARGET_NATIVE,
    TARGET_WEB
} Target;

static void usage(FILE *stream) {
    fprintf(stream,
            "usage: sourc build <source.src> [--emit-ir | --target=native | "
            "--target=web]\n");
}

static char *source_output_path(const char *source, const char *suffix) {
    size_t length = strlen(source);
    const char *slash = strrchr(source, '/');
    const char *dot = strrchr(source, '.');
    if (dot && (!slash || dot > slash)) {
        length = (size_t)(dot - source);
    }
    size_t suffix_length = strlen(suffix);
    if (length > SIZE_MAX - suffix_length - 1) {
        return NULL;
    }
    char *result = malloc(length + suffix_length + 1);
    if (!result) {
        return NULL;
    }
    memcpy(result, source, length);
    memcpy(result + length, suffix, suffix_length + 1);
    return result;
}

static int write_file(const char *path, const char *data, size_t length) {
    FILE *file = fopen(path, "wb");
    if (!file) {
        fprintf(stderr, "%s: %s\n", path, strerror(errno));
        return 0;
    }
    int ok = fwrite(data, 1, length, file) == length;
    if (fclose(file) != 0) {
        ok = 0;
    }
    if (!ok) {
        fprintf(stderr, "%s: failed writing output\n", path);
    }
    return ok;
}

static int append_file(const char *path, const char *data, size_t length) {
    FILE *file = fopen(path, "ab");
    if (!file) {
        fprintf(stderr, "%s: %s\n", path, strerror(errno));
        return 0;
    }
    int ok = fwrite(data, 1, length, file) == length;
    if (fclose(file) != 0) {
        ok = 0;
    }
    if (!ok) {
        fprintf(stderr, "%s: failed writing output\n", path);
    }
    return ok;
}

static int run_tool(char *const arguments[]) {
    pid_t child = fork();
    if (child < 0) {
        fprintf(stderr, "cannot start %s: %s\n", arguments[0],
                strerror(errno));
        return 0;
    }
    if (child == 0) {
        execvp(arguments[0], arguments);
        fprintf(stderr, "cannot execute %s: %s\n", arguments[0],
                strerror(errno));
        _exit(127);
    }
    int status;
    while (waitpid(child, &status, 0) < 0) {
        if (errno == EINTR) {
            continue;
        }
        fprintf(stderr, "cannot wait for %s: %s\n", arguments[0],
                strerror(errno));
        return 0;
    }
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

static int build_native(const char *ssa_path) {
    char *qbe_args[] = {"qbe", "-o", "out.s", (char *)ssa_path, NULL};
    if (!run_tool(qbe_args)) {
        fprintf(stderr, "Error: Asegúrate de tener qbe instalado en el PATH.\n");
        return 0;
    }
    char *cc_args[] = {"cc", "-o", "app", "out.s", NULL};
    if (!run_tool(cc_args)) {
        fprintf(stderr, "Error: No se pudo enlazar el ensamblado nativo con cc.\n");
        return 0;
    }
    return 1;
}

static char *source_assets_path(const char *source_path) {
    const char *slash = strrchr(source_path, '/');
    size_t directory_length = slash ? (size_t)(slash - source_path) : 1;
    const char *directory = slash ? source_path : ".";
    static const char suffix[] = "/assets";
    if (directory_length > SIZE_MAX - sizeof(suffix)) {
        return NULL;
    }
    char *path = malloc(directory_length + sizeof(suffix));
    if (!path) {
        return NULL;
    }
    memcpy(path, directory, directory_length);
    memcpy(path + directory_length, suffix, sizeof(suffix));
    return path;
}

static int copy_assets(const char *source_path) {
    if (mkdir("dist/assets", 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "dist/assets: %s\n", strerror(errno));
        return 0;
    }
    struct stat destination_status;
    if (stat("dist/assets", &destination_status) != 0 ||
        !S_ISDIR(destination_status.st_mode)) {
        fprintf(stderr, "dist/assets exists and is not a directory\n");
        return 0;
    }

    char *assets_path = source_assets_path(source_path);
    if (!assets_path) {
        fprintf(stderr, "out of memory while preparing assets path\n");
        return 0;
    }
    DIR *source_directory = opendir(assets_path);
    if (!source_directory) {
        int error = errno;
        free(assets_path);
        if (error == ENOENT) {
            return 1;
        }
        fprintf(stderr, "cannot open source assets directory: %s\n",
                strerror(error));
        return 0;
    }
    free(assets_path);

    int destination_fd = open("dist/assets", O_RDONLY);
    if (destination_fd < 0) {
        fprintf(stderr, "dist/assets: %s\n", strerror(errno));
        closedir(source_directory);
        return 0;
    }

    int source_fd = dirfd(source_directory);
    int ok = 1;
    for (;;) {
        errno = 0;
        struct dirent *entry = readdir(source_directory);
        if (!entry) {
            if (errno != 0) {
                fprintf(stderr, "cannot read source assets directory: %s\n",
                        strerror(errno));
                ok = 0;
            }
            break;
        }
        if (strcmp(entry->d_name, ".") == 0 ||
            strcmp(entry->d_name, "..") == 0) {
            continue;
        }
        struct stat source_status;
        if (fstatat(source_fd, entry->d_name, &source_status,
                    AT_SYMLINK_NOFOLLOW) != 0) {
            fprintf(stderr, "cannot inspect asset %s: %s\n", entry->d_name,
                    strerror(errno));
            ok = 0;
            break;
        }
        if (!S_ISREG(source_status.st_mode)) {
            continue;
        }
        int input_fd = openat(source_fd, entry->d_name,
                              O_RDONLY | O_NOFOLLOW);
        if (input_fd < 0) {
            fprintf(stderr, "cannot open asset %s: %s\n", entry->d_name,
                    strerror(errno));
            ok = 0;
            break;
        }
        int output_fd = openat(destination_fd, entry->d_name,
                               O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW,
                               0644);
        if (output_fd < 0) {
            fprintf(stderr, "cannot create dist/assets/%s: %s\n",
                    entry->d_name, strerror(errno));
            close(input_fd);
            ok = 0;
            break;
        }

        char buffer[8192];
        for (;;) {
            ssize_t bytes_read = read(input_fd, buffer, sizeof(buffer));
            if (bytes_read == 0) {
                break;
            }
            if (bytes_read < 0) {
                if (errno == EINTR) {
                    continue;
                }
                fprintf(stderr, "cannot read asset %s: %s\n", entry->d_name,
                        strerror(errno));
                ok = 0;
                break;
            }
            ssize_t written = 0;
            while (written < bytes_read) {
                ssize_t count = write(output_fd, buffer + written,
                                      (size_t)(bytes_read - written));
                if (count < 0 && errno == EINTR) {
                    continue;
                }
                if (count <= 0) {
                    fprintf(stderr, "cannot write dist/assets/%s: %s\n",
                            entry->d_name, strerror(errno));
                    ok = 0;
                    break;
                }
                written += count;
            }
            if (!ok) {
                break;
            }
        }
        if (close(input_fd) != 0) {
            fprintf(stderr, "cannot close asset %s: %s\n", entry->d_name,
                    strerror(errno));
            ok = 0;
        }
        if (close(output_fd) != 0) {
            fprintf(stderr, "cannot close dist/assets/%s: %s\n",
                    entry->d_name, strerror(errno));
            ok = 0;
        }
        if (!ok) {
            break;
        }
    }

    if (close(destination_fd) != 0) {
        fprintf(stderr, "cannot close dist/assets: %s\n", strerror(errno));
        ok = 0;
    }
    if (closedir(source_directory) != 0) {
        fprintf(stderr, "cannot close source assets directory: %s\n",
                strerror(errno));
        ok = 0;
    }
    return ok;
}

static int build_web(const Program *program, const char *source_path) {
    static const char index_html[] =
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>Sourc</title>\n"
        "<style>body{margin:0;background:#111;display:grid;place-items:center;"
        "min-height:100vh}canvas{background:#000;max-width:100%;height:auto}"
        "</style>\n"
        "<canvas id=\"canvas\" width=\"640\" height=\"480\"></canvas>\n"
        "<script type=\"module\" src=\"glue.js\"></script>\n"
        "</html>\n";
    static const char glue_js_prefix[] =
        "const canvas = document.getElementById('canvas');\n"
        "const stage = document.createElement('div');\n"
        "stage.className = 'sourc-stage';\n"
        "stage.style.cssText = 'position:relative;display:inline-block;"
        "max-width:100%;line-height:0';\n"
        "canvas.parentNode.insertBefore(stage, canvas);\n"
        "stage.appendChild(canvas);\n"
        "canvas.style.cssText = 'display:block;max-width:100%;height:auto';\n"
        "const ctx = canvas.getContext('2d');\n"
        "const overlay = document.createElement('div');\n"
        "overlay.className = 'sourc-overlay';\n"
        "overlay.style.cssText = 'position:absolute;left:0;top:0;"
        "transform-origin:top left;pointer-events:none;line-height:normal';\n"
        "stage.appendChild(overlay);\n"
        "const uiStyle = document.createElement('style');\n"
        "uiStyle.textContent = `\n"
        ".sourc-ui-text{position:absolute;pointer-events:auto;"
        "white-space:pre-wrap;user-select:text;line-height:1.35}\n"
        ".sourc-ui-panel{position:absolute;box-shadow:0 12px 32px "
        "rgba(0,0,0,.32);border:1px solid rgba(255,255,255,.12)}\n"
        ".sourc-ui-code{position:absolute;pointer-events:auto;"
        "box-sizing:border-box;overflow:auto;"
        "white-space:pre-wrap;user-select:text;cursor:text;color:#e8edf5;"
        "background:#10141c;border:1px solid #303846;border-radius:12px;"
        "padding:20px;font:15px/1.6 ui-monospace,SFMono-Regular,Consolas,"
        "monospace}\n"
        ".sourc-ui-button{position:absolute;box-sizing:border-box;"
        "pointer-events:auto;cursor:pointer;border:1px solid rgba(255,255,255,"
        ".18);border-radius:12px;color:#fff;background:linear-gradient("
        "135deg,#7657f6,#4e39b5);font:600 16px/1.2 system-ui,sans-serif;"
        "box-shadow:0 8px 24px rgba(0,0,0,.3);transition:transform .16s,"
        "box-shadow .16s,filter .16s}\n"
        ".sourc-ui-button:hover{transform:translateY(-2px);filter:brightness("
        "1.12);box-shadow:0 12px 28px rgba(0,0,0,.4)}\n"
        ".sourc-ui-button:active{transform:translateY(0)}\n"
        "`;\n"
        "document.head.appendChild(uiStyle);\n"
        "const uiNodes = new Map();\n"
        "const ensureUiNode = (key, tag, className) => {\n"
        "  let node = uiNodes.get(key);\n"
        "  if (!node) {\n"
        "    node = document.createElement(tag);\n"
        "    node.className = className;\n"
        "    overlay.appendChild(node);\n"
        "    uiNodes.set(key, node);\n"
        "  }\n"
        "  return node;\n"
        "};\n"
        "const placeUiNode = (node, x, y, w, h) => {\n"
        "  node.style.left = `${x}px`;\n"
        "  node.style.top = `${y}px`;\n"
        "  if (w !== null) node.style.width = `${Math.max(0, w)}px`;\n"
        "  if (h !== null) node.style.height = `${Math.max(0, h)}px`;\n"
        "};\n"
        "const resizeOverlay = () => {\n"
        "  const rect = canvas.getBoundingClientRect();\n"
        "  const scale = canvas.width ? rect.width / canvas.width : 1;\n"
        "  overlay.style.width = `${canvas.width}px`;\n"
        "  overlay.style.height = `${canvas.height}px`;\n"
        "  overlay.style.transform = `scale(${scale})`;\n"
        "};\n"
        "new ResizeObserver(resizeOverlay).observe(canvas);\n"
        "resizeOverlay();\n"
        "const decoder = new TextDecoder('utf-8');\n"
        "let memory;\n"
        "let instance;\n"
        "const drawQueue = [];\n"
        "const pressedKeys = Object.create(null);\n"
        "const imageCache = new Map();\n"
        "const musicCache = new Map();\n"
        "let audioUnlocked = false;\n"
        "let audioContext = null;\n"
        "let mouseX = 0;\n"
        "let mouseY = 0;\n"
        "let mousePressed = false;\n"
        "const assetUrl = (path) => {\n"
        "  if (path.startsWith('/') || path.split('/').some((part) => "
        "part === '.' || part === '..')) {\n"
        "    throw new Error(`invalid asset path: ${path}`);\n"
        "  }\n"
        "  return new URL('assets/' + path.split('/').map("
        "encodeURIComponent).join('/'), import.meta.url).href;\n"
        "};\n"
        "window.addEventListener('keydown', (event) => {\n"
        "  pressedKeys[event.key] = true;\n"
        "});\n"
        "window.addEventListener('keyup', (event) => {\n"
        "  pressedKeys[event.key] = false;\n"
        "});\n"
        "const updateMousePosition = (event) => {\n"
        "  const rect = canvas.getBoundingClientRect();\n"
        "  mouseX = (event.clientX - rect.left) * canvas.width / rect.width;\n"
        "  mouseY = (event.clientY - rect.top) * canvas.height / rect.height;\n"
        "};\n"
        "canvas.addEventListener('mousemove', updateMousePosition);\n"
        "canvas.addEventListener('mousedown', (event) => {\n"
        "  updateMousePosition(event);\n"
        "  mousePressed = true;\n"
        "});\n"
        "window.addEventListener('mouseup', () => { mousePressed = false; });\n"
        "const unlockAudio = () => {\n"
        "  audioUnlocked = true;\n"
        "  const AudioContextType = window.AudioContext || "
        "window.webkitAudioContext;\n"
        "  if (AudioContextType && !audioContext) "
        "audioContext = new AudioContextType();\n"
        "  if (audioContext && audioContext.state === 'suspended') "
        "audioContext.resume().catch((error) => "
        "console.error('audio context resume failed:', error));\n"
        "  for (const audio of musicCache.values()) {\n"
        "    if (audio.paused) audio.play().catch((error) => "
        "console.error('music playback failed:', error));\n"
        "  }\n"
        "};\n"
        "document.addEventListener('click', unlockAudio, { once: true });\n"
        "const textAt = (ptr) => {\n"
        "  const bytes = new Uint8Array(memory.buffer);\n"
        "  const start = ptr >>> 0;\n"
        "  if (start >= bytes.length) "
        "throw new RangeError('WASM string pointer is out of bounds');\n"
        "  let end = start;\n"
        "  while (end < bytes.length && bytes[end] !== 0) end++;\n"
        "  if (end === bytes.length) "
        "throw new RangeError('WASM string is not null-terminated');\n"
        "  return decoder.decode(bytes.subarray(start, end));\n"
        "};\n"
        "const colorOf = (value) => `#${(Number(value) >>> 0 & 0xffffff)"
        ".toString(16).padStart(6, '0')}`;\n"
        "const loadImage = (path) => {\n"
        "  let image = imageCache.get(path);\n"
        "  if (!image) {\n"
        "    image = new Image();\n"
        "    image.onerror = () => console.error(`failed to load image: ${path}`);\n"
        "    imageCache.set(path, image);\n"
        "    image.src = assetUrl(path);\n"
        "  }\n"
        "  return image;\n"
        "};\n"
        "const imports = { env: {\n";
    static const char glue_js_suffix[] =
        "  js_print: (ptr) => console.log(textAt(ptr)),\n"
        "  js_print_int: (value) => console.log(value.toString()),\n"
        "  js_print_bool: (value) => console.log(Boolean(value)),\n"
        "  js_print_float: (value) => console.log(value),\n"
        "  js_screen: (width, height) => {\n"
        "    canvas.width = Math.max(1, width | 0);\n"
        "    canvas.height = Math.max(1, height | 0);\n"
        "  },\n"
        "  js_cls: () => drawQueue.push(() => ctx.clearRect(0, 0, canvas.width, "
        "canvas.height)),\n"
        "  js_rect: (x, y, w, h, color) => drawQueue.push(() => {\n"
        "    ctx.fillStyle = colorOf(color);\n"
        "    ctx.fillRect(x, y, w, h);\n"
        "  }),\n"
        "  js_draw_rect_filled: (x, y, w, h, color) => drawQueue.push(() => {\n"
        "    ctx.fillStyle = colorOf(color);\n"
        "    ctx.fillRect(x, y, w, h);\n"
        "  }),\n"
        "  js_draw_rect_outline: (x, y, w, h, color, thickness) => "
        "drawQueue.push(() => {\n"
        "    ctx.strokeStyle = colorOf(color);\n"
        "    ctx.lineWidth = Math.max(0, thickness);\n"
        "    ctx.strokeRect(x, y, w, h);\n"
        "  }),\n"
        "  js_draw_line: (x1, y1, x2, y2, color) => drawQueue.push(() => {\n"
        "    ctx.beginPath();\n"
        "    ctx.strokeStyle = colorOf(color);\n"
        "    ctx.moveTo(x1, y1);\n"
        "    ctx.lineTo(x2, y2);\n"
        "    ctx.stroke();\n"
        "  }),\n"
        "  js_draw_gradient_rect: (x, y, w, h, color1, color2) => "
        "drawQueue.push(() => {\n"
        "    const gradient = ctx.createLinearGradient(x, y, x, y + h);\n"
        "    gradient.addColorStop(0, colorOf(color1));\n"
        "    gradient.addColorStop(1, colorOf(color2));\n"
        "    ctx.fillStyle = gradient;\n"
        "    ctx.fillRect(x, y, w, h);\n"
        "  }),\n"
        "  js_circle: (x, y, radius, color) => drawQueue.push(() => {\n"
        "    ctx.beginPath();\n"
        "    ctx.fillStyle = colorOf(color);\n"
        "    ctx.arc(x, y, Math.max(0, radius), 0, Math.PI * 2);\n"
        "    ctx.fill();\n"
        "  }),\n"
        "  js_draw_image: (ptr, x, y) => {\n"
        "    const path = textAt(ptr);\n"
        "    const image = loadImage(path);\n"
        "    drawQueue.push(() => {\n"
        "      if (image.complete && image.naturalWidth > 0) "
        "ctx.drawImage(image, x, y);\n"
        "    });\n"
        "  },\n"
        "  js_draw_text: (ptr, x, y, size, color, fontPtr) => {\n"
        "    const text = textAt(ptr);\n"
        "    const font = fontPtr ? textAt(fontPtr) : 'sans-serif';\n"
        "    drawQueue.push(() => {\n"
        "      ctx.fillStyle = colorOf(color);\n"
        "      ctx.font = `${Math.max(1, size | 0)}px ${font}`;\n"
        "      ctx.fillText(text, x, y);\n"
        "    });\n"
        "  },\n"
        "  js_draw_text_number: (value, x, y, size, color, fontPtr) => {\n"
        "    const font = fontPtr ? textAt(fontPtr) : 'sans-serif';\n"
        "    drawQueue.push(() => {\n"
        "      ctx.fillStyle = colorOf(color);\n"
        "      ctx.font = `${Math.max(1, size | 0)}px ${font}`;\n"
        "      ctx.fillText(String(value), x, y);\n"
        "    });\n"
        "  },\n"
        "  js_ui_text: (textPtr, x, y, size, color, fontPtr) => {\n"
        "    const text = textAt(textPtr);\n"
        "    const font = textAt(fontPtr);\n"
        "    const key = `text:${x}:${y}:${text}`;\n"
        "    const node = ensureUiNode(key, 'div', 'sourc-ui-text');\n"
        "    placeUiNode(node, x, y, null, null);\n"
        "    node.textContent = text;\n"
        "    node.style.color = colorOf(color);\n"
        "    node.style.font = `${Math.max(1, size | 0)}px ${font}`;\n"
        "  },\n"
        "  js_ui_panel: (x, y, w, h, color, radius) => {\n"
        "    const key = `panel:${x}:${y}:${w}:${h}`;\n"
        "    const node = ensureUiNode(key, 'div', 'sourc-ui-panel');\n"
        "    placeUiNode(node, x, y, w, h);\n"
        "    node.style.background = colorOf(color);\n"
        "    node.style.borderRadius = `${Math.max(0, radius)}px`;\n"
        "  },\n"
        "  js_ui_code: (textPtr, x, y, w, h) => {\n"
        "    const text = textAt(textPtr);\n"
        "    const key = `code:${x}:${y}:${w}:${h}`;\n"
        "    const node = ensureUiNode(key, 'pre', 'sourc-ui-code');\n"
        "    placeUiNode(node, x, y, w, h);\n"
        "    let code = node.querySelector('code');\n"
        "    if (!code) { code = document.createElement('code'); "
        "node.appendChild(code); }\n"
        "    code.textContent = text;\n"
        "  },\n"
        "  js_ui_button: (textPtr, x, y, w, h, idPtr) => {\n"
        "    const text = textAt(textPtr);\n"
        "    const id = textAt(idPtr);\n"
        "    const key = `button:${id}`;\n"
        "    const node = ensureUiNode(key, 'button', 'sourc-ui-button');\n"
        "    if (!node.dataset.sourcBound) {\n"
        "      node.dataset.sourcBound = 'true';\n"
        "      node.addEventListener('click', () => {\n"
        "        node.dataset.sourcClicked = 'true';\n"
        "        if (id === 'download-source') "
        "window.open('https://github.com/xelt-realm/Sourc/releases/latest', "
        "'_blank', 'noopener');\n"
        "      });\n"
        "    }\n"
        "    placeUiNode(node, x, y, w, h);\n"
        "    node.textContent = text;\n"
        "    const clicked = node.dataset.sourcClicked === 'true';\n"
        "    node.dataset.sourcClicked = 'false';\n"
        "    return clicked ? 1 : 0;\n"
        "  },\n"
        "  js_copy_to_clipboard: (textPtr) => {\n"
        "    const text = textAt(textPtr);\n"
        "    if (navigator.clipboard && window.isSecureContext) {\n"
        "      navigator.clipboard.writeText(text).catch((error) => "
        "console.error('clipboard copy failed:', error));\n"
        "      return;\n"
        "    }\n"
        "    const field = document.createElement('textarea');\n"
        "    field.value = text;\n"
        "    field.style.position = 'fixed';\n"
        "    field.style.opacity = '0';\n"
        "    document.body.appendChild(field);\n"
        "    field.select();\n"
        "    const copied = document.execCommand('copy');\n"
        "    field.remove();\n"
        "    if (!copied) throw new Error('clipboard copy is unavailable');\n"
        "  },\n"
        "  js_play_sound: (ptr) => {\n"
        "    if (!audioUnlocked) return;\n"
        "    const audio = new Audio(assetUrl(textAt(ptr)));\n"
        "    audio.play().catch((error) => console.error('sound playback "
        "failed:', error));\n"
        "  },\n"
        "  js_play_music: (ptr) => {\n"
        "    const path = textAt(ptr);\n"
        "    let audio = musicCache.get(path);\n"
        "    if (!audio) {\n"
        "      audio = new Audio(assetUrl(path));\n"
        "      audio.loop = true;\n"
        "      musicCache.set(path, audio);\n"
        "    }\n"
        "    if (audio.paused) audio.play().catch((error) => "
        "console.error('music playback failed:', error));\n"
        "  },\n"
        "  js_random: (min, max) => Math.random() * (max - min) + min,\n"
        "  js_sin: (value) => Math.sin(value),\n"
        "  js_cos: (value) => Math.cos(value),\n"
        "  js_sqrt: (value) => Math.sqrt(value),\n"
        "  js_floor: (value) => Math.floor(value),\n"
        "  js_abs: (value) => Math.abs(value),\n"
        "  js_flip: () => {},\n"
        "  js_key: (ptr) => pressedKeys[textAt(ptr)] ? 1 : 0,\n"
        "  js_mouse_x: () => mouseX,\n"
        "  js_mouse_y: () => mouseY,\n"
        "  js_mouse_down: () => mousePressed ? 1 : 0\n"
        "} };\n"
        "const { instance: wasm } = await WebAssembly.instantiate(\n"
        "  await (await fetch('app.wasm')).arrayBuffer(), imports);\n"
        "instance = wasm;\n"
        "memory = wasm.exports.memory;\n"
        "if (!memory) throw new Error('WASM module did not export memory');\n"
        "if (typeof wasm.exports.main !== 'function' || "
        "typeof wasm.exports.update !== 'function' || "
        "typeof wasm.exports.sourc_set_frame_time !== 'function') "
        "throw new Error('WASM module did not export required functions');\n"
        "const startedAt = performance.now();\n"
        "let previousFrame = startedAt;\n"
        "const frame = () => {\n"
        "  const now = performance.now();\n"
        "  wasm.exports.sourc_set_frame_time((now - previousFrame) / 1000, "
        "(now - startedAt) / 1000);\n"
        "  previousFrame = now;\n"
        "  wasm.exports.update();\n"
        "  for (const draw of drawQueue.splice(0)) draw();\n"
        "  requestAnimationFrame(frame);\n"
        "};\n"
        "wasm.exports.main();\n"
        "requestAnimationFrame(frame);\n";

    if (mkdir("dist", 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "dist: %s\n", strerror(errno));
        return 0;
    }
    struct stat dist_status;
    if (stat("dist", &dist_status) != 0 || !S_ISDIR(dist_status.st_mode)) {
        fprintf(stderr, "dist exists and is not a directory\n");
        return 0;
    }
    if (!copy_assets(source_path)) {
        return 0;
    }

    char *c_source = NULL;
    size_t c_length = 0;
    if (!codegen_c(program, &c_source, &c_length)) {
        return 0;
    }
    int ok = write_file("dist/main.c", c_source, c_length);
    free(c_source);
    if (ok) {
        ok = write_file("dist/index.html", index_html,
                        sizeof(index_html) - 1);
    }
    if (ok) {
        ok = write_file("dist/glue.js", glue_js_prefix,
                        sizeof(glue_js_prefix) - 1) &&
             append_file("dist/glue.js", glue_js_suffix,
                         sizeof(glue_js_suffix) - 1);
    }
    if (!ok) {
        return 0;
    }

    char *clang_args[] = {
        "clang", "--target=wasm32", "-O3", "-nostdlib",
        "-Wl,--no-entry", "-Wl,--export-all",
        "-Wl,--export=main", "-Wl,--export=update",
        "-Wl,--export=sourc_set_frame_time",
        "-Wl,--export-memory", "-o", "dist/app.wasm",
        "dist/main.c", NULL
    };
    if (!run_tool(clang_args)) {
        fprintf(stderr,
                "Error: Asegúrate de tener clang con soporte wasm32 instalado "
                "en el PATH.\n");
        return 0;
    }
    return 1;
}

int main(int argc, char **argv) {
    if (argc < 3 || strcmp(argv[1], "build") != 0) {
        usage(stderr);
        return 2;
    }
    Target target = TARGET_IR;
    int option_seen = 0;
    for (int i = 3; i < argc; i++) {
        if (strcmp(argv[i], "--emit-ir") == 0) {
            if (option_seen) {
                fprintf(stderr, "choose only one output option\n");
                return 2;
            }
            target = TARGET_IR;
            option_seen = 1;
        } else if (strcmp(argv[i], "--target=native") == 0) {
            if (option_seen) {
                fprintf(stderr, "choose only one output option\n");
                return 2;
            }
            target = TARGET_NATIVE;
            option_seen = 1;
        } else if (strcmp(argv[i], "--target=web") == 0) {
            if (option_seen) {
                fprintf(stderr, "choose only one output option\n");
                return 2;
            }
            target = TARGET_WEB;
            option_seen = 1;
        } else {
            fprintf(stderr, "unknown option: %s\n", argv[i]);
            usage(stderr);
            return 2;
        }
    }

    int source_fd = open(argv[2], O_RDONLY);
    if (source_fd < 0) {
        fprintf(stderr, "%s: %s\n", argv[2], strerror(errno));
        return 1;
    }
    struct stat status;
    if (fstat(source_fd, &status) != 0 || status.st_size < 0 ||
        (uintmax_t)status.st_size > SIZE_MAX) {
        fprintf(stderr, "%s: cannot read source size\n", argv[2]);
        close(source_fd);
        return 1;
    }
    size_t source_length = (size_t)status.st_size;
    const char *source = "";
    void *mapping = MAP_FAILED;
    if (source_length > 0) {
        mapping = mmap(NULL, source_length, PROT_READ, MAP_PRIVATE, source_fd, 0);
        if (mapping == MAP_FAILED) {
            fprintf(stderr, "%s: mmap failed: %s\n", argv[2], strerror(errno));
            close(source_fd);
            return 1;
        }
        source = mapping;
    }
    close(source_fd);

    TokenList tokens = {0};
    Ast ast;
    ast_init(&ast);
    int ok = lexer_tokenize(source, source_length, &tokens);
    if (ok) {
        ok = parser_parse(source, &tokens, &ast);
    }

    if (ok && target == TARGET_WEB) {
        ok = build_web(&ast.program, argv[2]);
    } else if (ok) {
        char *ir = NULL;
        size_t ir_length = 0;
        ok = codegen_qbe(&ast.program, &ir, &ir_length);
        char *ssa_path = source_output_path(argv[2], ".ssa");
        if (ok && !ssa_path) {
            fprintf(stderr, "out of memory while preparing output path\n");
            ok = 0;
        }
        if (ok) {
            ok = write_file(ssa_path, ir, ir_length);
        }
        if (ok && target == TARGET_NATIVE) {
            ok = build_native(ssa_path);
        }
        free(ssa_path);
        free(ir);
    }

    ast_destroy(&ast);
    lexer_destroy(&tokens);
    if (mapping != MAP_FAILED) {
        munmap(mapping, source_length);
    }
    return ok ? 0 : 1;
}
