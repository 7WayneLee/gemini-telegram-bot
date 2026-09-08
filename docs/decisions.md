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

### D10 補充（2026-09-08，追查 `/new` 無回應時發現）：缺陷範圍比原判斷大得多

原本判定為「streaming × semaphore 的組合缺陷」。追查使用者回報的
「`/new` 也沒有反應」時發現**第二個獨立缺陷**：

```
handlers.py 中無 RetryAfter 保護的 reply_text / reply_photo：43 處
handlers.py 的 RetryAfter 處理：完全沒有
media.py  的 RetryAfter 處理：完全沒有
唯一有保護的模組：streaming.py
```

因此 flood control 期間有**兩種不同的失效模式**：

| 路徑 | 失效方式 |
|---|---|
| 純文字訊息（streaming） | `_call_with_retry_after` 無上限等待 → 持有 semaphore → **整個 bot 凍結** |
| **所有指令**（`/new` `/status` `/model` `/gem` `/temp` …） | `reply_text` 直接拋 `RetryAfter` → handler 中斷 → **靜默無回應** |
| 圖片送出（media） | 同上，直接拋出 → 圖片送不出去 |

`/new` 不經過 `request_queue`（它只做 `sessions.reset()` 後 `reply_text`），
所以它**不是被死鎖擋住的**，而是自己那行未保護的 `reply_text` 拋了 `RetryAfter`。
兩個 bug 症狀相同（無回應）但成因完全不同，修一個不會修好另一個。

### 因此 T3.8 的範圍擴大

除原本三項外，追加第四項：

4. **Telegram 送出必須統一經過受保護的路徑。**
   把 `_call_with_retry_after`（修正為有上限的版本）抽成共用工具，
   讓 `handlers.py`（43 處）與 `media.py` 全部改用它。
   不得留下任何裸露的 `reply_text` / `reply_photo` / `edit_text` / `reply_media_group`。
   驗收方式：`grep` 不應在 `handlers.py` / `media.py` 找到未經包裝的直接呼叫。

**根本教訓**：RetryAfter 是**傳輸層**關注點，不該由各個 handler 各自處理。
當初 spec 的錯誤矩陣把它列為一列，我卻只在 T3.1（streaming）指定實作，
沒有把它當成橫切關注點統一處理 —— 這是切分任務時的結構性錯誤。

## D11 — ⚠️ `scripts/dump_response.py` 必須在 bot 停止時執行

該腳本經由 `GeminiService.init()` 建立 client，而其內部呼叫
`client.init(auto_refresh=True)` —— **會啟動背景 cookie 輪替**。

若在 bot 運行中執行此腳本，將有**兩個 process 以同一組 cookie 各自輪替
`__Secure-1PSIDTS`**，互相作廢，導致帳號反覆登出，必須重跑 SSH SOCKS 流程重取 cookie。
這正是不變量 2 所防範的情況。

腳本 docstring 寫的「reuses GeminiService so the process still owns exactly one
Gemini client」只保證**單一 process 內**唯一，未涵蓋與運行中 bot 的併存。
撰寫時未考慮此情境，驗收時 Commander 亦未察覺。

### 正確操作順序

1. 停止 bot
2. 確認 SSH tunnel 仍存活（本機執行時）：`curl --socks5-hostname 127.0.0.1:1080 https://ifconfig.me`
3. 執行 dump（建議先跑一個確認 JSON 正常，再跑第二個，避免白費 live 請求）
4. 重新啟動 bot

### 待補防護

腳本應自行偵測並拒絕在 bot 運行時執行，例如以 `GEMINI_COOKIE_PATH` 下的 lock file
或檢查既有 process。目前僅以文件約束，屬於可被誤觸的設計。

## D12 — T3.10 的預期輸出已用真實 fixture 示範並確認

