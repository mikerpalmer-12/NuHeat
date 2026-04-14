"""Pushover push notifications for error alerts.

Sends notifications to one or more Pushover users when configured
error types occur. Each error type can be independently enabled/disabled.
"""

import asyncio
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"

# Error types that can trigger notifications
ERROR_TYPES = {
    "auth_failure": "Authentication with NuHeat failed",
    "write_failure": "Failed to set thermostat temperature or resume schedule",
    "poll_failure": "Background poll encountered an error",
    "rate_limit_hit": "A client exceeded the rate limit",
    "thermostat_offline": "A thermostat went offline",
}

DEFAULT_ENABLED = {
    "auth_failure": True,
    "write_failure": True,
    "poll_failure": True,
    "rate_limit_hit": False,
    "thermostat_offline": True,
}


class PushoverNotifier:
    """Sends push notifications via Pushover."""

    def __init__(self):
        self._app_token: str = ""
        self._users: list[dict[str, str]] = []  # [{"name": "Mike", "user_key": "xxx"}]
        self._enabled_errors: dict[str, bool] = dict(DEFAULT_ENABLED)
        self._http: aiohttp.ClientSession | None = None

    @property
    def configured(self) -> bool:
        return bool(self._app_token and self._users)

    @property
    def app_token(self) -> str:
        return self._app_token

    @app_token.setter
    def app_token(self, value: str) -> None:
        self._app_token = value

    @property
    def users(self) -> list[dict[str, str]]:
        return list(self._users)

    @users.setter
    def users(self, value: list[dict[str, str]]) -> None:
        self._users = list(value)

    @property
    def enabled_errors(self) -> dict[str, bool]:
        return dict(self._enabled_errors)

    @enabled_errors.setter
    def enabled_errors(self, value: dict[str, bool]) -> None:
        self._enabled_errors.update(value)

    def load_from_config(self, data: dict[str, Any]) -> None:
        """Load notification settings from persistent config."""
        self._app_token = data.get("pushover_app_token", "")
        self._users = data.get("pushover_users", [])
        saved_errors = data.get("pushover_enabled_errors", {})
        for key in self._enabled_errors:
            if key in saved_errors:
                self._enabled_errors[key] = saved_errors[key]

    def to_config(self) -> dict[str, Any]:
        """Return config dict for persistence."""
        return {
            "pushover_app_token": self._app_token,
            "pushover_users": self._users,
            "pushover_enabled_errors": self._enabled_errors,
        }

    def to_display(self) -> dict[str, Any]:
        """Return config for API/UI display (token masked)."""
        masked = ""
        if self._app_token:
            masked = self._app_token[:4] + "***" + self._app_token[-4:] if len(self._app_token) > 8 else "***"
        users_display = []
        for u in self._users:
            key = u.get("user_key", "")
            masked_key = key[:4] + "***" + key[-4:] if len(key) > 8 else "***" if key else ""
            users_display.append({"name": u.get("name", ""), "user_key": masked_key})
        return {
            "app_token": masked,
            "app_token_set": bool(self._app_token),
            "users": users_display,
            "enabled_errors": self._enabled_errors,
            "error_descriptions": ERROR_TYPES,
        }

    async def notify(self, error_type: str, message: str, details: str = "") -> None:
        """Send a notification if the error type is enabled."""
        if not self.configured:
            return
        if not self._enabled_errors.get(error_type, False):
            return

        title = f"NuHeat: {ERROR_TYPES.get(error_type, error_type)}"
        body = message
        if details:
            body += f"\n{details}"

        for user in self._users:
            user_key = user.get("user_key", "")
            if not user_key:
                continue
            try:
                await self._send(user_key, title, body)
            except Exception:
                logger.exception("Failed to send Pushover notification to %s", user.get("name", "unknown"))

    async def _send(self, user_key: str, title: str, message: str) -> None:
        if not self._http or self._http.closed:
            self._http = aiohttp.ClientSession()

        payload = {
            "token": self._app_token,
            "user": user_key,
            "title": title,
            "message": message,
            "priority": 0,
        }

        async with self._http.post(PUSHOVER_API_URL, data=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error("Pushover API returned %d: %s", resp.status, body)

    async def send_test(self, user_key: str) -> bool:
        """Send a test notification to verify setup. Returns True on success."""
        if not self._app_token:
            return False
        try:
            if not self._http or self._http.closed:
                self._http = aiohttp.ClientSession()

            payload = {
                "token": self._app_token,
                "user": user_key,
                "title": "NuHeat: Test Notification",
                "message": "If you see this, Pushover notifications are working.",
                "priority": 0,
            }
            async with self._http.post(PUSHOVER_API_URL, data=payload) as resp:
                return resp.status == 200
        except Exception:
            logger.exception("Test notification failed")
            return False

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()


# Singleton
notifier = PushoverNotifier()
