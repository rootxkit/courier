"""Receiver keys: made once per receiver, readable by the ingest. P1-15."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from gateway.remote_id_auth import load_keys
from tools.remote_id_keys import main, new_key


def test_a_new_key_is_added_and_the_ingest_reads_it(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "receivers.keys"

    first = new_key(path, "rx-1")
    second = new_key(path, "rx-2")

    keys = load_keys(path)
    assert keys == {"rx-1": base64.b64decode(first), "rx-2": base64.b64decode(second)}
    assert len(keys["rx-1"]) == 32
    assert keys["rx-1"] != keys["rx-2"]


@pytest.mark.parametrize("receiver", ["", "a:b", " rx"])
def test_a_receiver_id_that_would_not_read_back_is_refused(
    tmp_path: Path, receiver: str
) -> None:
    with pytest.raises(ValueError, match="receiver id"):
        new_key(tmp_path / "k", receiver)


def test_a_receiver_is_given_one_key_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "k"
    assert main(["new", "rx-1", "--file", str(path)]) == 0
    assert "rx-1: " in capsys.readouterr().out

    assert main(["new", "rx-1", "--file", str(path)]) == 1
    assert "already has a key" in capsys.readouterr().err
    assert len(load_keys(path)) == 1
