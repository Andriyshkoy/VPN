#!/usr/bin/env python3
"""Fail when the committed OpenAPI contract differs from application routes."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vpn_control.app import app


def main():
    path = ROOT.parents[1] / "contracts/node-agent-v1/openapi.json"
    committed = json.loads(path.read_text())
    actual = app.openapi()
    if committed != actual:
        raise SystemExit("OpenAPI contract differs; regenerate contracts/node-agent-v1/openapi.json")
    print("OpenAPI contract matches application routes")


if __name__ == "__main__":
    main()
