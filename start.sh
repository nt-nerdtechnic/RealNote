#!/usr/bin/env bash
# 會議記錄 ASR — 啟動程式
# 用法：bash start.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── 配色（與 install.sh 一致） ────────────────────────────────
BOLD='\033[1m'
DIM='\033[2m'
RED='\033[91m'
GRN='\033[92m'
YLW='\033[93m'
NC='\033[0m'

ok()   { printf "  ${GRN}✓${NC}  %s\n" "$*"; }
warn() { printf "  ${YLW}!${NC}  %s\n" "$*"; }
fail() { printf "  ${RED}✗${NC}  %s\n" "$*"; }
info() { printf "  ${DIM}→${NC}  %s\n" "$*"; }

# ─── 標頭 ─────────────────────────────────────────────────────
printf "${BOLD}"
printf '╔══════════════════════════════════════════════╗\n'
printf '║  會議記錄 ASR  —  啟動                       ║\n'
printf '╚══════════════════════════════════════════════╝\n'
printf "${NC}\n"

FATAL=0

# ─── 前置條件檢查 ─────────────────────────────────────────────

# backend venv
if [[ -x "$SCRIPT_DIR/backend/.venv/bin/python3" ]]; then
  ok "backend/.venv"
else
  fail "backend/.venv 不存在 → 請先執行 bash install.sh"
  FATAL=1
fi

# node_modules
if [[ -d "$SCRIPT_DIR/node_modules" ]]; then
  ok "node_modules"
else
  fail "node_modules 不存在 → 請先執行 bash install.sh"
  FATAL=1
fi

# .env
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  ok ".env"
  # 提醒 API key
  if ! grep -q "OPENAI_API_KEY=.\+" "$SCRIPT_DIR/.env" 2>/dev/null; then
    warn "OPENAI_API_KEY 未設定（LLM 摘要功能將停用）"
  fi
else
  warn ".env 不存在（LLM 摘要功能將停用）"
fi

# 致命錯誤 → 中止
if [[ "$FATAL" -eq 1 ]]; then
  printf "\n${RED}${BOLD}請先執行：bash install.sh${NC}\n\n"
  exit 1
fi

# ─── 音訊裝置檢查 ─────────────────────────────────────────────
AUDIO_INFO=$(system_profiler SPAudioDataType 2>/dev/null)

HAS_BLACKHOLE=0; HAS_MULTIOUT=0; HAS_AGGREGATE=0
grep -qi "blackhole" <<< "$AUDIO_INFO"                      && HAS_BLACKHOLE=1
grep -qiE "multi.?output|多重輸出" <<< "$AUDIO_INFO"         && HAS_MULTIOUT=1
grep -qiE "aggregate|聚集" <<< "$AUDIO_INFO"                 && HAS_AGGREGATE=1
# 使用者可能改過聚集裝置名稱：Input Channels >= 3 也算
if [[ "$HAS_AGGREGATE" -eq 0 ]]; then
  grep -qE "Input Channels: [3-9]" <<< "$AUDIO_INFO"         && HAS_AGGREGATE=1
fi

AUDIO_WARN=0

if [[ "$HAS_BLACKHOLE" -eq 0 ]]; then
  printf "  ${RED}[缺少]${NC} BlackHole 2ch 虛擬音訊裝置\n"
  printf "    ${DIM}brew install --cask blackhole-2ch（安裝後需重新啟動電腦）${NC}\n"
  AUDIO_WARN=1
else
  ok "BlackHole 2ch 虛擬音訊裝置"
fi

if [[ "$HAS_MULTIOUT" -eq 0 ]]; then
  printf "  ${YLW}[缺少]${NC} 多重輸出裝置（Multi-Output Device）\n"
  printf "    ${DIM}音訊 MIDI 設定 → + → 建立多重輸出裝置 → 勾選喇叭/耳機 + BlackHole 2ch${NC}\n"
  AUDIO_WARN=1
else
  ok "多重輸出裝置（Multi-Output Device）"
fi

if [[ "$HAS_AGGREGATE" -eq 0 ]]; then
  printf "  ${YLW}[缺少]${NC} 聚集裝置（Aggregate Device）— 麥克風錄音時需要\n"
  printf "    ${DIM}音訊 MIDI 設定 → + → 建立聚集裝置 → 勾選 BlackHole 2ch + 麥克風${NC}\n"
  AUDIO_WARN=1
else
  ok "聚集裝置（Aggregate Device）"
fi

if [[ "$AUDIO_WARN" -eq 1 ]]; then
  printf "\n"
  read -rp "  音訊裝置設定不完整，仍要繼續？(y/N) " _reply
  [[ "$_reply" =~ ^[Yy]$ ]] || exit 0
fi

# ─── 啟動 ─────────────────────────────────────────────────────
printf "\n${BOLD}▸ 啟動${NC}\n"
printf "${DIM}──────────────────────────────────────────────────${NC}\n"
info "pnpm dev"
printf "\n"

cd "$SCRIPT_DIR"
exec pnpm dev
