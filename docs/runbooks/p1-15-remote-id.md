# Remote ID

Drones that are not ours broadcast who and where they are (ASTM F3411,
ASD-STAN EN 4709-002). P1-15 puts them on the same map and in the same
airspace monitor as our MAVLink aircraft.

```
receiver ──UDP JSON──▶ gateway.remote_id_ingest ──telemetry.<id>──▶ console, airspace monitor
 (ESP32, phone,          decode (gateway/odid.py)
  commercial unit)       join by transmitter (gateway/remote_id.py)
                         HAE → AMSL (common/geoid.py)
```

## What a receiver sends

One UDP datagram per message or message pack it hears, to
`REMOTE_ID_BIND_HOST:REMOTE_ID_BIND_PORT` (default 127.0.0.1:14600):

```json
{"receiver_id": "rx-tbilisi-1", "transmitter": "AA:BB:CC:DD:EE:FF",
 "payload_hex": "f219030212...", "rssi_dbm": -71}
```

`payload_hex` is the Open Drone ID message exactly as broadcast: one
25-byte message, or a message pack. `transmitter` is the Bluetooth or Wi-Fi
address it came from; it is what joins a Basic ID to a Location when they
arrive separately (Bluetooth 4).

## Run it

```
infra/geoid/fetch_geoid.sh              # once: local/geoid/egm2008-2_5.pgm
GEOID_PATH=local/geoid/egm2008-2_5.pgm python -m gateway.remote_id_ingest
```

Without `GEOID_PATH` the ingest runs and the aircraft are on the map, but
they have no AMSL altitude and the monitor does not evaluate them. It says
so at start-up.

Test traffic, without a receiver:

```
python tools/remote_id_sim.py --start-lat <lat> --start-lon <lon> \
    --alt-amsl-m <m> --track-deg <deg> --speed-ms <m/s> --duration-s 90 \
    --geoid local/geoid/egm2008-2_5.pgm
```

## How to read it

- A Remote ID aircraft is purple (red or orange while in an alert), with a
  dashed outline, and labelled
  "Remote ID". Its panel says the position is broadcast and not verified.
  Anyone can transmit one; an alert involving it is about a claimed
  position.
- Its arrow is the track over the ground; Remote ID has no heading.
- Its id is derived from its serial number, so the same aircraft keeps
  the same id across receivers and restarts.

## Verified 2026-09-29, on the laptop

- The decoder agrees with the reference library (opendroneid-core-c) on
  every field of 170 messages it encoded, and re-encodes them to the same
  bytes.
- The geoid reader agrees with GeographicLib's own Geoid class to 4e-13 m
  over 5,005 points. EGM96 puts the geoid 14.7 m above the ellipsoid at
  Tbilisi and 20.9 m at Batumi.
- One SITL aircraft hovered 30 m above home (475 m AMSL); a simulated Remote
  ID aircraft flew east through it at 480 m AMSL, broadcasting 494.7 m HAE.
  The monitor raised a critical conflict 475 m out (closest approach 1.2 m
  in 59.6 s, 3.2 m apart vertically), and cleared it after the pass. The
  console showed the Remote ID aircraft as broadcast and unverified, and the
  conflict line between the two.

## Not yet

- Receivers are not authenticated: keep the ingest on loopback until they
  are.
- Remote ID tracks are not stored, so replay does not show them. Their
  alerts are in the audit log.
- One of our aircraft that also broadcasts Remote ID appears twice, and
  would raise a conflict with itself. Matching the broadcast serial to the
  registered aircraft is the rest of P1-15.
- No real receiver has been connected yet: the decoder is checked against
  the reference library's bytes, not yet against a broadcast in the air.
