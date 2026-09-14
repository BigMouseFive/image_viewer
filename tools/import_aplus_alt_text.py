#!/usr/bin/env python3
"""将 ai-relay 导出的 A+ Alt Text 导入 image-reviewer。

只导入到已扫描且当前存在的 SKU + A+Lxx 图片。Alt Text 随图片自动交付，
无需单独确认；如有必要可在图片评审窗口直接修改。
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.db import Database  # noqa: E402

MODULES = ("A+L01", "A+L02", "A+L03", "A+L04", "A+L05")


def main() -> int:
    parser = argparse.ArgumentParser(description="导入 A+ Alt Text 草稿")
    parser.add_argument("--csv", required=True, dest="csv_path")
    parser.add_argument("--database", default=str(ROOT / "data" / "reviews.db"))
    parser.add_argument("--source-id", type=int, default=0, help="默认使用当前激活图片源")
    args = parser.parse_args()

    with Path(args.csv_path).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"SKU"} | {f"{module}_alt_text" for module in MODULES}
    missing = required - set(rows[0] if rows else ())
    if missing:
        raise ValueError(f"CSV 缺少字段：{', '.join(sorted(missing))}")

    db = Database(Path(args.database))
    source = db.source(args.source_id) if args.source_id else db.active_source()
    if not source:
        raise RuntimeError("没有可用图片源；请先启动 image-reviewer 并完成扫描")
    assets = db.assets(source["id"])
    asset_by_key = {}
    for asset in assets:
        module = asset["module"].rsplit("_", 1)[-1]
        if module in MODULES:
            asset_by_key[(asset["sku"], module)] = asset

    imported = missing_assets = 0
    for row in rows:
        sku = row["SKU"].strip()
        for module in MODULES:
            text = " ".join(row[f"{module}_alt_text"].split())
            if not text:
                continue
            asset = asset_by_key.get((sku, module))
            if not asset:
                missing_assets += 1
                continue
            db.upsert_alt_text(asset["id"], text, "prompt-derived", "ready", asset["revision"])
            imported += 1

    print(f"图片源 #{source['id']}：导入 {imported} 条 Alt Text；未匹配当前图片 {missing_assets} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
