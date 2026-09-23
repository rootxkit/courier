"""No test may roll its own free-port probe.

`agent/tests/test_halfopen.py` chose the relay's UDP intake port with a
`SOCK_STREAM` probe and shipped that way. Two other test files had already got
it right, so the knowledge existed and did not travel. A shared helper only
prevents the next occurrence if nothing bypasses it, which is what this checks.
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest

from tests.ports import free_tcp_port, free_udp_port

REPO_ROOT = Path(__file__).resolve().parent.parent

TEST_DIRECTORIES = ("agent", "gateway", "common", "tools", "tests")

# Names that mean "give me a port". Anything defining one of these locally is
# a probe that escaped review.
PROBE_NAMES = frozenset(
    {"free_port", "free_tcp_port", "free_udp_port", "_free_port", "_free_udp_port"}
)

ALLOWED = {REPO_ROOT / "tests" / "ports.py"}


def python_test_files() -> list[Path]:
    found: list[Path] = []
    for directory in TEST_DIRECTORIES:
        found.extend((REPO_ROOT / directory).rglob("test_*.py"))
        found.extend((REPO_ROOT / directory).rglob("tests/*.py"))
    return sorted({path for path in found if path.is_file()})


def test_no_test_file_defines_its_own_port_probe() -> None:
    offenders: list[str] = []
    for path in python_test_files():
        if path in ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in PROBE_NAMES:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno} "
                    f"defines {node.name}()"
                )

    assert not offenders, (
        "port probes must come from tests/ports.py, which forces the caller to "
        "name the protocol:\n  " + "\n  ".join(offenders)
    )


def test_the_helper_actually_reserves_the_protocol_it_names() -> None:
    """The presence half. A helper that returned a constant would pass above.

    Each port is bound on the protocol it was issued for, which is the property
    the whole exercise is about.
    """
    tcp_port = free_tcp_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", tcp_port))
        server.listen(1)

    udp_port = free_udp_port()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", udp_port))


def test_tcp_and_udp_are_separate_port_spaces() -> None:
    """The measurement the helper exists because of.

    A port held on UDP is bound happily by a TCP probe. That is why there is no
    protocol-agnostic `free_port()`: it cannot be right for both.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as held:
        held.bind(("127.0.0.1", 0))
        port = held.getsockname()[1]

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
            # No error: the TCP space does not know the UDP port is taken.
            tcp.bind(("127.0.0.1", port))

        with (
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as clash,
            pytest.raises(OSError),
        ):
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                clash.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            clash.bind(("127.0.0.1", port))
