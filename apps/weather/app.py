"""Weather app — current conditions for a configured location.

Renders a single frame: a procedurally-drawn weather icon (sun/cloud/rain/snow/storm...)
and the location on the left, and the current temperature, the day's high/low, and the
local time on the right. Text uses the kernel's hand-designed bitmap font
(services.fonts.pixel()) so it stays crisp on the LED panel instead of smearing like an
anti-aliased TTF.

The numbers come from `sources.fetch`, which prefers real NWS station observations and
falls back to Open-Meteo's model outside US coverage — see `sources.py` for why that
order matters. This module only renders; it never learns which service answered.
"""

from datetime import datetime, timedelta, timezone

from PIL import ImageDraw

from kernel.app import App

from apps.weather import sources
from apps.weather.icons import PHASES, draw_icon

# scale = bitmap-font pixel scale (1 = 5x7, 3 = 15x21...); color = RGB.
PALETTE = {
    "temp": {"scale": 4, "color": (245, 245, 245)},  # big current temp (shrinks to fit)
    "high": {"scale": 1, "color": (255, 150, 90)},   # warm — daily high
    "low": {"scale": 1, "color": (120, 200, 255)},   # cool — daily low
    "label": {"scale": 1, "color": (170, 170, 170)}, # place name in the header
    "time": {"scale": 1, "color": (255, 196, 84)},   # amber clock in the header
    "rain": {"scale": 1, "color": (120, 190, 255)},  # upcoming-rain alert (bottom)
    "humidity": {"scale": 1, "color": (130, 200, 185)},  # humidity (bottom, when no rain)
    "now": {"scale": 1, "color": (255, 120, 120)},   # it's happening *now* — warmer, reads first
}

# What the bottom row says when precipitation is being observed right now. Present
# tense and no time at all: a clock time next to weather you can already hear reads
# as a forecast, which is exactly the confusion this replaces.
NOW_TEXT = {"storm": "Storm now", "rain": "Raining now", "snow": "Snowing now"}


def _ellipsize(font, text, scale, max_w):
    """Trim `text` (appending an ellipsis) until it fits within `max_w` pixels."""
    if not text or font.measure(text, scale) <= max_w:
        return text
    while text and font.measure(text + "…", scale) > max_w:
        text = text[:-1]
    return text + "…" if text else ""