派工前以 `tests/fixtures/image-web.json` 實測示範，八個情境全部符合預期
（`<Image/>` 移除、`<FollowUp/>` 保留 label 為斜體、標題轉粗體、`---` 移除、
code fence 內原樣保留、未知大寫 tag 清除、小寫 HTML 不受影響、清空後走
`EMPTY_RESPONSE_TEXT`、U+2007 縮排維持）。

### 示範過程發現的踩坑點：渲染器只支援 `*italic*`，不支援 `_italic_`

```
*星號*  -> '<i>斜體</i>'
_底線_  -> '_斜體_'      ← 原樣輸出
```

`<FollowUp>` 的 label 若用 `_label_` 包裹會產生字面底線。**必須用 `*label*`。**
真實 fixture 未暴露此問題（Gemini 輸出用星號），但憑直覺實作極易踩到。

### 裁定：不補 `_italic_` 支援

`_italic_` 雖是標準 Markdown 語法，但補上會使 `some_var_name` 這類
snake_case 識別字被誤判為斜體。Gemini 實際輸出一律使用星號，
補此語法弊大於利。**維持只支援 `*italic*`。**

## D13 — 使用者裁定：`<FollowUp>` 整條刪除；串接回答照實呈現

推翻 D12 中「保留 `label` 為斜體提示」的規劃。使用者判斷建議追問在 Telegram
情境是雜訊而非幫助，**整條刪除，含 `label`**。

**副作用是實作變簡單**：不再需要 `<FollowUp>` 的特例 regex，
單一通用的大寫自閉合 tag 樣式即可涵蓋全部，少一條分支與一組測試。

移除後需 trim 前後多餘空白與連續空行（實測會留下尾端空行）。

### 串接的兩段回答：照實呈現，不修補

真實 fixture 中單一 candidate 的 `text` 含兩段完整回答，
第一段在「⋯呈現青竹般的藍」處被截斷後直接接第二段。

**裁定：不嘗試偵測或修補接縫。** 判斷「哪裡是接縫」沒有可靠訊號，
切錯會丟失真實內容，代價高於突兀的閱讀體驗。這是上游行為，照實呈現。


## D14 — D9 的 `/img` 缺口已補齊（T3.7）

`/img <prompt>` 已實作並納入 `PUBLIC_BOT_COMMANDS`（與 `/help`、Telegram 指令選單共用單一來源）。

生成措辭定義為具名常數：

```python
IMAGE_GENERATION_PREFIX = (
    "Generate an original AI image based on the following request. "
    "Do not search for or return existing web images:"
)
```

明示「不要搜尋或回傳既有網路圖片」，直接對應 fixture 揭露的事實 ——
未明示生成時 Gemini 走 `WebImage`（圖庫照片）而非 `GeneratedImage`。
使用者實測「台北101」拿到 alamy / iStock 圖庫照正是此現象。

送圖重用 T3.6 的既有相簿路徑，未自寫送圖邏輯。


## D15 — 🔴 憑證洩漏事件：`docker-compose config` 會展開並印出 env_file

**事件（2026-09-08，T4.1 執行期間）**

worker 為驗證 compose 檔語法而執行：

```
docker-compose -f deploy/docker-compose.yml config
```

由於 compose 使用 `env_file: ../.env`，Compose **展開並印出了解析後的環境變數**，
包含 `TELEGRAM_BOT_TOKEN` 與 `GEMINI_SECURE_1PSID` / `GEMINI_SECURE_1PSIDTS` 的真實值。

**worker 的處置正確**：偵測到、拒絕複述值、拒絕寫入任何檔案、立即以 escalation 升級。
這正是不變量 3 期望的行為。

### 影響評估（Commander 執行，僅比對存在與否，未印出任何值）

| 位置 | 結果 |
|---|---|
| Orca terminal-history | 0 個檔案 |
| Orca logs | 0 個檔案 |
| git 追蹤檔案 | 0 個 |
| 工作區（`.env` 以外） | 0 個 |
| **codex session logs** | **1 個檔案含 token 與 1PSID** |

