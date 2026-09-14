---
name: aplus-image-revision
description: Retrieve Amazon A+ image revision tasks from image-reviewer, inspect the human comments, original generation prompt, SKU, product-main reference and target image, then submit a corrected 970×600 PNG or a report-only result safely. Use only for images manually marked 需修改.
---

# Amazon A+ Image Revision

Use this Skill when an `image-reviewer` image is manually marked **需修改** (`needs_revision`) and must be corrected by an external image-editing AI such as Cursor. This includes a valid PNG canvas mismatch that a human has explicitly added to the **尺寸修复队列**.

`image-reviewer` is the source of truth for the target image, human comments, SKU/module, original generation context, product reference, revision, and SHA-256. Do **not** infer those values from a directory listing or directly overwrite a production file.

## Non-negotiable rules

1. **Never write to the task's `absolute_path`.** Submit through `POST /api/ai/revision-results`; the reviewer validates the result, backs up the source, applies it safely, audits it, and refreshes its revision.
2. The product main image is the visual fact source for shape, colour, material, transparent parts, structure, accessories, variants, package count, and product quantity.
3. Human `instructions` / `comments` are mandatory acceptance criteria. Preserve valid parts of the original image and make the minimum change needed.
4. A completed result must be a real **PNG at exactly `970×600` pixels**. This project uses that fixed `97:60` canvas; do not call it or resize it as `16:10`.
5. Never approve, ignore, or deliver an image. A successful submission becomes **已修改** (`modified_pending_review`) and requires human review.
6. If facts are missing or ambiguous, do not guess. Submit `needs_human_input` **without an image**.
7. Do not use a task whose source, comments, reference, or revision is stale. On HTTP `409`, discard the candidate and retrieve a new task.

## Local server

Default reviewer URL:

```text
http://127.0.0.1:8700
```

Override it only for a trusted local setup:

```bash
export IMAGE_REVIEWER_URL=http://127.0.0.1:8700
```

The personal workflow has no API token. The reviewer defaults to loopback-only access; do not expose its write API to an untrusted network.

## Workflow

### 1. Fetch only manually requested revisions

The external queue intentionally accepts only `needs_revision`. `已修改` is waiting for a human review and is **not** a new automatic revision task.

A `尺寸异常` image does not enter automatically. In image-reviewer, open a card marked **可加入 AI 尺寸修复队列**, optionally add human context, and click **加入尺寸修复队列**; or explicitly call `POST /api/dimension-repair-queue`. Only a fully decodable PNG whose manifest requires `970×600` can be queued. Broken files, non-PNG files, wrong paths, duplicate modules, product exceptions, and other manifest errors remain manual-only.

```bash
python .agents/skills/aplus-image-revision/scripts/get_tasks.py \
  --status needs_revision \
  --download-dir /tmp/aplus-revision
```

Optionally narrow by SKU:

```bash
python .agents/skills/aplus-image-revision/scripts/get_tasks.py \
  --sku AC0002 \
  --download-dir /tmp/aplus-revision
```

Each downloaded task is saved as:

```text
/tmp/aplus-revision/<task_id>/task.json
/tmp/aplus-revision/<task_id>/target.png
/tmp/aplus-revision/<task_id>/reference.<ext>  # when a main image is available
```

Process one task at a time. For a SKU with several tasks, inspect `A+L01` through `A+L05` for product/visual consistency before editing one module.

### 2. Read the complete frozen task context

Read `task.json` before opening an image editor. It includes:

- `task_id`, `sku`, `module`, `asset_id`;
- `comments` and line-separated `instructions`;
- `original_prompt`, `original_copy` (`headline` / `body`), and `product_info`;
- `target_image` / `image`: relative path, local absolute path, and controlled download URL;
- `main_image` / `reference_image`: product-main-image path, URL, asset revision, and SHA-256;
- `revision`, `sha256`, `instructions_hash` needed on submit;
- `expected_output`: PNG, `970×600`.

The local absolute paths are supplied so a local AI can inspect context, but they are **not** write permissions. Download images through the task URLs or use the copies created by `get_tasks.py`.

If `main_image.url` is `null`, product facts cannot be verified. Do not generate a candidate; submit `needs_human_input` and state what reference is needed.

### 3. Analyze before editing

Compare all of the following together:

1. target A+ image;
2. product main/reference image;
3. human comments and instructions;
4. original generation prompt/copy and product information.

The original prompt/copy explains intended module content, but it cannot override visible product facts from the main image. For comments such as “与主图不一致”, systematically compare silhouette, colours, materials, transparency, joints, accessories, package contents, product count, direction, and variant.

### 4. Apply Amazon A+ visual constraints

Read [background guidelines](references/background-guidelines.md) before generating/editing. In particular:

- keep the product visually primary;
- use clean, low-noise, relevant backgrounds;
- keep text readable and away from the canvas edge;
- do not add price, discount, ratings, Amazon/Prime, URLs, contact details, watermarks, or unsupported claims;
- keep the same SKU visually/factually consistent across five modules.

### 5. Validate the candidate locally

```bash
python .agents/skills/aplus-image-revision/scripts/validate_image.py candidate.png
```

This checks real PNG decoding, exact dimensions, file size, pixel safety, non-flat signal, and (during completed submission) that it differs from the target bytes. It does not prove product facts or copy compliance.

### 6. Submit a completed correction

```bash
python .agents/skills/aplus-image-revision/scripts/submit_result.py \
  --task /tmp/aplus-revision/air-.../task.json \
  --image candidate.png \
  --summary "Corrected the product colour and removed the extra accessory." \
  --changes '["Matched the main-image product colour", "Removed the extra accessory"]' \
  --provider cursor \
  --model "auto"
```

The server rechecks the frozen target/comments/reference/inventory state, writes a verified backup, records a durable application journal, atomically replaces the PNG, and changes the asset to `modified_pending_review` / **已修改**. For a `dimension_repair` task, the task includes a system instruction and current/expected dimensions; rebuild the canvas to `970×600` without stretching or inventing the product.

### 7. Report instead of guessing

For missing product facts, conflicting feedback, unsupported claims, or an editing failure, submit no image:

```bash
python .agents/skills/aplus-image-revision/scripts/submit_result.py \
  --task /tmp/aplus-revision/air-.../task.json \
  --status needs_human_input \
  --summary "The supplied main image does not show the required back-side connector." \
  --uncertainties '["Need a back-side product reference image"]'
```

Use `--status failed` for technical failures. Both report-only statuses leave the target image and review status unchanged.

## Conflict and retry rules

- **HTTP `409`**: Source file, comments, product reference, path, inventory state, or task snapshot changed. Do not reuse the candidate; retrieve/download a new task and analyze again.
- **HTTP `422`**: Correct the candidate/result metadata, or use `needs_human_input` if facts are uncertain.
- **HTTP `503`**: The reviewer could not safely apply or recover an image. Do not write files directly; inspect the server response and have a human check the backup/audit record.
- If a network response was lost after a successful completed submission, retry **only the same candidate bytes** with the same task fields. The reviewer returns `idempotent: true`. A different candidate for an already-applied task returns `409`.

See [API reference](references/api.md) and [troubleshooting](references/troubleshooting.md) for the full request contract and failure handling.
