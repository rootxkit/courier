# gateway

MAVLink ingest and (from Stage 1) command dispatch.

Receives MAVLink over UDP, identifies vehicles by SYSID, converts every field to
SI units at the parser boundary, and fans the result out to TimescaleDB, Redis
live state and NATS.

Safety-relevant: `mypy --strict` and an 80% coverage target apply here.
