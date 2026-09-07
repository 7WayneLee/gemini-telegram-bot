# 內部介面契約（Commander 指定）

跨任務、由多個 worker 平行開發時共用的介面。**實作方與呼叫方一律以本檔為準。**
認為介面有缺陷時用 `ask` 向 Commander 提出，**不得自行更改簽章**。

---

## I1 — `ChatSessionRegistry`

- **檔案**：`src/gemini_tg_bot/gemini/sessions.py`
- **實作方**：T2.3
- **呼叫方**：T2.5（handlers）、T3.3（research）

```python
from dataclasses import dataclass

from gemini_webapi.client import ChatSession

from gemini_tg_bot.gemini.service import GeminiService
from gemini_tg_bot.storage.db import Database


@dataclass(frozen=True)
class ChatState:
    """某個 chat 目前的持久化狀態快照（唯讀）。"""

    chat_id: int
    cid: str | None
    model: str | None        # model_name；None = 帳號預設
    gem_id: str | None
    temporary: bool
    updated_at: str | None


class ChatSessionRegistry:
    """Telegram chat_id → Gemini ChatSession，並持久化到 chat_sessions 表。"""

    def __init__(self, service: GeminiService, database: Database) -> None: ...

    async def get_state(self, chat_id: int) -> ChatState:
        """讀取持久化狀態；該 chat 尚無紀錄時回傳 temporary=False 的預設 ChatState。

        /temp 需先讀後翻轉，送出訊息時也需帶入 temporary 旗標，因此為必要方法。
        """

    async def get_or_create(self, chat_id: int) -> ChatSession:
        """取得該 chat 目前的 session；不存在則依其已存設定建立新的。"""

    async def reset(self, chat_id: int) -> None:
        """/new：結束目前 session 並開新對話（清除 cid/metadata，保留 model/gem/temporary）。"""

    async def set_model(self, chat_id: int, model: str | None) -> None:
        """設定模型。傳入 model_name；None 代表使用帳號預設。"""

    async def set_gem(self, chat_id: int, gem_id: str | None) -> None:
        """設定 gem。傳入 Gem.id；None 代表不套用 gem。"""

    async def set_temporary(self, chat_id: int, temporary: bool) -> None:
        """切換 temporary mode（不寫入 Gemini 歷史）。"""

    async def persist(self, chat_id: int, session: ChatSession) -> None:
        """把 session 目前的 cid 與 metadata 寫回 SQLite。每次成功回覆後呼叫。"""

    async def restore_all(self) -> None:
        """啟動時從 SQLite 還原所有 chat 的 session metadata。"""
```

### 實作備註（T2.3）

- **所有方法皆為 `async`**，`__init__` 除外。
- `ChatSession.metadata` 是 `list[str | None]`（合約 §3），以 JSON array 持久化。
  直接重用 `storage.models` 的 `serialize_metadata` / `deserialize_metadata`
  （T1.2 已驗收，round-trip 對 `None` 與順序皆已實測正確）。
- `client.start_chat()` 是**同步**方法，不要 `await`。
- 取得 client 一律經由 `GeminiService`，**不得自行建構 `GeminiClient`**（不變量 2）。

### 呼叫方備註（T2.5 / T3.3）

- 測試中一律 mock：`unittest.mock.AsyncMock(spec=ChatSessionRegistry)`。
- **不要 import 其內部實作細節，也不要修改 `sessions.py`。**
- 若該檔尚未落地導致 import 失敗，可於函式內延遲 import 接線，
  並在 `worker_done` 明確回報。

---

## I2 — `/status` 的資料來源分工（Commander 裁定）

`/status` 需要的欄位分屬不同擁有者，**不要**為此建立第二個「狀態提供者」去重複存取 DAO，
那會造成兩份事實來源。正確分工：

| 欄位 | 來源 | 擁有者 |
|---|---|---|
| 目前模型、session cid、temporary | `ChatSessionRegistry.get_state(chat_id)` | I1（T2.3） |
| 服務狀態 / DEGRADED 原因 / 最近錯誤 | `GeminiService.health` | T2.2（已完成） |
| cookie 最後刷新時間 | cookie 快取檔的 **mtime** | T2.5 讀取 |
| 佇列深度 | `RequestQueue.queue_depth` | T1.4（已完成） |
| 今日用量 | `usage_log` 表（`UsageLogDAO`） | T1.2（已完成） |
| 本月累計 egress 估算 | `EgressMeter`（見下） | T2.5 建立介面，T3.2b 實作累加 |

### cookie 刷新時間

路徑為 `{GEMINI_COOKIE_PATH}/.cached_cookies_{__Secure-1PSID}.json`。
取其 `st_mtime` 即可。**該路徑含 cookie 明文，絕不可寫入 log 或回覆訊息**（不變量 3）——
只回報時間，不回報路徑。檔案不存在時回報「尚未刷新」，不得拋例外。

### `EgressMeter`

T2.5 建立最小介面並回報 0；T3.2b 接手實作實際累加。

```python
class EgressMeter:
    """累計經 VM 中轉的媒體位元組數（URL 直傳路徑不計入）。"""

    def record(self, num_bytes: int) -> None: ...
    @property
    def month_to_date_bytes(self) -> int: ...
```
