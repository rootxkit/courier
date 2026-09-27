"""Regenerate the pinned MAVLink field-unit table from pymavlink's XML.

    python tools/refresh_mavlink_units.py

## Why this exists

`gateway/units.py` derives every scaling factor from pymavlink's generated
`fieldunits_by_name`, and CLAUDE.md requires that to be pinned by a test that
derives the same facts a *different* way. The independent source is pymavlink's
own XML message definitions.

**Those definitions are not installed on every platform.** Measured on
2026-09-24:

    pymavlink 2.4.49 / 2.4.50, Windows wheel     19 XML files
    pymavlink 2.4.49 / 2.4.50, sdist             21 XML files
    pymavlink 2.4.49 / 2.4.50, manylinux wheel    0 XML files

It is a packaging difference between platforms, not a version change - both
versions behave identically. So a test that reads the XML directly passes on a
Windows developer machine and cannot run on Linux CI, which is exactly what
happened: `test_the_generated_units_match_the_xml_definitions` passed locally
and failed on its first CI run with every field reporting `xml=None`.

The fix is to extract the table **here**, commit it, and have the test compare
the installed Python metadata against the committed file. That still satisfies
the rule - the pin comes from the XML, by a different route than the code uses
- and it works on a platform where the XML is absent.

## When to run this

When pymavlink is upgraded and `test_units.py` reports a mismatch. A mismatch
means the dialect changed a unit or a description, which is a real event: read
the diff before regenerating, because a changed unit changes a scaling factor.

Run it on Windows or against an sdist install, where the XML is present.
"""

from __future__ import annotations

import json
import pathlib
import sys
import xml.etree.ElementTree as ElementTree

import pymavlink

from gateway.classify import HOT_PATH_MESSAGE_NAMES

DEFINITIONS = pathlib.Path(pymavlink.__file__).parent / "message_definitions" / "v1.0"
OUTPUT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "gateway"
    / "tests"
    / "data"
    / "mavlink_fields.json"
)

# Read in this order; the first definition of a message wins, matching how
# pymavlink resolves a dialect built from includes.
#
# minimal.xml is in the list because HEARTBEAT lives there - upstream MAVLink
# moved it out of common.xml, which common.xml then includes. Leaving it out
# made the first run of this script report "no XML definition found for:
# HEARTBEAT" rather than silently omitting it, which is the right failure.
DIALECT_FILES = ("minimal.xml", "standard.xml", "common.xml", "ardupilotmega.xml")


def extract() -> dict[str, dict[str, dict[str, str]]]:
    """(message -> field -> {units, description}) for the hot-path messages."""
    wanted = set(HOT_PATH_MESSAGE_NAMES)
    found: dict[str, dict[str, dict[str, str]]] = {}

    for definition in DIALECT_FILES:
        path = DEFINITIONS / definition
        if not path.exists():
            continue
        for message in ElementTree.parse(path).iter("message"):
            name = message.get("name") or ""
            if name not in wanted or name in found:
                continue
            fields: dict[str, dict[str, str]] = {}
            for field in message.iter("field"):
                field_name = field.get("name") or ""
                fields[field_name] = {
                    "units": field.get("units") or "",
                    "description": " ".join((field.text or "").split()),
                }
            found[name] = fields
    return found


def main() -> int:
    if not DEFINITIONS.exists():
        print(
            f"pymavlink ships no message definitions at {DEFINITIONS}.\n"
            f"Linux wheels omit them; run this on Windows or against an sdist "
            f"install (`pip install --no-binary pymavlink pymavlink`).",
            file=sys.stderr,
        )
        return 2

    extracted = extract()
    missing = sorted(set(HOT_PATH_MESSAGE_NAMES) - set(extracted))
    if missing:
        print(f"no XML definition found for: {', '.join(missing)}", file=sys.stderr)
        return 1

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_source": "pymavlink message_definitions/v1.0, via tools/refresh_mavlink_units.py",
        "_pymavlink_version": _installed_version(),
        "messages": extracted,
    }
    OUTPUT.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    fields = sum(len(v) for v in extracted.values())
    print(f"wrote {OUTPUT.relative_to(OUTPUT.parent.parent.parent.parent)}")
    print(f"  {len(extracted)} messages, {fields} fields")
    return 0


def _installed_version() -> str:
    from importlib.metadata import version

    return version("pymavlink")


if __name__ == "__main__":
    sys.exit(main())
