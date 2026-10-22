"""The weather: today / tomorrow / a day of the week / the weekend / the week ahead, what each request asks for, and
where "home" is. No network: the weather service is faked.

    .venv/Scripts/python.exe tests/test_weather.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOUSEAVA_DIR"] = tempfile.mkdtemp(prefix="houseava-weather-")
os.environ["HOUSEAVA_FAKE_BULBS"] = "1"   # never switch real bulbs from a test

from houseava import web  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("[ok] " if cond else "[FAIL] ") + name + ("" if cond else f"   [{extra}]"))
    if not cond:
        fails.append(name)


# ---- what a request asks for ----
ASKS = [
    ("give me a saturday forecast", "week"),               # (whisper's "give me a seven-day forecast", 10-21)
    ("give me a 7 day forecast", "week"),
    ("what's the seven-day forecast", "week"),
    ("what's the weather this week", "week"),
    ("what's the weather going to be the next few days", "week"),
    ("what's the forecast for saturday", "saturday"),
    ("what's saturday's weather", "saturday"),
    ("is it going to rain on friday", "friday"),
    ("what's the weather this weekend", "weekend"),
    ("what's the weather tomorrow", "tomorrow"),
    ("what's the weather", "today"),
    ("is it going to rain tonight", "today"),
]
for text, want in ASKS:
    check(f"asks: {text!r} -> {want}", web.forecast_when(text) == want, web.forecast_when(text))

# ---- the answers, from a faked week: Wed 21 Oct 2026 .. Tue 27 Oct ----
DAYS = ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27"]
FORECAST = {"current": {"temperature_2m": 63.2, "weather_code": 65},
            "daily": {"time": DAYS, "temperature_2m_max": [66, 64, 70, 72, 71, 68, 65],
                      "temperature_2m_min": [60, 55, 50, 51, 52, 49, 48], "weather_code": [65, 61, 0, 1, 2, 3, 80]}}
calls = []


class Resp:
    def __init__(self, data, code=200):
        self.data, self.status_code = data, code

    def json(self):
        return self.data


def fake_get(url, params=None, headers=None, timeout=None):
    calls.append((url, dict(params or {})))
    if "geocoding" in url:
        if (params or {}).get("name") == "Nowhereville":
            return Resp({"results": []})
        return Resp({"results": [{"name": "Springfield", "latitude": 39.8, "longitude": -89.6}]})
    if "ip-api" in url:
        return Resp({"status": "success", "city": "Guessville", "lat": 1.0, "lon": 2.0})
    return Resp(FORECAST)


web.requests.get = fake_get


class Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


home = Cfg({"location.lat": 40.7128, "location.lon": -74.0060, "web.city": "New York"})

calls.clear()
say = web.weather(home, when="week")["say"]
check("week: all 7 days, by name, in degrees",
      say.startswith("The next 7 days in New York: Today heavy rain, high 66 degrees and low 60 degrees.")
      and "Tomorrow light rain, high 64 degrees and low 55 degrees." in say
      and "Saturday mostly clear, high 72 degrees and low 51 degrees." in say
      and say.endswith("Tuesday rain showers, high 65 degrees and low 48 degrees."), say)
check("week: home is location.lat/lon (no name lookup)", not any("geocoding" in u for u, _ in calls)
      and calls[-1][1]["latitude"] == 40.7128 and calls[-1][1]["forecast_days"] == 7, calls)
say = web.weather(home, when="saturday")["say"]
check("saturday: Saturday's own forecast",
      say == "Saturday in New York: mostly clear, high 72 degrees and low 51 degrees.", say)
say = web.weather(home, when="thursday")["say"]
check("the day after today by name says it's tomorrow", say.startswith("Tomorrow, Thursday, in New York"), say)
say = web.weather(home, when="wednesday")["say"]
check("today by name = today's answer, with the current temperature",
      say.startswith("It's 63 degrees and heavy rain in New York"), say)
say = web.weather(home, when="weekend")["say"]
check("weekend: Saturday and Sunday", say == (
      "This weekend in New York: Saturday mostly clear, high 72 degrees and low 51 degrees. "
      "Sunday partly cloudy, high 71 degrees and low 52 degrees."), say)
say = web.weather(home, when="tomorrow")["say"]
check("tomorrow", say == "Tomorrow in New York: light rain, high 64 degrees and low 55 degrees.", say)
say = web.weather(home)["say"]
check("today: now + the high and low",
      say == "It's 63 degrees and heavy rain in New York. High 66 degrees and low 60 degrees.", say)

calls.clear()
say = web.weather(home, where="Springfield", when="today")["say"]
check("a named place is looked up by name, not home", "in Springfield" in say and any("geocoding" in u for u, _ in calls),
      say)
calls.clear()
say = web.weather(Cfg({"web.city": "Springfield"}))["say"]
check("no location set: web.city by name", "in Springfield" in say and any("geocoding" in u for u, _ in calls), say)
calls.clear()
say = web.weather(Cfg({}))["say"]
check("nothing set: a guess from the internet address", "in Guessville" in say and any("ip-api" in u for u, _ in calls),
      say)
say = web.weather(Cfg({"location.lat": 40.7, "location.lon": -74.0}))["say"]
check("location with no city name: no 'in ...'",
      say == "It's 63 degrees and heavy rain. High 66 degrees and low 60 degrees.", say)

FORECAST["daily"]["temperature_2m_min"][1] = 1
say = web.weather(home, when="tomorrow")["say"]
check("one degree, not 'one degrees'", say.endswith("low 1 degree."), say)
FORECAST["daily"]["temperature_2m_min"][1] = 55

calls.clear()
say = web.weather(home, where="Nowhereville")["say"]
check("a named place that isn't on the map says so (no guess from the IP)",
      say == "I couldn't find Nowhereville on the map." and not any("ip-api" in u for u, _ in calls), (say, calls))

# "this weekend" is THIS week's Saturday and Sunday: asked on a Sunday it's today, on a Saturday today + tomorrow
HOME = home["web.city"]
FORECAST["daily"]["time"] = ["2026-10-25", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30",
                             "2026-10-31"]
say = web.weather(home, when="weekend")["say"]
check("weekend asked on a Sunday: just today (not next Saturday)", say.startswith(f"This weekend in {HOME}: Today")
      and "Saturday" not in say, say)
FORECAST["daily"]["time"] = ["2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29",
                             "2026-10-30"]
say = web.weather(home, when="weekend")["say"]
check("weekend asked on a Saturday: today and tomorrow", say.startswith(f"This weekend in {HOME}: Today")
      and ". Tomorrow " in say, say)
FORECAST["daily"]["time"] = DAYS

# the place in a weather question
from houseava.brain import _weather_place  # noqa: E402

ROOMS = ["kitchen", "bedroom", "living room"]
for text, want in (("what's the weather in chicago", "chicago"), ("is it going to rain in fargo tomorrow", "fargo"),
                   ("what's the forecast for saturday in chicago", "chicago"), ("weather in new york city", "new york city"),
                   ("what's the weather in paris this weekend, ava", "paris"), ("whats the weather in st. louis", "st. louis"),
                   ("what's the forecast for this weekend", ""), ("weather for the next 7 days", ""),
                   ("what's the weather like in here", ""), ("how hot is it at home", ""),
                   ("what's the weather in the bedroom", ""), ("give me a saturday forecast", ""),
                   ("what's the weather in my town", "")):
    check(f"place: {text!r} -> {want!r}", _weather_place(text, ROOMS) == want, _weather_place(text, ROOMS))

FORECAST["daily"]["time"] = DAYS[:3]
say = web.weather(home, when="week")["say"]
check("a short forecast is garbled, not a crash", say == "The weather came back garbled.", say)
FORECAST["daily"]["time"] = DAYS

# ---- the router hands the request through ----
from houseava.app import build_router  # noqa: E402
from houseava.config import Config  # noqa: E402

asked = []
real = web.weather
web.weather = lambda cfg, where="", when="today": (asked.append((where, when)) or {"ok": True, "say": "fake"})
try:
    r = build_router(Config(path=os.path.join(os.environ["HOUSEAVA_DIR"], "none.json")))
    for text, want in (("Give me a Saturday forecast.", ("", "week")), ("What's the weather on Saturday?", ("", "saturday")),
                       ("What's the weather tomorrow?", ("", "tomorrow")),
                       ("What's the weather in Chicago this weekend?", ("chicago", "weekend"))):
        asked.clear()
        res = r._fast(text, "kitchen", heard="wake")
        check(f"router: {text!r} asks the weather for {want}", asked == [want] and res and res.get("say") == "fake",
              (asked, res))
finally:
    web.weather = real

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
