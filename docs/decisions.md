# 架構決策紀錄（Commander 裁定）

供 T4.2 README 引用。每項均為 `docs/orchestration-plan.md` §2 指定由 Commander 親自判斷的決策點。

---

## D1 — Telegram 層採用 `python-telegram-bot >= 21`（實裝 22.8）

**選項**：`python-telegram-bot >= 21` vs `aiogram 3.x`（spec 要求擇一並說明理由）。

**裁定：python-telegram-bot。**

理由：

1. **錯誤處理矩陣直接對應**。spec 的錯誤矩陣點名 `RetryAfter`，這正是
   `telegram.error.RetryAfter` 的類別名稱（aiogram 對應類別為 `TelegramRetryAfter`）。
   採用 PTB 可讓錯誤處理實作與 spec 文字一對一對應，減少轉譯誤差。
2. **單一 event loop 契合不變量 5**。`Application.run_polling()` 是單 process、
   單 event loop 的 long polling 模型，與「GeminiClient 單例」的約束天然相容，
   不需額外抑制多 worker 行為。
3. **內建 JobQueue**。T3.3 的 Deep Research 背景輪詢與重啟恢復可直接使用
   `Application.job_queue`，不必另外引入排程器。
4. **內建 rate limiter**。`AIORateLimiter` 提供 flood control 的預設處理，
   與 T3.1 streaming 節流需求互補。

**代價**：PTB 的 `Application` 生命週期較 aiogram 重，且 22.x 對 `ExtBot` 的型別標註
較嚴格。此代價可接受。

---

## D2 — 共用相依性由 Commander 統一在 `pyproject.toml` 宣告

Phase 1 的多個 Worker 任務在同一 worktree 平行執行。若各自增修 `pyproject.toml`
會產生寫入競爭與 lock 衝突。因此所有跨任務共用相依性（`python-telegram-bot`、
`pydantic-settings`、`aiosqlite`、`pytest` 系列）由 Commander 於 Phase 1 開始前
一次宣告完成，**Worker 任務不得修改 `pyproject.toml` 或 `uv.lock`**。

## D3 — `research_tasks` 需增設 `plan_json` 欄位

見 `docs/upstream-api-contract.md` §6。`wait_for_deep_research()` 需要完整的
`DeepResearchPlan` 物件，spec 原 schema 的 `research_id` / `cid` 不足以復原。
`DeepResearchPlan` 為 pydantic BaseModel，以 `model_dump_json()` 持久化。
此為對 spec schema 的必要增補，已核可。
