#!/usr/bin/env bash
# image-reviewer macOS 一键安装与登录自启动
#
# 用法：
#   ./install-macos.sh            安装/更新依赖，注册自启动并立即启动
#   ./install-macos.sh install    同上
#   ./install-macos.sh status     查看 LaunchAgent 和 HTTP 服务状态
#   ./install-macos.sh restart    重启服务
#   ./install-macos.sh uninstall  停止服务并移除自启动（保留代码、数据库和 .venv）
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$PROJECT_DIR/.venv"
CONFIG="$PROJECT_DIR/config.yaml"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/image-reviewer.log"
WORKER_LOG_FILE="$LOG_DIR/image-reviewer-ai-worker.log"
SERVICE_LABEL="com.amazon.image-reviewer"
WORKER_LABEL="com.amazon.image-reviewer.ai-worker"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$SERVICE_LABEL.plist"
WORKER_PLIST="$PLIST_DIR/$WORKER_LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE_TARGET="$DOMAIN/$SERVICE_LABEL"
WORKER_TARGET="$DOMAIN/$WORKER_LABEL"

log()  { printf '\033[1;32m[image-reviewer]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[image-reviewer 警告]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[image-reviewer 错误]\033[0m %s\n' "$*" >&2; }

if [[ "$(uname -s)" != "Darwin" ]]; then
  err "此脚本仅支持 macOS。"
  exit 1
fi

config_port() {
  local value
  value="$(grep -E '^[[:space:]]*port:' "$CONFIG" | head -1 | awk '{print $2}' || true)"
  printf '%s' "${value:-8700}"
}
PORT="$(config_port)"

lan_ipv4() {
  local ip
  for interface in en0 en1; do
    ip="$(ipconfig getifaddr "$interface" 2>/dev/null || true)"
    if [[ -n "$ip" ]]; then
      printf '%s' "$ip"
      return
    fi
  done
  return 1
}

config_ai_revision_enabled() {
  awk '
    /^ai_revision:[[:space:]]*$/ { enabled_section=1; next }
    enabled_section && /^[^[:space:]]/ { enabled_section=0 }
    enabled_section && /^[[:space:]]+enabled:[[:space:]]*/ { print $2; exit }
  ' "$CONFIG" | tr '[:upper:]' '[:lower:]'
}
AI_REVISION_ENABLED="$(config_ai_revision_enabled)"

ai_worker_enabled() {
  [[ "$AI_REVISION_ENABLED" == "true" || "$AI_REVISION_ENABLED" == "1" || "$AI_REVISION_ENABLED" == "yes" ]]
}

is_loaded() {
  launchctl print "$SERVICE_TARGET" >/dev/null 2>&1
}

worker_is_loaded() {
  launchctl print "$WORKER_TARGET" >/dev/null 2>&1
}

http_ready() {
  curl --silent --fail --max-time 2 "http://127.0.0.1:$PORT/" >/dev/null 2>&1
}

wait_for_http() {
  local attempts=0
  until http_ready; do
    attempts=$((attempts + 1))
    if (( attempts >= 30 )); then
      return 1
    fi
    sleep 1
  done
}

stop_service() {
  if is_loaded; then
    launchctl bootout "$SERVICE_TARGET" >/dev/null 2>&1 || true
  fi
  if worker_is_loaded; then
    launchctl bootout "$WORKER_TARGET" >/dev/null 2>&1 || true
  fi
}

bootstrap_service() {
  local plist="$1"
  local target="$2"
  local attempts=0
  until launchctl bootstrap "$DOMAIN" "$plist"; do
    attempts=$((attempts + 1))
    if (( attempts >= 3 )); then
      err "无法加载 LaunchAgent: $target"
      return 1
    fi
    warn "LaunchAgent 尚未完全退出，1 秒后重试: $target"
    sleep 1
  done
}