class WeatherApp(App):
    def on_start(self, services):
        super().on_start(services)
        self._wx = None
        self._label = self.config.get("location_label") or ""

    def refresh(self):
        lat, lon, self._label = self._resolve_location()
        ttl = self.refresh_interval or 600
        self._wx = sources.fetch(self.services.data, lat, lon, self.config, ttl)

    def _resolve_location(self):
        """Return (lat, lon, label). Geocode `zipcode` via zippopotam.us when set,
        else use the configured lat/lon. Never raises — degrades to the fallback
        coordinates on any lookup failure so the sign always renders."""
        lat = self.config.get("latitude", 40.7128)
        lon = self.config.get("longitude", -74.0060)
        label = self.config.get("location_label") or ""
        zipc = str(self.config.get("zipcode") or "").strip()
        if not zipc:
            return lat, lon, label

        country = str(self.config.get("country") or "us").strip() or "us"
        base = self.config.get("geocode_base", "https://api.zippopotam.us")
        # ZIP -> coordinates is static, so cache it hard; {} fallback degrades to lat/lon.
        geo = self.services.data.get_json(f"{base}/{country}/{zipc}", ttl=2592000, fallback={})
        places = geo.get("places") if isinstance(geo, dict) else None
        if places:
            place = places[0]
            try:
                lat, lon = float(place["latitude"]), float(place["longitude"])
                if not label:
                    label = place.get("place name", "").strip() or zipc
            except (KeyError, TypeError, ValueError):
                pass  # keep the configured coords/label on malformed geocode data
        return lat, lon, label

    # --- data extraction (pure, defensive against missing fields) ----------
    def _reading(self):
        """The normalized reading from `sources`, plus the two fields that have to be
        recomputed every frame because they move with the clock."""
        wx = self._wx or sources.FALLBACK
        now = self._local_now(wx.get("utc_offset_seconds", 0) or 0)
        return {
            **wx,
            "now": now,  # naive local time, ticks each frame (recomputed here)
            "rain_at": self._next_rain(wx, now),  # datetime of next likely rain, or None
        }

    def _local_now(self, offset_seconds):
        """Current wall-clock time in the location's timezone, as a naive datetime
        (matches the API's local, tz-naive hourly timestamps under timezone=auto)."""
        return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).replace(tzinfo=None)

    def _fmt_time(self, dt):
        """Format a time per the `time_format` config: 24-hour ("HH:MM") when set to
        "24h", otherwise 12-hour ("H:MM AM/PM")."""
        if str(self.config.get("time_format", "12h")).startswith("24"):
            return dt.strftime("%H:%M")
        return dt.strftime("%I:%M %p").lstrip("0")

    def _next_rain(self, wx, now):
        """Local `datetime` of the next hour within 24h whose precipitation probability
        meets the threshold, or None if none does. `now` is naive local.

        The window opens at the *top of the current hour*, not at `now`. Hourly buckets
        are stamped with the hour they begin, so anchoring on `now` threw away the hour
        already in progress — the one you are standing in. At 16:35 in a downpour the
        16:00 bucket was skipped and the sign pointed at the next qualifying hour
        instead, quietly turning rain happening now into rain happening later.
        """
        threshold = self.config.get("rain_probability_threshold", 30)
        start = now.replace(minute=0, second=0, microsecond=0)
        horizon = now + timedelta(hours=24)
        for moment, prob in wx.get("hourly") or []:
            if isinstance(prob, (int, float)) and prob >= threshold and start <= moment <= horizon:
                return moment
        return None

    # --- rendering ---------------------------------------------------------
    def render(self, t):
        # Data is primed by the launcher's off-loop refresh(); until then _reading()
        # falls back to FALLBACK, so we never fetch on the render thread.
        width = self.services.width
        pf = self.services.fonts.pixel()
        reading = self._reading()

        image = self.blank()
        draw = ImageDraw.Draw(image)

        # Header row: local time on the right, location on the left — the label is
        # clipped to the clock's left edge so a long place name can't run into it.
        time_str = self._fmt_time(reading["now"])
        ts = PALETTE["time"]["scale"]
        time_x = width - pf.measure(time_str, ts) - 2
        pf.draw_text(draw, time_x, 2, time_str, PALETTE["time"]["color"], scale=ts)

        ls = PALETTE["label"]["scale"]
        label = _ellipsize(pf, self._label, ls, time_x - 4)
        if label:
            pf.draw_text(draw, 2, 2, label, PALETTE["label"]["color"], scale=ls)

        # Body left: the weather icon, cycling through its three animation states.
        # `t` is seconds since this app gained focus, so the loop restarts cleanly
        # each time the launcher rotates back around to us.
        step = self.config.get("icon_frame_seconds", 0.5) or 0.5
        phase = int(t / step) % PHASES
        draw_icon(image, 26, 33, reading["category"], reading["is_day"], phase)

        # Body right: big current temperature. Shrink a size if 3 digits won't fit.
        rx = 52
        temp_str = f"{reading['temp']}°" if reading["temp"] is not None else "--°"
        cs = PALETTE["temp"]["scale"]
        while cs > 1 and rx + pf.measure(temp_str, cs) > width - 2:
            cs -= 1
        pf.draw_text(draw, rx, 13, temp_str, PALETTE["temp"]["color"], scale=cs)

        # Body right: the day's high / low, as two color-coded segments. Drop the
        # degree glyphs if the pretty form would overflow (3-digit or sub-zero temps).
        hl_y = 46
        hs = PALETTE["high"]["scale"]
        for deg in ("°", ""):
            high_str = f"H {reading['high']}{deg}" if reading["high"] is not None else "H --"
            low_str = f"L {reading['low']}{deg}" if reading["low"] is not None else "L --"
            gap = pf.measure(high_str, hs) + 4
            if rx + gap + pf.measure(low_str, hs) <= width - 2:
                break
        pf.draw_text(draw, rx, hl_y, high_str, PALETTE["high"]["color"], scale=hs)
        pf.draw_text(draw, rx + gap, hl_y, low_str, PALETTE["low"]["color"], scale=hs)

        # Bottom row, in order of what you'd want to know: precipitation being observed
        # right now beats a forecast for later, which beats the humidity filler.
        if reading.get("now_precip"):
            key, text = "now", NOW_TEXT.get(reading["now_precip"], "Precip now")
        elif reading["rain_at"]:
            key, text = "rain", f"Rain at {self._fmt_time(reading['rain_at'])}"
        elif reading["humidity"] is not None:
            key, text = "humidity", f"Humidity {reading['humidity']}%"
        else:
            key = None
        if key:
            pf.draw_text(draw, 2, 57, text, PALETTE[key]["color"], scale=PALETTE[key]["scale"])

        return image
