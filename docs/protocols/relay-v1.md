# relay-v1 — ground relay to Gateway

- **Status:** DRAFT — under review; no implementation exists yet
- **Version:** `1`
- **Tasks:** produced by P1-01 (relay), consumed by P1-02 (Gateway)

This is the contract between the ground relay and the Gateway. **The Gateway is
built against this document, not against the relay's source.** Either side may
be rewritten in another language or replaced entirely as long as it conforms.

## 1. Design rules

Three rules explain every decision below. Read them first; the rest follows.

**The relay is dumb.** It does not parse MAVLink. It does not know what a
vehicle is, how many there are, or whether one is airborne. It moves opaque
datagrams from a socket to a server, in order, without loss. Every question of
meaning belongs to the Gateway.

**The relay is lossless.** It forwards every datagram it receives, unmodified.
Filtering at the ground station would be an irreversible decision taken at the
point in the system with the least information about what will later matter.
The messages the live path does not want — `ATTITUDE`, `VIBRATION`,
`EKF_STATUS_REPORT`, `ESC_TELEMETRY` — are exactly what an incident
investigation reads after a crash, and P10-03 flight replay cannot reconstruct
a message that was never recorded. ADR-001 measured ~2.8 KiB/s per aircraft;
on an internet uplink that is not worth optimising against the cost of an
unexplainable accident.

**The relay is receive-only toward the aircraft.** Its UDP socket is used for
`recvfrom` and nothing else. At Stage 0 the server cannot affect flight, and
this is where that guarantee is enforced physically rather than promised.
Nothing in this protocol carries a message travelling towards a vehicle, and
v2 must not add one without revisiting `ARCHITECTURE.md` §3.

## 2. Transport

```
wss://<gateway-host>/relay/v1
```

TLS is mandatory. One WebSocket connection per ground station.

Both WebSocket frame types are used, and the type distinguishes the two
channels:

| Frame type | Carries |
|---|---|
| **Text** | JSON control messages: `hello`, `welcome`, `ack`, `status` |
| **Binary** | Telemetry batches (§6) |

Every JSON message has a `"type"` field naming it. Unknown JSON `type` values
and unknown JSON fields **must be ignored rather than rejected**, so that a
newer relay can talk to an older Gateway within version 1.

## 3. Authentication

The relay authenticates on the HTTP upgrade request:

```
Authorization: Bearer <token>
```

Failure is rejected at the upgrade with HTTP `401`. The relay treats `401` as
fatal and does not retry with the same token: a bad credential is an operator
problem, and retrying turns it into a log flood.

**The token identifies a ground station, not a vehicle.** Vehicle identity
comes from the MAVLink SYSID inside the forwarded datagrams, and which stations
are permitted to carry which vehicles is server policy, evaluated by the
Gateway. A station is not trusted to assert what it is carrying.

> This refines **P1-07**. That task reads "per-vehicle token"; the tokens are
> per-station, and per-vehicle authorisation becomes a server-side policy check
> on `(station_id, sysid)` rather than a credential the relay holds. The
> security property is the same — a spoofed SYSID is rejected — but it is
> enforced where the policy lives, and a compromised ground station cannot mint
> vehicles it was never assigned.

## 4. Identity and sequencing

Every record is identified by the triple:

```
(station_id, epoch, seq)
```

| Field | Type | Meaning |
|---|---|---|
| `station_id` | string | Stable identity of the ground station, from its config |
| `epoch` | string, 32 lowercase hex chars | Random 128-bit value generated **when the relay's queue database is created** |
| `seq` | `u64` | Monotonically increasing within an epoch, starting at 0, never reused |

**Why `epoch` exists.** Without it, a queue file that is deleted, corrupted, or
restored from a backup restarts `seq` at zero. The Gateway, deduplicating on
`(station_id, seq)`, would recognise those numbers as already seen and discard
the new data — silently, and for as long as it took to climb past the old high
water mark. A fresh random epoch makes that case loud instead: the server sees
an identity it has never seen, and starts from zero legitimately.

The epoch changes only when the queue database is created. Restarting the relay
against an existing queue keeps the epoch and continues the sequence.

## 5. Session establishment

On connect, the relay sends `hello` and waits for `welcome` before sending any
data frame.

### `hello` — relay to server, text

```json
{
  "type": "hello",
  "station_id": "tbilisi-base-1",
  "epoch": "9f2c1b7d4e6a58039ab1c2d3e4f50617",
  "relay_version": "0.1.0",
  "protocol_version": 1,
  "oldest_seq_held": 41203,
  "newest_seq_held": 58817,
  "monotonic_ns": 992847110000000,
  "utc_ns": 1758412800123456789
}
```

