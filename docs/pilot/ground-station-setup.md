# Setting up a ground station

**DRAFT (P1-01b).** Written to be followed by a pilot without help. Two things
are still missing, and are marked TO MEASURE below: the radio port's stream
rates, which are measured in `docs/runbooks/real-drone-session.md` part 4, and
the proof that a second person can follow this document alone, which is the
task's other half.

A ground station is one Windows computer with QGroundControl (QGC), a
telemetry radio, and the ground relay. QGC flies the aircraft. The relay sends
a copy of everything QGC hears to the platform, over the internet. The platform
never sends anything back to the aircraft at this stage.

## 1. What you need from the platform operator

- a **station id**, e.g. `tbilisi-base-1`;
- a **token file**, e.g. `relay.token`. It is a password: never email it,
  never put it in a shared folder;
- the **Gateway address**, starting with `wss://`.

Never invent a station id. The platform only accepts the ones it issued.

## 2. Each aircraft's identity (SYSID)

Every aircraft flying from the same station needs its own MAVLink system id:
parameter `SYSID_THISMAV`, in QGC under Vehicle Setup -> Parameters.

- Give each aircraft a different number, 1-250. 255 is QGC's own.
- Tell the platform operator which number went on which airframe. The platform
  attributes telemetry by station and SYSID; two aircraft with the same SYSID
  on one station cannot be told apart, and the platform will refuse an address
  that is bound on another station.
- Write the SYSID on the airframe.

## 3. Radios: one network per station

Each station's telemetry radios must be on their own radio network, so that
two stations near each other do not hear each other's aircraft. On SiK-type
radios this is the `NETID` setting, and the aircraft's radio and the ground
radio must match. Use your radio's configuration tool (QGC can configure SiK
radios when they are connected).

| Station | NETID |
|---|---|
| TO FILL per station | |

## 4. Stream rates on the radio port

The radio carries far less than USB. What the aircraft sends over the radio is
set by the stream-rate parameters of the serial port the radio is on (on
ArduPilot these have been `SR1_*` / `SR2_*`; check the names in QGC's parameter
list for your firmware version).

**Radio budget: TO MEASURE.** Measured on USB, one aircraft sends about
2.8 KiB/s (ADR-001). The radio port's figure is measured in the real-drone
session and entered here, with the parameter values that produced it. Do not
fly more than one aircraft per radio network until it is.

## 5. QGC: forward everything to the relay

Application Settings -> General -> MAVLink:

- **Enable MAVLink forwarding**: on
- **Host**: `127.0.0.1:14445`

Start the relay (section 6) **before** connecting to the aircraft. QGC asks
the aircraft for its firmware version once, when it connects, and the
platform only records it if the relay is already listening.

## 6. The relay

1. Copy `agent/relay.example.toml` to `relay.toml` in the same folder.
2. Set `station_id` and `gateway_url` from section 1. Put the token file next
   to `relay.toml` and leave `token_path = "relay.token"`.
3. On Windows, write any full path with forward slashes: `C:/Users/pilot/...`.
4. Start it: `python -m agent --config relay.toml`

The relay keeps everything on disk while the internet is down and sends it
when it comes back. Closing it loses nothing already on disk, but anything QGC
forwards while it is closed is not recorded.

## 7. Checking it works

- The relay log says `uplink established`.
- The platform's map shows the station as healthy and the aircraft by name.
- The aircraft's firmware shows as `fw <version>`, not `fw unknown`. If it is
  unknown, disconnect and reconnect the aircraft in QGC with the relay running.
