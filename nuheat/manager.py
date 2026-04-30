"""High-level thermostat manager that wraps an API client."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from nuheat.activity_log import activity_log
from nuheat.api.base import NuHeatAPI
from nuheat.config import (
    SCHEDULE_MODE_NAMES,
    UPSTREAM_RETRY_DELAY_SECONDS,
    VERIFY_DELAY_SECONDS,
    VERIFY_RETRY_DELAY_SECONDS,
    WRITE_DEBOUNCE_SECONDS,
    ScheduleMode,
)
from nuheat.notifications import notifier
from nuheat.thermostat import Thermostat

logger = logging.getLogger(__name__)

MIN_REFRESH_INTERVAL_SECONDS = 60
TEMP_MATCH_TOLERANCE_C = 0.3  # NuHeat round-trips through an integer format


def _mode_name(mode: ScheduleMode) -> str:
    return SCHEDULE_MODE_NAMES.get(mode, mode.name.replace("_", " ").title())


class ThermostatManager:
    """Manages thermostats through any NuHeatAPI implementation.

    All reads are served from an in-memory cache that is updated by:
      1. The background poller (every NUHEAT_POLL_INTERVAL seconds)
      2. Optimistic updates on write entry, then verification reads at
         +15s (and +35s on first mismatch)
      3. An explicit force_refresh() call (throttled to once per 60s)

    Writes are debounced by 2s with last-write-wins per serial. The
    upstream API call fires immediately after the debounce window. A
    verify chain reads the thermostat 15s later to confirm propagation,
    re-verifies once at +20s on mismatch, and gives up after that.
    Upstream POST failures retry once after 20s. The per-thermostat
    `_writeStatus` field surfaces all of this to API consumers.
    """

    def __init__(self, api: NuHeatAPI):
        self._api = api
        self._cache: dict[str, Thermostat] = {}
        self._last_refresh_time: float = 0
        self._last_refresh_iso: str = ""
        # Per-thermostat tracking for detailed offline diagnostics
        self._online_since: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}
        self._offline_count: dict[str, int] = {}

        # Write pipeline state (per-serial, last-write-wins)
        self._pending_writes: dict[str, dict] = {}
        self._versions: dict[str, int] = {}
        self._write_status: dict[str, dict] = {}
        # Serials with a verify chain currently in flight — background poll
        # leaves their cache entry untouched so it can't trample the
        # optimistic value before verification completes.
        self._verify_in_flight: set[str] = set()

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
        """Fetch all thermostats from NuHeat and update the cache.

        Thermostats with a verify chain in flight are NOT overwritten —
        their optimistic cache entry stays in place until the verify chain
        either confirms the write or reconciles to upstream state.
        """
        start = time.time()

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

            self._last_seen[sn] = now

            if t.online and sn not in self._online_since:
                self._online_since[sn] = now
            if not t.online:
                self._offline_count[sn] = self._offline_count.get(sn, 0) + 1

            if old is not None:
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

            # Skip overwriting cache for serials with verify in flight —
            # the optimistic value is more accurate than what NuHeat would
            # return mid-propagation.
            if sn in self._verify_in_flight:
                thermostats.append(self._cache.get(sn) or t)
            else:
                self._cache[sn] = t
                thermostats.append(t)

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

    def get_write_status(self, serial_number: str) -> dict:
        """Return the current write-status object for a thermostat.

        Default is `state="ok"` for any serial that hasn't been written
        to since startup.
        """
        return self._write_status.get(serial_number) or {
            "state": "ok",
            "lastError": None,
            "updatedAt": None,
        }

    # --- Write API (returns immediately; pipeline runs in background) ---

    async def set_temperature(
        self,
        serial_number: str,
        temperature_c: float,
        hold_until: str | None = None,
    ) -> bool:
        """Queue a temperature change. Returns True once the write is
        queued and the optimistic cache is updated. Actual upstream
        propagation and verification happen in a background task; observe
        `_writeStatus` for outcome.
        """
        mode = ScheduleMode.TEMPORARY_HOLD if hold_until else ScheduleMode.HOLD
        payload = {
            "action": "temp",
            "temperature_c": temperature_c,
            "mode": mode,
            "hold_until": hold_until,
        }
        self._queue_write(serial_number, payload)
        return True

    async def resume_schedule(self, serial_number: str) -> bool:
        """Queue a resume-schedule (RUN mode) write. Returns True once
        queued; observe `_writeStatus` for outcome.
        """
        payload = {
            "action": "schedule",
            "mode": ScheduleMode.RUN,
        }
        self._queue_write(serial_number, payload)
        return True

    # --- Internal write pipeline ---

    def _queue_write(self, serial: str, payload: dict) -> None:
        """Stage a write and (re)start the debounce task. Last-write-wins
        per serial — temp and schedule actions share the same queue slot.
        """
        version = self._versions.get(serial, 0) + 1
        self._versions[serial] = version
        self._pending_writes[serial] = payload
        self._apply_optimistic_cache(serial, payload)
        self._set_status(serial, "pending")

        action = payload["action"]
        if action == "temp":
            temp_c = payload["temperature_c"]
            temp_f = temp_c * 9 / 5 + 32
            activity_log.log(
                "write",
                f"Queued {serial} -> {temp_c:.1f}°C / {temp_f:.1f}°F (v{version})",
                serial=serial,
                temperature_c=round(temp_c, 2),
                temperature_f=round(temp_f, 1),
                hold_until=payload.get("hold_until"),
                version=version,
            )
        else:
            activity_log.log(
                "write",
                f"Queued {serial} -> resume schedule (v{version})",
                serial=serial,
                version=version,
            )

        asyncio.create_task(self._run_write_pipeline(serial, version))

    def _apply_optimistic_cache(self, serial: str, payload: dict) -> None:
        cached = self._cache.get(serial)
        if not cached:
            return
        if payload["action"] == "temp":
            cached.target_temperature_c = payload["temperature_c"]
            cached.schedule_mode = payload["mode"]
            cached.schedule_mode_name = _mode_name(payload["mode"])
            cached.hold_until = payload["hold_until"]
        else:
            cached.schedule_mode = ScheduleMode.RUN
            cached.schedule_mode_name = _mode_name(ScheduleMode.RUN)
            cached.hold_until = None

    async def _run_write_pipeline(self, serial: str, version: int) -> None:
        """Debounce -> POST (with one retry on upstream failure) -> verify chain."""
        await asyncio.sleep(WRITE_DEBOUNCE_SECONDS)
        if self._versions.get(serial) != version:
            logger.debug("Write v%d for %s superseded during debounce", version, serial)
            return

        payload = self._pending_writes.get(serial)
        if not payload:
            return

        # Mark verify-in-flight up front so the background poll won't trample
        # the optimistic cache between now and the verify check.
        self._verify_in_flight.add(serial)
        try:
            sent = await self._send_upstream(serial, version, payload)
            if not sent:
                # Upstream POST exhausted — already marked failed inside
                # _send_upstream; nothing else to do.
                return
            # Upstream acked: run verify chain
            await self._verify_chain(serial, version, payload)
        finally:
            # Only the latest version owns the in-flight flag — clearing it
            # if a newer write has superseded would be wrong (newer write
            # holds it), but in that case we returned early above.
            if self._versions.get(serial) == version:
                self._verify_in_flight.discard(serial)

    async def _send_upstream(self, serial: str, version: int, payload: dict) -> bool:
        """Send the write to NuHeat. On failure, wait UPSTREAM_RETRY_DELAY
        and try once more. Returns True if NuHeat acked, False if both
        attempts failed (status set to 'failed' and notification sent).
        """
        success, error_msg = await self._post_once(serial, payload)
        if success:
            return True

        if self._versions.get(serial) != version:
            return False

        self._set_status(serial, "retrying", error_msg or "Upstream POST failed")
        activity_log.log(
            "write",
            f"Upstream POST failed for {serial}, retrying in {UPSTREAM_RETRY_DELAY_SECONDS}s",
            serial=serial, error=error_msg,
        )
        await asyncio.sleep(UPSTREAM_RETRY_DELAY_SECONDS)

        if self._versions.get(serial) != version:
            return False

        success, error_msg = await self._post_once(serial, payload)
        if success:
            return True

        self._set_status(serial, "failed", error_msg or "Upstream POST failed")
        activity_log.log(
            "error",
            f"Upstream POST failed twice for {serial}; giving up",
            serial=serial, error=error_msg,
        )
        await notifier.notify(
            "write_failure",
            f"Failed to reach NuHeat for {serial}",
            f"Last error: {error_msg or 'unknown'}",
        )
        return False

    async def _post_once(self, serial: str, payload: dict) -> tuple[bool, str | None]:
        """Single upstream POST. Returns (success, error_message)."""
        start = time.time()
        try:
            if payload["action"] == "temp":
                ok = await self._api.set_thermostat(
                    serial,
                    temperature_celsius=payload["temperature_c"],
                    schedule_mode=payload["mode"],
                    hold_until=payload["hold_until"],
                )
            else:
                ok = await self._api.set_thermostat(
                    serial, schedule_mode=ScheduleMode.RUN,
                )
        except Exception as e:
            duration_ms = round((time.time() - start) * 1000)
            activity_log.log(
                "error",
                f"Upstream POST raised for {serial}: {type(e).__name__}: {e} ({duration_ms}ms)",
                serial=serial, exception=type(e).__name__, duration_ms=duration_ms,
            )
            return False, f"{type(e).__name__}: {e}"

        duration_ms = round((time.time() - start) * 1000)
        if ok:
            if payload["action"] == "temp":
                temp_c = payload["temperature_c"]
                temp_f = temp_c * 9 / 5 + 32
                activity_log.log(
                    "write",
                    f"Sent {serial} -> {temp_c:.1f}°C / {temp_f:.1f}°F ({duration_ms}ms)",
                    serial=serial,
                    temperature_c=round(temp_c, 2),
                    temperature_f=round(temp_f, 1),
                    mode=payload["mode"].name,
                    hold_until=payload["hold_until"],
                    duration_ms=duration_ms,
                )
            else:
                activity_log.log(
                    "write",
                    f"Sent {serial} -> resume schedule ({duration_ms}ms)",
                    serial=serial, duration_ms=duration_ms,
                )
            return True, None

        return False, f"NuHeat returned non-success ({duration_ms}ms)"

    async def _verify_chain(self, serial: str, version: int, payload: dict) -> None:
        """Read upstream after VERIFY_DELAY and compare. On mismatch, wait
        VERIFY_RETRY_DELAY and verify once more. No re-POST on mismatch —
        we assume NuHeat is just lagging. After the second mismatch, mark
        failed and reconcile cache to actual upstream state.
        """
        self._set_status(serial, "verifying")
        await asyncio.sleep(VERIFY_DELAY_SECONDS)
        if self._versions.get(serial) != version:
            return

        actual = await self._read_for_verify(serial)
        if actual is not None and self._matches(actual, payload):
            self._cache[serial] = actual
            self._mark_refreshed()
            self._set_status(serial, "ok")
            activity_log.log(
                "write",
                f"Verify match for {serial} after {VERIFY_DELAY_SECONDS}s",
                serial=serial,
            )
            return

        # First mismatch (or read failed) — wait and re-verify, no re-POST
        activity_log.log(
            "write",
            f"Verify mismatch for {serial}; re-checking in {VERIFY_RETRY_DELAY_SECONDS}s",
            serial=serial,
            actual_target_c=getattr(actual, "target_temperature_c", None),
            actual_mode=getattr(actual, "schedule_mode_name", None),
        )
        await asyncio.sleep(VERIFY_RETRY_DELAY_SECONDS)
        if self._versions.get(serial) != version:
            return

        actual2 = await self._read_for_verify(serial)
        if actual2 is not None and self._matches(actual2, payload):
            self._cache[serial] = actual2
            self._mark_refreshed()
            self._set_status(serial, "ok")
            activity_log.log(
                "write",
                f"Verify match for {serial} on retry",
                serial=serial,
            )
            return

        # Exhausted — reconcile cache to upstream and notify
        if actual2 is not None:
            self._cache[serial] = actual2
        last_error = "Upstream did not reflect write after retry"
        self._set_status(serial, "failed", last_error)
        activity_log.log(
            "error",
            f"Verify failed for {serial}; cache reconciled to upstream state",
            serial=serial,
            actual_target_c=getattr(actual2, "target_temperature_c", None),
            actual_mode=getattr(actual2, "schedule_mode_name", None),
        )
        detail_lines = [f"Serial: {serial}"]
        if payload["action"] == "temp":
            detail_lines.append(f"Wanted: {payload['temperature_c']:.1f}°C")
            if actual2 is not None:
                detail_lines.append(f"Actual: {actual2.target_temperature_c:.1f}°C")
        else:
            detail_lines.append("Wanted: resume schedule (RUN)")
            if actual2 is not None:
                detail_lines.append(f"Actual mode: {actual2.schedule_mode_name}")
        await notifier.notify(
            "write_failure",
            f"Write to {serial} did not stick",
            "\n".join(detail_lines),
        )

    async def _read_for_verify(self, serial: str) -> Thermostat | None:
        try:
            data = await self._api.get_thermostat(serial)
            if data:
                return Thermostat.from_api(data)
            return None
        except Exception as e:
            activity_log.log(
                "error",
                f"Verify read failed for {serial}: {type(e).__name__}: {e}",
                serial=serial, exception=type(e).__name__,
            )
            return None

    def _matches(self, actual: Thermostat, payload: dict) -> bool:
        if payload["action"] == "temp":
            wanted_temp = payload["temperature_c"]
            wanted_mode = payload["mode"]
            if abs(actual.target_temperature_c - wanted_temp) > TEMP_MATCH_TOLERANCE_C:
                return False
            return int(actual.schedule_mode) == int(wanted_mode)
        # Schedule resume
        return int(actual.schedule_mode) == int(ScheduleMode.RUN)

    def _set_status(self, serial: str, state: str, error: str | None = None) -> None:
        self._write_status[serial] = {
            "state": state,
            "lastError": error,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }

    def _mark_refreshed(self) -> None:
        self._last_refresh_time = time.time()
        self._last_refresh_iso = datetime.now(timezone.utc).isoformat()

    async def close(self) -> None:
        await self._api.close()
