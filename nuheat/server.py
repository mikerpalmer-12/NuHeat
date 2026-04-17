"""FastAPI REST API server for controlling NuHeat thermostats over the network."""

import asyncio
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

STATIC_DIR = Path(__file__).parent / "static"

from nuheat.activity_log import activity_log
from nuheat.api.legacy import LegacyAPI
from nuheat.api.oauth2 import OAuth2API
from nuheat.config import DEFAULT_POLL_INTERVAL_SECONDS, ScheduleMode
from nuheat.manager import ThermostatManager
from nuheat.notifications import notifier
from nuheat.persistent_config import persistent_config

logger = logging.getLogger(__name__)

manager: ThermostatManager | None = None
_poll_task: asyncio.Task | None = None
_flush_task: asyncio.Task | None = None


# --- Rate Limiter ---


class Settings:
    """Mutable runtime settings. Persistent config overrides .env defaults."""

    def __init__(self):
        pc = persistent_config
        self.poll_interval = pc.get("poll_interval") or int(os.environ.get("NUHEAT_POLL_INTERVAL", DEFAULT_POLL_INTERVAL_SECONDS))
        self.rate_limit_reads = pc.get("rate_limit_reads") or int(os.environ.get("NUHEAT_RATE_LIMIT_READS", "60"))
        self.rate_limit_writes = pc.get("rate_limit_writes") or int(os.environ.get("NUHEAT_RATE_LIMIT_WRITES", "10"))
        self.write_throttle = pc.get("write_throttle") if pc.get("write_throttle") is not None else int(os.environ.get("NUHEAT_WRITE_THROTTLE", "60"))
        self.debug_mode = pc.get("debug_mode") if pc.get("debug_mode") is not None else activity_log.debug_mode
        self.api_logging = pc.get("api_logging", False)
        self.nuheat_api_logging = pc.get("nuheat_api_logging", False)
        # Apply flags from persisted config
        activity_log.debug_mode = self.debug_mode
        activity_log.nuheat_api_logging = self.nuheat_api_logging

    def to_dict(self) -> dict:
        return {
            "poll_interval": self.poll_interval,
            "rate_limit_reads": self.rate_limit_reads,
            "rate_limit_writes": self.rate_limit_writes,
            "write_throttle": self.write_throttle,
            "debug_mode": self.debug_mode,
            "api_logging": self.api_logging,
            "nuheat_api_logging": self.nuheat_api_logging,
        }


settings = Settings()


class RateLimiter:
    """Simple sliding-window rate limiter per IP address."""

    def __init__(self):
        self._read_hits: dict[str, list[float]] = defaultdict(list)
        self._write_hits: dict[str, list[float]] = defaultdict(list)

    def _prune(self, hits: list[float]) -> list[float]:
        cutoff = time.time() - 60
        return [t for t in hits if t > cutoff]

    def check_read(self, ip: str) -> bool:
        self._read_hits[ip] = self._prune(self._read_hits[ip])
        if len(self._read_hits[ip]) >= settings.rate_limit_reads:
            return False
        self._read_hits[ip].append(time.time())
        return True

    def check_write(self, ip: str) -> bool:
        self._write_hits[ip] = self._prune(self._write_hits[ip])
        if len(self._write_hits[ip]) >= settings.rate_limit_writes:
            return False
        self._write_hits[ip].append(time.time())
        return True


rate_limiter = RateLimiter()

WRITE_PATHS = {
    "/api/thermostats/{serial_number}/temperature",
    "/api/thermostats/{serial_number}/resume",
    "/qs/set",
    "/qs/resume",
    "/api/refresh",
}


def _is_write_path(path: str) -> bool:
    if path in ("/qs/set", "/qs/resume", "/api/refresh"):
        return True
    if "/temperature" in path or (path.count("/") >= 3 and path.endswith("/resume")):
        return True
    return False


# --- Manager Setup ---

