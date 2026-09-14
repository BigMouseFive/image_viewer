from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app.locks as locks
import app.main as reviewer
from app.ai_integration.service import AIIntegrationError, apply_external_result, recover_external_application, validate_uploaded_image
from app.db import Database
from app.inventory import load_manifest, reconcile
from app.iopaint import backup_asset
from app.scanner import scan


def _noise_image(path: Path, seed: int, size=(970, 600)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # effect_noise is deterministic enough for an input seed and deliberately
    # produces a non-flat PNG substantially larger than the 10 KB lower bound.
    image = Image.effect_noise(size, 20 + seed).convert("RGB")
    image.save(path, format="PNG")


def _reference_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.effect_noise((300, 300), 40).convert("RGB").save(path, format="JPEG", quality=92)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup_api(
    monkeypatch,
    tmp_path: Path,
    *,
    include_reference: bool = True,
    target_size=(970, 600),
    manifest: bool = False,
    target_status: str | None = "needs_revision",
):
    root = tmp_path / "images"
    target = root / "SKU-1" / "SKU-1_A+L01.png"
    _noise_image(target, 1, target_size)
    if include_reference:
        _reference_image(root / "_refs" / "SKU-1.jpg")
    if manifest:
        (root / "_image-reviewer-manifest.json").write_text(
            json.dumps({
                "schema_version": 1,
                "items": [{
                    "sku": "SKU-1", "module": "A+L01", "path": "SKU-1/SKU-1_A+L01.png",
                    "width": 970, "height": 600,
                }],
            }),
            encoding="utf-8",
        )

    monkeypatch.setattr(locks, "LOCK_ROOT", tmp_path / "locks")
    db = Database(tmp_path / "reviews.db")
    source_id = db.add_source("test", str(root))
    db.activate_source(source_id)
    scan(root, db, source_id)
    target_asset = next(asset for asset in db.assets(source_id) if asset["relative_path"] == "SKU-1/SKU-1_A+L01.png")
    if target_status is not None:
        db.update(target_asset["id"], target_status, "修正产品颜色\n删除多余配件", target_asset["revision"], source_id)

    prompt_csv = tmp_path / "prompts.csv"
    prompt_csv.write_text(
        "SKU,A+L01_prompt,A+L01_headline,A+L01_body\n"
        "SKU-1,Original prompt,Original headline,Original body\n",
        encoding="utf-8",
    )
    product_csv = tmp_path / "products.csv"
    product_csv.write_text(
        "SKU,Amazon 标题,Amazon 五点描述,ASIN\n"
        "SKU-1,Product title,Product bullets,B000000001\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(reviewer, "db", db)
    monkeypatch.setattr(reviewer, "allowed_root", tmp_path.resolve())
    monkeypatch.setattr(reviewer, "prompt_source_csv", prompt_csv)
    monkeypatch.setattr(reviewer, "product_source_csv", product_csv)
    monkeypatch.setattr(reviewer, "_prompt_cache", {"path": None, "mtime_ns": None, "items": {}})
    monkeypatch.setattr(reviewer, "_product_info_cache", {"path": None, "mtime_ns": None, "items": {}})
    return db, source_id, root, target


def _task(client: TestClient) -> dict:
    response = client.get("/api/ai/revision-tasks?status=needs_revision")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    return items[0]


def _result_form(task: dict, **overrides) -> dict:
    form = {
        "task_id": task["task_id"],
        "source_revision": str(task["revision"]),
        "source_sha256": task["sha256"],
        "instructions_hash": task["instructions_hash"],
        "result_status": "completed",
        "summary": "Corrected product details.",
        "changes_json": json.dumps(["Matched product colour", "Removed extra accessory"]),
        "uncertainties_json": "[]",
        "provider": "cursor",
        "model": "auto",
    }
    form.update(overrides)
    return form


def test_reconcile_without_manifest_recognizes_refs_as_non_deliverable():
    items = [
        {"id": 1, "sku": "SKU-1", "module": "SKU-1_A+L01", "relative_path": "SKU-1/SKU-1_A+L01.png", "width": 970, "height": 600},
        {"id": 2, "sku": "_refs", "module": "SKU-1", "relative_path": "_refs/SKU-1.jpg", "width": 400, "height": 400},
    ]
    reconciled = reconcile(items, None)
    assert reconciled[0]["asset_role"] == "deliverable"
    assert reconciled[0]["inventory_status"] == "present"
    assert reconciled[1]["asset_role"] == "reference"
    assert reconciled[1]["inventory_status"] == "extra"
    assert reconciled[1]["reference_sku"] == "SKU-1"


def test_no_manifest_duplicate_module_is_not_deliverable():
    items = [
        {"id": 1, "sku": "SKU-1", "module": "first_A+L01", "relative_path": "SKU-1/first_A+L01.png", "width": 970, "height": 600},
        {"id": 2, "sku": "SKU-1", "module": "second_A+L01", "relative_path": "SKU-1/second_A+L01.png", "width": 970, "height": 600},
    ]
    reconciled = reconcile(items, None)
    assert all(item["asset_role"] == "deliverable" for item in reconciled)
    assert all(item["inventory_status"] == "duplicate_module" for item in reconciled)


def test_invalid_manifest_and_wrong_path_fail_closed(tmp_path):
    invalid_root = tmp_path / "invalid"
    invalid_root.mkdir()
    (invalid_root / "_image-reviewer-manifest.json").write_text("{not json", encoding="utf-8")
    invalid = load_manifest(invalid_root)
    invalid_result = reconcile([
        {"id": 1, "sku": "SKU-1", "module": "SKU-1_A+L01", "relative_path": "SKU-1/SKU-1_A+L01.png", "width": 970, "height": 600},
    ], invalid)
    assert invalid_result[0]["inventory_status"] == "invalid_manifest"
    assert invalid_result[0]["asset_role"] == "other"

    manifest = {
        "schema_version": 1,
        "items": [{
            "sku": "SKU-1", "module": "A+L01", "path": "SKU-1/SKU-1_A+L01.png", "width": 970, "height": 600,
        }],
    }
    wrong_path = reconcile([
        {"id": 1, "sku": "SKU-1", "module": "unexpected_A+L01", "relative_path": "SKU-1/unexpected_A+L01.png", "width": 970, "height": 600},
    ], manifest)
    assert {item["inventory_status"] for item in wrong_path} == {"wrong_path", "missing"}


def test_validate_uploaded_image_rejects_bad_candidates(tmp_path):
    source = tmp_path / "source.png"
    candidate = tmp_path / "candidate.png"
    _noise_image(source, 1)
    _noise_image(candidate, 2)
    valid = validate_uploaded_image(candidate, _sha256(source))
    assert valid["format"] == "PNG"
    assert (valid["width"], valid["height"]) == (970, 600)

    with pytest.raises(AIIntegrationError, match="内容相同"):
        validate_uploaded_image(source, _sha256(source))

    wrong_size = tmp_path / "wrong.png"
    Image.effect_noise((970, 300), 50).convert("RGB").save(wrong_size, format="PNG")
    with pytest.raises(AIIntegrationError, match="970×600"):
        validate_uploaded_image(wrong_size, "different")

    flat = tmp_path / "flat.png"
    # Metadata alone can make a pure PNG larger than 10 KB, so use a text chunk
    # to reach the server's size gate before exercising the variance gate.
    image = Image.new("RGB", (970, 600), "white")
    image.save(flat, format="PNG", pnginfo=__import__("PIL.PngImagePlugin", fromlist=["PngInfo"]).PngInfo())
    with flat.open("ab") as stream:
        stream.write(b"x" * 12_000)
    with pytest.raises(AIIntegrationError, match="接近纯色"):
        validate_uploaded_image(flat, "different")


def test_invalid_dimensions_require_explicit_queue_then_apply(monkeypatch, tmp_path):
    db, source_id, root, target = _setup_api(
        monkeypatch,
        tmp_path,
        target_size=(970, 300),
        manifest=True,
        target_status=None,
    )
    client = TestClient(reviewer.app, base_url="http://127.0.0.1:8700")
    initial = client.get("/api/ai/revision-tasks?status=needs_revision")
    assert initial.status_code == 200
    assert initial.json()["items"] == []

    queued = client.post("/api/dimension-repair-queue", json={"all_eligible": True})
    assert queued.status_code == 200, queued.text
    assert len(queued.json()["queued"]) == 1

    task = _task(client)
    assert task["asset_status"] == "needs_revision"
    assert task["task_kind"] == "dimension_repair"
    assert task["comments"] == ""
    assert task["dimension_repair"] is not None
    assert task["dimension_repair"]["current_dimensions"] == {"width": 970, "height": 300}
    assert task["dimension_repair"]["expected_dimensions"] == {"width": 970, "height": 600}
    assert task["expected_output"] == {"format": "PNG", "width": 970, "height": 600}
    assert "尺寸异常" in task["instructions"][0]

    candidate = tmp_path / "dimension-fixed.png"
    _noise_image(candidate, 21)
    response = client.post(
        "/api/ai/revision-results",
        data=_result_form(task, summary="Rebuilt the canvas at 970×600 without stretching the product."),
        files={"image": ("dimension-fixed.png", candidate.read_bytes(), "image/png")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["new_status"] == "modified_pending_review"
    assert _sha256(target) == _sha256(candidate)

    updated = db.asset(task["asset_id"], source_id)
    assert updated is not None
    assert (updated["width"], updated["height"]) == (970, 600)
    reconciled = reconcile(db.assets(source_id), load_manifest(root))
    repaired = next(item for item in reconciled if item.get("id") == task["asset_id"])
    assert repaired["inventory_status"] == "present"
    audit = db.external_ai_result_for_task(task["task_id"], applied_only=True)
    assert audit is not None
    assert "尺寸异常" in json.loads(audit["instructions_json"])[0]


def test_corrupt_png_is_not_offered_as_dimension_repair(monkeypatch, tmp_path):
    db, source_id, root, target = _setup_api(monkeypatch, tmp_path, manifest=True, target_status=None)
    target.write_bytes(b"not a PNG file")
    scan(root, db, source_id)
    client = TestClient(reviewer.app, base_url="http://127.0.0.1:8700")

    tasks = client.get("/api/ai/revision-tasks?status=needs_revision")
    assert tasks.status_code == 200
    assert tasks.json()["items"] == []

    queue = client.post("/api/dimension-repair-queue", json={"all_eligible": True})
    assert queue.status_code == 200
    assert queue.json()["queued"] == []
    assert len(queue.json()["skipped"]) == 1
    assert "PNG" in queue.json()["skipped"][0]["reason"]


def test_non_970_by_600_aplus_manifest_is_invalid(tmp_path):
    root = tmp_path / "images"
    root.mkdir()
    (root / "_image-reviewer-manifest.json").write_text(
        json.dumps({
            "schema_version": 1,
            "items": [{
                "sku": "SKU-1", "module": "A+L01", "path": "SKU-1/SKU-1_A+L01.png",
                "width": 1200, "height": 600,
            }],
        }),
        encoding="utf-8",
    )
    manifest = load_manifest(root)
    assert manifest is not None
    invalid = reconcile([
        {"id": 1, "sku": "SKU-1", "module": "SKU-1_A+L01", "relative_path": "SKU-1/SKU-1_A+L01.png", "width": 1200, "height": 600},
    ], manifest)
    assert invalid[0]["inventory_status"] == "invalid_manifest"
    assert invalid[0]["asset_role"] == "other"


def test_delivery_content_url_rejects_changed_snapshot(monkeypatch, tmp_path):
    db, source_id, root, target = _setup_api(monkeypatch, tmp_path)
    client = TestClient(reviewer.app, base_url="http://127.0.0.1:8700")
    asset = next(item for item in db.assets(source_id) if item["relative_path"] == "SKU-1/SKU-1_A+L01.png")
    snapshot_revision, snapshot_sha = asset["revision"], asset["sha256"]

    original = client.get(
        f"/api/integrations/aplus/assets/{asset['id']}/content?revision={snapshot_revision}&sha256={snapshot_sha}"
    )
    assert original.status_code == 200
    assert original.headers["x-image-reviewer-sha256"] == snapshot_sha

    _noise_image(target, 22)
    scan(root, db, source_id)
    stale = client.get(
        f"/api/integrations/aplus/assets/{asset['id']}/content?revision={snapshot_revision}&sha256={snapshot_sha}"
    )
    assert stale.status_code == 409


def test_external_task_context_report_and_completed_apply(monkeypatch, tmp_path):
    db, source_id, root, target = _setup_api(monkeypatch, tmp_path)
    client = TestClient(reviewer.app, base_url="http://127.0.0.1:8700")
    task = _task(client)

    assert task["task_id"].startswith("air-")
    assert task["sku"] == "SKU-1"
    assert task["module"] == "A+L01"
    assert task["comments"] == "修正产品颜色\n删除多余配件"
    assert task["instructions"] == ["修正产品颜色", "删除多余配件"]
    assert task["original_prompt"] == "Original prompt"
    assert task["original_copy"] == {"headline": "Original headline", "body": "Original body"}
    assert task["product_info"]["asin"] == "B000000001"
    assert task["main_image"]["url"]
    assert task["target_image"]["url"]

    target_response = client.get(task["target_image"]["url"])
    reference_response = client.get(task["main_image"]["url"])
    assert target_response.status_code == 200
    assert target_response.headers["x-image-reviewer-sha256"] == task["sha256"]
    assert reference_response.status_code == 200

    before_bytes = target.read_bytes()
    report = client.post(
        "/api/ai/revision-results",
        data=_result_form(
            task,
            result_status="needs_human_input",
            summary="Need a back-side reference before editing.",
            uncertainties_json=json.dumps(["Need back-side reference"]),
        ),
    )
    assert report.status_code == 200, report.text
    assert report.json()["applied"] is False
    assert target.read_bytes() == before_bytes
    persisted_task = db.external_ai_task(task["task_id"])
    assert persisted_task is not None
    assert persisted_task["status"] == "reported"

    candidate = tmp_path / "candidate.png"
    _noise_image(candidate, 2)
    response = client.post(
        "/api/ai/revision-results",
        data=_result_form(task),
        files={"image": ("candidate.png", candidate.read_bytes(), "image/png")},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["applied"] is True
    assert payload["idempotent"] is False
    assert payload["new_status"] == "modified_pending_review"
    assert _sha256(target) == _sha256(candidate)

    updated = db.asset(task["asset_id"], source_id)
    assert updated is not None
    assert updated["revision"] == task["revision"] + 1
    assert updated["status"] == "modified_pending_review"
    result = db.external_ai_result_for_task(task["task_id"], applied_only=True)
    assert result is not None
    assert result["result_sha256"] == _sha256(candidate)
    assert Path(result["backup_path"]).is_file()
    assert _sha256(Path(result["backup_path"])) == task["sha256"]

    retry = client.post(
        "/api/ai/revision-results",
        data=_result_form(task),
        files={"image": ("candidate.png", candidate.read_bytes(), "image/png")},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["idempotent"] is True
    retried_asset = db.asset(task["asset_id"], source_id)
    assert retried_asset is not None
    assert retried_asset["revision"] == updated["revision"]

    different = tmp_path / "different.png"
    _noise_image(different, 3)
    conflict = client.post(
        "/api/ai/revision-results",
        data=_result_form(task),
        files={"image": ("different.png", different.read_bytes(), "image/png")},
    )
    assert conflict.status_code == 409
    assert _sha256(target) == _sha256(candidate)


def test_external_task_stale_source_is_not_applied(monkeypatch, tmp_path):
    db, source_id, root, target = _setup_api(monkeypatch, tmp_path)
    client = TestClient(reviewer.app, base_url="http://127.0.0.1:8700")
    task = _task(client)

    _noise_image(target, 5)
    scan(root, db, source_id)
    changed_sha = _sha256(target)
    candidate = tmp_path / "candidate.png"
    _noise_image(candidate, 6)
    response = client.post(
        "/api/ai/revision-results",
        data=_result_form(task),
        files={"image": ("candidate.png", candidate.read_bytes(), "image/png")},
    )
    assert response.status_code == 409
    assert _sha256(target) == changed_sha
    stale_asset = db.asset(task["asset_id"], source_id)
    assert stale_asset is not None
    assert stale_asset["status"] == "modified_pending_review"


def test_database_apply_failure_restores_source_and_rolls_back_journal(monkeypatch, tmp_path):
    db, source_id, root, target = _setup_api(monkeypatch, tmp_path)
    client = TestClient(reviewer.app, base_url="http://127.0.0.1:8700")
    task = _task(client)
    source = db.source(source_id)
    asset = db.asset(task["asset_id"], source_id)
    assert source is not None
    assert asset is not None
    source_bytes = target.read_bytes()

    candidate = tmp_path / "candidate.png"
    _noise_image(candidate, 9)

    def fail_apply(*args, **kwargs):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(db, "apply_external_revision", fail_apply)
    with pytest.raises(AIIntegrationError, match="数据库更新失败"):
        apply_external_result(
            db,
            source,
            root,
            task,
            asset,
            candidate,
            {
                "result_status": "completed",
                "summary": "Will fail safely.",
                "changes": ["Test candidate"],
                "uncertainties": [],
                "provider": "test",
                "model": "test-model",
            },
        )

    assert target.read_bytes() == source_bytes
    current = db.asset(asset["id"], source_id)
    journal = db.external_ai_application(task["task_id"])
    assert current is not None
    assert journal is not None
    assert current["revision"] == task["revision"]
    assert current["status"] == "needs_revision"
    assert journal["state"] == "rolled_back"


def test_recovery_marks_unreplaced_target_rolled_back(monkeypatch, tmp_path):
    db, source_id, root, target = _setup_api(monkeypatch, tmp_path)
    client = TestClient(reviewer.app, base_url="http://127.0.0.1:8700")
    task = _task(client)
    source = db.source(source_id)
    asset = db.asset(task["asset_id"], source_id)
    assert source is not None
    assert asset is not None

    candidate = tmp_path / "candidate.png"
    _noise_image(candidate, 10)
    validation = validate_uploaded_image(candidate, task["sha256"])
    backup = backup_asset(target, tmp_path / "backups", source_id, asset)
    db.prepare_external_application(
        task["task_id"], source_id, asset["id"], task["revision"], task["sha256"], validation["sha256"],
        validation,
        {
            "task_id": task["task_id"], "result_status": "completed", "summary": "Interrupted before replace.",
            "changes": [], "uncertainties": [], "provider": "test", "model": "test-model",
        },
        str(backup),
    )

    application = db.external_ai_application(task["task_id"])
    assert application is not None
    recovered = recover_external_application(db, source, root, application)
    assert recovered["state"] == "rolled_back"
    assert _sha256(target) == task["sha256"]
    unchanged = db.asset(asset["id"], source_id)
    assert unchanged is not None
    assert unchanged["revision"] == task["revision"]
    assert unchanged["status"] == "needs_revision"


def test_recovery_finishes_interrupted_external_application(monkeypatch, tmp_path):
    db, source_id, root, target = _setup_api(monkeypatch, tmp_path)
    client = TestClient(reviewer.app, base_url="http://127.0.0.1:8700")
    task = _task(client)
    source = db.source(source_id)
    asset = db.asset(task["asset_id"], source_id)
    assert source is not None
    assert asset is not None

    candidate = tmp_path / "candidate.png"
    _noise_image(candidate, 11)
    validation = validate_uploaded_image(candidate, task["sha256"])
    backup = backup_asset(target, tmp_path / "backups", source_id, asset)
    audit = {
        "task_id": task["task_id"],
        "result_status": "completed",
        "summary": "Recovered interrupted image application.",
        "changes": ["Recovered candidate"],
        "uncertainties": [],
        "provider": "test",
        "model": "test-model",
    }
    prepared = db.prepare_external_application(
        task["task_id"],
        source_id,
        asset["id"],
        task["revision"],
        task["sha256"],
        validation["sha256"],
        validation,
        audit,
        str(backup),
    )
    assert prepared["idempotent"] is False

    # Simulate a process termination after the atomic target replacement and
    # before Database.apply_external_revision() commits its transaction.
    target.write_bytes(candidate.read_bytes())
    application = db.external_ai_application(task["task_id"])
    assert application is not None
    recovered = recover_external_application(db, source, root, application)
    assert recovered["state"] == "applied"
    assert _sha256(target) == validation["sha256"]
    updated = db.asset(asset["id"], source_id)
    assert updated is not None
    assert updated["revision"] == task["revision"] + 1
    assert updated["status"] == "modified_pending_review"
    recovered_task = db.external_ai_task(task["task_id"])
    recovered_application = db.external_ai_application(task["task_id"])
    assert recovered_task is not None
    assert recovered_application is not None
    assert recovered_task["status"] == "applied"
    assert recovered_application["state"] == "applied"


def test_missing_reference_can_report_but_cannot_apply(monkeypatch, tmp_path):
    db, source_id, root, target = _setup_api(monkeypatch, tmp_path, include_reference=False)
    client = TestClient(reviewer.app, base_url="http://127.0.0.1:8700")
    task = _task(client)
    assert task["main_image"]["url"] is None

    report = client.post(
        "/api/ai/revision-results",
        data=_result_form(
            task,
            result_status="needs_human_input",
            summary="A product reference image is required.",
            uncertainties_json=json.dumps(["Need product main image"]),
        ),
    )
    assert report.status_code == 200, report.text
    assert report.json()["applied"] is False

    candidate = tmp_path / "candidate.png"
    _noise_image(candidate, 7)
    response = client.post(
        "/api/ai/revision-results",
        data=_result_form(task),
        files={"image": ("candidate.png", candidate.read_bytes(), "image/png")},
    )
    assert response.status_code == 422
    assert _sha256(target) == task["sha256"]
