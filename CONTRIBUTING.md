# Contributing to RealNote

**[繁體中文](CONTRIBUTING.zh-TW.md)** | English

Thank you for your interest in contributing! This document covers how to set up a development environment, the project structure, and the workflow for submitting changes.

---

## Development Setup

```bash
git clone https://github.com/nt-nerdtechnic/RealNote.git
cd RealNote
bash install.sh   # install all dependencies
pnpm dev          # start in dev mode (hot-reload frontend)
```

### Running the backend separately

```bash
pnpm run backend:dev   # FastAPI on a fixed port (useful for debugging)
```

### Type-checking

```bash
pnpm run typecheck     # TypeScript (Electron main + Vue renderer)
```

---

## Project Structure

```
src/               Electron + Vue (TypeScript)
  main/            Electron main process
  preload/         contextBridge IPC
  renderer/src/    Vue 3 app
backend/
  meeting_minutes_backend/   Python FastAPI backend + all ASR/LLM logic
data/
  settings.example.json      copy → settings.json and edit
docs/
  development-status.md      architecture reference
```

See [docs/development-status.md](docs/development-status.md) for a full architecture description and key design decisions.

---

## How to Contribute

1. **Open an issue first** for non-trivial changes — describe what you want to fix or add and why.
2. Fork the repository and create a branch: `git checkout -b feat/your-feature`
3. Make your changes, keeping diffs focused and minimal.
4. Run `pnpm run typecheck` to verify TypeScript is clean.
5. Test manually: start the app with `pnpm dev` and exercise the changed code path.
6. Open a pull request against `main` with a clear description of what changed and why.

---

## Code Style

- **Python**: follow existing style (no formatter enforced yet); keep functions short and focused.
- **TypeScript / Vue**: match the surrounding code style; no additional linter config required.
- **Commits**: use conventional prefixes — `feat:`, `fix:`, `chore:`, `docs:`.
- **Comments**: write comments only when the *why* is non-obvious. Avoid restating what the code does.

---

## Sensitive Files

The following are **gitignored** and must never be committed:

| File | Contains |
|------|---------|
| `data/settings.json` | user settings, possibly API keys |
| `data/output/` | recordings and transcripts |
| `data/glossary.txt` | user-specific terminology |
| `.env` | environment variables |

Run [gitleaks](https://github.com/gitleaks/gitleaks) before opening a PR if you are unsure.

---

## Reporting Bugs

Please include:
- macOS version and chip (Apple Silicon / Intel)
- Steps to reproduce
- What you expected vs. what happened
- Relevant lines from the event log in the app (the right-hand panel)

---

## License

By contributing you agree that your changes will be licensed under the [MIT License](LICENSE).
