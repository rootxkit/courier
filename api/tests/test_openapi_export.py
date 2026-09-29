"""The committed OpenAPI schema is the one the API serves. P6-01.

web-pilot's TypeScript types are generated from web-pilot/openapi.json. If the
API changes and the file is not regenerated, the console is typed against an
API that no longer exists, and nothing fails until a request does.

FastAPI's own output can change with its version (pyproject pins a floor,
not a ceiling), so this can also fail after a dependency upgrade with no
change to the API. The fix is the same: regenerate, and read the diff,
because the console's types change with it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / "tools" / "export_openapi.py"


def test_the_committed_schema_is_current() -> None:
    spec = importlib.util.spec_from_file_location("export_openapi", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    committed = module.TARGET.read_text(encoding="utf-8")

    assert committed == module.schema(), (
        "web-pilot/openapi.json is out of date: run python tools/export_openapi.py"
    )
