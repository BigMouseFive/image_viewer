# A+ 图片修订故障排查

先遵守两个安全边界：**不要直接覆盖 task 返回的 `absolute_path`**，也不要在 stale（`409`）后把旧候选图提交到新版本。所有正式修改必须经 `POST /api/ai/revision-results` 处理。

## 快速自检

```bash
# Python 版本；脚本要求 3.11+
python3 --version

# 不依赖 requests；validate_image.py 和 completed 提交的本地校验需要 Pillow。
# get_tasks.py 与 report-only 提交本身只使用标准库。
python3 -c 'from PIL import Image; print(Image.__version__)'

# 查看脚本参数
python .agents/skills/aplus-image-revision/scripts/get_tasks.py --help
python .agents/skills/aplus-image-revision/scripts/validate_image.py --help
python .agents/skills/aplus-image-revision/scripts/submit_result.py --help
```

默认 reviewer 地址是 `http://127.0.0.1:8700`。如服务在其他可信主机或端口运行：

```bash
export IMAGE_REVIEWER_URL=http://127.0.0.1:8700
python .agents/skills/aplus-image-revision/scripts/get_tasks.py
```

`submit_result.py` 默认优先使用 task 中的 `result_submit_url`；可用 `--url` 显式覆盖。

## 常见问题

### 无法连接服务 / `URLError` / connection refused

**检查：**

1. 确认 `IMAGE_REVIEWER_URL` 使用正确协议、主机和端口；不要多拼 `/api/...` 到环境变量中。
2. 在服务所在机器打开 `http://127.0.0.1:8700/docs` 或运行：

   ```bash
   curl -i http://127.0.0.1:8700/api/ai/revision-tasks
   ```

3. 确认 image-reviewer 已启动、有活动图片源，且本机/局域网防火墙允许访问。
4. 如果 task 的 `result_submit_url` 指向过期的公网/局域网地址，显式传入当前地址：

   ```bash
   python .agents/skills/aplus-image-revision/scripts/submit_result.py ... \
     --url http://127.0.0.1:8700
   ```

不要把这个未认证服务暴露到公网。

### 列表返回 `items: []`

外部任务列表不会把所有图片都返回。目标图片必须同时满足：

- 属于当前活动源；
- 是正常 `970×600` 清单中的可交付 PNG 且有非空人工 comments，**或**是已明确加入尺寸修复队列的有效 PNG 尺寸异常；
- 状态是**唯一允许的** `needs_revision`；
- 不是 reference、extra、missing、blocked、路径不符、模块重复、清单无效、产品异常、损坏文件或伪装 PNG。

按 SKU 查找：

```bash
python .agents/skills/aplus-image-revision/scripts/get_tasks.py \
  --status needs_revision \
  --sku YOUR-SKU
```

`unreviewed` 不会自动成为外部修图任务。尺寸异常也必须在页面中点击 **加入尺寸修复队列**（或显式调用 `POST /api/dimension-repair-queue`）后才会变为 `needs_revision`。`modified_pending_review`（已修改）不会自动再次入队，必须先由人工复核，必要时重新标记为“需修改”。

### `--download-dir` 不能下载 target/reference

- 始终使用 task 中的 `target_image.url`、`main_image.url`，不要改用本地路径或手工拼 `/images` URL。
- `main_image.url: null` 表示没有可用产品主图；这是产品事实不足，不是下载重试问题。提交 `needs_human_input`，说明需要的主图/角度。
- target/reference 返回 `409` 通常说明任务已经 stale 或已应用；重新拉取任务并重新下载。
- target 返回 `410` 表示源图已删除或不可访问；停止该任务并由人工确认素材。
- `get_tasks.py --download-dir DIR` 会为每个任务写入 `DIR/<task_id>/task.json`、`target.png` 和可用的 `reference.<ext>`。根 `--output FILE` 则是完整列表响应，不一定能直接提交。

### `submit_result.py` 提示 task JSON 不唯一或缺字段

`--task` 必须是一个任务对象，至少包含：

```json
{"task_id":"...","revision":0,"sha256":"<64 hex>","instructions_hash":"<64 hex>"}
```

脚本也可读取只有一个 `items[0]` 的列表响应。多个任务时不能猜测要提交哪一个；改用下载目录中的单任务文件：

```bash
--task /path/to/downloads/air-.../task.json
```

不要手动编辑 `revision`、`sha256` 或 `instructions_hash`。它们是并发保护字段。

### 尺寸异常没有出现在任务列表

尺寸异常不是自动重绘信号。确认以下条件：

1. manifest 中该模块目标为 `970×600`；
2. 源文件是可以完整解码的真实 PNG，不是空文件、损坏文件或仅改了扩展名；
3. 图片没有产品异常、路径不符、重复模块或其他清单错误；
4. 在 reviewer 图片弹窗点击 **加入尺寸修复队列**，或调用：

   ```bash
   curl -X POST http://127.0.0.1:8700/api/dimension-repair-queue \
     -H 'Content-Type: application/json' \
     -d '{"all_eligible":true}'
   ```

