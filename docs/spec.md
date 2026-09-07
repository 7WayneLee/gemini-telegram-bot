# Claude Code Prompt：Gemini Web API Telegram Bot on GCP VM

> 使用方式：在空目錄執行 `claude`，將以下全部內容貼入。
> 建議先建立 git repo，讓 Claude Code 可以分階段 commit。

---

## 專案目標

建立一個部署在 GCP VM 上的 Telegram bot，透過 [`gemini-webapi`](https://github.com/HanaokaYuzu/Gemini-API)（逆向工程的 Google Gemini web app Python wrapper）提供 Gemini 對話能力。

這不是官方 Gemini API。認證方式是瀏覽器 cookie（`__Secure-1PSID` / `__Secure-1PSIDTS`），cookie 會過期、會被 Google 主動作廢。**整個架構的核心挑戰是 cookie 生命週期管理，不是 bot 功能本身。** 請以此為設計優先序。

---

## 硬性技術約束

1. **Python 3.11+**（`gemini_webapi` 的最低需求）。Ubuntu 22.04 內建 3.10，容器請用 `python:3.12-slim`。
2. **全程 asyncio**。`gemini_webapi` 是 async-only，Telegram 層請用 `python-telegram-bot >= 21`（async 原生）或 `aiogram 3.x`，擇一並在 README 說明選擇理由。
3. **`GeminiClient` 必須是全程唯一實例（singleton）**。
   - `auto_refresh=True` 會在背景輪替 `__Secure-1PSIDTS`。多個 process 共用同一組 cookie 會互相作廢，導致帳號反覆登出。
   - 因此：**單一 process、單一 event loop、不得開多 worker / 多 replica**。
   - cookie 持久化路徑由環境變數 `GEMINI_COOKIE_PATH` 指定，掛載到 volume。
4. **Long polling，不使用 webhook**。不開任何 inbound port。
5. **預設拒絕所有使用者**。白名單機制為 P0 功能，不是後續迭代項目。

---

## 安全前提（請在 README 顯著位置說明）

- bot 持有的是一個**真實 Google 帳號的完整 web session**。若該帳號啟用了 Gemini extensions，任何能與 bot 對話的人都能透過 `@Gmail` 讀取信箱內容。
- 因此白名單預設為空，`ALLOWED_USER_IDS` 未設定時 bot 拒絕所有訊息（只回覆一則說明），並將未授權嘗試記錄到 log。
- cookie **不得**寫入 Docker image、不得 commit 進 git。`.env` 權限設為 `600`，`.gitignore` 必須涵蓋 `.env`、`data/`、`cookies/`。
- README 需明確建議：使用獨立的次要 Google 帳號，勿綁定主帳號（此為逆向 API，違反 Google ToS，有封號風險）。
- `gemini-webapi` 授權為 AGPL-3.0，README 中註記此事實。

---

## 成本與資源約束（P0 設計條件，非優化項）

VM 位於美國區、月預算上限 US$10、可能與其他服務共用機器。這些不是「有空再處理」的效能議題，而是會直接產生帳單的設計條件。

### Egress 是主要成本來源

免費層每月僅 1GB 北美對外流量，超出約 US$0.12/GB。而媒體處理的天真作法會讓同一份資料走兩次網路：Gemini 回傳圖片 → VM 下載 → VM 上傳給 Telegram。

**必須實作的優化**：Telegram 的 `sendPhoto` / `sendDocument` 接受 URL 字串，由 Telegram 伺服器自行抓取來源，完全不經過 VM。

- 實作時**先驗證** Gemini 回傳的 `Image.url` 是否可被外部無認證存取。
- 可以 → 直接傳 URL 給 Telegram，VM egress 為零。
- 不行（需要 cookie header 或 URL 帶時效簽章）→ 才退回「下載後上傳」，並在 log 記錄此路徑的流量。
- 這兩條路徑都要實作，用設定開關或自動偵測切換，不要只做其中一條。

### 其他資源限制

| 項目 | 設定 | 理由 |
|---|---|---|
| `MAX_CONCURRENCY` | 預設 `1` | 記憶體受限，且單一 Gemini 帳號本來就有配額上限，併發無實益 |
| 影片 / 音訊生成 | **預設停用**，需明確開啟環境變數 | 單支可達數十 MB，直接吃掉整月免費 egress |
| Docker 記憶體上限 | `mem_limit: 400m` | 避免與同機服務競爭時觸發 OOM killer 波及其他服務 |
| 媒體暫存 | 用完即刪 + 定時清理 | 磁碟空間與其他服務共用 |
| 日誌輪替 | `max-size: 5m`, `max-file: 2` | 同上 |
| 使用者上傳 | 上限 20MB（Telegram Bot API 限制） | 提前拒絕，不要下載到一半才失敗 |

`/status` 指令需回報本月累計 egress 估算值（累加所有經 VM 中轉的媒體位元組數），讓流量消耗可見而非事後從帳單發現。

---

## 架構分層

```
Telegram Long Polling
        │
   ┌────▼─────────────────────────────┐
   │ Auth Middleware (白名單，預設拒絕)  │
   └────┬─────────────────────────────┘
        │
   ┌────▼─────────────────────────────┐
   │ Command Router / Message Handler │
   └────┬─────────────────────────────┘
        │
   ┌────▼─────────────────────────────┐
   │ Request Queue                    │
   │  - 全域 Semaphore (預設併發 2)     │
   │  - 每使用者 rate limit (token bucket)│
   └────┬─────────────────────────────┘
        │
   ┌────▼─────────────────────────────┐
   │ GeminiService (singleton)        │
   │  - GeminiClient 生命週期          │
   │  - ChatSession registry           │
   │  - 健康狀態 / 錯誤分類             │
   └────┬─────────────────────────────┘
        │
   ┌────▼─────────────────────────────┐
   │ gemini_webapi.GeminiClient       │
   └──────────────────────────────────┘

旁路元件：
  - SQLite (aiosqlite)：session 對應、deep research task、使用統計
  - Cookie Store：檔案 + 檔案鎖，唯一寫入者
  - Admin Notifier：認證失效 / 嚴重錯誤主動推播給管理員
```

---

## 目錄結構

```
gemini-tg-bot/
├── src/gemini_tg_bot/
│   ├── __init__.py
│   ├── __main__.py          # 進入點：載入 config → 初始化 → 啟動 polling
│   ├── config.py            # pydantic-settings，從 .env 讀取並驗證
│   ├── gemini/
│   │   ├── service.py       # GeminiService：client 生命週期、init/reinit
│   │   ├── sessions.py      # ChatSession registry + metadata 持久化
│   │   ├── errors.py        # 錯誤分類：Auth / RateLimit / Transient / Fatal
│   │   └── research.py      # Deep Research 非同步任務管理
│   ├── telegram/
│   │   ├── auth.py          # 白名單 middleware
│   │   ├── handlers.py      # 指令與訊息 handler
│   │   ├── rendering.py     # Markdown → Telegram HTML、訊息切段
│   │   ├── streaming.py     # 節流的 editMessageText streaming
│   │   └── media.py         # 圖片 / 影片 / 音訊 / 檔案上下行
│   ├── storage/
│   │   ├── db.py            # aiosqlite 連線與 migration
│   │   └── models.py        # schema 定義與 DAO
│   └── queue.py             # Semaphore + per-user rate limiter
├── tests/
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── gemini-tg-bot.service   # systemd 替代方案
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md
```

---

## 資料模型（SQLite）

```sql
-- Telegram chat 對應到 Gemini 對話
CREATE TABLE chat_sessions (
    chat_id       INTEGER PRIMARY KEY,
    cid           TEXT,           -- Gemini conversation id
    metadata_json TEXT,           -- ChatSession.metadata 序列化
    model         TEXT,           -- 使用者選定的模型，NULL = 帳號預設
    gem_id        TEXT,
    temporary     INTEGER DEFAULT 0,
    updated_at    TEXT NOT NULL
);

-- Deep Research 背景任務
CREATE TABLE research_tasks (
    task_id     TEXT PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    cid         TEXT,
    research_id TEXT,
    prompt      TEXT NOT NULL,
    status      TEXT NOT NULL,   -- pending | running | done | failed | timeout
    result_path TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 使用量統計（供 /status 與後續配額觀察）
CREATE TABLE usage_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    command    TEXT,
    model      TEXT,
    ok         INTEGER NOT NULL,
    error_kind TEXT,
    latency_ms INTEGER,
    created_at TEXT NOT NULL
);
```

啟動時執行 migration；`research_tasks` 中狀態為 `running` 的任務需在啟動時恢復輪詢。

---

## 指令規格

### 一般使用者

| 指令 | 行為 |
|---|---|
| `/start`, `/help` | 說明可用指令 |
| `/new` | 結束目前 `ChatSession`，開新對話 |
| `/model` | 呼叫 `client.list_models()` 動態列出，用 inline keyboard 讓使用者選。**不得硬編碼模型清單**。 |
| `/gem` | `client.fetch_gems()` 後列出可用 gem，選定後套用於後續對話 |
| `/temp` | 切換 temporary mode（`temporary=True`，不寫入 Gemini 歷史） |
| `/img <prompt>` | 明確在 prompt 中要求「生成」圖片（README 指出：未明示 generate 時 Gemini 傾向回傳網路搜尋來的圖，而非 AI 生成圖） |
| `/research <topic>` | 送出 Deep Research 背景任務，立即回傳 task id |
| `/research_status` | 列出該 chat 的研究任務狀態 |
| `/status` | 目前模型、session cid、cookie 最後刷新時間、佇列深度、今日用量 |
| 純文字訊息 | 送入目前 `ChatSession`，streaming 回覆 |
| 圖片 / 文件 | 下載到暫存目錄 → 作為 `files=[...]` 參數；caption 作為 prompt，無 caption 則用預設提示語 |

### 管理員（`ADMIN_USER_ID`）

| 指令 | 行為 |
|---|---|
| `/setcookie` | 進入互動流程，接收新的 `__Secure-1PSID` / `__Secure-1PSIDTS`，寫入 cookie 檔並**熱重啟 GeminiClient，不需重啟 process**。接收後立即刪除該則 Telegram 訊息。 |
| `/allow <user_id>` | 加入白名單（寫入 DB，即時生效） |
| `/deny <user_id>` | 移出白名單 |
| `/health` | client 健康狀態、最近錯誤、DB 狀態 |

---

## 錯誤處理矩陣（P0，請完整實作）

| 情境 | 偵測 | 處置 |
|---|---|---|
| Cookie 失效 | `gemini_webapi` 拋出 `AuthError` | 進入 `DEGRADED` 狀態，停止接受新請求；主動推播管理員「認證失效，請 /setcookie」；使用者收到明確說明而非 stack trace |
| Gemini 端限流 | HTTP 429 / 特定錯誤訊息 | 指數退避重試最多 2 次；仍失敗則告知使用者稍後再試，記錄 `error_kind=rate_limit` |
| 暫時性網路錯誤 | `TimeoutError`、連線中斷 | 退避重試最多 2 次 |
| Telegram flood control | `RetryAfter` | 尊重 `retry_after` 秒數後重送，不得無視 |
| 回覆超過 4096 字元 | 送出前檢查 | 切段。**切點必須在段落邊界，且不得落在 code fence 內部**；跨段的 code block 需在每段補上開闔標記 |
| Markdown 解析失敗 | Telegram 回 `BadRequest: can't parse entities` | 自動降級為 `parse_mode=None` 純文字重送，並記錄原始內容供除錯 |
| Deep Research 逾時 | 超過 `timeout` 仍未 `done` | 任務標記 `timeout`，保留 cid 讓使用者可用 `/research_status` 再查 |
| 上傳檔案過大 | Telegram Bot API 下載上限 20MB | 提前拒絕並說明限制 |
| Process 重啟 | 啟動流程 | 從 SQLite 還原 chat session metadata 與 running 中的研究任務 |

---

## Rendering 層要求（實作重點）

Gemini 輸出標準 Markdown，Telegram 的 MarkdownV2 跳脫規則與其不相容，直接送出必然失敗。請實作：

1. **Markdown → Telegram HTML 轉換**：支援 `**bold**`、`*italic*`、`` `code` ``、code fence（含語言標記）、連結、清單。Telegram HTML 只支援有限標籤集（`b/i/u/s/code/pre/a/blockquote`），不支援的結構降級為純文字表現。
2. **LaTeX 處理**：Gemini 常輸出 `$...$` / `$$...$$`，Telegram 無法渲染。以 `<code>` 包裹原式，或轉為 Unicode 近似（擇一，在 README 說明）。
3. **切段器**：以 4000 字元為安全上限（保留 header 空間），優先在 `\n\n` 切、其次 `\n`、最後硬切。切段前先解析 code fence 位置以避免破壞。
4. **對應的單元測試**：至少涵蓋巢狀格式、未閉合 fence、超長 code block 三種情況。

## Streaming 層要求

- 先送出 placeholder 訊息（如「思考中…」），再用 `generate_content_stream` 的 `text_delta` 逐步 `editMessageText`。
- **節流**：每 1.5 秒或每累積 200 字元才 edit，取先到者。Telegram 對同一聊天室的編輯頻率有 flood control，無節流必定觸發。
- 串流期間不做 Markdown 解析（用純文字），**最後一次 edit 才套用完整 HTML 渲染**。這避免中途出現未閉合標籤導致解析錯誤。
- 若最終內容超過單則訊息上限，最後一次 edit 改為切段後多則送出，並修剪 placeholder。

---

## 部署產物

### Docker Compose（主要方案）

```yaml
services:
  bot:
    build: .
    restart: unless-stopped
    env_file: .env
    environment:
      GEMINI_COOKIE_PATH: /data/cookies
    volumes:
      - ./data/cookies:/data/cookies    # cookie 持久化，避免重建即失效
      - ./data/db:/data/db
      - ./data/tmp:/data/tmp            # 媒體暫存
    mem_limit: 400m                     # 與同機服務共存，避免 OOM 波及
    memswap_limit: 800m
    cpus: 0.5
    logging:
      driver: json-file
      options: { max-size: "5m", max-file: "2" }
```

- 容器內以非 root 使用者執行；確認 `/data/cookies` 對該使用者可寫。
- 不設定 `ports`（polling 不需 inbound）。
- `deploy/gemini-tg-bot.service` 提供 systemd + venv 的等價替代方案，供不使用 Docker 的情境。

### `.env.example`

需包含並附註解：`TELEGRAM_BOT_TOKEN`、`ADMIN_USER_ID`、`ALLOWED_USER_IDS`、`GEMINI_SECURE_1PSID`、`GEMINI_SECURE_1PSIDTS`、`GEMINI_COOKIE_PATH`、`GEMINI_PROXY`、`DEFAULT_MODEL`、`MAX_CONCURRENCY`、`USER_RATE_LIMIT_PER_MIN`、`RESEARCH_TIMEOUT_SEC`、`LOG_LEVEL`。

### GCP VM 設定說明（寫入 README）

**前提：VM 已部署於美國區，region 不可變更，SSH alias 為 `movie-nas`。**

**此主機同時運行 Jellyfin、qBittorrent 等既有媒體服務。** 以下為共用主機的硬性約束：

- **獨立的 compose project**：bot 使用自己的目錄與 `-p gemini-bot`，**不得**併入既有媒體 stack 的 compose 檔，避免重啟時連帶影響。
- **不共用 docker network**，使用預設 bridge 即可（polling 不需被其他容器存取）。
- 重啟只用 `docker compose -p gemini-bot restart bot`，**禁止在此主機使用 `docker compose down`**。
- 資源限制數字需先實測後填入，不得沿用範例值。部署前執行：
  ```bash
  free -h && swapon --show && df -h / && docker stats --no-stream
  ```
  若 swap 未啟用，先建立 2GB swap 再部署——加入新 process 後若觸發 OOM killer，被終結的不一定是 bot。
- 媒體暫存目錄需設硬性大小上限並用完即刪，磁碟與媒體服務共用。
- Egress 額度與 Jellyfin 串流共用。這使「零 egress 的圖片 URL 直傳路徑」成為必要功能而非優化項。
- 防火牆：**不需開啟任何 inbound port**，這是採用 polling 的主要安全收益。Jellyfin (8096) 與 qBittorrent 的既有埠不受影響，無衝突風險。

### Cookie 取得流程（**必須寫入 README，這是本專案最關鍵的操作步驟**）

cookie 的「出生 IP」必須與「使用 IP」一致。若在台灣的瀏覽器取得 cookie 卻拿到美國區 VM 使用，Google 會偵測到 session 地理位置大幅跳動，觸發安全驗證並頻繁作廢 cookie。

解法是讓瀏覽器透過 VM 出口登入，使 session 從誕生到使用都在同一個 IP：

1. 在本機建立到 VM 的 SOCKS 通道（`movie-nas` 為既有的 SSH alias）：
   ```bash
   ssh -D 1080 -C -q -N movie-nas
   ```
2. 建立獨立的 Firefox profile，並以與 VM 區域相符的時區啟動（降低指紋不一致訊號）：
   ```bash
   TZ=America/Chicago /Applications/Firefox.app/Contents/MacOS/firefox -P gemini-us
   ```
3. Firefox 設定 → Network Settings → Manual proxy：SOCKS Host `127.0.0.1`，Port `1080`，SOCKS v5，勾選 **Proxy DNS when using SOCKS v5**。
4. 先確認對外 IP 已是 VM 的位址，再開私密視窗登入 gemini.google.com。
5. F12 → Network → 複製 `__Secure-1PSID` 與 `__Secure-1PSIDTS`，**立即關閉私密視窗**（見上游 issue #6）。
6. 透過 bot 的 `/setcookie` 指令注入，或寫入 `.env` 後重啟。

務必使用 **Firefox**。上游 README 指出 Chromium 系瀏覽器的 Device Bound Session Credentials 會讓 cookie 只維持數小時且無法更新。

README 需說明：台灣帳號首次從美國 IP 登入時，Google 大機率要求二階段驗證並寄送新裝置通知，此為預期行為，通過後即穩定。

---

## 驗收標準

依序完成，每階段可獨立驗證：

1. **M1 — 連通**：`docker compose up` 後 bot 上線，白名單使用者發送文字可收到 Gemini 回覆；非白名單使用者被拒絕並留下 log。
2. **M2 — 渲染**：包含粗體、清單、多語言 code block、超過 4096 字元的長回覆能正確送達不破版；rendering 層單元測試通過。
3. **M3 — 會話**：多輪對話上下文正確；`/new` 重置；重啟 container 後仍能延續原對話。
4. **M4 — 媒體**：上傳圖片與 PDF 可被 Gemini 讀取；`/img` 生成的圖片能送回 Telegram。**驗收重點是 egress 路徑**：需明確驗證並在 README 記錄「Gemini 圖片 URL 是否可由 Telegram 直接抓取」的實測結果，若可行則預設走零 egress 路徑，`/status` 的流量計數應維持為 0。
5. **M5 — 韌性**：手動填入無效 cookie 後，bot 進入 DEGRADED 並推播管理員；`/setcookie` 熱更新後恢復服務，全程未重啟 process。
6. **M6 — 研究**：`/research` 送出後立即返回；完成時主動推播；重啟後仍能恢復輪詢。

---

## 範圍外（本次不做）

- Web UI / HTTP API
- 多帳號 cookie 池與輪替
- 群組聊天支援（僅支援 1:1 私訊與明確白名單的群組 id）
- 向量檢索 / RAG
- Kubernetes 或多副本部署（與 singleton 約束衝突）

---

## 工作方式要求

- 開工前先讀 <https://github.com/HanaokaYuzu/Gemini-API> 的 README 與 `src/gemini_webapi/` 原始碼，確認實際的例外類別名稱、`ModelOutput` 欄位、`ChatSession.metadata` 的型別。**不要依據記憶推測 API 介面**——這個套件更新頻繁，且 README 明確標示 `Model` enum 已棄用、`Model.from_name` / `Model.from_dict` 已移除。
- 每完成一個 milestone 就 commit，commit message 使用英文。
- 產出 `README.md` 使用繁體中文，技術術語保留英文原文。
- 對於任何你不確定的上游行為（例如錯誤類別的實際名稱、streaming 的 chunk 邊界語意），先寫一支最小驗證腳本確認，不要直接寫進正式邏輯後再賭。
- 完成後在 README 附上「已知風險」章節：ToS 違反與封號風險、cookie 過期頻率、AGPL-3.0 授權影響、單例架構的擴展限制。
