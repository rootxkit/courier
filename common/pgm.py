"""16-bit binary PGM grids with named header values. P5-00.

The terrain tiles are stored the way GeographicLib stores its
geoid grids: a binary PGM ("P5") whose comment lines carry named values
(`# Offset -108`), followed by big-endian unsigned 16-bit samples, row-major.
A stored value v means Offset + Scale * v. Plain enough to read without a
library, and to inspect with `head -c 400`.
"""

from __future__ import annotations

from dataclasses import dataclass


class PgmError(ValueError):
    """A file that is not a 16-bit binary PGM grid."""


@dataclass(frozen=True)
class Pgm:
    width: int
    height: int
    # "# Key value..." comment lines, first word after "#" as the key.
    header: dict[str, str]
    samples: bytes  # width * height big-endian uint16

    def raw(self, column: int, row: int) -> int:
        at = 2 * (row * self.width + column)
        return (self.samples[at] << 8) | self.samples[at + 1]

    def number(self, key: str) -> float:
        if key not in self.header:
            raise PgmError(f"no {key} in the header")
        try:
            return float(self.header[key].split()[0])
        except (ValueError, IndexError) as error:
            raise PgmError(f"{key} is not a number") from error


def parse(data: bytes) -> Pgm:
    position = 0

    def line() -> bytes:
        nonlocal position
        end = data.find(b"\n", position)
        if end < 0:
            raise PgmError("header ends before the data")
        text = data[position:end].strip()
        position = end + 1
        return text

    if line() != b"P5":
        raise PgmError("not a binary PGM (P5)")
    header: dict[str, str] = {}
    while True:
        text = line()
        if not text:
            continue
        if not text.startswith(b"#"):
            break
        words = text[1:].decode("ascii", errors="replace").split(None, 1)
        if len(words) == 2:
            header[words[0]] = words[1].strip()
    try:
        width, height = (int(word) for word in text.split())
        maxval = int(line())
    except ValueError as error:
        raise PgmError(f"unreadable raster size or maxval: {error}") from error
    if maxval != 0xFFFF:
        raise PgmError(f"maxval {maxval}, expected 65535")
    samples = data[position:]
    if len(samples) != 2 * width * height:
        raise PgmError(
            f"{len(samples)} bytes of samples, expected {2 * width * height}"
        )
    return Pgm(width, height, header, samples)


def encode(width: int, height: int, header: dict[str, str], samples: bytes) -> bytes:
    if len(samples) != 2 * width * height:
        raise PgmError("sample count does not match the size")
    lines = [b"P5"] + [
        f"# {key} {value}".encode("ascii") for key, value in header.items()
    ]
    lines.append(f"{width} {height}".encode("ascii"))
    lines.append(b"65535")
    return b"\n".join(lines) + b"\n" + samples
