#!/usr/bin/env bash
# 會議記錄 ASR — 安裝程式
# 用法：bash install.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── 配色（深色簡約） ──────────────────────────────────────────
BOLD='\033[1m'
DIM='\033[2m'
RED='\033[91m'
GRN='\033[92m'
YLW='\033[93m'
NC='\033[0m'

# ─── 工具函式 ──────────────────────────────────────────────────
ok()   { printf "  ${GRN}✓${NC}  %s\n" "$*"; }
fail() { printf "  ${RED}✗${NC}  %s\n" "$*"; ERRORS+=("$*"); }
warn() { printf "  ${YLW}!${NC}  %s\n" "$*"; }
info() { printf "  ${DIM}→${NC}  %s\n" "$*"; }

section() {
  printf "\n${BOLD}▸ %s${NC}\n" "$*"
  printf "${DIM}──────────────────────────────────────────────────${NC}\n"
}

run_spinner() {
  local msg="$1"; shift
  local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
  local i=0
  printf "  ${DIM}%s${NC}  %s" "${frames[0]}" "$msg"
  "$@" &>/tmp/_mm_install_out &
  local pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    printf "\r  ${DIM}%s${NC}  %s" "${frames[$((i % ${#frames[@]}))]}"; i=$((i+1))
    sleep 0.1
  done
  wait "$pid"; local rc=$?
  if [[ $rc -eq 0 ]]; then
    printf "\r  ${GRN}✓${NC}  %s\n" "$msg"
  else
    printf "\r  ${RED}✗${NC}  %s\n" "$msg"
    cat /tmp/_mm_install_out | tail -3 | sed 's/^/      /'
  fi
  return $rc
}

ERRORS=()

# ─── 標頭 ─────────────────────────────────────────────────────
clear
printf "${BOLD}"
printf '╔══════════════════════════════════════════════╗\n'
printf '║  會議記錄 ASR  —  安裝程式                   ║\n'
printf '╚══════════════════════════════════════════════╝\n'
printf "${NC}\n"

# ──────────────────────────────────────────────────────────────
section "系統環境"

# macOS
macos_ver=$(sw_vers -productVersion 2>/dev/null || echo "unknown")
ok "macOS $macos_ver"

# 磁碟空間（至少 5 GB）
free_gb=$(df -g "$HOME" 2>/dev/null | awk 'NR==2{print $4}')
if [[ -n "$free_gb" && "$free_gb" -ge 5 ]]; then
  ok "磁碟空間充足（${free_gb} GB 可用）"
else
  warn "磁碟空間不足（${free_gb:-?} GB），建議至少 5 GB"
fi

# Xcode CLT
if xcode-select -p &>/dev/null; then
  ok "Xcode Command Line Tools"
else
  fail "Xcode CLT 未安裝 → 請執行：xcode-select --install"
fi

# Homebrew
if command -v brew &>/dev/null; then
  ok "Homebrew $(brew --version 2>/dev/null | head -1 | awk '{print $2}')"
else
  fail "Homebrew 未安裝 → https://brew.sh"
fi

# ──────────────────────────────────────────────────────────────
section "系統套件"

_brew_pkg() {
  local pkg="$1" desc="$2" cask="${3:-}"
  local cmd="brew list"
  [[ -n "$cask" ]] && cmd="brew list --cask"
  if $cmd "$pkg" &>/dev/null; then
    ok "$desc"
  else
    local install_cmd="brew install"
    [[ -n "$cask" ]] && install_cmd="brew install --cask"
    if run_spinner "$desc" $install_cmd "$pkg"; then
      : # ok already printed by spinner
    else
      fail "$desc 安裝失敗"
    fi
  fi
}

_brew_pkg "ffmpeg"        "ffmpeg（音訊轉檔）"
_brew_pkg "blackhole-2ch" "BlackHole 2ch（虛擬音訊）" cask

# ──────────────────────────────────────────────────────────────
section "音訊裝置"

if system_profiler SPAudioDataType 2>/dev/null | grep -qi "BlackHole"; then
  ok "BlackHole 2ch 已在音訊裝置清單"
else
  warn "BlackHole 裝置未偵測到（已安裝但可能需重開機）"
  warn "重開機後若仍無效，請開啟「Audio MIDI Setup」手動確認"
fi

# 檢查是否已有 Multi-Output Device（BlackHole + 喇叭）
if system_profiler SPAudioDataType 2>/dev/null | grep -qi "Multi-Output\|Multi Output"; then
  ok "Multi-Output Device 已設定"
else
  info "建議在 Audio MIDI Setup 建立 Multi-Output Device"
  info "（BlackHole 2ch + 內建喇叭），讓錄音時仍可聽到聲音"
  info "路徑：應用程式 → 工具程式 → Audio MIDI Setup"
fi

# ──────────────────────────────────────────────────────────────
section "Node.js / pnpm"

# nvm 可能未載入，嘗試手動 source
if ! command -v node &>/dev/null; then
  [[ -s "$HOME/.nvm/nvm.sh" ]] && source "$HOME/.nvm/nvm.sh" 2>/dev/null || true
fi

if command -v node &>/dev/null; then
  ok "Node.js $(node --version)"
else
  fail "Node.js 未安裝 → 建議安裝 nvm：https://github.com/nvm-sh/nvm"
fi

if command -v pnpm &>/dev/null; then
  ok "pnpm $(pnpm --version)"
