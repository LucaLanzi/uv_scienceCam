# Documentation

This document explains `arducam_stream.py` from the first line to the last:
what each part does, why it is written that way, and how the parts work
together. Line numbers refer to `arducam_stream.py`. For installation and
everyday use, see `README.md`.

---

## 1. Overview

The program runs on the computer the USB camera is plugged into. It does three
jobs at once:

1. **Capture**: a background thread reads frames from the camera as fast as
   the camera delivers them and keeps only the newest frame in memory.
2. **Control**: it asks the camera which image settings it supports
   (brightness, exposure, white balance and so on) and changes them on request.
   It does this with the Linux tool `v4l2-ctl`, the tool Arducam's Linux
   quick-start guide uses.
3. **Serve**: a small web server lets any browser on the network:
   - watch the live feed,
   - change those settings,
   - save or load a whole group of settings as a JSON file,
   - take a full-resolution picture that downloads to the viewer's computer.

```
 ┌──────────── camera host (Linux) ──────────────────────────────┐
 │                                                               │
 │  USB camera ──► Camera._loop thread ──► newest frame (memory) │
 │      ▲                                     │                  │
 │      │ v4l2-ctl -c name=value              ├─► /stream  (MJPEG)
 │      │                                     └─► /api/snapshot  │
 │  V4L2Controls ◄── /api/control, /api/settings, /api/reset     │
 │                                                               │
 │  ThreadingHTTPServer  (one thread per browser connection)     │
 └───────────────────────────────▲───────────────────────────────┘
                                 │  HTTP on --port (default 8090)
                      ┌──────────┴──────────┐
                      │  browser on viewer  │  web page (INDEX_HTML)
                      └─────────────────────┘
```

The program works with any UVC (USB Video Class) camera, which includes Arducam's
USB camera boards. It doesn't depend on a particular sensor. The resolution and
the list of controls are read from the camera each time it starts.

---

## 2. File layout

| Lines | Section | Purpose |
|---|---|---|
| 1–15 | Docstring | Short usage summary |
| 17–35 | Imports | Standard library, plus OpenCV and NumPy |
| 38–206 | Camera capture | `resolve_device`, `largest_mode`, `Camera`, `TestPatternCamera` |
| 208–344 | Camera controls | Parses `v4l2-ctl` output; `V4L2Controls`, `FakeControls` |
| 347–486 | HTTP server | `make_handler` builds the request handler (routes, stream, snapshot) |
| 489–625 | Web page | `INDEX_HTML`: the HTML, CSS and JavaScript sent to the browser |
| 628–713 | CLI | `main()`: command-line arguments, start-up, shutdown |
| `Makefile` | Setup | `make setup` and the run targets (section 12) |

---

## 3. Header and imports (lines 1–35)

- **Line 1** `#!/usr/bin/env python3` lets you run the file directly
  (`./arducam_stream.py`) once it is marked executable (`chmod +x`).
- **Lines 2–15** is the module docstring with example commands.
- **Lines 17–29** import only the Python standard library:
  - `argparse` reads the command-line arguments.
  - `base64` builds the password check for `--auth`.
  - `datetime` puts timestamps in file names.
  - `json` handles API bodies and settings files.
  - `os` and `shutil` handle file paths and find programs.
  - `re` parses `v4l2-ctl` output.
  - `subprocess` runs `v4l2-ctl`.
  - `threading` and `time` run the capture thread and its timing.
  - `http.server` provides the web server. No Flask or other framework is needed.
  - `urllib.parse` splits URLs and query strings.
- **Lines 31–35** import OpenCV (`cv2`) and NumPy. If they are missing, the
  program stops with an install hint instead of a traceback.

---

## 4. Camera capture (lines 38–206)

### 4.1 `resolve_device(source)` (lines 42–46)

Turns whatever the user passed to `--camera` into a device path:

- `"0"` becomes `/dev/video0`.
- `"/dev/video2"` stays `/dev/video2`.
- `"/dev/v4l/by-id/usb-Arducam_...-video-index0"` becomes the real `/dev/videoN`
  it points to (`os.path.realpath` follows the link). The by-id names stay the
  same across reboots and re-plugging, whereas numbers like `video0` can change.

