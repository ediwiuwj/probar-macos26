#!/usr/bin/env python3
"""
Escritorio web para macOS 26 en un runner de GitHub Actions.

VISTA   : streaming MJPEG nativo (multipart/x-mixed-replace). El navegador
          reproduce el flujo solo, sin polling.
CAPTURA : CoreGraphics (CGDisplayCreateImage) en bucle cerrado -> ~30 fps.
          Si pyobjc no esta disponible, cae a 'screencapture' por proceso.
CONTROL : POST /event con inyeccion de raton/teclado via cliclick.

Endpoints:
  GET  /              -> HTML
  GET  /stream.mjpeg  -> stream de frames
  GET  /screen.jpg    -> frame actual (debug)
  GET  /info          -> {width,height,fps,backend}
  POST /event         -> {kind: move|down|up|key|text|scroll, ...}
"""
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import threading
import time

SHOT = "/tmp/webdesktop.jpg"
STATE = {"data": b"", "mtime": 0.0, "seq": 0, "fps": 0.0, "backend": "?",
         "iw": 1024, "ih": 768, "sw": 1024, "sh": 768}
LOCK = threading.Lock()
SCREEN = {"w": 1024, "h": 768}
FPS = float(os.environ.get("FPS", "30"))
QUALITY = float(os.environ.get("QUALITY", "0.45"))

KEYMAP = {
    "Enter": "return", "Backspace": "delete", "Tab": "tab", "Escape": "esc",
    " ": "space", "ArrowUp": "up", "ArrowDown": "down",
    "ArrowLeft": "left", "ArrowRight": "right", "Home": "home",
    "End": "end", "PageUp": "pageup", "PageDown": "pagedown",
    "Delete": "delete", "Meta": "command", "Control": "ctrl", "Alt": "alt",
    "Shift": "shift",
}

try:
    from Quartz import (
        CGMainDisplayID, CGDisplayCreateImage, CGDisplayPixelsWide,
        CGDisplayPixelsHigh, CGImageGetWidth, CGImageGetHeight,
        CGImageDestinationCreateWithURL,
        CGImageDestinationAddImage, CGImageDestinationFinalize,
        CFURLCreateFromFileSystemRepresentation,
        kCGImageDestinationLossyCompressionQuality,
    )
    QUARTZ = True
except Exception as _e:  # pragma: no cover
    QUARTZ = False
    _QUARTZ_ERR = str(_e)


def find_cliclick():
    p = shutil.which("cliclick")
    if p:
        return p
    for c in ("/opt/homebrew/bin/cliclick", "/usr/local/bin/cliclick"):
        if os.path.exists(c):
            return c
    return None


CLICLICK = find_cliclick()


def run_via_bash(cmd, timeout=15):
    """Todo pasa por /bin/bash, que ya tiene TCC de Screen Recording y
    Accessibility concedido en la imagen de GitHub."""
    try:
        return subprocess.run(["/bin/bash", "-c", cmd],
                              capture_output=True, timeout=timeout)
    except Exception:
        return None


def _publish(data):
    with LOCK:
        STATE["data"] = data
        STATE["mtime"] = time.time()
        STATE["seq"] += 1