def create_manager() -> ThermostatManager:
    """Create the appropriate API client. Persistent config overrides .env."""
    pc = persistent_config
    api_type = os.environ.get("NUHEAT_API_TYPE", "legacy").lower()

    if api_type == "oauth2":
        client_id = os.environ.get("NUHEAT_CLIENT_ID", "")
        client_secret = os.environ.get("NUHEAT_CLIENT_SECRET", "")
        redirect_uri = os.environ.get("NUHEAT_REDIRECT_URI", "http://localhost:8787/callback")
        if not client_id or not client_secret:
            raise ValueError("NUHEAT_CLIENT_ID and NUHEAT_CLIENT_SECRET are required for OAuth2")
        api = OAuth2API(client_id, client_secret, redirect_uri)
    else:
        email = pc.get("email") or os.environ.get("NUHEAT_EMAIL", "")
        password = pc.get("password") or os.environ.get("NUHEAT_PASSWORD", "")
        if not email or not password:
            raise ValueError("NUHEAT_EMAIL and NUHEAT_PASSWORD are required")
        api = LegacyAPI(email, password)
        serial_numbers_saved = pc.get("serial_numbers")
        if serial_numbers_saved:
            api.serial_numbers = serial_numbers_saved
        else:
            serial_numbers = os.environ.get("NUHEAT_SERIAL_NUMBERS", "")
            if serial_numbers:
                api.serial_numbers = [s.strip() for s in serial_numbers.split(",")]

    return ThermostatManager(api)


async def poll_loop() -> None:
    """Background task: the ONLY thing that polls NuHeat on a schedule."""
    while True:
        try:
            if manager:
                await manager.refresh()
                logger.debug("Polled %d thermostats", len(manager.get_all_cached()))
        except Exception as e:
            logger.exception("Error during poll")
            await notifier.notify("poll_failure", "Background poll error", str(e))
        await asyncio.sleep(settings.poll_interval)


def restart_poll_loop() -> None:
    """Restart the poll loop (called after settings change)."""
    global _poll_task
    if _poll_task:
        _poll_task.cancel()
    _poll_task = asyncio.create_task(poll_loop())


async def flush_loop() -> None:
    """Background task: periodically flush activity log to disk."""
    while True:
        await asyncio.sleep(30)  # check every 30s
        if activity_log.should_flush():
            count = activity_log.flush()
            if count > 0:
                logger.debug("Flushed %d log entries to disk", count)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager, _poll_task, _flush_task
    # Load notification config from persistent storage
    notifier.load_from_config(persistent_config.get_all())
    manager = create_manager()
    if not await manager.authenticate():
        logger.error("Initial authentication failed")
        await notifier.notify("auth_failure", "NuHeat authentication failed on startup")
    else:
        await manager.refresh()
    _poll_task = asyncio.create_task(poll_loop())
    _flush_task = asyncio.create_task(flush_loop())
    yield
    _poll_task.cancel()
    _flush_task.cancel()
    count = activity_log.flush()
    if count > 0:
        logger.info("Flushed %d log entries on shutdown", count)
    await manager.close()
    await notifier.close()


app = FastAPI(
    title="NuHeat Thermostat API",
    description="REST API for controlling NuHeat floor heating thermostats",
    version="0.2.0",
    lifespan=lifespan,
)


# --- Rate Limiting Middleware ---

STATIC_PATHS = ("/", "/api-reference", "/logs", "/settings", "/docs", "/redoc", "/openapi.json")


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next) -> Response:
    path = request.url.path
    # Skip rate limiting for static pages, docs, and logs
    if path in STATIC_PATHS or path.startswith("/docs/"):
        return await call_next(request)

    ip = request.client.host if request.client else "unknown"
    method = request.method
    start = time.time()

    if _is_write_path(path):
        if not rate_limiter.check_write(ip):
            activity_log.log("rate_limit", f"Write rate limit hit by {ip}",
                             ip=ip, path=path)
            asyncio.create_task(notifier.notify("rate_limit_hit", f"Write rate limit hit by {ip}", f"Path: {path}"))
            return JSONResponse(
                status_code=429,
                content={"detail": f"Write rate limit exceeded ({settings.rate_limit_writes}/min). Try again shortly."},
            )
    else:
        if not rate_limiter.check_read(ip):
            activity_log.log("rate_limit", f"Read rate limit hit by {ip}",
                             ip=ip, path=path)
            asyncio.create_task(notifier.notify("rate_limit_hit", f"Read rate limit hit by {ip}", f"Path: {path}"))
            return JSONResponse(
                status_code=429,
                content={"detail": f"Read rate limit exceeded ({settings.rate_limit_reads}/min). Try again shortly."},
            )

    response = await call_next(request)

    if settings.api_logging and path != "/api/logs":
        duration_ms = round((time.time() - start) * 1000)
        query = str(request.url.query) if request.url.query else ""
        activity_log.log(
            "api_request",
            f"{method} {path} -> {response.status_code} ({duration_ms}ms) from {ip}",
            method=method, path=path, query=query,
            status=response.status_code, duration_ms=duration_ms, ip=ip,
        )

    return response