The path is then passed to both OpenCV and `v4l2-ctl`, so both always talk to
the same device.

### 4.2 `largest_mode` / `parse_largest_mode` (lines 49–74)

If you don't pass `--width/--height`, the program picks the camera's largest
picture size for you:

- `largest_mode` runs `v4l2-ctl -d <dev> --list-formats-ext`, which prints every
  pixel format and the sizes available in it.
- `parse_largest_mode` walks that text line by line:
  - A line like `[0]: 'MJPG' (Motion-JPEG, compressed)` starts a format block.
    `in_fmt` records whether this block is the format we asked for
    (`--fourcc`, default MJPG).
  - Lines like `Size: Discrete 1920x1080` inside that block are size options. The
    one with the largest width × height wins.
- It returns `(width, height)`, or `None` if `v4l2-ctl` isn't installed or no size
  was found. In that case OpenCV just uses the camera's default size.

It only looks at sizes for the requested format because, over USB 2.0, the
compressed MJPG format is the only way most cameras can deliver full resolution
at a usable frame rate. Uncompressed YUYV is usually limited to small sizes or
very low fps.

### 4.3 `class Camera` (lines 77–166)

Owns the OpenCV capture object and the background thread that reads frames.

**State, set up in `__init__` (lines 80–91)**

| Attribute | Meaning |
|---|---|
| `device` | Device path |
| `req` | Requested width/height/fps/fourcc, kept so `_open()` can reopen the same way |
| `cond` | A `threading.Condition`: one lock plus "a new frame arrived" notifications |
| `frame` | The newest frame (a NumPy BGR image) |
| `frame_id` | A counter that goes up by one per frame, so readers can tell old from new |
| `_jpeg_cache`, `_jpeg_lock` | The newest frame's JPEG, stored as `(frame_id, bytes)` so it is encoded only once (see `wait_jpeg`) |
| `running` | Set to `False` to stop the thread |

`__init__` opens the camera, then starts `_loop` as a daemon thread. Daemon
threads don't keep the program alive when you quit.

**`_open()` (lines 93–109)**

- Releases any previous capture.
- Opens the device with the V4L2 backend (`cv2.CAP_V4L2`, the Linux video API).
- Applies the requested settings in this order: FOURCC (pixel format) first,
  then width, height and fps. Some drivers only offer the larger sizes once
  MJPG is selected, which is why the format goes first.
- `CAP_PROP_BUFFERSIZE = 1` asks the driver to queue at most one frame. That
  keeps the live view current instead of lagging behind with old buffered
  frames.
- Raises `RuntimeError` if the device can't be opened. `main()` turns this into
  a friendly message.

**`info()` (lines 111–120)** reports what the camera actually agreed to, which
may differ from what was requested. The FOURCC comes back from OpenCV as a
32-bit integer; it is turned back into four characters (e.g. `MJPG`) by taking
one byte at a time. The web page shows this information in its header, and so
does the start-up message.

**`_loop()` (lines 122–142): the capture thread**

- Calls `cap.read()` over and over. It blocks until the camera delivers the
  next frame, so the loop runs at the camera's frame rate.
- On success, it takes the condition's lock, stores the frame, adds 1 to
  `frame_id`, and calls `notify_all()` to wake every thread waiting for a new
  frame.
- On failure, it counts. After 30 failures in a row (camera unplugged, USB
  glitch) it waits 1 s and reopens the camera. If that fails too, it prints the
  error and keeps trying, so plugging the camera back in recovers the stream
  without restarting the program.

**`wait_frame(after_id, timeout)` (lines 144–150)**

This is how every other part of the program gets frames. It waits until a
frame newer than `after_id` exists, then returns `(frame, frame_id)`. It
returns `(None, after_id)` if nothing arrives within `timeout` seconds.
- `wait_frame(-1)` returns the current frame straight away, once at least one
  exists.
- `wait_frame(some_id)` returns the next frame after that one.

**`wait_jpeg(after_id, quality)` (lines 152–161)**

Used by the live stream. It waits for the next frame, then JPEG-encodes it.
The cache means that if five browsers are watching, each frame is encoded
once, not five times. The first viewer to ask for frame N encodes it, and the
other four reuse the stored bytes.

