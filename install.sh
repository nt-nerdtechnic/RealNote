#!/usr/bin/env bash
# RealNote — Installer
# Usage: bash install.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── Colors ───────────────────────────────────────────────────
BOLD='\033[1m'
DIM='\033[2m'
RED='\033[91m'
GRN='\033[92m'
YLW='\033[93m'
NC='\033[0m'

# ─── Helpers ──────────────────────────────────────────────────
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

# ─── Header ───────────────────────────────────────────────────
clear
printf "${BOLD}"
printf '╔══════════════════════════════════════════════╗\n'
printf '║  RealNote  —  Installer                      ║\n'
printf '╚══════════════════════════════════════════════╝\n'
printf "${NC}\n"

# ──────────────────────────────────────────────────────────────
section "System"

# macOS
macos_ver=$(sw_vers -productVersion 2>/dev/null || echo "unknown")
ok "macOS $macos_ver"

# Disk space (at least 5 GB)
free_gb=$(df -g "$HOME" 2>/dev/null | awk 'NR==2{print $4}')
if [[ -n "$free_gb" && "$free_gb" -ge 5 ]]; then
  ok "Disk space OK (${free_gb} GB free)"
else
  warn "Low disk space (${free_gb:-?} GB); 5 GB+ recommended"
fi

# Xcode CLT
if xcode-select -p &>/dev/null; then
  ok "Xcode Command Line Tools"
else
  fail "Xcode CLT missing → run: xcode-select --install"
fi

# Homebrew
if command -v brew &>/dev/null; then
  ok "Homebrew $(brew --version 2>/dev/null | head -1 | awk '{print $2}')"
else
  fail "Homebrew not found → https://brew.sh"
fi

# ──────────────────────────────────────────────────────────────
section "System Packages"

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
      fail "$desc installation failed"
    fi
  fi
}

_brew_pkg "ffmpeg"        "ffmpeg (audio processing)"
_brew_pkg "blackhole-2ch" "BlackHole 2ch (virtual audio)" cask

# ──────────────────────────────────────────────────────────────
section "Audio Devices"

if system_profiler SPAudioDataType 2>/dev/null | grep -qi "BlackHole"; then
  ok "BlackHole 2ch detected in audio devices"
else
  warn "BlackHole not detected (installed but may need a reboot)"
  warn "If still missing after reboot, open Audio MIDI Setup to verify"
fi

# Check for Multi-Output Device (BlackHole + speakers)
if system_profiler SPAudioDataType 2>/dev/null | grep -qi "Multi-Output\|Multi Output"; then
  ok "Multi-Output Device configured"
else
  info "Recommended: create a Multi-Output Device in Audio MIDI Setup"
  info "(BlackHole 2ch + Built-in Output) to hear audio while recording"
  info "Path: Applications → Utilities → Audio MIDI Setup"
fi

# ──────────────────────────────────────────────────────────────
section "Node.js / pnpm"

# nvm may not be loaded in non-interactive shells — try sourcing manually
if ! command -v node &>/dev/null; then
  [[ -s "$HOME/.nvm/nvm.sh" ]] && source "$HOME/.nvm/nvm.sh" 2>/dev/null || true
fi

if command -v node &>/dev/null; then
  ok "Node.js $(node --version)"
else
  fail "Node.js not found → install via nvm: https://github.com/nvm-sh/nvm"
fi

if command -v pnpm &>/dev/null; then
  ok "pnpm $(pnpm --version)"
else
  info "pnpm not found → installing..."
  if npm install -g pnpm &>/dev/null 2>&1; then
    ok "pnpm $(pnpm --version)"
  else
    fail "pnpm installation failed (npm install -g pnpm)"
  fi
fi

# ──────────────────────────────────────────────────────────────
section "Python / uv"

# Find Python 3.10+
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
[[ -z "$PY3" ]] && fail "Python 3.10+ not found (recommended: brew install python@3.12)"

if command -v uv &>/dev/null; then
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
else
  info "uv not found → installing..."
  if brew install uv &>/dev/null 2>&1; then
    ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
  else
    fail "uv installation failed (brew install uv)"
  fi
fi

