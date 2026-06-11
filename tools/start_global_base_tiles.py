#!/usr/bin/env python3
"""Start the Anton global base-tile build as a detached process."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


OUTPUT = Path("/home/slawek/mapgen-output/global-base-tiles")
BUILDER = Path("/home/slawek/mapgen/tools/build_global_base_tiles.py")
GEOJSON = OUTPUT / "countries.geojson"
LOG = OUTPUT / "build.log"


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(BUILDER),
        "--output",
        str(OUTPUT),
        "--geojson",
        str(GEOJSON),
        "--timeout-s",
        "1200",
        "--poll-s",
        "10",
    ]
    log = LOG.open("ab", buffering=0)
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(process.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
