#!/usr/bin/env python3
"""Short live smoke test for the local fallback pipeline; never touches primary storage."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import failover_guardian as guardian  # noqa: E402


def main() -> int:
    test_dir = guardian.ROOT / "recordings" / "failover-selftest"
    if test_dir.exists():
        raise RuntimeError(f"Refusing to reuse self-test directory: {test_dir}")
    config = guardian.DEFAULTS.copy()
    if guardian.CONFIG_PATH.exists():
        config.update(guardian.load_json(guardian.CONFIG_PATH))
    config["FallbackDirectory"] = str(test_dir)
    config["FallbackSegmentMinutes"] = 5
    recorder_config = guardian.load_json(guardian.RECORDER_CONFIG_PATH)

    logger = logging.getLogger("failover-selftest")
    logger.addHandler(logging.NullHandler())
    pipeline = guardian.FallbackPipeline(config, recorder_config, logger)
    try:
        pipeline.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            files = pipeline.files()
            if files and files[-1].stat().st_size >= 2 * 1024 * 1024:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Fallback file did not reach 2 MiB within 30 seconds")
    finally:
        files = pipeline.stop()

    checker = guardian.Guardian(config, recorder_config, logger)
    results = []
    for path in files:
        ok, detail = checker.validate(path)
        results.append({"File": str(path), "Bytes": path.stat().st_size, "AVScan": ok, "Detail": detail})
    print(json.dumps({"TestDirectory": str(test_dir), "Results": results}, ensure_ascii=False))
    return 0 if results and all(row["AVScan"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
