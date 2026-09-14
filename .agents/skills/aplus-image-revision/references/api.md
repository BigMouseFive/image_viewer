# image-reviewer 外部 AI 修图 API

外部 AI 必须使用 `/api/ai/...` 接口。旧的 `GET /api/revision-tasks` 仅是早期简化清单，缺少冻结的任务 ID、图片指纹、受控下载 URL 和安全提交契约，**不能用于修图或提交结果**。

## 基础约定

- 默认地址：`http://127.0.0.1:8700`；可用 `IMAGE_REVIEWER_URL` 覆盖。
- 这是个人本机工作流，当前没有 API token。服务默认只监听 loopback；不要把未认证写接口暴露到不可信网络。
- 任务是目标图、人工意见、产品主图、prompt/copy 和产品资料的**冻结快照**。
- 提交时必须原样带回 `task_id`、`revision`、`sha256`、`instructions_hash`。
- 客户端只能通过任务 `target_image.url` / `main_image.url` 下载文件；`absolute_path` 仅供本机检查，**绝不能直接写入**。
- 所有提交成功后都进入 `modified_pending_review`（页面“已修改”），不能自动确认或交付。

## `GET /api/ai/revision-tasks`

获取当前活动图片源中可由外部 AI 处理的任务。每个符合条件的图片会创建或复用持久化任务快照。

```text
GET /api/ai/revision-tasks?status=needs_revision&sku=AC0002&limit=60&offset=0
```

### 查询参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `status` | `needs_revision` | 仅允许 `needs_revision`。`已修改`必须先由人工复核，不能再次自动进入 AI 队列。|
| `sku` | 空 | SKU 的大小写不敏感子串筛选。|
| `limit` | `60` | 服务端限制在 `1`–`200`。|
| `offset` | `0` | 负数会按 `0` 处理。|

返回项必须同时满足：

- 当前活动图片源；
- 人工状态为 `needs_revision`；
- 是可解码真实 PNG，且当前磁盘尺寸与扫描记录一致；
- 是正常 `970×600` 清单素材并有非空 `comments`，**或**是人工明确加入队列的可修复尺寸异常；
- 不是产品异常、缺失、阻塞、名单外、路径不符、模块重复或无效清单素材。

产品主图缺失时，任务仍可能返回，但 `main_image.url` 为 `null`；此类任务只能提交 `needs_human_input` / `failed` 报告，不能自动覆盖图片。

### 尺寸修复队列

尺寸异常并不会隐式自动覆盖已确认、已忽略或已交付的图片。只有 manifest 目标为 `970×600`、源文件是可解码 PNG、且用户明确调用队列接口后，才成为 `dimension_repair` 任务：

```http
POST /api/dimension-repair-queue
Content-Type: application/json

{"all_eligible": true}
```

或仅加入指定图片（最多 200 个）：

```json
{"all_eligible": false, "asset_ids": [123, 456]}
```

此操作仅将合格图片的评审状态设为 `needs_revision`，不会替换、确认、忽略或交付图片。响应包含 `queued`、`existing` 和 `skipped`。已修改状态仍必须先人工复核。

### 列表响应

```json
{
  "source": {"id": 1, "name": "UAE A+ 图片"},
  "target_output": {"format": "PNG", "width": 970, "height": 600},
  "result_statuses": ["completed", "failed", "needs_human_input"],
  "items": [],
  "total": 0,
  "offset": 0,
  "limit": 60,
  "has_more": false
}
```

### 任务对象

