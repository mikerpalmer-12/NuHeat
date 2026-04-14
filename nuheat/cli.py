"""CLI for controlling NuHeat thermostats."""

import argparse
import asyncio
import os
import sys

from nuheat.api.legacy import LegacyAPI
from nuheat.api.oauth2 import OAuth2API
from nuheat.manager import ThermostatManager


def get_manager() -> ThermostatManager:
    api_type = os.environ.get("NUHEAT_API_TYPE", "legacy").lower()

    if api_type == "oauth2":
        client_id = os.environ.get("NUHEAT_CLIENT_ID", "")
        client_secret = os.environ.get("NUHEAT_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            print("Error: NUHEAT_CLIENT_ID and NUHEAT_CLIENT_SECRET required for OAuth2")
            sys.exit(1)
        redirect_uri = os.environ.get("NUHEAT_REDIRECT_URI", "http://localhost:8787/callback")
        api = OAuth2API(client_id, client_secret, redirect_uri)
    else:
        email = os.environ.get("NUHEAT_EMAIL", "")
        password = os.environ.get("NUHEAT_PASSWORD", "")
        if not email or not password:
            print("Error: NUHEAT_EMAIL and NUHEAT_PASSWORD are required")
            print("Set them as environment variables or in a .env file")
            sys.exit(1)
        api = LegacyAPI(email, password)
        serial_numbers = os.environ.get("NUHEAT_SERIAL_NUMBERS", "")
        if serial_numbers:
            api.serial_numbers = [s.strip() for s in serial_numbers.split(",")]

    return ThermostatManager(api)


async def cmd_status(mgr: ThermostatManager, args: argparse.Namespace) -> None:
    if not await mgr.authenticate():
        print("Authentication failed")
        return

    if args.serial:
        t = await mgr.get_thermostat(args.serial)
        if t:
            _print_thermostat(t)
        else:
            print(f"Thermostat {args.serial} not found")
    else:
        thermostats = await mgr.refresh()
        if not thermostats:
            print("No thermostats found. Check NUHEAT_SERIAL_NUMBERS.")
            return
        for t in thermostats:
            _print_thermostat(t)
            print()


async def cmd_set(mgr: ThermostatManager, args: argparse.Namespace) -> None:
    if not await mgr.authenticate():
        print("Authentication failed")
        return

    temp_c = args.temp_c
    if args.temp_f is not None:
        temp_c = (args.temp_f - 32) * 5 / 9

    if temp_c is None:
        print("Error: provide --temp-c or --temp-f")
        return

    success = await mgr.set_temperature(args.serial, temp_c, args.hold_until)
    if success:
        print(f"Set {args.serial} to {temp_c:.1f}C / {temp_c * 9/5 + 32:.1f}F")
    else:
        print("Failed to set temperature")


async def cmd_resume(mgr: ThermostatManager, args: argparse.Namespace) -> None:
    if not await mgr.authenticate():
        print("Authentication failed")
        return

    success = await mgr.resume_schedule(args.serial)
    if success:
        print(f"Resumed schedule for {args.serial}")
    else:
        print("Failed to resume schedule")


async def cmd_serve(mgr: ThermostatManager, args: argparse.Namespace) -> None:
    import uvicorn
    from nuheat.server import app  # noqa: F811

    print(f"Starting NuHeat REST API on {args.host}:{args.port}")
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


def _print_thermostat(t) -> None:
    status = "HEATING" if t.heating else "idle"
    online = "online" if t.online else "OFFLINE"
    print(f"  {t.name} ({t.serial_number}) [{online}]")
    if t.group:
        print(f"    Group:       {t.group}")
    print(f"    Status:      {status}")
    print(f"    Current:     {t.current_temperature_c:.1f}C / {t.current_temperature_f:.1f}F")
    print(f"    Target:      {t.target_temperature_c:.1f}C / {t.target_temperature_f:.1f}F")
    print(f"    Mode:        {t.schedule_mode_name}")
    if t.hold_until:
        print(f"    Hold until:  {t.hold_until}")
    if t.firmware:
        print(f"    Firmware:    v{t.firmware}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nuheat",
        description="Control NuHeat floor heating thermostats",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    status_parser = subparsers.add_parser("status", help="Show thermostat status")
    status_parser.add_argument("--serial", "-s", help="Specific thermostat serial number")

    # set
    set_parser = subparsers.add_parser("set", help="Set target temperature")
    set_parser.add_argument("serial", help="Thermostat serial number")
    set_parser.add_argument("--temp-c", type=float, help="Target temperature in Celsius")
    set_parser.add_argument("--temp-f", type=float, help="Target temperature in Fahrenheit")
    set_parser.add_argument("--hold-until", help="ISO datetime for temporary hold")

    # resume
    resume_parser = subparsers.add_parser("resume", help="Resume programmed schedule")
    resume_parser.add_argument("serial", help="Thermostat serial number")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start the REST API server")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    serve_parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")

    args = parser.parse_args()
    mgr = get_manager()

    cmd_map = {
        "status": cmd_status,
        "set": cmd_set,
        "resume": cmd_resume,
        "serve": cmd_serve,
    }

    async def run():
        try:
            await cmd_map[args.command](mgr, args)
        finally:
            await mgr.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
