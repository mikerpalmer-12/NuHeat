"""High-level thermostat manager that wraps an API client."""

import asyncio
import logging
import time
from typing import Any

from nuheat.api.base import NuHeatAPI
from nuheat.config import MIN_SET_INTERVAL_SECONDS, ScheduleMode
from nuheat.thermostat import Thermostat

logger = logging.getLogger(__name__)


class ThermostatManager:
    """Manages thermostats through any NuHeatAPI implementation."""

    def __init__(self, api: NuHeatAPI):
        self._api = api
        self._cache: dict[str, Thermostat] = {}
        self._last_set_time: float = 0

    @property
    def api(self) -> NuHeatAPI:
        return self._api

    async def authenticate(self) -> bool:
        return await self._api.authenticate()

    async def refresh(self) -> list[Thermostat]:
        """Fetch all thermostats and update the cache."""
        data_list = await self._api.get_thermostats()
        thermostats = []
        for data in data_list:
            t = Thermostat.from_api(data)
            self._cache[t.serial_number] = t
            thermostats.append(t)
        return thermostats

    async def get_thermostat(self, serial_number: str) -> Thermostat | None:
        data = await self._api.get_thermostat(serial_number)
        if not data:
            return None
        t = Thermostat.from_api(data)
        self._cache[t.serial_number] = t
        return t

    def get_cached(self, serial_number: str) -> Thermostat | None:
        return self._cache.get(serial_number)

    def get_all_cached(self) -> list[Thermostat]:
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
            # Update cache immediately
            if serial_number in self._cache:
                self._cache[serial_number].target_temperature_c = temperature_c
                self._cache[serial_number].schedule_mode = mode
                self._cache[serial_number].schedule_mode_name = mode.name.replace("_", " ").title()
            logger.info("Set %s to %.1f°C", serial_number, temperature_c)
        return success

    async def resume_schedule(self, serial_number: str) -> bool:
        """Resume the programmed schedule."""
        success = await self._api.set_thermostat(
            serial_number, schedule_mode=ScheduleMode.RUN
        )
        if success and serial_number in self._cache:
            self._cache[serial_number].schedule_mode = ScheduleMode.RUN
            self._cache[serial_number].schedule_mode_name = "Run"
        return success

    async def close(self) -> None:
        await self._api.close()
