"""Configuration constants and temperature conversion utilities."""

from enum import IntEnum

# Legacy API
LEGACY_API_URL = "https://mynuheat.com/api"
LEGACY_AUTH_ENDPOINT = f"{LEGACY_API_URL}/authenticate/user"
LEGACY_THERMOSTAT_ENDPOINT = f"{LEGACY_API_URL}/thermostat"

LEGACY_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
    "DNT": "1",
    "Origin": f"{LEGACY_API_URL}",
}

# OAuth2 API
OAUTH2_API_URL = "https://api.mynuheat.com/api/v1"
OAUTH2_IDENTITY_URL = "https://identity.mynuheat.com"
OAUTH2_AUTH_ENDPOINT = f"{OAUTH2_IDENTITY_URL}/connect/authorize"
OAUTH2_TOKEN_ENDPOINT = f"{OAUTH2_IDENTITY_URL}/connect/token"
OAUTH2_SCOPES = "openapi openid profile offline_access"

# Polling and write pipeline timing
DEFAULT_POLL_INTERVAL_SECONDS = 300  # 5 minutes — full background poll cadence
WRITE_DEBOUNCE_SECONDS = 2           # collapse rapid writes to same (serial, action)
VERIFY_DELAY_SECONDS = 15            # wait after upstream ack, then read & compare
VERIFY_RETRY_DELAY_SECONDS = 20      # wait after a verify mismatch, then re-verify
UPSTREAM_RETRY_DELAY_SECONDS = 20    # wait after an upstream POST failure, then re-POST once
HEATING_GRACE_SECONDS = 180          # protect optimistic heating prediction from being overwritten
                                     # by stale NuHeat cloud values during this window after a write


class ScheduleMode(IntEnum):
    RUN = 1
    TEMPORARY_HOLD = 2
    HOLD = 3


class ScheduleType(IntEnum):
    MORNING = 0
    LEAVE = 1
    RETURN = 2
    SLEEP = 3


SCHEDULE_MODE_NAMES = {
    ScheduleMode.RUN: "Running Schedule",
    ScheduleMode.TEMPORARY_HOLD: "Temporary Hold",
    ScheduleMode.HOLD: "Permanent Hold",
}


def fahrenheit_to_nuheat(fahrenheit: float) -> int:
    """Convert Fahrenheit to NuHeat's internal temperature format (legacy API)."""
    return round(((fahrenheit - 33) * 56) + 33)


def nuheat_to_fahrenheit(nuheat_temp: int) -> float:
    """Convert NuHeat's internal temperature format to Fahrenheit (legacy API)."""
    return ((nuheat_temp - 33) / 56) + 33


def celsius_to_fahrenheit(celsius: float) -> float:
    return (celsius * 9 / 5) + 32


def fahrenheit_to_celsius(fahrenheit: float) -> float:
    return (fahrenheit - 32) * 5 / 9


def celsius_to_nuheat(celsius: float) -> int:
    """Convert Celsius to NuHeat's internal temperature format (legacy API)."""
    return fahrenheit_to_nuheat(celsius_to_fahrenheit(celsius))


def nuheat_to_celsius(nuheat_temp: int) -> float:
    """Convert NuHeat's internal temperature format to Celsius (legacy API)."""
    return fahrenheit_to_celsius(nuheat_to_fahrenheit(nuheat_temp))


def oauth2_to_celsius(api_temp: int) -> float:
    """Convert OAuth2 API temperature (Celsius * 100) to Celsius."""
    return api_temp / 100.0


def celsius_to_oauth2(celsius: float) -> int:
    """Convert Celsius to OAuth2 API temperature format (Celsius * 100)."""
    return round(celsius * 100)
