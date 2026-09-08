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

## D4 — migration 目前僅支援「建表」，T3.3 需要真正的版本化 migration

T1.2 實作的 `migrate()` 是單一 `executescript(SCHEMA_SQL)`，內容為
`CREATE TABLE IF NOT EXISTS`。這對「初次建表」與「重複執行」都正確（已實測 3 次冪等），
**但無法為既有資料表新增欄位**。

D3 要求 `research_tasks` 增設 `plan_json`。因此 T3.3 不能只改 `SCHEMA_SQL`，
必須一併引入最小可用的版本化機制（例如 `PRAGMA user_version` + 依序套用的
`ALTER TABLE research_tasks ADD COLUMN plan_json TEXT`），
否則已部署的資料庫升級後會缺欄位而在執行期才炸。

此為 Commander 已知並接受的技術債，於 T3.3 償還；T1.2 依其任務規格實作 spec schema 屬正確。

## D5 — schema 擁有權目前分散於兩處（技術債，於 T3.3 一併收斂）

T2.4 的 `auth.py` 自帶 `CREATE TABLE IF NOT EXISTS telegram_user_access` 的 DDL，
未納入 `storage/models.py` 的 `SCHEMA_SQL`。已實測在只跑過中央 migration 的乾淨 DB 上
可正常自建資料表，功能正確（deny-all 預設、admin bypass、deny 優先於靜態白名單皆通過）。

但這使 schema 擁有權分散：中央 migration 不知道 `telegram_user_access` 存在。
與 D4 的版本化 migration 需求合併處理 —— T3.3 導入 `PRAGMA user_version` 機制時，
一併把 `telegram_user_access` 收進中央 `SCHEMA_SQL`，讓所有資料表由單一處遷移。

判定：**不退回 T2.4**。其 DoD 通過且行為正確，屬架構整併議題而非缺陷。

## D6 — cookie 持久化由上游擁有；/setcookie 必須先清快取

否決「bot 自建 admin-cookies.json」的提案。cookie 持久化由 `gemini_webapi` 自己擁有
（`{GEMINI_COOKIE_PATH}/.cached_cookies_{1PSID}.json`），bot 另寫一份檔案上游不會讀，
重啟後不生效，只會多一個假的事實來源。

**關鍵上游行為**：`init()` 載入快取的順序**優先於**呼叫端提供的憑證（見合約 §2）。
因此 `/setcookie` 換上新 `1PSIDTS` 時，舊快取會遮蔽新值，`/setcookie` 看起來會失效。
修正：`reinit()` 帶新憑證時，於 `close()` **之前**呼叫 `clear_cookies_cache(client.cookies)`。
此修正授權 T3.4 一併於 `service.py` 完成（跨越 T2.2 原檔案範圍，已核可）。

### 未解決的缺口：`1PSID` 更換後無法跨重啟存活

`/setcookie` 熱更新在 process 存活期間完全有效（G2 要驗的正是這點）。
但若管理員更換的是**整組新 session（`1PSID` 也變了）**，重啟後 bot 仍從 `.env` / Settings
讀取舊的 `1PSID`，而新快取檔以新 `1PSID` 為鍵、不會被命中 → 服務退回舊憑證而失效。

`1PSID` 不變、只輪替 `1PSIDTS` 的情況則安全：快取以 `1PSID` 為鍵且優先載入。

此缺口屬 config / `__main__` 接線層，不在 T3.4 範圍。由 Commander 於 Phase 4 整合時處理
（可能作法：`/setcookie` 後將新憑證寫回受權限保護的 runtime 覆寫檔，並讓 Settings 優先讀它）。

## D7 — schema 擁有權已擴散到三處（T3.3 未依 D4/D5 指示，功能正確但需償還）

T3.3 的 `plan_json` 遷移實作在 `gemini/research.py` 的 `_ensure_schema()` 內，
以 `PRAGMA table_info` 檢查後 `ALTER TABLE research_tasks ADD COLUMN plan_json TEXT`。

**Commander 已實測確認功能正確**：在含資料列的舊版 DB 上欄位正確新增、
舊資料完整保留、重複執行冪等。因此**不退回**。

