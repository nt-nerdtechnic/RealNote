# 貢獻指南

**繁體中文** | [English](CONTRIBUTING.md)

感謝你對 RealNote 的興趣！本文件說明如何建立開發環境、專案結構，以及提交變更的流程。

---

## 開發環境設置

```bash
git clone https://github.com/nt-nerdtechnic/RealNote.git
cd RealNote
bash install.sh   # 安裝所有依賴
pnpm dev          # 啟動開發模式（前端熱重載）
```

### 獨立啟動後端（方便除錯）

```bash
pnpm run backend:dev   # FastAPI 固定 port 啟動
```

### 型別檢查

```bash
pnpm run typecheck     # TypeScript（Electron main + Vue renderer）
```

---

## 專案結構

```
src/               Electron + Vue（TypeScript）
  main/            Electron main process
  preload/         contextBridge IPC
  renderer/src/    Vue 3 應用
backend/
  meeting_minutes_backend/   Python FastAPI backend + 所有 ASR/LLM 邏輯
data/
  settings.example.json      複製為 settings.json 後填入設定
docs/
  development-status.md      架構技術參考文件
```

完整架構說明請參考 [docs/development-status.md](docs/development-status.md)。

---

## 貢獻流程

1. **先開 Issue**：非瑣碎的變更請先說明你要修什麼、為什麼。
2. Fork 並建立分支：`git checkout -b feat/your-feature`
3. 做最小化的修改，保持 diff 清晰。
4. 執行 `pnpm run typecheck` 確認 TypeScript 無錯誤。
5. 手動測試：執行 `pnpm dev` 驗證受影響的程式碼路徑正常。
6. 對 `main` 開 Pull Request，清楚說明修改內容與原因。

---

## 程式碼風格

- **Python**：遵循現有風格，函式保持短小精悍。
- **TypeScript / Vue**：配合周圍的程式碼風格。
- **Commit 訊息**：使用慣例前綴 — `feat:`、`fix:`、`chore:`、`docs:`。
- **註解**：只在「為什麼」不明顯時寫，不要描述程式碼本身在做什麼。

---

## 敏感檔案

以下為 **gitignored**，絕對不能 commit：

| 檔案 | 內容 |
|------|------|
| `data/settings.json` | 使用者設定，可能含 API key |
| `data/output/` | 錄音與逐字稿 |
| `data/glossary.txt` | 個人術語表 |
| `.env` | 環境變數 |

不確定時，開 PR 前先用 [gitleaks](https://github.com/gitleaks/gitleaks) 掃描。

---

## 回報 Bug

請包含：
- macOS 版本與晶片（Apple Silicon / Intel）
- 重現步驟
- 預期行為與實際行為
- App 右側事件 log 的相關訊息

---

## 授權

提交變更即代表你同意你的貢獻以 [MIT License](LICENSE) 授權釋出。