**`close()` (lines 163–166)** stops the loop and releases the device.

### 4.4 `class TestPatternCamera` (lines 169–205)

A stand-in camera made in software, used with `--camera test`. It lets you try
the whole system (web page, controls, snapshots) on a machine with no camera.

- It subclasses `Camera`, so `wait_frame`/`wait_jpeg` are inherited unchanged.
  `__init__`, `info`, `_loop` and `close` are replaced.
- `_loop` draws a colour gradient whose blue channel pulses over time, then
  applies the fake `brightness` and `contrast` values from `FakeControls`. This
  way, moving a slider in the browser visibly changes the image. It also stamps
  the current time on the image so you can see the stream is live. It runs at
  `--fps` (default 30).

---

## 5. Camera controls (lines 208–344)

### 5.1 How UVC controls work

A UVC camera reports a list of controls to the operating system. Each control
has a name, a type, a range, a default and a current value. On Linux, `v4l2-ctl`
can read and set them:

```
v4l2-ctl -d /dev/video0 --list-ctrls-menus      # read all controls
v4l2-ctl -d /dev/video0 -c brightness=10        # set one
```

Example of what the first command prints:

```
                     brightness 0x00980900 (int)    : min=-64 max=64 step=1 default=0 value=0
        white_balance_automatic 0x0098090c (bool)   : default=1 value=1
           power_line_frequency 0x00980918 (menu)   : min=0 max=2 default=1 value=1 (50 Hz)
				0: Disabled
				1: 50 Hz
				2: 60 Hz
      white_balance_temperature 0x0098091a (int)    : min=2800 max=6500 step=1 default=4600 value=4600 flags=inactive
```

`flags=inactive` means the control is currently overridden by an automatic
mode. For example, `white_balance_temperature` has no effect while
`white_balance_automatic=1`.

The program builds its control list from this output and never hard-codes
control names. Whatever your camera supports appears in the web page, and
nothing it doesn't support does.

### 5.2 The parser (lines 212–243)

- **`CTRL_RE`** matches a control line and captures three things:
  - the name (`brightness`),
  - the type (`int`, `bool`, `menu`, ...),
  - the rest of the line after the colon.

  The hex ID `0x00980900` is optional because older `v4l2-ctl` versions don't
  print it.
- **`MENU_RE`** matches menu option lines such as `1: 50 Hz`.
- **`SKIP_TYPES`** lists the types that are ignored: `ctrl_class` (section
  headings) and `string` (not adjustable).
- **`parse_v4l2_ctrls(text)`** loops over the lines:
  - A control line becomes a dict. Every `key=value` pair on it (`min`, `max`,
    `step`, `default`, `value`) is extracted with a regex and stored as an
    integer where possible.
  - `flags=a,b` becomes a list, e.g. `["inactive"]`.
  - Menu-type controls get an empty `"menu"` list.
  - A menu option line adds `{value, label}` to the most recent menu control.
  - It returns a list of dicts, which is sent to the browser as JSON without
    changes.

### 5.3 Helper rules (lines 246–253)

- **`settable(c)`** is false for buttons (they trigger an action, they have no
  value) and for `read-only` or `disabled` controls. Only settable controls
  are saved, loaded or reset.
- **`apply_order(c)`** decides the order in which several settings are applied
  at once (loading a file, or resetting). Switches (`bool`, `menu`) go first and
  numbers go second.

  The order matters. If you load a profile with `auto_exposure=1` (manual) and
  `exposure_time_absolute=300`, the camera ignores the exposure value unless
  manual mode is already on. Setting the switch first makes the number stick.

### 5.4 `class V4L2Controls` (lines 256–310)

