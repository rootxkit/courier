# P1-01 hardware test: the relay loses nothing

Acceptance criterion: **pull the network cable for two minutes, and once the
link returns the flight record has no holes.**

This needs two machines. A receiver on the same laptop as the relay would make
the test meaningless — cutting the laptop's network does not break loopback, so
the relay would never notice an outage and the test would always pass.

Two machines means a real network, which means the bearer token would otherwise
cross a LAN in the clear. So the test runs over TLS, and the relay refuses
plain `ws://` to anything but localhost.

| | |
|---|---|
| **Laptop** | ground station: QGroundControl, the aircraft on USB, the relay |
| **Receiver** | any second machine on the same LAN: `tools/relay_sink.py` |

The receiver can be a desktop, a spare laptop, a Raspberry Pi, or a VM with a
bridged adapter. It needs Python 3.12 and this repository.

---

## 0. Before you start

**The probe and the relay cannot run at the same time.** Both bind UDP 14445.
On Windows both will appear to start, and each will silently receive part of
the stream — which looks exactly like packet loss. Close
`tools/mavlink_probe.py` before starting the relay.

**Connect the aircraft by USB, not over the radio link.** Cutting the laptop's
network must not also cut the vehicle link, or the test measures nothing: the
relay would have no telemetry to buffer during the outage.

Propellers off. The aircraft does not need to fly or even be on battery.

---

## 1. Generate a development CA and a server certificate

On the **receiver**, once. These commands are the same on Windows (Git Bash) and
on WSL or Linux.

First find the receiver's LAN address. You need it in the certificate, because
the relay verifies the certificate against the address it connects to:

    # Linux / WSL
    ip addr show | grep 'inet '

    # Windows PowerShell
    ipconfig

Take the LAN address — `192.168.1.50` in the examples below — and substitute
yours everywhere it appears.

    mkdir -p relay-test && cd relay-test

Write the server extensions file. The extensions matter: OpenSSL 3 verifies
them strictly, and a CA without `keyUsage` fails the handshake with
`CA cert does not include key usage extension`.

    cat > server.ext <<'EOF'
    basicConstraints=CA:FALSE
    keyUsage=critical,digitalSignature,keyEncipherment
    extendedKeyUsage=serverAuth
    subjectAltName=IP:192.168.1.50
    EOF

Then the CA:

    openssl req -x509 -newkey rsa:2048 -nodes \
      -keyout ca.key -out ca.crt -days 30 \
      -subj "/CN=courier-dev-ca" \
      -addext "basicConstraints=critical,CA:TRUE" \
      -addext "keyUsage=critical,keyCertSign,cRLSign"

The server key and request:

    openssl req -newkey rsa:2048 -nodes \
      -keyout server.key -out server.csr \
      -subj "/CN=192.168.1.50"

And sign it:

    openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
      -out server.crt -days 30 -extfile server.ext

Check the address went in:

    openssl x509 -in server.crt -noout -text | grep -A1 "Subject Alternative Name"

Make a token — any long random string, the same on both machines:

    openssl rand -hex 32 > sink.token
    cat sink.token

**Copy `ca.crt` and `sink.token` to the laptop.** `ca.key` stays on the
receiver and is never copied anywhere.

---

## 2. Start the receiver

On the **receiver**:

    python tools/relay_sink.py serve \
      --out ./sink-data \
      --token-file ./relay-test/sink.token \
      --cert ./relay-test/server.crt \
      --key ./relay-test/server.key \
      --host 0.0.0.0 \
      --port 8443

It prints one line per session and one per reported gap. Leave it running.

If a firewall prompts, allow inbound TCP 8443 on the private network. On
Windows that prompt is easy to miss behind other windows; if the relay cannot
connect at all, check this first.

---

## 3. Configure the relay on the laptop

Put `ca.crt` and `relay.token` next to the repository, then write `relay.toml`:

    station_id = "hw-test-laptop"
    gateway_url = "wss://192.168.1.50:8443/relay/v1"
    token_path = "relay.token"
    queue_path = "relay-queue.sqlite3"
    ca_path = "ca.crt"

    bind_host = "127.0.0.1"
    bind_port = 14445

`relay.toml`, `*.token` and the queue file are gitignored. Do not commit them.

Confirm QGroundControl is forwarding: **Application Settings → General →
MAVLink → Enable MAVLink forwarding**, host `127.0.0.1:14445`.

Start the relay:

    python -m agent --config relay.toml

It logs JSON, one object per line. `"uplink established"` means it is
connected, and the receiver should print a `session` line within a second.

---

## 4. Procedure A — cut the network

The main test.

| Step | Action | Expected |
|---|---|---|
| 1 | Run **5 minutes** untouched | Receiver quiet; relay logs little beyond the occasional ack |
| 2 | **Unplug the laptop's ethernet, or turn off its Wi-Fi.** Note the time | Relay logs `uplink session ended`, then retries with lengthening backoff |
| 3 | Wait **2 minutes**. Watch QGC | QGC keeps showing telemetry throughout. **If it stops, the aircraft link was cut too — restart from step 1 with the aircraft on USB** |
| 4 | Reconnect the network. Note the time | Relay reconnects within ~10 s; receiver prints a new `session` line whose `resume_from_seq` is from before the outage |
| 5 | Run **5 more minutes** | The backlog drains in seconds, then normal flow resumes |
| 6 | `Ctrl+C` the **relay** | |
| 7 | `Ctrl+C` the **receiver** | It prints the verification report |

