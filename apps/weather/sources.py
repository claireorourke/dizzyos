"""Where the weather app's numbers come from.

Two providers, normalized to one reading dict so `app.py` only ever renders — it
never learns which service answered.

**NWS (api.weather.gov) is primary.** It is the only one of the two that reports what
a human standing outside can actually see: real METAR station observations, including
present weather like `thunderstorms`. Open-Meteo's `current` block is *model output*
interpolated to the hour, and a model does not know about the squall line that just
rolled over you — during an August thunderstorm over Manhattan it happily reported
`weather_code: 3` (overcast) and `precipitation: 0.0`, while the stations one borough
west were reporting heavy thunderstorms. A sign that contradicts the window is worse
than no sign.

**Open-Meteo stays as the fallback.** api.weather.gov is US-only, and this app takes a
`country` for non-US postal codes, so anything outside NWS coverage still needs a
provider. It also catches NWS outages. Which one answered is recorded in the reading's
`source` field.

The normalized reading:

    temp, high, low, humidity   rounded °F, or None
    category                    icon name for icons.draw_icon ("storm", "rain", ...)
    observed                    True if `category` came from a station, not a model
    now_precip                  None | "storm" | "rain" | "snow" — falling *right now*
    is_day                      1 / 0
    utc_offset_seconds          the location's UTC offset
    hourly                      [(naive local datetime, precip probability %), ...]
    source                      "nws" | "open-meteo" | "fallback"
"""

import math
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from apps.weather.icons import category as wmo_category

# api.weather.gov asks callers to identify themselves and reserves the right to block
# anonymous default agents. A contact URL is the form they ask for.
_NWS_HEADERS = {
    "User-Agent": "(dizzyos LED sign, https://github.com/claireorourke/dizzyos)",
    "Accept": "application/geo+json",
}

# A gridpoint and its station list are fixed properties of a coordinate, so cache them
# for a month rather than re-deriving them on every refresh.
_STATIC_TTL = 2592000

# Shown only if nothing can be reached on first paint. Mild, clear, offset 0 (UTC) so
# the clock still renders offline.
FALLBACK = {
    "temp": 70, "high": 75, "low": 60, "humidity": 50,
    "category": "partly", "observed": False, "now_precip": None,
    "is_day": 1, "utc_offset_seconds": 0, "hourly": [], "source": "fallback",
}

# NWS `presentWeather[].weather` values, most severe first — the first match wins, so
# "thunderstorms + rain" reads as a storm rather than as plain rain.
_OBS_WEATHER = [
    (("thunderstorms", "squalls", "funnel_cloud", "water_spout"), "storm", "storm"),
    (("snow", "snow_grains", "snow_pellets", "ice_crystals", "ice_pellets", "hail"),
     "snow", "snow"),
    (("rain", "spray"), "rain", "rain"),
    (("drizzle",), "drizzle", "rain"),
    (("fog", "fog_mist", "haze", "smoke", "dust", "sand", "volcanic_ash"), "fog", None),
]

# Sky cover, when no present weather is being reported. NWS gives eighths-of-sky codes.
_SKY = {"OVC": "cloudy", "BKN": "cloudy", "SCT": "partly", "FEW": "clear",
        "SKC": "clear", "CLR": "clear", "NCD": "clear", "NSC": "clear"}

# Last-ditch scan of the human-readable `textDescription`, for the rare ob that carries
# neither structured present weather nor cloud layers.
_TEXT_KEYWORDS = [
    ("thunder", "storm", "storm"), ("snow", "snow", "snow"), ("sleet", "snow", "snow"),
    ("freezing", "snow", "snow"), ("drizzle", "drizzle", "rain"),
    ("rain", "rain", "rain"), ("shower", "rain", "rain"),
    ("fog", "fog", None), ("mist", "fog", None), ("haze", "fog", None),
    # Sky wording, least cloudy phrasing first so "Partly Cloudy" doesn't get read as
    # overcast by the bare "cloud" match further down.
    ("partly", "partly", None), ("mostly sunny", "partly", None),
    ("overcast", "cloudy", None), ("cloud", "cloudy", None),
    ("sunny", "clear", None), ("fair", "clear", None), ("clear", "clear", None),
]


