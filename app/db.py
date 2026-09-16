from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from pathlib import Path
import uuid

STATUSES = {"unreviewed", "needs_revision", "modified_pending_review", "approved", "ignored", "delivered"}


class _ClosingConnection(sqlite3.Connection):
    """Commit/rollback *and* close when used in a ``with`` statement.

    ``sqlite3.Connection.__exit__`` deliberately leaves the connection open.
    Most repository methods use ``with db.connect()`` and a large source scan
    opens hundreds of short-lived connections, so leaving those descriptors to
    garbage collection can exhaust macOS's process limit.
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def now():
    return datetime.now(timezone.utc).isoformat()


def comments_hash(comments: str) -> str:
    """Use the same stable comment fingerprint as the AI task APIs."""
    return hashlib.sha256(str(comments or "").strip().encode("utf-8")).hexdigest()


def external_task_instructions(task: dict) -> list[str]:
    """Return human comments plus any frozen system size-repair requirement."""
    instructions = [line.strip() for line in str(task.get("comments") or "").splitlines() if line.strip()]
    try:
        dimension_repair = json.loads(task.get("dimension_repair_json") or "null")
    except (TypeError, ValueError, json.JSONDecodeError):
        dimension_repair = None
    if isinstance(dimension_repair, dict) and str(dimension_repair.get("instruction") or "").strip():
        instructions.insert(0, str(dimension_repair["instruction"]).strip())
    return instructions


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        con = sqlite3.connect(self.path, timeout=15, factory=_ClosingConnection)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def init(self):
        with self.connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS image_sources (
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, path TEXT UNIQUE NOT NULL,
              active INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_scanned_at TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS product_exceptions (
              source_id INTEGER NOT NULL, sku TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              PRIMARY KEY(source_id, sku), FOREIGN KEY(source_id) REFERENCES image_sources(id)
            );
            CREATE TABLE IF NOT EXISTS suggestions (
              id INTEGER PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
              sort_order INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS assets (
              id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, sku TEXT NOT NULL, module TEXT NOT NULL,
              relative_path TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL, sha256 TEXT NOT NULL,
              width INTEGER, height INTEGER, image_format TEXT, revision INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'unreviewed', comments TEXT NOT NULL DEFAULT '',
              reviewed_revision INTEGER NOT NULL DEFAULT -1, discovered_at TEXT NOT NULL,
              content_updated_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
              UNIQUE(source_id, relative_path), FOREIGN KEY(source_id) REFERENCES image_sources(id)
            );
            CREATE TABLE IF NOT EXISTS versions (
              id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, revision INTEGER NOT NULL,
              sha256 TEXT NOT NULL, comments TEXT NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(asset_id, revision)
            );
            CREATE TABLE IF NOT EXISTS aplus_alt_texts (
              asset_id INTEGER PRIMARY KEY, alt_text TEXT NOT NULL,
              source TEXT NOT NULL DEFAULT 'manual', review_status TEXT NOT NULL DEFAULT 'needs_confirmation',
              reviewed_revision INTEGER NOT NULL DEFAULT -1, updated_at TEXT NOT NULL,
              FOREIGN KEY(asset_id) REFERENCES assets(id)
            );
            CREATE TABLE IF NOT EXISTS aplus_deliveries (
              id INTEGER PRIMARY KEY, source_delivery_id TEXT UNIQUE NOT NULL, sku TEXT NOT NULL,
              profile_id TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
              fingerprint TEXT UNIQUE NOT NULL, asins_json TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL, UNIQUE(sku, version)
            );
            CREATE TABLE IF NOT EXISTS aplus_delivery_slots (
              id INTEGER PRIMARY KEY, delivery_id INTEGER NOT NULL, slot_key TEXT NOT NULL,
              sequence INTEGER NOT NULL, module TEXT NOT NULL, asset_id INTEGER NOT NULL,
              revision INTEGER NOT NULL, sha256 TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
              alt_text TEXT NOT NULL, FOREIGN KEY(delivery_id) REFERENCES aplus_deliveries(id),
              UNIQUE(delivery_id, sequence)
            );

            CREATE TABLE IF NOT EXISTS ai_revision_results (
              id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, source_id INTEGER NOT NULL, asset_id INTEGER NOT NULL,
              source_revision INTEGER NOT NULL, result_revision INTEGER, source_sha256 TEXT NOT NULL,
              result_sha256 TEXT NOT NULL DEFAULT '', result_status TEXT NOT NULL,
              summary TEXT NOT NULL DEFAULT '', changes_json TEXT NOT NULL DEFAULT '[]',
              uncertainties_json TEXT NOT NULL DEFAULT '[]', provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
              backup_path TEXT NOT NULL DEFAULT '', error_message TEXT NOT NULL DEFAULT '',
              instructions_hash TEXT NOT NULL DEFAULT '', instructions_json TEXT NOT NULL DEFAULT '[]',
              comments TEXT NOT NULL DEFAULT '', original_prompt TEXT NOT NULL DEFAULT '',
              original_copy_json TEXT NOT NULL DEFAULT '{}', application_status TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL, applied_at TEXT,
              FOREIGN KEY(source_id) REFERENCES image_sources(id), FOREIGN KEY(asset_id) REFERENCES assets(id)
            );
            CREATE TABLE IF NOT EXISTS external_ai_tasks (
              task_id TEXT PRIMARY KEY, source_id INTEGER NOT NULL, asset_id INTEGER NOT NULL,
              sku TEXT NOT NULL, module TEXT NOT NULL, relative_path TEXT NOT NULL,
              reference_path TEXT, reference_asset_id INTEGER, reference_revision INTEGER,
              reference_sha256 TEXT NOT NULL DEFAULT '', context_hash TEXT NOT NULL DEFAULT '',
              source_revision INTEGER NOT NULL, source_sha256 TEXT NOT NULL,
              comments TEXT NOT NULL DEFAULT '', instructions_hash TEXT NOT NULL,
              original_prompt TEXT, original_headline TEXT, original_body TEXT,
              product_info_json TEXT NOT NULL DEFAULT '{}', dimension_repair_json TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'open',
              result_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              FOREIGN KEY(source_id) REFERENCES image_sources(id), FOREIGN KEY(asset_id) REFERENCES assets(id)
            );
            -- A filesystem rename and a SQLite commit cannot be one OS-level
            -- transaction. This journal lets startup safely recover the narrow
            -- interval between those two operations.
            CREATE TABLE IF NOT EXISTS external_ai_applications (
              task_id TEXT PRIMARY KEY, source_id INTEGER NOT NULL, asset_id INTEGER NOT NULL,
              source_revision INTEGER NOT NULL, source_sha256 TEXT NOT NULL, candidate_sha256 TEXT NOT NULL,
              validation_json TEXT NOT NULL, audit_json TEXT NOT NULL, backup_path TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'prepared', error_message TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, applied_at TEXT,
              FOREIGN KEY(task_id) REFERENCES external_ai_tasks(task_id),
              FOREIGN KEY(source_id) REFERENCES image_sources(id), FOREIGN KEY(asset_id) REFERENCES assets(id)
            );
            """)
            # WAL lets the periodic scanner and request handlers coexist more
            # predictably; busy_timeout above remains the fallback for writers.
            con.execute("PRAGMA journal_mode=WAL")
            # Migrate the original single-source table created by the first version.
            columns = {r["name"] for r in con.execute("PRAGMA table_info(assets)")}
            if "source_id" not in columns:
                con.execute("ALTER TABLE assets RENAME TO assets_legacy")
                con.execute("""CREATE TABLE assets (
                  id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, sku TEXT NOT NULL, module TEXT NOT NULL,
                  relative_path TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL, sha256 TEXT NOT NULL,
                  width INTEGER, height INTEGER, image_format TEXT, revision INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'unreviewed',
                  comments TEXT NOT NULL DEFAULT '', reviewed_revision INTEGER NOT NULL DEFAULT -1,
                  discovered_at TEXT NOT NULL, content_updated_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                  missing INTEGER NOT NULL DEFAULT 0, mtime_ns INTEGER, ctime_ns INTEGER,
                  UNIQUE(source_id, relative_path)
                )""")
                # The default source is created during application startup, so keep
                # legacy rows until it can be associated with that source.
            columns = {r["name"] for r in con.execute("PRAGMA table_info(assets)")}
            if "missing" not in columns:
                con.execute("ALTER TABLE assets ADD COLUMN missing INTEGER NOT NULL DEFAULT 0")
            if "mtime_ns" not in columns:
                con.execute("ALTER TABLE assets ADD COLUMN mtime_ns INTEGER")
            if "ctime_ns" not in columns:
                con.execute("ALTER TABLE assets ADD COLUMN ctime_ns INTEGER")
            if "image_format" not in columns:
                con.execute("ALTER TABLE assets ADD COLUMN image_format TEXT")
            if "content_updated_at" not in columns:
                con.execute("ALTER TABLE assets ADD COLUMN content_updated_at TEXT NOT NULL DEFAULT ''")
            # Keep a stable content-change timestamp separate from updated_at:
            # review decisions also update updated_at, but delivery prioritization
            # must reflect the actual image revision time. Existing rows can be
            # reconstructed from the archived revision immediately before the
            # current one, falling back to their first discovery time.
            con.execute("""UPDATE assets
                           SET content_updated_at=COALESCE(
                             NULLIF(content_updated_at, ''),
                             (SELECT MAX(created_at) FROM versions
                              WHERE versions.asset_id=assets.id
                                AND versions.revision=assets.revision - 1),
                             discovered_at, updated_at, ?)
                           WHERE content_updated_at IS NULL OR content_updated_at=''""", (now(),))
            con.execute("CREATE INDEX IF NOT EXISTS idx_assets_source_order ON assets(source_id, sku, module, relative_path)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_assets_source_status_order ON assets(source_id, status, sku, module, relative_path)")

            for table, column, definition in (
                ("ai_revision_results", "instructions_hash", "TEXT NOT NULL DEFAULT ''"),
                ("ai_revision_results", "instructions_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("ai_revision_results", "comments", "TEXT NOT NULL DEFAULT ''"),
                ("ai_revision_results", "original_prompt", "TEXT NOT NULL DEFAULT ''"),
                ("ai_revision_results", "original_copy_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("ai_revision_results", "application_status", "TEXT NOT NULL DEFAULT ''"),
                ("external_ai_tasks", "reference_path", "TEXT"),
                ("external_ai_tasks", "reference_asset_id", "INTEGER"),
                ("external_ai_tasks", "reference_revision", "INTEGER"),
                ("external_ai_tasks", "reference_sha256", "TEXT NOT NULL DEFAULT ''"),
                ("external_ai_tasks", "context_hash", "TEXT NOT NULL DEFAULT ''"),
                ("external_ai_tasks", "original_prompt", "TEXT"),
                ("external_ai_tasks", "original_headline", "TEXT"),
                ("external_ai_tasks", "original_body", "TEXT"),
                ("external_ai_tasks", "product_info_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("external_ai_tasks", "dimension_repair_json", "TEXT NOT NULL DEFAULT ''"),
                ("external_ai_tasks", "status", "TEXT NOT NULL DEFAULT 'open'"),
                ("external_ai_tasks", "result_id", "INTEGER"),
            ):
                existing_columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
                if column not in existing_columns:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            con.execute("CREATE INDEX IF NOT EXISTS idx_ai_results_asset_order ON ai_revision_results(asset_id, created_at DESC)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_ai_results_task_order ON ai_revision_results(task_id, created_at DESC)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_external_ai_tasks_source_order ON external_ai_tasks(source_id, status, created_at, task_id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_external_ai_applications_state ON external_ai_applications(state, created_at, task_id)")
            # Old snapshots did not include their reference/product context.
            # Rebuild this index so a changed reference can produce a fresh,
            # independently auditable task even when target revision is unchanged.
            con.execute("DROP INDEX IF EXISTS idx_external_ai_tasks_snapshot")
            con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_external_ai_tasks_snapshot
                           ON external_ai_tasks(source_id, asset_id, source_revision, instructions_hash, context_hash)""")


    def migrate_legacy(self, source_id):
        with self.connect() as con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets_legacy'").fetchone():
                return
            con.execute("""INSERT OR IGNORE INTO assets(
              id, source_id, sku, module, relative_path, size, mtime, sha256,
              width, height, revision, status, comments, reviewed_revision,
              discovered_at, content_updated_at, updated_at
            ) SELECT legacy.id, ?, legacy.sku, legacy.module, legacy.relative_path, legacy.size, legacy.mtime, legacy.sha256,
              legacy.width, legacy.height, legacy.revision, legacy.status, legacy.comments, legacy.reviewed_revision,
              legacy.discovered_at,
              COALESCE(
                (SELECT MAX(versions.created_at) FROM versions
                 WHERE versions.asset_id=legacy.id AND versions.revision=legacy.revision - 1),
                legacy.discovered_at, legacy.updated_at, ?
              ),
              legacy.updated_at
              FROM assets_legacy AS legacy""", (source_id, now()))
            con.execute("DROP TABLE assets_legacy")

    def source(self, source_id):
        with self.connect() as con:
            row = con.execute("SELECT * FROM image_sources WHERE id=?", (source_id,)).fetchone()
            return dict(row) if row else None

    def sources(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM image_sources ORDER BY id")]

    def active_source(self):
        with self.connect() as con:
            row = con.execute("SELECT * FROM image_sources WHERE active=1 LIMIT 1").fetchone()
            return dict(row) if row else None

    def add_source(self, name, path):
        with self.connect() as con:
            con.execute("INSERT INTO image_sources(name,path,created_at) VALUES(?,?,?)", (name, path, now()))
            return con.execute("SELECT last_insert_rowid()").fetchone()[0]

    def activate_source(self, source_id):
        with self.connect() as con:
            if not con.execute("SELECT 1 FROM image_sources WHERE id=?", (source_id,)).fetchone(): raise KeyError(source_id)
            con.execute("UPDATE image_sources SET active=0")
            con.execute("UPDATE image_sources SET active=1 WHERE id=?", (source_id,))

    def mark_scanned(self, source_id, scanned_at=None):
        with self.connect() as con:
            con.execute("UPDATE image_sources SET last_scanned_at=? WHERE id=?", (scanned_at or now(), source_id))

    def product_exceptions(self, source_id):
        with self.connect() as con:
            return {row["sku"] for row in con.execute(
                "SELECT sku FROM product_exceptions WHERE source_id=?", (source_id,)
            )}

    def set_product_exception(self, source_id, sku, enabled):
        sku = sku.strip()
        if not sku:
            raise ValueError("SKU 不能为空")
        with self.connect() as con:
            if enabled:
                timestamp = now()
                con.execute(
                    """INSERT INTO product_exceptions(source_id,sku,created_at,updated_at)
                       VALUES(?,?,?,?)
                       ON CONFLICT(source_id,sku) DO UPDATE SET updated_at=excluded.updated_at""",
                    (source_id, sku, timestamp, timestamp),
                )
            else:
                con.execute("DELETE FROM product_exceptions WHERE source_id=? AND sku=?", (source_id, sku))
        return enabled

    def _asset_query(self, source_id, status=None, q=None):
        sql = " FROM assets WHERE source_id=? AND missing=0"; args = [source_id]
        if status and status != "all": sql += " AND status=?"; args.append(status)
        if q: sql += " AND (sku LIKE ? OR module LIKE ?)"; args += [f"%{q}%"] * 2
        return sql, args

    def asset(self, asset_id, source_id=None):
        sql = "SELECT * FROM assets WHERE id=?"
        args = [asset_id]
        if source_id is not None:
            sql += " AND source_id=?"
            args.append(source_id)
        with self.connect() as con:
            row = con.execute(sql, args).fetchone()
            return dict(row) if row else None

    def assets(self, source_id, status=None, q=None):
        sql, args = self._asset_query(source_id, status, q)
        with self.connect() as con:
            return [dict(x) for x in con.execute("SELECT *" + sql + " ORDER BY sku, module, relative_path", args)]

    def asset_page(self, source_id, status=None, q=None, limit=60, offset=0):
        sql, args = self._asset_query(source_id, status, q)
        with self.connect() as con:
            total = con.execute("SELECT COUNT(*)" + sql, args).fetchone()[0]
            rows = con.execute("SELECT *" + sql + " ORDER BY sku, module, relative_path LIMIT ? OFFSET ?", args + [limit, offset])
            return {"items": [dict(x) for x in rows], "total": total, "offset": offset, "limit": limit, "has_more": offset + limit < total}

    def update(self, asset_id, status, comments, expected_revision, source_id=None):
        if status not in STATUSES:
            raise ValueError("invalid status")
        with self.connect() as con:
            sql = "UPDATE assets SET status=?,comments=?,reviewed_revision=?,updated_at=? WHERE id=? AND revision=? AND missing=0"
            args = [status, comments, expected_revision, now(), asset_id, expected_revision]
            if source_id is not None:
                sql += " AND source_id=?"
                args.append(source_id)
            cursor = con.execute(sql, args)
            if cursor.rowcount:
                return
            row = con.execute("SELECT revision, missing FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not row:
                raise KeyError(asset_id)
            if row["missing"]:
                raise FileNotFoundError(asset_id)
            raise RuntimeError("revision conflict")

    def suggestions(self):
        with self.connect() as con:
            return [dict(x) for x in con.execute(
                "SELECT * FROM suggestions WHERE enabled=1 ORDER BY sort_order,id"
            )]

    def create_suggestion(self, title, content):
        with self.connect() as con:
            sort_order = con.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM suggestions WHERE enabled=1"
            ).fetchone()[0]
            cursor = con.execute(
                "INSERT INTO suggestions(title,content,sort_order) VALUES(?,?,?)",
                (title, content, sort_order),
            )
            row = con.execute("SELECT * FROM suggestions WHERE id=?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def update_suggestion(self, suggestion_id, title, content):
        with self.connect() as con:
            cursor = con.execute(
                "UPDATE suggestions SET title=?,content=? WHERE id=? AND enabled=1",
                (title, content, suggestion_id),
            )
            if not cursor.rowcount:
                raise KeyError(suggestion_id)
            return dict(con.execute("SELECT * FROM suggestions WHERE id=?", (suggestion_id,)).fetchone())

    def delete_suggestion(self, suggestion_id):
        with self.connect() as con:
            cursor = con.execute(
                "UPDATE suggestions SET enabled=0 WHERE id=? AND enabled=1",
                (suggestion_id,),
            )
            if not cursor.rowcount:
                raise KeyError(suggestion_id)

    def move_suggestion(self, suggestion_id, direction):
        with self.connect() as con:
            ids = [row["id"] for row in con.execute(
                "SELECT id FROM suggestions WHERE enabled=1 ORDER BY sort_order,id"
            )]
            if suggestion_id not in ids:
                raise KeyError(suggestion_id)
            index = ids.index(suggestion_id)
            target = index - 1 if direction == "up" else index + 1
            if 0 <= target < len(ids):
                ids[index], ids[target] = ids[target], ids[index]
            con.executemany(
                "UPDATE suggestions SET sort_order=? WHERE id=?",
                [(position, item_id) for position, item_id in enumerate(ids)],
            )
            return [dict(x) for x in con.execute(
                "SELECT * FROM suggestions WHERE enabled=1 ORDER BY sort_order,id"
            )]

    def alt_text(self, asset_id):
        with self.connect() as con:
            row = con.execute("SELECT * FROM aplus_alt_texts WHERE asset_id=?", (asset_id,)).fetchone()
            return dict(row) if row else None

    def alt_texts(self, asset_ids):
        if not asset_ids:
            return {}
        placeholders = ",".join("?" for _ in asset_ids)
        with self.connect() as con:
            rows = con.execute(
                f"SELECT * FROM aplus_alt_texts WHERE asset_id IN ({placeholders})", asset_ids
            ).fetchall()
        return {row["asset_id"]: dict(row) for row in rows}

    def upsert_alt_text(self, asset_id, alt_text, source, review_status, reviewed_revision):
        with self.connect() as con:
            con.execute(
                """INSERT INTO aplus_alt_texts(asset_id,alt_text,source,review_status,reviewed_revision,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(asset_id) DO UPDATE SET alt_text=excluded.alt_text, source=excluded.source,
                   review_status=excluded.review_status, reviewed_revision=excluded.reviewed_revision,
                   updated_at=excluded.updated_at""",
                (asset_id, alt_text, source, review_status, reviewed_revision, now()),
            )

    def next_delivery_version(self, sku):
        with self.connect() as con:
            return con.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM aplus_deliveries WHERE sku=?", (sku,)
            ).fetchone()[0]

    def delivery_by_fingerprint(self, fingerprint):
        with self.connect() as con:
            row = con.execute("SELECT * FROM aplus_deliveries WHERE fingerprint=?", (fingerprint,)).fetchone()
            return dict(row) if row else None

    def latest_synced_delivery_snapshots(self, source_id, profile_id=None):
        """Return the latest successful immutable delivery snapshot for each SKU.

        A delivery does not store source_id directly, so scope it through its
        frozen slot assets. The result deliberately contains only the most
        recent *synced* delivery: drafts and ready-to-sync records must not
        make a product look already delivered.
        """
        sql = """
            SELECT deliveries.id AS delivery_id, deliveries.sku, deliveries.profile_id,
                   deliveries.version, deliveries.created_at, slots.asset_id,
                   slots.sequence, slots.module, slots.revision, slots.sha256, slots.alt_text
              FROM aplus_deliveries AS deliveries
              JOIN aplus_delivery_slots AS slots ON slots.delivery_id=deliveries.id
              JOIN assets AS assets ON assets.id=slots.asset_id
             WHERE deliveries.status='synced' AND assets.source_id=?
        """
        args = [source_id]
        if profile_id:
            sql += " AND deliveries.profile_id=?"
            args.append(profile_id)
        sql += " ORDER BY deliveries.sku COLLATE NOCASE, deliveries.version DESC, slots.sequence ASC"
        with self.connect() as con:
            rows = [dict(row) for row in con.execute(sql, args)]

        snapshots = {}
        for row in rows:
            sku = row["sku"]
            snapshot = snapshots.get(sku)
            if snapshot is not None and snapshot["id"] != row["delivery_id"]:
                continue
            if snapshot is None:
                snapshot = {
                    "id": row["delivery_id"],
                    "sku": sku,
                    "profile_id": row["profile_id"],
                    "version": row["version"],
                    "created_at": row["created_at"],
                    "slots": {},
                }
                snapshots[sku] = snapshot
            snapshot["slots"][row["module"]] = {
                "asset_id": row["asset_id"],
                "sequence": row["sequence"],
                "revision": row["revision"],
                "sha256": row["sha256"],
                "alt_text": row["alt_text"],
            }
        return snapshots

    def create_delivery(self, source_delivery_id, sku, profile_id, fingerprint, asins, slots):
        version = self.next_delivery_version(sku)
        status = "ready_to_sync" if asins else "draft"
        with self.connect() as con:
            cursor = con.execute(
                """INSERT INTO aplus_deliveries(source_delivery_id,sku,profile_id,version,status,fingerprint,asins_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (source_delivery_id, sku, profile_id, version, status, fingerprint, json.dumps(asins), now()),
            )
            delivery_id = cursor.lastrowid
            con.executemany(
                """INSERT INTO aplus_delivery_slots(delivery_id,slot_key,sequence,module,asset_id,revision,sha256,width,height,alt_text)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                [
                    (delivery_id, slot["slot_key"], slot["sequence"], slot["module"], slot["asset_id"],
                     slot["revision"], slot["sha256"], slot["width"], slot["height"], slot["alt_text"])
                    for slot in slots
                ],
            )
            row = con.execute("SELECT * FROM aplus_deliveries WHERE id=?", (delivery_id,)).fetchone()
        return dict(row)

    def mark_delivery_assets_delivered(self, delivery_id):
        """Mark only assets that still exactly match the synced immutable delivery snapshot."""
        with self.connect() as con:
            cursor = con.execute(
                """UPDATE assets
                   SET status='delivered', reviewed_revision=revision, updated_at=?
                   WHERE id IN (
                     SELECT asset_id FROM aplus_delivery_slots WHERE delivery_id=?
                   )
                   AND EXISTS (
                     SELECT 1 FROM aplus_delivery_slots AS slots
                     WHERE slots.delivery_id=? AND slots.asset_id=assets.id
                       AND slots.revision=assets.revision AND slots.sha256=assets.sha256
                   )
                   AND missing=0""",
                (now(), delivery_id, delivery_id),
            )
            return cursor.rowcount

    def revision_tasks(self, source_id):
        rows = self.assets(source_id, "needs_revision")
        return [{"source_id": source_id, "sku": r["sku"], "module": r["module"], "image_path": r["relative_path"], "revision": r["revision"], "instructions": [x for x in r["comments"].split("\n") if x.strip()]} for r in rows]


    def create_or_get_external_task(
        self,
        source_id,
        asset,
        reference=None,
        instructions_hash_value=None,
        original_prompt=None,
        original_copy=None,
        product_info=None,
        context_hash_value=None,
        dimension_repair=None,
    ):
        """Persist a complete, immutable input snapshot for one target revision."""
        timestamp = now()
        original_copy = original_copy or {}
        comments = str(asset.get("comments") or "").strip()
        instruction_digest = instructions_hash_value or comments_hash(comments)
        if isinstance(reference, dict):
            reference_path = reference.get("relative_path") or None
            reference_asset_id = reference.get("id")
            reference_revision = reference.get("revision")
            reference_sha256 = str(reference.get("sha256") or "")
        else:
            reference_path = str(reference) if reference else None
            reference_asset_id = None
            reference_revision = None
            reference_sha256 = ""
        normalized_dimension_repair = dimension_repair if isinstance(dimension_repair, dict) else None
        context_payload = {
            "reference_path": reference_path,
            "reference_asset_id": reference_asset_id,
            "reference_revision": reference_revision,
            "reference_sha256": reference_sha256,
            "original_prompt": original_prompt or "",
            "original_headline": original_copy.get("headline") or "",
            "original_body": original_copy.get("body") or "",
            "product_info": product_info or {},
            "dimension_repair": normalized_dimension_repair,
        }
        context_digest = context_hash_value or hashlib.sha256(
            json.dumps(context_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        lookup = (source_id, asset["id"], asset["revision"], instruction_digest, context_digest)
        with self.connect() as con:
            existing = con.execute(
                """SELECT * FROM external_ai_tasks
                   WHERE source_id=? AND asset_id=? AND source_revision=?
                     AND instructions_hash=? AND context_hash=?
                   ORDER BY created_at DESC LIMIT 1""",
                lookup,
            ).fetchone()
            if existing:
                return dict(existing), False
            task_value = f"air-{uuid.uuid4().hex}"
            try:
                con.execute(
                    """INSERT INTO external_ai_tasks(
                       task_id,source_id,asset_id,sku,module,relative_path,
                       reference_path,reference_asset_id,reference_revision,reference_sha256,context_hash,
                       source_revision,source_sha256,comments,instructions_hash,
                       original_prompt,original_headline,original_body,product_info_json,dimension_repair_json,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        task_value,
                        source_id,
                        asset["id"],
                        asset["sku"],
                        asset["module"],
                        asset["relative_path"],
                        reference_path,
                        reference_asset_id,
                        reference_revision,
                        reference_sha256,
                        context_digest,
                        asset["revision"],
                        asset["sha256"],
                        comments,
                        instruction_digest,
                        original_prompt or None,
                        original_copy.get("headline") or None,
                        original_copy.get("body") or None,
                        json.dumps(product_info or {}, ensure_ascii=False, sort_keys=True),
                        json.dumps(normalized_dimension_repair, ensure_ascii=False, sort_keys=True) if normalized_dimension_repair else "",
                        "open",
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = con.execute(
                    """SELECT * FROM external_ai_tasks
                       WHERE source_id=? AND asset_id=? AND source_revision=?
                         AND instructions_hash=? AND context_hash=?
                       ORDER BY created_at DESC LIMIT 1""",
                    lookup,
                ).fetchone()
                if existing:
                    return dict(existing), False
                raise
            row = con.execute("SELECT * FROM external_ai_tasks WHERE task_id=?", (task_value,)).fetchone()
            return dict(row), True

    def external_ai_task(self, task_id, source_id=None):
        sql = "SELECT * FROM external_ai_tasks WHERE task_id=?"
        args = [task_id]
        if source_id is not None:
            sql += " AND source_id=?"
            args.append(source_id)
        with self.connect() as con:
            row = con.execute(sql, args).fetchone()
            return dict(row) if row else None

    def external_ai_tasks(self, source_id, statuses=None, limit=100):
        sql = "SELECT * FROM external_ai_tasks WHERE source_id=?"
        args = [source_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            args.extend(statuses)
        sql += " ORDER BY created_at,task_id LIMIT ?"
        args.append(limit)
        with self.connect() as con:
            return [dict(row) for row in con.execute(sql, args)]

    def external_ai_result_for_task(self, task_id, applied_only=False):
        sql = "SELECT * FROM ai_revision_results WHERE task_id=?"
        args = [task_id]
        if applied_only:
            sql += " AND application_status='applied'"
        sql += " ORDER BY id DESC LIMIT 1"
        with self.connect() as con:
            row = con.execute(sql, args).fetchone()
            return dict(row) if row else None

    def external_ai_application(self, task_id):
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM external_ai_applications WHERE task_id=?", (task_id,)
            ).fetchone()
            return dict(row) if row else None

    def pending_external_ai_applications(self):
        with self.connect() as con:
            return [dict(row) for row in con.execute(
                "SELECT * FROM external_ai_applications WHERE state='prepared' ORDER BY created_at,task_id"
            )]

    def prepare_external_application(
        self,
        task_id,
        source_id,
        asset_id,
        source_revision,
        source_sha256,
        candidate_sha256,
        validation,
        audit,
        backup_path,
    ):
        """Persist the intent before replacing the production image file."""
        timestamp = now()
        validation_json = json.dumps(validation, ensure_ascii=False, sort_keys=True)
        audit_json = json.dumps(audit, ensure_ascii=False, sort_keys=True)
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            task = con.execute("SELECT * FROM external_ai_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task or task["source_id"] != source_id or task["asset_id"] != asset_id:
                raise RuntimeError("AI 任务不存在或来源不匹配")
            if (
                task["source_revision"] != source_revision
                or task["source_sha256"] != source_sha256
            ):
                raise RuntimeError("AI 任务快照已过期")
            existing = con.execute(
                "SELECT * FROM external_ai_applications WHERE task_id=?", (task_id,)
            ).fetchone()
            if existing:
                if existing["state"] == "applied":
                    return {"application": dict(existing), "idempotent": True}
                if existing["state"] in {"prepared", "recovery_required"}:
                    raise RuntimeError("该 AI 任务存在未恢复的图片应用记录")
                con.execute(
                    """UPDATE external_ai_applications
                       SET source_id=?,asset_id=?,source_revision=?,source_sha256=?,candidate_sha256=?,
                           validation_json=?,audit_json=?,backup_path=?,state='prepared',error_message='',
                           updated_at=?,applied_at=NULL
                       WHERE task_id=?""",
                    (
                        source_id, asset_id, source_revision, source_sha256, candidate_sha256,
                        validation_json, audit_json, backup_path, timestamp, task_id,
                    ),
                )
            else:
                con.execute(
                    """INSERT INTO external_ai_applications(
                       task_id,source_id,asset_id,source_revision,source_sha256,candidate_sha256,
                       validation_json,audit_json,backup_path,state,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,'prepared',?,?)""",
                    (
                        task_id, source_id, asset_id, source_revision, source_sha256, candidate_sha256,
                        validation_json, audit_json, backup_path, timestamp, timestamp,
                    ),
                )
            row = con.execute(
                "SELECT * FROM external_ai_applications WHERE task_id=?", (task_id,)
            ).fetchone()
            return {"application": dict(row), "idempotent": False}

    def mark_external_application(self, task_id, state, error_message=""):
        if state not in {"rolled_back", "recovery_required", "applied"}:
            raise ValueError("invalid external application state")
        with self.connect() as con:
            cursor = con.execute(
                """UPDATE external_ai_applications
                   SET state=?,error_message=?,updated_at=?,applied_at=CASE WHEN ?='applied' THEN ? ELSE applied_at END
                   WHERE task_id=?""",
                (state, str(error_message or ""), now(), state, now(), task_id),
            )
            if not cursor.rowcount:
                raise KeyError(task_id)
            row = con.execute(
                "SELECT * FROM external_ai_applications WHERE task_id=?", (task_id,)
            ).fetchone()
            return dict(row)

    def _insert_ai_revision_result(self, con, result):
        cursor = con.execute(
            """INSERT INTO ai_revision_results(
               task_id,source_id,asset_id,source_revision,result_revision,source_sha256,result_sha256,
               result_status,summary,changes_json,uncertainties_json,provider,model,backup_path,error_message,
               instructions_hash,instructions_json,comments,original_prompt,original_copy_json,application_status,
               created_at,applied_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                result["task_id"], result["source_id"], result["asset_id"], result["source_revision"],
                result.get("result_revision"), result["source_sha256"], result.get("result_sha256", ""),
                result["result_status"], result.get("summary", ""), json.dumps(result.get("changes", []), ensure_ascii=False),
                json.dumps(result.get("uncertainties", []), ensure_ascii=False), result.get("provider", ""),
                result.get("model", ""), result.get("backup_path", ""), result.get("error_message", ""),
                result.get("instructions_hash", ""), json.dumps(result.get("instructions", []), ensure_ascii=False),
                result.get("comments", ""), result.get("original_prompt", ""),
                json.dumps(result.get("original_copy", {}), ensure_ascii=False), result.get("application_status", ""),
                now(), now() if result.get("result_revision") is not None else None,
            ),
        )
        return dict(con.execute("SELECT * FROM ai_revision_results WHERE id=?", (cursor.lastrowid,)).fetchone())

    def record_ai_revision_result(self, result):
        with self.connect() as con:
            return self._insert_ai_revision_result(con, result)

    def record_external_report(self, task_id, result, task_status="reported"):
        """Atomically append a report without ever replacing an applied result."""
        if task_status not in {"reported", "rejected"}:
            raise ValueError("invalid external task status")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            task_row = con.execute("SELECT * FROM external_ai_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task_row:
                raise KeyError(task_id)
            task = dict(task_row)
            if task["status"] == "applied" and task.get("result_id"):
                existing = con.execute("SELECT * FROM ai_revision_results WHERE id=?", (task["result_id"],)).fetchone()
                return {
                    "task": task,
                    "result": dict(existing) if existing else None,
                    "idempotent": True,
                    "applied": True,
                }
            recorded = self._insert_ai_revision_result(con, result)
            cursor = con.execute(
                """UPDATE external_ai_tasks SET status=?,result_id=?,updated_at=?
                   WHERE task_id=? AND status IN ('open','reported','rejected')""",
                (task_status, recorded["id"], now(), task_id),
            )
            if cursor.rowcount != 1:
                latest = con.execute("SELECT * FROM external_ai_tasks WHERE task_id=?", (task_id,)).fetchone()
                if latest and latest["status"] == "applied" and latest["result_id"]:
                    existing = con.execute("SELECT * FROM ai_revision_results WHERE id=?", (latest["result_id"],)).fetchone()
                    return {
                        "task": dict(latest),
                        "result": dict(existing) if existing else None,
                        "idempotent": True,
                        "applied": True,
                    }
                raise RuntimeError("AI 任务状态在记录报告时发生冲突")
            updated = con.execute("SELECT * FROM external_ai_tasks WHERE task_id=?", (task_id,)).fetchone()
            return {"task": dict(updated), "result": recorded, "idempotent": False, "applied": False}

    def mark_external_task(self, task_id, status, result_id=None):
        """Legacy helper retained for old callers; never rolls back an applied task."""
        if status not in {"open", "reported", "rejected", "applied"}:
            raise ValueError("invalid external task status")
        with self.connect() as con:
            cursor = con.execute(
                """UPDATE external_ai_tasks SET status=?,result_id=?,updated_at=?
                   WHERE task_id=? AND (status!='applied' OR ?='applied')""",
                (status, result_id, now(), task_id, status),
            )
            if not cursor.rowcount:
                raise RuntimeError("已应用的 AI 任务不可被回退")
            row = con.execute("SELECT * FROM external_ai_tasks WHERE task_id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def apply_external_revision(
        self,
        task_id,
        source_id,
        asset_id,
        expected_revision,
        expected_sha256,
        expected_instructions_hash,
        metadata,
        result,
        backup_path,
        application_candidate_sha256=None,
    ):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            application = con.execute(
                "SELECT * FROM external_ai_applications WHERE task_id=?", (task_id,)
            ).fetchone()
            if not application:
                raise RuntimeError("AI 图片应用日志不存在")
            if application["state"] == "applied":
                task_row = con.execute("SELECT * FROM external_ai_tasks WHERE task_id=?", (task_id,)).fetchone()
                existing = con.execute(
                    "SELECT * FROM ai_revision_results WHERE id=?", (task_row["result_id"],)
                ).fetchone() if task_row and task_row["result_id"] else None
                current = con.execute("SELECT * FROM assets WHERE id=? AND source_id=?", (asset_id, source_id)).fetchone()
                return {
                    "asset": dict(current) if current else None,
                    "result": dict(existing) if existing else None,
                    "idempotent": True,
                }
            if application["state"] != "prepared":
                raise RuntimeError("AI 图片应用日志当前不可完成")
            if (
                application["source_id"] != source_id
                or application["asset_id"] != asset_id
                or application["source_revision"] != expected_revision
                or application["source_sha256"] != expected_sha256
                or application["candidate_sha256"] != (application_candidate_sha256 or metadata["sha256"])
                or application["backup_path"] != backup_path
            ):
                raise RuntimeError("AI 图片应用日志与任务不匹配")
            task_row = con.execute("SELECT * FROM external_ai_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task_row or task_row["source_id"] != source_id or task_row["asset_id"] != asset_id:
                raise RuntimeError("AI 任务不存在或来源不匹配")
            task = dict(task_row)
            if (
                task["source_revision"] != expected_revision
                or task["source_sha256"] != expected_sha256
                or task["instructions_hash"] != expected_instructions_hash
            ):
                raise RuntimeError("AI 任务快照已过期")
            if task["status"] == "applied":
                existing = con.execute("SELECT * FROM ai_revision_results WHERE id=?", (task["result_id"],)).fetchone()
                current = con.execute("SELECT * FROM assets WHERE id=? AND source_id=?", (asset_id, source_id)).fetchone()
                candidate_sha256 = application_candidate_sha256 or metadata["sha256"]
                if not existing or existing["result_sha256"] != candidate_sha256:
                    raise RuntimeError("AI 任务已由另一份候选图应用")
                con.execute(
                    """UPDATE external_ai_applications
                       SET state='applied',error_message='',updated_at=?,applied_at=?
                       WHERE task_id=? AND state='prepared'""",
                    (now(), now(), task_id),
                )
                return {
                    "asset": dict(current) if current else None,
                    "result": dict(existing),
                    "idempotent": True,
                }
            if task["status"] not in {"open", "reported", "rejected"}:
                raise RuntimeError("AI 任务当前不可应用")
            if con.execute(
                "SELECT 1 FROM product_exceptions WHERE source_id=? AND sku=?",
                (source_id, task["sku"]),
            ).fetchone():
                raise RuntimeError("产品已标记为异常，不能应用 AI 结果")
            current_row = con.execute("SELECT * FROM assets WHERE id=? AND source_id=?", (asset_id, source_id)).fetchone()
            if not current_row or current_row["missing"]:
                raise RuntimeError("原图片已删除")
            current = dict(current_row)
            if current["revision"] != expected_revision or current["sha256"] != expected_sha256:
                raise RuntimeError("图片已被更新，任务已过期")
            try:
                dimension_repair = json.loads(task["dimension_repair_json"] or "null")
            except (TypeError, ValueError, json.JSONDecodeError):
                dimension_repair = None
            if current["status"] != "needs_revision":
                raise RuntimeError(f"当前图片状态不允许应用 AI 结果：{current['status']}")
            if comments_hash(current["comments"]) != task["instructions_hash"]:
                raise RuntimeError("人工修改意见已变化，任务已过期")
            con.execute(
                """INSERT OR IGNORE INTO versions(asset_id,revision,sha256,comments,created_at)
                   VALUES(?,?,?,?,?)""",
                (asset_id, current["revision"], current["sha256"], current["comments"], now()),
            )
            cursor = con.execute(
                """UPDATE assets SET size=?,mtime=?,mtime_ns=?,ctime_ns=?,sha256=?,width=?,height=?,
                   revision=?,status='modified_pending_review',reviewed_revision=-1,missing=0,
                   content_updated_at=?,updated_at=?
                   WHERE id=? AND source_id=? AND revision=? AND sha256=?
                     AND status='needs_revision'""",
                (
                    metadata["size"], metadata["mtime"], metadata["mtime_ns"], metadata["ctime_ns"], metadata["sha256"],
                    metadata["width"], metadata["height"], current["revision"] + 1, now(), now(), asset_id, source_id,
                    expected_revision, expected_sha256,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("图片版本在更新时发生冲突")
            updated_row = con.execute("SELECT * FROM assets WHERE id=? AND source_id=?", (asset_id, source_id)).fetchone()
            if not updated_row:
                raise RuntimeError("更新后的图片记录不存在")
            updated = dict(updated_row)
            audit = dict(result)
            audit.update({
                "source_id": source_id,
                "asset_id": asset_id,
                "source_revision": expected_revision,
                "result_revision": updated["revision"],
                "source_sha256": expected_sha256,
                "result_sha256": metadata["sha256"],
                "result_status": "completed",
                "backup_path": backup_path,
                "instructions_hash": task["instructions_hash"],
                "instructions": external_task_instructions(task),
                "comments": task["comments"],
                "original_prompt": task["original_prompt"] or "",
                "original_copy": {"headline": task["original_headline"], "body": task["original_body"]},
                "application_status": "applied",
            })
            recorded = self._insert_ai_revision_result(con, audit)
            transitioned = con.execute(
                """UPDATE external_ai_tasks SET status='applied',result_id=?,updated_at=?
                   WHERE task_id=? AND status IN ('open','reported','rejected')""",
                (recorded["id"], now(), task_id),
            )
            if transitioned.rowcount != 1:
                raise RuntimeError("AI 任务状态在应用时发生冲突")
            con.execute(
                """UPDATE external_ai_applications
                   SET state='applied',error_message='',updated_at=?,applied_at=?
                   WHERE task_id=? AND state='prepared'""",
                (now(), now(), task_id),
            )
            return {"asset": updated, "result": recorded, "idempotent": False}

    def ai_revision_results(self, asset_id=None, source_id=None, task_id=None, limit=100):
        sql = "SELECT * FROM ai_revision_results WHERE 1=1"
        args = []
        if source_id is not None:
            sql += " AND source_id=?"; args.append(source_id)
        if asset_id is not None:
            sql += " AND asset_id=?"; args.append(asset_id)
        if task_id is not None:
            sql += " AND task_id=?"; args.append(task_id)
        sql += " ORDER BY created_at DESC,id DESC LIMIT ?"; args.append(limit)
        with self.connect() as con:
            return [dict(row) for row in con.execute(sql, args)]
