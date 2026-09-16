# image-reviewer

A local Amazon A+ image review workspace for browsing assets, recording human review decisions, validating five-image deliveries, and safely coordinating **external AI image revisions**.

The reviewer indexes source images and stores review state, comments, version history, delivery snapshots, AI task snapshots, audit records, and backups under this project. An external AI must **not** overwrite an A+ image directly: it retrieves a task through the API and submits a candidate PNG back to the reviewer.

## Start

### macOS login startup

```bash
cd /Users/wenwendemac/project/amazon/image-reviewer
chmod +x install-macos.sh
./install-macos.sh
```

The installer creates `.venv`, installs dependencies, registers the Web LaunchAgent, and starts it. By default the reviewer listens on all local network interfaces, so it is available from this machine and trusted LAN devices:

```text
http://127.0.0.1:8700/
http://<本机局域网-IP>:8700/
```

The installer prints the detected LAN URL after startup. This is a trusted-private-network default: the reviewer currently has no login/authentication layer, including for external-AI result submission. Do **not** expose port `8700` to the public internet or use port forwarding. To return to local-only access, set `server.host: 127.0.0.1` and restart the service.

Useful commands:

```bash
./install-macos.sh status
./install-macos.sh restart
./install-macos.sh uninstall
```

### Manual start

```bash
cd /Users/wenwendemac/project/amazon/image-reviewer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py
```

After a code or configuration change, restart the LaunchAgent:

```bash
./install-macos.sh restart
```

## Configuration

`config.yaml` paths are relative to the `image-reviewer` project unless absolute:

```yaml
server:
  host: 0.0.0.0
  port: 8700
  # Optional stable LAN URL used in generated external-AI task URLs.
  # public_url: http://192.168.1.20:8700
images:
  allowed_root_dir: ..
  default_source:
    name: UAE A+ 图片
    path: ../ai-relay/outputs/aplus_images_uae
  scan_interval_seconds: 5
product_info:
  csv: ../ai-relay/products_amazon_info_202609021610.csv
aplus_prompts:
  csv: ../ai-relay/outputs/aplus_prompts_20260903.csv
```

- `server.host`: default `0.0.0.0`, which listens on all local interfaces and supports trusted LAN access. The external AI write API has no token, so never expose this port to the public internet; set it to `127.0.0.1` to make the service local-only again.
- `server.public_url`: optional stable LAN URL used in generated external-AI task/download URLs, for example `http://192.168.1.20:8700`. When omitted, local browsing still works through the machine's LAN IP; external tooling should explicitly set `IMAGE_REVIEWER_URL`.
- `images.allowed_root_dir`: upper bound for directories selectable through the UI. Set the smallest practical scope.
- `images.default_source`: initial A+ image directory when the database has no source.
- `images.scan_interval_seconds`: background scan interval.
- `product_info.csv`: supplies product title, bullets, and ASIN context.
- `aplus_prompts.csv`: supplies the original A+L01–A+L05 prompt/headline/body context for external revision tasks.

### External AI revision only

The retired in-app Cursor ACP worker, its background LaunchAgent, and its private job API have been removed. The supported image-revision workflow is the external API plus the project Skill documented below. Existing review data and external-AI task/result audit records are preserved; legacy Cursor job history remains untouched in existing local SQLite databases but is no longer executed or exposed by the application.

## UI structure and refresh persistence

The desktop UI uses a left sidebar with four views:

1. **概览与目录** — select/manage source directories, rescan, and inspect source path, manifest/reference information, totals, SKU count, scan time, review counts, and inventory counts.
2. **图片评审** — main review workspace with multi-select review/inventory filters, search, lazy loading, image comparison, comments, Alt Text, refresh, and IOPaint.
3. **修改复核** — fixed view of `需修改` and `已修改` images for human follow-up.
4. **交付管理** — pending `可交付` and current `已交付` views, with delivery-change focus filters.

`可交付` is intentionally a **pending delivery** list rather than a list of every technically valid historical SKU: it includes only first deliveries, images changed since the most recent synced delivery, or changed Alt Text. Unchanged historical delivery snapshots remain under `已交付`, so they do not bury new work.

