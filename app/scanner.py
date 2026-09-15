from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from PIL import Image

from .db import now
from .locks import asset_lock

EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
_SCAN_LOCKS: dict[int, threading.Lock] = {}
_SCAN_LOCKS_GUARD = threading.Lock()


def _source_lock(source_id: int) -> threading.Lock:
    with _SCAN_LOCKS_GUARD:
        return _SCAN_LOCKS.setdefault(source_id, threading.Lock())


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _image_metadata(path: Path) -> tuple[int | None, int | None, str | None]:
    try:
        with Image.open(path) as image:
            image_format = image.format
            width, height = image.size
            image.load()
            return width, height, image_format
    except (OSError, ValueError):
        return None, None, None


def _is_within(root: Path, path: Path) -> bool:
    return path != root and root in path.parents


def _safe_path(root: Path, relative_path: str) -> Path | None:
    unresolved = root / relative_path
    if unresolved.is_symlink():
        return None
    try:
        resolved = unresolved.resolve(strict=True)
    except OSError:
        return None
    if not _is_within(root, resolved) or resolved.is_symlink() or not resolved.is_file():
        return None
    if resolved.suffix.lower() not in EXTENSIONS:
        return None
    return resolved


def _same_file_identity(before, after) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
    )


def _metadata(path: Path) -> dict:
    """Read a stable file snapshot, retrying once if another writer swaps it."""
    last_error: OSError | None = None
    for _ in range(2):
        try:
            before = path.stat()
            digest = _digest(path)
            width, height, image_format = _image_metadata(path)
            after = path.stat()
        except OSError as error:
            last_error = error
            continue
        if _same_file_identity(before, after):
            return {
                "size": after.st_size,
                "mtime": after.st_mtime,
                "mtime_ns": after.st_mtime_ns,
                "ctime_ns": after.st_ctime_ns,
                "sha256": digest,
                "width": width,
                "height": height,
                "image_format": image_format,
            }
    raise last_error or OSError("图片在读取期间发生变化")


def _asset_labels(relative_path: str, path: Path) -> tuple[str, str]:
    parts = Path(relative_path).parts
    sku = parts[0] if len(parts) > 1 else "未分类"
    return sku, path.stem


def _changed_status(previous: str) -> str:
    return "modified_pending_review" if previous in {"needs_revision", "approved", "ignored", "delivered"} else previous


def _mark_missing(db, source_id: int, asset_id: int, scan_time: str) -> None:
    with db.connect() as con:
        con.execute(
            "UPDATE assets SET missing=1,updated_at=? WHERE id=? AND source_id=?",
            (scan_time, asset_id, source_id),
        )


def _refresh_existing(root: Path, db, source_id: int, asset_id: int, scan_time: str, *, force_digest: bool = False):
    """Synchronize one existing DB asset while holding the shared asset lock."""
    with asset_lock(source_id, asset_id):
        with db.connect() as con:
            current_row = con.execute(
                "SELECT * FROM assets WHERE id=? AND source_id=?", (asset_id, source_id)
            ).fetchone()
        if current_row is None:
            return None
        current = dict(current_row)
        target = _safe_path(root, current["relative_path"])
        if target is None:
            _mark_missing(db, source_id, asset_id, scan_time)
            return None
        # Periodic scans run frequently. Once an asset lock is held, matching
        # high-resolution file metadata means no cooperative writer can have
        # replaced the file without also updating this DB row, so avoid a full
        # SHA-256 pass on every unchanged image.
        stat = target.stat()
        if (
            not force_digest
            and not current.get("missing")
            and bool(current.get("image_format"))
            and current.get("size") == stat.st_size
            and current.get("mtime_ns") == stat.st_mtime_ns
            and current.get("ctime_ns") == stat.st_ctime_ns
        ):
            return current

        metadata = _metadata(target)
        sku, module = _asset_labels(current["relative_path"], target)
        with db.connect() as con:
            latest_row = con.execute(
                "SELECT * FROM assets WHERE id=? AND source_id=?", (asset_id, source_id)
            ).fetchone()
            if latest_row is None:
                return None
            latest = dict(latest_row)
            if latest["sha256"] != metadata["sha256"]:
                revision = latest["revision"] + 1
                status = _changed_status(latest["status"])
                con.execute(
                    """INSERT OR IGNORE INTO versions(asset_id,revision,sha256,comments,created_at)
                       VALUES(?,?,?,?,?)""",
                    (latest["id"], latest["revision"], latest["sha256"], latest["comments"], scan_time),
                )
                cursor = con.execute(
                    """UPDATE assets SET sku=?,module=?,size=?,mtime=?,mtime_ns=?,ctime_ns=?,
                       sha256=?,width=?,height=?,image_format=?,revision=?,status=?,reviewed_revision=-1,
                       missing=0,content_updated_at=?,updated_at=?
                       WHERE id=? AND source_id=? AND revision=? AND sha256=?""",
                    (
                        sku,
                        module,
                        metadata["size"],
                        metadata["mtime"],
                        metadata["mtime_ns"],
                        metadata["ctime_ns"],
                        metadata["sha256"],
                        metadata["width"],
                        metadata["height"],
                        metadata["image_format"],
                        revision,
                        status,
                        scan_time,
                        scan_time,
                        latest["id"],
                        source_id,
                        latest["revision"],
                        latest["sha256"],
                    ),
                )
                if not cursor.rowcount:
                    raise RuntimeError("图片版本在扫描更新时发生冲突")
            else:
                con.execute(
                    """UPDATE assets SET sku=?,module=?,size=?,mtime=?,mtime_ns=?,ctime_ns=?,
                       width=?,height=?,image_format=?,missing=0,updated_at=? WHERE id=? AND source_id=?""",
                    (
                        sku,
                        module,
                        metadata["size"],
                        metadata["mtime"],
                        metadata["mtime_ns"],
                        metadata["ctime_ns"],
                        metadata["width"],
                        metadata["height"],
                        metadata["image_format"],
                        scan_time,
                        latest["id"],
                        source_id,
                    ),
                )
            row = con.execute("SELECT * FROM assets WHERE id=? AND source_id=?", (asset_id, source_id)).fetchone()
            return dict(row) if row else None


