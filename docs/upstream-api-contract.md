# 上游 API 合約 — `gemini_webapi`

> **版本：`gemini-webapi` 2.1.1**（`docs/upstream-api.json` 的 `package.version`）
> **產生方式**：由 Commander 親自執行 `uv run python scripts/probe_upstream.py --json` 反射產出，
> 並與 `docs/upstream-api.json` 逐項比對確認一致（兩次執行輸出 byte-identical）。
> **本檔為 CLAUDE.md 不變量 6 的唯一事實來源。** 任何 `gemini_webapi` 的類別名稱、
> 方法簽章、屬性欄位一律引用本檔，不得憑記憶推測。
> **合約未涵蓋者，回報 Commander 補做偵察，不得自行猜測。**
> 升級 `gemini-webapi` 版本時必須重跑 probe 並逐項比對本檔。

---

## 零、import 路徑約定

**一律使用 top-level 匯入**，不要用內部模組路徑：

```python
from gemini_webapi import GeminiClient, ChatSession          # 客戶端
from gemini_webapi import ModelOutput, Candidate, Image      # 型別
from gemini_webapi import DeepResearchPlan, DeepResearchResult
from gemini_webapi import exceptions as gw_exc               # 例外
from gemini_webapi.utils import clear_cookies_cache, save_cookies
```

`gemini_webapi` 與 `gemini_webapi.types` 皆匯出下列型別（實測確認，兩者為同一物件）：

```
AvailableModel, Candidate, ChatHistory, ChatInfo, ChatTurn, Citation,
DeepResearchDocument, DeepResearchPlan, DeepResearchResult, Gem, GemJar,
GeneratedImage, GeneratedMedia, GeneratedVideo, Image, ModelOutput,
RPCData, Video, WebImage
```

`docs/upstream-api.json` 記錄的 `qualified_name`（如
`gemini_webapi.types.research.DeepResearchPlan`）是**反射得到的真實位置，供辨識用**，
不是建議的 import 路徑 —— 內部模組路徑屬實作細節，升級時較可能搬動。

pydantic 型別一律用其自身的序列化（`model_dump_json` / `model_validate_json`），
**不要手刻解碼**：手刻會在上游新增欄位時靜默丟失資料。

---

## 一、例外階層（T2.1 錯誤分類的依據）

### ⚠️ 最重要的陷阱：兩個互不相干的根

`APIError` 與 `GeminiError` **都直接繼承 `Exception`，彼此無繼承關係**。

```
Exception
├── APIError
│   └── ImageGenerationError
└── GeminiError
    ├── ModelInvalidError
    ├── TemporarilyBlockedError
    ├── TimeoutError            ← 注意：遮蔽 builtins.TimeoutError
    └── UsageLimitExceededError

AuthError → 直接繼承 Exception（不屬於上述任一根）
```

**因此：`except GeminiError` 抓不到 `APIError` / `ImageGenerationError`，
`except APIError` 也抓不到任何 `GeminiError` 子類。攔截時必須同時列出兩個根。**

`gemini_webapi.exceptions.TimeoutError` 會遮蔽 builtins 的同名例外，
import 時務必使用限定名稱（如 `from gemini_webapi import exceptions as gw_exc`
再用 `gw_exc.TimeoutError`），避免與 `asyncio.TimeoutError` 混淆。

### 分類對照表（Commander 裁定，T2.1 據此實作）

上游**沒有** `RateLimitError` 這個類別。限流語意由下列兩者承擔：

| 分類 | 對應例外 | 處置 |
|---|---|---|
| **Auth** | `AuthError` | 進入 `DEGRADED`、停止接受新請求、推播管理員「認證失效，請 /setcookie」 |
| **RateLimit** | `UsageLimitExceededError`、`TemporarilyBlockedError` | 指數退避重試最多 2 次；仍失敗告知使用者稍後再試，記 `error_kind=rate_limit` |
| **Transient** | `gemini_webapi.exceptions.TimeoutError`、`asyncio.TimeoutError`、連線中斷類（`curl_cffi` / OSError） | 退避重試最多 2 次 |
| **Fatal** | `ModelInvalidError`、`ImageGenerationError`、`APIError`、`GeminiError`（基底兜底） | 不重試，回報使用者明確訊息（不得吐 stack trace） |

`TemporarilyBlockedError` 歸為 RateLimit 而非 Fatal：它是暫時性封鎖，退避後可能恢復。

#### DEGRADED 升級政策（Commander 裁定，T2.2 實作）

DEGRADED 有兩種成因，**必須可區分**（`DegradedReason.AUTH` / `DegradedReason.BLOCKED`），
因為兩者的恢復路徑完全不同：

| 成因 | 觸發 | 恢復方式 | 推播語氣 |
|---|---|---|---|
| `AUTH` | `AuthError`，**立即**升級 | **僅能**由 `reinit()`（管理員 `/setcookie`）恢復；不會隨時間自行恢復 | 「認證失效，請 /setcookie」 |
| `BLOCKED` | **連續 3 次** `TemporarilyBlockedError`（`BLOCKED_ESCALATION_THRESHOLD`） | `blocked_until = now + 900s` 到期後 half-open 探測，成功即回 HEALTHY | 「暫時封鎖，約 N 分鐘後自動重試，無需人工介入」 |

- 計數器語義為「**連續**」：任何一次成功請求即歸零。
- `UsageLimitExceededError` **不**累加此計數器（配額問題，非封鎖）。
- 時間來源需可注入（`time_source`，預設 `time.monotonic`），讓測試不必 sleep。

理由：AUTH 是憑證問題，等待無意義，必須人工換 cookie；BLOCKED 是上游節流，
會自行恢復，若也要求人工介入只會製造無謂的管理員噪音。

### 傳輸層例外 — `curl_cffi` 0.16.3

`gemini_webapi` 底層以 `curl_cffi` 發送請求，其例外會穿透上來。

**實測繼承事實：**

```
curl_cffi.curl.CurlError                    → Exception            （不是 OSError！）
curl_cffi.requests.exceptions.RequestException → CurlError, OSError   （雙重繼承）
    ├── Timeout ── ConnectTimeout / ReadTimeout
    ├── ConnectionError ── DNSError / SSLError ── CertificateVerifyError
    ├── ProxyError / ChunkedEncodingError / IncompleteRead
    └── InvalidURL / MissingSchema / InvalidSchema / ImpersonateError / ...
```

- 因為 `RequestException` 繼承 `OSError`，**所有 requests 層例外都已是 `OSError` 子類**。
- 但 **裸 `CurlError` 不是 `OSError`**，單用 `except OSError` 會漏接。

**T2.1 實作指定：**

```python
import asyncio
from curl_cffi.curl import CurlError
from curl_cffi.requests import exceptions as cc_exc

FATAL_TRANSPORT = (          # 設定／程式錯誤，重試無意義
    cc_exc.InvalidURL, cc_exc.MissingSchema, cc_exc.InvalidSchema,
    cc_exc.URLRequired, cc_exc.InvalidHeader, cc_exc.ImpersonateError,
)
TRANSIENT = (
    cc_exc.Timeout, cc_exc.ConnectionError, cc_exc.ChunkedEncodingError,
    cc_exc.IncompleteRead, cc_exc.ProxyError,
    CurlError, asyncio.TimeoutError, OSError,   # 兜底，必須放最後
)
```

**兩個必守的順序規則：**

1. **Fatal 的比對必須早於 Transient。** `InvalidURL` 繼承 `RequestException → CurlError`，
   若先比 Transient 會被 `CurlError` 兜底吃掉而錯誤地重試。
2. **`gemini_webapi` 的分類優先於 `curl_cffi`。** 先判 `AuthError` /
   `UsageLimitExceededError` / `TemporarilyBlockedError` / `gw_exc.TimeoutError`，
   再落到傳輸層。