`oldest_seq_held` and `newest_seq_held` describe what the relay still has on
disk. When the queue is empty, `oldest_seq_held` is the next sequence number to
be assigned and `newest_seq_held` is that value minus one.

### `welcome` — server to relay, text

```json
{
  "type": "welcome",
  "protocol_version": 1,
  "resume_from_seq": 58120
}
```

**The server is authoritative about what it holds.** The relay sends everything
from `resume_from_seq` onward, regardless of what it believes was acknowledged.
This is what makes a lost `ack` harmless: an ack that never arrived costs a
retransmission, never a gap.

For an epoch the server has never seen, `resume_from_seq` is `0`.

The relay must not assume `resume_from_seq` is at or ahead of its own
high-water mark. A server may legitimately ask for data the relay has already
sent.

## 6. Data frames — binary

A binary frame is a batch of one or more records, concatenated with no header
and no padding. All integers are **little-endian**.

```
repeated:
  u64  seq              record sequence number within the epoch
  i64  recv_utc_ns      wall-clock time the datagram was received (§9)
  u16  len              length of the datagram in bytes
  u8   datagram[len]    the UDP payload, exactly as received
```

Records within a batch are in ascending `seq` order with no gaps.

**The datagram is opaque.** It is not re-framed, not split into MAVLink
messages, not validated, and not modified. A datagram that is malformed,
truncated, or not MAVLink at all is forwarded unchanged — deciding that is the
Gateway's job, and a relay that discarded "invalid" traffic would hide exactly
the corruption worth investigating.

`len` is `u16`; a UDP payload cannot exceed 65507 bytes, so the field cannot
overflow. The relay's receive buffer must be at least 65535 bytes: sizing it to
the datagrams QGC happens to send today would silently truncate anything
larger.

### Batching

A batch is flushed on whichever comes first:

- **100 ms** since the batch opened, or
- **64 KiB** accumulated.

The time bound sets the relay's contribution to end-to-end latency, which
P1-08 budgets at under 500 ms end to end. The size bound keeps a burst — a
reconnect draining a backlog — from producing frames large enough to stall the
connection.

## 7. Acknowledgement

### `ack` — server to relay, text

```json
{
  "type": "ack",
  "epoch": "9f2c1b7d4e6a58039ab1c2d3e4f50617",
  "seq": 58904
}
```

**Cumulative**: every record up to and including `seq`, in this epoch, is
durably stored server-side. The relay may delete those records and must not
delete any record beyond `seq`.

`epoch` is included so that an `ack` arriving late, after the relay has started
a new epoch, is discarded rather than deleting records it does not describe.

The server acknowledges only what it has committed to durable storage. An `ack`
for data still in a server-side buffer would turn a Gateway crash into a hole
in the flight record, which is the exact failure this design exists to prevent.

## 8. Status

### `status` — relay to server, text, every 1 s

```json
{
  "type": "status",
  "queue_depth": 1240,
  "queue_bytes": 2310450,
  "dropped_total": 0,
  "last_datagram_age_ms": 38,
  "uptime_s": 7321,
  "monotonic_ns": 992847110000000,
  "utc_ns": 1758412800123456789
}
```

| Field | Meaning |
|---|---|
| `queue_depth` | Records on disk awaiting acknowledgement |
| `queue_bytes` | Bytes those records occupy |
| `dropped_total` | Records discarded since the epoch began, persisted across restarts |
| `last_datagram_age_ms` | Milliseconds since a datagram last arrived on the UDP socket |
| `uptime_s` | Seconds since the relay started |

**`last_datagram_age_ms` is the field that matters.** `ARCHITECTURE.md` §3
separates two failure domains that a naive implementation renders identically:

| Situation | Symptom without `status` | Symptom with `status` |
|---|---|---|
| Relay or internet down | Telemetry stops | No `status` for >3 s: the station is unreachable |
| Relay up, radio silent | Telemetry stops | `status` continues, `last_datagram_age_ms` climbs |

The first is a tracking outage; the aircraft is fine and the pilot still has
QGC. The second means the ground station has lost the aircraft, which is a
flight-safety event. **The pilot console must never present these as the same
thing**, and this field is what makes them distinguishable.

The Gateway should treat three consecutive missed `status` messages as the
station being unreachable. Relying on TCP or WebSocket timeouts alone is not
sufficient: a half-open connection can survive for minutes.

## 9. Clocks

`recv_utc_ns` comes from the ground PC's wall clock, which **may be wrong** —
unsynchronised, drifting, or stepped by NTP mid-flight. It is recorded because
it is useful, not because it is trusted.

Every `hello` and `status` carries a `(monotonic_ns, utc_ns)` pair sampled at
the same instant. This lets the Gateway estimate the station's clock offset and
detect a step: the monotonic clock cannot jump, so a change in the difference
between the two is a wall-clock correction, not elapsed time.

