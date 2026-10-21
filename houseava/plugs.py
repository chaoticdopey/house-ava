"""Hubspace smart plugs via the Afero cloud.

Hubspace is cloud-only — there is no local LAN path — so this NEEDS the internet and your
Hubspace login (password from the HOUSEAVA_HUBSPACE_PASSWORD env var, never the config file).
Built on `aiohubspace` if it's installed; degrades gracefully to an honest message if not.
Afero rate-limits, so state is cached briefly and we don't poll hard.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

log = logging.getLogger("houseava.plugs")


class Plugs:
    def __init__(self, cfg):
        self.cfg = cfg
        self.enabled = bool(cfg.get("plugs.enabled", True))
        self.devices = dict(cfg.get("plugs.devices", {}) or {})     # friendly -> hubspace name
        self._bridge = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._last_err = ""
        self._state_cache: dict[str, tuple[float, bool]] = {}

    # ---- a private asyncio loop so callers stay synchronous ----
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop and self._loop.is_running():
            return self._loop
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="hubspace")
        self._thread.start()
        return self._loop

    def _run(self, coro, timeout: float = 20.0):
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)

    def available(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "plugs are turned off in the config"
        try:
            import aiohubspace  # noqa: F401
        except Exception:
            return False, "the aiohubspace library isn't installed (pip install aiohubspace)"
        user = str(self.cfg.get("plugs.username", "") or "")
        if not user or not self.cfg.hubspace_password():
            return False, "no Hubspace login set (username in config, password in HOUSEAVA_HUBSPACE_PASSWORD)"
        return True, ""

    async def _bridge_ready(self):
        if self._bridge is not None:
            return self._bridge
        from aiohubspace import HubspaceBridgeV2  # name per aiohubspace; adjust if the API differs

        user = str(self.cfg.get("plugs.username"))
        pw = self.cfg.hubspace_password()
        bridge = HubspaceBridgeV2(user, pw)
        await bridge.initialize()
        self._bridge = bridge
        return bridge

    async def _find(self, hubspace_name: str):
        bridge = await self._bridge_ready()
        for dev in list(getattr(bridge, "devices", []) or []):
            if str(getattr(dev, "friendly_name", getattr(dev, "name", ""))).lower() == hubspace_name.lower():
                return dev
        return None

    def set_state(self, friendly: str, on: bool) -> dict:
        ok, why = self.available()
        if not ok:
            # (said OUT LOUD: never the library / config details - those go in the log; 10-17 "bedroom mic one"
            # got "the aiohubspace library isn't installed (pip install aiohubspace)")
            log.info("plug %s not available: %s", friendly, why)
            return {"ok": False, "say": f"The {friendly} plug isn't set up yet, so I can't switch it."}
        target = self.devices.get(friendly.lower().strip())
        if not target:
            known = ", ".join(self.devices) or "none"
            return {"ok": False, "say": f"I don't have a plug called {friendly}. I have: {known}."}

        async def go():
            dev = await self._find(target)
            if dev is None:
                return False
            # aiohubspace device API: prefer .turn_on()/.turn_off(), else .set_state(power=...)
            if hasattr(dev, "turn_on") and hasattr(dev, "turn_off"):
                await (dev.turn_on() if on else dev.turn_off())
            elif hasattr(dev, "set_state"):
                await dev.set_state(power="on" if on else "off")
            else:
                return False
            return True

        try:
            done = self._run(go())
        except Exception as exc:
            self._last_err = str(exc)
            return {"ok": False, "say": f"The Hubspace cloud didn't answer for the {friendly}."}
        if not done:
            return {"ok": False, "say": f"I couldn't find the {friendly} in your Hubspace account."}
        self._state_cache[friendly.lower()] = (time.time(), on)
        from . import quips
        return {"ok": True, "say": quips.plug_confirm(friendly, on, self.cfg)}
