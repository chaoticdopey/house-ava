"""Answer questions from the web: search snippets (your SearXNG if set, else DuckDuckGo),
then let the brain turn them into one or two spoken sentences. Also weather (Open-Meteo, no key).
Adapted from desktop Ava, standalone.
"""

from __future__ import annotations

import datetime as _dt
import html
import re
import time

import requests

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) HouseAva/0.1"}


def _strip(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def search_snippets(query: str, cfg=None, n: int = 6) -> list[dict]:
    """Web results [{title, snippet, url}] from, in order: your SearXNG (if `web.searxng_url`
    is set — the usual path), then Wikipedia + Google News (JSON/RSS APIs that
    don't IP-block, so they work from anywhere), then DuckDuckGo as a last resort."""
    query = (query or "").strip()
    if not query:
        return []
    out = _searxng(query, cfg, n)
    if out:
        return out
    out = _wikipedia(query, 2) + _gnews(query, 3)
    if out:
        return out[:n]
    return _duckduckgo(query, n)


def _searxng(query: str, cfg, n: int) -> list[dict]:
    searx = str((cfg.get("web.searxng_url", "") if cfg else "") or "").strip().rstrip("/")
    if not searx:
        return []
    try:
        r = requests.get(f"{searx}/search", params={"q": query, "format": "json", "language": "en"},
                         headers=_UA, timeout=8)
        if r.status_code == 200:
            return [{"title": _strip(i.get("title", "")), "snippet": _strip(i.get("content", "")),
                     "url": i.get("url", "")} for i in (r.json().get("results") or [])[:n]]
    except Exception:
        pass
    return []


def _wikipedia(query: str, n: int = 2) -> list[dict]:
    """Best Wikipedia pages for a query, with their summary extracts. Great for factual lookups."""
    try:
        r = requests.get("https://en.wikipedia.org/w/api.php", params={
            "action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": n},
            headers=_UA, timeout=8)
        titles = [h["title"] for h in r.json().get("query", {}).get("search", [])][:n]
    except Exception:
        return []
    out = []
    for title in titles:
        try:
            s = requests.get("https://en.wikipedia.org/api/rest_v1/page/summary/" + requests.utils.quote(title),
                             headers=_UA, timeout=8)
            d = s.json() if s.status_code == 200 else {}
            extract = _strip(d.get("extract", ""))
            if extract:
                out.append({"title": title, "snippet": extract,
                            "url": (d.get("content_urls", {}).get("desktop", {}) or {}).get("page", "")})
        except Exception:
            continue
    return out


def _gnews(query: str, n: int = 3) -> list[dict]:
    """Recent headlines from Google News RSS — for current events the model won't know."""
    try:
        url = "https://news.google.com/rss/search?q=" + requests.utils.quote(query) + "&hl=en-US&gl=US&ceid=US:en"
        r = requests.get(url, headers=_UA, timeout=8)
        items = re.findall(r"<item>(.*?)</item>", r.text, re.S)[:n]
    except Exception:
        return []
    out = []
    for it in items:
        tm = re.search(r"<title>(.*?)</title>", it, re.S)
        if not tm:
            continue
        title = _strip(re.sub(r"<!\[CDATA\[|\]\]>", "", tm.group(1)))
        title = re.sub(r"\s+-\s+[^-]+$", "", title)      # drop " - Publisher"
        lm = re.search(r"<link>(.*?)</link>", it, re.S)
        out.append({"title": title, "snippet": title, "url": lm.group(1).strip() if lm else ""})
    return out


def _duckduckgo(query: str, n: int) -> list[dict]:
    try:
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=_UA, timeout=10)
        if r.status_code != 200:
            return []
        out = []
        for b in re.split(r'<div class="result\b', r.text)[1:]:
            if "result--ad" in b[:200]:
                continue
            tm = re.search(r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', b, re.S)
            if not tm:
                continue
            sm = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', b, re.S)
            out.append({"title": _strip(tm.group(2)), "snippet": _strip(sm.group(1)) if sm else "",
                        "url": html.unescape(tm.group(1))})
            if len(out) >= n:
                break
        return out
    except Exception:
        return []


def answer(question: str, cfg, brain, persona_system: str = "", context: str = "", said: str = "") -> dict:
    """Search, then let the brain answer briefly and in character. context = the last few lines of the conversation
    (a follow-up like "what does it mean in Spanish?" is about them); said = their actual words."""
    question = (question or "").strip()
    if not question:
        return {"ok": False, "say": "Look up what, exactly?"}
    snippets = search_snippets(question, cfg)
    if not snippets:
        return {"ok": False, "say": "I couldn't reach the web just now, so I can't look that up."}
    ctx = "\n\n".join(f"[{i + 1}] {s['title']}\n{s['snippet']}" for i, s in enumerate(snippets[:6]))
    sys = persona_system or "You answer questions briefly and factually for a voice assistant."
    prompt = (f"Today is {time.strftime('%A %B %d, %Y')}. Using ONLY these web results, answer in one or two "
              f"short spoken sentences, in your usual voice. If they don't contain the answer, say what they do "
              f"say. Don't mention result numbers or sources."
              + (f"\n\nRECENT CONVERSATION (they may be following up on it):\n{context}" if context else "")
              + (f"\n\nTHEIR WORDS: {said}" if said and said.strip() != question.strip() else "")
              + f"\n\nQUESTION: {question}\n\nRESULTS:\n{ctx}")
    try:
        out = brain.chat(sys, prompt)
        from .persona import unsmug                      # (no "Fucking obvious, right?" on the end of a real answer)
        return {"ok": True, "say": unsmug((out or "").strip()[:500]) or "I found results but couldn't make sense of them."}
    except Exception:
        s = snippets[0]
        return {"ok": True, "say": f"Here's what I found: {s['title']}. {s['snippet'][:240]}"}


_WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "icy fog",
        51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
        71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers", 81: "rain showers",
        82: "heavy rain showers", 95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}