def fetch(data, lat, lon, config, ttl):
    """Return a normalized reading for (lat, lon). Never raises — falls through
    NWS -> Open-Meteo -> FALLBACK so the sign always has something to render."""
    provider = str(config.get("provider", "auto")).lower()
    if provider in ("auto", "nws"):
        try:
            reading = _nws(data, lat, lon, config, ttl)
        except Exception:  # noqa: BLE001 - any NWS failure just means try the other one
            reading = None
        if reading:
            return reading
        if provider == "nws":
            return dict(FALLBACK)
    try:
        return _open_meteo(data, lat, lon, config, ttl)
    except Exception:  # noqa: BLE001 - degrade to the bundled snapshot rather than blank
        return dict(FALLBACK)


# --- NWS ------------------------------------------------------------------
def _nws(data, lat, lon, config, ttl):
    """Normalized reading from api.weather.gov, or None if this point is outside
    NWS coverage (i.e. not the US) or the service gave us nothing usable."""
    base = config.get("nws_base", "https://api.weather.gov")
    # /points rejects more than four decimals, and trimming also keeps nearby
    # coordinates sharing one cache entry.
    point = _json(data, f"{base}/points/{lat:.4f},{lon:.4f}", _STATIC_TTL)
    props = (point or {}).get("properties") or {}
    grid = props.get("forecastGridData")
    hourly_url = props.get("forecastHourly")
    if not grid or not hourly_url:
        return None  # outside coverage — the caller falls back to Open-Meteo

    hourly = _nws_hourly(_json(data, hourly_url, ttl))
    if not hourly["periods"]:
        return None  # no forecast, no clock offset, nothing worth showing
    current_hour = hourly["periods"][hourly["index"]]

    observation = _nws_observation(data, props.get("observationStations"), config, lat, lon)
    high, low = _nws_high_low(_json(data, grid, ttl), hourly["now"], hourly["offset"])

    # The observation wins on conditions when we have one: it is measured, not modeled.
    # Without it, fall back to the forecast's own wording for the current hour.
    if observation:
        category, now_precip = observation["category"], observation["now_precip"]
    else:
        category, now_precip = _forecast_category(current_hour["text"]), None

    return {
        "temp": observation["temp"] if observation and observation["temp"] is not None
        else current_hour["temp"],
        "high": high,
        "low": low,
        "humidity": observation["humidity"] if observation else None,
        "category": category,
        "observed": bool(observation),
        "now_precip": now_precip,
        "is_day": 1 if is_day(lat, lon) else 0,
        "utc_offset_seconds": hourly["offset"],
        "hourly": [(p["time"], p["pop"]) for p in hourly["periods"]],
        "source": "nws",
    }


def _nws_hourly(payload):
    """Pull the hourly forecast into naive-local periods, plus the location's UTC
    offset (read off the timestamps, which carry it) and the index of the hour we are
    actually in — the feed's first period can lag the wall clock by an hour, which
    would otherwise flip `is_day` at the wrong moment around sunrise and sunset."""
    periods, offset, first = [], 0, None
    for raw in ((payload or {}).get("properties") or {}).get("periods") or []:
        start = _parse_time(raw.get("startTime"))
        if start is None:
            continue
        if first is None:
            offset = int(start.utcoffset().total_seconds()) if start.utcoffset() else 0
            first = start.replace(tzinfo=None)
        pop = (raw.get("probabilityOfPrecipitation") or {}).get("value")
        periods.append({
            "time": start.replace(tzinfo=None),
            "pop": pop if isinstance(pop, (int, float)) else 0,
            "temp": _to_f(raw.get("temperature"), raw.get("temperatureUnit")),
            "text": str(raw.get("shortForecast") or ""),
        })

    now = _local_now(offset)
    index = 0
    for i, period in enumerate(periods):
        if period["time"] <= now:
            index = i
        else:
            break
    return {"periods": periods, "offset": offset, "now": now, "index": index}


def _local_now(offset_seconds):
    """Wall-clock time at a UTC offset, as a naive datetime — the same shape the
    forecast timestamps take once their offset is stripped."""
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).replace(tzinfo=None)


