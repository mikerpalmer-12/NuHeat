"""Official NuHeat OAuth2 API client (api.mynuheat.com/api/v1).

This client requires a ClientId and ClientSecret obtained from NuHeat's
developer program. The initial authorization requires a browser-based
login flow. Once authorized, tokens are persisted and refreshed
automatically.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp

from nuheat.api.base import NuHeatAPI
from nuheat.config import (
    OAUTH2_API_URL,
    OAUTH2_AUTH_ENDPOINT,
    OAUTH2_SCOPES,
    OAUTH2_TOKEN_ENDPOINT,
    ScheduleMode,
    oauth2_to_celsius,
    celsius_to_oauth2,
)

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_PATH = Path.home() / ".nuheat" / "oauth2_tokens.json"


class OAuth2API(NuHeatAPI):
    """Client for the official OAuth2 NuHeat API.

    Usage:
        1. Call get_authorization_url() to get the browser login URL
        2. User logs in and is redirected to your redirect_uri with a ?code= param
        3. Call exchange_code(code) with that code
        4. Now get_thermostat / set_thermostat / get_thermostats work
        5. Tokens are persisted to disk and auto-refreshed
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str = "http://localhost:8787/callback",
        token_path: Path = DEFAULT_TOKEN_PATH,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._token_path = token_path

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expiry: float = 0
        self._http: aiohttp.ClientSession | None = None

        self._load_tokens()

    def _load_tokens(self) -> None:
        if self._token_path.exists():
            try:
                data = json.loads(self._token_path.read_text())
                self._access_token = data.get("access_token")
                self._refresh_token = data.get("refresh_token")
                self._token_expiry = data.get("token_expiry", 0)
                logger.info("Loaded saved OAuth2 tokens")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Could not load saved tokens: %s", e)

    def _save_tokens(self) -> None:
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(json.dumps({
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "token_expiry": self._token_expiry,
        }))

    async def _get_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    def get_authorization_url(self, state: str = "nuheat") -> str:
        """Build the URL the user must visit in their browser to authorize."""
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "scope": OAUTH2_SCOPES,
            "redirect_uri": self._redirect_uri,
            "state": state,
        }
        return f"{OAUTH2_AUTH_ENDPOINT}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> bool:
        """Exchange an authorization code for access and refresh tokens."""
        http = await self._get_http()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "response_type": "token",
            "scope": OAUTH2_SCOPES,
            "redirect_uri": self._redirect_uri,
        }
        async with http.post(
            OAUTH2_TOKEN_ENDPOINT,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                logger.error("Token exchange failed: %d", resp.status)
                return False
            tokens = await resp.json()

        self._access_token = tokens["access_token"]
        self._refresh_token = tokens.get("refresh_token")
        self._token_expiry = time.time() + tokens.get("expires_in", 3600) - 60
        self._save_tokens()
        logger.info("OAuth2 tokens acquired and saved")
        return True

    async def _refresh_access_token(self) -> bool:
        if not self._refresh_token:
            logger.error("No refresh token available")
            return False

        http = await self._get_http()
        data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "response_type": "token",
            "scope": OAUTH2_SCOPES,
            "redirect_uri": self._redirect_uri,
            "refresh_token": self._refresh_token,
        }
        async with http.post(
            OAUTH2_TOKEN_ENDPOINT,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                logger.error("Token refresh failed: %d", resp.status)
                return False
            tokens = await resp.json()

        self._access_token = tokens["access_token"]
        if "refresh_token" in tokens:
            self._refresh_token = tokens["refresh_token"]
        self._token_expiry = time.time() + tokens.get("expires_in", 3600) - 60
        self._save_tokens()
        logger.info("OAuth2 tokens refreshed")
        return True

    async def authenticate(self) -> bool:
        """Check if we have valid tokens. Refresh if expired."""
        if self._access_token and time.time() < self._token_expiry:
            return True
        if self._refresh_token:
            return await self._refresh_access_token()
        return False

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make an authenticated API request."""
        if not await self.authenticate():
            logger.error("Not authenticated - run the OAuth2 authorization flow first")
            return None

        http = await self._get_http()
        url = f"{OAUTH2_API_URL}{path}"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        async with http.request(method, url, headers=headers, **kwargs) as resp:
            if resp.status == 401:
                logger.info("Access token rejected, refreshing...")
                if await self._refresh_access_token():
                    headers["Authorization"] = f"Bearer {self._access_token}"
                    async with http.request(method, url, headers=headers, **kwargs) as retry:
                        if retry.status != 200:
                            return None
                        return await retry.json()
                return None
            if resp.status != 200:
                logger.error("API request %s %s failed: %d", method, path, resp.status)
                return None
            return await resp.json()

    async def get_thermostats(self) -> list[dict[str, Any]]:
        raw = await self._request("GET", "/Thermostat")
        if raw is None:
            return []
        return [self._normalize(t) for t in raw]

    async def get_thermostat(self, serial_number: str) -> dict[str, Any]:
        thermostats = await self.get_thermostats()
        for t in thermostats:
            if t["serial_number"] == serial_number:
                return t
        return {}

    async def set_thermostat(
        self,
        serial_number: str,
        temperature_celsius: float | None = None,
        schedule_mode: int | None = None,
        hold_until: str | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"serialNumber": serial_number}

        if temperature_celsius is not None:
            payload["setPointTemp"] = celsius_to_oauth2(temperature_celsius)

        if schedule_mode is not None:
            payload["scheduleMode"] = schedule_mode
        elif temperature_celsius is not None:
            payload["scheduleMode"] = ScheduleMode.HOLD

        result = await self._request("PUT", "/Thermostat", json=payload)
        return result is not None

    async def get_energy_log(
        self, serial_number: str, period: str = "Day", date: str = ""
    ) -> dict[str, Any]:
        """Get energy usage. period: 'Day', 'Week', or 'Month'."""
        path = f"/EnergyLog/{period}/{serial_number}/{date}"
        result = await self._request("GET", path)
        return result or {}

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize OAuth2 API response to the common format."""
        return {
            "serial_number": raw.get("serialNumber", ""),
            "name": raw.get("name", ""),
            "group": "",
            "online": True,
            "heating": raw.get("isHeating", False),
            "current_temperature_c": oauth2_to_celsius(raw.get("currentTemperature", 0)),
            "target_temperature_c": oauth2_to_celsius(raw.get("setPointTemp", 0)),
            "min_temperature_c": 5.0,
            "max_temperature_c": 69.0,
            "schedule_mode": raw.get("operatingMode", 0),
            "schedule_mode_name": _operating_mode_name(raw.get("operatingMode", 0)),
            "hold_until": None,
            "firmware": "",
            "schedules": [],
        }


def _operating_mode_name(mode: int) -> str:
    names = {1: "Heat", 2: "Off"}
    return names.get(mode, f"Unknown ({mode})")
