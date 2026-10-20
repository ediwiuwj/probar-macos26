#!/usr/bin/env python3
"""
Escritorio web para macOS 26 en un runner de GitHub Actions.

VISTA   : streaming MJPEG nativo (multipart/x-mixed-replace). El navegador
          reproduce el flujo solo, sin polling.
CAPTURA : CoreGraphics (CGDisplayCreateImage) + JPEG con ImageIO, en un PROCESO
          HIJO para no competir con el hilo HTTP por el GIL.
          Si pyobjc no esta, cae a 'screencapture' por proceso.
CONTROL : POST /event con inyeccion de raton/teclado via cliclick.

Endpoints:
  GET  /              -> HTML
  GET  /stream.mjpeg  -> stream de frames
  GET  /screen.jpg    -> frame actual (debug)
  GET  /info          -> {width,height,screen_w,screen_h,fps,backend}
  GET  /profile       -> desglose de tiempos + benchmark del encoder
  POST /event         -> {kind: move|down|up|key|text|scroll, ...}
"""
import http.server
import json
import multiprocessing as mp
import os
import shutil
import socketserver
import subprocess
import threading
import time

SHOT = "/tmp/webdesktop.jpg"
STATE = {"data": b"", "mtime": 0.0, "seq": 0, "fps": 0.0, "backend": "?",
         "iw": 1024, "ih": 768, "sw": 1024, "sh": 768,
         "t_capture": 0.0, "t_encode": 0.0, "t_publish": 0.0, "n": 0,
         "bench": {}}
LOCK = threading.Lock()
SCREEN = {"w": 1024, "h": 768}
FPS = float(os.environ.get("FPS", "30"))
QUALITY = float(os.environ.get("QUALITY", "0.35"))

KEYMAP = {
    "Enter": "return", "Backspace": "delete", "Tab": "tab", "Escape": "esc",
    " ": "space", "ArrowUp": "up", "ArrowDown": "down",
    "ArrowLeft": "left", "ArrowRight": "right", "Home": "home",
    "End": "end", "PageUp": "pageup", "PageDown": "pagedown",
    "Delete": "delete", "Meta": "command", "Control": "ctrl", "Alt": "alt",
    "Shift": "shift",
}


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


SCALE_FILE = "/tmp/wd_scale"
_scale_cache = {"mtime": 0.0, "v": 1.0}


def read_scale():
    """Factor de escala en runtime (lo cambia /set?scale=)."""
    try:
        st = os.stat(SCALE_FILE)
        if st.st_mtime != _scale_cache["mtime"]:
            with open(SCALE_FILE) as f:
                _scale_cache["v"] = max(0.1, min(1.0, float(f.read().strip() or 1.0)))
            _scale_cache["mtime"] = st.st_mtime
    except Exception:
        _scale_cache["v"] = 1.0
    return _scale_cache["v"]


def downscale(img, iw, ih, sc):
    """Reescala la imagen con CoreGraphics (CPU)."""
    try:
        from Quartz import (CGBitmapContextCreate, CGBitmapContextCreateImage,
                            CGColorSpaceCreateDeviceRGB, CGContextDrawImage,
                            CGRectMake, CGContextSetInterpolationQuality,
                            kCGImageAlphaPremultipliedFirst,
                            kCGBitmapByteOrder32Host, kCGInterpolationMedium)
        w2, h2 = max(1, int(iw * sc)), max(1, int(ih * sc))
        cs = CGColorSpaceCreateDeviceRGB()
        ctx = CGBitmapContextCreate(
            None, w2, h2, 8, w2 * 4, cs,
            kCGImageAlphaPremultipliedFirst | kCGBitmapByteOrder32Host)
        if ctx is None:
            return None
        CGContextSetInterpolationQuality(ctx, kCGInterpolationMedium)
        CGContextDrawImage(ctx, CGRectMake(0, 0, w2, h2), img)
        return CGBitmapContextCreateImage(ctx)
    except Exception:
        return None


# ---------------------------------------------------------------- captura (hijo)

