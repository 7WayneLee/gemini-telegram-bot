# Orca 編排計畫：Gemini Telegram Bot

> Commander: Opus 5 ／ Worker: Sol 5.6
> 搭配 `gemini-telegram-bot-prompt.md` 使用——該檔為規格來源，本檔為執行編排。
> 任務 ID 與依賴關係需轉譯為 AI Brain 的 DAG 任務格式。

---

## 一、專案不變量（注入 `CLAUDE.md`，每個 Sol 任務都必須攜帶）

以下規則違反任何一條即為任務失敗，不接受「已完成但略作調整」：

### 安全與環境

1. **禁止對真實 cookie 發出 live 請求。** 開發期所有測試一律 mock `GeminiClient`。需要真帳號的驗證標記為 human gate，由人工執行。違反此條會作廢正式環境的 session。
2. **禁止啟動第二個 `GeminiClient` 實例。** 全程單例。若程式碼中出現第二處 `GeminiClient(...)` 建構（測試 mock 除外），視為架構違規。
3. **禁止將 cookie 值寫入 log、測試 fixture、commit、或任何 `docs/` 產出。** 測試用假值一律為 `FAKE_1PSID_FOR_TEST` 形式的顯性假字串。
4. **禁止在正式主機 `movie-nas` 上執行任何指令。** 該主機同時運行 Jellyfin、qBittorrent 等既有服務，一次誤下的 docker 指令即可造成媒體服務中斷。Sol 只在開發環境寫碼與跑 mock 測試；所有部署、重啟、docker 操作一律為 HUMAN gate。
5. 禁止引入 webhook、多 worker、多 replica 設計。

### 實作紀律

6. **禁止憑記憶推測上游 API。** 所有 `gemini_webapi` 的類別名稱、方法簽章、屬性欄位，一律引用 `docs/upstream-api-contract.md`。合約未涵蓋的，回報 Commander 補做偵察，不得自行猜測。
7. **禁止硬編碼模型清單。** 模型必須來自 `client.list_models()` 動態取得。上游已棄用 `Model` enum 並移除 `Model.from_name` / `Model.from_dict`。
8. 每個任務單一 commit，commit message 用英文，且只能觸碰任務宣告的檔案範圍。
9. 回報完成時必須附上 DoD 指令的**實際輸出**，不接受「測試已通過」的文字聲明。

---

## 二、角色分工

| | Commander (Opus 5) | Worker (Sol 5.6) |
|---|---|---|
| 職責 | DAG 維護、合約審查、phase gate 驗收、失敗診斷、架構決策 | 實作、測試撰寫、機械性重構 |
| 不得做 | 直接寫實作程式碼 | 做架構決策、變更不變量、修改 DoD 定義 |
| 需 Commander 親自判斷的決策點 | egress 雙路徑的取捨、錯誤分類的邊界、切段器的降級策略、singleton 生命週期 | — |

**配額策略**：Opus 只在 phase gate 與失敗診斷時介入。單一任務內的迭代（測試失敗 → 修正 → 重跑）由 Sol 自行完成，連續失敗 3 次才升級給 Commander。Commander 每次介入前先讀 `git diff`，不要求 Sol 重述已在程式碼裡的內容。

---

## 三、任務 DAG

`SOL` = Worker 執行 ／ `OPUS` = Commander 親自執行 ／ `HUMAN` = 阻塞等待人工

### Phase 0 — 上游偵察（不可跳過）

| ID | Owner | 依賴 | 產出 | DoD（可執行指令） |
|---|---|---|---|---|
| T0.1 | SOL | — | `scripts/probe_upstream.py` | `python scripts/probe_upstream.py --json > docs/upstream-api.json` 退出碼 0，且 JSON 含 exceptions / ModelOutput 欄位 / ChatSession 簽章 三個 section |
| T0.2 | OPUS | T0.1 | `docs/upstream-api-contract.md` | Commander **親自執行** probe，將輸出與合約文件逐項比對；不採信 Sol 的摘要 |

**T0.1 規格**：以 `inspect` / `dir()` / `typing.get_type_hints()` 反射 `gemini_webapi`，輸出：