else
  info "pnpm 未安裝  → 安裝中..."
  if npm install -g pnpm &>/dev/null 2>&1; then
    ok "pnpm $(pnpm --version)"
  else
    fail "pnpm 安裝失敗（npm install -g pnpm）"
  fi
fi

# ──────────────────────────────────────────────────────────────
section "Python / uv"

# 尋找 Python 3.12
PY3=""
for py in python3.12 python3 python; do
  if command -v "$py" &>/dev/null; then
    ver=$("$py" --version 2>&1 | awk '{print $2}')
    major=${ver%%.*}; minor=${ver#*.}; minor=${minor%%.*}
    if [[ "$major" -eq 3 && "$minor" -ge 10 ]]; then
      PY3="$py"
      ok "Python $ver"
      break
    fi
  fi
done
[[ -z "$PY3" ]] && fail "未找到 Python 3.10+（建議：brew install python@3.12）"

if command -v uv &>/dev/null; then
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
else
  info "uv 未安裝  → 安裝中..."
  if brew install uv &>/dev/null 2>&1; then
    ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
  else
    fail "uv 安裝失敗（brew install uv）"
  fi
fi

# ──────────────────────────────────────────────────────────────
section "前端依賴"

cd "$SCRIPT_DIR"
if [[ -d node_modules && -f pnpm-lock.yaml ]]; then
  ok "node_modules 已存在"
else
  if run_spinner "pnpm install" pnpm install; then
    :
  else
    fail "pnpm install 失敗"
  fi
fi

# ──────────────────────────────────────────────────────────────
section "後端依賴（Python venv）"

if [[ -d "$SCRIPT_DIR/backend/.venv" ]]; then
  ok "backend/.venv 已存在"
else
  if run_spinner "uv sync（建立 .venv + 安裝套件）" \
       uv sync --project "$SCRIPT_DIR/backend"; then
    :
  else
    fail "uv sync 失敗"
  fi
fi

# 驗證關鍵套件
VENV_PY="$SCRIPT_DIR/backend/.venv/bin/python3"
if [[ -x "$VENV_PY" ]]; then
  for pkg in mlx_whisper faster_whisper silero_vad; do
    if "$VENV_PY" -c "import $pkg" &>/dev/null 2>&1; then
      ok "$pkg"
    else
      fail "$pkg 未安裝（執行 uv sync --project backend 修復）"
    fi
  done
else
  warn "找不到 backend/.venv/bin/python3，跳過套件驗證"
fi

# ──────────────────────────────────────────────────────────────
section "語音辨識模型（MLX Whisper）"

# 取得 HuggingFace 快取路徑
HF_CACHE=$("$VENV_PY" -c \
  "from huggingface_hub import constants; print(constants.HF_HUB_CACHE)" \
  2>/dev/null || echo "$HOME/.cache/huggingface/hub")

MLX_MODEL="mlx-community/whisper-medium-mlx-q4"
MLX_CACHE_DIR="$HF_CACHE/models--mlx-community--whisper-medium-mlx-q4"

if [[ -d "$MLX_CACHE_DIR" ]]; then
  ok "whisper-medium-mlx-q4（已快取）"
else
  if run_spinner "下載 $MLX_MODEL（~400 MB）" \
       "$VENV_PY" -c "
from huggingface_hub import snapshot_download
snapshot_download('$MLX_MODEL')
"; then
    :
  else
    fail "模型下載失敗，請確認網路連線後重試"
  fi
fi

# Tiny 預覽模型
MLX_TINY_CACHE="$HF_CACHE/models--mlx-community--whisper-tiny-mlx-q4"
if [[ -d "$MLX_TINY_CACHE" ]]; then
  ok "whisper-tiny-mlx-q4（預覽用，已快取）"
else
  if run_spinner "下載 whisper-tiny-mlx-q4（~40 MB）" \
       "$VENV_PY" -c "
from huggingface_hub import snapshot_download
snapshot_download('mlx-community/whisper-tiny-mlx-q4')
"; then
    :
  else
    warn "tiny 模型下載失敗（非必要，可稍後重試）"
  fi
fi

# ──────────────────────────────────────────────────────────────
section "環境設定"

if [[ -f "$SCRIPT_DIR/.env" ]]; then
  ok ".env 已存在"
else
  if [[ -f "$SCRIPT_DIR/.env.example" ]]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    ok ".env 已從 .env.example 建立"
    warn "如需 LLM 摘要功能，請在 .env 填入 OPENAI_API_KEY"
  else
    warn ".env.example 不存在，請手動建立 .env"
  fi
fi

# ──────────────────────────────────────────────────────────────
printf "\n"
if [[ ${#ERRORS[@]} -eq 0 ]]; then
  printf "${BOLD}"
  printf '╔══════════════════════════════════════════════╗\n'
  printf '║  安裝完成                                    ║\n'
  printf '╚══════════════════════════════════════════════╝\n'
  printf "${NC}\n"
  printf "  啟動：${BOLD}pnpm dev${NC}\n\n"
else
  printf "${RED}${BOLD}"
  printf '╔══════════════════════════════════════════════╗\n'
  printf '║  安裝未完成，請修正以下問題後重新執行        ║\n'
  printf '╚══════════════════════════════════════════════╝\n'
  printf "${NC}\n"
  for e in "${ERRORS[@]}"; do
    printf "  ${RED}✗${NC}  %s\n" "$e"
  done
  printf "\n"
  exit 1
fi