def capture_quartz():
    """Captura nativa de CoreGraphics + JPEG con ImageIO (sin procesos)."""
    did = CGMainDisplayID()
    SCREEN["w"] = CGDisplayPixelsWide(did) or 1024
    SCREEN["h"] = CGDisplayPixelsHigh(did) or 768
    path = SHOT.encode()
    url = CFURLCreateFromFileSystemRepresentation(None, path, len(path), False)
    interval = 1.0 / FPS
    frames = 0
    t0 = time.time()
    mem_ok = True
    try:
        from Foundation import NSMutableData
        from Quartz import CGImageDestinationCreateWithData
    except Exception:
        mem_ok = False
    while True:
        t = time.time()
        try:
            img = CGDisplayCreateImage(did)
            if img is not None:
                # Tamano real de la IMAGEN capturada y del display actual.
                # Se recalcula en cada frame: si el usuario cambia la
                # resolucion en Ajustes, el mapeo del puntero se corrige solo.
                try:
                    iw = CGImageGetWidth(img)
                    ih = CGImageGetHeight(img)
                    sw = CGDisplayPixelsWide(did)
                    sh = CGDisplayPixelsHigh(did)
                except Exception:
                    iw = ih = sw = sh = 0
                if iw and ih:
                    with LOCK:
                        STATE["iw"] = iw
                        STATE["ih"] = ih
                        STATE["sw"] = sw or iw
                        STATE["sh"] = sh or ih
                    SCREEN["w"], SCREEN["h"] = sw or iw, sh or ih
                data = None
                if mem_ok:
                    try:
                        buf = NSMutableData.data()
                        dest = CGImageDestinationCreateWithData(
                            buf, "public.jpeg", 1, None)
                        if dest is not None:
                            CGImageDestinationAddImage(
                                dest, img,
                                {kCGImageDestinationLossyCompressionQuality: QUALITY})
                            if CGImageDestinationFinalize(dest):
                                data = bytes(buf)
                    except Exception:
                        mem_ok = False
                        data = None
                if data is None:
                    dest = CGImageDestinationCreateWithURL(
                        url, "public.jpeg", 1, None)
                    if dest is not None:
                        CGImageDestinationAddImage(
                            dest, img,
                            {kCGImageDestinationLossyCompressionQuality: QUALITY})
                        CGImageDestinationFinalize(dest)
                        with open(SHOT, "rb") as f:
                            data = f.read()
                if data:
                    _publish(data)
                    frames += 1
                    el = time.time() - t0
                    if el > 1:
                        with LOCK:
                            STATE["fps"] = frames / el
                        frames = 0
                        t0 = time.time()
        except Exception as e:
            with LOCK:
                STATE["backend"] = "quartz-error: " + str(e)[:80]
            time.sleep(1)
            continue
        dt = time.time() - t
        if dt < interval:
            time.sleep(interval - dt)


def capture_subprocess():
    """Fallback: screencapture por proceso."""
    interval = 1.0 / max(1, FPS // 4)
    while True:
        run_via_bash("/usr/sbin/screencapture -x -t jpg " + SHOT, timeout=20)
        try:
            with open(SHOT, "rb") as f:
                _publish(f.read())
        except Exception:
            pass
        time.sleep(interval)


def cliclick(*args):
    if not CLICLICK:
        return
    run_via_bash(" ".join([CLICLICK] + [str(a) for a in args]), timeout=10)


def handle_event(ev):
    kind = ev.get("kind")
    # El navegador envia coordenadas en pixeles de la IMAGEN que ve.
    # Las convertimos a pixeles de PANTALLA, que pueden diferir si la
    # captura esta escalada o si se cambio la resolucion.
    with LOCK:
        iw = STATE["iw"] or 1
        ih = STATE["ih"] or 1
        sw = STATE["sw"] or iw
        sh = STATE["sh"] or ih
    x_img = int(ev.get("x", 0))
    y_img = int(ev.get("y", 0))
    x = int(round(x_img * sw / iw))
    y = int(round(y_img * sh / ih))
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
  html,body{margin:0;height:100%;background:#08080c;color:#ddd;
            font:13px system-ui;overflow:hidden}
  #stage{position:absolute;inset:4px;display:flex;align-items:center;
         justify-content:center}
  img{max-width:100%;max-height:100%;width:auto;height:auto;display:block;
      image-rendering:auto;user-select:none;cursor:crosshair;
      border:1px solid #2b2b33;box-shadow:0 0 30px #000}
  #mk{position:absolute;width:16px;height:16px;margin:-8px 0 0 -8px;
      border:2px solid #ff3b30;border-radius:50%;display:none;
      pointer-events:none;box-shadow:0 0 6px #000}
  #hud{position:fixed;top:8px;left:10px;background:#000b;padding:4px 9px;
       border-radius:6px;font-size:11px;pointer-events:none}
  #bar{position:fixed;bottom:8px;left:10px;background:#000b;padding:5px 11px;
       border-radius:6px;font-size:11px}