Within `可交付`, use **最近图片调整** as a locating shortcut for every SKU whose image bytes/revision differ from its most recent synced snapshot. It also includes products whose changed image is still `已修改`/待人工复核, so recent work is not hidden; such groups are visibly marked **待复核，暂不可交付** and their delivery button is disabled until the five-image gate passes. Groups are sorted by most recent image-content change and display the affected `A+Lxx` module(s) plus the prior delivery version. **Alt Text 已调整** and **首次交付** are available as separate focus filters.

The browser persists the current view, review-status selection, inventory-status selection, delivery subview/focus filter, search text, and scroll position in `localStorage`. Reloading restores the same view and position after content loads.

## Review and inventory status

### Review statuses

| Status | Meaning |
| --- | --- |
| `unreviewed` / 未处理 | No explicit review decision; it does not enter AI revision tasks. A valid size exception must first be explicitly added to the 尺寸修复队列. |
| `needs_revision` / 需修改 | Human has requested revision and supplied comments. This is the only external AI task input status. |
| `modified_pending_review` / 已修改 | A source image changed; human review is required. It never automatically re-enters the AI queue. |
| `approved` / 已确认 | Current revision is confirmed. |
| `ignored` / 忽略 | Current revision is explicitly accepted without further work. |
| `delivered` / 已交付 | Current immutable revision was synced to A+ Tool. |

When a source image changes, its `revision` increments and prior `needs_revision`, `approved`, `ignored`, or `delivered` content becomes `modified_pending_review`. Comments are retained for human review.

### Inventory status and manifest behavior

The optional `_image-reviewer-manifest.json` declares the exact expected file path, module, and dimensions for A+ delivery assets. Create it with:

```bash
.venv/bin/python tools/build_aplus_manifest.py \
  --prompt-csv ../ai-relay/outputs/aplus_prompts_20260903.csv \
  --output-dir ../ai-relay/outputs/aplus_images_uae \
  --generation-log ../ai-relay/outputs/aplus_images_uae/generation_log.csv
```

Supported inventory states include:

- `present`: exact manifest path and dimensions are valid;
- `missing` / `blocked`: an expected slot has no usable generated file;
- `invalid_dimensions`: file dimensions do not match the manifest. A real PNG whose expected manifest canvas is exactly `970×600` may be explicitly added to the AI size-repair queue; all other dimension errors remain manual-only;
- `extra`: not declared by the manifest;
- `wrong_path`: same SKU/module exists at a path other than the declared path;
- `duplicate_module`: multiple actual files map to one SKU/module;
- `invalid_manifest`: manifest exists but is malformed, unsafe, or internally inconsistent;
- `product_exception`: the SKU was manually marked as exceptional.

A malformed manifest fails closed: files are not treated as normal delivery assets. With no manifest, actual images retain legacy `present` behavior, except duplicate SKU/module files are marked `duplicate_module` so the delivery system never chooses one nondeterministically.

`_refs/{SKU}.*` is automatically recognized as a product-main reference image, never counts as a delivery module, and is used for image comparison/external task context.

### Product exception

Each SKU has a direct `标记产品异常` / `解除产品异常` control next to the delivery button. It switches immediately without a confirmation dialog.

API equivalent:

```bash
curl -X PUT http://127.0.0.1:8700/api/products/AC0002/exception \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true}'
```

A product exception makes deliverable images read-only in inventory terms and blocks delivery creation/batch sync. It does not erase historical review status.

## External AI revision workflow

The complete external-AI contract and operating instructions live in the project Skill:

```text
/Users/wenwendemac/project/amazon/.agents/skills/aplus-image-revision/
```

Start with:

```bash
python ../.agents/skills/aplus-image-revision/scripts/get_tasks.py \
  --status needs_revision \
  --download-dir /tmp/aplus-revision
```

The workflow is:

