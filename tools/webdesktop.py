#!/usr/bin/env python3
"""
Escritorio web para macOS 26 corriendo en un runner de GitHub Actions.

Sirve la consola real del runner (que tiene sesion grafica de 'runner') como
imagenes JPEG y permite inyectar raton y teclado.

Endpoints:
  GET  /            -> HTML del escritorio
  GET  /screen.jpg  -> captura actual de la consola
  GET  /info        -> {width,height}
  POST /event       -> {kind: move|down|up|key|text, ...}
"""
import http.server
import json
import os
import socketserver
import subprocess
import threading
import time

SHOT = "/tmp/webdesktop.jpg"
STATE = {"data": b"", "mtime": 0}
LOCK = threading.Lock()
SCREEN = {"w": 1024, "h": 768}

CLICLICK_CANDIDATES = [
    "/opt/homebrew/bin/cliclick",
    "/usr/local/bin/cliclick",
    "cliclick",
]

KEYMAP = {
    "Enter": "return", "Backspace": "delete", "Tab": "tab", "Escape": "esc",
    " ": "space", "ArrowUp": "up", "ArrowDown": "down",
    "ArrowLeft": "left", "ArrowRight": "right", "Home": "home",
    "End": "end", "PageUp": "pageup", "PageDown": "pagedown",
    "Delete": "delete", "Meta": "command", "Control": "ctrl", "Alt": "alt",
    "Shift": "shift",
}


def find_cliclick():
    for c in CLICLICK_CANDIDATES:
        if os.path.isabs(c) and os.path.exists(c):
            return c
        if not os.path.isabs(c):
            from shutil import which
            p = which(c)
            if p:
                return p
    return None


CLICLICK = find_cliclick()


def run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception as e:
        return None


def run_via_bash(cmd, timeout=15):
    """Ejecuta a traves de /bin/bash, que ya tiene los permisos TCC (accesibilidad
    y captura de pantalla) concedidos en la imagen de GitHub."""
    try:
        return subprocess.run(["/bin/bash", "-c", cmd],
                              capture_output=True, timeout=timeout)
    except Exception:
        return None


def detect_size():
    r = run(["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", SHOT])
    if r and r.returncode == 0:
        out = r.stdout.decode("utf-8", "replace")
        vals = {}
        for line in out.splitlines():
            if "pixelWidth" in line:
                vals["w"] = int(line.split(":")[-1].strip())
            if "pixelHeight" in line:
                vals["h"] = int(line.split(":")[-1].strip())
        if "w" in vals and "h" in vals:
            SCREEN["w"], SCREEN["h"] = vals["w"], vals["h"]


def capture_loop():
    while True:
        run_via_bash("/usr/sbin/screencapture -x -t jpg " + SHOT, timeout=20)
        try:
            with open(SHOT, "rb") as f:
                data = f.read()
            if data:
                with LOCK:
                    STATE["data"] = data
                    STATE["mtime"] = time.time()
        except Exception:
            pass
        time.sleep(0.55)


def cliclick(*args):
    if not CLICLICK:
        return
    run_via_bash(" ".join([CLICLICK] + [str(a) for a in args]), timeout=10)


def handle_event(ev):
    kind = ev.get("kind")
    x = int(ev.get("x", 0))
    y = int(ev.get("y", 0))
    if kind == "move":
        cliclick(f"m:{x},{y}")
    elif kind == "down":
        cliclick(f"dd:{x},{y}")
    elif kind == "up":
        cliclick(f"du:{x},{y}")
    elif kind == "key":
        k = ev.get("key", "space")
        cliclick("kp:" + KEYMAP.get(k, k))
    elif kind == "text":
        t = ev.get("text", "")
        if t:
            cliclick("t:" + t)
    elif kind == "scroll":
        d = int(ev.get("dy", 0))
        if d:
            cliclick("wd:" if d < 0 else "wu:")


HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>macOS 26 (Tahoe) - runner</title>
<style>
  html,body{margin:0;background:#0b0b0f;color:#ddd;font:13px system-ui;
            display:flex;flex-direction:column;align-items:center;
            justify-content:center;height:100vh;overflow:hidden}
  #wrap{position:relative;box-shadow:0 0 40px #000;border:1px solid #222}
  img{display:block;image-rendering:auto;user-select:none}
  #hud{position:absolute;top:6px;left:8px;background:#000a;padding:4px 8px;
       border-radius:6px;font-size:11px;pointer-events:none}
  #bar{position:fixed;bottom:8px;left:8px;background:#000a;padding:5px 10px;
       border-radius:6px;font-size:11px}
</style></head><body>
<div id="wrap"><img id="s" src="/screen.jpg?t=0"><div id="hud">macOS 26 - cargando...</div></div>
<div id="bar">raton: clic / arrastrar &nbsp;|&nbsp; teclado: escribe en la pagina &nbsp;|&nbsp; ~1s de latencia</div>
<script>
const img=document.getElementById('s'), hud=document.getElementById('hud');
let W=1024,H=768,lastMove=0;
fetch('/info').then(r=>r.json()).then(j=>{W=j.width;H=j.height;});
function pos(e){const r=img.getBoundingClientRect();
  return {x:Math.max(0,Math.min(W-1,Math.round((e.clientX-r.left)/r.width*W))),
          y:Math.max(0,Math.min(H-1,Math.round((e.clientY-r.top)/r.height*H)))};}
function send(ev){fetch('/event',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(ev)}).catch(()=>{});}
img.addEventListener('mousemove',e=>{const now=Date.now();if(now-lastMove<90)return;
  lastMove=now;send({kind:'move',...pos(e)});});
img.addEventListener('mousedown',e=>{e.preventDefault();send({kind:'down',...pos(e)});});
window.addEventListener('mouseup',e=>{send({kind:'up',...pos(e)});});
img.addEventListener('contextmenu',e=>e.preventDefault());
img.addEventListener('wheel',e=>{e.preventDefault();send({kind:'scroll',dy:e.deltaY});});
window.addEventListener('keydown',e=>{
  if(e.key.length===1){send({kind:'text',text:e.key});}
  else{send({kind:'key',key:e.key});}
  e.preventDefault();
});
function poll(){img.src='/screen.jpg?t='+Date.now();
  hud.textContent='macOS 26 - '+new Date().toLocaleTimeString();}
setInterval(poll,700);
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, HTML, "text/html; charset=utf-8")
        elif path == "/screen.jpg":
            with LOCK:
                data = STATE["data"]
            if not data:
                self._send(503, b"sin captura", "text/plain")
            else:
                self._send(200, data, "image/jpeg")
        elif path == "/info":
            self._send(200, json.dumps({"width": SCREEN["w"], "height": SCREEN["h"]}),
                       "application/json")
        else:
            self._send(404, b"no", "text/plain")

    def do_POST(self):
        if self.path.split("?")[0] != "/event":
            self._send(404, b"no", "text/plain")
            return
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b"{}"
        try:
            ev = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            ev = {}
        try:
            handle_event(ev)
        except Exception:
            pass
        self._send(200, b"ok", "text/plain")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "6081"))
    threading.Thread(target=capture_loop, daemon=True).start()
    time.sleep(3)
    detect_size()
    print(f"webdesktop: {SCREEN['w']}x{SCREEN['h']} cliclick={CLICLICK}", flush=True)
    Server(("127.0.0.1", port), Handler).serve_forever()
