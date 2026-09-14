#!/usr/bin/env python3
"""Build an image-reviewer inventory manifest from an A+ prompt CSV."""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.inventory import MANIFEST_NAME, MODULES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt-csv", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--generation-log", action="append", type=Path, default=[])
    args = parser.parse_args()
    latest = {}
    for log in args.generation_log:
        if not log.exists():
            continue
        with log.open(newline="", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                latest[(row.get("SKU", ""), row.get("module", ""))] = row
    items = []
    with args.prompt_csv.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            sku = row["SKU"].strip()
            for module in MODULES:
                key = (sku, module["id"])
                log = latest.get(key, {})
                items.append({
                    "sku": sku, "module": module["id"],
                    "path": f"{sku}/{sku}_{module['id']}.png",
                    "width": module["width"], "height": module["height"], "required": True,
                    "generation_status": log.get("status", "ready"),
                    "reason": log.get("reason", log.get("error", "")),
                })
    manifest = {
        "schema_version": 1, "kind": "amazon-basic-aplus",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {"prompt_csv": str(args.prompt_csv)}, "modules": MODULES, "items": items,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.output_dir / MANIFEST_NAME
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {target} ({len(items)} expected images)")


if __name__ == "__main__":
    main()