_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# the week ahead: "the 7 day forecast", "this week", "the next few days". And "a Saturday forecast": that's how
# whisper hears "a seven-day forecast" (10-21), and the week has Saturday in it anyway ("the forecast for Saturday"
# and "Saturday's weather" are still just Saturday)
_RX_WEEK = re.compile(r"\b(?:(?:5|7|10|five|seven|ten)[ -]?days?|week(?:ly)?|(?:next|coming) (?:few|several|couple(?: of)?) "
                      r"days|extended|long[ -]range|a saturday forecast)\b", re.I)
_RX_WEEKEND = re.compile(r"\bweekend\b", re.I)
_RX_DAY = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b", re.I)


def forecast_when(text: str) -> str:
    """What a weather request asks about: "today", "tomorrow", "week", "weekend", or a day ("saturday")."""
    low = (text or "").lower()
    if _RX_WEEK.search(low):
        return "week"
    if _RX_WEEKEND.search(low):
        return "weekend"
    m = _RX_DAY.search(low)
    if m:
        return m.group(1)
    return "tomorrow" if "tomorrow" in low else "today"


def _home(cfg):
    """(lat, lon) from location.lat / location.lon, or None. Exact, where a town's name can match a few places."""
    try:
        return float(cfg.get("location.lat")), float(cfg.get("location.lon"))
    except (TypeError, ValueError, AttributeError):
        return None


def weather(cfg, where: str = "", when: str = "today") -> dict:
    """The weather now and for today, tomorrow, a day of the week, the weekend or the week ahead (see forecast_when).
    Where: a place that was named, else home (location.lat/lon, called web.city), else web.city by name, else a
    guess from this box's internet address."""
    city = (where or "").strip()
    home_name = str((cfg.get("web.city", "") if cfg else "") or "").strip()
    try:
        spot = None if city else _home(cfg)
        if spot:
            (lat, lon), label = spot, home_name
        else:
            name = city or home_name
            res = []
            if name:
                r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                                 params={"name": name, "count": 1, "language": "en"}, headers=_UA, timeout=8)
                res = (r.json().get("results") or []) if r.status_code == 200 else []
            if res:
                g = res[0]
                lat, lon, label = float(g["latitude"]), float(g["longitude"]), g.get("name", name)
            elif name:
                return {"ok": False, "say": f"I couldn't find {name} on the map."}
            else:
                r = requests.get("http://ip-api.com/json/?fields=status,city,lat,lon", headers=_UA, timeout=6)
                d = r.json() if r.status_code == 200 else {}
                if d.get("status") != "success":
                    return {"ok": False, "say": "I couldn't work out where to get the weather for."}
                lat, lon, label = float(d["lat"]), float(d["lon"]), d.get("city", "")
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code", "temperature_unit": "fahrenheit",
            "timezone": "auto", "forecast_days": 7}, headers=_UA, timeout=8)
        d = r.json()
    except Exception as exc:
        return {"ok": False, "say": f"The weather service didn't answer: {exc}"}
    cur, daily = d.get("current", {}), d.get("daily", {})
    try:
        days = [_dt.date.fromisoformat(x) for x in daily["time"]]
        hi = [round(x) for x in daily["temperature_2m_max"]]
        lo = [round(x) for x in daily["temperature_2m_min"]]
        sky = [_WMO.get(int(c), "mixed") for c in daily["weather_code"]]
        assert days and len(days) == len(hi) == len(lo) == len(sky)
    except Exception:
        return {"ok": False, "say": "The weather came back garbled."}
    at = f" in {label}" if label else ""

    def deg(n) -> str:
        return f"{n} degree" if abs(n) == 1 else f"{n} degrees"

    def hilo(i: int) -> str:
        return f"high {deg(hi[i])} and low {deg(lo[i])}"

    def day(i: int) -> str:
        return "today" if i == 0 else "tomorrow" if i == 1 else days[i].strftime("%A")

    w = (when or "today").lower().strip()
    if w == "week":
        parts = [f"{day(i).capitalize()} {sky[i]}, {hilo(i)}" for i in range(len(days))]
        return {"ok": True, "say": f"The next {len(days)} days{at}: " + ". ".join(parts) + "."}
    if w == "weekend":
        idx = [i for i, x in enumerate(days) if x.weekday() >= 5 and (x - days[0]).days <= 6 - days[0].weekday()]
        if idx:
            return {"ok": True, "say": f"This weekend{at}: " + ". ".join(
                f"{day(i).capitalize()} {sky[i]}, {hilo(i)}" for i in idx) + "."}
        w = "today"
    if w in _DAYS:
        i = next((i for i, x in enumerate(days) if x.weekday() == _DAYS.index(w)), 0)
        if i:
            name = days[i].strftime("%A") if i > 1 else f"Tomorrow, {days[i].strftime('%A')},"
            return {"ok": True, "say": f"{name}{at}: {sky[i]}, {hilo(i)}."}
        w = "today"
    if w == "tomorrow" and len(days) > 1:
        return {"ok": True, "say": f"Tomorrow{at}: {sky[1]}, {hilo(1)}."}
    if cur:
        now = f"It's {deg(round(cur.get('temperature_2m', 0)))} and {_WMO.get(int(cur.get('weather_code', 0)), 'mixed')}{at}"
        return {"ok": True, "say": f"{now}. {hilo(0).capitalize()}."}
    return {"ok": True, "say": f"Today{at}: {sky[0]}, {hilo(0)}."}
