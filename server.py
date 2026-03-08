"""
SoundBridge — Backend Server (zero external dependencies)
=========================================================
Uses only Python stdlib — no websockets package required.
Implements RFC 6455 WebSocket over asyncio streams.

Serves:
  - Static frontend at  GET  /          (HTTP :8080)
  - WebSocket at        ws://<host>:8765/
  - REST stats at       GET  /api/stats
  - REST token at       GET  /api/token

Run:  python3 server.py
"""

import asyncio
import base64
import hashlib
import json
import math
import random
import string
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ──────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────
WS_HOST       = "0.0.0.0"
WS_PORT       = 8765
HTTP_PORT     = 8080
SAMPLE_RATE   = 44100
CHANNELS      = 1
FRAME_SAMPLES = 2048
CHUNK_MS      = int(FRAME_SAMPLES / SAMPLE_RATE * 1000)   # ~46 ms

STATIC_DIR = Path(__file__).parent / "static"

# ──────────────────────────────────────────────────────────
# Global state  (all access from asyncio loop thread)
# ──────────────────────────────────────────────────────────
session = {
    "active":     False,
    "token":      "",
    "started_at": 0.0,
    "master_vol": 0.75,
    "bitrate":    128,
}
clients: dict[str, dict] = {}   # cid → {writer, name, muted, latency, drift, joined_at}
audio_queue: asyncio.Queue | None = None
seq_counter = 0
_loop: asyncio.AbstractEventLoop | None = None

# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────
def make_token(n=6):
    return "SB-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=n))

# ──────────────────────────────────────────────────────────
# RFC 6455 WebSocket — minimal but complete implementation
# ──────────────────────────────────────────────────────────
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def _ws_accept_key(key: str) -> str:
    raw = hashlib.sha1((key + WS_MAGIC).encode()).digest()
    return base64.b64encode(raw).decode()

async def _ws_handshake(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> bool:
    """Read HTTP upgrade request, send 101. Return True on success."""
    try:
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=10)
            if not chunk:
                return False
            raw += chunk

        headers: dict[str, str] = {}
        lines = raw.decode(errors="replace").split("\r\n")
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        key = headers.get("sec-websocket-key", "")
        if not key:
            return False

        accept = _ws_accept_key(key)
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        writer.write(response.encode())
        await writer.drain()
        return True
    except Exception:
        return False

async def _ws_read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    """Read one WebSocket frame. Returns (opcode, payload) or None on close/error."""
    try:
        header = await reader.readexactly(2)
        fin = (header[0] & 0x80) != 0
        opcode = header[0] & 0x0F
        masked = (header[1] & 0x80) != 0
        length = header[1] & 0x7F

        if length == 126:
            ext = await reader.readexactly(2)
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = await reader.readexactly(8)
            length = struct.unpack(">Q", ext)[0]

        mask_key = b""
        if masked:
            mask_key = await reader.readexactly(4)

        payload = await reader.readexactly(length)
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        # opcode 8 = close, 9 = ping, 10 = pong
        if opcode == 8:
            return None
        if opcode == 9:  # ping → send pong
            return (9, payload)
        return (opcode, payload)
    except Exception:
        return None

async def _ws_send(writer: asyncio.StreamWriter, data: bytes | str,
                   binary: bool = False) -> bool:
    """Send a WebSocket frame (text or binary). Returns False if broken."""
    try:
        if isinstance(data, str):
            payload = data.encode()
            opcode = 0x01   # text
        else:
            payload = data
            opcode = 0x02 if binary else 0x01

        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 126]) + struct.pack(">H", length)
        else:
            header = bytes([0x80 | opcode, 127]) + struct.pack(">Q", length)

        writer.write(header + payload)
        await writer.drain()
        return True
    except Exception:
        return False

# ──────────────────────────────────────────────────────────
# Audio generator (440 Hz sine wave demo)
# ──────────────────────────────────────────────────────────
_phase = 0.0

def _audio_generator(frame_size: int, sample_rate: int):
    global _phase
    amplitude = 16000
    freq = 440.0
    while True:
        samples = []
        for _ in range(frame_size):
            val = int(amplitude * math.sin(2 * math.pi * freq * _phase / sample_rate))
            samples.append(max(-32768, min(32767, val)))
            _phase = (_phase + 1) % sample_rate
        yield struct.pack(f"<{frame_size}h", *samples)

