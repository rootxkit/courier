"""The UDP intake socket — receive only.

At Stage 0 the server cannot affect flight. `ARCHITECTURE.md` §3 rests on that,
and `relay-v1.md` §1 states it as an absolute rule. This module is where it is
enforced: the socket object is private, and this class exposes no method that
can transmit.

That is deliberately stronger than "we do not call send". A capability no code
can express cannot be reached by accident, by a refactor, or by someone who has
not read the architecture. When a command path is eventually built it arrives
through `mavlink-router` as a separate component (P3B-01), not through here.
"""

from __future__ import annotations

import socket

from agent.framing import UDP_RECEIVE_BUFFER_BYTES

__all__ = ["ReceiveOnlyUDPSocket"]


class ReceiveOnlyUDPSocket:
    """A bound UDP socket that can receive and close, and nothing else."""

    def __init__(self, host: str, port: int, *, timeout_s: float = 0.5) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((host, port))
        self._socket.settimeout(timeout_s)
        self._host = host
        self._port = port

    @property
    def endpoint(self) -> tuple[str, int]:
        """The configured endpoint, as asked for."""
        return self._host, self._port

    @property
    def bound_endpoint(self) -> tuple[str, int]:
        """The endpoint actually bound, which differs when port 0 was asked for."""
        host, port = self._socket.getsockname()
        return str(host), int(port)

    def receive(self) -> bytes | None:
        """Return the next datagram, or None if none arrived before the timeout.

        The sender's address is deliberately discarded. The relay does not
        reply, so it has no use for a return path.
        """
        try:
            datagram, _address = self._socket.recvfrom(UDP_RECEIVE_BUFFER_BYTES)
        except TimeoutError:
            return None
        except OSError:
            # The socket was closed underneath us during shutdown.
            return None
        return datagram

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> ReceiveOnlyUDPSocket:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
