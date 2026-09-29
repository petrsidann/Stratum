#!/usr/bin/env python3
"""Publish a completed hunt payload into the committed queue layout.

Writes (stdlib only, no network):

    data/hunts/<UTCstamp>Z/hunt_result.json   immutable per-hunt artifact
    data/hunts/latest.json                    status pointer for the PWA poller
                                              (app/src/lib/onDemandHunt.ts)

Usage:
    python3 scripts/publish_hunt_artifacts.py hunt-result.json [--hunt-id ID]

The artifact is normalized to schemas/hunt_result.schema.json: required keys
are asserted, `status` is mapped success->complete so browser-side validation
never rejects a good run, and unknown keys are preserved (schema is open).
Exit codes: 0 published, 2 schema violation (nothing is written).
"""

import argparse
import datetime as dt
import json
import os
import sys

REQUIRED = ("query", "timestamp_utc", "agents_executed",
            "markets_scanned_count", "top_edges", "visual_reports_png",
            "status")

DATA_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "hunts")


def validate(payload: dict) -> str:
    """Return '' if payload satisfies the hunt_result contract, else reason."""
    if not isinstance(payload, dict):
        return "payload is not an object"
    missing = [k for k in REQUIRED if k not in payload]
    if missing:
        return f"missing required keys: {missing}"
    if not isinstance(payload["top_edges"], list):
        return "top_edges must be a list"
    if not isinstance(payload["agents_executed"], list):
        return "agents_executed must be a list"
    if not isinstance(payload["markets_scanned_count"], (int, float)):
        return "markets_scanned_count must be numeric"
    return ""


def publish(payload: dict, hunt_id: str) -> str:
    err = validate(payload)
    if err:
        print(f"[PUBLISH] schema violation: {err}", file=sys.stderr)
        sys.exit(2)

    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    status = str(payload.get("status"))
    mapped = "complete" if status == "success" else (
        status if status in ("no_results", "error", "complete") else "error")

    artifact = dict(payload)
    artifact["status"] = mapped
    artifact.setdefault("schema_version", "3.1.0")

    art_dir = os.path.join(DATA_ROOT, stamp)
    os.makedirs(art_dir, exist_ok=True)
    art_rel = f"data/hunts/{stamp}/hunt_result.json"
    with open(os.path.join(art_dir, "hunt_result.json"), "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, indent=2)

    pointer = {
        "hunt_id": hunt_id or f"cli-{stamp}",
        "query": payload.get("query", ""),
        "sport": payload.get("sport", "auto"),
        "status": mapped,
        "stage": "done",
        "progress": 1.0,
        "updated_at": now.isoformat(),
        "result_path": art_rel,
    }
    with open(os.path.join(DATA_ROOT, "latest.json"), "w", encoding="utf-8") as fh:
        json.dump(pointer, fh, indent=2)

    print(f"[PUBLISH] wrote {art_rel} + data/hunts/latest.json (status={mapped})")
    return art_rel


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("payload_file", help="path to a hunt_result.json payload")
    ap.add_argument("--hunt-id", default=os.environ.get("HUNT_ID", ""),
                    help="correlation id echoed into latest.json")
    args = ap.parse_args(argv)

    with open(args.payload_file, encoding="utf-8") as fh:
        payload = json.load(fh)
    publish(payload, args.hunt_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
