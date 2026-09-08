# Gemini Telegram Bot

[![CI](https://github.com/7WayneLee/gemini-telegram-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/7WayneLee/gemini-telegram-bot/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

**正體中文** · [English](README.md)

自架的 Telegram bot，透過 [`gemini-webapi`](https://github.com/HanaokaYuzu/Gemini-API)
與 Google Gemini 對話。

**這不是官方 Gemini API。** 認證用的是瀏覽器 cookie 而非 API key —— 也因此
cookie 的處理才是這個專案真正的重點。

### 功能

- **串流回覆**，Markdown 轉成 Telegram HTML —— 程式碼區塊、巢狀清單、
  超長訊息切段而不破壞格式
- **圖片以相簿送出**，能直傳的就交給 Telegram 自己抓，不經過你的伺服器
- **檔案上傳** —— 丟一張圖或 PDF 就能提問
- **動態選擇模型與 gem**（`/model`、`/gem`）
- **Deep Research** 背景任務，重啟後仍會繼續
- **預設白名單** —— 沒設定的 bot 誰都不理
- **從聊天室救回 cookie** —— `/setcookie` 更新憑證，不必 SSH、不必重啟
- **約 150 MB 記憶體**，適合跑在小型共用主機上

---

## ⚠️ 開始之前

### bot 持有的是一個真實 Google 帳號的完整 web session

不是受限的 API token，是**完整的登入態**。若該帳號啟用了 Gemini extensions，
**任何能與這個 bot 對話的人，都能透過 `@Gmail` 讀取該帳號的信箱**，
`@Google Drive` 同理。

因此白名單是核心功能而非選項。`ALLOWED_USER_IDS` 未設定時 bot **拒絕所有訊息**。
這個預設值不要改。

### 請使用獨立的次要 Google 帳號

**不要用你的主帳號。** 這是逆向工程的 API，違反 Google 服務條款，
帳號可能被限制。見[已知風險](#已知風險)。

---

## 快速開始

需要 **Python 3.12+** 與 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/7WayneLee/gemini-telegram-bot.git
cd gemini-telegram-bot
uv sync

cp .env.example .env && chmod 600 .env
# 填入五個必要值 —— 見下方「設定」。
# cookie 是最麻煩的部分，見下一節。

uv run python scripts/preflight.py        # 檢查設定，不會發出任何 Gemini 請求
uv run python -m gemini_tg_bot --dry-run  # 全鏈路煙霧測試，不需憑證
uv run python -m gemini_tg_bot            # 啟動
```

`--dry-run` 會用假的傳輸層跑完整條流程，**讓你在還沒碰 cookie 之前就先確認環境沒問題**。

---

## 取得 cookie

### cookie 的出生 IP 必須與使用 IP 一致

若你在某個國家的瀏覽器取得 cookie，卻拿到另一個國家的伺服器使用，
Google 會偵測到 session 地理位置跳動，**反覆作廢它**。
你會陷入每隔幾小時就要重新登入、卻不知道原因的迴圈。

所以要**透過伺服器的出口登入**。如果 bot 就跑在你自己這台機器上，可以跳過 tunnel。

### 步驟

1. **開一條到伺服器的 SOCKS tunnel**（把 `your-vm` 換成你的 SSH alias），保持開著：

   ```bash
   ssh -D 1080 -C -q -N your-vm
   ```

2. **用獨立的 Firefox profile 啟動**，時區設成與伺服器所在區域相符：

   ```bash
   TZ=America/Chicago firefox -P gemini
   ```

   **要用 Firefox。** Chromium 的 Device Bound Session Credentials 會讓 cookie
   只維持數小時且無法更新。

3. **設定 → Network Settings → Manual proxy**：SOCKS Host `127.0.0.1`、Port `1080`、
   **SOCKS v5**，並勾選 **「Proxy DNS when using SOCKS v5」** ——
   沒勾的話 DNS 會走本地，洩漏你的真實位置。

4. **登入 `gemini.google.com` 前先確認對外 IP**：

   ```bash
   curl --socks5-hostname 127.0.0.1:1080 https://ifconfig.me
   ```

5. **取得 cookie。** 可以自動從 profile 讀出：

   ```bash
   sh scripts/grab_cookies.sh          # 兩個值直接進剪貼簿
   ```

   或從 F12 → Network 手動複製 `__Secure-1PSID` 與 `__Secure-1PSIDTS`。

6. **套用**：貼進 `.env`，或傳 `/setcookie` 給 bot 後貼上 —— 後者不需重啟。

> 首次從新的國家登入時，Google 大機率會要求二階段驗證並寄送新裝置通知。
> 這是正常的，通過後即穩定。

### cookie 會自動輪替

啟動後 bot 會在背景輪替 `__Secure-1PSIDTS` 並寫入 `GEMINI_COOKIE_PATH`。因此：

- `.env` 裡那份在 bot 啟動後就過時了，實際生效的是已儲存的那份
- 重啟不需要重新取得 cookie，只有被 Google 作廢的 session 才需要
- `GEMINI_COOKIE_PATH` 必須掛在 volume 上，否則容器重建就會失去 session
- `/setcookie` 會寫入權限 `0600` 的覆寫檔，下次啟動時優先採用，
  **因此熱更新可以跨重啟存活**

### 在本機測試但使用遠端伺服器的 IP

`GEMINI_PROXY` 支援 SOCKS，掛著 tunnel 就能在自己的機器上跑 bot，
同時對外呈現伺服器的出口 IP：

```
GEMINI_PROXY=socks5h://127.0.0.1:1080
```

用 `socks5h` 而非 `socks5` —— `h` 代表 DNS 也走 tunnel。

---

## 設定

`.env.example` 內含完整註解。必填五項：

| 變數 | 說明 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 發的 token |
| `ADMIN_USER_ID` | 可執行管理員指令的 Telegram user ID |
| `ALLOWED_USER_IDS` | 逗號分隔的白名單。**留空代表誰都不允許** |
| `GEMINI_SECURE_1PSID` | Gemini cookie |
| `GEMINI_SECURE_1PSIDTS` | Gemini cookie |

其餘皆有安全預設值，其中三個值得注意：

| 變數 | 預設 | 理由 |
|---|---|---|
| `MAX_CONCURRENCY` | `1` | 單一 Gemini 帳號本有配額上限，併發效益有限 |
| `ENABLE_VIDEO_GENERATION` | `false` | 單支影片可達數十 MB |
| `ENABLE_AUDIO_GENERATION` | `false` | 同上 |

---

## 指令

### 白名單內的使用者

| 指令 | 行為 |
|---|---|
| `/start`、`/help` | 顯示可用指令 |
| `/new` | 結束目前對話，開始新的一輪 |
| `/model` | 以 inline keyboard 選擇模型，清單為動態取得 |
| `/gem` | 選擇要套用於對話的 gem |
| `/temp` | 切換暫時模式 —— 不寫入 Gemini 歷史 |
| `/img <prompt>` | **明確要求生成圖片。** 未明示時 Gemini 傾向回傳網路搜尋結果而非 AI 生成圖 |
| `/research <topic>` | 送出 Deep Research 任務，立即回傳 task id |
| `/research_status` | 該聊天室的研究任務狀態 |
| `/status` | 模型、session id、cookie 最後刷新時間、佇列深度、用量、egress 估算 |
| 純文字 | 以串流方式回覆 |
| 圖片／文件 | 送給 Gemini，caption 作為 prompt |

### 僅限管理員（`ADMIN_USER_ID`）

| 指令 | 行為 |
|---|---|
| `/setcookie` | 互動式接收新 cookie 並熱重啟 client，不重啟 process。訊息收到後立即刪除 |
| `/allow <user_id>` | 加入白名單，即時生效 |
| `/deny <user_id>` | 移出白名單。deny 優先於靜態清單 |
| `/health` | client 健康狀態、最近錯誤、資料庫狀態 |

---

## 部署

採用 long polling，**不需開啟任何 inbound port**，沒有東西需要對外暴露。

提供兩種方式，資源限制相同，看你的主機環境選擇。

### systemd

小型主機、或該機器上沒有其他東西在用 Docker 時建議這個。

```bash
# 在伺服器上
sudo useradd --system --home-dir /var/lib/gemini-tg-bot --shell /usr/sbin/nologin gemini-tg-bot
sudo mkdir -p /opt/gemini-tg-bot && sudo tar xzf gemini-tg-bot.tar.gz -C /opt/gemini-tg-bot

# venv 要建在服務帳號讀得到的位置（不要在 /root 底下）
cd /opt/gemini-tg-bot
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv-python uv venv --python 3.12 .venv
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv-python uv pip install --python .venv/bin/python .
sudo chmod -R a+rX /opt/uv-python /opt/gemini-tg-bot

sudo install -m 600 .env /etc/gemini-tg-bot.env
sudo cp deploy/gemini-tg-bot.service /etc/systemd/system/
sudo systemctl enable --now gemini-tg-bot
```

把環境變數檔放在 `/etc` 而非專案目錄，可降低被某個工具意外讀取的機會。

### Docker Compose

```bash
docker compose -p gemini-bot -f deploy/docker-compose.yml up -d
```

用獨立的 project 名，避免被併進既有的 stack；若主機上還跑著其他服務，
重啟請用 `docker compose -p gemini-bot restart bot` 而不是 `down`。

### 資源限制

兩種部署檔都附了保守的預設值：

| 項目 | 預設 |
|---|---|
| 記憶體 | `256m` |
| 記憶體 + swap | `512m` |
| CPU | 單核的 50% |

典型常駐記憶體是 120–180 MB。這些限制的用意是：真的出事時，
**被 OOM killer 終結的是 bot，而不是主機上的其他東西** —— 除非真的需要，否則不用調高。

**媒體暫存要放在磁碟，不要用 tmpfs。** tmpfs 會吃 RAM，等於架空了記憶體限制。

完整步驟與實務上踩過的坑見 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

---

## 已知風險

### 違反 Google 服務條款，帳號可能被封

`gemini-webapi` 是對 web app 的**逆向工程**封裝。使用它違反 Google 條款，
帳號可能在沒有預警的情況下被限制。**請用次要帳號。**

### cookie 會過期，且時機不可預測

Google 會主動作廢 session；你能控制的因素中，IP 一致性影響最大。
bot 在認證失敗時會進入降級狀態並推播管理員，所以你不需要盯著 log ——
但收到通知時需要處理。

穩態下這比聽起來少見，因為只要 bot 持續運行，session 就會持續輪替。
通常只有在重開機、長時間停機、或被 Google 作廢後才需要人工介入。

### AGPL-3.0 的義務

`gemini-webapi` 是 AGPL-3.0，而本專案直接 import 它，因此本專案也是 AGPL-3.0。
這是相依套件造成的結果，不是偏好選擇。

AGPL 是**涵蓋網路使用**的 copyleft：

- **自己用** —— 沒有額外義務
- **透過網路提供給他人使用**，哪怕只是朋友 ——
  第 13 條要求你必須向那些使用者提供完整的對應原始碼，包含你的修改

若你 fork 並修改，這個義務會一併繼承。

### 無法水平擴展

背景 cookie 輪替意味著兩個 process 共用同一帳號會互相作廢，
因此本專案在設計上就是單一 process。見 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

### 上游可能無預警變動

`gemini-webapi` 追隨 Google 的內部介面，更新頻繁。本專案依賴的所有上游事實都記錄在
[`docs/upstream-api-contract.md`](docs/upstream-api-contract.md)，
由對已安裝套件做反射產生。**升級該套件時，請重跑 `scripts/probe_upstream.py`
並與該文件比對。**

---

## 開發

```bash
uv sync
uv run pytest -q                            # 全套測試
uv run python -m gemini_tg_bot --dry-run    # 全鏈路，不需憑證
```

所有測試皆為 mock，沒有任何一個會用真實 cookie 發出請求。

| 文件 | 內容 |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 修改 bot 時需要的設計說明 |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | 部署步驟與踩過的坑 |
| [`docs/upstream-api-contract.md`](docs/upstream-api-contract.md) | 上游 API 事實，由反射產出 |
| [`docs/egress-findings.md`](docs/egress-findings.md) | 圖片傳送方式的實測結果 |
| `CLAUDE.md` | 專案不變量 |

歡迎貢獻。CI 會執行測試、dry run 與憑證掃描。

---

## 授權

**AGPL-3.0-or-later** —— 見 [`LICENSE`](LICENSE) 與 [已知風險](#agpl-30-的義務)。