1. Human marks a normal `970×600` manifest PNG as `需修改` and writes comments. For a valid PNG size exception, the human opens the card and clicks **加入尺寸修复队列** (or calls `POST /api/dimension-repair-queue`); this changes only its review status to `needs_revision`.
2. External AI calls `GET /api/ai/revision-tasks?status=needs_revision`.
3. Each task includes SKU, module, human comments/instructions, original prompt/copy, product info, target image, product main image, revision/SHA, and submit URL.
4. AI downloads only through task URLs, analyzes target + product main image + comments, and creates a `970×600` PNG candidate. A `dimension_repair` task also includes the frozen current/expected dimensions and a mandatory no-stretch canvas-rebuild instruction.
5. AI validates locally, then submits `multipart/form-data` to `POST /api/ai/revision-results`.
6. Reviewer rechecks the target/comment/reference/inventory snapshots, verifies PNG size/content, backs up the original, records a durable application journal, atomically replaces the target, and updates the asset to `已修改`.
7. A human reviews the new image and explicitly marks it `OK`, `忽略`, or `需修改` again.

If product facts are insufficient, AI submits `needs_human_input` without an image. The original remains untouched.

### External API summary

```text
GET  /api/ai/revision-tasks?status=needs_revision&sku=AC0002
GET  /api/ai/revision-tasks/{task_id}
GET  /api/ai/revision-tasks/{task_id}/target
GET  /api/ai/revision-tasks/{task_id}/reference
POST /api/ai/revision-results
GET  /api/ai/revision-results?asset_id=...&task_id=...
```

A completed result requires a real PNG at exactly `970×600`, 10KB–30MiB, non-flat, and different from the source bytes. A main image is mandatory for automatic replacement. `needs_human_input` and `failed` are report-only and must not upload a file.

The API uses optimistic task fields and SHA-256 checks. A `409` means stale content/comments/reference/inventory state; fetch a new task and re-analyze. A network retry after a successful application is safe only with the **identical candidate bytes**; a different candidate is rejected.

The reviewer stores an application journal before replacing a file. On startup it resolves interrupted applications when the target SHA clearly matches either the original or recorded candidate; ambiguous cases are marked for human recovery rather than silently overwritten.

Read the complete API contract:

```text
../.agents/skills/aplus-image-revision/references/api.md
```

## IOPaint quick erase

When configured, `消除` opens a normal manifest image in local IOPaint/LaMa:

```yaml
iopaint:
  enabled: true
  url: http://127.0.0.1:5055
  backup_before_edit: true
```

The reviewer backs up before opening IOPaint. When the browser returns to the review page, it refreshes that asset and detects any changed version. IOPaint edits are still followed by `已修改` and human review; do not use multiple IOPaint tabs for different source images simultaneously.

## A+ delivery gate

A SKU can create an A+ delivery version only when all fixed modules `A+L01`–`A+L05` pass all gates:

- exact expected slots are present and normal;
- each image is `970×600`;
- each current revision is `approved` or `ignored`, or an unchanged already-delivered revision;
- every image has Alt Text;
- no product exception or duplicate/path/manifest issue exists.

The system never creates a partial five-image delivery. Changed images must be reviewed again before a new delivery. Once all five current images are confirmed, the delivery screen compares their asset ID/revision/SHA and Alt Text against the latest synced snapshot for that SKU. It creates a new pending-delivery entry only for a first delivery or a real image/Alt Text change; ordinary review clicks do not affect the image-change ordering. Delivery snapshots are immutable and fingerprinted: their content URLs include the frozen revision/SHA and return `409` rather than serving later changed bytes.

## Data, backups, and recovery

Persistent data is under `data/`:

```text
data/reviews.db                 # sources, assets, reviews, tasks, audits, delivery snapshots
data/backups/<source>/<asset>/  # versioned source backups
data/ai-incoming/               # temporary API uploads, deleted after request handling
data/locks/                     # per-asset process locks
```

SQLite uses WAL mode and a busy timeout to coexist with periodic scans and request handlers. Back up the database while running with:

```bash
sqlite3 data/reviews.db ".backup 'data/reviews-backup.db'"
```

Do not restore an old database over a running service. If an external application reports recovery required, inspect the task audit and versioned backup before changing a production image.

## Validation

```bash
cd /Users/wenwendemac/project/amazon/image-reviewer
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest -q
.venv/bin/python -m compileall -q app tests run.py tools
node --check static/app.js
bash -n install-macos.sh
```

## Network safety

The API intentionally has no token for this personal local workflow. Keep the default `127.0.0.1` binding. If you deliberately expose it to another machine, use a trusted network plus appropriate authentication, TLS, and network restrictions; otherwise any reachable caller can read tasks and submit a technically valid image replacement.