def _audio_thread(queue: asyncio.Queue, loop: asyncio.AbstractEventLoop,
                   frame_size: int, sample_rate: int, stop_event: threading.Event):
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        cable_device = None
        for i, d in enumerate(devices):
            if "CABLE Output" in d['name']:
                cable_device = i
                break
        if cable_device is None:
            cable_device = sd.default.device[0]
        print(f"[Audio] Using device: {sd.query_devices(cable_device)['name']}")

        def callback(indata, frames, time_info, status):
            if stop_event.is_set():
                raise sd.CallbackStop()
            pcm = (indata[:, 0] * 32767).astype('int16')
            try:
                loop.call_soon_threadsafe(queue.put_nowait, pcm.tobytes())
            except Exception:
                pass

        with sd.InputStream(device=cable_device, channels=1,
                            samplerate=sample_rate, blocksize=frame_size,
                            dtype='float32', callback=callback):
            while not stop_event.is_set():
                time.sleep(0.05)
    except Exception as e:
        print(f"[Audio] Falling back to demo tone ({e})")
        gen = _audio_generator(frame_size, sample_rate)
        interval = frame_size / sample_rate
        while not stop_event.is_set():
            raw = next(gen)
            loop.call_soon_threadsafe(queue.put_nowait, raw)
            time.sleep(interval)

# ──────────────────────────────────────────────────────────
# Audio broadcaster
# ──────────────────────────────────────────────────────────
async def broadcast_audio():
    global seq_counter
    while True:
        raw = await audio_queue.get()
        if not session["active"] or not clients:
            continue

        seq_counter += 1
        ts = time.time_ns()

        vol = session["master_vol"]
        if vol != 1.0:
            samples = list(struct.unpack(f"<{len(raw)//2}h", raw))
            samples = [max(-32768, min(32767, int(s * vol))) for s in samples]
            raw = struct.pack(f"<{len(samples)}h", *samples)

        # Packet: 0x01 | seq(4B BE) | ts_ns(8B BE) | PCM int16 LE
        header = struct.pack(">IQ", seq_counter & 0xFFFFFFFF, ts)
        packet = b"\x01" + header + raw

        dead = []
        for cid, info in list(clients.items()):
            if info.get("muted"):
                continue
            ok = await _ws_send(info["writer"], packet, binary=True)
            if not ok:
                dead.append(cid)

        for cid in dead:
            clients.pop(cid, None)
            print(f"[WS] Client {cid} removed (send error)")

# ──────────────────────────────────────────────────────────
# JSON broadcast helper
# ──────────────────────────────────────────────────────────
async def broadcast_json(msg: dict, exclude: str | None = None):
    data = json.dumps(msg)
    for cid, info in list(clients.items()):
        if cid == exclude:
            continue
        await _ws_send(info["writer"], data)