show_status() {
  if is_loaded; then
    log "LaunchAgent 已加载: $SERVICE_LABEL"
    launchctl print "$SERVICE_TARGET" 2>/dev/null | grep -E 'state =|pid =|last exit code =' || true
  else
    warn "LaunchAgent 未加载: $SERVICE_LABEL"
  fi
  if ai_worker_enabled; then
    if worker_is_loaded; then
      log "旧 Cursor ACP Worker LaunchAgent 已加载: $WORKER_LABEL"
      launchctl print "$WORKER_TARGET" 2>/dev/null | grep -E 'state =|pid =|last exit code =' || true
    else
      warn "旧 Cursor ACP Worker LaunchAgent 未加载: $WORKER_LABEL"
    fi
  else
    log "旧 Cursor ACP Worker 已禁用；请使用外部 AI API 与 aplus-image-revision Skill。"
  fi

  if http_ready; then
    log "HTTP 服务正常: http://127.0.0.1:$PORT/"
  else
    warn "HTTP 服务当前无法访问: http://127.0.0.1:$PORT/"
  fi
}

uninstall() {
  log "停止并移除 image-reviewer 登录自启动..."
  stop_service
  rm -f "$PLIST" "$WORKER_PLIST"
  log "已移除 LaunchAgent: $SERVICE_LABEL、$WORKER_LABEL"
  log "项目代码、评审数据库、日志和 .venv 均已保留。"
}

restart() {
  if ! [[ -f "$PLIST" ]]; then
    err "尚未安装 LaunchAgent，请先运行: $PROJECT_DIR/install-macos.sh"
    exit 1
  fi
  if is_loaded; then
    launchctl kickstart -k "$SERVICE_TARGET"
  else
    launchctl bootstrap "$DOMAIN" "$PLIST"
  fi
  if ai_worker_enabled; then
    if [[ ! -f "$WORKER_PLIST" ]]; then
      warn "旧 Worker 已在配置中启用，但尚未安装；请运行 install-macos.sh 更新 LaunchAgent。"
    elif worker_is_loaded; then
      launchctl kickstart -k "$WORKER_TARGET"
    else
      launchctl bootstrap "$DOMAIN" "$WORKER_PLIST"
    fi
    log "Web 服务与旧 Cursor ACP Worker 已重启。"
  else
    if worker_is_loaded; then
      launchctl bootout "$WORKER_TARGET" >/dev/null 2>&1 || true
    fi
    log "Web 服务已重启；旧 Cursor ACP Worker 保持禁用。"
  fi
}

ACTION="${1:-install}"
case "$ACTION" in
  uninstall) uninstall; exit 0 ;;
  status) show_status; exit 0 ;;
  restart) restart; wait_for_http || warn "HTTP 服务未能在 30 秒内就绪"; show_status; exit 0 ;;
  install) ;;
  *) err "未知命令: $ACTION（可用：install、status、restart、uninstall）"; exit 2 ;;
esac

# 1. Python 3.11+
log "检查 Python 3.11+ ..."
if ! command -v python3 >/dev/null 2>&1; then
  err "未找到 python3，请先安装：brew install python@3.12"
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  err "Python 版本过低（需要 >= 3.11）：$(python3 --version 2>&1)"
  exit 1
fi
log "$(python3 --version) 可用"

