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
| `generate_content` | **async** | `(prompt, files=None, model=None, gem=None, chat=None, temporary=False, deep_research=False, extended_thinking=False, **kwargs) -> ModelOutput` |
| `generate_content_stream` | **async generator** | 同上參數 → `AsyncGenerator[ModelOutput, None]` |
| `list_models` | **sync** | `() -> list[AvailableModel] \| None` — **可能回傳 `None`，必須處理** |
| `resolve_model` | **sync** | `(name: str) -> AvailableModel` |
| `fetch_gems` | **async** | `(include_hidden=False, **kwargs) -> GemJar` |
| `start_auto_refresh` | **async** | `() -> None` |
| `close` | **async** | `(delay: float = 0) -> None` |

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
| `<FollowUp label="..." query="..."/>` | **移除 tag，但把 `label` 保留為斜體提示行** | `label` 是模型產生的有用內容（建議追問），丟掉可惜；`query` 對 Telegram 無用 |

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

- `Citation`、`ChatHistory` 的欄位結構
- `GeneratedVideo` / `GeneratedMedia` 的屬性與 `save()` 簽章（T3.2b 若處理影音需補）
- `Image.url` 的對外可存取性 → **由 T3.2a HUMAN gate 實測回答**
- 各例外的實際觸發條件與 HTTP 狀態碼對應 → 需 live 觀察，屬 G1/G2 範圍