def is_day(lat, lon, when=None):
    """True if the sun is above the horizon at (lat, lon) right now.

    Computed rather than taken from either provider. NWS's hourly `isDaytime` is a
    flat 06:00-18:00 convention, not actual daylight — trusting it hangs a moon over
    a bright August evening, since the sun here doesn't set until after 20:00. The
    standard low-precision solar position (accurate to well under a minute at this
    scale) settles it for both providers with one definition.
    """
    when = when or datetime.now(timezone.utc)
    # Days since the J2000.0 epoch, as a float.
    n = (when - datetime(2000, 1, 1, 12, tzinfo=timezone.utc)).total_seconds() / 86400.0

    mean_longitude = math.radians((280.460 + 0.9856474 * n) % 360)
    anomaly = math.radians((357.528 + 0.9856003 * n) % 360)
    ecliptic = mean_longitude + math.radians(1.915) * math.sin(anomaly) \
        + math.radians(0.020) * math.sin(2 * anomaly)
    obliquity = math.radians(23.439 - 0.0000004 * n)

    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic))
    right_ascension = math.atan2(math.cos(obliquity) * math.sin(ecliptic), math.cos(ecliptic))

    sidereal = (18.697374558 + 24.06570982441908 * n) % 24  # Greenwich, in hours
    hour_angle = math.radians((sidereal * 15 + lon) - math.degrees(right_ascension))

    latitude = math.radians(lat)
    elevation = math.asin(math.sin(latitude) * math.sin(declination)
                          + math.cos(latitude) * math.cos(declination) * math.cos(hour_angle))
    # -0.833° rather than 0: the sun's disc has width, and the atmosphere refracts it
    # into view slightly before it geometrically rises. This is the usual convention.
    return math.degrees(elevation) > -0.833


def _nws_high_low(payload, now, offset):
    """Today's high and low from the raw gridpoint series.

    Uses the gridpoint rather than the day/night forecast periods because by evening
    the day's high is already in the past — the periods list starts at "Tonight" and
    would hand back *tomorrow's* high instead.
    """
    props = (payload or {}).get("properties") or {}
    return (_series_value(props.get("maxTemperature"), now, offset),
            _series_value(props.get("minTemperature"), now, offset))


def _series_value(series, now, offset):
    """Value of the gridpoint series entry covering `now` (naive local).

    Entries are ISO8601 `start/duration` pairs, stamped in UTC. Rather than parse the
    durations, take the last entry that has already started — the series is ordered and
    runs one entry per day, so that is the one in effect.
    """
    if not isinstance(series, dict) or now is None:
        return None
    celsius = "degc" in str(series.get("uom", "")).lower()
    chosen = None
    for entry in series.get("values") or []:
        start = _parse_time(str(entry.get("validTime", "")).split("/")[0])
        if start is None:
            continue
        # Shift the UTC stamp into local wall-clock before comparing — `now` is naive
        # local, so comparing it against a naive UTC stamp is off by the offset.
        local_start = (start + timedelta(seconds=offset)).replace(tzinfo=None)
        if local_start <= now or chosen is None:
            chosen = entry.get("value")
        else:
            break
    if not isinstance(chosen, (int, float)):
        return None
    return round(chosen * 9 / 5 + 32) if celsius else round(chosen)


def _nws_observation(data, stations_url, config, lat, lon):
    """Latest usable observation from the stations nearest this point.

    Walks outward from the closest station until one has a recent ob that actually
    says something, because the flagship city stations are often the sparsest — KNYC
    in Central Park routinely reports no present weather at all.

    One deliberate exception to nearest-wins: a thunderstorm anywhere within
    `storm_radius_km` promotes the icon to `storm`. Thunder and lightning carry far
    outside the rain footprint, so when the cell is over the next town you are still
    watching lightning out of your window, and the sign should agree with you.
    """
    if not stations_url:
        return None
    limit = int(config.get("observation_stations", 5) or 5)
    max_age = timedelta(minutes=int(config.get("observation_max_age_minutes", 90) or 90))
    radius = float(config.get("storm_radius_km", 25) or 25)

    catalog = _json(data, stations_url, _STATIC_TTL)
    nearest, storm_nearby = None, False
    for station in (((catalog or {}).get("features")) or [])[:limit]:
        url = station.get("id")
        if not url:
            continue
        payload = _json(data, f"{url}/observations/latest", 300)
        props = (payload or {}).get("properties") or {}
        stamp = _parse_time(props.get("timestamp"))
        if stamp is None or datetime.now(stamp.tzinfo) - stamp > max_age:
            continue  # a stale ob is worse than the forecast

        category, now_precip = _observed_category(props)
        if category == "storm" and _within(_coords(station), (lat, lon), radius):
            storm_nearby = True
        if nearest is None and category:
            nearest = {
                "category": category,
                "now_precip": now_precip,
                "temp": _to_f((props.get("temperature") or {}).get("value"), "C"),
                "humidity": _round((props.get("relativeHumidity") or {}).get("value")),
            }
    if nearest and storm_nearby:
        nearest["category"] = "storm"
        nearest["now_precip"] = nearest["now_precip"] or "storm"
    return nearest


