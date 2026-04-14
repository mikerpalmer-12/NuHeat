"""High-level thermostat manager that wraps an API client."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from nuheat.api.base import NuHeatAPI
from nuheat.config import MIN_SET_INTERVAL_SECONDS, ScheduleMode
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
        return await self._api.authenticate()

    async def refresh(self) -> list[Thermostat]:
        """Fetch all thermostats from NuHeat and update the cache.

        Called by the background poller. No throttle here since the poller
        already runs on a fixed interval.
        """
        data_list = await self._api.get_thermostats()
        thermostats = []
        for data in data_list:
            t = Thermostat.from_api(data)
            self._cache[t.serial_number] = t
            thermostats.append(t)
        self._mark_refreshed()
        return thermostats

    async def force_refresh(self) -> bool:
        """Force a cache refresh, throttled to once per 60 seconds.

        Returns True if a refresh was performed, False if throttled.
        """
        elapsed = time.time() - self._last_refresh_time
        if elapsed < MIN_REFRESH_INTERVAL_SECONDS:
            return False
        await self.refresh()
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
            await asyncio.sleep(wait)

        mode = ScheduleMode.TEMPORARY_HOLD if hold_until else ScheduleMode.HOLD
        success = await self._api.set_thermostat(
            serial_number,
            temperature_celsius=temperature_c,
            schedule_mode=mode,
            hold_until=hold_until,
        )
        self._last_set_time = time.time()

        if success:
            # Update cache optimistically
            if serial_number in self._cache:
                self._cache[serial_number].target_temperature_c = temperature_c
                self._cache[serial_number].schedule_mode = mode
                self._cache[serial_number].schedule_mode_name = mode.name.replace("_", " ").title()
            logger.info("Set %s to %.1f°C", serial_number, temperature_c)
            # Targeted refresh after write to confirm state
            await self._refresh_one(serial_number)
        return success

    async def resume_schedule(self, serial_number: str) -> bool:
        """Resume the programmed schedule."""
        success = await self._api.set_thermostat(
            serial_number, schedule_mode=ScheduleMode.RUN
        )
        if success:
            if serial_number in self._cache:
                self._cache[serial_number].schedule_mode = ScheduleMode.RUN
                self._cache[serial_number].schedule_mode_name = "Run"
            # Targeted refresh after write
            await self._refresh_one(serial_number)
        return success

    async def _refresh_one(self, serial_number: str) -> None:
        """Refresh a single thermostat from NuHeat after a write."""
        try:
            await asyncio.sleep(2)  # NuHeat needs a moment to reflect changes
            data = await self._api.get_thermostat(serial_number)
            if data:
                self._cache[serial_number] = Thermostat.from_api(data)
                self._mark_refreshed()
        except Exception:
            logger.exception("Failed to refresh %s after write", serial_number)

    def _mark_refreshed(self) -> None:
        self._last_refresh_time = time.time()
        self._last_refresh_iso = datetime.now(timezone.utc).isoformat()

    async def close(self) -> None:
        await self._api.close()
