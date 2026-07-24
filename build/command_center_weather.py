#!/usr/bin/env python3
"""
5-day weather forecasts for the Command Center footer - one city per company.

Source of truth is AccuWeather when an API key is available (env
ACCUWEATHER_API_KEY, or secrets/accuweather.json {"api_key": "..."}), falling
back to Open-Meteo (free, no key) otherwise. Either way the output shape is
identical, so the frontend never cares which source produced it:

    {"sierra": {"city": "Las Vegas, NV", "source": "AccuWeather",
                "updatedAt": "2026-07-24T17:03Z",
                "days": [{"date": "2026-07-24", "dow": "Thu", "emoji": "☀️",
                          "phrase": "Sunny", "hi": 108, "lo": 88, "precip": 5}, ...]}}

Forecasts are cached in data/command-center-weather.json for CACHE_TTL_HOURS
(the CI refresh runs every ~10 minutes; AccuWeather's free tier is 50
calls/day, and 4 cities x 8 refreshes/day = 32 stays comfortably under it).
AccuWeather location keys never change, so they cache forever. A failed pull
serves the stale cache rather than blanking the footer.

Smoke test:  py build/command_center_weather.py
"""
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE_FILE = os.path.join(ROOT, "data", "command-center-weather.json")
KEY_FILE = os.path.join(ROOT, "secrets", "accuweather.json")
CACHE_TTL_HOURS = 3

# One city per company board. lat/lon + IANA tz feed Open-Meteo; the query
# string finds the AccuWeather location key (looked up once, cached forever).
CITIES = {
    "sierra":   {"city": "Las Vegas, NV", "accu_q": "Las Vegas, NV", "lat": 36.1699, "lon": -115.1398, "tz": "America/Los_Angeles"},
    "russett":  {"city": "Tucson, AZ",    "accu_q": "Tucson, AZ",    "lat": 32.2226, "lon": -110.9747, "tz": "America/Phoenix"},
    "ultimate": {"city": "Boise, ID",     "accu_q": "Boise, ID",     "lat": 43.6150, "lon": -116.2023, "tz": "America/Boise"},
    "brothers": {"city": "Denver, CO",    "accu_q": "Denver, CO",    "lat": 39.7392, "lon": -104.9903, "tz": "America/Denver"},
}

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# AccuWeather day icon numbers -> (emoji, short phrase). Phrases come from the
# API itself; this map only supplies the emoji (fallback phrase if missing).
ACCU_ICONS = {
    1: "☀️", 2: "\U0001f324️", 3: "⛅", 4: "⛅", 5: "\U0001f324️",
    6: "\U0001f325️", 7: "☁️", 8: "☁️", 11: "\U0001f32b️",
    12: "\U0001f327️", 13: "\U0001f326️", 14: "\U0001f326️",
    15: "⛈️", 16: "⛈️", 17: "⛈️", 18: "\U0001f327️",
    19: "\U0001f328️", 20: "\U0001f328️", 21: "\U0001f328️", 22: "❄️",
    23: "\U0001f328️", 24: "\U0001f9ca", 25: "\U0001f9ca", 26: "\U0001f9ca",
    29: "\U0001f328️", 30: "\U0001f975", 31: "\U0001f976", 32: "\U0001f4a8",
}

# Open-Meteo WMO weather codes -> (emoji, phrase).
WMO_CODES = {
    0: ("☀️", "Sunny"), 1: ("\U0001f324️", "Mostly Sunny"),
    2: ("⛅", "Partly Cloudy"), 3: ("☁️", "Cloudy"),
    45: ("\U0001f32b️", "Fog"), 48: ("\U0001f32b️", "Freezing Fog"),
    51: ("\U0001f326️", "Light Drizzle"), 53: ("\U0001f326️", "Drizzle"),
    55: ("\U0001f326️", "Heavy Drizzle"), 56: ("\U0001f9ca", "Freezing Drizzle"),
    57: ("\U0001f9ca", "Freezing Drizzle"), 61: ("\U0001f327️", "Light Rain"),
    63: ("\U0001f327️", "Rain"), 65: ("\U0001f327️", "Heavy Rain"),
    66: ("\U0001f9ca", "Freezing Rain"), 67: ("\U0001f9ca", "Freezing Rain"),
    71: ("\U0001f328️", "Light Snow"), 73: ("\U0001f328️", "Snow"),
    75: ("❄️", "Heavy Snow"), 77: ("\U0001f328️", "Snow Grains"),
    80: ("\U0001f326️", "Light Showers"), 81: ("\U0001f326️", "Showers"),
    82: ("\U0001f327️", "Heavy Showers"), 85: ("\U0001f328️", "Snow Showers"),
    86: ("\U0001f328️", "Snow Showers"), 95: ("⛈️", "Thunderstorms"),
    96: ("⛈️", "T-Storms w/ Hail"), 99: ("⛈️", "T-Storms w/ Hail"),
}