落地檔案：
`~/.codex/sessions/2026/09/08/rollout-2026-09-08T17-16-17-01a0804d-cd54-7451-90d3-ed7d6d762019.jsonl`

範圍限於本機磁碟，未外傳。但憑證已寫入非預期檔案，**應視為已洩漏並輪替**。

### 🔴 禁止事項（即刻生效，所有任務適用）

**不得對帶有真實 `env_file` 的 compose 檔執行 `docker-compose config` 或 `docker compose config`。**

### 安全的替代驗證方式

1. 拋棄式目錄 + `.env.example`（只含 `FAKE_` 值）中執行 config
2. 純 YAML 語法驗證：`yaml.safe_load(open('deploy/docker-compose.yml'))`
3. Dockerfile 以 `docker build` 實際驗證（不涉及 env_file 展開）

### 根本問題：`.env` 就在 repo 根目錄，任何展開它的工具都會洩漏

`.gitignore` 能防止 commit，但擋不住「讀取並印出」。
未來若引入其他會解析 `.env` 的工具（compose、direnv、dotenv CLI 等），
同類事件會重演。長期解法是把正式憑證移出 repo 目錄
（例如 systemd 的 `EnvironmentFile=/etc/gemini-tg-bot.env`，unit 檔已如此設計），
開發期則只用 `FAKE_` 值。


## D16 — T4.1 交付；兩項技術債待償

### worker 在部署前抓到一個真實的 production bug

`__main__.py` 的 `DATABASE_PATH = Path("data/db/bot.sqlite3")` 是**相對路徑**。
原 Dockerfile 為 `WORKDIR /app`，會解析為 `/app/data/db/bot.sqlite3` ——
**不是 compose 掛載的 `/data/db` volume**。後果：

- 容器重建即遺失整個資料庫（chat sessions、research tasks、usage log）
- 且 `/app` 為 root 所有，非 root 的 uid 10001 連 `mkdir` 都會失敗 → 啟動直接掛掉

修法：Dockerfile 末尾加 `WORKDIR /`，使相對路徑解析為 `/data/db/bot.sqlite3`，
正好對上掛載點。systemd 側 `WorkingDirectory=/var/lib/gemini-tg-bot` 搭配
`ExecStartPre mkdir .../data/db` 亦一致。

### 債務 1：`WORKDIR /` 是隱性耦合

此修法只在「應用使用相對路徑」時成立。若日後有人把 `WORKDIR` 改回 `/app`，
資料庫會**靜默寫到非持久化位置** —— 這是最糟的失敗模式（不報錯、只是資料消失）。

穩健作法是讓 `DATABASE_PATH` 比照 `GEMINI_COOKIE_PATH` 改為環境變數可設定。
該修改屬 `src/`，不在 T4.1 範圍，故未動。**建議下次觸碰 `__main__.py` 時一併處理。**

### 債務 2：`uv.lock` 未進 build context，image 相依性非鎖定

`.dockerignore` 採 deny-all + allowlist（優於 denylist，`.env` 結構上無法進入 context），
但只放行 `pyproject.toml` 與 `src/`。Dockerfile 用 `pip install .`，
因此**每次 build 會重新解析相依版本**，與開發環境的 `uv.lock` 可能不同。

影響：開發測過的版本組合不保證等於 image 內的版本組合。
對此專案風險中等（相依少且皆有下限約束），但值得日後改為 `uv sync --frozen`。

### 未能親自驗證的項目

`docker compose build` 的 DoD **Commander 未能獨立重跑** —— 驗收當下本機 Docker daemon 未運行。
worker 回報成功並附具體輸出（`Successfully built 58ce5fd1ca3c`、
`uid=10001 workdir=/ cookie_dir=10001:10001:700 db_path=/data/db/bot.sqlite3 secret_files=absent`）。
依驗證協定 §4，此項如實記為**未經 Commander 親自驗證**，
`.dockerignore` 的安全性質則已靜態驗證確認。


