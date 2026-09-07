# 上游 API 合約 — `gemini_webapi`

> **版本：`gemini-webapi` 2.1.1**（`docs/upstream-api.json` 的 `package.version`）
> **產生方式**：由 Commander 親自執行 `uv run python scripts/probe_upstream.py --json` 反射產出，
> 並與 `docs/upstream-api.json` 逐項比對確認一致（兩次執行輸出 byte-identical）。
> **本檔為 CLAUDE.md 不變量 6 的唯一事實來源。** 任何 `gemini_webapi` 的類別名稱、
> 方法簽章、屬性欄位一律引用本檔，不得憑記憶推測。
> **合約未涵蓋者，回報 Commander 補做偵察，不得自行猜測。**
> 升級 `gemini-webapi` 版本時必須重跑 probe 並逐項比對本檔。

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

`TemporarilyBlockedError` 歸為 RateLimit 而非 Fatal：它是暫時性封鎖，退避後可能恢復；
但若連續觸發應升級為 DEGRADED，由 T2.2 的狀態機決定門檻。

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

## 七、合約未涵蓋範圍

以下尚未反射確認，需要時**必須回報 Commander 補做偵察，不得猜測**：

- `AvailableModel` / `Gem` / `GemJar` 的欄位結構（T2.5 `/model`、`/gem` 實作前需補）
- `Candidate`、`Citation`、`ChatHistory` 的欄位結構
- `GeneratedVideo` / `GeneratedMedia` 的屬性與 `save()` 簽章（T3.2b 若處理影音需補）
- `Image.url` 的對外可存取性 → **由 T3.2a HUMAN gate 實測回答**
- 各例外的實際觸發條件與 HTTP 狀態碼對應 → 需 live 觀察，屬 G1/G2 範圍
