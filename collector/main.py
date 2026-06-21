#!/usr/bin/env python3
"""Collector entrypoint — runs the Mac live sampler and the Biome importer together.

The Biome importer runs on its own background thread (every ``biome_import_interval_seconds``)
while the foreground sampler runs its steady poll loop on the main thread. This is the
process the launchd LaunchAgent starts at login.
"""
from __future__ import annotations

import threading

import importer
import sampler


def main() -> None:
    cfg = sampler.load_settings()

    # Startup self-check: the launchd python3 needs its OWN Full Disk Access grant to read
    # ~/Library/Biome (separate from the Terminal's). Without it the importer silently
    # collects zero iPhone/Watch data. Make that failure loud and obvious at launch.
    ok, why = importer.biome_access_ok()
    if not ok:
        print(
            "\n" + "!" * 72 + "\n"
            "[importer] STARTUP CHECK FAILED — cannot read ~/Library/Biome\n"
            f"  reason: {why}\n"
            "  No iPhone / Apple Watch data will import until fixed.\n"
            "  Fix: System Settings -> Privacy & Security -> Full Disk Access -> add\n"
            "  /opt/homebrew/Cellar/python@3.13/3.13.5/Frameworks/Python.framework/"
            "Versions/3.13/Resources/Python.app\n"
            "  then restart: launchctl kickstart -k gui/$(id -u)/com.saad.timetracker.collector\n"
            + "!" * 72,
            flush=True,
        )
    else:
        print("[importer] startup check OK — Biome readable.", flush=True)

    t = threading.Thread(
        target=importer.run,
        args=(int(cfg.get("biome_import_interval_seconds", 600)),),
        daemon=True,
    )
    t.start()

    sampler.run()  # blocks forever


if __name__ == "__main__":
    main()