**命名遮蔽**：`cc_exc.ConnectionError` 與 `cc_exc.Timeout` 會遮蔽 builtins 同名例外，
務必以限定名稱使用，不可 `from curl_cffi.requests.exceptions import ConnectionError`。

---

## 二、`GeminiClient`

`gemini_webapi.client.GeminiClient` — **全程唯一實例（不變量 2）**。

### 公開方法（完整清單）

```
close, create_deep_research_plan, create_gem, deep_research, delete_chat, delete_gem,
fetch_gems, fetch_latest_chat_response, generate_content, generate_content_stream,
init, list_chats, list_models, read_chat, reset_close_task, resolve_model,
start_activity_watchdog, start_auto_refresh, start_chat, start_deep_research,
update_gem, wait_for_deep_research
```

### 核心簽章

| 方法 | 型態 | 簽章重點 |
|---|---|---|
| `init` | **async** | `(timeout=450, auto_close=False, close_delay=450, auto_refresh=True, refresh_interval=600, watchdog_timeout=120, impersonate='chrome145', verbose=False) -> None` |
| `start_chat` | **sync** | `(**kwargs) -> ChatSession` — 注意**不是** coroutine，不要 await |
| `generate_content` | **async** | `(prompt, files=None, model=None, gem=None, chat=None, temporary=False, deep_research=False, extended_thinking=False, **kwargs) -> ModelOutput` — 註：`extended_thinking` 為布林值，僅對應 Low/High，詳見 [二之二](#二之二思考等級網頁版三段編碼與本專案的轉接層2026-10-11) |
| `generate_content_stream` | **async generator** | 同上參數 → `AsyncGenerator[ModelOutput, None]` |
| `list_models` | **sync** | `() -> list[AvailableModel] \| None` — **可能回傳 `None`，必須處理** |
| `resolve_model` | **sync** | `(name: str) -> AvailableModel` |
| `fetch_gems` | **async** | `(include_hidden=False, **kwargs) -> GemJar` |
| `start_auto_refresh` | **async** | `() -> None` |
| `close` | **async** | `(delay: float = 0) -> None` |

> 註：`extended_thinking` 為布林值（`bool`），僅能對應 Low 與 High，無法原生表達 Medium；詳見 [二之二、思考等級](#二之二思考等級網頁版三段編碼與本專案的轉接層2026-10-11)。

### 建構子與 cookie 持久化

```python
GeminiClient(
    secure_1psid: str | None = None,
    secure_1psidts: str | None = None,
    proxy: str | None = None,
    **kwargs,
)
```

#### ⚠️ `GEMINI_COOKIE_PATH` 是環境變數，不是建構子參數

`gemini_webapi/utils/rotate_1psidts.py` 內部自行讀取，且為 **lazy**（每次輪替才讀）：

```python
_path = os.getenv("GEMINI_COOKIE_PATH")
return Path(_path) if _path else Path(tempfile.gettempdir()) / "gemini_webapi"
```

- 正確作法：**process 啟動時把設定值寫入 `os.environ["GEMINI_COOKIE_PATH"]`**，
  再建立 client。
- 錯誤作法：當成參數傳給 `GeminiClient(...)` — 會被 `**kwargs` 吞掉而**靜默失效**。
- 未設定時落在 `tempdir`，容器重建即失效 → 這是 spec 要求掛 volume 的直接原因。

#### ⚠️ 快取 cookie 的優先序高於呼叫端提供的憑證

`init()` 會先載入快取檔（`utils/get_access_token.py` 的 `_load_cached_jar`），
**順序在建構子傳入的憑證之前**。上游原始碼註解：

> Cached cookies are tried ahead of the ones the caller supplies…
> or create the very entry that shadows real credentials on the next run.

**對 `/setcookie` 的直接後果**：管理員換上新的 `1PSIDTS`（`1PSID` 不變）時，
舊快取檔會**遮蔽新值**，新 client 仍拿到過期憑證，服務續留 `DEGRADED`，
看起來像 `/setcookie` 沒有作用。

上游確有自癒：偵測到 `_cookie_source` 來自 Cache 且 `AccountStatus.UNAUTHENTICATED`
時會呼叫 `clear_cookies_cache()`。但那需要**先失敗一輪**，對 G2 演練而言即為失敗。

**因此 `reinit()` 在帶入新憑證時，必須先清舊快取：**

```python
from gemini_webapi.utils import clear_cookies_cache

if new_credentials is not None and self._client is not None:
    try:
        clear_cookies_cache(self._client.cookies)  # 需舊 client 的 jar → 必須在 close() 前
    except Exception:
        logger.warning("failed to clear cookie cache during reinit")  # 不得中斷 reinit
await self._client.close()
```

- `clear_cookies_cache(cookies: Cookies, verbose: bool = False)` 取的是 curl_cffi 的
  `Cookies` jar，可由 `client.cookies` 取得。
- **不帶新憑證的 reinit（單純重啟）不可清快取** —— 快取內常是最新鮮的輪替值。
- `save_cookies(cookies: Cookies, verbose: bool = False)` 同樣吃 jar，
  格式為 `{name, value, domain, path, expires}` 的 JSON list。**不要手刻此格式。**

#### `__Secure-1PSID` 是唯一必要的憑證，`1PSIDTS` 可為空

`utils/get_access_token.py` 組 candidate jar 的階段（Phase 1，離線、不發請求）
一切以 `base_psid` 為條件：

```python
psidts = _extract_cookie_value(jar, "__Secure-1PSIDTS") or ""   # 空字串是合法候選

if base_psid:                                                    # 只看 1PSID
    if (base_psidts or "") not in tried_sessions.get(base_psid, set()):
        register(Cookies(base_jar), "Base Cookies", base_psid)
elif verbose and not cookie_jars_to_test:
    logger.debug("Skipping loading base cookies. __Secure-1PSID is not provided.")
```

連線成功後由 `rotate_1psidts()` 換發新的 `1PSIDTS` 並寫入快取。

**運維後果**：`1PSIDTS` 過期或值錯誤**不會**造成認證失敗，只要 `1PSID` 仍有效。
需要人工重新取得 cookie 的情形，只有 `1PSID` 本身失效。

> 已於正式環境實測（2026-09-08）：把 `GEMINI_SECURE_1PSIDTS` 改成無效字串、
> 並清空 cookie 快取目錄後重啟，client 仍正常完成認證並服務請求。
> 上游失敗訊息把矛頭指向 `SECURE_1PSIDTS`（"could get expired frequently"），
> **具誤導性** —— 真正要換的通常是 `1PSID`。

#### ⚠️ 本機瀏覽器 cookie 是隱性的後備來源

快取與呼叫端憑證之後，上游還會嘗試 `load_browser_cookies(domain_name=...)`，
只採用 `__Secure-1PSID` 與呼叫端相符的 jar：

```python
if base_psid and base_psid != secure_1psid:
    continue        # 不相符才跳過；相符就會被採用
```

**運維後果**：在**開發機**上，若同一個 Google 帳號正登入瀏覽器，
即使 `.env` 的憑證是錯的，client 仍可能從瀏覽器取得可用 jar 而通過認證 ——
**本機測試會通過，VM 上卻失敗**。無頭 VM 沒有瀏覽器 profile，此路徑為空轉。

診斷憑證問題時，`InitSession.cookie_source` 會標明實際來源
（`Cache` / `Base Cookies` / `Cache (Latest)` / 瀏覽器名稱）。

#### 快取檔路徑含有 cookie 明文

```
{GEMINI_COOKIE_PATH}/.cached_cookies_{__Secure-1PSID}.json
```

**檔名本身帶有 1PSID 原始值。** 因此此路徑**不得寫入 log**（不變量 3），
且目錄權限需收斂。

#### reinit 語義（Commander 裁定）

必須 **close 後重建新物件**，不可對同一物件重複呼叫 `init()`：

```python
if self._client is not None:
    await self._client.close()     # async, (delay: float = 0)
self._client = GeminiClient(...)   # src/ 內唯一的建構點
await self._client.init(...)       # async, auto_refresh 預設 True
```

只 `init` 不 `close` 會留下孤兒 `AsyncSession` 與重複的背景 refresh task，
正是不變量 2 要防的「多重輪替互相作廢」。

**不變量 2 的正確解讀**：同時只有一個存活實例，且 `src/` 內只有**一處**
`GeminiClient(...)` 建構點。經同一工廠方法重建新實例是允許的。

相關工具：`gemini_webapi.utils` 匯出 `clear_cookies_cache(cookies)` 與 `save_cookies`，
T3.4 的 `/setcookie` 熱更新可能需要。

**要點**

- `auto_refresh` 預設就是 `True`，`refresh_interval` 預設 600 秒。此即不變量 2 的成因：
  多 process 共用同一組 cookie 會互相輪替作廢。
- `start_chat` 是同步方法，`await client.start_chat()` 會出錯。
- `list_models()` 是同步且**可能回傳 `None`**。T2.5 的 `/model` 指令必須處理 `None`
  （例如回覆「模型清單暫時無法取得」），不得假設一定是 list。
- `impersonate` 預設 `'chrome145'`。專案的 cookie 取得流程使用 Firefox（見 spec），
  兩者不一致並不影響 cookie 有效性，但若日後出現指紋問題，此為可調參數。

---

## 二之二、⚠️ 思考等級：網頁版三段編碼與本專案的轉接層（2026-10-11）

> 由 Commander 於 2026-10-11 親自執行偵察（HUMAN-gated）：擷取 gemini.google.com 的四次 StreamGenerate 真實請求確認。

### 傳輸層編碼對照表

思考等級在 HTTP 請求的兩處欄位中傳遞，兩者數值必須一致：

| Level | `f.req` inner list, index 80 | `x-goog-ext-525001261-jspb`, element right after the model number |
|---|---|---|
| Low | 1 | 1 |
| Medium | 4 | 4 |
| High | 2 | 2 |

> **注意**：`f.req` inner list 索引 17 是每次對話輪次的計數器（per-conversation turn counter），並非思考等級。

### 4 Argon（2026-10-14 擷取）

> 偵察背景：Gemini 網頁版現已推出第四個模型「4 Argon」。Commander 依使用者明確指示，於使用者的瀏覽器 session 中親自操作頁面並送出測試 prompts 進行偵察（HUMAN-gated）。

#### 模型選擇器（Model picker）

網頁版模型選擇器列出四個模型及其說明：
- **3.5 Flash-Lite**（Fastest answers）
- **3.8 Flash**（All-around help）
- **3.1 Pro**（Advanced reasoning）
- **4 Argon**（Complex tasks）

針對 4 Argon，「Thinking level」（思考等級）子選單提供 Low（Quick and efficient）、Medium（Balanced depth）與 High（Extra thorough）三檔，網頁 UI 預設為 Low。

#### User-status RPC（`otAQ7b`）

官方 User-status RPC `otAQ7b`（從 `part_body[15]` 讀取）包含下列模型項目：

| model_id | category ([1]) | display ([11]) | description ([12]) | [17] | [9] |
|---|---|---|---|---|---|
| 8c46e95b1a07cecc | Flash-Lite | 3.5 Flash-Lite | Fastest answers | 6 | 1 |
| 56fdd199312815e2 | Flash | 3.8 Flash | All-around help | 1 | 20 |
| e6fa609c3fa255c0 | Pro | 3.1 Pro | Advanced reasoning | 3 | 4 |
| 95a4c0abcba24364 | Argon | 4 Argon | Complex tasks | 8 | 4 |

回應中亦包含 `part_body[24]`（提及 Argon 的 mode-picker 清單），上游 `gemini-webapi` 2.1.1 並未讀取 `[24]`。

#### 上游解析機制

- `AvailableModel.from_rpc`（`gemini_webapi/types/availablemodel.py`）通用解析每一個 `[15]` 項目。`_derive_name_and_aliases` 將 category "Argon" 轉換為標準名稱 `gemini-argon`，並將 `[17]` = 8 作為模型編號（model number）。
- `client.py` 約第 943 行的 `list_models()` 會回傳於 `init()` 時建置的模型註冊表（`_model_registry`）。
- 因此 Bot 在下次 client 初始化（`init()`）後即可直接列出 "4 Argon"，無需修改程式碼。

#### StreamGenerate 擷取與思考等級

在 4 Argon 上分別針對三個思考等級各取樣一次：

| Level | `f.req` inner index 80 | model header index 15 |
|---|---|---|
| Low | 1 | 1 |
| Medium | 4 | 4 |
| High | 2 | 2 |

- 這些數值與 3.x 模型完全一致，因此本專案的思考等級轉接層（thinking-level adapter）適用且無需變更：改寫 inner index 80 以及 model header 中位於 session id 前的元素。
- 在官方 header（共 20 個元素）中，索引 4 為 model id `95a4c0abcba24364`，索引 11 為 2，索引 14 為 8（模型編號），索引 15 為思考等級，索引 16 為 session id。

#### 與上游 Header 之差異

對比上游實作（`constants.py` 約第 69 行，以及 `client.py` 約第 1513–1514 行附加的 flag 與 session id）：
- 上游在索引 8 的 capability list 為 `[4,5,6,8]`；官方真實 header 則為 `[4,5,6,8,16,19,20,4,5,6,8,16,19,20]`。
- 上游組出的 header 共有 17 個元素；官方 header 則有 20 個元素。
- 現有 3.x 模型在上游 header 存在相同差異的情況下已能正常運作。
- **驗證狀態**：Bot 透過上游向 Argon 發送請求的行為目前仍待驗證（pending verification）。

### 上游 `gemini-webapi` 2.1.1 的限制

上游 `gemini-webapi` 2.1.1 僅提供 `extended_thinking: bool` 參數：

- `client.py` 第 1501 行：`extended_thinking` 為 `True` 時於 `inner[80]` 寫入 2，否則寫入 1。
- `client.py` 第 1513–1514 行：在 model header（`x-goog-ext-525001261-jspb`）中、session id 前附加相同數值（`2` 或 `1`）。

因此上游原生僅能表達 Low（1）與 High（2），無法送出 Medium（4）。

### 本專案轉接層（`ThinkingLevelGeminiClient`）機制與容錯

本專案於 `src/gemini_tg_bot/gemini/thinking.py` 實作轉接層：

- **Singleton 不變量**：`ThinkingLevelGeminiClient` 繼承 `GeminiClient`，僅覆寫 `_live_client` property。全專案唯一的建構點仍在 `service.py::_create_client`，維持不變量 2。
- **ContextVar 作用域**：遇到 Medium 等級時，`thinking_request(ThinkingLevel.MEDIUM)` 向上游請求 High（`extended_thinking=True`），同時設定一個 `ContextVar`。在該 ContextVar 作用期間，發往 `Endpoint.GENERATE` 的 StreamGenerate 請求會被攔截，並將兩處欄位的數值由 2（High）改寫為 4（Medium）。
- **完全隔離背景任務**：透過 `ContextVar` 隔離請求作用域，非 Medium 請求以及背景任務（如 cookie 輪替）取得的皆為未經改寫的原生 session，不受任何影響。
- **改寫為 all-or-nothing 且 fail-safe**：改寫前會嚴格檢查 `f.req` 與 header 的結構。若 payload 形狀不符合預期（如上游結構漂移），轉接層會記錄 `WARNING`，並將請求維持原樣（以 High 送出），絕不會因為轉接層拋出例外導致使用者請求失敗。

### 回應端：各等級是否回傳思考內容（2026-10-12 擷取）

本專案之 Bot 在 Medium 等級未顯示思考區塊。為釐清原因，Commander 於 2026-10-12 以被動式頁面監聽（passive in-page monitor）擷取官方 gemini.google.com 的 StreamGenerate 回應。此擷取為 HUMAN-gated（由使用者選擇等級並點擊送出）。測試模型為 3.8 Flash，相同 prompt 於 Medium 與 High 各取樣一次（Low 未進行擷取，故不對 Low 的回應作任何陳述）。

| Level | Request value | Thought text in the response |
|---|---|---|
| Medium | `f.req` inner list 索引 80 = 4，model header = 4 | 每個 response candidate 均有索引 37，但 `candidate[37][0]` 為 `null`；整份回應完全未包含思考文字，唯一的長文字即為回答。官方網頁在該輪亦未顯示思考面板，僅有「Show code」與回答。 |
| High | 數值為 2 與 2 | `candidate[37][0][0]` 包含思考文字（該取樣中為 654 字元）。此欄位即上游 `gemini-webapi` 2.1.1 於 `client.py` 第 2113 行所讀取之位置：`thoughts = get_nested_value(candidate_data, [37, 0, 0]) or ""`。 |

**思考內容回傳取決於 Prompt（2026-10-13 實測修正）**：

- **2026-10-12 初始取樣**：當時以簡單算術 prompt 測試，Medium 未回傳思考文字（0 字元），且官方網頁亦未顯示思考面板。早期推論曾認為 Medium 原則上不回傳思考文字，此推論已被證實不正確。
- **2026-10-13 正式環境實測**：一筆正式環境的私聊請求（Medium）成功回傳了 328 字元的思考內容，Bot 亦正常呈現思考區塊。該請求確實以 Medium 發出（日誌中未記錄任何 fallback WARNING）。
- **結論**：Medium 等級是否回傳思考文字**取決於 Prompt**（prompt-dependent），而非 Medium 等級一律回傳 0 字元。當 Prompt 觸發模型的深入推理時，Gemini 仍會在 `candidate[37][0][0]` 中回傳思考文字。
- **對本專案的影響**：
  - **轉接層正常運作**：無論是 2026-10-12 回傳 0 字元，還是 2026-10-13 回傳 328 字元，Bot 向上游發出的 Medium 請求皆維持在 Medium，未發生回退至 High 的情況。
  - **自動相容呈現**：上游 `gemini-webapi` 2.1.1 於 `client.py` 第 2113 行讀取 `candidate[37][0][0]` 作為 `thoughts`；當 Gemini 回傳思考文字時，Bot 即正常顯示思考區塊；若回傳 0 字元，Bot 則僅輸出回覆文字並記錄 INFO 日誌，不需特殊分支處理。
  - **使用者決策（2026-10-12）**：Gemini 未回傳思考內容時，Bot 不另外顯示耗時行，維持現狀。

### 版本鎖定與升級流程

- **版本釘選**：`gemini-webapi` 於 `pyproject.toml` 嚴格鎖定為 `==2.1.1`，因正式環境透過 `uv pip install` 安裝會忽略 `uv.lock`。測試 `tests/test_thinking.py::test_upstream_version_pin` 會在安裝版本不符時失敗。
- **升級流程**：日後升級 `gemini-webapi` 時，必須重新檢視新版本原始碼確認傳輸層行為，並重跑 `tests/test_thinking.py`。若上游未來原生支援思考等級參數，應移除轉接層並改用上游官方參數。

---

## 二之三、串流停滯、找回與重試（2026-10-13）

上游 `gemini-webapi` 2.1.1 在處理長串流或停滯時具備自動恢復與重試機制，但在高延遲或併發場景下會引發顯著的副作用。

### 上游機制與原始碼分析

依據 `.venv/lib/python3.12/site-packages/gemini_webapi/` 原始碼實測確認之機制：

1. **Watchdog（看門狗停滯偵測）**：
   串流讀取迴圈（`client.py` 第 1825–1877 行）中，當串流在 `stall_threshold` 內未產出新內容（若模型未標記為 `is_thinking` 或 `is_queueing`，則門檻為 `min(self.timeout, self.watchdog_timeout)`，預設 120 秒），watchdog 會記錄 DEBUG 日誌（`client.py` 第 1871–1874 行）並呼叫 `await self.close()`（第 1875 行），主動中斷串流並進入找回流程。
2. **Recovery（對話歷史找回）**：
   若串流未完成且具有對話 ID（`cid`）（`client.py` 第 1887–1930 行），上游會以 `read_chat(cid)` 輪詢對話歷史，輪詢上限 `recovery_deadline` 預設為 `min(timeout, watchdog_timeout)`（即 120 秒，第 1906–1910 行）。若輪詢逾時且先前未產生任何內文（`has_generated_text` 為 `False`），則拋出 `APIError`（第 1926–1929 行；若已有部分內文則拋出 `GeminiError`）。
3. **Retries（重試與退避）**：
   串流生成方法 `_generate` 由 `@running(retry=5)` 修飾（`client.py` 第 1376 行）。當捕獲 `APIError` 時，裝飾器會自動進行重試（`utils/decorators.py` 第 49–59 行）：
   - 退避延遲係數為 `_DELAY_FACTOR = 5`（`utils/decorators.py` 第 8 行），5 次重試分別等待 5、10、15、20、25 秒（`delay = (retry - current_retry + 1) * _DELAY_FACTOR`，第 51 行）。
   - 每次重試前，若 client 不處於運行狀態（因 watchdog 已呼叫 `close()`），裝飾器會重新呼叫 `await client.init(...)`（第 31–40 行）重建連線。
4. **Shared Session（共用 HTTP Session 截斷效應）**：
   `GeminiClient.close()` 會直接關閉底層的共用 HTTP 連線工作階段（`client.py` 第 352–406 行，特別是第 384–387 行 `await self.client.close()`）。當併發數（`MAX_CONCURRENCY`）大於 1 時，若其中一筆請求因停滯觸發 watchdog 或拋錯而呼叫 `close()`，**其他正在併發串流的請求連線將被直接切斷**。
5. **多併發下失敗請求的行為**：
   當某筆請求停滯觸發 `self.close()` 時，共用 session 被關閉，其他正在串流的併發請求會在讀取 chunk 時收到傳輸層例外。上游在 `client.py` 第 2053–2064 行以 `except Exception` 將這類例外轉換為 `APIError`，因此這些請求同樣會被 `@running(retry=5)` 退避後重新 `init()` 並重送 prompt（新對話的第一則訊息會產生新的 conversation id）。例外是 `ReadTimeout`：第 2041–2045 行將其轉為 `TimeoutError`，不會重試。也就是說，單一請求停滯會讓其他併發請求被迫重送、延長完成時間，而不是直接失敗；本專案以 `REQUEST_TIMEOUT_SEC` 限制每筆請求的總耗時。

### 單一請求最壞耗時約 30 分鐘

在最壞情況下，一筆請求若用盡 5 次重試（共 6 次嘗試）：
- 每次失敗嘗試經歷：約 120 秒串流無進展 + 約 120 秒 `read_chat` 找回逾時 = 約 240 秒。
- 5 次失敗重試累積等待：約 `5 * 240 = 1200` 秒。
- 5 次重試之指數退避延遲：`5 + 10 + 15 + 20 + 25 = 75` 秒。
- 5 次失敗合計約 1275 秒（超過 21 分鐘）。
- 加上第 6 次嘗試之執行時間（或各階段網路逾時），單一請求耗時可長達約 30 分鐘。

### 正式環境觀測紀錄

Commander 於正式環境日誌中記錄之真實案例：

- **2026-10-13 私聊請求**：
  一筆私聊請求（Medium）耗時長達 29.6 分鐘，共進行了 6 次嘗試。前 5 次失敗嘗試中，每筆皆是在約 120 秒未收到新內容後，進入對話歷史輪詢 120 秒未果；第 6 次嘗試則於 2 分 19 秒成功完成。由於該請求為新對話的第一則訊息，每一次重試均建立了全新的對話，日誌中的 WARNING 訊息出現了 5 個互不相同的 conversation id。
- **2026-10-11 01:34–01:40 UTC 群組請求**：
  在尚未引入思考等級轉接層的舊版本上，一筆群組請求耗時 329 秒。其 DEBUG 追蹤顯示：
  - `"Connection idle for 120s (queueing=False)"`
  - 12 次 `"Response not ready"` 輪詢與 recovery 逾時
  - 隨後的重試在 10 秒內完成。

### 觀測詮釋

- **【詮釋／推論】**：Gemini 偶發性的串流停滯現象早已存在，早在思考等級轉接層引入之前就已發生，並非轉接層所引起。

### 併發影響與本專案防護措施

1. **併發風險**：上游 `GeminiClient` 全局共用單一底層 HTTP session，單一請求停滯引發的 `close()` 會中斷其他進行中的併發請求，使其退避後重送。單一 Gemini 帳號建議 `MAX_CONCURRENCY` 不超過 3。**使用者決策（2026-10-13）**：正式環境設為 3。
2. **總逾時防護（`REQUEST_TIMEOUT_SEC`）**：為避免單一請求陷入長達 30 分鐘的停滯重試輪迴並霸佔佇列槽位，本專案現已引入 `REQUEST_TIMEOUT_SEC`（預設 600 秒）。當互動式請求自取得槽位起的總耗時超過限制時，主動中斷請求並提示使用者重新發送，保護系統可用性。

---

## 三、`ChatSession`

`gemini_webapi.client.ChatSession`

```python
ChatSession(
    geminiclient: GeminiClient,
    metadata: list[str | None] | None = None,
    cid: str = '',
    rid: str = '',
    rcid: str = '',
    model: AvailableModel | Model | str | dict | None = None,
    gem: Gem | str | None = None,
)
```

### ⚠️ `metadata` 的實際型別 — 決定 SQLite 序列化方式

```
actual_type:            list[str | None]
runtime_type:           builtins.list
runtime_element_types:  [builtins.str, builtins.NoneType]
runtime_length:         10
descriptor_type:        builtins.property
```

**`metadata` 是 `list[str | None]`（一個可含 `None` 的字串陣列），不是 dict。**

T1.2 / T2.3 的 `chat_sessions.metadata_json` 實作要求：

- 以 `json.dumps(metadata)` 存成 **JSON array**，`None` 序列化為 `null`。
- 還原時 `json.loads` 得回 list，**必須保留順序與 `None` 空位**，
  不得以 `filter(None, ...)` 或 dict 轉換壓縮，否則位置語意會錯亂。
- 觀察到的 runtime 長度為 10，但**不得硬編碼長度**，以實際回傳為準。

### 公開方法

| 方法 | 型態 | 簽章 |
|---|---|---|
| `send_message` | **async** | `(prompt, files=None, temporary=False, deep_research=False, extended_thinking=False, **kwargs) -> ModelOutput` |
| `send_message_stream` | **async generator** | 同上 → `AsyncGenerator[ModelOutput, None]` |
| `read_history` | **async** | `(limit: int = 10) -> ChatHistory \| None` |
| `choose_candidate` | **sync** | `(index: int) -> ModelOutput` |

---

## 四、`ModelOutput`

`gemini_webapi.types.modeloutput.ModelOutput`

```python
ModelOutput(*, metadata: list[str], candidates: list[Candidate], chosen: int = 0)
```

spec 點名要確認的欄位，**全部存在**：

| 屬性 | 型別 | 備註 |
|---|---|---|
| `text` | `str` | |
| `text_delta` | `str` | streaming 增量文字 |
| `thoughts` | `str \| None` | |
| `images` | `list[Image]` | |
| `videos` | `list[GeneratedVideo]` | |
| `media` | `list[GeneratedMedia]` | 與 `videos` **並存的獨立欄位**，勿混用 |
| `candidates` | `list[Candidate]` | `visible_in_dir=false`，`dir()` 看不到但確實存在 |
| `deep_research_document` | `DeepResearchDocument \| None` | |

### ⚠️ Streaming 的 chunk 邊界語意（spec 明列的不確定項，已確認）

`generate_content_stream` / `send_message_stream` 是 **async generator，
每次 yield 一個完整的 `ModelOutput` 物件**，不是裸字串。

T3.1 實作方式：

```python
async for chunk in client.generate_content_stream(...):
    delta = chunk.text_delta      # 增量；累加它
    full  = chunk.text            # 目前為止的完整文字
```

節流累積請針對 `text_delta` 累加，最後一次 edit 可直接採用最後一個 chunk 的 `text`。

---

## 四之二、⚠️ 回應含圖片時 `text` 的佔位符格式（上游 bug，實測確認）

> 此節補 T0.1 的偵察缺口。由 G1 實機回報的症狀反推 + 上游原始碼確認。

### 症狀

- `Generate an image...` → bot 只回 `_551`
- `台北101 長什麼樣子?附上照片` → 文字完整，但末尾殘留 `_0`

### 根因：上游 `ARTIFACTS_RE` 漏清

Gemini 在 `text` 中以 URL 形式嵌入圖片佔位符：

```
http://googleusercontent.com/image_generation_content/<a>_<b>
```

上游 `client.py` 於解析時會嘗試清除（`text = ARTIFACTS_RE.sub("", text)`），
但 `constants.py` 的樣式為：

```python
ARTIFACTS_RE = re.compile(r"https?://googleusercontent\.com/(?:\w+/)+\d+\n*")
```

其結尾只吃 `\d+`。當 Google 送出 `<數字>_<數字>` 形式的 id 時，
只有第一段數字被吃掉，**殘留 `_<數字>`**：

| 輸入 | `ARTIFACTS_RE.sub` 之後 |
|---|---|
| `.../image_generation_content/551` | `''`（乾淨，舊格式） |
| `.../image_generation_content/0_551` | `'_551'` ← **殘留** |
| `.../image_generation_content/1_0` | `'_0'` ← **殘留** |

**這是上游缺陷，不是本專案 handlers 的錯。** 版本 `gemini-webapi 2.1.1`。
升級上游時必須重測此節；若上游修好，本專案的補救仍應保留（無害且防回歸）。

### 本專案的補救（T3.2b 實作）

在 rendering / media 層再做一次清理，樣式需同時涵蓋新舊格式與殘留：

```python
# 完整 URL（含 <a>_<b> 形式的 id）
GOOGLEUSERCONTENT_ARTIFACT_RE = re.compile(
    r"https?://googleusercontent\.com/(?:\w+/)+\d+(?:_\d+)*\n*"
)
# 上游已吃掉 URL 前段後留下的孤兒殘骸，行首/獨立出現才算
ORPHAN_ARTIFACT_SUFFIX_RE = re.compile(r"(?m)^[ \t]*_\d+[ \t]*$\n?")
```

**順序**：先清完整 URL，再清孤兒殘骸。孤兒樣式必須夠保守
（限定整行只有 `_數字`），否則會誤刪正常文字中的底線片語。

### 佔位符與 `Image` 物件的對應

`GeneratedImage.image_id` 取自 `get_nested_value(gen_img_data, [1, 0])`，
**當該值不存在時，上游會退回填入同樣的佔位字串**：

```python
image_id = (
    get_nested_value(gen_img_data, [1, 0])
    or f"http://googleusercontent.com/image_generation_content/{img_idx}"
)
```

因此 `image_id` 有時可用來把 `text` 中的佔位符對應回具體圖片。
但**不可依賴**：`[1, 0]` 存在時 `image_id` 會是另一種值，且退回值用的是
`img_idx` 而非 Google 的原始 id。**正確作法是把圖片全部移除出 text，
另以 `sendPhoto` 送出，不要嘗試就地替換。**

### ⚠️ 第二種佔位符格式：XML 風格的 agent tag（2026-09-08 實機發現）

除了 googleusercontent URL 之外，Gemini **另有一種完全不同的佔位符格式**，
以 XML 風格的自閉合 tag 出現在 `text` 中。上游 `ARTIFACTS_RE` 完全不處理這類，
本專案原有的清理器也攔不到。

實機樣本（使用者回報，「台北101 長什麼樣子?附上照片」）：

```
<Image alt="從象山俯瞰台北101與台北市景觀" caption="台北101外觀與市景"
       src="image_agent_tag_17171708647064102964"/>

<FollowUp label="想了解台北101內部的阻尼球原理或觀景台參觀資訊嗎？"
          query="請介紹台北101的風阻尼球運作原理以及觀景台參觀重點。"/>
```

未清理時會被 HTML escape 成 `&lt;Image ...&gt;` 原樣顯示在 Telegram 訊息中。

#### Commander 裁定的處理方式

| tag | 處置 | 理由 |
|---|---|---|
| `<Image .../>` | **整個移除** | 對應的圖片已透過 `output.images` 以 `sendPhoto` 送出，就地替換沒有意義 |
| `<FollowUp .../>` | **整個移除**（含 `label`） | 使用者裁定（D13）：建議追問在 Telegram 情境是雜訊而非幫助 |
| 其他大寫自閉合 tag | **整個移除** | 通用樣式一併涵蓋 |

**因此不需要任何 tag 的特例處理** —— 單一通用樣式即可全部移除，實作更簡單。

移除後必須 **trim 前後多餘空白與連續空行**，否則會留下尾端空行。

#### 必須用通用樣式，不得逐一列舉 tag 名稱

Gemini 隨時可能新增其他 agent tag。請用保守的通用樣式比對
**首字母大寫的自閉合 tag**：

```python
AGENT_TAG_RE = re.compile(
    r'<[A-Z][A-Za-z0-9_]*(?:\s+[A-Za-z_][\w.-]*\s*=\s*"[^"]*")*\s*/>'
)
```

首字母大寫 + 自閉合 + `name="value"` 屬性的組合，足以與使用者可能討論的一般 HTML
（多為小寫、且通常包在 code fence 內）區隔。

#### 🔴 絕對不可觸碰 code fence 內的內容

若使用者問「HTML 的 `<Image src="x"/>` 是什麼意思」，該片段會出現在 code block 內，
**必須原樣保留**。已實測目前 code fence 內的 XML 有正確保留與 escape：

```
'說明：\n<pre><code class="language-html">&lt;Image src="x"/&gt;\n</code></pre>結束'
```

清理必須在**解析出 code fence 之後、只作用於 fence 外的文字**，
不得對整段原始文字直接做 regex 替換。

#### ✅ 實測樣本已回答的問題（2026-09-08，`tests/fixtures/`）

使用者以 `scripts/dump_response.py` 擷取兩份真實 `ModelOutput`。結論如下。

**兩種圖片來源使用完全不同的佔位符格式：**

| 來源 | `text` 中的佔位符 | `url` 網域 | 有 `image_id`？ |
|---|---|---|---|
| `GeneratedImage` | googleusercontent 殘留（`_543`） | `lh3.googleusercontent.com/gg-dl/...` | ✅ 有 |
| `WebImage` | XML `<Image src="image_agent_tag_NNNN"/>` | **第三方公開網址**（`travel.taipei`、`ctplayer.com`） | ❌ 無 |

**`image_id` 證實了 `<a>_<b>` 的推斷。** 實測值：

```
image_id = "http://googleusercontent.com/image_generation_content/0_543"
text     = "\n\n_543\n\n"
```

上游 `ARTIFACTS_RE` 只吃到 `0`，殘留 `_543`。與先前由症狀反推的結論完全一致。

**`WebImage` 沒有 `image_id`**，欄位僅 `url / title / alt / proxy`。
因此 XML tag 中的 `image_agent_tag_NNNN` **無法對應回具體圖片** ——
再次確認「只能移除佔位符、另以 `sendPhoto` 送出」，不可嘗試就地替換。

**對 T3.2a（egress）的直接影響：**
`WebImage` 指向第三方公開網址，Telegram 幾乎確定可直接抓取（零 egress 可行）。
`GeneratedImage` 指向 Google CDN 的 `gg-dl` 路徑，**很可能帶時效簽章或需認證**。
兩者行為極可能不同 → `web_image_mode` 與 `generated_image_mode`
必須維持為**獨立開關**，不可合併成單一設定。

**`alt` 不適合當 caption：**
`WebImage.alt` 為空字串；`GeneratedImage.alt` 是檔名
（`watermarked_img_3040285439948722320.jpg`）。兩者皆無敘述價值。

**上游品質觀察（非本專案缺陷，但需容忍）：**
- 單一 candidate 的 `text` 中出現**兩段串接的完整回答**，且第一段在
  「⋯呈現青竹般的藍」處被截斷後直接接上第二段。渲染層不應嘗試修復，照實呈現即可。
- 模型文字聲稱「目前環境無法直接提供即時圖片附件」，但 `web_images` 實際有 2 張圖。
  文字與 payload 不一致，**以 payload 為準**。

#### 尚未確認

- 是否存在**成對**形式（`<Tag ...>內容</Tag>`）而非只有自閉合。目前樣本只見自閉合。
  若實作時遇到成對形式，回報 Commander 補合約。
- `src="image_agent_tag_NNNN"` 的數字與 `output.images` 的順序是否有對應關係。
  目前不依賴此對應（圖片一律另外送出）。

---

### 尚待實機確認

- `<a>_<b>` 中兩段數字的實際語意（是否為 candidate index / image index）
- web search 來的圖（`WebImage`）是否也用同一種佔位符
- 一則回應含多張圖時，佔位符在 text 中的排列方式

→ 由 `scripts/dump_response.py` 擷取真實 `ModelOutput` 後補齊，並轉為
`tests/fixtures/` 的實測樣本，取代憑空捏造的 mock。

---

## 五、Image 類別

三個類別皆為 **pydantic `BaseModel`**。

| 類別 | 屬性 |
|---|---|
| `Image` | `url`(必填, str), `alt`(''), `title`('[Image]'), `client`, `proxy` |
| `WebImage` | 同 `Image` |
| `GeneratedImage` | `Image` 的欄位 + `cid`, `rid`, `rcid`, `image_id`, `client_ref` |

### `save()` — **是 async**

```python
async def save(
    path: str = 'temp',
    filename: str | None = None,
    verbose: bool = False,
    client: AsyncSession | None = None,
    **kwargs,
) -> str          # 回傳實際存檔路徑
```

`client` 參數型別為 `curl_cffi.requests.session.AsyncSession | None`。

**T3.2b 的 egress 判斷依據**：`Image.url` 是純 `str` 欄位，物件本身另帶 `client` / `proxy`
（暗示下載時可能需要帶 session）。**`url` 是否可被 Telegram 伺服器無認證抓取，
反射無法回答，必須由 T3.2a 的人工 live 實測決定** — 這正是該節點必須是 HUMAN gate 的原因。

---

## 五之二、影片生成（2026-10-14 擷取）

> 偵察背景：使用者詢問 Bot 是否可新增 `/video` 指令。Commander 依使用者明確指示，於使用者的 Chrome 瀏覽器 session 中親自操作頁面進行偵察（HUMAN-gated）。
> - 該 session 為使用者的 Google AI Pro 帳號，非 Bot 自身之帳號。
> - 本次測試共擷取並成功生成 1 次影片。
> - **驗證狀態**：Bot 端以 Bot 帳號發送請求之行為目前仍待驗證（pending verification）。

### 一、網頁介面操作流程

在 Chat 模式中，點擊「+」按鈕（`aria-label="Upload and tools"`）展開選單：
- Upload files、Add from Drive、More uploads
- Create image、Create video、Create music
- More tools

點擊「Create video」進入 Videos 頁面：
- 提供影片範本（video templates）；
- 輸入框（composer）顯示「Describe your video」，並附帶「Videos」chip；
- 模型選擇器（Flash）；
- 圖片參考按鈕（image-reference button）；
- 長寬比下拉選單（aspect-ratio dropdown）顯示「Landscape」。

本次測試使用 3.8 Flash 於 Low 等級，測試 prompt 為 `"A small paper boat drifting on a calm lake at sunset, gentle ripples, soft warm light"`。

### 二、請求結構（StreamGenerate）

請求發往與一般對話相同的 `StreamGenerate` 端點（`Endpoint.GENERATE`）。其 `f.req` inner list 共有 99 個元素。

以下為官方影片請求非空欄位與上游 2.1.1 一般請求（`client.py` 第 1454–1506 行建構之 81 元素 list）的對照表：

| `f.req` 內部索引 | 官方影片請求 | 上游 2.1.1 一般請求 |
|---|---|---|
| 0 (`message_content`) | 10 個元素：`[prompt, 0, null, null, null, null, 0, null, null, [null,null,null,null,null,null,[[null,null,null,1]]]]` | 7 個元素：`[prompt, 0, None, files, None, None, 0]` |
| 1 | `["en-GB"]` | `[language]` |
| 3 | `"!"` 後接 2224 字元的不透明字串（opaque string） | 僅在 `deep_research` 時設定：`"!" + secrets.token_urlsafe(2600)` |
| 4 | 32 字元 16 進位字串（hex characters） | 僅在 `deep_research` 時設定：`uuid4().hex` |
| 6, 7, 10, 11, 17, 18, 27, 41, 53 | `[1]`, `1`, `1`, `0`, `[[0]]`, `0`, `1`, `[1]`, `0` | 數值完全相同（其中索引 7 為 `STREAMING_FLAG_INDEX`） |
| 30 | `[4,16,19]` | `[4]` |
| 49 | `11` | 僅在 `deep_research` 時設定：`1` |
| 55 | `[[16]]` | 僅在 `deep_research` 時設定：`[[1]]` |
| 59 | 36 字元 UUID | 大寫 `uuid4` |
| 67 | `0` | 未設定（None） |
| 68 | `2` | `1` |
| 79 | `1`（3.8 Flash 之模型編號） | 模型編號（model number） |
| 80 | `1`（Low） | `1` 或 `2` |
| 91, 96, 98 | `1`, `0`, `1` | 未設定（上游 list 僅 81 個元素） |

#### 表格解讀與推論

- 本次未擷取官方 normal-chat 作為基準線（baseline），故哪些差異純屬影片專屬尚未嚴格證實。
- 類比上游 Deep Research 旗標（49 = 1 與 55 = `[[1]]`），索引 49 = 11、55 = `[[16]]` 以及 `message_content[9]` 最可能為影片專屬欄位。
- `message_content[9]` 亦可能承載長寬比（aspect ratio），此點尚待驗證。
- 其餘差異可能為官方網頁客戶端在所有請求中皆會送出的欄位。
- 未來實作應重現官方影片請求的每一個欄位。

### 三、即時回應

- **到達時間與長度**：送出後約 4 秒到達，總長約 956 字元。
- **結構**：sparse bundle 格式的 frames。
- **依序包含之內容**：
  1. Title frame：`{"11": ["Sunset Drift on Calm Waters"]}`。
  2. Text frame：`{"7": [...]}`：
     - 文字內容："I'm generating your video. This could take a few minutes, so check back to see when your video is ready."；
     - 接續工具標記（tool marker）：`["video_generation", [null, null, "Video Generation", <gstatic icon URL>], 1, ...]}`。
  3. 結尾 frame：key 為 `"46"`，包含一個 id 與空字串。
- **StreamGenerate 即時回應中不包含任何影片。**

### 四、完成與歷史讀取

- **時效**：送出後約 50 秒影片就緒。此為時間上限（upper bound），因頁面僅在該時間點重新讀取對話。
- **讀取 RPC**：頁面呼叫 read-chat RPC `hNvQHb`（`GRPC.LIST_CONVERSATION_TURNS`），payload 為 `[cid, 10, null, 1, [1], [4], null, 1]`。此與上游 `read_chat`（`components/chat_mixin.py` 第 151 行）完全一致。
- **影片在最新 model turn 的位置**：
  - 取得 candidate `conv_turn[3][0][0]`，進入其 rich-content container `[12]`。
  - 該 container 在索引 0 包含一個 sparse bundle。其 key `"60"` 即對應 `Field.VIDEO`（59），經上游 `get_field` 的 key 偏移 +1（`utils/parsing.py` 第 111–131 行，`bundle.get(str(index + 1))`）讀取。
  - `video_info = bundle["60"][0][0][0]`，此即上游路徑 `[0, 0, 0]`。
- **影片檔案項目（The video file entry）**：`video_info[0]` 共有 25 個元素：
  - `[2]` = `"video.mp4"`
  - `[11]` = `"video/mp4"`
  - `[24]` = `3530649`（位元組，約 3.5 MB）
  - `[7]` = 3 個 URLs：
    - `[0]`：googleusercontent 於 `/gg/` 路徑下的圖片 URL，無 query string（thumbnail 縮圖）；
    - `[1]`：`https://contribution.usercontent.google.com/download?…`，共 216 字元（含 164 字元 query string，影片本體下載網址）；
    - `[2]`：另一個 `/gg/` 圖片 URL，無 query string。
- **bundle 內的其他項目**：
  - `bundle["60"][0][0][2][0]` 為 70 字元的 `googleusercontent.com/generated_video_content/…` URL。
  - `bundle["60"][0][0][8]` 為第二個檔案項目：`"omni_context.pb"`、`"application/protobuf"`、3,537,040 位元組，其自身下載 URL 位於 `[8][7][1]`。
  - 相同影片結構亦出現在 payload 的另外兩處位置。

### 五、上游 2.1.1 現有支援度

- `read_chat` 透過 `_parse_candidate`（`client.py` 第 2069 行起）解析每個 candidate，建構 `GeneratedVideo(url=urls[1], thumbnail=urls[0], cid, rid, rcid, client_ref)`（`client.py` 第 2164–2181 行），影片會正確存入 `ModelOutput.videos`；URL `[2]` 則被忽略。
- `ChatHistory`（`types/chathistory.py` 第 33–53 行）具有 `cid` 與 `turns`；每個 `ChatTurn`（第 8–31 行）具有 `role`、`text` 與 `model_output`。
- `fetch_latest_chat_response(cid, limit=5, match=None)`（`components/chat_mixin.py` 第 270–300 行）可回傳符合條件之最新 model output。
- `GeneratedVideo.save()`（`types/video.py` 第 47–107 行、第 177–202 行）：
  - 使用 client 的 cookies（第 87 行）與 Referer header（第 125 行）下載。
  - 當伺服器回應 206（"still generating"）時，每 10 秒自動重試輪詢（第 177–201 行）。
- **上游完全沒有請求生成影片的參數**：`generate_content`、`generate_content_stream`、`send_message` 與 `send_message_stream` 皆無影片旗標。要送出官方影片請求，需要實作類似 `ThinkingLevelGeminiClient`（§二之二）的請求轉接層（request adapter）。

### 六、下載實測

Commander 於頁面內以 `Range: bytes=0-0` 對 `URL [1]` 進行 fetch 測試：
- 帶瀏覽器憑證時，回傳 206 `video/mp4`。
- 未帶憑證時，fetch 失敗。原因可能是 CORS 或身份認證，目前尚無定論（inconclusive）。

因此 Telegram 伺服器是否能直接抓取該 URL 仍屬未知。實作規劃需考慮兩種路徑：
- 由 Bot 帶 cookie 下載後上傳檔案至 Telegram（每部影片約 3.5 MB egress）；或
- 優先嘗試直接傳送 URL，失敗時自動回退至下載上傳路徑（如生成圖片之模式）。

### 七、配額（Quota）

- 公開資訊顯示 Google AI Pro 每日約 3 部影片，Ultra 約 5 部。
- 觀測到的 RPC `qpEbW`（payload `[[[2,5]]]`) 在生成前後回傳完全相同的數值，因此無法作為可用的剩餘配額計數器。
- Bot 自身使用的帳號與測試帳號不同，Bot 帳號是否具備影片生成權限仍待驗證。

### 八、尚待確認事項

- Bot 帳號端的實際請求驗證（HUMAN gate）。
- 達到每日配額上限時的錯誤回傳格式。
- 請求欄位中哪些為嚴格必填。
- 長寬比（aspect ratio）之編碼格式。

---

## 六、Deep Research（T3.3 依據）

| 方法 | 型態 | 簽章 |
|---|---|---|
| `create_deep_research_plan` | async | `(prompt, chat=None, model=None) -> DeepResearchPlan` |
| `start_deep_research` | async | `(plan: DeepResearchPlan, chat=None, confirm_prompt=None) -> ModelOutput` |
| `wait_for_deep_research` | async | `(plan: DeepResearchPlan, poll_interval=10.0, timeout=600.0) -> DeepResearchResult` |
| `deep_research` | async | `(prompt, poll_interval=10.0, timeout=600.0, model=None, chat=None) -> DeepResearchResult` — 一次做完的阻塞版 |

### Commander 裁定：必須走「拆開的三段式」，不得用一次性 `deep_research()`

spec 要求 `/research` **立即回傳 task id**、且**重啟後恢復輪詢**。
`deep_research()` 是阻塞式單一呼叫，無法滿足這兩點。正確流程：

```
create_deep_research_plan(prompt)  →  start_deep_research(plan)  →  [持久化]
                                          ↓（背景任務）
                                   wait_for_deep_research(plan, ...)
```

### 重啟恢復的關鍵事實

`wait_for_deep_research()` 需要 `DeepResearchPlan` 物件，而非只有 `research_id`。
所幸 **`DeepResearchPlan` 是 pydantic `BaseModel`**，欄位為：

```
research_id, title, query, steps, eta_text, confirm_prompt, modify_prompt,
confirmation_url, metadata, cid, response_text, raw_state
```

（注意其 `metadata` 同樣是 `list[str | None]`。）

**因此 T3.3 必須：**

- 以 `plan.model_dump_json()` 將**整個 plan** 持久化。
- spec 的 `research_tasks` schema 只有 `research_id` / `cid` / `prompt`，**不足以復原 plan**。
  T3.3 需新增一個欄位（建議 `plan_json TEXT`）承載完整 plan，並在 migration 中處理。
  此為對 spec schema 的**必要增補**，已由 Commander 核可，不算違反不變量 8。
- 啟動時對 `status='running'` 的列，以 `DeepResearchPlan.model_validate_json(plan_json)`
  復原後續傳 `wait_for_deep_research()`。

相關型別：`DeepResearchResult{plan, start_output, final_output, done}`、
`DeepResearchDocument{id, title, content, sources}`（皆為 pydantic BaseModel）。

---

## 七、`AvailableModel` / `Gem` / `GemJar`（T2.5 依據）

皆為 pydantic `BaseModel`（`GemJar` 為一般容器類別）。

```python
class AvailableModel:            # client.list_models() -> list[...] | None   （同步）
    model_id: str
    model_name: str              # ← 穩定識別，用這個
    display_name: str            # ← 給人看的
    description: str
    capacity: int
    capacity_field: int = 12
    model_number: int = 1
    is_available: bool = True
    aliases: list[str] = []

class Gem:                       # await client.fetch_gems(include_hidden=False) -> GemJar
    id: str
    name: str
    description: str | None = None
    prompt: str | None = None
    predefined: bool
```

`client.resolve_model(name: str) -> AvailableModel`（**同步**）可由 `model_name` 反查。

`GemJar` 方法：

```python
filter(predefined: bool | None = None, name: str | None = None) -> GemJar
get(id: str | None = None, name: str | None = None, default: Gem | None = None) -> Gem | None
keys() / values() / items()      # key 為 gem id
```

### ⚠️ inline keyboard 的 64-byte 約束（Commander 裁定）

Telegram `callback_data` 上限 **64 bytes**。因此：

- **不要**把 `model_id`、`description` 或清單索引放進 `callback_data`
  （索引會在重啟後錯位）。
- 模型：`callback_data = f"model:{model_name}"`，送出前驗證編碼後 ≤ 64 bytes，
  超過則略過並記 log。選定後以 `resolve_model(model_name)` 取回物件。
  `chat_sessions.model` 存 `model_name`，`NULL` = 帳號預設。
- Gem：`callback_data = f"gem:{gem.id}"`，同樣驗證長度。`chat_sessions.gem_id` 存 `gem.id`。
- 清單只顯示 `is_available` 為 `True` 者；顯示用 `display_name`，識別用 `model_name`。

---

## 七之二、`Candidate` 與 fixture 序列化（T3.2b 依據）

`Candidate` 為 pydantic `BaseModel`：

```python
class Candidate:
    rcid: str
    text: str
    text_delta: str | None = None
    thoughts: str | None = None
    thoughts_delta: str | None = None
    web_images: list[WebImage] = []
    generated_images: list[GeneratedImage] = []
    generated_videos: list[GeneratedVideo] = []
    generated_media: list[GeneratedMedia] = []
    citations: list[Citation] = []
    deep_research_plan: DeepResearchPlan | None = None
    deep_research_document: DeepResearchDocument | None = None

    @property
    def images(self) -> list[Image]:      # web_images + generated_images，順序固定
        ...
```

`ModelOutput(*, metadata: list[str], candidates: list[Candidate], chosen: int = 0)`。

`Candidate.text` / `thoughts` 有 `field_validator` 會做 `html.unescape()`，
因此拿到的已是解碼後的文字。

### ⚠️ 直接 `model_dump_json()` 在真實回應上會失敗

`Image.client` 的型別是 `curl_cffi.requests.session.AsyncSession`，
真實回應中它是**活的 session 物件**，不可序列化：

```
PydanticSerializationError: Unable to serialize unknown type:
<class 'curl_cffi.requests.session.AsyncSession'>
```

`GeneratedImage` 另有 `client_ref`（指向 `GeminiClient` 本身），同樣不可序列化。

### fixture 的正確作法（實測可行）

**不要**降級成 `SimpleNamespace` 手刻假物件 —— 用 pydantic 原生序列化並排除那兩個欄位，
fixture 就能還原成**真正的 `ModelOutput`**：

```python
EXCLUDE = {
    "candidates": {
        "__all__": {
            "web_images": {"__all__": {"client"}},
            "generated_images": {"__all__": {"client", "client_ref"}},
        }
    }
}

# dump（由使用者實機執行）
payload = output.model_dump_json(exclude=EXCLUDE, indent=2)

# load（測試中）
output = ModelOutput.model_validate_json(payload)   # client 還原為 None，其餘完整
```

已實測：排除後 round-trip 完整，`images`、`url`、`title`、`alt`、`text` 皆正確還原，
`client` 還原為 `None`（不影響 URL 直傳路徑；走下載路徑時 `save()` 可自帶 client）。

---

## 八、合約未涵蓋範圍

以下尚未反射確認，需要時**必須回報 Commander 補做偵察，不得猜測**：

- `Citation` 的欄位結構仍未涵蓋（`ChatHistory` 已由 §五之二 涵蓋）
- `GeneratedVideo` 的屬性與 `save()` 簽章已由 §五之二 涵蓋；`GeneratedMedia`（音樂）仍未涵蓋（T3.2b 若處理音樂需補）
- `Image.url` 的對外可存取性 → **由 T3.2a HUMAN gate 實測回答**
- 各例外的實際觸發條件與 HTTP 狀態碼對應 → 需 live 觀察，屬 G1/G2 範圍