| Method | What it does |
|---|---|
| `__init__` | Checks that `v4l2-ctl` is installed. If not, it prints a warning and sets `available = False`. Streaming still works, but tuning is turned off and the web page says why. |
| `list()` | Runs `--list-ctrls-menus` and parses it. This is called on every request, so the page always shows live values, including changes made by the camera's own auto modes. |
| `set(name, value)` | Checks that `name` is a real control on this camera and that `value` is a whole number, then runs `v4l2-ctl -c name=value`. Returns `None` on success, or the error text from `v4l2-ctl`. The name and value are passed to `subprocess` as a list and never through a shell, so nothing a browser sends can run extra commands. A lock makes sure only one `v4l2-ctl` set runs at a time. |
| `apply(settings)` | Takes a `{name: value}` dict (a settings file). It ignores unknown or non-settable names, so a profile saved on a different camera loads without errors, sorts the rest with `apply_order`, sets each one, and returns a list of errors. |
| `snapshot_settings()` | Returns the current `{name: value}` for every settable control. This is what "Save settings" downloads. |
| `reset_defaults()` | Applies each control's `default`. |

Controls can be changed while OpenCV is streaming from the same device. Linux
allows one program to stream and another to adjust settings at the same time.

### 5.5 `class FakeControls` (lines 313–344)

The test-mode version of `V4L2Controls`, with five made-up controls kept in a
Python dict. It copies the real behaviour of an auto mode: `exposure` is
marked `inactive` while `exposure_auto` is on, so you can see the greying-out
in the web page without a camera.

---

## 6. HTTP server (lines 347–486)

### 6.1 `make_handler(cam, controls, args)`

Python's `http.server` creates a new handler object for each request and
doesn't let you pass in extra objects. `make_handler` gets around this with a
closure: it defines the `Handler` class inside a function, so every method can
use `cam`, `controls` and `args`.

`auth_token` (line 352) is the Base64 encoding of `user:pass`. It is worked
out once, so each request only compares strings.

`protocol_version = "HTTP/1.1"` lets browsers reuse one connection for many
requests (keep-alive). That's why every response sets `Content-Length`.

### 6.2 Helper methods

- **`log_message`** keeps request logging quiet unless `--verbose` is given.
  The live stream would otherwise fill the terminal.
- **`_authorized()`**: if `--auth` was given, it checks the `Authorization:
  Basic ...` header. When the header is missing or wrong, it replies `401` with
  `WWW-Authenticate`, which makes the browser show its built-in password box.
  Note that basic auth isn't encrypted over plain HTTP. It keeps out casual
  visitors on a local network; it doesn't make the page safe on the internet.
- **`_send(code, body, ctype, extra)`** is the one place that writes complete
  responses. It turns dicts and lists into JSON and strings into UTF-8, sets
  the length, turns off caching (`no-store`, so values and snapshots are never
  stale), and adds any extra headers.
- **`_json_body()`** reads and decodes a request's JSON body. It returns
  `None` for invalid JSON, which the caller answers with a `400` error.
- **`_state(errors)`** builds the main JSON reply: camera info, the full
  control list, whether tuning is available, and any errors. Every request
  that changes a setting replies with this, so the page refreshes from a
  single response.

### 6.3 Routes

| Method & path | Handler | Response |
|---|---|---|
| `GET /` | `do_GET` | The web page (`INDEX_HTML`) |
| `GET /stream` | `_stream` | Endless MJPEG stream |
| `GET /api/state` | `do_GET` | `_state()` JSON |
| `GET /api/snapshot?format=png\|jpg\|tiff\|bmp` | `_snapshot` | Image file as a download |
| `GET /api/settings` | `do_GET` | Current settings as a downloadable `camera_settings_<time>.json` |
| `POST /api/control` `{"name","value"}` | `do_POST` | Sets one control. Replies with `_state`: status 200, or 400 plus the error |
| `POST /api/settings` `{name: value, ...}` | `do_POST` | Applies a profile. Replies with `_state`, including per-control errors |
| `POST /api/reset` | `do_POST` | Resets all controls to their defaults. Replies with `_state` |
| anything else | | `404` |

`Content-Disposition: attachment; filename="..."` is what makes the browser
save the file instead of displaying it.

### 6.4 `_stream()`: live video (lines 437–459)

The stream uses MJPEG over HTTP: the `multipart/x-mixed-replace` content type.
The server never ends the response. It keeps sending parts, and each part is
one JPEG:

```
--frame
Content-Type: image/jpeg
Content-Length: 48213

<jpeg bytes>
```