## D17 — 圖片-only 回應會閃現「Gemini 未回傳文字。」（實機回報）

**使用者實機回報（2026-09-08 20:07，`/img 一隻貓`）**：
先出現「Gemini 未回傳文字。」，該訊息隨後被刪除，然後才出現貓的圖片。

### 根因：語義與順序都錯

實測 API 序列：

```
text = '\n\n_547\n\n'   → 清理後為空 → EMPTY_RESPONSE_TEXT
sendMessage       ← placeholder「思考中…」
editMessageText   ← 串流
editMessageText   ← 最後一次 edit 寫入「Gemini 未回傳文字。」  ← 使用者看到
sendPhoto 400 → sendPhoto 200                                 ← 圖片送出
deleteMessage     ← placeholder 這時才刪除
```

兩個問題：

1. **語義錯誤**：回應含 1 張圖片，並非「空回應」。
   `EMPTY_RESPONSE_TEXT` 只有在「既無文字也無圖片」時才為真。
   圖片生成的正常回應（text 只有 `_NNN` 佔位符殘留）會被誤判為空。
2. **順序錯誤**：placeholder 在圖片送出**之後**才刪除，
   造成使用者看見一則看似錯誤的訊息閃現約一秒。

### 裁定的修法

- 判斷是否為空必須**同時考慮 text 與 images**：
  清理後 text 為空 **且** `output.images` 為空時，才顯示 `EMPTY_RESPONSE_TEXT`。
- text 為空但有圖片時：**不要**寫入任何文字，直接刪除 placeholder 後送圖，
  或讓圖片的 caption 承載內容（此情況無文字可承載，故直接刪 placeholder）。
- placeholder 的刪除應在送圖**之前或同時**，不得在之後。

此為使用者可見的 UX 缺陷，優先度高於剩餘的技術債。


## D18 — 🔴 G2 演練失敗：cookie 失效不會觸發 DEGRADED，也不會推播管理員

**2026-09-08 20:19-20:23 實機演練，Commander 全程比對 log。**

### 演練結果

| G2 驗收項目 | 結果 |
|---|---|
| `/setcookie` 收到後立即刪除訊息 | ✅ 通過 |
| `reinit` 呼叫 `clear_cookies_cache` | ✅ 通過（D6 的修正確實生效） |
| 全程 process 未重啟 | ✅ 通過（pid 20505 未變） |
| **進入 `DEGRADED`** | 🔴 **失敗** |
| **主動推播管理員** | 🔴 **失敗** |

### 根因鏈

填入無效 cookie 後的實際 log：

```
WARNING  Account status: UNAUTHENTICATED - Session is not authenticated or cookies have expired.
WARNING  RPC request GPRiHf failed: Permission denied or unauthenticated.
SUCCESS  Gemini client initialized successfully.          ← 上游宣稱成功
```

**上游 `init()` 在 session 未認證時不拋例外，只記 warning 並回報 SUCCESS。**

於是：

1. `reinit()` 收不到例外 → 判定成功 → 回覆「Cookie 已更新，Gemini 服務已熱重啟。」
2. 服務維持 `HEALTHY`，DEGRADED 狀態機從未被觸發
3. 後續請求最終失敗，但錯誤型別是 **`APIError` 而非 `AuthError`**
4. 合約將 `APIError` 歸類為 `Fatal`（此歸類本身正確），而 `Fatal` **不觸發 DEGRADED**
5. 使用者收到通用訊息「處理請求時發生錯誤，請稍後再試。」，
   而非「認證失效，請 /setcookie」；**管理員完全沒有收到推播**
6. `usage_log` 記錄 `error_kind='fatal'`、**`latency_ms=91014`** ——
   每次請求會卡 91 秒才失敗，使用者只看到「思考中…」長時間不動

### 我們忽略了上游提供的正確訊號

`client.account_status` 是 `AccountStatus` enum，共 10 個成員：