def capture_worker(q, fps, quality):
    """Proceso hijo: captura con CoreGraphics y encola los frames."""
    try:
        from Quartz import (
            CGMainDisplayID, CGDisplayCreateImage, CGDisplayPixelsWide,
            CGDisplayPixelsHigh, CGImageGetWidth, CGImageGetHeight,
            CGImageDestinationCreateWithData, CGImageDestinationAddImage,
            CGImageDestinationFinalize, kCGImageDestinationLossyCompressionQuality,
        )
        from Foundation import NSMutableData
    except Exception as e:
        q.put(("err", "quartz no disponible: " + str(e)[:150]))
        return

    did = CGMainDisplayID()
    interval = 1.0 / max(1.0, fps) if fps > 0 else 0.0
    # Tasa de refresco del display virtual: si es baja, ES el techo de fps.
    try:
        from Quartz import (CGDisplayCopyDisplayMode,
                            CGDisplayModeGetRefreshRate,
                            CGDisplayModeGetPixelWidth,
                            CGDisplayModeGetPixelHeight)
        mode = CGDisplayCopyDisplayMode(did)
        rr = float(CGDisplayModeGetRefreshRate(mode)) if mode else 0.0
        mw = int(CGDisplayModeGetPixelWidth(mode)) if mode else 0
        mh = int(CGDisplayModeGetPixelHeight(mode)) if mode else 0
    except Exception:
        rr, mw, mh = 0.0, 0, 0
    q.put(("meta", rr, mw, mh))
    n = 0
    bench = None
    t_prev = time.time()
    while True:
        t = time.time()
        # Tiempo de ciclo completo (incluye sleep y bloqueo en put).
        ms_iter = (t - t_prev) * 1000
        t_prev = t
        try:
            t0c = time.time()
            img = CGDisplayCreateImage(did)
            t_capture = (time.time() - t0c) * 1000
            if img is None:
                time.sleep(0.1)
                continue
            iw = CGImageGetWidth(img)
            ih = CGImageGetHeight(img)
            sw = CGDisplayPixelsWide(did)
            sh = CGDisplayPixelsHigh(did)

            sc = read_scale()
            if sc != 1.0:
                img2 = downscale(img, iw, ih, sc)
                if img2 is not None:
                    img = img2
                    iw = CGImageGetWidth(img)
                    ih = CGImageGetHeight(img)

            t0e = time.time()
            data = None
            try:
                buf = NSMutableData.data()
                dest = CGImageDestinationCreateWithData(buf, "public.jpeg", 1, None)
                if dest is not None:
                    CGImageDestinationAddImage(
                        dest, img,
                        {kCGImageDestinationLossyCompressionQuality: quality})
                    if CGImageDestinationFinalize(dest):
                        data = bytes(buf)
            except Exception:
                data = None
            t_encode = (time.time() - t0e) * 1000

            if data is None:
                # fallback: escribir a fichero y releer
                try:
                    from Quartz import (CGImageDestinationCreateWithURL,
                                        CFURLCreateFromFileSystemRepresentation)
                    p = SHOT.encode()
                    url = CFURLCreateFromFileSystemRepresentation(
                        None, p, len(p), False)
                    dest = CGImageDestinationCreateWithURL(url, "public.jpeg", 1, None)
                    if dest is not None:
                        CGImageDestinationAddImage(
                            dest, img,
                            {kCGImageDestinationLossyCompressionQuality: quality})
                        CGImageDestinationFinalize(dest)
                        with open(SHOT, "rb") as f:
                            data = f.read()
                except Exception:
                    data = None

            if data is None:
                time.sleep(0.1)
                continue

            n += 1
            if n % 200 == 0:
                try:
                    from Quartz import CGImageCreateWithImageInRect, CGRectMake
                    out = {}
                    for label, (sc, q2) in (("full_q35", (1.0, 0.35)),
                                           ("half_q35", (0.5, 0.35)),
                                           ("quarter_q35", (0.25, 0.35)),
                                           ("full_q20", (1.0, 0.20))):
                        g = img
                        if sc != 1.0:
                            g = CGImageCreateWithImageInRect(
                                img, CGRectMake(0, 0, int(iw * sc), int(ih * sc)))
                        tb = time.time()
                        b = NSMutableData.data()
                        dd = CGImageDestinationCreateWithData(b, "public.jpeg", 1, None)
                        CGImageDestinationAddImage(
                            dd, g,
                            {kCGImageDestinationLossyCompressionQuality: q2})
                        CGImageDestinationFinalize(dd)
                        out[label] = [round((time.time() - tb) * 1000, 2), len(b)]
                    bench = out
                except Exception as e:
                    bench = {"error": str(e)[:100]}

            q.put(("f", data, iw, ih, sw, sh, t_capture, t_encode, bench, ms_iter))
            bench = None
        except Exception as e:
            q.put(("err", str(e)[:200]))
            time.sleep(1)
        dt = time.time() - t
        if dt < interval:
            time.sleep(interval - dt)

