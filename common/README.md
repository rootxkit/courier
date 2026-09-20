# common

Shared logging and configuration. Every service imports this; nothing here
imports a service.

## Configuration

A service declares what it needs as a settings class composed from the mixins
in `config.py`, and gets a validated, frozen object or a refusal to start:

```python
from common import NatsSettings, PostgresSettings, RedisSettings, ServiceSettings


class DispatchSettings(ServiceSettings, PostgresSettings, RedisSettings, NatsSettings):
    service_name: str = "dispatch"
```

No service reads `os.environ`. A value read directly is a value that was never
validated, has no declared type, and fails when it is first used rather than
before the process starts — which, for a service supervising aircraft, is the
difference between a failed deploy and a surprise in flight.
`tests/test_no_direct_environ.py` enforces this.

What does **not** belong here: scoring weights, separation minima, altitude
bands, battery thresholds, geofences. Those are operational parameters that
operators change without a deploy, so they live in the database where a change
is audited. Startup configuration is infrastructure — where the database is,
what to log, which port to bind.

## Logging

One JSON object per line on stdout. Context travels as fields, never
interpolated into the message:

```python
from common import bind, get_logger

log = bind(get_logger(__name__), drone_id=drone_id, mission_id=mission_id)
log.warning("battery below reserve", extra={"batt_pct": 22.5})
```

`print()` is banned repository-wide by ruff (T20). Timestamps are UTC-aware,
matching the `TIMESTAMPTZ` convention everywhere else.

## Startup

`start_service` does both, in the order that matters — configuration validated
first, logging installed before anything else runs:

```python
from common.startup import start_service

settings, log = start_service(DispatchSettings)
```