```
AVAILABLE (1000)                     ACCESS_TEMPORARILY_UNAVAILABLE (1014)
UNAUTHENTICATED (1016)               ACCOUNT_REJECTED (1021)
ACCOUNT_UNTRUSTED (1033)             TOS_PENDING (1040)
TOS_OUT_OF_DATE (1042)               ACCOUNT_REJECTED_BY_GUARDIAN (1054)
GUARDIAN_APPROVAL_REQUIRED (1057)    LOCATION_REJECTED (1060)
```

`service.py` **一個都沒有檢查**（grep `account_status` 無命中），完全依賴 `AuthError` 例外。

其中 **`LOCATION_REJECTED` (1060) 特別重要** —— 那正是「cookie 出生 IP 與使用 IP 不符」的訊號，
整套 SSH SOCKS 流程就是為了避免它。若真的發生，目前的實作**不會察覺，也不會告知任何人**。

### 這是合約層級的疏失，責任在 Commander

我在合約中把錯誤分類建立在「例外型別」之上，並驗證了四類分類的正確性 ——
但**從未驗證「上游是否真的會拋出 `AuthError`」**。
T2.2 的 DEGRADED 測試全部以 mock 注入 `AuthError` 進行，因此永遠是綠的；
真實情況下該例外根本不會出現。這是 mock 測試與真實行為脫節的典型案例，
也正是 G2 這類實機 gate 存在的理由。


## D19 — ✅ G2 韌性演練通過（T3.13 修正後重測，2026-09-08 21:52）

第一次演練失敗（D18），修正後重跑，五項驗收全數通過。

| 驗收項目 | 首次（修正前） | 重測（T3.13 後） |
|---|---|---|
| `/setcookie` 收到後立即刪除訊息 | ✅ | ✅ |
| `reinit` 呼叫 `clear_cookies_cache` | ✅ | ✅ |
| **進入 `DEGRADED`** | 🔴 維持 healthy | ✅ `AccountStatusError` 拋出 |
| **主動推播管理員** | 🔴 無 | ✅ 收到認證失效通知 |
| **快速失敗** | 🔴 `latency_ms=91014`（91 秒） | ✅ 立即回覆，`text message` 失敗次數 0 |
| `/setcookie` 熱恢復 | 未達此步 | ✅ 服務回復正常 |
| **全程 process 未重啟** | ✅ | ✅ pid 28703 未變 |

### 決定性證據

**降級時**（21:52 前）：使用者發訊息收到
「Gemini 服務目前處於認證失效狀態，暫時無法接受請求。」
log 中 `operation=text message` 的失敗次數為 **0** ——
請求在進入 Gemini client 之前就被擋下，走快速失敗路徑，
因此既無 91 秒空轉，也無錯誤 log。修正前此處必有一次 `APIError` 與 91 秒延遲。

**恢復時**：

```
21:52:59  Skipping cookie cache write: the session is not authenticated.   ← 假 session 關閉
21:53:04  Account quota updated: 2388/2400 credits remaining               ← 新 session 認證成功
21:53:05  Account abuse status: Clean
21:53:05  SUCCESS  Gemini client initialized successfully.
```

`quota updated` 只有在 session 有效時才會出現，是 `AccountStatus.AVAILABLE` 的實證。

### 過程中順帶驗證的項目

- **D6 的快取優先序修正**：`reinit` 帶新憑證時清舊快取，使新貼上的 cookie 真正生效，
  而非被舊快取遮蔽。若無此修正，`/setcookie` 會在第一次嘗試時看似無效。
- **`.env` 的 `1PSIDTS` 會被輪替淘汰**：演練中確認 `.env` 內的值與快取中的已不同，
  快取才是最新來源。這對維運有直接意義 —— **重取 cookie 時不能只看 `.env`**。
  （見 D6 未解缺口：`1PSID` 更換後無法跨重啟存活。）
- **`auto_refresh` 正常運作**：快取檔 mtime 在 bot 啟動後更新，證明背景輪替有在動。
