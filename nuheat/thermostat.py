"""Thermostat data model."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from nuheat.config import (
    ScheduleMode,
    celsius_to_fahrenheit,
    nuheat_to_celsius,
    nuheat_to_fahrenheit,
)

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
EVENT_NAMES = {0: "Morning", 1: "Leave", 2: "Return", 3: "Sleep"}
# NuHeat schedule index 0 = Monday, Python weekday() 0 = Monday
WEEKDAY_TO_SCHEDULE_INDEX = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}


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

    def get_hold_info(self) -> dict[str, Any]:
        """Compute hold status based on schedule mode and schedule data."""
        if self.schedule_mode == ScheduleMode.RUN:
            return {"status": "schedule", "description": "Running Schedule"}

        if self.schedule_mode == ScheduleMode.HOLD:
            return {"status": "permanent_hold", "description": "Permanent Hold"}

        if self.schedule_mode == ScheduleMode.TEMPORARY_HOLD:
            next_event = self._find_next_event()
            if next_event:
                return {
                    "status": "temporary_hold",
                    "description": f"Hold until {next_event['day']} {next_event['time_12h']} ({next_event['event_type']})",
                    "next_event_day": next_event["day"],
                    "next_event_time": next_event["time"],
                    "next_event_time_12h": next_event["time_12h"],
                    "next_event_type": next_event["event_type"],
                    "next_event_datetime": next_event["datetime_iso"],
                }
            return {"status": "temporary_hold", "description": "Temporary Hold"}

        return {"status": "unknown", "description": self.schedule_mode_name}

    def _find_next_event(self) -> dict[str, Any] | None:
        """Find the next active scheduled event from now."""
        if not self.schedules or len(self.schedules) < 7:
            return None

        now = datetime.now()
        current_weekday = now.weekday()  # 0=Monday

        # Search up to 7 days ahead
        for day_offset in range(8):
            check_weekday = (current_weekday + day_offset) % 7
            day_data = self.schedules[check_weekday]
            events = day_data.get("Events", [])

            for event in sorted(events, key=lambda e: e.get("Clock", "")):
                if not event.get("Active", False):
                    continue

                clock = event.get("Clock", "")
                if not clock:
                    continue

                parts = clock.split(":")
                if len(parts) < 2:
                    continue

                hour, minute = int(parts[0]), int(parts[1])
                event_date = now.date() + timedelta(days=day_offset)
                event_dt = datetime(event_date.year, event_date.month, event_date.day,
                                    hour, minute)

                if event_dt > now:
                    day_name = DAY_NAMES[check_weekday]
                    event_type = EVENT_NAMES.get(event.get("ScheduleType", -1), "Unknown")
                    return {
                        "day": day_name,
                        "time": clock,
                        "time_12h": _format_12h(hour, minute),
                        "event_type": event_type,
                        "datetime_iso": event_dt.isoformat(),
                    }

        return None

    def to_dict(self) -> dict[str, Any]:
        hold_info = self.get_hold_info()
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
            "hold_info": hold_info,
            "firmware": self.firmware,
        }

    def get_schedule(self) -> list[dict[str, Any]]:
        """Return the weekly schedule with converted temperatures."""
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


def _format_12h(hour: int, minute: int) -> str:
    ampm = "AM" if hour < 12 else "PM"
    h = hour if hour <= 12 else hour - 12
    if h == 0:
        h = 12
    return f"{h}:{minute:02d} {ampm}"
