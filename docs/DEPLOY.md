# 部署檢查清單

> 本文件記錄實際部署一次的完整流程與踩到的坑。
> 範例以 Debian 12 + systemd 為主；Docker 方案見 `deploy/docker-compose.yml`。
> 指令中的 `your-vm` 請換成你的 SSH alias。

---

## ⚠️ 兩個會讓第一次部署失敗的陷阱

這兩點都是本機開發環境與 VM 環境的差異造成的，照抄 `.env` 必踩。

### 陷阱 1：`GEMINI_PROXY` 必須清空

開發時的值是 `socks5h://127.0.0.1:1080`，指向**你 Mac 上的 SSH tunnel**。
VM 上沒有這個 tunnel，照抄過去所有 Gemini 請求都會失敗。

```diff
- GEMINI_PROXY=socks5h://127.0.0.1:1080
+ GEMINI_PROXY=
```

理由：cookie 的出生 IP 就是 VM 的 IP。bot 在 VM 上執行時**直連即可**，
本來就不需要 proxy —— proxy 只是為了讓「本機開發」也能用 VM 的出口 IP。

### 陷阱 2：`.env` 裡的 `__Secure-1PSIDTS` 已經過時

`auto_refresh` 會每 600 秒輪替 `1PSIDTS`，輪替後的新值**只存在 cookie 快取檔**，
不會寫回 `.env`。已實測確認兩者不同。

VM 冷啟動時若只有 `.env`，用的是舊值，**可能認證失敗**。

**建議解法：把 cookie 快取一併帶到 VM。**

```bash
# 在你的 Mac 上（檔名含 1PSID，勿貼進任何對話）
scp data/cookies/.cached_cookies_*.json your-vm:/tmp/
```

然後在 VM 上把它放進掛載的 cookie volume。
上游 `init()` **優先載入快取**（合約 §2 已記載），因此快取內的新鮮憑證會勝過 `.env` 的舊值。

替代解法：部署後直接用 `/setcookie` 熱更新（不需重啟 process，G2 已實測可行）。

---

## 部署前確認

```bash
# 在 your-vm 上
free -h && df -h / && swapon --show
```

對照你自己實測的數值。
若機器規格有變，**必須重新計算資源限制**，不可沿用。

---

## 部署方式：建議用 systemd 而非 Docker

實測 `docker stats --no-stream` 在 your-vm 上無任何輸出，
代表既有的其他服務 **可能是原生安裝而非容器**。

若確實如此，為了這一個 bot 引入 Docker daemon，在 969Mi 的機器上是不划算的常駐開銷。
因此建議使用 `deploy/gemini-tg-bot.service`。

兩種方式的資源限制等價：

| | Docker Compose | systemd |
|---|---|---|
| 記憶體 | `mem_limit: 256m` | `MemoryMax=256M` |
| swap | `memswap_limit: 512m` | `MemorySwapMax=512M` |
| CPU | `cpus: 0.5` | `CPUQuota=50%` |

### systemd 路徑對應

```
EnvironmentFile   /etc/gemini-tg-bot.env          ← .env 放這裡，非 repo 目錄
WorkingDirectory  /var/lib/gemini-tg-bot
cookie 快取        /var/lib/gemini-tg-bot/cookies
資料庫             /var/lib/gemini-tg-bot/data/db/bot.sqlite3
```

把 `.env` 放在 `/etc/` 而非 repo 目錄，順帶降低 D15（憑證洩漏）那類事件的風險。

### 若選擇 Docker

```bash
docker compose -p gemini-bot -f deploy/docker-compose.yml up -d
```

- **必須用獨立的 project 名 `-p gemini-bot`**，不要併進既有 stack 的 compose 檔
- **禁止在此主機使用 `docker compose down`** —— 容易誤停到同機的其他服務
- 重啟一律 `docker compose -p gemini-bot restart bot`
- 不需開啟任何 inbound port（long polling 的主要安全收益）

---

## ⚠️ 陷阱 3：`sudo uv venv` 會把 Python 裝進 `/root`，被 `ProtectHome=true` 擋住

**2026-09-08 實際踩到，症狀完全不提示真正原因。**

