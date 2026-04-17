"""High-level thermostat manager that wraps an API client."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from nuheat.activity_log import activity_log
from nuheat.api.base import NuHeatAPI
from nuheat.config import MIN_SET_INTERVAL_SECONDS, ScheduleMode
from nuheat.notifications import notifier
from nuheat.thermostat import Thermostat

logger = logging.getLogger(__name__)

MIN_REFRESH_INTERVAL_SECONDS = 60


class ThermostatManager:
    """Manages thermostats through any NuHeatAPI implementation.

    All reads are served from an in-memory cache that is updated only by:
      1. The background poller (every NUHEAT_POLL_INTERVAL seconds)
      2. A targeted refresh after a successful write
      3. An explicit force_refresh() call (throttled to once per 60s)
    """

    def __init__(self, api: NuHeatAPI):
        self._api = api
        self._cache: dict[str, Thermostat] = {}
        self._last_set_time: float = 0
        self._last_refresh_time: float = 0
        self._last_refresh_iso: str = ""
        # Per-thermostat tracking for detailed offline diagnostics
        self._online_since: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}
        self._offline_count: dict[str, int] = {}

    @property
    def api(self) -> NuHeatAPI:
        return self._api

    @property
    def last_updated(self) -> str:
        """ISO timestamp of the last successful cache refresh."""
        return self._last_refresh_iso

    @property
    def last_updated_epoch(self) -> float:
        return self._last_refresh_time

    async def authenticate(self) -> bool:
        start = time.time()
        success = await self._api.authenticate()
        duration_ms = round((time.time() - start) * 1000)
        if success:
            activity_log.log("auth", f"Authentication successful ({duration_ms}ms)",
                             duration_ms=duration_ms)
        else:
            activity_log.log("error", f"Authentication failed ({duration_ms}ms)",
                             duration_ms=duration_ms)
            await notifier.notify("auth_failure", "NuHeat authentication failed")
        return success

    async def refresh(self) -> list[Thermostat]:
        """Fetch all thermostats from NuHeat and update the cache."""
        start = time.time()
        expected_count = len(self._cache) or None

        try:
            data_list = await self._api.get_thermostats()
        except Exception as e:
            duration_ms = round((time.time() - start) * 1000)
            activity_log.log("error", f"Poll failed: {type(e).__name__}: {e}",
                             duration_ms=duration_ms, exception=type(e).__name__)
            raise

        duration_ms = round((time.time() - start) * 1000)
        now = time.time()
        thermostats = []
        missing_serials = set(self._cache.keys())

        for data in data_list:
            t = Thermostat.from_api(data)
            sn = t.serial_number
            missing_serials.discard(sn)
            old = self._cache.get(sn)

            # Track last-seen and online-since
            self._last_seen[sn] = now

            if t.online and sn not in self._online_since:
                self._online_since[sn] = now
            if not t.online:
                self._offline_count[sn] = self._offline_count.get(sn, 0) + 1

            # Transition detection
            if old is not None:
                # Online -> Offline
                if old.online and not t.online:
                    online_duration = now - self._online_since.get(sn, now)
                    online_duration_min = round(online_duration / 60)
                    activity_log.log(
                        "error",
                        f"Thermostat {t.name or sn} went OFFLINE (was online for {online_duration_min}min)",
                        serial=sn,
                        name=t.name,
                        last_temp_c=old.current_temperature_c,
                        last_target_c=old.target_temperature_c,
                        last_heating=old.heating,
                        last_mode=old.schedule_mode_name,
                        firmware=old.firmware,
                        online_duration_minutes=online_duration_min,
                        hold_until=old.hold_until,
                    )
                    self._online_since.pop(sn, None)
                    await notifier.notify(
                        "thermostat_offline",
                        f"{t.name or sn} went offline",
                        f"Serial: {sn}\nWas online for {online_duration_min} min\n"
                        f"Last temp: {old.current_temperature_c:.1f}°C, Heating: {old.heating}",
                    )

                # Offline -> Online
                if not old.online and t.online:
                    self._online_since[sn] = now
                    activity_log.log(
                        "poll",
                        f"Thermostat {t.name or sn} came back ONLINE",
                        serial=sn,
                        name=t.name,
                        current_temp_c=t.current_temperature_c,
                        target_c=t.target_temperature_c,
                        heating=t.heating,
                        firmware=t.firmware,
                    )

                # Heating state changed while online
                if old.online and t.online and old.heating != t.heating:
                    activity_log.log(
                        "poll",
                        f"{t.name or sn} heating: {old.heating} -> {t.heating}",
                        serial=sn,
                        heating=t.heating,
                        current_temp_c=t.current_temperature_c,
                        target_c=t.target_temperature_c,
                    )

            else:
                # First time seeing this thermostat
                status = "online" if t.online else "OFFLINE"
                activity_log.log(
                    "poll",
                    f"First sight of {t.name or sn}: {status}",
                    serial=sn,
                    name=t.name,
                    online=t.online,
                    heating=t.heating,
                    current_temp_c=t.current_temperature_c,
                    target_c=t.target_temperature_c,
                    firmware=t.firmware,
                    mode=t.schedule_mode_name,
                )

            self._cache[sn] = t
            thermostats.append(t)

        # Detect thermostats that didn't come back in this poll (API didn't return them at all)
        for sn in missing_serials:
            last_seen_ago = round(now - self._last_seen.get(sn, now))
            old = self._cache.get(sn)
            activity_log.log(
                "error",
                f"Thermostat {old.name if old else sn} missing from API response "
                f"(last seen {last_seen_ago}s ago)",
                serial=sn,
                last_seen_seconds_ago=last_seen_ago,
            )

        self._mark_refreshed()

        # Summary poll log
        online = sum(1 for t in thermostats if t.online)
        offline = len(thermostats) - online
        activity_log.log(
            "poll",
            f"Poll complete: {online} online, {offline} offline ({duration_ms}ms)",
            count=len(thermostats),
            online=online,
            offline=offline,
            duration_ms=duration_ms,
        )
        return thermostats

    async def force_refresh(self) -> bool:
        """Force a cache refresh, throttled to once per 60 seconds.

        Returns True if a refresh was performed, False if throttled.
        """
        elapsed = time.time() - self._last_refresh_time
        if elapsed < MIN_REFRESH_INTERVAL_SECONDS:
            activity_log.log("refresh", "Force refresh throttled",
                             seconds_since_last=int(elapsed))
            return False
        await self.refresh()
        activity_log.log("refresh", "Force refresh completed")
        return True

    def get_cached(self, serial_number: str) -> Thermostat | None:
        """Get a single thermostat from cache. Never hits NuHeat."""
        return self._cache.get(serial_number)

    def get_all_cached(self) -> list[Thermostat]:
        """Get all thermostats from cache. Never hits NuHeat."""
        return list(self._cache.values())

    async def set_temperature(
        self,
        serial_number: str,
        temperature_c: float,
        hold_until: str | None = None,
    ) -> bool:
        """Set target temperature with throttle protection."""
        elapsed = time.time() - self._last_set_time
        if elapsed < MIN_SET_INTERVAL_SECONDS:
            wait = MIN_SET_INTERVAL_SECONDS - elapsed
            logger.info("Throttling: waiting %.0fs before next set command", wait)
            activity_log.log("write", f"Write throttled for {wait:.0f}s",
                             serial=serial_number, wait_seconds=round(wait))
            await asyncio.sleep(wait)

        mode = ScheduleMode.TEMPORARY_HOLD if hold_until else ScheduleMode.HOLD
        start = time.time()
        success = await self._api.set_thermostat(
            serial_number,
            temperature_celsius=temperature_c,
            schedule_mode=mode,
            hold_until=hold_until,
        )
        duration_ms = round((time.time() - start) * 1000)
        self._last_set_time = time.time()

        if success:
            if serial_number in self._cache:
                self._cache[serial_number].target_temperature_c = temperature_c
                self._cache[serial_number].schedule_mode = mode
                self._cache[serial_number].schedule_mode_name = mode.name.replace("_", " ").title()
            logger.info("Set %s to %.1f°C", serial_number, temperature_c)
            temp_f = temperature_c * 9 / 5 + 32
            activity_log.log("write", f"Set {serial_number} to {temperature_c:.1f}°C / {temp_f:.1f}°F ({duration_ms}ms)",
                             serial=serial_number,
                             temperature_c=round(temperature_c, 2),
                             temperature_f=round(temp_f, 1),
                             mode=mode.name, hold_until=hold_until, duration_ms=duration_ms)
            await self._refresh_one(serial_number)
        else:
            activity_log.log("error", f"Failed to set temperature on {serial_number} ({duration_ms}ms)",
                             serial=serial_number,
                             temperature_c=round(temperature_c, 2),
                             duration_ms=duration_ms)
            await notifier.notify("write_failure",
                                  f"Failed to set temperature on {serial_number}",
                                  f"Target: {temperature_c:.1f}°C")
        return success

    async def resume_schedule(self, serial_number: str) -> bool:
        """Resume the programmed schedule."""
        start = time.time()
        success = await self._api.set_thermostat(
            serial_number, schedule_mode=ScheduleMode.RUN
        )
        duration_ms = round((time.time() - start) * 1000)
        if success:
            if serial_number in self._cache:
                self._cache[serial_number].schedule_mode = ScheduleMode.RUN
                self._cache[serial_number].schedule_mode_name = "Run"
            activity_log.log("write", f"Resumed schedule for {serial_number} ({duration_ms}ms)",
                             serial=serial_number, duration_ms=duration_ms)
            await self._refresh_one(serial_number)
        else:
            await notifier.notify("write_failure", f"Failed to resume schedule for {serial_number}")
            activity_log.log("error", f"Failed to resume schedule for {serial_number} ({duration_ms}ms)",
                             serial=serial_number, duration_ms=duration_ms)
        return success

    async def _refresh_one(self, serial_number: str) -> None:
        """Refresh a single thermostat from NuHeat after a write."""
        try:
            await asyncio.sleep(5)
            data = await self._api.get_thermostat(serial_number)
            if data:
                self._cache[serial_number] = Thermostat.from_api(data)
                self._mark_refreshed()
        except Exception as e:
            logger.exception("Failed to refresh %s after write", serial_number)
            activity_log.log("error", f"Post-write refresh failed for {serial_number}: {e}",
                             serial=serial_number, exception=type(e).__name__)

    def _mark_refreshed(self) -> None:
        self._last_refresh_time = time.time()
        self._last_refresh_iso = datetime.now(timezone.utc).isoformat()

    async def close(self) -> None:
        await self._api.close()
