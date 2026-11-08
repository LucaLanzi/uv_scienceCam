# Arducam network stream (USB UVC cameras)

Streams a USB Arducam to a browser over the network. From the browser you can
tune the camera's image settings live, then take a full-resolution picture
that downloads to the viewing computer.

## Setup (on the machine the camera is plugged into, Linux)

    make setup

This runs, in order:

1. `sudo apt-get install -y v4l-utils python3-venv python3-pip`. `v4l-utils`
   provides `v4l2-ctl`, which the image controls use.
2. Creates a virtual environment in `.venv/` and installs `requirements.txt`
   into it. Recent Debian, Ubuntu and Raspberry Pi OS block system-wide
   `pip install`, so the packages go there instead.
3. `sudo usermod -aG video <you>`, so you can use the camera without sudo. It
   is skipped if you are already in the group. **Log out and back in (or
   reboot) once afterwards** for this to take effect.

4. If the `ufw` firewall is active, opens port 8090 (or `PORT=...`) so other
   machines can reach the page.

Running `make setup` again is safe; it skips anything already done.

## Run

    make list                                   # find the camera
    make run                                    # camera 0, largest MJPG size, port 8090
    make run CAMERA=/dev/video2 PORT=9000
    make run CAMERA=0 ARGS="--auth me:secret --save-dir snapshots"
    make formats CAMERA=0                       # supported resolutions / fps
    make test                                   # test pattern, no camera needed

`ARGS` passes any of the options below to the program. You can also run the
script directly with the venv's Python:

    .venv/bin/python arducam_stream.py --camera 0 --port 8090

Open `http://<camera-host-ip>:8090/` on the viewing machine.

**Why port 8090:** on the rover's Jetson, port 8080 is already used by the
astronaut-streamer dashboard (in `billee-hardware-2027`). The other ports in
use there are 5000, 5001 (F´ dashboards), 5900 (spectrometer VNC), 8443, 8554,
8888, 8889, 9997, and UDP 8189 and 8890 (astronaut-streamer). Avoid those if
you pick a different port with `PORT=`/`--port`.

Tip: for a name that stays the same after a reboot, use the stable path from
`ls /dev/v4l/by-id/`, e.g. `--camera /dev/v4l/by-id/usb-Arducam_..-video-index0`.

## Useful options

| Option | Purpose |
|---|---|
| `--width/--height/--fps` | Capture mode (default: largest size the camera supports) |
| `--fourcc YUYV` | Uncompressed (on USB 2.0 only low res/fps is possible) |
| `--settings file.json` | Apply a saved tuning profile at start-up |
| `--save-dir DIR` | Also keep each snapshot on the camera host |
| `--stream-quality N`, `--stream-fps N` | Lower preview bandwidth (snapshots are unaffected) |
| `--auth user:pass` | Require a password |
| `--camera test` | Synthetic test pattern, no hardware needed |

## Web UI

- **Image controls**: every control the camera reports (brightness, contrast,
  saturation, gamma, gain, sharpness, white balance, exposure...), with its real
  range. Greyed-out controls are inactive because an "auto" mode is on. For
  example, set `auto_exposure` to *Manual Mode* to unlock `exposure_time_absolute`,
  and turn off `white_balance_automatic` to unlock `white_balance_temperature`.
  Click "default N" to restore one control.
- **Save settings / Load settings**: download or upload the current tuning as
  JSON, so you can reuse a profile that gives the required look.
- **Take picture**: grabs a fresh full-resolution frame (PNG, JPEG, TIFF or BMP)
  and your browser downloads it.

## HTTP API (for scripting)

    GET  /stream                       MJPEG live stream (works in VLC too)
    GET  /api/snapshot?format=png      download a picture
    GET  /api/state                    camera info + controls (JSON)
    POST /api/control  {"name":"brightness","value":10}
    GET  /api/settings / POST /api/settings   export / import a profile
    POST /api/reset                    all controls to defaults

Example: `curl -o pic.png "http://camera-host:8090/api/snapshot?format=png"`
