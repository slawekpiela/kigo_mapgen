#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "kigo_airspace.py"


def load_module():
    spec = importlib.util.spec_from_file_location("kigo_airspace", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["kigo_airspace"] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="kigo_airspace_test_") as tmp:
        tmp_path = Path(tmp)
        package_dir = tmp_path / "packages"
        cache_dir = tmp_path / "cache"
        package_dir.mkdir()
        (package_dir / "PL.txt").write_text(
            """
* sample package

AC R
AN LOW AREA
AL SFC
AH FL120
DP 49:00:00 N 019:00:00 E
DP 49:10:00 N 019:00:00 E
DP 49:10:00 N 019:10:00 E

AC R
AN HIGH AREA
AL FL120
AH FL160
DP 49:00:00 N 019:00:00 E
DP 49:10:00 N 019:00:00 E
DP 49:10:00 N 019:10:00 E
""".strip()
            + "\n",
            encoding="utf-8",
        )

        os.environ["KIGO_AIRSPACE_PACKAGE_DIRS"] = str(package_dir)
        os.environ["KIGO_AIRSPACE_CACHE_DIR"] = str(cache_dir)
        module = load_module()

        payload = {
            "countries": [
                {"code": "PL", "controlled_airspace_base": "FL95"},
            ],
            "task": {
                "points": [
                    {"lat": 49.05, "lon": 19.05},
                ],
            },
        }

        first = module.build_airspace_response(payload)
        text = first.data.decode("utf-8")
        assert "AN LOW AREA" in text
        assert "AH FL95" in text
        assert "AN HIGH AREA" not in text
        assert first.cache_status == "miss-filled"

        second = module.build_airspace_response(json.loads(json.dumps(payload)))
        assert second.data == first.data
        assert second.cache_status == "hit"

    print("ok")


if __name__ == "__main__":
    main()
