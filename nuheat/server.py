"""FastAPI REST API server for controlling NuHeat thermostats over the network."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from nuheat.api.legacy import LegacyAPI
from nuheat.api.oauth2 import OAuth2API
from nuheat.config import DEFAULT_POLL_INTERVAL_SECONDS, ScheduleMode
from nuheat.manager import ThermostatManager

logger = logging.getLogger(__name__)

manager: ThermostatManager | None = None
_poll_task: asyncio.Task | None = None


def create_manager() -> ThermostatManager:
    """Create the appropriate API client based on environment config."""
    api_type = os.environ.get("NUHEAT_API_TYPE", "legacy").lower()

    if api_type == "oauth2":
        client_id = os.environ.get("NUHEAT_CLIENT_ID", "")
        client_secret = os.environ.get("NUHEAT_CLIENT_SECRET", "")
        redirect_uri = os.environ.get("NUHEAT_REDIRECT_URI", "http://localhost:8787/callback")
        if not client_id or not client_secret:
            raise ValueError("NUHEAT_CLIENT_ID and NUHEAT_CLIENT_SECRET are required for OAuth2")
        api = OAuth2API(client_id, client_secret, redirect_uri)
    else:
        email = os.environ.get("NUHEAT_EMAIL", "")
        password = os.environ.get("NUHEAT_PASSWORD", "")
        if not email or not password:
            raise ValueError("NUHEAT_EMAIL and NUHEAT_PASSWORD are required")
        api = LegacyAPI(email, password)
        serial_numbers = os.environ.get("NUHEAT_SERIAL_NUMBERS", "")
        if serial_numbers:
            api.serial_numbers = [s.strip() for s in serial_numbers.split(",")]

    return ThermostatManager(api)


async def poll_loop() -> None:
    """Background task to periodically refresh thermostat data."""
    interval = int(os.environ.get("NUHEAT_POLL_INTERVAL", DEFAULT_POLL_INTERVAL_SECONDS))
    while True:
        try:
            if manager:
                await manager.refresh()
                logger.debug("Polled %d thermostats", len(manager.get_all_cached()))
        except Exception:
            logger.exception("Error during poll")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager, _poll_task
    manager = create_manager()
    if not await manager.authenticate():
        logger.error("Initial authentication failed")
    else:
        await manager.refresh()
    _poll_task = asyncio.create_task(poll_loop())
    yield
    _poll_task.cancel()
    await manager.close()


app = FastAPI(
    title="NuHeat Thermostat API",
    description="REST API for controlling NuHeat floor heating thermostats",
    version="0.1.0",
    lifespan=lifespan,
)


# --- Request/Response Models ---

class SetTemperatureRequest(BaseModel):
    temperature_c: float | None = Field(None, description="Target temperature in Celsius")
    temperature_f: float | None = Field(None, description="Target temperature in Fahrenheit")
    hold_until: str | None = Field(None, description="ISO datetime for temporary hold (omit for permanent hold)")

    def get_celsius(self) -> float | None:
        if self.temperature_c is not None:
            return self.temperature_c
        if self.temperature_f is not None:
            return (self.temperature_f - 32) * 5 / 9
        return None


class ResumeScheduleRequest(BaseModel):
    pass


class ThermostatResponse(BaseModel):
    serial_number: str
    name: str
    group: str
    online: bool
    heating: bool
    current_temperature_c: float
    current_temperature_f: float
    target_temperature_c: float
    target_temperature_f: float
    min_temperature_c: float
    max_temperature_c: float
    schedule_mode: int
    schedule_mode_name: str
    hold_until: str | None
    firmware: str


class MessageResponse(BaseModel):
    message: str
    success: bool


# --- Endpoints ---

@app.get("/api/thermostats", response_model=list[ThermostatResponse])
async def list_thermostats():
    """List all thermostats with current status."""
    if not manager:
        raise HTTPException(status_code=503, detail="Manager not initialized")
    thermostats = await manager.refresh()
    return [t.to_dict() for t in thermostats]


@app.get("/api/thermostats/{serial_number}", response_model=ThermostatResponse)
async def get_thermostat(serial_number: str):
    """Get a single thermostat's current status."""
    if not manager:
        raise HTTPException(status_code=503, detail="Manager not initialized")
    thermostat = await manager.get_thermostat(serial_number)
    if not thermostat:
        raise HTTPException(status_code=404, detail=f"Thermostat {serial_number} not found")
    return thermostat.to_dict()


@app.put("/api/thermostats/{serial_number}/temperature", response_model=MessageResponse)
async def set_temperature(serial_number: str, req: SetTemperatureRequest):
    """Set the target temperature for a thermostat.

    Provide temperature_c (Celsius) or temperature_f (Fahrenheit).
    Omit hold_until for a permanent hold, or provide an ISO datetime
    for a temporary hold that resumes the schedule at that time.
    """
    if not manager:
        raise HTTPException(status_code=503, detail="Manager not initialized")

    temp_c = req.get_celsius()
    if temp_c is None:
        raise HTTPException(status_code=400, detail="Provide temperature_c or temperature_f")

    success = await manager.set_temperature(serial_number, temp_c, req.hold_until)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to set temperature")

    return {"message": f"Temperature set to {temp_c:.1f}°C", "success": True}


@app.post("/api/thermostats/{serial_number}/resume", response_model=MessageResponse)
async def resume_schedule(serial_number: str):
    """Resume the programmed schedule for a thermostat."""
    if not manager:
        raise HTTPException(status_code=503, detail="Manager not initialized")

    success = await manager.resume_schedule(serial_number)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to resume schedule")

    return {"message": "Schedule resumed", "success": True}


@app.get("/api/health")
async def health():
    """Health check endpoint."""
    cached = manager.get_all_cached() if manager else []
    return {
        "status": "ok",
        "thermostats_cached": len(cached),
        "api_type": os.environ.get("NUHEAT_API_TYPE", "legacy"),
    }