| 字段 | 说明 |
| --- | --- |
| `task_id` | 持久化 ID，例如 `air-...`；下载和提交均使用它。|
| `source_id` / `asset_id` | 服务端来源与图片 ID，便于追踪。|
| `sku` / `module` | SKU 与模块，通常为 `A+L01`–`A+L05`。|
| `task_status` | `open`、`reported`、`rejected` 或 `applied`。|
| `task_kind` | `review_revision` 或 `dimension_repair`。|
| `asset_status` | 当前评审状态；活跃任务必须为 `needs_revision`。|
| `comments` | 冻结的人工意见原文（首尾空白会被规范化）。|
| `instructions` | 从 `comments` 拆出的非空行数组；每条都是验收要求。|
| `instructions_hash` | `SHA-256(comments.strip().encode("utf-8"))`；提交时必须一致。|
| `dimension_repair` | 普通任务为 `null`；尺寸修复任务包含当前/目标尺寸和不可省略的系统修复指令。|
| `revision` / `sha256` | 冻结的目标图版本与 SHA-256；提交映射为 `source_revision` / `source_sha256`。|
| `target_image` | `relative_path`、本机 `absolute_path`、下载 `url`。|
| `main_image` | 产品主图的路径、下载 URL、`asset_id`、`revision` 和 `sha256`；主图缺失时路径/URL 为 `null`。|
| `image` / `reference_image` | `target_image` / `main_image` 的兼容别名。|
| `target_image_path` / `reference_image_path` | 本机绝对路径兼容别名；禁止写入。|
| `target_image_url` / `reference_image_url` | 下载 URL 兼容别名。|
| `original_prompt` | 原始生图提示词（可为 `null`）。|
| `original_copy` | `{"headline": string|null, "body": string|null}`。|
| `product_info` | 商品 `title`、`bullets`、`asin`（没有资料时为空对象）。|
| `expected_output` | 固定为 `{"format":"PNG","width":970,"height":600}`。|
| `result_submit_url` | 完整提交 URL。|

重复拉取同一目标版本、同一规范化评论、同一产品主图、同一 prompt/copy 和同一商品资料时会复用相同 `task_id`。目标/评论/主图/prompt/copy/商品资料任一变化时会创建新快照或让旧任务过期。

## `GET /api/ai/revision-tasks/{task_id}`

返回一个任务对象，字段与列表单项相同。

- 活跃任务会重新校验图片路径、目标 revision/SHA、人工评论、清单状态、产品主图 revision/SHA 与磁盘内容。
- 已 `applied` 的任务可查看历史上下文，但 `target_image.url` 和 `main_image.url` 为 `null`，不可再用于新编辑。
- `409` 表示任务已过期；重新获取任务列表。

## 下载目标图和产品主图

### `GET /api/ai/revision-tasks/{task_id}/target`

下载冻结任务的待改 PNG。仅对未过期、未应用任务有效。

成功响应常含：

```text
Content-Type: image/png
Cache-Control: no-cache
ETag: "sha256-<task-sha256>"
X-Image-Reviewer-Revision: <task-revision>
X-Image-Reviewer-SHA256: <task-sha256>
```

下载后验证字节 SHA-256 等于任务 `sha256`。`scripts/get_tasks.py --download-dir ...` 会自动验证。

### `GET /api/ai/revision-tasks/{task_id}/reference`

下载冻结任务的产品主图。它是产品形态、颜色、材质、透明部分、结构、配件、数量、包装和变体的事实依据。

只有 `main_image.url` 非空时才能调用。主图不存在时端点返回 `404`；不要猜测，提交 `needs_human_input`。

## `POST /api/ai/revision-results`

使用 `multipart/form-data` 提交结果：

```text
POST /api/ai/revision-results
```

### 表单字段

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `task_id` | 是 | 任务 `task_id`。|
| `source_revision` | 是 | 任务的 `revision`，不是候选图的新版本。|
| `source_sha256` | 是 | 任务的 `sha256`。|
| `instructions_hash` | 是 | 任务的 `instructions_hash`。|
| `result_status` | 是 | `completed`、`needs_human_input` 或 `failed`。|
| `summary` | 是 | 非空、最多 4,000 字符。|
| `changes_json` | 否 | JSON 字符串数组，默认 `[]`；最多 50 项，整个字段最多 20KB。|
| `uncertainties_json` | 否 | JSON 字符串数组，默认 `[]`；最多 50 项，整个字段最多 20KB。|
| `provider` / `model` | 否 | 生成/编辑提供方与模型标识，均最多 200 字符。|
| `image` | 视状态 | `completed` 必填的 PNG 文件；report-only 状态禁止上传。|

例如：

```text
changes_json=["将产品颜色与主图一致","删除多余配件"]
```

### 状态与行为