该操作只把符合条件的图片设为 `needs_revision`，不会立即修改图片。之后重新执行 `get_tasks.py --status needs_revision`。

### 本地图片校验失败

运行：

```bash
python .agents/skills/aplus-image-revision/scripts/validate_image.py candidate.png
```

要求与服务端相同：

- 实际解码格式为 PNG（仅把扩展名改为 `.png` 不够）；
- 精确 `970×600`；
- 文件可完整解码；
- 大小为 10 KB–30 MiB（含边界）；
- 灰度方差至少为 `2`，不能接近纯色；
- 提交 `completed` 时，候选 SHA-256 还必须不同于 task 的源 `sha256`。

常见修复：从编辑工具以 PNG 重新导出；在不拉伸主体的前提下重新布局到 970×600；移除全画布纯色占位；确认提交的是最终候选而非下载的原 target。

### `completed` 提示缺少 image、存在 uncertainties 或 MIME 不支持

- `--status completed` 必须带 `--image candidate.png`。
- `completed` 的 `--uncertainties` 必须是空 JSON 数组 `[]`；有无法确认的事实时改用 `--status needs_human_input`，不要上传图片。
- report-only（`needs_human_input` / `failed`）不允许传 `--image`；脚本会拒绝，以避免误上传。
- 通过本脚本上传时文件 part 使用 `image/png`。不要上传 JPEG、WebP 或伪装后缀的文件。

### HTTP `409`：任务 stale（过期）

可能是图片被其他人/流程改过、人工意见改过、路径变化或提交快照字段不匹配。处理方式：

1. 不要重试旧候选图，也不要篡改 task JSON 的 hash/revision；
2. 重新拉取列表；
3. 重新下载 target 和 reference；
4. 对新版本重新分析、生成和校验；
5. 用新任务字段提交。

唯一例外：你确认服务端可能已成功应用候选图，但网络在收到响应前中断。只有带**完全相同候选文件字节**和相同任务字段的重试会返回 `200` / `idempotent: true`；同一 task 的不同候选图会返回 `409`，不会覆盖已应用结果。

### HTTP `404` / `410`

| 状态 | 含义 | 操作 |
| --- | --- | --- |
| `404` | task ID 不存在，或请求了不存在的产品主图 | 刷新任务；主图不存在时报告不确定性。|
| `410` | 原目标图已删除/不可访问 | 停止提交；让人工恢复或重新选择素材。|

### HTTP `415` / `422`

| 状态 | 常见原因 | 操作 |
| --- | --- | --- |
| `415` | 上传文件 MIME 不是 PNG/二进制 PNG | 使用真实 PNG，通过本脚本上传。|
| `422` | `status` 非法、`summary` 为空、`changes_json`/`uncertainties_json` 不是 JSON 数组、图片尺寸/大小/解码/信号不合规、report-only 带图片、当前素材状态不允许修改 | 查看响应的 `detail`，修复具体问题；若内容提示图片/意见已变化，按 `409` 流程处理。|
| `503` | 备份、文件系统、SQLite 或中断应用恢复不能安全完成 | 不要直接覆盖目标图；让人工检查任务审计、版本化备份和服务日志。|

服务端在处理无效 completed 图片时可能保存一条 `rejected` 审计记录。它不意味着原图已被替换；检查响应中的 `applied`，不要把 `rejected` 当成“已修改”。

### report-only 看起来没有“关闭”任务

这是预期行为。`needs_human_input` 和 `failed` 只记录报告，原图和人工评审状态不变，任务记录变为 `reported`。这样人工可以补充主图/说明，或在同一快照仍有效时让 Agent 重试。

如果服务在文件替换和数据库提交之间异常退出，启动时会检查持久化应用日志。能安全匹配原图/候选图时会自动完成或标记回滚；无法安全判断时会返回/保留需要人工恢复的状态，不会继续自动覆盖。

报告应具体写出缺失信息，例如：

```bash
python .agents/skills/aplus-image-revision/scripts/submit_result.py \
  --task task.json \
  --status needs_human_input \
  --summary "Cannot determine the back-side connector from the supplied main image." \
  --uncertainties '["Need a back-side product reference image"]'
```

### 产品/模块间不一致但技术校验通过

`validate_image.py` 只能发现文件级问题，不会识别错误变体、错误数量、虚假配件、文字拼写或未经支持的宣称。重新阅读 [background-guidelines.md](background-guidelines.md)，尤其是“产品主图是视觉事实依据”和五模块一致性检查。事实无法证明时不要提交 `completed`。

## 需要人工介入的情况

立即提交 report-only 或暂停处理，而不是继续猜测：

- 没有产品主图，或主图无法看清必要结构；
- 主图、原图、文字或人工意见互相冲突；
- 无法确认包装/配件数量、颜色变体、透明部件或产品背面；
- 需要加入性能、认证、医疗、安全、兼容性或比较性文字但没有明确依据；
- 修改会影响同 SKU 的其他模块，而当前任务没有给出可验证的系列规则。