</style></head><body>
<div id="stage"><div id="wrap" style="position:relative;display:flex;align-items:center;justify-content:center">
  <img id="s" src="/stream.mjpeg"><div id="mk"></div></div></div>
<div id="hud">macOS 26</div>
<div id="bar">raton: clic / arrastrar &nbsp;|&nbsp; teclado: escribe en la pagina &nbsp;|&nbsp; <span id="fps">-</span> fps</div>
<script>
const img=document.getElementById('s'), hud=document.getElementById('hud');
const mk=document.getElementById('mk');
let W=1024,H=768,lastMove=0;
function refreshInfo(){fetch('/info').then(r=>r.json()).then(j=>{
  W=j.width;H=j.height;
  document.getElementById('fps').textContent=j.fps.toFixed(1);
}).catch(()=>{});}
refreshInfo(); setInterval(refreshInfo,500);
function pos(e){const r=img.getBoundingClientRect();
  if(!r.width||!r.height) return {x:0,y:0};
  return {x:Math.max(0,Math.min(W-1,Math.round((e.clientX-r.left)/r.width*W))),
          y:Math.max(0,Math.min(H-1,Math.round((e.clientY-r.top)/r.height*H)))};}
function mark(e){const r=img.getBoundingClientRect();
  if(!r.width||!r.height)return;
  mk.style.display='block';
  mk.style.left=(e.clientX-r.left)+'px';
  mk.style.top=(e.clientY-r.top)+'px';}
function send(ev){fetch('/event',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(ev)}).catch(()=>{});}
img.addEventListener('mousemove',e=>{const now=Date.now();if(now-lastMove<50)return;
  lastMove=now;send({kind:'move',...pos(e)});});
img.addEventListener('mousedown',e=>{e.preventDefault();mark(e);
  send({kind:'down',...pos(e)});});
window.addEventListener('mouseup',e=>{send({kind:'up',...pos(e)});});
img.addEventListener('contextmenu',e=>e.preventDefault());
img.addEventListener('wheel',e=>{e.preventDefault();send({kind:'scroll',dy:e.deltaY});});
window.addEventListener('keydown',e=>{
  if(e.key.length===1){send({kind:'text',text:e.key});}
  else{send({kind:'key',key:e.key});}
  e.preventDefault();
});
setInterval(()=>{hud.textContent='macOS 26 - '+new Date().toLocaleTimeString();},500);
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

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        last_seq = -1
        try:
            while True:
                with LOCK:
                    data = STATE["data"]
                    seq = STATE["seq"]
                if data and seq != last_seq:
                    last_seq = seq
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n")
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                time.sleep(0.005)
        except Exception:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, HTML, "text/html; charset=utf-8")
        elif path == "/stream.mjpeg":
            self._stream()
        elif path == "/screen.jpg":
            with LOCK:
                data = STATE["data"]
            if not data:
                self._send(503, b"sin captura", "text/plain")
            else:
                self._send(200, data, "image/jpeg")
        elif path == "/info":
            with LOCK:
                fps = STATE["fps"]
                backend = STATE["backend"]
                iw = STATE["iw"]
                ih = STATE["ih"]
                sw = STATE["sw"]
                sh = STATE["sh"]
            self._send(200, json.dumps({"width": iw, "height": ih,
                                        "screen_w": sw, "screen_h": sh,
                                        "fps": round(fps, 1), "backend": backend}),
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
    if QUARTZ:
        with LOCK:
            STATE["backend"] = "coregraphics"
        threading.Thread(target=capture_quartz, daemon=True).start()
    else:
        with LOCK:
            STATE["backend"] = "screencapture (sin pyobjc: " + _QUARTZ_ERR[:60] + ")"
        threading.Thread(target=capture_subprocess, daemon=True).start()
    time.sleep(4)
    print(f"webdesktop: {SCREEN['w']}x{SCREEN['h']} target={FPS}fps "
          f"backend={STATE['backend']} cliclick={CLICLICK}", flush=True)
    Server(("127.0.0.1", port), Handler).serve_forever()
