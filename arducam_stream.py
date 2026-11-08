#!/usr/bin/env python3
"""
arducam_stream.py - Stream a USB (UVC) Arducam over the network, with
remote image tuning and snapshot download, all from a web browser.

    python3 arducam_stream.py --camera 0
    python3 arducam_stream.py --camera /dev/video2 --width 1920 --height 1080 --port 8080

Then open http://<this-machine-ip>:8080/ on the viewing machine.

Camera controls (brightness, contrast, exposure, white balance, gain, ...)
are discovered from the camera itself with `v4l2-ctl` (package v4l-utils),
the same tool Arducam's Linux quick-start guide uses, so every control the
camera exposes shows up in the web UI with its real range.
"""

import argparse
import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import cv2
    import numpy as np
except ImportError:
    sys.exit("OpenCV is required:  pip install opencv-python numpy")


# --------------------------------------------------------------------------
# Camera capture
# --------------------------------------------------------------------------

def resolve_device(source):
    """Turn '0', '/dev/video0' or a /dev/v4l/by-id/... link into a device path."""
    if source.isdigit():
        return f"/dev/video{source}"
    return os.path.realpath(source)


def largest_mode(device, fourcc="MJPG"):
    """Ask v4l2-ctl for the biggest frame size the camera supports in `fourcc`.

    Returns (width, height) or None. Used when --width/--height are not given,
    so any UVC camera opens at its full resolution without extra flags.
    """
    if not shutil.which("v4l2-ctl"):
        return None
    out = subprocess.run(["v4l2-ctl", "-d", device, "--list-formats-ext"],
                         capture_output=True, text=True, timeout=5).stdout
    return parse_largest_mode(out, fourcc)


def parse_largest_mode(text, fourcc="MJPG"):
    best, in_fmt = None, False
    for line in text.splitlines():
        m = re.search(r"\[\d+\]:\s*'(\w+)'", line)       # e.g.  [0]: 'MJPG' (Motion-JPEG...)
        if m:
            in_fmt = m.group(1).upper() == fourcc.upper()
            continue
        m = re.search(r"Size:\s*Discrete\s+(\d+)x(\d+)", line)
        if m and in_fmt:
            w, h = int(m.group(1)), int(m.group(2))
            if best is None or w * h > best[0] * best[1]:
                best = (w, h)
    return best