# --- Frontend ---

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.ico")


@app.get("/icon.png", include_in_schema=False)
async def icon():
    return FileResponse(STATIC_DIR / "icon.png", media_type="image/png")


@app.get("/", include_in_schema=False)
async def dashboard():
    """Serve the web dashboard."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api-reference", include_in_schema=False)
async def api_reference():
    """Serve the API reference page."""
    return FileResponse(STATIC_DIR / "api.html")


@app.get("/logs", include_in_schema=False)
async def logs_page():
    """Serve the activity logs page."""
    return FileResponse(STATIC_DIR / "logs.html")


@app.get("/settings", include_in_schema=False)
async def settings_page():
    """Serve the settings page."""
    return FileResponse(STATIC_DIR / "settings.html")


# --- Settings API ---

class UpdateSettingsRequest(BaseModel):
    poll_interval: int | None = Field(None, ge=30, le=3600, description="Poll interval in seconds (30-3600)")
    rate_limit_reads: int | None = Field(None, ge=1, le=1000, description="Read rate limit per minute per IP (1-1000)")
    rate_limit_writes: int | None = Field(None, ge=1, le=100, description="Write rate limit per minute per IP (1-100)")
    write_throttle: int | None = Field(None, ge=0, le=300, description="Minimum seconds between write commands (0-300)")
    debug_mode: bool | None = Field(None, description="Write every log entry to disk immediately")
    api_logging: bool | None = Field(None, description="Log every API request with method, path, IP, status, and duration")
    nuheat_api_logging: bool | None = Field(None, description="Log every outbound call to the NuHeat API with timing")


@app.get("/api/settings")
async def get_settings():
    """Get current runtime settings."""
    return settings.to_dict()


@app.put("/api/settings")
async def update_settings(req: UpdateSettingsRequest):
    """Update runtime settings. Changes take effect immediately."""
    changes = []

    if req.poll_interval is not None and req.poll_interval != settings.poll_interval:
        old = settings.poll_interval
        settings.poll_interval = req.poll_interval
        restart_poll_loop()
        changes.append(f"poll_interval: {old}s -> {req.poll_interval}s")

    if req.rate_limit_reads is not None and req.rate_limit_reads != settings.rate_limit_reads:
        old = settings.rate_limit_reads
        settings.rate_limit_reads = req.rate_limit_reads
        changes.append(f"rate_limit_reads: {old} -> {req.rate_limit_reads}/min")

    if req.rate_limit_writes is not None and req.rate_limit_writes != settings.rate_limit_writes:
        old = settings.rate_limit_writes
        settings.rate_limit_writes = req.rate_limit_writes
        changes.append(f"rate_limit_writes: {old} -> {req.rate_limit_writes}/min")

    if req.write_throttle is not None and req.write_throttle != settings.write_throttle:
        old = settings.write_throttle
        settings.write_throttle = req.write_throttle
        if manager:
            from nuheat import config
            config.MIN_SET_INTERVAL_SECONDS = req.write_throttle
        changes.append(f"write_throttle: {old}s -> {req.write_throttle}s")

    if req.debug_mode is not None and req.debug_mode != settings.debug_mode:
        old = settings.debug_mode
        settings.debug_mode = req.debug_mode
        activity_log.debug_mode = req.debug_mode
        changes.append(f"debug_mode: {old} -> {req.debug_mode}")

    if req.api_logging is not None and req.api_logging != settings.api_logging:
        old = settings.api_logging
        settings.api_logging = req.api_logging
        changes.append(f"api_logging: {old} -> {req.api_logging}")

    if req.nuheat_api_logging is not None and req.nuheat_api_logging != settings.nuheat_api_logging:
        old = settings.nuheat_api_logging
        settings.nuheat_api_logging = req.nuheat_api_logging
        activity_log.nuheat_api_logging = req.nuheat_api_logging
        changes.append(f"nuheat_api_logging: {old} -> {req.nuheat_api_logging}")

    if changes:
        activity_log.log("settings", "Settings updated: " + ", ".join(changes))
        # Persist all runtime settings
        persistent_config.update({
            "poll_interval": settings.poll_interval,
            "rate_limit_reads": settings.rate_limit_reads,
            "rate_limit_writes": settings.rate_limit_writes,
            "write_throttle": settings.write_throttle,
            "debug_mode": settings.debug_mode,
            "api_logging": settings.api_logging,
            "nuheat_api_logging": settings.nuheat_api_logging,
        })

    return {"settings": settings.to_dict(), "changes": changes}


# --- Account Config API ---

@app.get("/api/account")
async def get_account():
    """Get current NuHeat account configuration (credentials are masked)."""
    if not manager:
        return {"email": "", "serial_numbers": [], "api_type": "legacy", "authenticated": False}

    api = manager.api
    api_type = os.environ.get("NUHEAT_API_TYPE", "legacy").lower()

    if api_type == "legacy":
        from nuheat.api.legacy import LegacyAPI
        if isinstance(api, LegacyAPI):
            email = api._email
            masked = email[0] + "***" + email[email.index("@"):] if "@" in email else "***"
            return {
                "email": masked,
                "serial_numbers": api.serial_numbers,
                "api_type": "legacy",
                "authenticated": api._session_id is not None,
            }

    return {"email": "", "serial_numbers": [], "api_type": api_type, "authenticated": False}


class UpdateAccountRequest(BaseModel):
    email: str | None = Field(None, description="NuHeat account email")
    password: str | None = Field(None, description="NuHeat account password")
    serial_numbers: list[str] | None = Field(None, description="List of thermostat serial numbers")


@app.put("/api/account")
async def update_account(req: UpdateAccountRequest):
    """Update NuHeat credentials and/or serial numbers.

    Changing credentials triggers a full re-authentication and cache refresh.
    Changing serial numbers updates the list and refreshes the cache.
    """
    global manager
    if not manager:
        raise HTTPException(status_code=503, detail="Manager not initialized")

    api = manager.api
    changes = []

    from nuheat.api.legacy import LegacyAPI
    if not isinstance(api, LegacyAPI):
        raise HTTPException(status_code=400, detail="Account changes only supported for legacy API")

    # Credentials change: rebuild the API client
    needs_reauth = False
    if req.email is not None and req.email != api._email:
        api._email = req.email
        api._session_id = None
        needs_reauth = True
        changes.append("email updated")

    if req.password is not None:
        api._password = req.password
        api._session_id = None
        needs_reauth = True
        changes.append("password updated")

    # Serial numbers change
    if req.serial_numbers is not None:
        cleaned = [s.strip() for s in req.serial_numbers if s.strip()]
        old_count = len(api.serial_numbers)
        api.serial_numbers = cleaned
        changes.append(f"serial_numbers: {old_count} -> {len(cleaned)}")

    # Re-authenticate if credentials changed
    auth_ok = True
    if needs_reauth:
        auth_ok = await manager.authenticate()
        if auth_ok:
            changes.append("re-authenticated successfully")
        else:
            changes.append("authentication FAILED - check credentials")

    # Refresh cache with new config
    if auth_ok:
        await manager.refresh()

    if changes:
        activity_log.log("settings", "Account updated: " + ", ".join(changes))
        # Persist account config
        persist = {"serial_numbers": api.serial_numbers}
        if req.email is not None:
            persist["email"] = req.email
        if req.password is not None:
            persist["password"] = req.password
        persistent_config.update(persist)

    return {
        "success": auth_ok,
        "changes": changes,
        "serial_numbers": api.serial_numbers,
    }


# --- Notifications API ---

class UpdateNotificationsRequest(BaseModel):
    app_token: str | None = Field(None, description="Pushover application token")
    users: list[dict[str, str]] | None = Field(None, description='List of {"name": "...", "user_key": "..."}')
    enabled_errors: dict[str, bool] | None = Field(None, description="Which error types trigger notifications")


@app.get("/api/notifications")
async def get_notifications():
    """Get current notification configuration."""
    return notifier.to_display()


@app.put("/api/notifications")
async def update_notifications(req: UpdateNotificationsRequest):
    """Update Pushover notification settings."""
    changes = []

    if req.app_token is not None:
        notifier.app_token = req.app_token
        changes.append("app_token updated")

    if req.users is not None:
        notifier.users = req.users
        changes.append(f"users: {len(req.users)} configured")

    if req.enabled_errors is not None:
        notifier.enabled_errors = req.enabled_errors
        changes.append("enabled_errors updated")

    if changes:
        persistent_config.update(notifier.to_config())
        activity_log.log("settings", "Notifications updated: " + ", ".join(changes))

    return {"changes": changes, "notifications": notifier.to_display()}


@app.post("/api/notifications/test")
async def test_notification(
    user_key: str = Query(..., description="Pushover user key to send test to"),
):
    """Send a test notification to verify Pushover is working."""
    if not notifier.app_token:
        raise HTTPException(status_code=400, detail="Set an app token first")
    success = await notifier.send_test(user_key)
    if not success:
        raise HTTPException(status_code=500, detail="Test notification failed")
    return {"success": True, "message": "Test notification sent"}


# --- Logs API ---

@app.get("/api/logs")
async def get_logs(
    limit: int = Query(100, description="Max entries to return"),
    category: str | None = Query(None, description="Filter by category: auth, poll, read, write, rate_limit, refresh, error"),
):
    """Get activity log entries for troubleshooting."""
    return {
        "entries": activity_log.get_entries(limit=limit, category=category),
        "categories": ["auth", "poll", "write", "rate_limit", "refresh", "error"],
    }


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
    last_updated: str


class MessageResponse(BaseModel):
    message: str
    success: bool


# --- Helper ---

def _thermostat_response(t) -> dict:
    """Build a thermostat response dict with last_updated."""
    d = t.to_dict()
    d["last_updated"] = manager.last_updated if manager else ""
    return d


# --- Read Endpoints (cache only, never hit NuHeat) ---

@app.get("/api/thermostats", response_model=list[ThermostatResponse])
async def list_thermostats():
    """List all thermostats from cache. Data refreshes every 5 minutes."""
    if not manager:
        raise HTTPException(status_code=503, detail="Manager not initialized")
    return [_thermostat_response(t) for t in manager.get_all_cached()]


@app.get("/api/thermostats/{serial_number}", response_model=ThermostatResponse)
async def get_thermostat(serial_number: str):
    """Get a single thermostat from cache."""
    if not manager:
        raise HTTPException(status_code=503, detail="Manager not initialized")
    thermostat = manager.get_cached(serial_number)
    if not thermostat:
        raise HTTPException(status_code=404, detail=f"Thermostat {serial_number} not found")
    return _thermostat_response(thermostat)


@app.get("/api/thermostats/{serial_number}/schedule")
async def get_schedule(serial_number: str):
    """Get the weekly schedule for a thermostat."""
    if not manager:
        raise HTTPException(status_code=503, detail="Manager not initialized")
    thermostat = manager.get_cached(serial_number)
    if not thermostat:
        raise HTTPException(status_code=404, detail=f"Thermostat {serial_number} not found")
    return {
        "serial_number": serial_number,
        "name": thermostat.name,
        "schedule": thermostat.get_schedule(),
    }


# --- Write Endpoints (hit NuHeat, throttled) ---

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


# --- Force Refresh (throttled to once per 60s) ---

@app.post("/api/refresh")
async def force_refresh():
    """Force a cache refresh from NuHeat. Throttled to once per 60 seconds."""
    if not manager:
        raise HTTPException(status_code=503, detail="Manager not initialized")

    refreshed = await manager.force_refresh()
    if not refreshed:
        elapsed = int(time.time() - manager.last_updated_epoch)
        return {
            "refreshed": False,
            "message": f"Throttled. Last refresh was {elapsed}s ago. Try again after 60s.",
            "last_updated": manager.last_updated,
        }

    return {
        "refreshed": True,
        "message": "Cache refreshed from NuHeat",
        "last_updated": manager.last_updated,
        "thermostats": len(manager.get_all_cached()),
    }


# --- Health ---

@app.get("/api/health")
async def health():
    """Health check endpoint."""
    cached = manager.get_all_cached() if manager else []
    return {
        "status": "ok",
        "thermostats_cached": len(cached),
        "api_type": os.environ.get("NUHEAT_API_TYPE", "legacy"),
        "last_updated": manager.last_updated if manager else "",
        "rate_limits": {
            "reads_per_minute": settings.rate_limit_reads,
            "writes_per_minute": settings.rate_limit_writes,
        },
        "poll_interval": settings.poll_interval,
    }


# --- Query String Endpoints (reads from cache, writes are throttled) ---

@app.get("/qs/status")
async def qs_status(
    serial: str | None = Query(None, description="Thermostat serial number (omit for all)"),
):
    """Get thermostat status via query string. Served from cache."""
    if not manager:
        raise HTTPException(status_code=503, detail="Manager not initialized")

    if serial:
        thermostat = manager.get_cached(serial)
        if not thermostat:
            raise HTTPException(status_code=404, detail=f"Thermostat {serial} not found")
        return _thermostat_response(thermostat)
    else:
        return [_thermostat_response(t) for t in manager.get_all_cached()]


@app.get("/qs/set")
async def qs_set(
    serial: str = Query(..., description="Thermostat serial number"),
    temp_c: float | None = Query(None, description="Target temperature in Celsius"),
    temp_f: float | None = Query(None, description="Target temperature in Fahrenheit"),
    hold_until: str | None = Query(None, description="ISO datetime for temporary hold"),
):
    """Set thermostat temperature via query string."""
    if not manager:
        raise HTTPException(status_code=503, detail="Manager not initialized")

    target_c = temp_c
    if target_c is None and temp_f is not None:
        target_c = (temp_f - 32) * 5 / 9
    if target_c is None:
        raise HTTPException(status_code=400, detail="Provide temp_c or temp_f")

    success = await manager.set_temperature(serial, target_c, hold_until)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to set temperature")

    temp_f_display = target_c * 9 / 5 + 32
    return {
        "success": True,
        "message": f"Temperature set to {target_c:.1f}°C / {temp_f_display:.1f}°F",
        "serial": serial,
        "target_temperature_c": round(target_c, 1),
        "target_temperature_f": round(temp_f_display, 1),
        "hold_until": hold_until,
    }


@app.get("/qs/resume")
async def qs_resume(
    serial: str = Query(..., description="Thermostat serial number"),
):
    """Resume the programmed schedule via query string."""
    if not manager:
        raise HTTPException(status_code=503, detail="Manager not initialized")

    success = await manager.resume_schedule(serial)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to resume schedule")

    return {"success": True, "message": "Schedule resumed", "serial": serial}


@app.get("/qs/schedule")
async def qs_schedule(
    serial: str = Query(..., description="Thermostat serial number"),
):
    """Get weekly schedule via query string."""
    if not manager:
        raise HTTPException(status_code=503, detail="Manager not initialized")
    thermostat = manager.get_cached(serial)
    if not thermostat:
        raise HTTPException(status_code=404, detail=f"Thermostat {serial} not found")
    return {
        "serial_number": serial,
        "name": thermostat.name,
        "schedule": thermostat.get_schedule(),
    }
