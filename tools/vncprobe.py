#!/usr/bin/env python3
"""
Sonda VNC para el runner de macOS 26.

1) Autentica contra el VNC local (security type 2 = VNC clasico) con la
   contraseña indicada.
2) Pide el framebuffer completo en encoding RAW y comprueba si los pixeles
   son todos negro.

Devuelve codigo de salida 0 si la pantalla tiene contenido, 1 si es negra.
"""
import os
import shutil
import socket
import struct
import subprocess
import sys


def find_openssl():
    p = shutil.which("openssl")
    if p:
        return p
    for c in (
        "/opt/homebrew/opt/openssl@3/bin/openssl",
        "/opt/homebrew/bin/openssl",
        "/usr/local/opt/openssl@3/bin/openssl",
    ):
        if os.path.exists(c):
            return c
    return None


def des_ecb(key_hex, data):
    oss = find_openssl()
    if not oss:
        return None, "openssl no encontrado"
    with open("/tmp/_k.bin", "wb") as f:
        f.write(data)
    base = [oss, "enc", "-des-ecb", "-nopad", "-K", key_hex]
    attempts = [
        base + ["-provider", "legacy", "-provider", "default",
                "-in", "/tmp/_k.bin", "-out", "/tmp/_o.bin"],
        base + ["-in", "/tmp/_k.bin", "-out", "/tmp/_o.bin"],
    ]
    for cmd in attempts:
        if os.path.exists("/tmp/_o.bin"):
            os.remove("/tmp/_o.bin")
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and os.path.exists("/tmp/_o.bin"):
            with open("/tmp/_o.bin", "rb") as f:
                return f.read(), "ok"
        err = r.stderr.decode("utf-8", "replace")
    return None, err[:300]


def recvn(sock, n):
    buf = b""
    while len(buf) < n:
        d = sock.recv(n - len(buf))
        if not d:
            raise EOFError("socket cerrado")
        buf += d
    return buf


def probe(pw, host="127.0.0.1", port=5900):
    key = pw.encode("latin-1")[:8].ljust(8, b"\0")
    s = socket.create_connection((host, port), timeout=20)
    try:
        s.recv(12)
        s.sendall(b"RFB 003.008\n")
        n = recvn(s, 1)[0]
        types = list(recvn(s, n))
        print("  security types:", types)
        if 2 not in types:
            return 2, "el servidor NO ofrece VNC clasico (tipo 2)"
        s.sendall(bytes([2]))
        chal = recvn(s, 16)
        resp, err = des_ecb(key.hex(), chal)
        if resp is None:
            return 2, "no pude cifrar el challenge: " + err
        s.sendall(resp)
        status = struct.unpack(">I", recvn(s, 4))[0]
        if status != 0:
            try:
                ln = struct.unpack(">I", recvn(s, 4))[0]
                reason = recvn(s, ln).decode("utf-8", "replace")
            except Exception:
                reason = "(sin motivo)"
            return 1, "AUTENTICACION RECHAZADA: " + reason
        print("  autenticacion VNC clasico: OK")

        s.sendall(bytes([1]))  # ClientInit, shared
        hdr = recvn(s, 24)
        w = struct.unpack(">H", hdr[0:2])[0]
        h = struct.unpack(">H", hdr[2:4])[0]
        nl = struct.unpack(">I", hdr[20:24])[0]
        name = recvn(s, nl)
        print(f"  framebuffer: {w}x{h}  nombre='{name[:60].decode('utf-8','replace')}'")

        # SetEncodings: solo raw (0)
        s.sendall(struct.pack(">BBH", 2, 0, 1) + struct.pack(">i", 0))
        # SetPixelFormat: 32 bpp, true color, R=16 G=8 B=0
        pf = bytes([32, 24, 0, 1]) + struct.pack(">HHH", 255, 255, 255) + \
            struct.pack(">HHH", 16, 8, 0)
        s.sendall(struct.pack(">Bxxx", 0) + pf)
        # FramebufferUpdateRequest completo
        s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))

        t = recvn(s, 1)[0]
        recvn(s, 1)
        nrects = struct.unpack(">H", recvn(s, 2))[0]
        print(f"  mensaje framebuffer={t}  rectangulos={nrects}")
        nonzero = 0
        total = 0
        canvas = bytearray(w * h)
        for _ in range(nrects):
            rx, ry, rw, rh, enc = struct.unpack(">HHHHi", recvn(s, 12))
            if enc != 0:
                return 3, f"encoding {enc} (no raw), no puedo medir pixeles"
            data = recvn(s, rw * rh * 4)
            total += len(data)
            nonzero += sum(1 for b in data if b)
            # pixel format pedido: 32bpp little-endian, rojo en byte 1
            for i in range(rw * rh):
                r = data[i * 4 + 1]
                g = data[i * 4 + 2]
                b = data[i * 4 + 3]
                px = rx + (i % rw)
                py = ry + (i // rw)
                if 0 <= px < w and 0 <= py < h:
                    canvas[py * w + px] = (r * 299 + g * 587 + b * 114) // 1000
        if total == 0:
            return 3, "no se recibieron pixeles"
        pct = 100.0 * nonzero / total
        print(f"  bytes no nulos: {nonzero}/{total} ({pct:.2f}%)")
        print("  --- render ASCII del framebuffer VNC ---")
        for y in range(0, h, max(1, h // 30)):
            row = []
            for x in range(0, w, max(1, w // 60)):
                v = canvas[y * w + x]
                row.append(" .:-=+*#%@"[min(9, v * 10 // 256)])
            print("  |" + "".join(row) + "|")
        if nonzero == 0:
            return 1, "PANTALLA NEGRA (todos los pixeles a 0)"
        return 0, f"PANTALLA CON CONTENIDO ({pct:.2f}% de bytes no nulos)"
    finally:
        try:
            s.close()
        except Exception:
            pass


def main():
    pws = sys.argv[1:] or ["macos26"]
    rc = 1
    for pw in pws:
        print(f"--- password '{pw}' ---")
        try:
            code, msg = probe(pw)
        except Exception as e:
            code, msg = 3, f"error: {e}"
        print("  ->", msg)
        if code == 0:
            rc = 0
    sys.exit(rc)


if __name__ == "__main__":
    main()