class Camera:
    """Reads frames continuously in a background thread; keeps the newest one."""

    def __init__(self, device, width=None, height=None, fps=None, fourcc="MJPG"):
        self.device = device
        self.req = dict(width=width, height=height, fps=fps, fourcc=fourcc)
        self.cond = threading.Condition()
        self.frame = None
        self.frame_id = 0
        self._jpeg_cache = (-1, None)
        self._jpeg_lock = threading.Lock()
        self.running = True
        self.cap = None
        self._open()
        threading.Thread(target=self._loop, daemon=True).start()

    def _open(self):
        if self.cap is not None:
            self.cap.release()
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise RuntimeError(f"Could not open camera {self.device}")
        r = self.req
        if r["fourcc"]:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*r["fourcc"]))
        if r["width"]:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, r["width"])
        if r["height"]:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, r["height"])
        if r["fps"]:
            cap.set(cv2.CAP_PROP_FPS, r["fps"])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.cap = cap

    def info(self):
        c = self.cap
        fcc = int(c.get(cv2.CAP_PROP_FOURCC))
        return {
            "device": self.device,
            "width": int(c.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(c.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": round(c.get(cv2.CAP_PROP_FPS), 2),
            "fourcc": "".join(chr((fcc >> 8 * i) & 0xFF) for i in range(4)) if fcc else "?",
        }

    def _loop(self):
        failures = 0
        while self.running:
            ok, frame = self.cap.read()
            if not ok:
                failures += 1
                if failures >= 30:  # camera unplugged / hung: try to reopen
                    print("Camera read failing, reopening...", file=sys.stderr)
                    time.sleep(1)
                    try:
                        self._open()
                    except RuntimeError as e:
                        print(e, file=sys.stderr)
                    failures = 0
                time.sleep(0.02)
                continue
            failures = 0
            with self.cond:
                self.frame = frame
                self.frame_id += 1
                self.cond.notify_all()

    def wait_frame(self, after_id=-1, timeout=5.0):
        """Return (frame, id) for the first frame newer than after_id."""
        with self.cond:
            self.cond.wait_for(lambda: self.frame_id > after_id, timeout)
            if self.frame_id <= after_id:
                return None, after_id
            return self.frame, self.frame_id

    def wait_jpeg(self, after_id, quality, timeout=5.0):
        """JPEG of the next new frame, encoded once and shared by all viewers."""
        frame, fid = self.wait_frame(after_id, timeout)
        if frame is None:
            return None, after_id
        with self._jpeg_lock:
            if self._jpeg_cache[0] != fid:
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
                self._jpeg_cache = (fid, buf.tobytes() if ok else None)
            return self._jpeg_cache[1], fid

    def close(self):
        self.running = False
        if self.cap is not None:
            self.cap.release()


class TestPatternCamera(Camera):
    """Synthetic camera (--camera test) so the server can be tried without hardware."""

    def __init__(self, controls, width=1280, height=720, fps=30, **_):
        self.device = "test-pattern"
        self.controls = controls
        self.w, self.h, self.fps = width or 1280, height or 720, fps or 30
        self.cond = threading.Condition()
        self.frame, self.frame_id = None, 0
        self._jpeg_cache, self._jpeg_lock = (-1, None), threading.Lock()
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def info(self):
        return {"device": self.device, "width": self.w, "height": self.h,
                "fps": self.fps, "fourcc": "TEST"}

    def _loop(self):
        x = np.linspace(0, 255, self.w, dtype=np.float32)
        y = np.linspace(0, 255, self.h, dtype=np.float32)[:, None]
        while self.running:
            t = time.time()
            img = np.dstack([np.broadcast_to(x, (self.h, self.w)),
                             np.broadcast_to(y, (self.h, self.w)),
                             np.full((self.h, self.w), 128 + 100 * np.sin(t), np.float32)])
            v = self.controls.values
            img = (img - 128) * (v["contrast"] / 32) + 128 + v["brightness"] * 2
            img = np.clip(img, 0, 255).astype(np.uint8)
            cv2.putText(img, dt.datetime.now().strftime("%H:%M:%S.%f")[:-3],
                        (40, 80), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
            with self.cond:
                self.frame, self.frame_id = img, self.frame_id + 1
                self.cond.notify_all()
            time.sleep(1 / self.fps)

    def close(self):
        self.running = False


# --------------------------------------------------------------------------
# Camera controls via v4l2-ctl
# --------------------------------------------------------------------------

CTRL_RE = re.compile(r"^\s*([a-z0-9_]+)\s+(?:0x[0-9a-f]+\s+)?\((\w+)\)\s*:\s*(.*)$")
MENU_RE = re.compile(r"^\s*(-?\d+):\s+(.+?)\s*$")
SKIP_TYPES = {"ctrl_class", "string"}


def parse_v4l2_ctrls(text):
    """Parse `v4l2-ctl --list-ctrls-menus` output into a list of dicts."""
    ctrls, current = [], None
    for line in text.splitlines():
        m = CTRL_RE.match(line)
        if m:
            name, ctype, rest = m.groups()
            if ctype in SKIP_TYPES:
                current = None
                continue
            fields = dict(re.findall(r"(\w+)=(\S+)", rest))
            flags = fields.pop("flags", "")
            current = {"name": name, "type": ctype,
                       "flags": [f for f in flags.split(",") if f]}
            for k, v in fields.items():
                try:
                    current[k] = int(v)
                except ValueError:
                    current[k] = v
            if ctype in ("menu", "intmenu"):
                current["menu"] = []
            ctrls.append(current)
            continue
        m = MENU_RE.match(line)
        if m and current is not None and "menu" in current:
            current["menu"].append({"value": int(m.group(1)), "label": m.group(2)})
    return ctrls


def settable(c):
    return c["type"] != "button" and not ({"read-only", "disabled"} & set(c["flags"]))


def apply_order(c):
    # Apply auto/mode switches (bool, menu) before numeric values, so e.g.
    # auto_exposure=Manual is in effect before exposure_time_absolute is set.
    return 0 if c["type"] in ("bool", "menu", "intmenu") else 1


class V4L2Controls:
    def __init__(self, device):
        self.device = device
        self.lock = threading.Lock()
        if not shutil.which("v4l2-ctl"):
            print("WARNING: v4l2-ctl not found; image tuning disabled. "
                  "Install it with:  sudo apt install v4l-utils", file=sys.stderr)
            self.available = False
        else:
            self.available = True

    def list(self):
        if not self.available:
            return []
        out = subprocess.run(["v4l2-ctl", "-d", self.device, "--list-ctrls-menus"],
                             capture_output=True, text=True, timeout=5)
        return parse_v4l2_ctrls(out.stdout)

    def set(self, name, value):
        """Set one control. Returns an error string, or None on success."""
        if not self.available:
            return "v4l2-ctl not installed"
        known = {c["name"]: c for c in self.list()}
        if name not in known:
            return f"unknown control {name!r}"
        try:
            value = int(value)
        except (TypeError, ValueError):
            return f"value for {name} must be an integer"
        with self.lock:
            r = subprocess.run(["v4l2-ctl", "-d", self.device, "-c", f"{name}={value}"],
                               capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return (r.stderr or r.stdout).strip() or f"failed to set {name}"
        return None

    def apply(self, settings):
        """Apply a {name: value} dict; returns list of errors."""
        known = {c["name"]: c for c in self.list()}
        items = [(k, v) for k, v in settings.items() if k in known and settable(known[k])]
        items.sort(key=lambda kv: apply_order(known[kv[0]]))
        errors = []
        for k, v in items:
            err = self.set(k, v)
            if err:
                errors.append(f"{k}: {err}")
        return errors

    def snapshot_settings(self):
        return {c["name"]: c["value"] for c in self.list()
                if settable(c) and "value" in c}

    def reset_defaults(self):
        return self.apply({c["name"]: c["default"] for c in self.list()
                           if settable(c) and "default" in c})


class FakeControls(V4L2Controls):
    """Simulated controls for --camera test."""

    def __init__(self):
        self.available = True
        self.lock = threading.Lock()
        self.values = {"brightness": 0, "contrast": 32, "exposure_auto": 1,
                       "exposure": 156, "power_line_frequency": 1}

    def list(self):
        v = self.values
        return [
            {"name": "brightness", "type": "int", "min": -64, "max": 64, "step": 1,
             "default": 0, "value": v["brightness"], "flags": []},
            {"name": "contrast", "type": "int", "min": 0, "max": 64, "step": 1,
             "default": 32, "value": v["contrast"], "flags": []},
            {"name": "exposure_auto", "type": "bool", "default": 1,
             "value": v["exposure_auto"], "flags": []},
            {"name": "exposure", "type": "int", "min": 1, "max": 5000, "step": 1,
             "default": 156, "value": v["exposure"],
             "flags": ["inactive"] if v["exposure_auto"] else []},
            {"name": "power_line_frequency", "type": "menu", "min": 0, "max": 2,
             "default": 1, "value": v["power_line_frequency"], "flags": [],
             "menu": [{"value": 0, "label": "Disabled"}, {"value": 1, "label": "50 Hz"},
                      {"value": 2, "label": "60 Hz"}]},
        ]

    def set(self, name, value):
        if name not in self.values:
            return f"unknown control {name!r}"
        self.values[name] = int(value)
        return None


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

def make_handler(cam, controls, args):
    auth_token = (base64.b64encode(args.auth.encode()).decode() if args.auth else None)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            if args.verbose:
                super().log_message(fmt, *a)

        # -- helpers --
        def _authorized(self):
            if not auth_token:
                return True
            if self.headers.get("Authorization", "") == f"Basic {auth_token}":
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="arducam"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _send(self, code, body, ctype="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json_body(self):
            n = int(self.headers.get("Content-Length", 0))
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return None

        def _state(self, errors=None):
            return {"camera": cam.info(), "controls": controls.list(),
                    "tuning": controls.available, "errors": errors or []}

        # -- routes --
        def do_GET(self):
            if not self._authorized():
                return
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path == "/":
                self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            elif url.path == "/stream":
                self._stream()
            elif url.path == "/api/state":
                self._send(200, self._state())
            elif url.path == "/api/snapshot":
                self._snapshot(q.get("format", ["png"])[0].lower())
            elif url.path == "/api/settings":
                stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                self._send(200, json.dumps(controls.snapshot_settings(), indent=2),
                           extra={"Content-Disposition":
                                  f'attachment; filename="camera_settings_{stamp}.json"'})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            path = urlparse(self.path).path
            body = self._json_body()
            if body is None:
                return self._send(400, {"error": "invalid JSON"})
            if path == "/api/control":
                err = controls.set(body.get("name"), body.get("value"))
                self._send(200 if not err else 400, self._state([err] if err else None))
            elif path == "/api/settings":
                self._send(200, self._state(controls.apply(body)))
            elif path == "/api/reset":
                self._send(200, self._state(controls.reset_defaults()))
            else:
                self._send(404, {"error": "not found"})

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            last_id, min_dt, last_t = -1, 1.0 / args.stream_fps if args.stream_fps else 0, 0
            try:
                while True:
                    jpg, last_id = cam.wait_jpeg(last_id, args.stream_quality)
                    if jpg is None:
                        continue
                    now = time.monotonic()
                    if now - last_t < min_dt:
                        continue
                    last_t = now
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: %d\r\n\r\n" % len(jpg))
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def _snapshot(self, fmt):
            if fmt not in ("png", "jpg", "jpeg", "bmp", "tiff"):
                return self._send(400, {"error": "format must be png, jpg, bmp or tiff"})
            # Wait for a frame captured *after* the request, so the latest
            # settings are guaranteed to be applied to the picture.
            _, cur = cam.wait_frame(-1)
            frame, _ = cam.wait_frame(cur)
            if frame is None:
                return self._send(503, {"error": "no frame from camera"})
            ext = "jpg" if fmt == "jpeg" else fmt
            params = [cv2.IMWRITE_JPEG_QUALITY, 98] if ext == "jpg" else []
            ok, buf = cv2.imencode("." + ext, frame, params)
            if not ok:
                return self._send(500, {"error": "encode failed"})
            data = buf.tobytes()
            name = f"snapshot_{dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]}.{ext}"
            if args.save_dir:
                os.makedirs(args.save_dir, exist_ok=True)
                with open(os.path.join(args.save_dir, name), "wb") as f:
                    f.write(data)
            ctype = {"jpg": "image/jpeg", "png": "image/png",
                     "bmp": "image/bmp", "tiff": "image/tiff"}[ext]
            self._send(200, data, ctype,
                       {"Content-Disposition": f'attachment; filename="{name}"'})

    return Handler


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Arducam Stream</title>
<style>
  :root { --bg:#16181d; --panel:#21242b; --line:#333844; --text:#e6e8ee; --dim:#9097a6; --acc:#4c9aff; --err:#ff6b6b; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px system-ui, sans-serif; background:var(--bg); color:var(--text); }
  header { padding:10px 16px; border-bottom:1px solid var(--line); display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
  header h1 { font-size:16px; margin:0; }
  #info { color:var(--dim); font-size:12px; }
  main { display:flex; gap:16px; padding:16px; flex-wrap:wrap; }
  #view { flex:1 1 640px; min-width:0; }
  #view img { width:100%; background:#000; border-radius:6px; display:block; }
  #side { flex:0 1 380px; width:380px; max-width:100%; background:var(--panel); border-radius:6px; padding:12px; align-self:flex-start; }
  .bar { display:flex; gap:8px; flex-wrap:wrap; margin:10px 0; }
  button, select { background:#2d313b; color:var(--text); border:1px solid var(--line); border-radius:4px; padding:6px 10px; font:inherit; cursor:pointer; }
  button.primary { background:var(--acc); border-color:var(--acc); color:#fff; }
  button:hover { filter:brightness(1.15); }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em; color:var(--dim); margin:4px 0 10px; }
  .ctrl { padding:7px 0; border-bottom:1px solid var(--line); }
  .ctrl.inactive { opacity:.45; }
  .ctrl label { display:flex; justify-content:space-between; gap:8px; margin-bottom:4px; }
  .ctrl .name { font-weight:500; }
  .ctrl .def { color:var(--dim); font-size:11px; cursor:pointer; }
  .row { display:flex; gap:8px; align-items:center; }
  .row input[type=range] { flex:1; }
  .row input[type=number] { width:80px; background:#2d313b; color:var(--text); border:1px solid var(--line); border-radius:4px; padding:3px; }
  #msg { color:var(--err); font-size:12px; min-height:1em; white-space:pre-wrap; }
</style></head>
<body>
<header><h1>Arducam Stream</h1><span id="info"></span></header>
<main>
  <section id="view">
    <img id="feed" src="/stream" alt="camera feed">
    <div class="bar">
      <button class="primary" onclick="snap()">Take picture</button>
      <select id="fmt"><option value="png">PNG (lossless)</option><option value="jpg">JPEG</option>
        <option value="tiff">TIFF</option><option value="bmp">BMP</option></select>
      <button onclick="reloadFeed()">Reconnect feed</button>
    </div>
  </section>
  <aside id="side">
    <h2>Image controls</h2>
    <div class="bar">
      <button onclick="location.href='/api/settings'">Save settings</button>
      <button onclick="document.getElementById('file').click()">Load settings</button>
      <button onclick="post('/api/reset', {})">Reset defaults</button>
      <input type="file" id="file" accept=".json" hidden onchange="loadFile(this)">
    </div>
    <div id="msg"></div>
    <div id="controls"></div>
  </aside>
</main>
<script>
const $ = id => document.getElementById(id);
const pretty = n => n.replace(/_/g, ' ');
let builtFor = '', timers = {};

function msg(errors) { $('msg').textContent = (errors || []).join('\n'); }

async function post(url, body) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  render(await r.json());
}

function setCtrl(name, value) {
  clearTimeout(timers[name]);
  timers[name] = setTimeout(() => post('/api/control', {name, value: Number(value)}), 60);
}

function widget(c) {
  const d = document.createElement('div');
  d.className = 'ctrl'; d.id = 'c_' + c.name;
  const lab = `<label><span class="name">${pretty(c.name)}</span>` +
    (c.default !== undefined ? `<span class="def" title="click to restore default">default ${c.default}</span>` : '') + `</label>`;
  if (c.type === 'bool') {
    d.innerHTML = `<label><span class="name">${pretty(c.name)}</span><input type="checkbox"></label>`;
    d.querySelector('input').onchange = e => setCtrl(c.name, e.target.checked ? 1 : 0);
  } else if (c.type === 'menu' || c.type === 'intmenu') {
    d.innerHTML = lab + `<select style="width:100%">` +
      c.menu.map(m => `<option value="${m.value}">${m.label}</option>`).join('') + `</select>`;
    d.querySelector('select').onchange = e => setCtrl(c.name, e.target.value);
  } else if (c.type === 'button') {
    d.innerHTML = `<button style="width:100%">${pretty(c.name)}</button>`;
    d.querySelector('button').onclick = () => setCtrl(c.name, 1);
  } else {
    const rng = c.min !== undefined && c.max !== undefined;
    d.innerHTML = lab + `<div class="row">` +
      (rng ? `<input type="range" min="${c.min}" max="${c.max}" step="${c.step || 1}">` : '') +
      `<input type="number" ${rng ? `min="${c.min}" max="${c.max}" step="${c.step || 1}"` : ''}></div>`;
    const r = d.querySelector('input[type=range]'), n = d.querySelector('input[type=number]');
    if (r) r.oninput = () => { n.value = r.value; setCtrl(c.name, r.value); };
    n.onchange = () => { if (r) r.value = n.value; setCtrl(c.name, n.value); };
  }
  const def = d.querySelector('.def');
  if (def) def.onclick = () => setCtrl(c.name, c.default);
  return d;
}

function render(s) {
  const cam = s.camera;
  $('info').textContent = `${cam.device} · ${cam.width}×${cam.height} @ ${cam.fps} fps · ${cam.fourcc}`;
  msg(s.errors);
  const box = $('controls');
  if (!s.tuning) { box.textContent = 'v4l2-ctl not installed on the camera host (sudo apt install v4l-utils).'; return; }
  const key = s.controls.map(c => c.name).join(',');
  if (key !== builtFor) { box.innerHTML = ''; s.controls.forEach(c => box.appendChild(widget(c))); builtFor = key; }
  for (const c of s.controls) {
    const d = $('c_' + c.name); if (!d) continue;
    const inactive = c.flags.includes('inactive') || c.flags.includes('read-only');
    d.classList.toggle('inactive', inactive);
    d.title = inactive ? 'Inactive: usually controlled by an "auto" setting above' : '';
    for (const el of d.querySelectorAll('input,select')) {
      if (el === document.activeElement) continue;   // don't fight the user's drag
      if (el.type === 'checkbox') el.checked = !!c.value; else if (c.value !== undefined) el.value = c.value;
    }
  }
}

function snap() {
  const a = document.createElement('a');
  a.href = '/api/snapshot?format=' + $('fmt').value + '&t=' + Date.now();
  document.body.appendChild(a); a.click(); a.remove();
}
function reloadFeed() { $('feed').src = '/stream?t=' + Date.now(); }
async function loadFile(inp) {
  const f = inp.files[0]; if (!f) return;
  try { await post('/api/settings', JSON.parse(await f.text())); } catch (e) { msg(['Bad settings file: ' + e]); }
  inp.value = '';
}
$('feed').onerror = () => setTimeout(reloadFeed, 2000);
fetch('/api/state').then(r => r.json()).then(render);
setInterval(() => fetch('/api/state').then(r => r.json()).then(render).catch(() => {}), 5000);
</script>
</body></html>
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Stream a USB Arducam over the network with remote tuning and snapshots.")
    p.add_argument("-c", "--camera", default=None,
                   help="camera index (0), device path (/dev/video0, /dev/v4l/by-id/...), "
                        "or 'test' for a synthetic test pattern")
    p.add_argument("--list", action="store_true", help="list cameras and exit")
    p.add_argument("--list-formats", action="store_true",
                   help="list resolutions/frame rates supported by --camera and exit")
    p.add_argument("--host", default="0.0.0.0", help="address to listen on (default all interfaces)")
    p.add_argument("-p", "--port", type=int, default=8080, help="port to serve on (default 8080)")
    p.add_argument("--width", type=int,
                   help="capture width (default: the largest size the camera offers)")
    p.add_argument("--height", type=int,
                   help="capture height (default: the largest size the camera offers)")
    p.add_argument("--fps", type=int, help="capture frame rate")
    p.add_argument("--fourcc", default="MJPG",
                   help="pixel format to request: MJPG (default, needed for high res/fps) or YUYV")
    p.add_argument("--stream-quality", type=int, default=80,
                   help="JPEG quality of the live preview, 1-100 (snapshots are always full quality)")
    p.add_argument("--stream-fps", type=float, default=0,
                   help="cap the live preview frame rate to save bandwidth (0 = no cap)")
    p.add_argument("--settings", help="JSON settings file to apply to the camera at start-up")
    p.add_argument("--save-dir", help="also keep a copy of every snapshot in this folder on the camera host")
    p.add_argument("--auth", metavar="USER:PASS", help="require HTTP basic auth")
    p.add_argument("-v", "--verbose", action="store_true", help="log every HTTP request")
    args = p.parse_args()

    if args.list:
        if shutil.which("v4l2-ctl"):
            subprocess.run(["v4l2-ctl", "--list-devices"])
        else:
            for dev in sorted(d for d in os.listdir("/dev") if d.startswith("video")):
                print("/dev/" + dev)
        return
    if args.camera is None:
        p.error("--camera is required (use --list to see available cameras)")

    if args.camera == "test":
        controls = FakeControls()
        cam = TestPatternCamera(controls, width=args.width, height=args.height, fps=args.fps)
    else:
        device = resolve_device(args.camera)
        if args.list_formats:
            subprocess.run(["v4l2-ctl", "-d", device, "--list-formats-ext"])
            return
        controls = V4L2Controls(device)
        if not (args.width and args.height):
            mode = largest_mode(device, args.fourcc)
            if mode:
                args.width, args.height = mode
        try:
            cam = Camera(device, args.width, args.height, args.fps, args.fourcc)
        except RuntimeError as e:
            sys.exit(f"{e}\nIs the camera plugged in, and is your user in the 'video' group?")

    if args.settings:
        with open(args.settings) as f:
            for err in controls.apply(json.load(f)):
                print("settings:", err, file=sys.stderr)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(cam, controls, args))
    server.daemon_threads = True
    i = cam.info()
    print(f"Camera {i['device']}: {i['width']}x{i['height']} @ {i['fps']} fps ({i['fourcc']})")
    print(f"Serving on http://{args.host}:{args.port}/   (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        cam.close()


if __name__ == "__main__":
    main()
