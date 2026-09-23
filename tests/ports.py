"""Reserving a free port for a test, for the protocol you actually need.

There is no `free_port()` here, deliberately. The caller must say TCP or UDP,
because the two are **separate port spaces** and a probe of one says nothing
about the other. That is not a theoretical distinction: a port held on UDP is
bound happily by a TCP probe, measured on 2026-09-23, and
`agent/tests/test_halfopen.py` shipped for a while choosing the relay's UDP
intake port with a `SOCK_STREAM` probe. The relay binds its intake exclusively,
so the collision surfaces as `PortInUseError` with nothing in the message to
suggest the port was picked wrongly.

Both functions have an unavoidable race: the probe socket is closed before the
caller binds, so something else can take the number in between. Holding it open
is not an option when the code under test binds exclusively. The race is narrow
and the alternative is worse, but it is the first thing to suspect if a test
fails with "address in use" rather than a timeout.
"""

from __future__ import annotations

import socket


def free_tcp_port() -> int:
    """A TCP port nothing is listening on, for a server or a proxy."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def free_udp_port() -> int:
    """A UDP port nothing is bound to, for a MAVLink intake socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
