"""The relay can never transmit on its UDP socket.

This is the Stage 0 safety guarantee at its first link: the server cannot
affect flight because the component nearest the aircraft is physically unable
to speak to it (`relay-v1.md` §1, `ARCHITECTURE.md` §3).

Two tests, deliberately. One checks the object, one checks the source. The
object test would pass if someone reached into the private socket; the source
test would pass if someone added a `send` method nobody calls. Together they
are hard to defeat by accident, which is the only way this would ever be
defeated.
"""

from __future__ import annotations

import ast
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent import udp
from agent.udp import ReceiveOnlyUDPSocket

AGENT_ROOT = Path(udp.__file__).resolve().parent

# Every socket method that can put bytes on the wire.
TRANSMIT_METHODS = frozenset(
    {"send", "sendall", "sendto", "sendmsg", "sendfile", "sendmsg_afalg"}
)


@pytest.fixture
def bound_socket() -> Iterator[ReceiveOnlyUDPSocket]:
    # Port 0: let the OS choose, so the test never collides with a real relay
    # or with QGC forwarding on this machine.
    sock = ReceiveOnlyUDPSocket("127.0.0.1", 0, timeout_s=0.01)
    yield sock
    sock.close()


def test_the_socket_wrapper_exposes_no_transmit_method(
    bound_socket: ReceiveOnlyUDPSocket,
) -> None:
    exposed = {name for name in dir(bound_socket) if not name.startswith("_")}

    assert not exposed & TRANSMIT_METHODS, (
        f"ReceiveOnlyUDPSocket exposes {sorted(exposed & TRANSMIT_METHODS)}"
    )


def test_the_wrapper_is_not_a_socket_subclass(
    bound_socket: ReceiveOnlyUDPSocket,
) -> None:
    """Subclassing would inherit sendto and defeat the whole point."""
    assert not isinstance(bound_socket, socket.socket)


@pytest.mark.parametrize("method", sorted(TRANSMIT_METHODS))
def test_transmit_methods_raise_attribute_error(
    bound_socket: ReceiveOnlyUDPSocket, method: str
) -> None:
    with pytest.raises(AttributeError):
        getattr(bound_socket, method)


def _source_files() -> list[Path]:
    return [path for path in AGENT_ROOT.rglob("*.py") if "tests" not in path.parts]


def test_there_are_source_files_to_check() -> None:
    """Guard against this suite passing because it scanned nothing."""
    assert _source_files()


@pytest.mark.parametrize("module", _source_files(), ids=lambda path: str(path.name))
def test_no_module_calls_a_transmit_method(module: Path) -> None:
    """No `.send*(` call anywhere in the relay, on any object.

    Deliberately broader than "not on the UDP socket": a call this test cannot
    prove is safe is a call that should not be in this package. The WebSocket
    uplink sends through the `websockets` library, whose connection objects use
    `.send(...)` — so those are the calls this test would flag, and they live
    in relay.py, which is why the exemption is named there explicitly rather
    than left as a blanket allowance.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in TRANSMIT_METHODS:
            continue
        # The uplink's own sends are on a websockets connection, never a socket.
        if _is_websocket_send(func):
            continue
        offenders.append(f"{func.attr} at line {node.lineno}")

    assert not offenders, (
        f"{module.name} transmits: {', '.join(offenders)}. The relay is "
        f"receive-only toward the aircraft (relay-v1 §1)."
    )


def _is_websocket_send(func: ast.Attribute) -> bool:
    """True for `connection.send(...)` on the uplink WebSocket."""
    return (
        func.attr == "send"
        and isinstance(func.value, ast.Name)
        and (func.value.id == "connection")
    )


def test_the_detector_detects() -> None:
    """A broken invariant check is worse than none."""
    tree = ast.parse("sock.sendto(b'x', ('127.0.0.1', 14445))\n")
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in TRANSMIT_METHODS
    ]

    assert calls, "the AST scan would not notice a sendto call"


def test_receive_returns_none_when_nothing_arrives(
    bound_socket: ReceiveOnlyUDPSocket,
) -> None:
    assert bound_socket.receive() is None


def test_receive_returns_the_datagram_verbatim() -> None:
    """Checked end to end through a real socket, with an external sender."""
    receiver = ReceiveOnlyUDPSocket("127.0.0.1", 0, timeout_s=2.0)
    try:
        port = receiver.bound_endpoint[1]
        payload = bytes(range(256))

        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(payload, ("127.0.0.1", port))
        finally:
            sender.close()

        assert receiver.receive() == payload
    finally:
        receiver.close()