Browsers show this in a plain `<img>` tag, replacing the picture with each new
part. VLC and OpenCV can also open the `/stream` URL directly.

The loop:
1. Calls `cam.wait_jpeg(last_id, quality)` to get the next new frame. Because
   it passes `last_id`, the same frame is never sent twice.
2. If `--stream-fps` was given, it skips frames that arrive sooner than
   `1/stream_fps` seconds after the last one sent. This saves bandwidth on slow
   networks.
3. Writes the part.

When the viewer closes the tab, the write fails with `BrokenPipeError` or
`ConnectionResetError`. The loop then ends quietly and the thread for that
connection exits. `Connection: close` tells the browser this connection is
never reused.

The live view uses `--stream-quality` (default 80) to keep bandwidth
reasonable. Snapshots don't go through this path, so they aren't affected.

### 6.5 `_snapshot(fmt)`: taking a picture (lines 461–484)

1. Checks the format. `jpeg` is accepted as another name for `jpg`.
2. **Waits for a fresh frame.** It first reads the current frame id, then
   waits for the frame after it. Without this step, clicking "Take picture"
   right after moving a slider could return a frame captured before the change
   took effect.
3. Encodes the camera's full-resolution frame:
   - PNG and TIFF are lossless.
   - BMP is uncompressed.
   - JPEG uses quality 98.
4. Names the file `snapshot_YYYYMMDD_HHMMSS_mmm.<ext>`, with milliseconds so
   quick shots don't overwrite each other.
5. If `--save-dir` is set, also writes a copy on the camera host.
6. Sends the file as a download.

The fresh-frame wait in step 2 makes each picture take one extra frame
period: about 33 ms at 30 fps.

---

## 7. The web page: `INDEX_HTML` (lines 489–625)

One self-contained HTML string, so the program is a single file with nothing
else to install or serve. It is a raw string (`r"""`) so backslashes in the
JavaScript are left alone.

### 7.1 Layout and style (lines 493–542)

- CSS variables at the top (`--bg`, `--acc`, ...) set the dark colour scheme.
- `<header>` shows the title and `#info` (device, resolution, fps, format).
- `#view` holds `<img id="feed" src="/stream">`, the live video, plus the
  **Take picture** button, the file-format selector, and **Reconnect feed**.
- `#side` is the controls panel:
  - **Save settings**, **Load settings** (opens a hidden file picker) and
    **Reset defaults**,
  - `#msg` for error messages,
  - `#controls`, filled in by JavaScript.
- The flex layout wraps the panel below the video on narrow screens such as
  phones.

### 7.2 JavaScript (lines 543–622)

| Function | Role |
|---|---|
| `$` / `pretty` | Shorthand for `getElementById`; turns `white_balance_automatic` into "white balance automatic" for display |
| `msg(errors)` | Shows server errors in red |
| `post(url, body)` | Sends JSON with POST, then calls `render()` with the `_state` reply |
| `setCtrl(name, value)` | Sends one control change. It waits 60 ms after the last movement before sending, so dragging a slider sends a few requests instead of hundreds. |
| `widget(c)` | Builds the input for one control, based on its type (see below) |
| `render(state)` | Updates the page from a `_state` reply (see below) |
| `snap()` | Starts the download by clicking a temporary link to `/api/snapshot?format=...`. The `&t=` timestamp stops any caching. |
| `reloadFeed()` | Reconnects the stream by changing the `<img>` source |
| `loadFile(input)` | Reads the chosen JSON file in the browser and posts it to `/api/settings` |

**Control types in `widget(c)`**

| Type | Input |
|---|---|
| `bool` | Checkbox |
| `menu` / `intmenu` | Dropdown built from `c.menu` |
| `button` | Button that sends `1` |
| `int` and others | Slider plus number box when `min`/`max` exist; number box only otherwise |

Each control also gets a "default N" label. Clicking it restores that one
control.

**How `render(state)` works**

- Updates the header info and the error message.
- Builds the control inputs only when the list of control names changes,
  which normally happens once. This avoids rebuilding the page during a slider
  drag.
