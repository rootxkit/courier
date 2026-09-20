# agent

On-vehicle / ground-relay MAVLink agent.

At Stage 0 this is the ground relay (`P1-01`): it reads the MAVLink stream QGC
forwards to UDP `127.0.0.1:14445`, authenticates, and forwards it to the Gateway
over a TLS WebSocket with a disk-backed queue that replays after an internet
dropout. It runs on the pilot's ground station PC, not on the aircraft.

From Stage 2 (`P8-01`) the same process runs on an onboard computer over UART.

Nothing here may assume it can send anything back to the vehicle — see
`docs/ARCHITECTURE.md` §2.
