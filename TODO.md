# TODO

## Future Enhancements

### Smart poll-until-confirmed after writes
Instead of a fixed 5-second delay before the post-write confirmation GET,
poll NuHeat multiple times (e.g., at 2s, 5s, 10s) and stop as soon as
the new setpoint is reflected in the response. This would be faster when
NuHeat acknowledges quickly and more resilient when thermostats are slow.
Currently using a simple 5-second sleep before the confirmation read.

### OAuth2 API migration
The Legacy API (mynuheat.com/api) is unofficial and could break without
notice. If/when OAuth2 developer credentials are obtained from NuHeat,
swap to the official API. The code is already structured for this -
just change NUHEAT_API_TYPE=oauth2 and add the client credentials.
See README.md for details.

### Energy usage dashboard
The OAuth2 API exposes energy log endpoints (day/week/month) that the
Legacy API does not. Once on OAuth2, add an energy usage page to the
dashboard showing heating hours and estimated costs over time.

### Configurable post-write delay
The 5-second delay before the confirmation GET after a write is
currently hardcoded in manager.py. Could be made a runtime setting
for tuning per-environment (faster LAN vs slower WiFi thermostats).