# ──────────────────────────────────────────────────────────
# WebSocket connection handler
# ──────────────────────────────────────────────────────────
async def ws_connection(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername", "?")
    print(f"[WS] New TCP connection from {peer}")

    if not await _ws_handshake(reader, writer):
        writer.close()
        return

    cid = str(id(writer))
    print(f"[WS] Handshake OK — cid={cid}")

    try:
        while True:
            frame = await _ws_read_frame(reader)
            if frame is None:
                break
            opcode, payload = frame

            if opcode == 9:  # ping → pong
                header_pong = bytes([0x80 | 0x0A, len(payload)])
                writer.write(header_pong + payload)
                await writer.drain()
                continue

            if opcode == 0x02:
                # binary from client — ignored for now
                continue

            if opcode == 0x01:
                try:
                    msg = json.loads(payload.decode())
                except Exception:
                    continue
                await handle_ctrl(cid, writer, msg)

    except Exception:
        pass
    finally:
        if cid in clients:
            name = clients[cid].get("name", cid)
            del clients[cid]
            print(f"[WS] {name} disconnected")
            await broadcast_json({"type": "device_left", "id": cid, "name": name})
        try:
            writer.close()
        except Exception:
            pass

# ──────────────────────────────────────────────────────────
# Control message handler
# ──────────────────────────────────────────────────────────
async def handle_ctrl(cid: str, writer: asyncio.StreamWriter, msg: dict):
    t = msg.get("type")

    async def send(obj):
        await _ws_send(writer, json.dumps(obj))

    if t == "join":
        token = msg.get("token", "")
        if not session["active"]:
            await send({"type": "error", "msg": "No active session"})
            return
        if token != session["token"]:
            await send({"type": "error", "msg": "Invalid session token"})
            return
        name = msg.get("name", f"Phone-{cid[-6:]}")
        clients[cid] = {
            "writer":    writer,
            "name":      name,
            "muted":     False,
            "latency":   0,
            "drift":     0,
            "joined_at": time.time(),
        }
        print(f"[WS] {name} joined")
        await send({
            "type":         "joined",
            "token":        session["token"],
            "server_time":  time.time_ns(),
            "sample_rate":  SAMPLE_RATE,
            "channels":     CHANNELS,
            "frame_samples": FRAME_SAMPLES,
        })
        await broadcast_json({"type": "device_joined", "id": cid, "name": name},
                             exclude=cid)

    elif t == "ping":
        await send({
            "type":        "pong",
            "client_time": msg.get("client_time"),
            "server_time": time.time_ns(),
        })

    elif t == "stats":
        if cid in clients:
            clients[cid]["latency"] = msg.get("latency", 0)
            clients[cid]["drift"]   = msg.get("drift", 0)

    elif t == "start_session":
        session["active"]     = True
        session["token"]      = make_token()
        session["started_at"] = time.time()
        print(f"[Session] Started — token={session['token']}")
        await send({
            "type":        "session_started",
            "token":       session["token"],
            "server_time": time.time_ns(),
        })

    elif t == "stop_session":
        session["active"] = False
        clients.clear()
        print("[Session] Stopped")
        await send({"type": "session_stopped"})

    elif t == "set_master_vol":
        session["master_vol"] = max(0.0, min(1.0, float(msg.get("vol", 1.0))))
        await broadcast_json({"type": "master_vol", "vol": session["master_vol"]})

    elif t == "mute_device":
        target = str(msg.get("id", ""))
        if target in clients:
            clients[target]["muted"] = bool(msg.get("muted", True))

    elif t == "kick_device":
        target = str(msg.get("id", ""))
        if target in clients:
            try:
                clients[target]["writer"].close()
            except Exception:
                pass
            clients.pop(target, None)

    elif t == "get_state":
        await send({
            "type": "state",
            "session": {
                "active":     session["active"],
                "token":      session["token"],
                "uptime":     int(time.time() - session["started_at"]) if session["active"] else 0,
                "master_vol": session["master_vol"],
                "bitrate":    session["bitrate"],
            },
            "devices": [
                {
                    "id":      cid2,
                    "name":    info["name"],
                    "muted":   info["muted"],
                    "latency": info["latency"],
                    "drift":   info["drift"],
                }
                for cid2, info in clients.items()
            ],
        })

# ──────────────────────────────────────────────────────────
# HTTP server
# ──────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass   # suppress access log

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/api/stats", "/api/token"):
            if path == "/api/stats":
                body = json.dumps({
                    "session": {
                        "active":     session["active"],
                        "token":      session["token"],
                        "uptime":     int(time.time() - session["started_at"]) if session["active"] else 0,
                        "master_vol": session["master_vol"],
                        "bitrate":    session["bitrate"],
                    },
                    "device_count": len(clients),
                    "devices": [
                        {"id": cid, "name": i["name"], "muted": i["muted"],
                         "latency": i["latency"], "drift": i["drift"]}
                        for cid, i in clients.items()
                    ],
                }).encode()
            else:
                body = json.dumps({
                    "token":  session["token"],
                    "active": session["active"],
                }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Static files
        if path in ("/", ""):
            path = "/index.html"
        fp = STATIC_DIR / path.lstrip("/")
        if fp.exists() and fp.is_file():
            ext = fp.suffix.lower()
            ctype = {".html": "text/html", ".js": "application/javascript",
                     ".css": "text/css", ".json": "application/json",
                     ".ico": "image/x-icon"}.get(ext, "application/octet-stream")
            body = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

def _run_http():
    srv = HTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    print(f"[HTTP] http://localhost:{HTTP_PORT}")
    srv.serve_forever()

# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────
async def main():
    global audio_queue, _loop
    _loop      = asyncio.get_running_loop()
    audio_queue = asyncio.Queue(maxsize=500)

    # HTTP server in background thread
    threading.Thread(target=_run_http, daemon=True).start()

    # Audio capture thread
    stop_event = threading.Event()
    threading.Thread(
        target=_audio_thread,
        args=(audio_queue, _loop, FRAME_SAMPLES, SAMPLE_RATE, stop_event),
        daemon=True,
    ).start()

    # Auto-start session
    session["active"]     = True
    session["token"]      = make_token()
    session["started_at"] = time.time()
    print(f"[Session] Auto-started — token = {session['token']}")

    # Audio broadcaster task
    asyncio.create_task(broadcast_audio())

    # WebSocket server (pure asyncio — no external library)
    ws_server = await asyncio.start_server(ws_connection, WS_HOST, WS_PORT)
    print(f"[WS]   ws://localhost:{WS_PORT}")
    print(f"[HTTP] http://localhost:{HTTP_PORT}")
    print("Press Ctrl+C to stop.\n")

    async with ws_server:
        await ws_server.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Server] Stopped.")
