import os
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import time

import pytest
from PIL import Image

from app.db import Database
from app.scanner import refresh_asset, scan
from app.inventory import apply_product_exceptions, attach_references, filter_inventory, reconcile, reference_images
from app.iopaint import backup_asset, resolve_editable_asset
from app.main import (
    attach_delivery_eligibility,
    delivery_counts,
    delivery_filter_matches,
    delivery_slots_and_errors,
    delivery_sort_key,
)


def make_source(db, path):
    source_id = db.add_source("test", str(path))
    db.activate_source(source_id)
    return source_id


def write_image(path, color, size=(16, 12)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    current = time.time_ns()
    os.utime(path, ns=(current, current))


def test_legacy_migration_preserves_latest_content_change_time(tmp_path):
    database_path = tmp_path / "legacy-reviews.db"
    with sqlite3.connect(database_path) as con:
        con.executescript("""
            CREATE TABLE assets (
              id INTEGER PRIMARY KEY, sku TEXT NOT NULL, module TEXT NOT NULL,
              relative_path TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL,
              sha256 TEXT NOT NULL, width INTEGER, height INTEGER,
              revision INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'unreviewed',
              comments TEXT NOT NULL DEFAULT '', reviewed_revision INTEGER NOT NULL DEFAULT -1,
              discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE versions (
              id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, revision INTEGER NOT NULL,
              sha256 TEXT NOT NULL, comments TEXT NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(asset_id, revision)
            );
        """)
        con.execute(
            """INSERT INTO assets(
               id,sku,module,relative_path,size,mtime,sha256,width,height,revision,status,
               comments,reviewed_revision,discovered_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                7, "SKU-1", "SKU-1_A+L01", "SKU-1/SKU-1_A+L01.png", 1, 1.0,
                "current-sha", 970, 600, 2, "approved", "", 2,
                "2026-08-01T00:00:00+00:00", "2026-09-10T00:00:00+00:00",
            ),
        )
        # Scanner/AI writes the archived prior revision at the time the current
        # image content was introduced. Migration must retain that ordering cue.
        con.execute(
            "INSERT INTO versions(asset_id,revision,sha256,comments,created_at) VALUES(?,?,?,?,?)",
            (7, 1, "prior-sha", "", "2026-09-09T12:00:00+00:00"),
        )

    db = Database(database_path)
    source_id = db.add_source("legacy", str(tmp_path / "images"))
    db.migrate_legacy(source_id)

    migrated = db.assets(source_id)
    assert len(migrated) == 1
    assert migrated[0]["revision"] == 2
    assert migrated[0]["content_updated_at"] == "2026-09-09T12:00:00+00:00"


def test_scanner_records_decoded_format_not_only_filename_extension(tmp_path):
    root = tmp_path / "images"
    png = root / "SKU-1" / "valid.png"
    disguised = root / "SKU-1" / "disguised.png"
    write_image(png, "red", (970, 600))
    disguised.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (970, 300), "blue").save(disguised, format="JPEG")
    db = Database(tmp_path / "reviews.db")
    source_id = make_source(db, root)

    scan(root, db, source_id)
    assets = {asset["module"]: asset for asset in db.assets(source_id)}

    assert assets["valid"]["image_format"] == "PNG"
    assert assets["disguised"]["image_format"] == "JPEG"


def test_overwrite_moves_reviewed_asset_to_pending_and_updates_dimensions(tmp_path):
    root = tmp_path / "images"
    image = root / "SKU-1" / "module.png"
    write_image(image, "red")
    db = Database(tmp_path / "reviews.db")
    source_id = make_source(db, root)

    scan(root, db, source_id)
    asset = db.assets(source_id)[0]
    db.update(asset["id"], "needs_revision", "fix it", asset["revision"], source_id)

    write_image(image, "blue", (24, 18))
    scan(root, db, source_id)
    updated = db.assets(source_id)[0]

    assert updated["revision"] == 1
    assert updated["status"] == "modified_pending_review"
    assert (updated["width"], updated["height"]) == (24, 18)
    with db.connect() as con:
        version = con.execute("SELECT * FROM versions WHERE asset_id=?", (asset["id"],)).fetchone()
    assert version["revision"] == 0
    assert version["sha256"] == asset["sha256"]


def test_overwrite_moves_delivered_asset_back_to_pending_review(tmp_path):
    root = tmp_path / "images"
    image = root / "SKU-1" / "SKU-1_A+L01.png"
    write_image(image, "red", (970, 600))
    db = Database(tmp_path / "reviews.db")
    source_id = make_source(db, root)
    scan(root, db, source_id)
    asset = db.assets(source_id)[0]
    db.update(asset["id"], "delivered", "", asset["revision"], source_id)

    write_image(image, "blue", (970, 600))
    scan(root, db, source_id)
    updated = db.assets(source_id)[0]

    assert updated["revision"] == 1
    assert updated["status"] == "modified_pending_review"


def test_alt_text_is_kept_when_image_revision_changes(tmp_path):
    root = tmp_path / "images"
    image = root / "SKU-1" / "SKU-1_A+L01.png"
    write_image(image, "red", (970, 600))
    db = Database(tmp_path / "reviews.db")
    source_id = make_source(db, root)
    scan(root, db, source_id)
    asset = db.assets(source_id)[0]

    db.upsert_alt_text(asset["id"], "Red product image.", "prompt-derived", "ready", asset["revision"])
    saved = db.alt_text(asset["id"])
    assert saved is not None
    assert saved["alt_text"] == "Red product image."
    assert saved["review_status"] == "ready"

    write_image(image, "blue", (970, 600))
    scan(root, db, source_id)
    changed = db.assets(source_id)[0]
    assert changed["revision"] == 1
    changed_alt_text = db.alt_text(changed["id"])
    assert changed_alt_text is not None
    assert changed_alt_text["alt_text"] == "Red product image."


def test_delivery_requires_all_five_slots_to_be_confirmed_or_ignored():
    profile = {
        "slots": [
            {"slot_key": f"slot-{index}", "sequence": index, "module": f"A+L{index:02d}", "width": 970, "height": 600}
            for index in range(1, 6)
        ]
    }
    assets = {
        module: {
            "id": index, "inventory_status": "present", "status": "approved" if index != 3 else "needs_revision",
            "reviewed_revision": 0 if index != 3 else -1, "revision": 0,
            "width": 970, "height": 600, "sha256": str(index),
        }
        for index, module in enumerate((f"A+L{index:02d}" for index in range(1, 6)), start=1)
    }
    alt_texts = {asset["id"]: {"alt_text": f"Description {asset['id']}"} for asset in assets.values()}

    slots, errors = delivery_slots_and_errors(profile, assets, alt_texts)
    assert slots == []
    assert any("A+L03" in error and "尚未完成处理" in error for error in errors)

    assets["A+L03"]["status"] = "ignored"
    assets["A+L03"]["reviewed_revision"] = 0
    slots, errors = delivery_slots_and_errors(profile, assets, alt_texts)
    assert len(slots) == 5
    assert errors == []


def test_unchanged_delivered_assets_can_be_used_in_a_new_delivery():
    profile = {
        "slots": [
            {"slot_key": f"slot-{index}", "sequence": index, "module": f"A+L{index:02d}", "width": 970, "height": 600}
            for index in range(1, 6)
        ]
    }
    assets = {
        module: {
            "id": index, "inventory_status": "present", "status": "delivered",
            "reviewed_revision": 0, "revision": 0,
            "width": 970, "height": 600, "sha256": str(index),
        }
        for index, module in enumerate((f"A+L{index:02d}" for index in range(1, 6)), start=1)
    }
    alt_texts = {asset["id"]: {"alt_text": f"Description {asset['id']}"} for asset in assets.values()}

    slots, errors = delivery_slots_and_errors(profile, assets, alt_texts)

    assert len(slots) == 5
    assert errors == []


def test_delivery_eligibility_marks_only_complete_sku():
    items = [
        {
            "id": index, "sku": "SKU-1", "module": f"SKU-1_A+L{index:02d}", "asset_role": "deliverable",
            "inventory_status": "present", "status": "approved", "reviewed_revision": 0, "revision": 0,
            "width": 970, "height": 600, "sha256": str(index),
        }
        for index in range(1, 6)
    ]
    original = __import__("app.main", fromlist=["db"]).db
    fake = Database(Path(tempfile.mkdtemp()) / "reviews.db")
    try:
        with fake.connect() as con:
            con.execute("PRAGMA foreign_keys=OFF")
            con.executemany(
                "INSERT INTO aplus_alt_texts(asset_id,alt_text,source,review_status,reviewed_revision,updated_at) VALUES(?,?,?,?,?,?)",
                [(item["id"], "Description", "test", "ready", 0, "now") for item in items],
            )
        __import__("app.main", fromlist=["db"]).db = fake
        assert all(item["sku_deliverable"] for item in attach_delivery_eligibility(items))
        items[2]["status"] = "needs_revision"
        assert not any(item["sku_deliverable"] for item in attach_delivery_eligibility(items))
    finally:
        __import__("app.main", fromlist=["db"]).db = original


def test_delivery_view_prioritizes_only_changed_or_initial_skus(tmp_path, monkeypatch):
    db = Database(tmp_path / "reviews.db")
    monkeypatch.setattr(__import__("app.main", fromlist=["db"]), "db", db)
    root = tmp_path / "images"
    source_id = make_source(db, root)
    now_value = "2026-09-15T12:00:00+00:00"
    rows = []
    for sku in ("UNCHANGED", "CHANGED", "INITIAL"):
        for index in range(1, 6):
            module = f"A+L{index:02d}"
            rows.append((
                source_id, sku, module, f"{sku}/{sku}_{module}.png", 1, 1.0,
                f"{sku}-{index}-sha", 970, 600, "PNG", 0, "delivered" if sku == "UNCHANGED" else "approved",
                "", 0, now_value, now_value, now_value,
            ))
    with db.connect() as con:
        con.executemany(
            """INSERT INTO assets(
               source_id,sku,module,relative_path,size,mtime,sha256,width,height,image_format,
               revision,status,comments,reviewed_revision,discovered_at,content_updated_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    assets = db.assets(source_id)
    by_sku = {}
    for asset in assets:
        by_sku.setdefault(asset["sku"], []).append(asset)
        db.upsert_alt_text(asset["id"], f"{asset['sku']} {asset['module']}", "test", "ready", asset["revision"])

    def saved_alt_text(asset_id):
        value = db.alt_text(asset_id)
        assert value is not None
        return value["alt_text"]

    # Freeze an already-synced v1 for UNCHANGED and CHANGED. CHANGED then gets
    # a freshly reviewed image revision in A+L03; its old snapshot remains v1.
    for sku in ("UNCHANGED", "CHANGED"):
        slots = [
            {
                "slot_key": f"slot-{index}", "sequence": index, "module": f"A+L{index:02d}",
                "asset_id": asset["id"], "revision": asset["revision"], "sha256": asset["sha256"],
                "width": 970, "height": 600,
                "alt_text": saved_alt_text(asset["id"]),
            }
            for index, asset in enumerate(by_sku[sku], start=1)
        ]
        delivery = db.create_delivery(f"source-{sku}", sku, "uae-aplus-five-image-v1", f"fingerprint-{sku}", ["B000000001"], slots)
        with db.connect() as con:
            con.execute("UPDATE aplus_deliveries SET status='synced' WHERE id=?", (delivery["id"],))

    changed = next(asset for asset in by_sku["CHANGED"] if asset["module"] == "A+L03")
    metadata_changed = next(asset for asset in by_sku["CHANGED"] if asset["module"] == "A+L01")
    with db.connect() as con:
        con.execute(
            """UPDATE assets
               SET revision=1, sha256=?, status='approved', reviewed_revision=1,
                   content_updated_at=?, updated_at=?
               WHERE id=?""",
            ("changed-new-sha", "2026-09-16T08:00:00+00:00", "2026-09-16T08:00:00+00:00", changed["id"]),
        )
        con.execute(
            "UPDATE aplus_alt_texts SET alt_text=?,updated_at=? WHERE asset_id=?",
            ("Later metadata edit", "2026-09-17T08:00:00+00:00", metadata_changed["id"]),
        )

    attached = attach_delivery_eligibility(reconcile(db.assets(source_id), None), source_id)
    summary_by_sku = {}
    for item in attached:
        summary_by_sku.setdefault(item["sku"], item["delivery"])

    assert summary_by_sku["UNCHANGED"]["state"] == "delivered_current"
    assert summary_by_sku["UNCHANGED"]["required"] is False
    assert summary_by_sku["CHANGED"]["state"] == "image_updated"
    assert summary_by_sku["CHANGED"]["required"] is True
    assert summary_by_sku["CHANGED"]["image_changed_modules"] == ["A+L03"]
    assert summary_by_sku["CHANGED"]["metadata_changed_modules"] == ["A+L01"]
    changed_item = next(item for item in attached if item["sku"] == "CHANGED")
    assert delivery_filter_matches(changed_item, "deliverable", "image_updated") is True
    pending_changed = dict(
        changed_item,
        delivery=dict(changed_item["delivery"], ready=False, required=False),
    )
    assert delivery_filter_matches(pending_changed, "deliverable", "image_updated") is True
    assert delivery_filter_matches(pending_changed, "deliverable", "all") is False
    # A later metadata save must not make an older image revision look newer
    # than another image-adjusted product in the delivery ordering.
    assert delivery_sort_key(changed_item, "deliverable")[1] == -datetime.fromisoformat(
        "2026-09-16T08:00:00+00:00"
    ).timestamp()
    assert summary_by_sku["INITIAL"]["state"] == "initial_delivery"
    assert summary_by_sku["INITIAL"]["required"] is True
    assert delivery_counts(attached) == {
        "deliverable": 2,
        "image_updated": 1,
        "image_updated_ready": 1,
        "image_updated_pending_review": 0,
        "metadata_updated": 0,
        "metadata_updated_ready": 0,
        "initial_delivery": 1,
        "delivered": 1,
    }


def test_delivery_versions_are_immutable_and_fingerprinted(tmp_path):
    db = Database(tmp_path / "reviews.db")
    slots = [{
        "slot_key": "hero_banner", "sequence": 1, "module": "A+L01", "asset_id": 1,
        "revision": 0, "sha256": "a" * 64, "width": 970, "height": 600, "alt_text": "Banner image.",
    }]
    first = db.create_delivery("source-1", "SKU-1", "uae-aplus-five-image-v1", "f" * 64, ["B000000001"], slots)
    second = db.create_delivery("source-2", "SKU-1", "uae-aplus-five-image-v1", "e" * 64, [], slots)
    assert first["version"] == 1
    assert first["status"] == "ready_to_sync"
    assert second["version"] == 2
    assert second["status"] == "draft"


def test_refresh_asset_updates_only_requested_file_and_detects_deletion(tmp_path):
    root = tmp_path / "images"
    first_path = root / "SKU-1" / "first.png"
    second_path = root / "SKU-1" / "second.png"
    write_image(first_path, "red")
    write_image(second_path, "green")
    db = Database(tmp_path / "reviews.db")
    source_id = make_source(db, root)
    scan(root, db, source_id)
    assets = {item["module"]: item for item in db.assets(source_id)}
    first, second = assets["first"], assets["second"]
    db.update(first["id"], "approved", "", first["revision"], source_id)

    write_image(first_path, "blue", (24, 18))
    refreshed = refresh_asset(root, db, source_id, first["id"])
    untouched = next(item for item in db.assets(source_id) if item["id"] == second["id"])
    assert refreshed is not None
    assert refreshed["revision"] == 1
    assert refreshed["status"] == "modified_pending_review"
    assert (refreshed["width"], refreshed["height"]) == (24, 18)
    assert untouched["updated_at"] == second["updated_at"]

    first_path.unlink()
    assert refresh_asset(root, db, source_id, first["id"]) is None
    assert all(item["id"] != first["id"] for item in db.assets(source_id))


def test_stale_revision_cannot_approve_new_content(tmp_path):
    root = tmp_path / "images"
    image = root / "SKU-1" / "module.png"
    write_image(image, "red")
    db = Database(tmp_path / "reviews.db")
    source_id = make_source(db, root)

    scan(root, db, source_id)
    stale = db.assets(source_id)[0]
    write_image(image, "blue")
    scan(root, db, source_id)

    with pytest.raises(RuntimeError, match="revision conflict"):
        db.update(stale["id"], "approved", "", stale["revision"], source_id)
    assert db.assets(source_id)[0]["status"] == "unreviewed"


def test_manifest_reconciles_missing_blocked_extra_and_dimensions(tmp_path):
    actual = [{"id": 1, "sku": "SKU-1", "module": "A+L01", "relative_path": "SKU-1/SKU-1_A+L01.png", "width": 970, "height": 300}, {"id": 2, "sku": "SKU-2", "module": "old.jpg", "relative_path": "_refs/SKU-2.jpg", "width": 100, "height": 100}]
    manifest = {"schema_version": 1, "items": [
        {"sku": "SKU-1", "module": "A+L01", "path": "SKU-1/SKU-1_A+L01.png", "width": 970, "height": 600, "generation_status": "ready"},
        {"sku": "SKU-1", "module": "A+L02", "path": "SKU-1/SKU-1_A+L02.png", "width": 970, "height": 600, "generation_status": "blocked"},
        {"sku": "SKU-1", "module": "A+L03", "path": "SKU-1/SKU-1_A+L03.png", "width": 970, "height": 600, "generation_status": "ready"},
    ]}
    result = reconcile(actual, manifest)
    statuses = {(item["sku"], item["module"]): item["inventory_status"] for item in result}
    assert statuses[("SKU-1", "A+L01")] == "invalid_dimensions"
    assert statuses[("SKU-1", "A+L02")] == "blocked"
    assert statuses[("SKU-1", "A+L03")] == "missing"
    assert statuses[("SKU-2", "old.jpg")] == "extra"


def test_inventory_filter_accepts_multiple_statuses():
    items = [
        {"sku": "SKU-1", "module": "A+L01", "relative_path": "SKU-1/A+L01.png", "inventory_status": "present"},
        {"sku": "SKU-2", "module": "A+L01", "relative_path": "SKU-2/A+L01.png", "inventory_status": "missing"},
        {"sku": "SKU-3", "module": "A+L01", "relative_path": "SKU-3/A+L01.png", "inventory_status": "blocked"},
    ]
    assert [item["sku"] for item in filter_inventory(items, {"present", "missing"})] == ["SKU-1", "SKU-2"]
    assert len(filter_inventory(items, {"all"})) == 3


def test_reference_image_is_attached_to_matching_sku_only():
    items = [
        {"id": 1, "sku": "AC0002", "module": "A+L01", "relative_path": "AC0002/AC0002_A+L01.png", "asset_role": "deliverable"},
        {"id": 2, "sku": "AC0003", "module": "A+L01", "relative_path": "AC0003/AC0003_A+L01.png", "asset_role": "deliverable"},
        {"id": 3, "sku": "_refs", "module": "AC0002", "relative_path": "_refs/AC0002.jpg", "asset_role": "reference", "reference_sku": "AC0002"},
    ]
    attached = attach_references(items[:2], reference_images(items))
    assert [item["id"] for item in attached[0]["reference_images"]] == [3]
    assert attached[1]["reference_images"] == []


def test_suggestions_can_be_created_updated_moved_and_soft_deleted(tmp_path):
    db = Database(tmp_path / "reviews.db")
    first = db.create_suggestion("文字", "检查文字")
    second = db.create_suggestion("外观", "检查外观")

    updated = db.update_suggestion(first["id"], "文字错误", "修正文字错误")
    assert (updated["title"], updated["content"]) == ("文字错误", "修正文字错误")

    moved = db.move_suggestion(second["id"], "up")
    assert [item["id"] for item in moved] == [second["id"], first["id"]]

    db.delete_suggestion(second["id"])
    assert [item["id"] for item in db.suggestions()] == [first["id"]]
    with db.connect() as con:
        assert con.execute("SELECT enabled FROM suggestions WHERE id=?", (second["id"],)).fetchone()[0] == 0

    with pytest.raises(KeyError):
        db.update_suggestion(second["id"], "已删除", "不能更新")



def test_iopaint_asset_resolution_rejects_escape_symlink_and_missing_file(tmp_path):
    root = tmp_path / "images"
    target = root / "SKU-1" / "module.png"
    outside = tmp_path / "outside.png"
    write_image(target, "red")
    write_image(outside, "blue")

    assert resolve_editable_asset(root, "SKU-1/module.png") == target.resolve()
    with pytest.raises(FileNotFoundError):
        resolve_editable_asset(root, "../outside.png")

    link = root / "SKU-1" / "linked.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前环境不支持创建符号链接")
    with pytest.raises(ValueError, match="符号链接"):
        resolve_editable_asset(root, "SKU-1/linked.png")


def test_iopaint_backup_is_versioned_and_not_overwritten(tmp_path):
    target = tmp_path / "images" / "SKU-1" / "module.png"
    write_image(target, "red")
    asset = {"id": 12, "revision": 3, "sha256": "abcdef1234567890"}
    backup_root = tmp_path / "backups"

    first = backup_asset(target, backup_root, 7, asset)
    original = first.read_bytes()
    write_image(target, "blue")
    second = backup_asset(target, backup_root, 7, asset)

    assert first == second
    assert second.read_bytes() == original
    assert second.name == "revision-3-abcdef123456.png"


def test_product_exceptions_are_source_scoped_and_do_not_change_asset_review_status(tmp_path):
    root = tmp_path / "images"
    image = root / "SKU-1" / "module.png"
    write_image(image, "red")
    db = Database(tmp_path / "reviews.db")
    first_source = make_source(db, root)
    scan(root, db, first_source)
    asset = db.assets(first_source)[0]
    db.update(asset["id"], "approved", "", asset["revision"], first_source)

    second_source = db.add_source("other", str(tmp_path / "other"))
    db.set_product_exception(first_source, "SKU-1", True)
    assert db.product_exceptions(first_source) == {"SKU-1"}
    assert db.product_exceptions(second_source) == set()

    marked = apply_product_exceptions(
        reconcile(db.assets(first_source), None),
        db.product_exceptions(first_source),
    )
    assert marked[0]["inventory_status"] == "product_exception"
    assert marked[0]["product_exception"] is True
    assert db.assets(first_source)[0]["status"] == "approved"

    db.set_product_exception(first_source, "SKU-1", False)
    restored = apply_product_exceptions(
        reconcile(db.assets(first_source), None),
        db.product_exceptions(first_source),
    )
    assert restored[0]["product_exception"] is False
    assert restored[0]["inventory_status"] == "present"


def test_deleted_assets_are_hidden_from_lists_and_tasks(tmp_path):
    root = tmp_path / "images"
    image = root / "SKU-1" / "module.png"
    write_image(image, "red")
    db = Database(tmp_path / "reviews.db")
    source_id = make_source(db, root)

    scan(root, db, source_id)
    asset = db.assets(source_id)[0]
    db.update(asset["id"], "needs_revision", "fix it", asset["revision"], source_id)
    image.unlink()
    scan(root, db, source_id)

    assert db.assets(source_id) == []
    assert db.revision_tasks(source_id) == []