def _within(station_point, here, radius_km):
    """True if a storm at `station_point` is close enough to `here` — the configured
    location, not another station — to count as overhead."""
    if not station_point:
        return False
    return _distance_km(station_point, here) <= radius_km


def _coords(station):
    coords = (station.get("geometry") or {}).get("coordinates")
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        try:
            return float(coords[1]), float(coords[0])  # GeoJSON is (lon, lat)
        except (TypeError, ValueError):
            return None
    return None


def _distance_km(a, b):
    """Great-circle distance between two (lat, lon) pairs."""
    radius = 6371.0
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat, dlon = lat2 - lat1, math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(h)))


def _observed_category(props):
    """(icon category, what's falling now) from one station observation."""
    reported = {str(entry.get("weather") or "").lower()
                for entry in props.get("presentWeather") or []}
    for names, category, precip in _OBS_WEATHER:
        if reported.intersection(names):
            return category, precip

    # Nothing falling: read the sky instead. The densest layer sets the icon.
    layers = {str(layer.get("amount") or "").upper()
              for layer in props.get("cloudLayers") or []}
    for code in ("OVC", "BKN", "SCT", "FEW", "SKC", "CLR", "NCD", "NSC"):
        if code in layers:
            return _SKY[code], None

    text = str(props.get("textDescription") or "").lower()
    for keyword, category, precip in _TEXT_KEYWORDS:
        if keyword in text:
            return category, precip
    return None, None


def _forecast_category(text):
    """Icon category from an NWS `shortForecast` phrase ("Showers And
    Thunderstorms Likely"), used when no station observation is available."""
    category, _ = _forecast_pair(text)
    return category or "cloudy"


def _forecast_pair(text):
    lowered = str(text).lower()
    for keyword, category, precip in _TEXT_KEYWORDS:
        if keyword in lowered:
            return category, precip
    return None, None


# --- Open-Meteo -----------------------------------------------------------
def _open_meteo(data, lat, lon, config, ttl):
    """Normalized reading from Open-Meteo — model output, no observations, so
    `observed` is False and there is no `now_precip` to trust."""
    base = config.get("api_base", "https://api.open-meteo.com/v1/forecast")
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,weather_code,is_day,relative_humidity_2m",
        "hourly": "precipitation_probability",
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "forecast_days": 2,  # 48h of hourly, so "next rain" is found even late in the day
    }
    payload = _json(data, f"{base}?{urlencode(params)}", ttl)
    if not payload:
        return dict(FALLBACK)

    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    hourly = payload.get("hourly") or {}

    periods = []
    for stamp, pop in zip(hourly.get("time") or [], hourly.get("precipitation_probability") or []):
        moment = _parse_time(stamp)
        if moment is not None and isinstance(pop, (int, float)):
            periods.append((moment.replace(tzinfo=None), pop))

    return {
        "temp": _round(current.get("temperature_2m")),
        "high": _round(highs[0]) if highs else None,
        "low": _round(lows[0]) if lows else None,
        "humidity": _round(current.get("relative_humidity_2m")),
        "category": wmo_category(current.get("weather_code", 3)),
        "observed": False,
        "now_precip": None,
        "is_day": 1 if is_day(lat, lon) else 0,
        "utc_offset_seconds": payload.get("utc_offset_seconds", 0) or 0,
        "hourly": periods,
        "source": "open-meteo",
    }


# --- shared helpers -------------------------------------------------------
def _json(data, url, ttl):
    headers = _NWS_HEADERS if "weather.gov" in url else None
    return data.get_json(url, ttl=ttl, fallback={}, headers=headers)


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _round(value):
    return round(value) if isinstance(value, (int, float)) else None


def _to_f(value, unit):
    """Round to whole °F, converting from Celsius when that's what came back."""
    if not isinstance(value, (int, float)):
        return None
    if str(unit or "").upper().endswith("C"):
        return round(value * 9 / 5 + 32)
    return round(value)