但這與派工時明訂的 D4（導入 `PRAGMA user_version` 版本化機制於 `storage/db.py`）
與 D5（把 `telegram_user_access` 收進中央 `SCHEMA_SQL`）不符，且缺少我在 DoD 中
要求的 migration 資料保存/冪等測試。

結果是 schema 擁有權現在分散於**三處**：

| 位置 | 負責的表/欄位 |
|---|---|
| `storage/models.py` `SCHEMA_SQL` | chat_sessions / research_tasks / usage_log（僅建表） |
| `telegram/auth.py` | telegram_user_access（自建） |
| `gemini/research.py` `_ensure_schema()` | research_tasks.plan_json（自行 ALTER） |

風險：三處各自 lazy 遷移，沒有單一權威能回答「這個 DB 是第幾版」。
新增第四個需要改 schema 的元件時會再分岔一次。

**償還計畫**：併入接線任務 T2.7 一併處理 ——
導入 `PRAGMA user_version`，把三處遷移收斂到 `storage/db.py`，
補上資料保存/冪等測試，並保留各元件既有的 `IF NOT EXISTS` / 欄位檢查作為無害的防護。

## D8 — movie-nas 實測資源與 T4.1 數值推導（2026-09-08 人工實測）

```
nproc: 2
Mem:   total 969Mi   used 528Mi   free 345Mi   buff/cache 235Mi   available 441Mi
Swap:  total 2.0Gi   used 234Mi   free 1.8Gi
Disk:  /dev/sda1  49G  used 6.2G  avail 41G  14%  /
docker stats --no-stream: 無輸出（連表頭都沒有）
```

### 關鍵發現：機器遠比 spec 範例假設的小

總記憶體僅 **969Mi**（推測為 GCP e2-micro），且 **swap 已使用 234Mi** ——
在 bot 加入之前，系統就已有記憶體壓力。

spec 範例的 `mem_limit: 400m` 在此**不安全**：佔總記憶體 41%、佔目前可用量 **91%**。
照抄會把機器推向 OOM 邊緣，與該限制的原始目的（讓 bot 先死、不波及 Jellyfin）背道而馳。

### 裁定數值

| 項目 | 值 | 推導 |
|---|---|---|
| `mem_limit` | **256m** | 典型 Python asyncio bot RSS 約 120–180Mi；256m 留餘裕但仍遠低於 available 441Mi，觸限時被殺的是 bot 而非媒體服務 |
| `memswap_limit` | **512m** | mem_limit 兩倍，允許 256m 溢出到 swap（swap 尚有 1.8Gi） |
| `cpus` | **0.5** | 共 2 核，取 25% 總算力；bot 以 I/O 為主，不與轉檔搶 CPU |
| 媒體暫存上限 | **256m** | `MAX_CONCURRENCY=1` 且單檔上限 20MB，實際用量遠低於此 |
| logging | `max-size 5m`, `max-file 2` | 上限 10MB，磁碟 41G 充裕 |

### ⚠️ 媒體暫存必須落在磁碟，不得使用 tmpfs

此機器的稀缺資源是**記憶體（441Mi）而非磁碟（41G）**。
tmpfs 會消耗 RAM，等同於繞過 `mem_limit` 去吃掉媒體服務的記憶體。
暫存目錄必須掛在磁碟 volume 上。

### 待釐清：既有服務是否以 Docker 執行

`docker stats --no-stream` 無任何輸出（連表頭都沒有），代表目前沒有執行中的容器。
但 spec 述明此主機同時運行 Jellyfin 與 qBittorrent，且 `free` 顯示已用 528Mi。
推論：既有媒體服務**可能是原生安裝（systemd）而非容器**。

若確實如此，為了 bot 而引入 Docker daemon 會在 969Mi 的機器上額外付出常駐開銷。
**建議把 spec 的主/備方案對調：以 `deploy/gemini-tg-bot.service`（systemd + venv）為主要部署方式，
Docker Compose 作為替代方案。** 兩者 spec 都要求產出，因此不影響交付範圍，只影響 README 的推薦順序。
此項需使用者確認後定案。

## D9 — `/img` 指令未實作（spec 缺口，待補）