| `result_status` | 是否上传 `image` | 行为 |
| --- | --- | --- |
| `completed` | **必须** | 校验、备份、记录应用日志、原子覆盖并更新版本；状态转为 `modified_pending_review`。|
| `needs_human_input` | **不得上传** | 仅保存报告，原图与状态不变。|
| `failed` | **不得上传** | 仅保存失败报告，原图与状态不变。|

没有单独的 `report_only` 字段；`result_status != completed` 即为报告模式。

### `completed` 的服务端门禁

服务端在锁内再次确认：

1. 任务仍为当前 `needs_revision` 目标，且 revision、SHA、评论、路径未变化；
2. SKU 仍是正常 `970×600` 清单素材，或是任务快照中同一个、人工排队的有效尺寸修复；不能是产品异常、路径不符、重复模块、损坏/伪装 PNG 或其他清单异常；
3. 冻结的产品主图仍存在、版本/SHA 未变化；
4. 候选图是可解码真实 PNG，精确 `970×600`，10KB–30MiB，非近纯色，且字节不同于源图；
5. 原图备份 SHA-256 正确；
6. 服务端先保存持久化应用日志，再写入同目录临时文件并原子替换；素材版本、审计结果、任务状态与应用日志在一个 SQLite 事务中完成。

服务启动时会检查未完成应用日志：若目标仍是原图则标记回滚；若目标是登记候选图则补全数据库；若无法安全判断则标记为需要人工恢复，拒绝继续覆盖。

### 成功响应

```json
{
  "ok": true,
  "applied": true,
  "idempotent": false,
  "result": {"task_id": "air-...", "result_status": "completed"},
  "old_revision": 3,
  "new_revision": 4,
  "old_status": "needs_revision",
  "new_status": "modified_pending_review",
  "backup_path": "/.../backups/...",
  "validation": {
    "sha256": "...",
    "format": "PNG",
    "width": 970,
    "height": 600,
    "bytes": 123456,
    "variance": 42.0
  }
}
```

报告成功时：

```json
{
  "ok": true,
  "applied": false,
  "result": {"task_id": "air-...", "result_status": "needs_human_input"},
  "task_status": "reported",
  "message": "已记录 AI 报告，原图未修改"
}
```

若网络在成功提交后断开，可以用**完全相同的候选文件字节**和同一任务字段重试，服务返回 `200` 与 `idempotent: true`。已应用任务的不同候选图返回 `409`，不会被误认为成功。

## HTTP 状态与处理方式

| 状态 | 常见原因 | 客户端动作 |
| --- | --- | --- |
| `200` | 读取成功、报告记录成功、完成应用成功或同候选图幂等重试。 | 检查 `applied`、`idempotent` 和新状态。|
| `400` | 无活动图片源或目录不可用。 | 修复 reviewer 配置/激活目录。|
| `404` | 任务不存在或产品主图不存在。 | 重新获取；主图缺失时报告 `needs_human_input`。|
| `409` | 任务 stale、图片/评论/主图/清单状态变化，或已应用任务用了不同候选图。 | 丢弃候选图，重新获取并重新分析。|
| `410` | 目标图已删除或不可访问。 | 停止该任务，让人工恢复/确认素材。|
| `415` | `completed` 文件 MIME 不是 PNG 或通用二进制。 | 用真实 PNG 和 `image/png` 重试。|
| `422` | 格式、尺寸、图片信号、表单、状态或 report-only 上传错误。 | 修复具体问题；无法确认事实时提交报告。|
| `503` | 备份、SQLite、文件系统或恢复日志无法安全完成。 | 不直接覆盖文件；让人工检查返回信息、审计记录和备份。|

## stale 处理

`stale` 不是可提交的 `result_status`，用 HTTP `409` 表达。可能原因：

- 目标图 revision/SHA、磁盘字节或路径变化；
- 人工评论变化；
- 产品主图缺失或版本/SHA 变化；
- 清单无效、模块重复、路径不符、产品异常、源文件不再可解码 PNG，或其他库存状态变化；
- 提交表单的 revision/SHA/hash 不匹配；
- 同一任务已由不同候选图完成。

处理步骤：停止使用旧任务、重新获取列表、重新下载 target/reference、重新分析并生成新候选。不要手动编辑本地 `task.json` 的 revision/SHA/hash 来绕过冲突。
