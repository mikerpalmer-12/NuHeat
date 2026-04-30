"""Legacy NuHeat API client (mynuheat.com/api)."""

import logging
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

from nuheat.activity_log import activity_log
from nuheat.api.base import NuHeatAPI
from nuheat.config import (
    LEGACY_AUTH_ENDPOINT,
    LEGACY_HEADERS,
    LEGACY_THERMOSTAT_ENDPOINT,
    ScheduleMode,
    nuheat_to_celsius,
    celsius_to_nuheat,
)

logger = logging.getLogger(__name__)


class LegacyAPI(NuHeatAPI):
    """Client for the legacy mynuheat.com/api."""

    def __init__(self, email: str, password: str):
        self._email = email
        self._password = password
        self._session_id: str | None = None
        self._http: aiohttp.ClientSession | None = None
        self._serial_numbers: list[str] = []

    @property
    def serial_numbers(self) -> list[str]:
        return list(self._serial_numbers)

    @serial_numbers.setter
    def serial_numbers(self, value: list[str]) -> None:
        self._serial_numbers = list(value)

    async def _get_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(headers=LEGACY_HEADERS)
        return self._http

    async def authenticate(self) -> bool:
        http = await self._get_http()
        data = urlencode({
            "Email": self._email,
            "Password": self._password,
            "application": "0",
        })
        start = time.time()
        try:
            async with http.post(LEGACY_AUTH_ENDPOINT, data=data) as resp:
                status = resp.status
                duration_ms = round((time.time() - start) * 1000)
                body_text = await resp.text()
                activity_log.log(
                    "nuheat_api",
                    f"NuHeat API POST /authenticate/user -> {status} ({duration_ms}ms)",
                    method="POST", path="authenticate/user",
                    duration_ms=duration_ms, status=status,
                )
                if status != 200:
                    logger.error("Authentication failed with status %d", status)
                    activity_log.log(
                        "error",
                        f"Auth HTTP {status} from NuHeat",
                        http_status=status,
                        body_preview=body_text[:200],
                    )
                    return False
                try:
                    result = await resp.json(content_type=None)
                except Exception:
                    activity_log.log(
                        "error",
                        "Auth response was not JSON",
                        body_preview=body_text[:200],
                    )
                    return False
        except aiohttp.ClientError as e:
            activity_log.log("error", f"Auth network error: {type(e).__name__}: {e}",
                             exception=type(e).__name__)
            return False

        if result.get("ErrorCode", -1) != 0 or not result.get("SessionId"):
            ec = result.get("ErrorCode")
            logger.error("Authentication failed: ErrorCode=%s", ec)
            activity_log.log("error", f"Auth rejected by NuHeat (ErrorCode={ec})",
                             error_code=ec)
            return False

        self._session_id = result["SessionId"]
        logger.info("Authenticated as %s", result.get("Email", self._email))
        return True

    async def _request(
        self, method: str, url: str, retry: bool = True, **kwargs: Any
    ) -> dict[str, Any] | None:
        """Make an authenticated request, re-authenticating on 401."""
        if not self._session_id:
            if not await self.authenticate():
                return None

        http = await self._get_http()
        sep = "&" if "?" in url else "?"
        auth_url = f"{url}{sep}sessionid={self._session_id}"
        path = url.split("?")[0].replace("https://mynuheat.com/api/", "")
        serial = self._extract_serial(url)
        start = time.time()

        try:
            async with http.request(method, auth_url, **kwargs) as resp:
                status = resp.status
                duration_ms = round((time.time() - start) * 1000)

                if status == 401 and retry:
                    logger.info("Session expired, re-authenticating...")
                    activity_log.log(
                        "auth",
                        f"Session expired on {method} /{path} (HTTP 401) - re-authenticating",
                        method=method, path=path, serial=serial,
                    )
                    self._session_id = None
                    return await self._request(method, url, retry=False, **kwargs)

                if status != 200:
                    body_text = await resp.text()
                    logger.error("Request %s %s failed with status %d", method, url, status)
                    activity_log.log(
                        "error",
                        f"NuHeat API {method} /{path} returned HTTP {status} ({duration_ms}ms)",
                        http_status=status,
                        method=method,
                        path=path,
                        serial=serial,
                        duration_ms=duration_ms,
                        body_preview=body_text[:200],
                    )
                    return None

                result = await resp.json(content_type=None)

                msg = f"NuHeat API {method} /{path} -> 200 ({duration_ms}ms)"
                if serial:
                    msg += f" [{serial}]"
                activity_log.log(
                    "nuheat_api",
                    msg,
                    method=method,
                    path=path,
                    serial=serial,
                    duration_ms=duration_ms,
                    status=200,
                )

                return result
        except aiohttp.ClientError as e:
            duration_ms = round((time.time() - start) * 1000)
            activity_log.log(
                "error",
                f"NuHeat API network error: {type(e).__name__}: {e} ({duration_ms}ms)",
                method=method,
                path=path,
                serial=serial,
                duration_ms=duration_ms,
                exception=type(e).__name__,
            )
            return None
        except Exception as e:
            duration_ms = round((time.time() - start) * 1000)
            activity_log.log(
                "error",
                f"NuHeat API unexpected error: {type(e).__name__}: {e} ({duration_ms}ms)",
                method=method,
                path=path,
                serial=serial,
                duration_ms=duration_ms,
                exception=type(e).__name__,
            )
            return None

    @staticmethod
    def _extract_serial(url: str) -> str:
        """Pull the serialnumber query parameter out of a URL for logging."""
        if "serialnumber=" not in url:
            return ""
        try:
            return url.split("serialnumber=")[1].split("&")[0]
        except (IndexError, ValueError):
            return ""

    async def get_thermostat(self, serial_number: str) -> dict[str, Any]:
        url = f"{LEGACY_THERMOSTAT_ENDPOINT}?serialnumber={serial_number}"
        raw = await self._request("GET", url)
        if raw is None:
            return {}
        return self._normalize(raw)

    async def set_thermostat(
        self,
        serial_number: str,
        temperature_celsius: float | None = None,
        schedule_mode: int | None = None,
        hold_until: str | None = None,
    ) -> bool:
        url = f"{LEGACY_THERMOSTAT_ENDPOINT}?serialnumber={serial_number}"
        params: dict[str, Any] = {}

        if temperature_celsius is not None:
            params["SetPointTemp"] = celsius_to_nuheat(temperature_celsius)

        if schedule_mode is not None:
            params["ScheduleMode"] = schedule_mode
        elif temperature_celsius is not None:
            if hold_until:
                params["ScheduleMode"] = ScheduleMode.TEMPORARY_HOLD
                params["HoldSetPointDateTime"] = hold_until
            else:
                params["ScheduleMode"] = ScheduleMode.HOLD

        if not params:
            return False

        data = urlencode(params)
        result = await self._request("POST", url, data=data)
        return result is not None

    async def get_thermostats(self) -> list[dict[str, Any]]:
        """Fetch status for all configured serial numbers.

        The legacy API has no list-all endpoint, so we query each
        serial number individually.
        """
        results = []
        for sn in self._serial_numbers:
            thermostat = await self.get_thermostat(sn)
            if thermostat:
                results.append(thermostat)
        return results

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize legacy API response to a common format."""
        return {
            "serial_number": raw.get("SerialNumber", ""),
            "name": raw.get("Room", ""),
            "group": raw.get("GroupName", ""),
            "online": raw.get("Online", False),
            "heating": raw.get("Heating", False),
            "current_temperature_c": round(nuheat_to_celsius(raw.get("Temperature", 0)), 1),
            "target_temperature_c": round(nuheat_to_celsius(raw.get("SetPointTemp", 0)), 1),
            "min_temperature_c": round(nuheat_to_celsius(raw.get("MinTemp", 500)), 1),
            "max_temperature_c": round(nuheat_to_celsius(raw.get("MaxTemp", 7000)), 1),
            "schedule_mode": raw.get("ScheduleMode", 0),
            "schedule_mode_name": _schedule_mode_name(raw.get("ScheduleMode", 0)),
            "hold_until": raw.get("HoldSetPointDateTime") if raw.get("ScheduleMode") == ScheduleMode.TEMPORARY_HOLD else None,
            "firmware": raw.get("SWVersion", ""),
            "schedules": raw.get("Schedules", []),
        }


def _schedule_mode_name(mode: int) -> str:
    try:
        return ScheduleMode(mode).name.replace("_", " ").title()
    except ValueError:
        return f"Unknown ({mode})"