def capture_subprocess_worker(q, fps):
    """Fallback sin pyobjc: screencapture por proceso."""
    interval = 1.0 / max(1, fps // 4)
    while True:
        run_via_bash("/usr/sbin/screencapture -x -t jpg " + SHOT, timeout=20)
        try:
            with open(SHOT, "rb") as f:
                data = f.read()
            if data:
                q.put(("f", data, 1024, 768, 1024, 768, 0.0, 0.0, None))
        except Exception:
            pass
        time.sleep(interval)


# ---------------------------------------------------------------- servidor

def publish(data, iw, ih, sw, sh, t_capture, t_encode, bench, ms_iter=0.0):
    with LOCK:
        STATE["data"] = data
        STATE["mtime"] = time.time()
        STATE["seq"] += 1
        STATE["iw"], STATE["ih"] = iw, ih
        STATE["sw"], STATE["sh"] = sw, sh
        n = STATE["n"] + 1
        STATE["n"] = n
        a = n - 1
        STATE["t_capture"] = (STATE["t_capture"] * a + t_capture) / n
        STATE["t_encode"] = (STATE["t_encode"] * a + t_encode) / n
        STATE["t_iter"] = (STATE.get("t_iter", 0.0) * a + ms_iter) / n
        if bench:
            STATE["bench"] = bench


def consumer(q):
    """Hilo del padre: consume los frames del hijo y los publica."""
    frames = 0
    t0 = time.time()
    while True:
        try:
            msg = q.get(timeout=1)
        except Exception:
            msg = None
        if msg:
            if msg[0] == "err":
                with LOCK:
                    STATE["backend"] = "error: " + msg[1]
                continue
            if msg[0] == "meta":
                with LOCK:
                    STATE["refresh_hz"] = msg[1]
                    STATE["mode_w"] = msg[2]
                    STATE["mode_h"] = msg[3]
                continue
            _, data, iw, ih, sw, sh, tc, te, bench, msi = msg
            publish(data, iw, ih, sw, sh, tc, te, bench, msi)
            frames += 1
        el = time.time() - t0
        if el >= 1:
            with LOCK:
                STATE["fps"] = frames / el
            frames = 0
            t0 = time.time()


def cliclick(*args):
    if not CLICLICK:
        return
    run_via_bash(" ".join([CLICLICK] + [str(a) for a in args]), timeout=10)


def handle_event(ev):
    kind = ev.get("kind")
    # El navegador envia coordenadas en pixeles de la IMAGEN que ve; las
    # convertimos a pixeles de PANTALLA (pueden diferir si se escalo o si
    # cambio la resolucion).
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
         justify-content:center;overflow:hidden}
  img{max-width:100%;max-height:100%;width:auto;height:auto;display:block;
      object-fit:contain;image-rendering:auto;user-select:none;
      cursor:crosshair;border:1px solid #2b2b33;box-shadow:0 0 30px #000}
  #mk{position:absolute;width:16px;height:16px;margin:-8px 0 0 -8px;
      border:2px solid #ff3b30;border-radius:50%;display:none;
      pointer-events:none;box-shadow:0 0 6px #000}
  #hud{position:fixed;top:8px;left:10px;background:#000b;padding:4px 9px;
       border-radius:6px;font-size:11px;pointer-events:none}
  #bar{position:fixed;bottom:8px;left:10px;background:#000b;padding:5px 11px;
       border-radius:6px;font-size:11px}
