# CLAUDE.md — Gemini Telegram Bot

本檔為**專案不變量**。違反任何一條即為任務失敗，不接受「已完成但略作調整」。

---

## 一、安全與環境（硬性禁令）

1. **禁止對真實 cookie 發出 live 請求。**
   開發期所有測試一律 mock `GeminiClient`。需要真帳號的驗證一律標記為 HUMAN gate，
   由人工執行。違反此條會作廢正式環境的 session。

2. **禁止啟動第二個 `GeminiClient` 實例。**
   全程單例（singleton）。程式碼中若出現第二處 `GeminiClient(...)` 建構
   （測試 mock 除外），視為架構違規。
   理由：`auto_refresh=True` 會在背景輪替 `__Secure-1PSIDTS`，多 process 共用同一組
   cookie 會互相作廢，導致帳號反覆登出。

3. **禁止將 cookie 值寫入 log、測試 fixture、commit、或任何 `docs/` 產出。**
   測試用假值一律使用顯性假字串，格式為 `FAKE_1PSID_FOR_TEST` / `FAKE_1PSIDTS_FOR_TEST`。

4. **正式主機上只允許唯讀診斷，任何會改變狀態的指令一律由人工執行。**
   本專案設計為與其他服務共用主機，一次誤下的指令即可造成同機服務中斷。

   Agent **可以**執行：`journalctl`、`systemctl status` / `show`、
   唯讀的 sqlite 查詢、`ls` / `cat`（非機密檔案）等不改變狀態的指令。
   連線一律加 `-o ClearAllForwardings=yes`（ssh config 的 8080 轉送會與本機衝突）。

   Agent **不得**執行：`git pull`、`uv pip install`、`systemctl restart` / `stop`、
   `rm`、`sed -i`、`cp`、`mkdir`，以及任何寫入、刪除、重啟、容器操作。
   **所有部署、重啟、容器操作一律由人工執行。**

   判斷準則：**這道指令會改變主機上的任何狀態嗎？** 會，就交給人。
   不確定就當作會。第 5 條（不得展開或印出 `.env`）不受此放寬影響，全程有效。

   > 放寬決議：2026-09-09，由 Commander 提出選項、使用者選擇「只開唯讀診斷」。
   > 原文為「禁止在正式主機上執行任何指令」。

5. **禁止執行任何會展開或印出 `.env` 的指令。**
   已知會觸發的：`docker compose config`、`docker-compose config`
   （兩者都會展開 `env_file` 並把解析後的 `TELEGRAM_BOT_TOKEN` 與
   `GEMINI_SECURE_1PSID` / `1PSIDTS` 真實值印到 stdout）。
   同理適用於任何 dotenv / direnv 類工具，以及 `env`、`printenv`、
   `set` 等會列出環境變數的指令。

   **這條曾真實發生過並造成憑證洩漏。**`.gitignore` 只能防 commit，擋不住「讀取並印出」。

   驗證 compose 檔請改用：
   - 拋棄式目錄搭配只含 `FAKE_` 值的 `.env.example`
   - 純 YAML 語法檢查：`yaml.safe_load(open(...))`
   - `docker build`（不涉及 `env_file` 展開）

   若不確定某指令是否會印出環境變數，**先用 ask 問 Commander，不要試跑**。

6. **禁止引入 webhook、多 worker、多 replica 設計。**
   一律 long polling、單一 process、單一 event loop、不開任何 inbound port。

---

## 二、實作紀律

7. **禁止憑記憶推測上游 API。**
   所有 `gemini_webapi` 的類別名稱、方法簽章、屬性欄位，一律引用
   `docs/upstream-api-contract.md`。合約未涵蓋的，回報 Commander 補做偵察，
   **不得自行猜測**。

8. **禁止硬編碼模型清單。**
   模型必須來自 `client.list_models()` 動態取得。
   上游已棄用 `Model` enum 並移除 `Model.from_name` / `Model.from_dict`。

9. **每個任務單一 commit**，commit message 用英文，且**只能觸碰任務宣告的檔案範圍**。

10. **回報完成時必須附上 DoD 指令的實際輸出。**
   不接受「測試已通過」的文字聲明。

---

## 三、技術約束

- Python 3.11+（`gemini_webapi` 最低需求）；容器用 `python:3.12-slim`。
- 全程 asyncio。Telegram 層採 `python-telegram-bot >= 21` 或 `aiogram 3.x`，
  擇一並在 README 說明理由。
- cookie 持久化路徑由環境變數 `GEMINI_COOKIE_PATH` 指定，掛載到 volume。
- **預設拒絕所有使用者**。`ALLOWED_USER_IDS` 未設定時 bot 拒絕所有訊息，
  只回覆一則說明，並將未授權嘗試寫入 log。白名單為 P0 功能。
- `MAX_CONCURRENCY` 預設 `1`；影片／音訊生成預設停用；
  使用者上傳上限 20MB（Telegram Bot API 限制），提前拒絕。
- Egress 是主要成本來源（月預算 US$10，免費層僅 1GB/月）。
  媒體必須實作**兩條路徑**：Telegram URL 直傳（零 egress）與下載後上傳（計量），
  以設定開關或自動偵測切換。

---

## 四、角色邊界

| | Commander (Opus 5) | Worker (Sol) |
|---|---|---|
| 職責 | DAG 維護、合約審查、phase gate 驗收、失敗診斷、架構決策 | 實作、測試撰寫、機械性重構 |
| 不得做 | 直接寫實作程式碼 | 做架構決策、變更不變量、修改 DoD 定義 |

需 Commander 親自判斷的決策點：egress 雙路徑取捨、錯誤分類邊界、
切段器降級策略、singleton 生命週期。

單一任務內的迭代（測試失敗 → 修正 → 重跑）由 Worker 自行完成，
**連續失敗 3 次才升級給 Commander**。

---

## 五、驗證協定（Commander 於每個任務收尾執行）

```bash
git log --oneline -1                       # 1. 確認確實有 commit
git show --name-only HEAD                  # 2. 確認觸碰檔案在宣告範圍內
git diff HEAD~1 --stat                     # 3. 確認改動規模相稱
<該任務的 DoD 指令>                          # 4. 親自執行，看退出碼
grep -rn "GeminiClient(" src/ --include=*.py | grep -v test    # 5. 單例檢查
grep -rniE "1PSID|1PSIDTS" src/ tests/ docs/ | grep -v FAKE_    # 6. cookie 洩漏檢查
```

**第 4 步是核心：Commander 不讀完成報告來判定通過，只看自己執行的結果。**
Worker 的報告僅用於失敗時的診斷輸入。
任一步失敗 → 任務退回，附上失敗的指令輸出（不附解法，讓 Worker 自行診斷）。

---

## 六、Human Gate 清單（agent 不可代勞）

| 項目 | 時機 |
|---|---|
| VM 資源實測（`free -h` / `docker stats` / `swapon`） | T4.1 前 |
| SSH SOCKS 取得 cookie（`ssh -D 1080 -C -q -N 正式主機`） | G1 前，及每次 cookie 失效 |
| M1 連通性實測 | G1 |
| egress URL 可抓取性實測 | T3.2a |
| M5 cookie 失效演練 | G2 |
| **所有在正式主機上的操作** | 全程 |
