"""Thermostat data model."""

from dataclasses import dataclass, field
from typing import Any

from nuheat.config import celsius_to_fahrenheit, nuheat_to_celsius, nuheat_to_fahrenheit

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
EVENT_NAMES = {0: "Morning", 1: "Leave", 2: "Return", 3: "Sleep"}


@dataclass
class Thermostat:
    serial_number: str
    name: str = ""
    group: str = ""
    online: bool = False
    heating: bool = False
    current_temperature_c: float = 0.0
    target_temperature_c: float = 0.0
    min_temperature_c: float = 5.0
    max_temperature_c: float = 69.0
    schedule_mode: int = 0
    schedule_mode_name: str = ""
    hold_until: str | None = None
    firmware: str = ""
    schedules: list[Any] = field(default_factory=list)

    @property
    def current_temperature_f(self) -> float:
        return round(celsius_to_fahrenheit(self.current_temperature_c), 1)

    @property
    def target_temperature_f(self) -> float:
        return round(celsius_to_fahrenheit(self.target_temperature_c), 1)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Thermostat":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict[str, Any]:
        return {
            "serial_number": self.serial_number,
            "name": self.name,
            "group": self.group,
            "online": self.online,
            "heating": self.heating,
            "current_temperature_c": self.current_temperature_c,
            "current_temperature_f": self.current_temperature_f,
            "target_temperature_c": self.target_temperature_c,
            "target_temperature_f": self.target_temperature_f,
            "min_temperature_c": self.min_temperature_c,
            "max_temperature_c": self.max_temperature_c,
            "schedule_mode": self.schedule_mode,
            "schedule_mode_name": self.schedule_mode_name,
            "hold_until": self.hold_until,
            "firmware": self.firmware,
        }

    def get_schedule(self) -> list[dict[str, Any]]:
        """Return the weekly schedule with converted temperatures.

        Returns a list of 7 days (Monday-Sunday), each with their events.
        """
        result = []
        for i, day_data in enumerate(self.schedules):
            day_name = DAY_NAMES[i] if i < len(DAY_NAMES) else f"Day {i}"
            events = []
            for event in day_data.get("Events", []):
                nuheat_temp = event.get("TempFloor", 0)
                events.append({
                    "type": EVENT_NAMES.get(event.get("ScheduleType", -1), "Unknown"),
                    "time": event.get("Clock", ""),
                    "temperature_c": round(nuheat_to_celsius(nuheat_temp), 1),
                    "temperature_f": round(nuheat_to_fahrenheit(nuheat_temp), 1),
                    "active": event.get("Active", False),
                })
            result.append({"day": day_name, "events": events})
        return result