</style></head><body>
<div id="stage"><img id="s" src="/stream.mjpeg"><div id="mk"></div></div>
<div id="hud">macOS 26</div>
<div id="bar">raton: clic / arrastrar &nbsp;|&nbsp; teclado: escribe en la pagina &nbsp;|&nbsp; <span id="fps">-</span> fps</div>
<script>
const img=document.getElementById('s'), hud=document.getElementById('hud');
const mk=document.getElementById('mk'), stage=document.getElementById('stage');
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
function mark(e){const sr=stage.getBoundingClientRect();
  if(!sr.width||!sr.height)return;
  mk.style.display='block';
  mk.style.left=(e.clientX-sr.left)+'px';
  mk.style.top=(e.clientY-sr.top)+'px';}
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
    # CRITICO: sin esto, Nagle + delayed ACK anaden decenas de ms por frame
    # (escrituras pequenas de ~30KB) y el stream se queda en ~8 fps.
    disable_nagle_algorithm = True
    tcp_nodelay = True

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
        elif path == "/set":
            try:
                from urllib.parse import urlparse, parse_qs
                qp = parse_qs(urlparse(self.path).query)
                v = max(0.1, min(1.0, float(qp.get("scale", ["1.0"])[0])))
                with open(SCALE_FILE, "w") as f:
                    f.write(str(v))
                self._send(200, json.dumps({"scale": v}), "application/json")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)[:120]}),
                           "application/json")
        elif path == "/info":
            with LOCK:
                fps = STATE["fps"]
                backend = STATE["backend"]
                iw, ih = STATE["iw"], STATE["ih"]
                sw, sh = STATE["sw"], STATE["sh"]
            self._send(200, json.dumps({"width": iw, "height": ih,
                                        "screen_w": sw, "screen_h": sh,
                                        "scale": read_scale(),
                                        "refresh_hz": STATE.get("refresh_hz", 0),
                                        "ms_iter": round(STATE.get("t_iter", 0), 2),
                                        "fps": round(fps, 1), "backend": backend}),
                       "application/json")
        elif path == "/profile":
            with LOCK:
                n = STATE["n"] or 1
                info = {
                    "frames": STATE["n"],
                    "ms_capture": round(STATE["t_capture"], 2),
                    "ms_encode": round(STATE["t_encode"], 2),
                    "ms_total": round(STATE["t_capture"] + STATE["t_encode"], 2),
                    "ms_iter": round(STATE.get("t_iter", 0), 2),
                    "refresh_hz": STATE.get("refresh_hz", 0),
                    "mode": [STATE.get("mode_w", 0), STATE.get("mode_h", 0)],
                    "cpu_count": os.cpu_count(),
                    "fps": round(STATE["fps"], 1),
                    "backend": STATE["backend"],
                    "bench_ms_bytes": STATE.get("bench", {}),
                }
            self._send(200, json.dumps(info), "application/json")
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
    ctx = mp.get_context("spawn")
    q = ctx.Queue(maxsize=3)
    proc = None
    try:
        proc = ctx.Process(target=capture_worker, args=(q, FPS, QUALITY), daemon=True)
        proc.start()
        time.sleep(4)
        if not proc.is_alive():
            proc = None
    except Exception as e:
        proc = None
    if proc is not None:
        with LOCK:
            STATE["backend"] = "coregraphics (proceso hijo)"
        threading.Thread(target=consumer, args=(q,), daemon=True).start()
    else:
        with LOCK:
            STATE["backend"] = "screencapture (fallback)"
        p2 = ctx.Process(target=capture_subprocess_worker, args=(q, FPS), daemon=True)
        p2.start()
        threading.Thread(target=consumer, args=(q,), daemon=True).start()
    time.sleep(3)
    print(f"webdesktop: {STATE['iw']}x{STATE['ih']} target={FPS}fps "
          f"backend={STATE['backend']} cliclick={CLICLICK}", flush=True)
    Server(("127.0.0.1", port), Handler).serve_forever()