# 2. 独立虚拟环境与依赖
log "准备独立虚拟环境并安装依赖..."
if ! [[ -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$PROJECT_DIR/requirements.txt"
"$VENV/bin/python" -m compileall -q "$PROJECT_DIR/app" "$PROJECT_DIR/run.py" "$PROJECT_DIR/ai_worker.py"
mkdir -p "$LOG_DIR" "$PLIST_DIR"
log "依赖与代码检查完成"

# 3. 用 plistlib 生成 XML，避免项目路径中的 XML 特殊字符破坏 plist。
log "注册 LaunchAgent..."
stop_service
sleep 1
rm -f "$WORKER_PLIST"
PLIST_PATH="$PLIST" WORKER_PLIST_PATH="$WORKER_PLIST" AI_WORKER_ENABLED="$AI_REVISION_ENABLED" PROJECT_PATH="$PROJECT_DIR" PYTHON_PATH="$VENV/bin/python" LOG_PATH="$LOG_FILE" WORKER_LOG_PATH="$WORKER_LOG_FILE" LABEL_VALUE="$SERVICE_LABEL" WORKER_LABEL_VALUE="$WORKER_LABEL" python3 <<'PY'
import os
import plistlib

common = {
    "WorkingDirectory": os.environ["PROJECT_PATH"],
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ThrottleInterval": 5,
    "ProcessType": "Background",
}
web = {
    **common,
    "Label": os.environ["LABEL_VALUE"],
    "ProgramArguments": [os.environ["PYTHON_PATH"], os.path.join(os.environ["PROJECT_PATH"], "run.py")],
    "StandardOutPath": os.environ["LOG_PATH"],
    "StandardErrorPath": os.environ["LOG_PATH"],
}
worker = {
    **common,
    "Label": os.environ["WORKER_LABEL_VALUE"],
    "ProgramArguments": [os.environ["PYTHON_PATH"], os.path.join(os.environ["PROJECT_PATH"], "ai_worker.py")],
    "StandardOutPath": os.environ["WORKER_LOG_PATH"],
    "StandardErrorPath": os.environ["WORKER_LOG_PATH"],
}
payloads = [(os.environ["PLIST_PATH"], web)]
if os.environ.get("AI_WORKER_ENABLED", "").lower() in {"true", "1", "yes"}:
    payloads.append((os.environ["WORKER_PLIST_PATH"], worker))
for path, payload in payloads:
    with open(path, "wb") as file:
        plistlib.dump(payload, file)
PY
plutil -lint "$PLIST" >/dev/null
bootstrap_service "$PLIST" "$SERVICE_TARGET"
launchctl enable "$SERVICE_TARGET" >/dev/null 2>&1 || true
if ai_worker_enabled; then
  plutil -lint "$WORKER_PLIST" >/dev/null
  bootstrap_service "$WORKER_PLIST" "$WORKER_TARGET"
  launchctl enable "$WORKER_TARGET" >/dev/null 2>&1 || true
  log "Web 与旧 Cursor ACP Worker LaunchAgent 已启动；以后登录 macOS 时会自动启动。"
else
  log "Web LaunchAgent 已启动；旧 Cursor ACP Worker 保持禁用。"
fi

# 4. 就绪检查
log "等待 HTTP 服务就绪..."
READY=""
for _ in {1..30}; do
  if http_ready; then READY="1"; break; fi
  sleep 1
done
if [[ -z "$READY" ]]; then
  err "服务未能在 30 秒内启动，请查看日志：$LOG_FILE"
  launchctl print "$SERVICE_TARGET" 2>/dev/null | grep -E 'state =|pid =|last exit code =' || true
  exit 1
fi

LAN_IP="$(lan_ipv4 || true)"
log "✅ image-reviewer 已启动：http://127.0.0.1:$PORT/"
cat <<EOF

本机访问：
  http://127.0.0.1:$PORT/
EOF
if [[ -n "$LAN_IP" ]]; then
  cat <<EOF
可信局域网访问：
  http://$LAN_IP:$PORT/
EOF
else
  warn "未检测到 Wi-Fi/以太网 IPv4；连接局域网后可执行：ipconfig getifaddr en0"
fi
cat <<EOF

安全提示：服务默认监听局域网，当前评审与外部 AI API 未设置登录认证。仅在可信私有网络使用；不要端口映射或暴露到公网。不需要局域网访问时，将 config.yaml 的 server.host 改回 127.0.0.1 后重启。

常用命令：
  查看状态  $PROJECT_DIR/install-macos.sh status
  重启服务  $PROJECT_DIR/install-macos.sh restart
  查看 Web 日志     tail -f $LOG_FILE
  查看旧 AI Worker 日志（仅手动启用时） tail -f $WORKER_LOG_FILE
  卸载自启  $PROJECT_DIR/install-macos.sh uninstall

外部修图请使用项目 Skill：$PROJECT_DIR/../.agents/skills/aplus-image-revision/
EOF
