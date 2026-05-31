#!/usr/bin/env bash
# RealNote — Launcher
# Usage: bash start.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── Colors (same as install.sh) ──────────────────────────────
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

# ─── Header ───────────────────────────────────────────────────
printf "${BOLD}"
printf '╔══════════════════════════════════════════════╗\n'
printf '║  RealNote  —  Launching                      ║\n'
printf '╚══════════════════════════════════════════════╝\n'
printf "${NC}\n"

FATAL=0

# ─── Pre-flight checks ────────────────────────────────────────

# backend venv
if [[ -x "$SCRIPT_DIR/backend/.venv/bin/python3" ]]; then
  ok "backend/.venv"
else
  fail "backend/.venv not found → run: bash install.sh"
  FATAL=1
fi

# node_modules
if [[ -d "$SCRIPT_DIR/node_modules" ]]; then
  ok "node_modules"
else
  fail "node_modules not found → run: bash install.sh"
  FATAL=1
fi

# .env
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  ok ".env"
  if ! grep -q "OPENAI_API_KEY=.\+" "$SCRIPT_DIR/.env" 2>/dev/null; then
    warn "OPENAI_API_KEY not set (LLM summary will be disabled)"
  fi
else
  warn ".env not found (LLM summary will be disabled)"
fi

# Abort on fatal errors
if [[ "$FATAL" -eq 1 ]]; then
  printf "\n${RED}${BOLD}Please run: bash install.sh${NC}\n\n"
  exit 1
fi

# ─── Audio device checks ──────────────────────────────────────
AUDIO_INFO=$(system_profiler SPAudioDataType 2>/dev/null)

HAS_BLACKHOLE=0; HAS_MULTIOUT=0; HAS_AGGREGATE=0
grep -qi "blackhole" <<< "$AUDIO_INFO"                      && HAS_BLACKHOLE=1
grep -qiE "multi.?output|多重輸出" <<< "$AUDIO_INFO"         && HAS_MULTIOUT=1
grep -qiE "aggregate|聚集" <<< "$AUDIO_INFO"                 && HAS_AGGREGATE=1
# User may have renamed the aggregate device; Input Channels >= 3 also counts
if [[ "$HAS_AGGREGATE" -eq 0 ]]; then
  grep -qE "Input Channels: [3-9]" <<< "$AUDIO_INFO"         && HAS_AGGREGATE=1
fi

AUDIO_WARN=0

if [[ "$HAS_BLACKHOLE" -eq 0 ]]; then
  printf "  ${RED}[missing]${NC} BlackHole 2ch virtual audio device\n"
  printf "    ${DIM}brew install --cask blackhole-2ch  (reboot required after install)${NC}\n"
  AUDIO_WARN=1
else
  ok "BlackHole 2ch virtual audio device"
fi

if [[ "$HAS_MULTIOUT" -eq 0 ]]; then
  printf "  ${YLW}[missing]${NC} Multi-Output Device\n"
  printf "    ${DIM}Audio MIDI Setup → + → Create Multi-Output Device → check speakers + BlackHole 2ch${NC}\n"
  AUDIO_WARN=1
else
  ok "Multi-Output Device"
fi

if [[ "$HAS_AGGREGATE" -eq 0 ]]; then
  printf "  ${YLW}[missing]${NC} Aggregate Device (required for mic recording)\n"
  printf "    ${DIM}Audio MIDI Setup → + → Create Aggregate Device → check BlackHole 2ch + microphone${NC}\n"
  AUDIO_WARN=1
else
  ok "Aggregate Device"
fi

if [[ "$AUDIO_WARN" -eq 1 ]]; then
  printf "\n"
  read -rp "  Audio device setup is incomplete. Continue anyway? (y/N) " _reply
  [[ "$_reply" =~ ^[Yy]$ ]] || exit 0
fi

# ─── Launch ───────────────────────────────────────────────────
printf "\n${BOLD}▸ Launching${NC}\n"
printf "${DIM}──────────────────────────────────────────────────${NC}\n"
info "pnpm dev"
printf "\n"

cd "$SCRIPT_DIR"
exec pnpm dev