def refresh_asset(root: Path, db, source_id: int, asset_id: int):
    root = root.resolve()
    with db.connect() as con:
        current = con.execute(
            "SELECT id FROM assets WHERE id=? AND source_id=?", (asset_id, source_id)
        ).fetchone()
    if current is None:
        raise KeyError(asset_id)
    # _refresh_existing marks a genuinely absent/unreadable path missing. A
    # separate OSError here means the file changed during an otherwise valid
    # read; callers should retry instead of hiding a still-existing asset.
    return _refresh_existing(root, db, source_id, asset_id, now(), force_digest=True)


def _insert_new_asset(db, source_id: int, root: Path, relative_path: str, scan_time: str):
    target = _safe_path(root, relative_path)
    if target is None:
        return None
    metadata = _metadata(target)
    sku, module = _asset_labels(relative_path, target)
    with db.connect() as con:
        current = con.execute(
            "SELECT id FROM assets WHERE source_id=? AND relative_path=?", (source_id, relative_path)
        ).fetchone()
        if current:
            return int(current["id"])
        cursor = con.execute(
            """INSERT INTO assets(
              source_id,sku,module,relative_path,size,mtime,mtime_ns,ctime_ns,
              sha256,width,height,image_format,discovered_at,content_updated_at,updated_at,missing
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (
                source_id,
                sku,
                module,
                relative_path,
                metadata["size"],
                metadata["mtime"],
                metadata["mtime_ns"],
                metadata["ctime_ns"],
                metadata["sha256"],
                metadata["width"],
                metadata["height"],
                metadata["image_format"],
                scan_time,
                scan_time,
                scan_time,
            ),
        )
        return int(cursor.lastrowid)


def _is_file_still_present(root: Path, relative_path: str) -> bool:
    return _safe_path(root, relative_path) is not None


def scan(root: Path, db, source_id: int):
    root = root.resolve()
    if not root.is_dir():
        return 0

    with _source_lock(source_id):
        scan_time = now()
        with db.connect() as con:
            existing = {
                row["relative_path"]: dict(row)
                for row in con.execute("SELECT * FROM assets WHERE source_id=?", (source_id,))
            }
        seen: set[str] = set()
        count = 0
        try:
            paths = root.rglob("*")
            for path in paths:
                try:
                    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in EXTENSIONS:
                        continue
                    resolved = path.resolve(strict=True)
                    if not _is_within(root, resolved):
                        continue
                    relative_path = resolved.relative_to(root).as_posix()
                    seen.add(relative_path)
                    existing_row = existing.get(relative_path)
                    if existing_row is None:
                        _insert_new_asset(db, source_id, root, relative_path, scan_time)
                    else:
                        # Keep ordinary periodic scans cheap. The source snapshot
                        # plus high-resolution file metadata is sufficient to
                        # skip unchanged files; changed/reappeared files still go
                        # through _refresh_existing under the shared asset lock.
                        stat = resolved.stat()
                        metadata_unchanged = (
                            not existing_row["missing"]
                            and bool(existing_row.get("image_format"))
                            and existing_row["size"] == stat.st_size
                            and existing_row["mtime_ns"] == stat.st_mtime_ns
                            and existing_row["ctime_ns"] == stat.st_ctime_ns
                        )
                        if not metadata_unchanged:
                            _refresh_existing(root, db, source_id, int(existing_row["id"]), scan_time)
                    count += 1
                except (OSError, RuntimeError):
                    # A file that is actively being replaced is retried by the
                    # next periodic scan; do not poison other assets' metadata.
                    continue
        except OSError:
            return count

        # Do not mark an item missing solely because rglob raced a writer. Check
        # the concrete path under that asset's shared lock first.
        for relative_path, existing_row in existing.items():
            if existing_row["missing"] or relative_path in seen:
                continue
            asset_id = int(existing_row["id"])
            with asset_lock(source_id, asset_id):
                with db.connect() as con:
                    latest = con.execute(
                        "SELECT relative_path,missing FROM assets WHERE id=? AND source_id=?",
                        (asset_id, source_id),
                    ).fetchone()
                if latest and not latest["missing"] and not _is_file_still_present(root, latest["relative_path"]):
                    _mark_missing(db, source_id, asset_id, scan_time)
        db.mark_scanned(source_id, scan_time)
        return count