- 所有 exception 類別名稱與繼承階層
- `ModelOutput` 的全部屬性與型別（特別確認 `text` / `text_delta` / `thoughts` / `images` / `videos` / `media` / `candidates` / `deep_research_document`）
- `Image` / `WebImage` / `GeneratedImage` 的屬性與 `save()` 簽章
- `GeminiClient` 與 `ChatSession` 的公開方法簽章
- `ChatSession.metadata` 的實際型別（決定 SQLite 序列化方式）
- 已安裝版本號（合約需標註版本，日後升級時重跑比對）

此任務**不得**參考 README 撰寫合約，必須由執行結果產生。

### Phase 1 — 純邏輯層（無外部依賴，可平行）

| ID | Owner | 依賴 | 產出 | DoD |
|---|---|---|---|---|
| T1.1 | SOL | — | `config.py`, `.env.example` | `pytest tests/test_config.py -q` |
| T1.2 | SOL | T0.2 | `storage/db.py`, `storage/models.py` | `pytest tests/test_storage.py -q` 含 migration 冪等性測試 |
| T1.3 | SOL | — | `telegram/rendering.py` | `pytest tests/test_rendering.py -q --cov=src/gemini_tg_bot/telegram/rendering --cov-fail-under=90` |
| T1.4 | SOL | — | `queue.py` | `pytest tests/test_queue.py -q` |

**T1.3 為本專案最適合 Worker 的任務**：純函數、零外部依賴、可完整測試。必測案例：巢狀格式、未閉合 code fence、超過 4096 字元且 code block 跨段、LaTeX 片段、Telegram 不支援的標籤降級。

### Phase 2 — 整合層

| ID | Owner | 依賴 | 產出 | DoD |
|---|---|---|---|---|
| T2.1 | SOL | T0.2 | `gemini/errors.py` | `pytest tests/test_errors.py -q`，錯誤分類需覆蓋 Auth / RateLimit / Transient / Fatal 四類 |
| T2.2 | SOL | T0.2, T1.1, T2.1 | `gemini/service.py` | `pytest tests/test_service.py -q`（全 mock），含 DEGRADED 狀態轉換測試 |
| T2.3 | SOL | T1.2, T2.2 | `gemini/sessions.py` | `pytest tests/test_sessions.py -q`，含「重啟後還原 metadata」測試 |
| T2.4 | SOL | T1.1, T1.2 | `telegram/auth.py` | `pytest tests/test_auth.py -q`，**必須含白名單為空時全數拒絕的測試** |
| T2.5 | SOL | T1.3, T2.2, T2.4 | `telegram/handlers.py`, `__main__.py` | `pytest tests/test_handlers.py -q && python -m gemini_tg_bot --dry-run` 退出碼 0 |

**`--dry-run` 模式規格**（T2.5 一併實作）：以假 Gemini client 與假 Telegram transport 完整啟動一次，跑通「收訊息 → 白名單 → 佇列 → service → 渲染 → 送出」全鏈路後正常結束。這是讓整合層能被 agent 機械驗證、不占用 human gate 的關鍵設施。

### Gate G1 — 連通性實測（HUMAN）

| ID | Owner | 依賴 | 內容 |
|---|---|---|---|
| G1 | HUMAN | T2.5 | 依 SSH SOCKS 流程取得 cookie → 部署 → 白名單使用者發送文字收到回覆；非白名單被拒絕且留下 log |

此 gate 未通過前，Phase 3 全部阻塞。DAG 排程器需將 HUMAN 節點視為外部阻塞，**不得由 agent 自行標記完成**。

### Phase 3 — 功能層