def _http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "command-center-weather/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _accu_key():
    k = os.environ.get("ACCUWEATHER_API_KEY", "").strip()
    if k:
        return k
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            return (json.load(f).get("api_key") or "").strip()
    except Exception:
        return ""


def _dow(date_str):
    return DOW[dt.date.fromisoformat(date_str).weekday()]


def _fetch_accuweather(co, cfg, api_key, location_keys):
    """5-day forecast from AccuWeather. Mutates location_keys (persisted cache)."""
    loc = location_keys.get(co)
    if not loc:
        q = urllib.parse.quote(cfg["accu_q"])
        hits = _http_json(f"https://dataservice.accuweather.com/locations/v1/cities/search?apikey={api_key}&q={q}")
        if not hits:
            raise RuntimeError(f"AccuWeather found no location for {cfg['accu_q']!r}")
        loc = hits[0]["Key"]
        location_keys[co] = loc
    fc = _http_json(f"https://dataservice.accuweather.com/forecasts/v1/daily/5day/{loc}?apikey={api_key}&details=true")
    days = []
    for d in fc.get("DailyForecasts", [])[:5]:
        date = d["Date"][:10]
        day = d.get("Day", {})
        days.append({
            "date": date,
            "dow": _dow(date),
            "emoji": ACCU_ICONS.get(day.get("Icon"), "\U0001f321️"),
            "phrase": day.get("IconPhrase", ""),
            "hi": round(d["Temperature"]["Maximum"]["Value"]),
            "lo": round(d["Temperature"]["Minimum"]["Value"]),
            "precip": day.get("PrecipitationProbability", 0),
        })
    return {"city": cfg["city"], "source": "AccuWeather", "days": days}


def _fetch_open_meteo(cfg):
    """5-day forecast from Open-Meteo (no API key) - same output shape."""
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={cfg['lat']}&longitude={cfg['lon']}"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&temperature_unit=fahrenheit&forecast_days=5"
           f"&timezone={urllib.parse.quote(cfg['tz'])}")
    d = _http_json(url)["daily"]
    days = []
    for i, date in enumerate(d["time"][:5]):
        emoji, phrase = WMO_CODES.get(d["weather_code"][i], ("\U0001f321️", "—"))
        days.append({
            "date": date,
            "dow": _dow(date),
            "emoji": emoji,
            "phrase": phrase,
            "hi": round(d["temperature_2m_max"][i]),
            "lo": round(d["temperature_2m_min"][i]),
            "precip": d["precipitation_probability_max"][i] or 0,
        })
    return {"city": cfg["city"], "source": "Open-Meteo", "days": days}


def _read_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    tmp = CACHE_FILE + f".tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_FILE)


def get_weather(force=False):
    """{company: forecast} for the footer. Serves cache inside the TTL; a
    failed city keeps its last good forecast rather than disappearing."""
    cache = _read_cache()
    now = dt.datetime.now(dt.timezone.utc)
    fetched = cache.get("fetchedAt")
    if not force and fetched and cache.get("weather"):
        try:
            age = (now - dt.datetime.fromisoformat(fetched)).total_seconds()
            if age < CACHE_TTL_HOURS * 3600:
                return cache["weather"]
        except ValueError:
            pass

    api_key = _accu_key()
    location_keys = cache.get("locationKeys", {})
    old = cache.get("weather", {})
    stamp = now.strftime("%Y-%m-%dT%H:%MZ")
    weather = {}
    for co, cfg in CITIES.items():
        try:
            if api_key:
                weather[co] = _fetch_accuweather(co, cfg, api_key, location_keys)
            else:
                weather[co] = _fetch_open_meteo(cfg)
            weather[co]["updatedAt"] = stamp
        except Exception as e:
            print(f"weather: {co} ({cfg['city']}) pull failed: {e}", file=sys.stderr)
            if co in old:
                weather[co] = old[co]  # stale beats blank
    if weather:
        _write_cache({"fetchedAt": now.isoformat(), "locationKeys": location_keys, "weather": weather})
    return weather


if __name__ == "__main__":
    print(json.dumps(get_weather(force="--force" in sys.argv), indent=2))
