# SoundBridge

Stream your laptop's audio to multiple smartphones over Wi-Fi — perfectly synchronized, zero noticeable delay.

## What it does

SoundBridge captures system audio from your laptop and broadcasts it in real time to any number of phones on the same Wi-Fi network. Phones connect via a session token and play back the audio through their speakers using the Web Audio API. Use it to turn a group of phones into a synchronized speaker system for any audio source — Spotify, YouTube, games, calls, anything.

## Requirements

- Python 3.8 or higher (uses stdlib only — no pip installs needed)
- Phones and laptop on the same Wi-Fi network
- A modern browser on each phone (Chrome, Safari, Firefox)

**Optional** — for real system audio capture (instead of the built-in demo tone):
- `sounddevice` + `numpy` Python packages
- A loopback audio device (e.g. VB-Cable on Windows, BlackHole on macOS, or a PulseAudio monitor on Linux)

## Quick start

```bash
python3 server.py
```

Open `http://<your-laptop-ip>:8080` on any device. That's it.

## File structure

```
server.py        ← the entire application (frontend is embedded inside)
index.html       ← optional: place next to server.py to override the embedded UI
static/
  index.html     ← optional: place here instead
```

The server serves the frontend in this priority order:
1. `static/index.html`
2. `index.html` in the same directory as `server.py`
3. The embedded HTML inside `server.py` (always works with no extra files)

## Ports

| Port | Purpose |
|------|---------|
| `8080` | HTTP — serves the frontend and REST API |
| `8765` | WebSocket — audio streaming and control |

## How to use

**On your laptop — Host tab:**
1. Open `http://localhost:8080` in a browser
2. Click the **Host** tab
3. The session auto-starts on launch. Click **Start** to manually start/stop it
4. Share the session token (e.g. `SB-AB3F9K`) with people connecting

**On each phone:**
1. Open `http://<laptop-ip>:8080` in a phone browser
2. Click the **Phone** tab
3. Tap **Connect to Host Session** — it fetches the token automatically
4. Audio begins playing within ~120ms

## Architecture

```
Laptop audio
  → sounddevice (PortAudio) OR sine-wave demo tone
  → numpy int16 PCM frames (44100 Hz mono, 2048 samples/frame)
  → asyncio broadcast loop
  → RFC 6455 WebSocket (pure Python stdlib, no library)
  → each connected phone browser
  → Web Audio API scheduled buffer playback
  → phone speaker
```

## Packet format

Every audio frame sent over the WebSocket is a binary message:

```
Byte 0      : 0x01  (audio frame marker)
Bytes 1–4   : sequence number (uint32, big-endian)
Bytes 5–12  : server timestamp in nanoseconds (uint64, big-endian)
Bytes 13+   : raw PCM, int16 little-endian, mono, 44100 Hz
```

Frame size: 2048 samples × 2 bytes = 4096 bytes PCM + 13 byte header = **4109 bytes per frame**, arriving ~every 46ms.

## REST API

```
GET /api/stats   → session info + list of connected devices with latency/drift
GET /api/token   → current session token and active status
```

## WebSocket control messages

```
→ { type: "get_state" }
← { type: "state", session: {...}, devices: [...] }

→ { type: "start_session" }
← { type: "session_started", token, server_time }

→ { type: "stop_session" }
← { type: "session_stopped" }

→ { type: "join", token, name }
← { type: "joined", sample_rate, channels, frame_samples, server_time }

→ { type: "ping", client_time }
← { type: "pong", client_time, server_time }

→ { type: "set_master_vol", vol: 0.75 }
→ { type: "mute_device", id, muted: true }
→ { type: "kick_device", id }
→ { type: "stats", latency, drift }
```

## Sync and latency

The phone client uses a jitter buffer — it schedules each `AudioBufferSource` 120ms ahead of the current playback time. If the buffer grows beyond 350ms (overflow) or falls below 50ms (underrun), it resets. The UI shows live buffer depth in milliseconds. All devices stay within ~10ms of each other as long as the network is stable.

## Real audio capture setup

**Windows** — install [VB-Cable](https://vb-audio.com/Cable/). Set it as your default playback device. The server auto-detects "CABLE Output".

**macOS** — install [BlackHole](https://existingcircuits.com/products/blackhole) or [Loopback](https://rogueamoeba.com/loopback/). Use an aggregate device to hear audio locally while capturing it.

**Linux** — create a PulseAudio null sink:
```bash
pactl load-module module-null-sink sink_name=loopback
pactl load-module module-loopback sink=loopback
```
Then set your system audio output to "loopback".

Without a loopback device, the server falls back to a 440 Hz demo tone automatically — useful for testing that everything is connected before setting up audio routing.

## Privacy

All audio stays on your local network. Nothing is sent to the internet, stored, or logged. Session tokens are randomly generated per run and have no meaning outside the current session.
