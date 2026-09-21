"""Ground relay configuration.

TOML, not environment variables, and deliberately so: this file is edited by a
pilot on a laptop, not by an operator with a deployment pipeline. A text file
they can open, read and comment is the right interface; `set COURIER_...` is
not. The rest of the system keeps the environment-based configuration in
`common.config` — this is the one component whose operator is not a developer.

The bearer token is never in this file. It lives in its own file, referenced by
path, so that a configuration can be shared, pasted into a support thread or
committed as an example without leaking a credential.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import AnyWebsocketUrl, BaseModel, ConfigDict, Field, ValidationError

from agent.queue import DEFAULT_QUEUE_MAX_BYTES
from common.config import ConfigurationError

__all__ = ["RelayConfig", "load_config", "read_token"]

# The in-memory hand-off between the UDP thread and the writer thread. Sized so
# a slow disk write cannot stall intake: at the ~12 datagrams/s per aircraft
# measured in ADR-001, this is minutes of buffer, and it is bounded so a
# pathological stall drops datagrams rather than exhausting memory.
DEFAULT_INTAKE_QUEUE_SIZE = 10000


class RelayConfig(BaseModel):
    """Validated relay configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    station_id: str = Field(min_length=1)
    gateway_url: AnyWebsocketUrl
    token_path: Path
    queue_path: Path

    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=14445, ge=1, le=65535)
    queue_max_bytes: int = Field(default=DEFAULT_QUEUE_MAX_BYTES, gt=0)
    intake_queue_size: int = Field(default=DEFAULT_INTAKE_QUEUE_SIZE, gt=0)

    @property
    def uses_tls(self) -> bool:
        """relay-v1 §2 requires wss. Plain ws exists for tests only."""
        return self.gateway_url.scheme == "wss"


def _describe(error: ValidationError, path: Path) -> str:
    lines = [f"invalid relay configuration in {path}:"]
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"]) or "(file)"
        lines.append(f"  {location}: {detail['msg']}")
    return "\n".join(lines)


def load_config(path: Path) -> RelayConfig:
    """Read and validate a relay TOML file.

    Raises ConfigurationError with a message naming the offending key. A pilot
    reading this on a laptop gets a sentence, not a stack trace.
    """
    try:
        raw: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"no relay configuration at {path}. "
            f"Copy agent/relay.example.toml and edit it."
        ) from error
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"{path} is not valid UTF-8 text") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"{path} is not valid TOML: {error}") from error

    try:
        return RelayConfig(**raw)
    except ValidationError as error:
        raise ConfigurationError(_describe(error, path)) from error


def read_token(path: Path) -> str:
    """Read the bearer token, or explain what is wrong with it."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"no token file at {path}. The Gateway operator issues this; it is "
            f"not stored in the relay configuration."
        ) from error
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"token file {path} is not valid UTF-8") from error

    if not token:
        raise ConfigurationError(f"token file {path} is empty")
    return token
