"""Abstract base class for NuHeat API clients."""

from abc import ABC, abstractmethod
from typing import Any


class NuHeatAPI(ABC):
    """Interface that both Legacy and OAuth2 API clients implement."""

    @abstractmethod
    async def authenticate(self) -> bool:
        """Authenticate with the NuHeat API. Returns True on success."""
        ...

    @abstractmethod
    async def get_thermostat(self, serial_number: str) -> dict[str, Any]:
        """Get thermostat status. Returns normalized thermostat data."""
        ...

    @abstractmethod
    async def set_thermostat(
        self,
        serial_number: str,
        temperature_celsius: float | None = None,
        schedule_mode: int | None = None,
        hold_until: str | None = None,
    ) -> bool:
        """Set thermostat state. Returns True on success."""
        ...

    @abstractmethod
    async def get_thermostats(self) -> list[dict[str, Any]]:
        """List all thermostats. Returns list of normalized thermostat data."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources."""
        ...
