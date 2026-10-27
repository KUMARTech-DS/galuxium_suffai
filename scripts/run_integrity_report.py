#!/usr/bin/env python3
"""Print source integrity report for data_offline fixtures."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from suffai.ingest import load_source_collections, normalize_all, referential_integrity_report


def main() -> None:
    data_dir = ROOT / "data_offline"
    report = referential_integrity_report(normalize_all(load_source_collections(data_dir)))
    print(json.dumps(report, indent=2, default=str))
    gates = report["gates"]
    if not all(gates.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
