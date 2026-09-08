# Gemini Telegram Bot

透過 [`gemini-webapi`](https://github.com/HanaokaYuzu/Gemini-API) 提供 Gemini 對話能力的
Telegram bot，設計為部署在資源受限、且與其他服務共用的 GCP VM 上。

**這不是官方 Gemini API。** 認證方式是瀏覽器 cookie，不是 API key。

---

## ⚠️ 動手前必讀：安全前提

這一節不是免責聲明，是實際的操作風險。

### bot 持有的是一個真實 Google 帳號的完整 web session

不是受限的 API token，是**完整的登入態**。這代表：

- 若該帳號啟用了 Gemini extensions，**任何能與這個 bot 對話的人，都能透過
  `@Gmail` 讀取該帳號的信箱內容**，`@Google Drive` 同理。
- 因此白名單是 P0 功能而非後續迭代項目。`ALLOWED_USER_IDS` **未設定時 bot 拒絕所有訊息**，
  只回覆一則說明並將未授權嘗試寫入 log。這個預設值不要改。

### 請使用獨立的次要 Google 帳號

**不要綁定你的主帳號。** 這是逆向工程的 API，違反 Google ToS，有封號風險
（詳見下方「已知風險」）。

### cookie 絕不可進入版本控制或 image

- `.env` 權限設為 `600`
- `.gitignore` 已涵蓋 `.env`、`data/`、`cookies/`
- `Dockerfile` 只 `COPY pyproject.toml` 與 `src/`，密鑰不會進入任何 image layer
- cookie 快取檔名本身**含有 `__Secure-1PSID` 明文**
  （`.cached_cookies_{1PSID}.json`），因此該路徑不會被寫進 log，
  `/status` 只回報刷新時間、不回報路徑

---

## 快速開始

```bash
# 1. 建立設定檔
cp .env.example .env && chmod 600 .env

# 2. 填入五個必要值（見下方「設定」）
#    TELEGRAM_BOT_TOKEN / ADMIN_USER_ID / ALLOWED_USER_IDS
#    GEMINI_SECURE_1PSID / GEMINI_SECURE_1PSIDTS

# 3. 起飛前檢查（不會發出任何 Gemini 請求，也不會印出任何祕密值）
uv run python scripts/preflight.py --check-ip

# 4. 啟動
uv run python -m gemini_tg_bot
```

`preflight.py` 會檢查 `.env` 完整性與權限、cookie 目錄可寫性、以及
**proxy 後的對外 IP** —— 最後一項是 cookie 壽命的關鍵，見下一節。

---

## Cookie 取得流程（本專案最關鍵的操作步驟）

### 為什麼不能直接在瀏覽器複製 cookie

cookie 的「**出生 IP**」必須與「**使用 IP**」一致。

若你在台灣的瀏覽器取得 cookie、卻拿到美國區 VM 上使用，Google 會偵測到 session
地理位置大幅跳動，觸發安全驗證並**頻繁作廢 cookie**。你會陷入「每隔幾小時就要重新登入」
的迴圈，而且找不出原因。

解法是讓瀏覽器**透過 VM 的出口登入**，使 session 從誕生到使用都在同一個 IP。

### 步驟

1. **建立到 VM 的 SOCKS 通道**（`movie-nas` 為既有的 SSH alias）：

   ```bash
   ssh -D 1080 -C -q -N movie-nas
   ```

   這個 terminal 要保持開著。

2. **建立獨立的 Firefox profile**，並以與 VM 區域相符的時區啟動
   （降低指紋不一致訊號）：

   ```bash
   TZ=America/Chicago /Applications/Firefox.app/Contents/MacOS/firefox -P gemini-us
   ```

   **務必使用 Firefox。** 上游 README 指出 Chromium 系瀏覽器的
   Device Bound Session Credentials (DBSC) 會讓 cookie 只維持數小時且無法更新。

3. **Firefox 設定 → Network Settings → Manual proxy**：
   - SOCKS Host `127.0.0.1`，Port `1080`，**SOCKS v5**
   - **勾選 "Proxy DNS when using SOCKS v5"** —— 沒勾的話 DNS 會走本地，洩漏真實位置

4. **先確認對外 IP 已是 VM 的位址**，再開私密視窗登入 `gemini.google.com`：

   ```bash
   curl --socks5-hostname 127.0.0.1:1080 https://ifconfig.me
   ```

5. **F12 → Network → 複製 `__Secure-1PSID` 與 `__Secure-1PSIDTS`**，
   然後**立即關閉私密視窗**（見上游 issue #6）。

6. 填入 `.env`，或透過 bot 的 `/setcookie` 指令注入（不需重啟 process）。

### 更快的作法：`scripts/grab_cookies.sh`

步驟 5 的 F12 手動複製可以省略。保持 tunnel 開著並登入後，在 Mac 上執行：

```bash
sh scripts/grab_cookies.sh
```

它會直接從 Firefox profile 的 `cookies.sqlite` 讀出兩個 cookie 並**送進剪貼簿**，
接著在 Telegram 傳 `/setcookie` 貼上即可。終端機只顯示長度，不顯示任何值。

指定其他 profile：`sh scripts/grab_cookies.sh <profile-name>`（預設 `gemini-us`）。

**前提仍然是 tunnel 必須開著** —— cookie 的出生 IP 必須是 VM 的 IP，
這一點不會因為取得方式變方便而改變。取之前先確認：

```bash
curl --socks5-hostname 127.0.0.1:1080 https://ifconfig.me
```

> **預期行為**：台灣帳號首次從美國 IP 登入時，Google 大機率要求二階段驗證並寄送
> 新裝置通知。這是正常的，通過後即穩定。

### 在本機測試（不必動正式主機）

`GEMINI_PROXY` 支援 SOCKS。掛著上面的 tunnel 就能在本機以 VM 的出口 IP 執行：

```
GEMINI_PROXY=socks5h://127.0.0.1:1080
```

用 `socks5h` 而非 `socks5` —— `h` 代表 DNS 也走 tunnel，對應步驟 3 勾選的
「Proxy DNS」。DNS 走本地會洩漏真實位置。

### cookie 會自動輪替

啟動後 `auto_refresh` 會在背景輪替 `__Secure-1PSIDTS`（預設每 600 秒），
並寫入 `GEMINI_COOKIE_PATH`。因此：

- **`.env` 裡那份在啟動後就過時了**，實際生效的是快取檔
- 重啟不需要重新取得 cookie；只有整個 session 被 Google 作廢時才需要
- 這也是 `GEMINI_COOKIE_PATH` 必須掛在 volume 的原因 —— 沒掛的話容器重建即失效

---

## 設定

`.env.example` 內含完整註解。必填五項：

| 變數 | 說明 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 發的 token |
| `ADMIN_USER_ID` | 可執行管理員指令的 Telegram user ID |
| `ALLOWED_USER_IDS` | 逗號分隔的白名單。**留空 = 拒絕所有人** |
| `GEMINI_SECURE_1PSID` | Gemini cookie |
| `GEMINI_SECURE_1PSIDTS` | Gemini cookie |

其餘皆有安全預設值，其中三個特別值得注意：

| 變數 | 預設 | 為什麼是這個值 |
|---|---|---|
| `MAX_CONCURRENCY` | `1` | 記憶體受限，且單一 Gemini 帳號本有配額上限，併發無實益 |
| `ENABLE_VIDEO_GENERATION` | `false` | 單支可達數十 MB，會直接吃掉整月免費 egress |
| `ENABLE_AUDIO_GENERATION` | `false` | 同上 |

---

## 指令

### 一般使用者

| 指令 | 行為 |
|---|---|
| `/start`、`/help` | 說明可用指令 |
| `/new` | 結束目前對話，開新的 `ChatSession` |
| `/model` | 呼叫 `client.list_models()` **動態**列出，inline keyboard 選擇 |
| `/gem` | 列出可用 gem，選定後套用於後續對話 |
| `/temp` | 切換 temporary mode（不寫入 Gemini 歷史） |
| `/research <topic>` | 送出 Deep Research 背景任務，**立即回傳 task id** |
| `/research_status` | 列出該 chat 的研究任務狀態 |
| `/status` | 目前模型、session cid、cookie 最後刷新時間、佇列深度、今日用量、本月 egress 估算 |
| 純文字訊息 | 送入目前 `ChatSession`，**streaming 回覆** |
| 圖片 / 文件 | 作為 `files=[...]` 送給 Gemini，caption 作為 prompt |

模型清單一律動態取得，**不硬編碼** —— 上游已棄用 `Model` enum 並移除
`Model.from_name` / `Model.from_dict`。

### 管理員（限 `ADMIN_USER_ID`）

| 指令 | 行為 |
|---|---|
| `/setcookie` | 互動流程接收新 cookie，**熱重啟 client 不重啟 process**；訊息收到後立即刪除 |
| `/allow <user_id>` | 加入白名單（寫入 DB，即時生效） |
| `/deny <user_id>` | 移出白名單。**deny 優先於靜態白名單** |
| `/health` | client 健康狀態、最近錯誤、DB 狀態 |

---

## 部署

> **重要**：正式主機同時運行 Jellyfin 與 qBittorrent。
> **禁止在該主機使用 `docker compose down`** —— 會波及既有媒體服務。
> 重啟一律 `docker compose -p gemini-bot restart bot`。

### 主機實測資源（2026-09-08）

```
nproc: 2
Mem:   total 969Mi   available 441Mi
Swap:  total 2.0Gi   used 234Mi     ← 加入 bot 前系統已有記憶體壓力
Disk:  49G, avail 41G
```

**這台機器只有約 1GB 記憶體。** 資源限制據此推導（見 `docs/decisions.md` 的 D8）：

| 項目 | 值 |
|---|---|
| `mem_limit` / `MemoryMax` | `256m` |
| `memswap_limit` / `MemorySwapMax` | `512m` |
| `cpus` / `CPUQuota` | `0.5` / `50%` |
| 媒體暫存上限 | `256m`（**必須落在磁碟，不得用 tmpfs**） |

限制的目的是**讓 bot 先被 OOM killer 終結，而不是波及媒體服務**。
換機器後必須重新實測，不要沿用這些數字。

**媒體暫存不得使用 tmpfs**：這台機器稀缺的是記憶體（441Mi）而非磁碟（41G），
tmpfs 會吃 RAM，等同繞過 `mem_limit` 去搶 Jellyfin 的記憶體。

### 建議以 systemd 為主要方式

實測 `docker stats --no-stream` 在該主機上無任何輸出，代表既有媒體服務
**可能是原生安裝而非容器**。若確實如此，為了這一個 bot 引入 Docker daemon，
在 969Mi 的機器上是不划算的常駐開銷。

因此本專案雖同時提供兩種部署產物，**在此主機上建議使用
`deploy/gemini-tg-bot.service`（systemd + venv）**，Docker Compose 作為替代方案。

### Docker Compose

```bash
docker compose -p gemini-bot -f deploy/docker-compose.yml up -d
```

- **獨立的 compose project**（`-p gemini-bot`），不得併入既有媒體 stack 的 compose 檔
- 不共用 docker network，預設 bridge 即可
- **不設定任何 `ports`** —— long polling 不需要 inbound port

### 防火牆

**不需開啟任何 inbound port。** 這是採用 long polling 而非 webhook 的主要安全收益。
Jellyfin (8096) 與 qBittorrent 的既有埠不受影響，無衝突風險。

---

## 成本與 egress

VM 位於美國區，月預算上限 US$10。**egress 是主要成本來源**：
免費層每月僅 1GB 北美對外流量，超出約 US$0.12/GB，而且這個額度**與 Jellyfin 串流共用**。

### 零 egress 的圖片路徑

天真的作法會讓同一份資料走兩次網路：Gemini 回傳圖片 → VM 下載 → VM 上傳給 Telegram。

Telegram 的 `sendPhoto` / `sendMediaGroup` 接受 **URL 字串**，由 Telegram 伺服器
自行抓取來源，完全不經過 VM。本專案兩條路徑都實作：

| 路徑 | egress | 何時採用 |
|---|---|---|
| **A. URL 直傳** | **0** | 預設樂觀嘗試 |
| **B. 下載後上傳** | 計量 | Telegram 回 `BadRequest` 時自動退回 |

退回是**逐張**判斷的，一張失敗不影響其他張。`/status` 會回報本月累計 egress 估算值，
讓流量消耗可見，而不是事後從帳單發現。

若 `/status` 的 egress 維持 0，代表路徑 A 正常運作。

### 其他成本控制

- 影片／音訊生成**預設停用**，需明確開啟環境變數
- 使用者上傳上限 20MB（Telegram Bot API 限制），**提前拒絕**而非下載到一半才失敗
- 媒體暫存用完即刪
- 日誌輪替 `max-size: 5m`、`max-file: 2`

---

## 架構

```
Telegram Long Polling
        │
   ┌────▼──────────────────────────┐
   │ Auth Middleware（預設拒絕）      │
   └────┬──────────────────────────┘
   ┌────▼──────────────────────────┐
   │ Command Router / Handlers     │
   └────┬──────────────────────────┘
   ┌────▼──────────────────────────┐
   │ RequestQueue                  │
   │  Semaphore + per-user bucket  │
   └────┬──────────────────────────┘
   ┌────▼──────────────────────────┐
   │ GeminiService（singleton）      │
   │  生命週期 / DEGRADED 狀態機      │
   └────┬──────────────────────────┘
   ┌────▼──────────────────────────┐
   │ gemini_webapi.GeminiClient    │
   └───────────────────────────────┘

旁路：SQLite (aiosqlite)、Cookie Store、Admin Notifier
```

### 為什麼必須是單例

`auto_refresh=True` 會在背景輪替 `__Secure-1PSIDTS`。**多個 process 共用同一組 cookie
會互相作廢**，導致帳號反覆登出。因此：單一 process、單一 event loop、
不得開多 worker 或多 replica。

程式碼中只有一處 `GeminiClient(...)` 建構點（`gemini/service.py`），這是刻意的架構約束。

### DEGRADED 狀態機

服務降級有兩種成因，恢復路徑完全不同：

| 成因 | 觸發 | 恢復 |
|---|---|---|
| `AUTH` | `AuthError`，**立即** | **僅能**由 `/setcookie` 恢復，不會隨時間自癒 |
| `BLOCKED` | 連續 3 次 `TemporarilyBlockedError` | 15 分鐘後 half-open 探測，成功即自動恢復 |

分開處理的理由：AUTH 是憑證問題，等待無意義，必須人工換 cookie；
BLOCKED 是上游節流，會自行恢復，若也要求人工介入只會製造無謂的管理員噪音。

### Rendering

Gemini 輸出標準 Markdown，與 Telegram 的 MarkdownV2 跳脫規則不相容，直接送出必然失敗。
本專案轉為 Telegram HTML（僅支援 `b/i/u/s/code/pre/a/blockquote`），不支援的結構降級為純文字。

- **LaTeX**：Telegram 無法排版 LaTeX。本專案選擇**以 `<code>` 包裹原式**保留原文，
  而非轉 Unicode 近似 —— 近似轉換在複雜公式上會失真且不可逆，保留原文至少可讀可複製。
- **切段**：4000 字元安全上限，優先在 `\n\n` 切、其次 `\n`、最後硬切。
  切點不會落在 code fence 內部，跨段的 code block 會在每段補上開闔標記。
- **巢狀清單**：Telegram HTML 無 `<ul>`/`<li>`，以符號與縮排模擬。
  第一層 `• `、第二層 `◦ `、第三層 `▪ `，縮排使用 **U+2007 FIGURE SPACE**
  （一般空白會被 Telegram 摺疊）。最多三層，更深者併入第三層。
- **Streaming**：每 1.5 秒或每累積 200 字元才 edit（取先到者）。
  **串流期間一律純文字，只有最後一次 edit 才套用 HTML** —— 這避免中途出現
  未閉合標籤導致 `can't parse entities`。

---

## 已知風險

### 1. 違反 Google ToS，有封號風險

`gemini-webapi` 是**逆向工程**的 web app wrapper，不是官方 API。使用它違反 Google 的
服務條款。帳號可能被限制或停權，而且不會有事先通知。

**請務必使用獨立的次要帳號，不要綁定主帳號。**

### 2. cookie 會過期，且過期頻率不可預測

Google 會主動作廢 session。實務上影響最大的因素是 **IP 一致性** ——
出生 IP 與使用 IP 不同會顯著提高作廢頻率（見上方 cookie 取得流程）。

即使一切正確，仍應預期**定期需要人工重新取得 cookie**。
bot 會在偵測到 `AuthError` 時進入 DEGRADED 並主動推播管理員，
你不需要盯著 log，但需要在收到推播時處理。

### 3. AGPL-3.0 授權影響

`gemini-webapi` 採用 **AGPL-3.0**。這是 copyleft 授權，且**涵蓋網路服務**：
若你把基於它的服務提供給第三方使用，AGPL 要求你也必須提供對應的原始碼。

自用不受影響。但若你打算對外提供服務，請先確認你能接受這個義務。

### 4. singleton 架構的擴展限制

單例約束（見上方「為什麼必須是單例」）意味著**這個架構無法水平擴展**。
不能開多 replica、不能用 Kubernetes 做多副本部署、不能靠加機器提高吞吐。

若未來需要更高吞吐，唯一的路是**多帳號 cookie 池與輪替**，那需要重新設計
cookie 生命週期管理，不是調整設定就能達成。本專案明確不做這件事。

### 5. 上游 API 可能無預警變動

`gemini_webapi` 追隨 Google 的內部介面，更新頻繁。本專案的所有上游 API 事實記錄在
`docs/upstream-api-contract.md`（由反射實測產生，非依據 README 撰寫），
**升級 `gemini-webapi` 版本時必須重跑 `scripts/probe_upstream.py` 並逐項比對**。

已知的上游缺陷可見合約 §四之二：`ARTIFACTS_RE` 對 `<數字>_<數字>` 形式的
圖片佔位符清理不完全，本專案自行補了一層清理。

---

## 開發

```bash
uv sync
uv run pytest -q                        # 全套測試
uv run python -m gemini_tg_bot --dry-run   # 不需憑證的全鏈路煙霧測試
```

`--dry-run` 以假 client 與假 transport 完整跑一次
`收訊息 → 白名單 → 佇列 → service → streaming → 渲染 → 送出 → research → DB`，
退出碼 0 表示整合層健康。**開發期所有測試一律 mock，不對真實 cookie 發 live 請求。**

### 文件

| 檔案 | 內容 |
|---|---|
| `CLAUDE.md` | 專案不變量（安全禁令、實作紀律） |
| `docs/upstream-api-contract.md` | 上游 API 事實來源，由反射實測產出 |
| `docs/decisions.md` | 架構決策紀錄與技術債 |
| `docs/interfaces.md` | 跨模組介面契約 |
| `docs/egress-findings.md` | egress 路徑實測與裁定 |
| `docs/RESUME.md` | 續跑指引 |

### 授權

本專案相依於 AGPL-3.0 授權的 `gemini-webapi`，見上方「已知風險」第 3 點。