A later refinement can align records to GPS time using `SYSTEM_TIME`, observed
at 3 Hz in ADR-001 and carrying the autopilot's GPS-derived time. **This
document does not specify that alignment**; it notes the raw material exists.
Deciding it belongs with the Gateway's time handling, not with the transport.

## 10. Delivery guarantees

- **In order**, per `(station_id, epoch)`.
- **At least once.** A record may be delivered more than once, after a
  reconnect or a lost `ack`.
- **Deduplicated on `(station_id, epoch, seq)`** by the Gateway.

Exactly-once delivery is not offered, because it cannot be had over a link that
can fail between "stored" and "acknowledged". Dedupe on a stable key is
equivalent, and vastly simpler.

### No live-first reordering (deliberate v1 omission)

When the relay reconnects after an outage, it sends its backlog in sequence
order. It does **not** send live telemetry first and backfill the gap behind
it, even though a pilot would rather see the present than the past.

That behaviour would break cumulative acknowledgement. Acknowledging a range
with a hole in it requires selective acknowledgement, which means per-record
state on both sides and a materially more complex protocol — the part of a
transport most likely to harbour a bug that appears only under the conditions
nobody can reproduce.

The arithmetic does not justify it:

```
3 aircraft x 2.8 KiB/s          =  8.4 KiB/s
30-minute outage: 1800 s x 8.4  =  ~15 MB of backlog
15 MB over a 10 Mbit/s uplink   =  ~12 seconds to drain
```

Twelve seconds of stale map after a half-hour outage, against a permanent
increase in the complexity of the one component whose job is not to lose data.
**Do not "improve" this without redoing the arithmetic** — if aircraft counts
or stream rates rise by an order of magnitude, the trade changes, and then the
change is justified by numbers rather than by discomfort.

## 11. Gaps

The relay's queue is capped (default 1 GiB). At the cap it drops the oldest
records and increments `dropped_total`; it never blocks intake, because
blocking intake would lose live telemetry to protect old telemetry.

This means the server may ask for data the relay no longer holds — when
`resume_from_seq` is below `oldest_seq_held` in the same epoch. That is a
**permanent gap**, and it must be recorded rather than papered over.

The relay reports it explicitly before sending data:

### `gap` — relay to server, text

```json
{
  "type": "gap",
  "epoch": "9f2c1b7d4e6a58039ab1c2d3e4f50617",
  "from_seq": 58120,
  "to_seq": 61099,
  "reason": "queue_capacity"
}
```

`from_seq` is inclusive, `to_seq` exclusive: the records in `[from_seq, to_seq)`
no longer exist anywhere and will never arrive. The Gateway records the gap
against the station and the time range, so that a later investigator reading a
hole in the flight record can tell "this was dropped at the ground station,
here is when and why" from "we have no idea".

The relay then resumes from `oldest_seq_held`.

> **Open for review.** This message is not in the original specification for
> this protocol. It was added because the queue cap and `resume_from_seq`
> together make the case reachable, and silence would make a hole in a flight
> record indistinguishable from a bug. Remove it only by removing the cap.

## 12. Reconnection

On disconnect the relay reconnects with exponential backoff, **capped at 10 s**,
with jitter. Jitter matters once there is more than one ground station: without
it, a Gateway restart brings every station back simultaneously, each draining a
backlog.

`401` is fatal and is not retried (§3). Every other failure is retried
indefinitely — a relay that gives up is a relay that loses a flight.

The relay keeps accepting UDP and queueing to disk throughout. Connection state
never propagates back to the socket.

## 13. What the Gateway must implement

For P1-02, conformance means:

1. Accept the upgrade, validate the bearer token, resolve it to a `station_id`.
2. Reply to `hello` with `welcome`, carrying the true `resume_from_seq` for
   that `(station_id, epoch)` — `0` for an unknown epoch.
3. Persist records durably **before** acknowledging them.
4. Send a cumulative `ack` carrying the epoch, at least once per second while
   data is flowing.
5. Deduplicate on `(station_id, epoch, seq)`.
6. Record `gap` messages as first-class events against the station.
7. Treat three consecutive missed `status` messages as the station being
   unreachable, and surface that **differently** from a station reporting a
   rising `last_datagram_age_ms` (§8).
8. Parse MAVLink only after all of the above. Nothing in this protocol requires
   the transport layer to understand the payload.

## 14. Versioning

The path carries the major version (`/relay/v1`). Within version 1, unknown
JSON message types and unknown fields must be ignored, so additive changes do
not require a version bump. Anything that changes the meaning of an existing
field, the binary record layout, or the delivery guarantees requires `/relay/v2`.