```
gemini-tg-bot.service: Failed to locate executable /opt/gemini-tg-bot/.venv/bin/python:
No such file or directory
Main process exited, code=exited, status=203/EXEC
```

`.venv` 目錄明明存在，`uv pip install` 也成功了。查 `pyvenv.cfg` 才發現：

```
home = /root/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin
```

`sudo` 讓 `$HOME` 變成 `/root`，uv 把 managed Python 下載到那裡，
venv 內的 `bin/python` 只是指向該處的 symlink。

**兩層阻擋，改權限也沒用：**

1. `/root` 通常是 `drwx------`，服務帳號進不去
2. **本專案的 systemd unit 有 `ProtectHome=true`** —— `/root` 對服務**完全不可見**

因此 systemd 回報「No such file」是真的看不到，不是路徑寫錯。

### 正確作法：把 managed Python 裝到全域可讀的位置

```bash
sudo rm -rf /opt/gemini-tg-bot/.venv
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv-python ~/.local/bin/uv python install 3.12
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv-python ~/.local/bin/uv venv --python 3.12 /opt/gemini-tg-bot/.venv
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv-python ~/.local/bin/uv pip install --python /opt/gemini-tg-bot/.venv/bin/python /opt/gemini-tg-bot
sudo chmod -R a+rX /opt/uv-python /opt/gemini-tg-bot/.venv
```

### 驗證（**啟動 systemd 前先做這兩步，省一輪來回**）

```bash
sudo readlink -f /opt/gemini-tg-bot/.venv/bin/python
# 應顯示 /opt/uv-python/...，若是 /root/... 就是還沒修好

sudo -u gemini-tg-bot /opt/gemini-tg-bot/.venv/bin/python -c "import gemini_tg_bot; print('import ok')"
# 以服務帳號實際執行一次，同時驗證權限與 import
```

---

## 其他實際踩到的小坑

- **不要整段貼多行指令。** 連續的 `install -d` 指令在貼上時會被當成續行吃掉，
  造成目錄沒建立卻沒有錯誤訊息。逐行執行。
- **對 `/var/lib/gemini-tg-bot/cookies`（mode 700）用萬用字元會失敗。**
  你的 shell 讀不到該目錄，glob 展開不了，會把字面字串傳給指令：
  ```
  chmod: cannot access '.../cookies/.cached_cookies_*.json': No such file or directory
  ```
  用 `sudo sh -c "chmod 600 .../.cached_cookies_*.json"` 讓 glob 在 root 的 shell 展開。
  **注意這個錯誤不代表檔案沒複製成功** —— `sudo cp` 的來源在 `/tmp`（可讀），複製其實是成功的。

---

## 部署後驗證

依序確認：

1. **`/status`** —— 服務狀態 `healthy`、cookie 刷新時間有值
2. **發一句話** —— 收到回覆
3. **非白名單帳號發訊息** —— 被拒絕且 log 留下 user_id
4. **`台北101 長什麼樣子?附上照片`** —— 應為單一相簿 + caption，無 tag 外洩
5. **再看 `/status`** —— egress 應仍為 **0 B**（WebImage 走 URL 直傳）
6. **等 10 分鐘後再看 `/status`** —— cookie 刷新時間應往前跳，證明 `auto_refresh` 運作

---

## 已知限制（部署後仍存在）

| 編號 | 內容 |
|---|---|
| **cookie 更換** | 若整組 session 換新（`1PSID` 也變），`/setcookie` 會寫入 runtime override 並跨重啟生效 |

**D6 最需要留意**：日後若因封號或安全驗證而必須換整組 cookie，
記得同步更新 `.env`（或 `/etc/gemini-tg-bot.env`），不能只靠 `/setcookie`。

---

## 成本觀察

- `WebImage`（搜尋來的圖）→ URL 直傳，**egress 0**
- `GeneratedImage`（`/img` 生成的圖）→ Telegram 抓不到簽章網址，**每張都中轉計量**

免費層每月僅 1GB 北美流量，**且為整台機器共用**。
頻繁使用 `/img` 會顯著消耗額度，`/status` 的 egress 數字是唯一的可見性來源。