- For each control:
  - It greys the control out if it is `inactive` or `read-only`, and adds a
    tooltip explaining why.
  - It updates the shown values, **except** on the input you are using
    (`document.activeElement`). A server reply then never makes the slider jump
    under your mouse.

**At start-up and on a timer**

- On load, the page fetches `/api/state` and calls `render()`.
- Every 5 seconds it fetches `/api/state` again, so values changed by the
  camera's auto modes or by another viewer show up.
- If the stream image errors (for example, the server restarted), the page
  reconnects after 2 seconds.

---

## 8. Command line and start-up: `main()` (lines 632–709)

### 8.1 Arguments

| Argument | Default | Purpose |
|---|---|---|
| `-c, --camera` | (required) | `0`, `/dev/videoN`, `/dev/v4l/by-id/...`, or `test` |
| `--list` | | List cameras (`v4l2-ctl --list-devices`, or `/dev/video*` if `v4l2-ctl` isn't installed) and exit |
| `--list-formats` | | Show every format, size and fps the chosen camera supports, then exit |
| `--host` | `0.0.0.0` | Network interface to listen on. `0.0.0.0` means all of them. Use `127.0.0.1` to allow this machine only. |
| `-p, --port` | `8090` | TCP port. Not 8080: on the rover's Jetson, the astronaut-streamer dashboard uses 8080. |
| `--width`, `--height` | Largest the camera offers | Capture size |
| `--fps` | Camera default | Capture frame rate |
| `--fourcc` | `MJPG` | Pixel format the camera sends |
| `--stream-quality` | `80` | Live-view JPEG quality |
| `--stream-fps` | `0` (no limit) | Live-view frame-rate limit |
| `--settings FILE` | | Profile to apply at start-up |
| `--save-dir DIR` | | Keep a server-side copy of every snapshot |
| `--auth USER:PASS` | | Require a password |
| `-v, --verbose` | | Log every request |

### 8.2 Start-up sequence

1. Parse the arguments. Handle `--list` and exit.
2. Require `--camera`.
3. **Test mode:** create `FakeControls` and a `TestPatternCamera`.
4. **Real camera:**
   1. Resolve the device path, and handle `--list-formats`.
   2. Create `V4L2Controls`.
   3. If no size was given, look up the largest size with `largest_mode`.
   4. Open the `Camera`. If that fails, exit with a hint about the camera
      being plugged in and the `video` group.
5. If `--settings` was given, apply the profile, printing any per-control
   problems as warnings.
6. Create a `ThreadingHTTPServer`, which handles each connection on its own
   thread. If the port is already taken, it exits with a message naming the port
   and how to find the program using it (`sudo ss -tlnp 'sport = :8090'`),
   instead of a traceback. Without threads, one open live stream would block all other
   requests. `daemon_threads = True` means open streams don't stop the program
   from exiting.
7. Print what the camera actually agreed to, and the address to open.
8. `serve_forever()` until Ctrl+C. Then close the server and release the
   camera (`finally` makes sure this always happens).

Lines 712–713 (`if __name__ == "__main__": main()`) start the program when you
run the file. You can still `import arducam_stream` from another script, for
example to reuse `parse_v4l2_ctrls`, without starting the server.

---

## 9. Threads, and how a request flows

| Thread | Count | Does |
|---|---|---|
| Main | 1 | Accepts connections |
| `Camera._loop` | 1 | Reads frames, signals `cond` |
| Request threads | 1 per connection | Run `Handler` methods; stream threads stay for as long as the viewer watches |

Shared data is protected:
- `cond` guards `frame` and `frame_id`.
- `_jpeg_lock` guards the JPEG cache.
- `V4L2Controls.lock` makes sure only one control change runs at a time.

Frames are never modified after they are stored, only replaced by the next
one. That's why a handler can keep using a frame after releasing the lock.

**Example: moving the exposure slider**

1. The browser's `oninput` calls `setCtrl("exposure_time_absolute", 300)`.
2. 60 ms after the last movement, it sends `POST /api/control`.
3. The handler calls `V4L2Controls.set`, which checks the name and value and
   runs `v4l2-ctl -d /dev/videoN -c exposure_time_absolute=300`.
4. The camera applies the change. The very next frames that `_loop` reads are
   brighter or darker, and the live stream shows them within a frame or two.
5. The handler replies with `_state()`. `render()` updates the other controls'
   values and inactive flags.

**Example: taking a picture**

1. `snap()` opens `/api/snapshot?format=png`.
2. `_snapshot` waits for the next new frame, encodes it as PNG at full
   resolution, and replies with `Content-Disposition: attachment`.
3. The browser saves `snapshot_<timestamp>.png` to the viewer's Downloads
   folder.

---

## 10. Extending the program

- **Add an API endpoint:** add a branch to `do_GET` or `do_POST` and use
  `self._send(...)`.
- **Process every frame** (cropping, overlays, rotation): change the frame in
  `Camera._loop` before storing it. The stream and snapshots then both use the
  processed frame.
- **Software adjustments the camera lacks:** apply them in `_snapshot` (on
  pictures only) or in `_loop` (on everything).
- **Use HTTPS:** wrap the server socket with `ssl.SSLContext.wrap_socket`, or
  put the program behind a reverse proxy such as nginx or Caddy.
- **Windows/macOS:** capture with OpenCV mostly works there if you remove
  `cv2.CAP_V4L2`. `v4l2-ctl` is Linux-only, so the controls would need a
  different backend, for example OpenCV's `CAP_PROP_*` properties.

---

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "Could not open camera" | Wrong index (run `--list`), the camera is in use by another program, or you lack permission: run `make setup` (or `sudo usermod -aG video $USER`), then log out and back in. |
| Two `/dev/video` entries for one camera | UVC cameras often create a second "metadata" node. Use the first, or the `video-index0` by-id link. |
| Low fps at high resolution | Make sure the header shows `MJPG`. Check what's available with `--list-formats`. |
| No controls in the page | Install `v4l-utils` on the camera host. |
| A control is greyed out | An auto mode is in charge: turn off `auto_exposure` (set Manual Mode), `white_balance_automatic`, `focus_automatic_continuous`, and so on. |
| Settings reset after unplugging the camera | UVC cameras forget settings when they lose power. Save a profile and pass it with `--settings`. |
| "Cannot listen on port 8090" | Another program is using the port. Find it with `sudo ss -tlnp 'sport = :8090'`, or run on another port: `make run PORT=8091`. |
| Can't reach the page from another computer | Check the host's firewall, e.g. `sudo ufw allow 8090/tcp`, and use the host's LAN IP address. |

---

## 12. The Makefile

`make setup` installs everything the program needs on the camera host. Each
step is its own target, so you can also run them one at a time.

| Target | What it does |
|---|---|
| `setup` | Runs `system-deps`, `venv`, `video-group` and `firewall`, in that order |
| `system-deps` | `sudo apt-get install -y v4l-utils python3-venv python3-pip` |
| `venv` | Creates `.venv/` and installs `requirements.txt` into it. When it finishes, it writes a marker file, `.venv/.installed`. Make only runs this step again if `requirements.txt` is newer than the marker. |
| `video-group` | Adds the user to the `video` group unless they are already in it. |
| `firewall` | If `ufw` is active, runs `sudo ufw allow $(PORT)/tcp` so other machines can reach the page. Otherwise it does nothing. |
| `run` | `.venv/bin/python arducam_stream.py --camera $(CAMERA) --port $(PORT) $(ARGS)` |
| `test`, `list`, `formats` | Test pattern, list cameras, list supported resolutions/fps |
| `clean` | Deletes `.venv/` and `__pycache__/` |

**Choosing the user for `video-group`.** The user is `SUDO_USER` if set, then
`USER`, then the output of `id -un`. This way, even `sudo make setup` adds you
and not root.

**Changing the defaults.** Variables set on the command line override them:
`CAMERA` (default `0`), `PORT` (default `8090`), `ARGS` (extra options for the
program) and `PYTHON` (the interpreter that creates the venv, default
`python3`).

**Why `opencv-python-headless`.** `requirements.txt` uses the "headless"
build of OpenCV, which leaves out the desktop window code. The program never
opens a window, and the headless build avoids missing-library errors such as
`libGL.so.1` on servers and Raspberry Pis that have no desktop installed.
