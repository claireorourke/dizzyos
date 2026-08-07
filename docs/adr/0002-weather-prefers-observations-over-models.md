# 0002 — The weather app prefers observations over models

**Status:** Accepted (2026-08)

## Context

A thunderstorm was over Manhattan — audible thunder, visible lightning, heavy rain —
and the sign showed a cloud icon and the line "Rain at 17:00". It was telling someone
already standing in the rain to expect rain later.

Nothing had crashed. The app was faithfully rendering what it was given. Two separate
causes stacked up:

**The data source could not see the storm.** The app read Open-Meteo's `current`
block, which is *model output* interpolated to the hour, not an observation. At 18:15
local it reported `weather_code: 3` (overcast) and `precipitation: 0.0` — and so did
its 15-minute series. Meanwhile NWS stations one borough west were reporting `Heavy
Thunderstorms and Heavy Rain`. A forecast model that has not ingested the last hour of
radar simply does not know a convective cell exists; that is what models are like, and
no amount of parsing on our side recovers information the payload never carried.

**The app had no way to say "now".** `_next_rain()` scanned hourly buckets with
`now <= t`. Buckets are stamped with the hour they *begin*, so the hour in progress —
the one you are standing in — was always excluded. Even with perfect data, the sign
could only ever point at a later hour. It had no "it is raining" state at all, and the
storm icon in `icons.py` (bolt, flash cycle and all) had nothing that could select it.

The second bug is the one that makes the first one dangerous. A sign that is merely
*late* is forgivable; a sign that converts present tense into future tense is actively
misleading.

## Decision

**Prefer measurements over predictions for anything describing right now.**

The weather app fetches from **NWS (api.weather.gov) first**: real METAR station
observations, including structured `presentWeather` like `thunderstorms`. It walks
outward from the nearest station until one has a recent ob that says something, because
the flagship city stations are often the sparsest — KNYC in Central Park routinely
reports no present weather at all.

**Open-Meteo stays as the fallback, not as a second opinion.** api.weather.gov is
US-only and this app takes a `country` for non-US postal codes, so out-of-coverage
locations still need a provider; it also covers NWS outages. The two are never merged
or reconciled — exactly one answers, and `sources.py` normalizes both to a single
reading dict so `app.py` never learns which.

Two supporting decisions fell out of building it:

- **Thunderstorms escalate within a radius** (`storm_radius_km`, default 25). If any
  nearby station reports thunderstorms, the icon becomes `storm` even when the closest
  station is dry. Thunder and lightning carry far outside the rain footprint — this is
  precisely the case that started this ADR.
- **Day/night is computed, not fetched.** NWS's hourly `isDaytime` is a flat 06:00–18:00
  convention rather than actual daylight, so it would hang a moon over a bright August
  evening two hours before sunset. `sources.is_day()` does the standard solar-elevation
  calculation instead (validated to ~2 minutes against published sunrise/sunset), giving
  both providers one definition.

## Consequences

- The sign can now say **"Storm now" / "Raining now" / "Snowing now"** in the present
  tense, and it outranks the forecast line. A time of day next to weather you can
  already hear reads as a forecast, which is the confusion being fixed.
- `_next_rain()` opens its window at the **top of the current hour**, so the hour in
  progress counts.
- More HTTP per refresh (points, stations, N observations, hourly, gridpoint) instead of
  one Open-Meteo call. The static lookups cache for a month and the rest for the refresh
  interval, so it is a handful of requests per 10 minutes — fine for both free tiers.
- `DataService` grew optional request headers, since NWS asks callers to identify
  themselves.
- Icon fidelity is bounded by the icon set: NWS reports `smoke`, which renders with the
  fog bands because there is no smoke icon. Acceptable; noted in the mapping.

## Revisit when (any one)

- The sign is used somewhere NWS does not cover often enough that the fallback's
  blindness to current conditions becomes the common case rather than the rare one.
- A keyless radar or lightning-strike feed becomes available — that beats point
  observations for exactly the convective case that motivated this.
- Station walking proves too slow or too chatty on the Pi's connection.

## Escape hatch

Providers are isolated behind `sources.fetch()` returning one normalized dict, and are
selectable per-install with `provider: auto | nws | open_meteo`. Adding or swapping a
service is a new private function plus a branch in `fetch()`; nothing in `app.py`
changes, because it only renders.