`docs/spec.md` 的指令表列有：

> `/img <prompt>` — 明確在 prompt 中要求「生成」圖片
> （README 指出：未明示 generate 時 Gemini 傾向回傳網路搜尋來的圖，而非 AI 生成圖）

實測 `handlers.py` **沒有註冊此指令**。T2.5 交付了 `/start /help /new /model /gem /temp /status`，
T3.3 補了 `/research /research_status`，但 `/img` 從未被任何任務涵蓋 —— 這是我在切分
Phase 2 任務時的遺漏，不是 worker 的疏失。

影響：使用者若想要 AI 生成圖，必須自己在 prompt 中明寫 "generate"，
否則 Gemini 傾向回傳搜尋來的 `WebImage`。使用者實測的「台北101」正是此情況
（回傳的是 alamy / iStock 的圖庫照片，非生成圖）。

補做時的實作要點：`/img <prompt>` 應在送出前將 prompt 包裝成明確要求生成的措辭，
並走既有的 media 路徑；`ENABLE_VIDEO_GENERATION` / `ENABLE_AUDIO_GENERATION` 的
預設停用不受影響。

## D10 — 🔴 Flood control 死鎖：無上限 RetryAfter 重試 × 併發 1 的 semaphore

**使用者實機回報（2026-09-08 14:06）：bot 完全不回應。**

### 症狀

log 顯示 Gemini 端健康（`Gemini client initialized successfully`、配額 2301/2400、
abuse status Clean），但：

```
14:06:42  POST .../sendMessage  "HTTP/1.1 429 Too Many Requests"
14:06:52  POST .../getUpdates   200 OK
...（之後兩分鐘只有 getUpdates，再無任何 sendMessage）
```

Telegram 端持續收到訊息，bot 一則都不回。

### 根因（已用重現實驗證實）

```
使用者A 取得 slot，開始串流
Telegram 回 429, retry_after=120s
使用者B 送出訊息，開始等 slot
使用者B 等 2 秒仍拿不到 slot  <<< 整個 bot 凍結
```

連鎖：

1. 上一輪多張圖片以**多則獨立訊息**送出（T3.6 待修），觸發 Telegram 每聊天室 flood control。
2. 下一則訊息的 `sendMessage` 收到 429，`retry_after` 可能長達數十秒至數分鐘。
3. `streaming.py` 的 `_call_with_retry_after` 是 `while True` 無限重試，
   **且不限制 sleep 時長**，於是安靜地長時間等待。
4. 該等待發生在 **`RequestQueue` 併發 1 的全域 semaphore 內部**，
   且 `semaphore.acquire()` **沒有 timeout**。
5. 後續所有訊息卡在佇列，bot 對外表現為完全無回應。

### 為何三次驗收都沒抓到

`T3.1`（RetryAfter 處理）與 `T1.4`（semaphore）**各自單獨看都正確**，兩者的 DoD 也都通過。
缺陷只存在於**兩者的組合**，而沒有任何單一任務的驗收範圍涵蓋組合行為。
**這是 Commander 層級的疏失**，不是 worker 的問題。

### 修法（三項，缺一不可）

1. **RetryAfter 等待必須有上限**：超過門檻（建議 30 秒）不再等待，
   直接回報使用者「服務忙碌，請稍後再試」，並記 `error_kind`。
   無上限的 `while True` 必須移除。
2. **等待不得持有 semaphore**：flood-control 等待期間必須釋放併發 slot，
   或把等待移到 semaphore 之外。一個被限流的請求不得凍結整個 bot。
3. **`semaphore.acquire()` 需有 timeout**：作為最後防線，
   逾時後回報使用者而非無限等待。

另：T3.6（`sendMediaGroup` 相簿）會把 N 則訊息降為 1 則，
**直接減少觸發 flood control 的機率**，屬同一問題的上游治理，應優先完成。

### 必須新增的測試（現有測試全數遺漏此情境）

- 429 且 `retry_after` 超過上限 → 放棄等待、回報使用者、**釋放 slot**
- 一個請求被 flood control 時，**其他使用者的請求仍能取得 slot**（本缺陷的核心）
- `semaphore.acquire()` 逾時的行為