## 5. Procedure B — stop the receiver instead

The same outage from the other end. It also tests that the receiver resumes
from what is on its disk rather than from memory.

Identical to Procedure A, except that at step 2 you press `Ctrl+C` on the
**receiver** rather than touching the network, and at step 4 you start it again
with the same command and the same `--out` directory.

The relay's behaviour should be indistinguishable. The receiver should report
two or more sessions and no missing sequence numbers.

---

## 6. Reading the report

The report prints on exit, and can be re-read at any time without disturbing
anything:

    python tools/relay_sink.py report --out ./sink-data

A pass looks like this:

    ========================================================================
    relay-v1 sink verification report
    data directory: sink-data
    ========================================================================

    station_id : hw-test-laptop
    epoch      : 9f2c1b7d4e6a58039ab1c2d3e4f50617
    sessions   : 2
    seq range  : 0 .. 31847  (31848 stored)
    received   : 31903
    duplicates : 55  (re-sent after a reconnect; harmless once deduped)
    missing    : none
    gaps       : none reported
    relay restarts inferred from uptime_s: 0
    last status: queue_depth=3 queue_bytes=612 last_datagram_age_ms=41
    drops      : intake=0 cap=0

    VERDICT: PASS - contiguous, nothing dropped, nothing lost.

    ========================================================================
    OVERALL: PASS
    ========================================================================

What each line has to say for the test to have passed:

| Line | Pass | What a failure means |
|---|---|---|
| `sessions` | **2 or more** | 1 means the outage never happened — the relay never lost its connection, so nothing was tested |
| `missing` | **none** | Sequence numbers that never arrived. This is the criterion; anything here is a fail |
| `duplicates` | any number | Expected. The relay resends from the server's resume point, so a few records cross twice. Dedupe makes them harmless |
| `gaps` | **none reported** | The queue hit its cap and dropped records. Either the outage was far longer than planned, or `queue_max_bytes` is too small |
| `drops intake` | **0** | The relay could not write to disk fast enough. Investigate before flying |
| `drops cap` | **0** | Same as `gaps` |
| `relay restarts` | **0** | The relay process died and restarted. Records in its memory at that moment were lost — find out why it died |
| `UPSTREAM: CLEAN` (analyze_capture) | **yes on USB** | MAVLink sequence gaps mean frames were lost between QGC and the relay, which the sink report cannot see |
| `seq range` starting at 0 | **yes** | A first sequence above 0 means an earlier epoch's data is in this directory, or records were lost before the receiver ever saw them |

### Then check upstream integrity

The sink report covers relay to sink. It says nothing about QGC to relay, and
the relay's own counters cannot: `dropped_intake_total` counts datagrams the
relay took off the socket and could not hand on, but one the OS discarded from
the UDP receive buffer before `recvfrom` was never counted anywhere.

Every stored record still holds the MAVLink frames as they arrived, and MAVLink
carries a per-`(sysid, compid)` sequence byte. A hole in it is loss upstream of
the relay's queue.

    python -m tools.analyze_capture local/sink-data --bucket-seconds 10

A pass needs **`UPSTREAM: CLEAN`** — zero MAVLink sequence gaps on every
source. On a USB link there is no radio to lose frames, so any gap is loss
between QGC and the relay.

Over a radio link, expect some loss and record the percentage rather than
demanding zero: that is the radio, not the software, and it is the number
P1-01b's stream-rate budget has to live within.

The frames-per-second buckets are the other half. A steady rate across the
outage confirms the relay kept receiving while disconnected; a dip during the
outage would mean the relay stopped taking datagrams while it was busy
reconnecting, which would be a real defect.

Record the report, the two timestamps from steps 2 and 4, and the QGC version
in the test notes. Then update P1-01 in `TASKS.md`.

---

## 7. If it fails

**The relay cannot connect at all.** Check the receiver's firewall first. Then
confirm the certificate's `subjectAltName` matches the address in `gateway_url`
exactly — `192.168.1.50` and `localhost` are different names even when they
reach the same machine.

**`certificate verify failed`.** `ca_path` is missing from `relay.toml`, or it
points at `server.crt` rather than `ca.crt`. Verification is never disabled;
that is the point of having a CA.

**`token rejected by the Gateway; not retrying`.** The two token files differ. A
trailing newline is fine — both sides strip — but a value copied with quotes
around it is not.

**`missing` is non-empty while `drops intake` is 0.** The interesting failure.
Keep `sink-data` and the relay's log: the sequence numbers in the report say
exactly which records vanished, and the events file says which session they
should have arrived in.

**Nothing arrives and `last_datagram_age_ms` keeps climbing.** The relay is
healthy and the radio is silent — QGC forwarding is off, or pointed elsewhere,
or the probe is still running and holding 14445.
