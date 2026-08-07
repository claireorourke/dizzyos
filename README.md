# dizzyos

A tiny "operating system" for an LED-matrix sign. A small **kernel** provides shared
services (display, data, fonts, input) and self-contained **apps** plug in — each with
its own lifecycle. A **launcher** rotates through them with smooth transitions.

Runs on two chained Adafruit 64×64 HUB75 panels (a 128×64 canvas) driven by a
Raspberry Pi + Adafruit RGB Matrix Bonnet — and, thanks to a drop-in emulator, on your
Mac with **zero code changes**.

Apps so far: **Cafe Menu**, which renders [Izzy's Cafe](https://izzybennett.com/izzys-cafe/)
from the site's `/izzys-cafe.json` feed; **Weather**, current conditions from NWS station
observations (Open-Meteo outside the US);
and **Subway**, live next-train times from the MTA's realtime feeds.

---

## Quickstart on a Mac (no hardware needed)

```bash
python3 -m venv .venv && source .venv/bin/activate
make install                 # or: pip install -r requirements.txt

# Easiest: the preview script (creates the venv on first run):
./preview.sh                 # live browser preview at http://localhost:8888
./preview.sh png             # render one static frame to a PNG and open it

# Or use the make targets — live preview serves at http://localhost:8888:
make dev                     # full app rotation
make dev APP=weather         # just one app — handy while building it
make frames APP=weather      # render frames to PNGs, headless -> frames/weather/
```

`make dev`/`make frames` are thin wrappers over `python run.py` (use it directly if
you prefer: `python run.py --app weather`, `python run.py --dump-frames frames/`).

The `--led-*` flags mirror `rpi-rgb-led-matrix`, e.g.
`python run.py --led-rows=64 --led-cols=64 --led-chain=2`. Panel geometry defaults
live in `config.yaml`.

### Emulator look

`emulator_config.json` tunes how the browser emulator renders. It ships configured to
**simulate two chained Adafruit 64×64 3mm-pitch (P3) panels** — a 128×64 canvas drawn
with the `real` pixel style plus a subtle glow, so previews resemble the physical sign
lit up. Panel geometry (rows/cols/chain) lives in `config.yaml`; this file only
controls appearance (pixel size/style/glow).

## Architecture

Apps live in this repo (a monorepo) behind a clean kernel/app seam. For *why* — and
when to revisit it — see [ADR 0001](docs/adr/0001-monorepo-with-clean-seam.md); the
seam rules for app authors live in `apps/__init__.py`.

```
run.py            entrypoint + CLI (--led-* flags, --dump-frames)
config.yaml       matrix geometry, app rotation, dwell/transition
kernel/
  display.py      Mac/Pi drop-in shim + matrix construction
  launcher.py     rotate apps, double-buffered frame loop, transitions
  app.py          the App base class (the whole app contract)
  loader.py       discover + instantiate apps from apps/<name>/
  data.py         cached, stale-on-error fetching — JSON or bytes (http + file)
  render.py       font loading + text/scroll helpers
  services.py     the handle passed to each app
  input.py        (optional) Bonnet GPIO buttons -> next/prev app
apps/
  cafe_menu/      the site menu, scrolling
  weather/        current conditions + high/low, procedural weather icons
  subway/         next trains per station, incl. a tiny GTFS-realtime reader
fonts/            drop a .ttf/.otf here for crisp text
```

An app implements one method:

```python
class MyApp(App):
    def refresh(self):        # pull data off the render loop (optional)
        ...
    def render(self, t):      # return a PIL.Image for elapsed time t (seconds)
        ...
```

## Adding an app

1. `mkdir apps/weather` with an `app.py` defining one `App` subclass and a
   `manifest.yaml` (name, `dwell`, `refresh_interval`, any config keys).
2. Add `weather` to `launcher.rotation` in `config.yaml`.

That's it — the loader discovers it, the launcher rotates to it. Fetch data via
`self.services.data.get_json(url, ttl=...)` — or `get_bytes(...)` for a binary feed,
which is how the subway app reads the MTA's protobuf. Both cache with a TTL and keep
showing the last good value if a fetch fails.

## Deploying to the Raspberry Pi

1. Flash **Raspberry Pi OS Lite** (use a Pi 4 / 3B+ / Zero 2 W — solid GPIO timing).
2. Install the real driver (builds hzeller/rpi-rgb-led-matrix + Python bindings):
   ```bash
   curl https://raw.githubusercontent.com/adafruit/Raspberry-Pi-Installer-Scripts/main/rgb-matrix.sh -O
   sudo bash rgb-matrix.sh
   ```
3. Clone this repo, then `pip install Pillow PyYAML` (the emulator is **not** needed
   on the Pi — `kernel/display.py` uses the real `rgbmatrix` module automatically).
4. Run the same command, adding hardware flags:
   ```bash
   python run.py --led-gpio-mapping=adafruit-hat-pwm --led-slowdown-gpio=4
   ```
5. Auto-start on boot: `sudo cp dizzyos.service /etc/systemd/system/ &&
   sudo systemctl enable --now dizzyos`.

### Hardware notes

- **Power:** two 64×64 panels can pull ~3–4A **each** at full white. Use a **5V 8–10A**
  supply and feed the panels directly, not through the Pi.
- **Solder the Bonnet's address-E jumper** (required for 1:32-scan 64-row panels).
- **Solder the hardware-PWM jumper** (GPIO4↔GPIO18) to kill flicker, then use
  `--led-gpio-mapping=adafruit-hat-pwm`.
- **Stack vs. side-by-side:** the panels chain into 128×64. To mount them as a tall
  64×128 instead, set `pixel_mapper_config: "Rotate:90"` in `config.yaml` — no code
  change.

## Credits

Built on [rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) and
[RGBMatrixEmulator](https://github.com/ty-porter/RGBMatrixEmulator). Architecture nods
to the [MLB](https://github.com/MLB-LED-scoreboard/mlb-led-scoreboard)/[NHL](https://github.com/riffnshred/nhl-led-scoreboard)
LED scoreboards, which use the same stack.