| ID | Owner | 依賴 | 產出 | DoD |
|---|---|---|---|---|
| T3.1 | SOL | G1 | `telegram/streaming.py` | `pytest tests/test_streaming.py -q`，須測節流（1.5s / 200 字元取先到者）與 `RetryAfter` 處理 |
| T3.2a | HUMAN | G1 | `docs/egress-findings.md` | 人工實測：Gemini 回傳的 `Image.url` 能否被 Telegram 伺服器直接抓取（無認證） |
| T3.2b | SOL | T3.2a | `telegram/media.py` | `pytest tests/test_media.py -q`，**兩條路徑都要實作**：URL 直傳（零 egress）與下載後上傳（計量） |
| T3.3 | SOL | T1.2, T2.2 | `gemini/research.py` | `pytest tests/test_research.py -q`，含「重啟後恢復 running 任務輪詢」測試 |
| T3.4 | SOL | T2.2, T2.4 | admin 指令 | `pytest tests/test_admin.py -q`，`/setcookie` 需測「熱重啟 client 且不重啟 process」與「接收後刪除訊息」 |

**T3.2a 為何必須是 HUMAN**：URL 是否可公開存取只能對真實回應驗證，而這需要 live 呼叫。Sol 無法在不違反不變量 1 的前提下完成。

### Gate G2 — 韌性演練（HUMAN）

| ID | Owner | 依賴 | 內容 |
|---|---|---|---|
| G2 | HUMAN | T3.4 | 填入無效 cookie → 確認進入 DEGRADED 並推播管理員 → `/setcookie` 熱更新 → 服務恢復，全程 process 未重啟 |

### Phase 4 — 交付

| ID | Owner | 依賴 | 產出 | DoD |
|---|---|---|---|---|
| T4.1 | SOL | G2 | `deploy/Dockerfile`, `docker-compose.yml`, systemd unit | `docker compose -f deploy/docker-compose.yml build` 退出碼 0 |
| T4.2 | OPUS | T4.1 | `README.md` | Commander 親自撰寫：SSH SOCKS cookie 流程、成本與 egress 章節、已知風險（ToS／封號／AGPL-3.0／singleton 擴展限制） |

---

## 四、驗證協定（對應「回報與實際程式碼不符」）

Commander 在每個任務收尾時執行，順序固定：

1. `git log --oneline -1` — 確認確實有 commit
2. `git show --name-only HEAD` — 確認觸碰檔案落在任務宣告範圍內，無夾帶
3. `git diff HEAD~1 --stat` — 確認改動規模與任務相稱
4. **親自執行該任務的 DoD 指令**，看退出碼
5. `grep -rn "GeminiClient(" src/ --include=*.py | grep -v test` — 確認仍為單例
6. `grep -rniE "1PSID|1PSIDTS" src/ tests/ docs/ | grep -v FAKE_` — 確認無真實 cookie 洩漏

第 4 步是核心：**Commander 不讀完成報告來判定通過，只看自己執行的結果。** Sol 的報告僅用於失敗時的診斷輸入。

任一步失敗 → 任務退回，附上失敗的指令輸出（不附解法，讓 Sol 自行診斷，保留 Opus 配額）。

---

## 五、Human Gate 清單（需你親自執行，agent 不可代勞）

| 項目 | 時機 | 原因 |
|---|---|---|
| VM 資源實測（`free -h` / `docker stats` / `swapon`） | T4.1 前 | 決定 `mem_limit` 實際數值；agent 不得存取此主機 |
| SSH SOCKS 取得 cookie（`ssh -D 1080 -C -q -N movie-nas`） | G1 前，及每次 cookie 失效 | 需瀏覽器互動與 Google 二階段驗證 |
| M1 連通性實測 | G1 | 需 live 呼叫 |
| egress URL 可抓取性實測 | T3.2a | 需真實回應 |
| M5 cookie 失效演練 | G2 | 需刻意破壞真實憑證 |
| **所有在 `movie-nas` 上的操作**（部署、重啟、docker 指令） | 全程 | 該主機同時運行 Jellyfin 與 qBittorrent，誤操作會中斷媒體服務 |

---

## 六、啟動指令

給 Commander 的初始指示：

```
讀取 gemini-telegram-bot-prompt.md（規格）與本檔（編排）。
將第三節的 DAG 轉為 AI Brain 任務，第一節的不變量寫入 CLAUDE.md。
從 T0.1 開始，不得跳過 Phase 0。
每個 phase 結束時執行第四節的驗證協定並回報結果，等待我確認後再進入下一 phase。
遇到 HUMAN 節點時停止排程並明確告知我需要做什麼。
```