# ──────────────────────────────────────────────────────────────
section "Frontend Dependencies"

cd "$SCRIPT_DIR"
if [[ -d node_modules && -f pnpm-lock.yaml ]]; then
  ok "node_modules already exists"
else
  if run_spinner "pnpm install" pnpm install; then
    :
  else
    fail "pnpm install failed"
  fi
fi

# ──────────────────────────────────────────────────────────────
section "Backend Dependencies (Python venv)"

if [[ -d "$SCRIPT_DIR/backend/.venv" ]]; then
  ok "backend/.venv already exists"
else
  if run_spinner "uv sync (create .venv + install packages)" \
       uv sync --project "$SCRIPT_DIR/backend"; then
    :
  else
    fail "uv sync failed"
  fi
fi

# Verify key packages
VENV_PY="$SCRIPT_DIR/backend/.venv/bin/python3"
if [[ -x "$VENV_PY" ]]; then
  for pkg in mlx_whisper faster_whisper silero_vad; do
    if "$VENV_PY" -c "import $pkg" &>/dev/null 2>&1; then
      ok "$pkg"
    else
      fail "$pkg not installed (run: uv sync --project backend)"
    fi
  done
else
  warn "backend/.venv/bin/python3 not found, skipping package check"
fi

# ──────────────────────────────────────────────────────────────
section "ASR Models (MLX Whisper)"

# Resolve HuggingFace cache path
HF_CACHE=$("$VENV_PY" -c \
  "from huggingface_hub import constants; print(constants.HF_HUB_CACHE)" \
  2>/dev/null || echo "$HOME/.cache/huggingface/hub")

MLX_MODEL="mlx-community/whisper-medium-mlx-q4"
MLX_CACHE_DIR="$HF_CACHE/models--mlx-community--whisper-medium-mlx-q4"

if [[ -d "$MLX_CACHE_DIR" ]]; then
  ok "whisper-medium-mlx-q4 (cached)"
else
  if run_spinner "Downloading $MLX_MODEL (~400 MB)" \
       "$VENV_PY" -c "
from huggingface_hub import snapshot_download
snapshot_download('$MLX_MODEL')
"; then
    :
  else
    fail "Model download failed — check your internet connection and retry"
  fi
fi

# Tiny preview model
MLX_TINY_CACHE="$HF_CACHE/models--mlx-community--whisper-tiny-mlx-q4"
if [[ -d "$MLX_TINY_CACHE" ]]; then
  ok "whisper-tiny-mlx-q4 (preview model, cached)"
else
  if run_spinner "Downloading whisper-tiny-mlx-q4 (~40 MB)" \
       "$VENV_PY" -c "
from huggingface_hub import snapshot_download
snapshot_download('mlx-community/whisper-tiny-mlx-q4')
"; then
    :
  else
    warn "tiny model download failed (optional, can retry later)"
  fi
fi

# ──────────────────────────────────────────────────────────────
section "Configuration"

if [[ -f "$SCRIPT_DIR/.env" ]]; then
  ok ".env already exists"
else
  if [[ -f "$SCRIPT_DIR/.env.example" ]]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    ok ".env created from .env.example"
    warn "For LLM summary, set OPENAI_API_KEY in .env (optional)"
  else
    warn ".env.example not found — create .env manually"
  fi
fi

# ──────────────────────────────────────────────────────────────
printf "\n"
if [[ ${#ERRORS[@]} -eq 0 ]]; then
  printf "${BOLD}"
  printf '╔══════════════════════════════════════════════╗\n'
  printf '║  Installation complete                        ║\n'
  printf '╚══════════════════════════════════════════════╝\n'
  printf "${NC}\n"
  printf "  Start the app: ${BOLD}pnpm dev${NC}\n\n"
else
  printf "${RED}${BOLD}"
  printf '╔══════════════════════════════════════════════╗\n'
  printf '║  Installation incomplete — fix issues below  ║\n'
  printf '╚══════════════════════════════════════════════╝\n'
  printf "${NC}\n"
  for e in "${ERRORS[@]}"; do
    printf "  ${RED}✗${NC}  %s\n" "$e"
  done
  printf "\n"
  exit 1
fi
