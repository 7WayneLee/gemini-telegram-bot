# 部署到 movie-nas 的檢查清單

> **所有在 movie-nas 上的操作都是 HUMAN gate**（CLAUDE.md 不變量 4）。
> 本文件由 Commander 撰寫供人工執行，agent 不得代勞。

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
scp data/cookies/.cached_cookies_*.json movie-nas:/tmp/
```

然後在 VM 上把它放進掛載的 cookie volume。
上游 `init()` **優先載入快取**（合約 §2 已記載），因此快取內的新鮮憑證會勝過 `.env` 的舊值。

替代解法：部署後直接用 `/setcookie` 熱更新（不需重啟 process，G2 已實測可行）。

---

## 部署前確認

```bash
# 在 movie-nas 上
free -h && df -h / && swapon --show
```

對照 `docs/decisions.md` 的 D8 實測值（969Mi 總記憶體 / 441Mi 可用 / 2GB swap）。
若機器規格有變，**必須重新計算資源限制**，不可沿用。

---

## 部署方式：建議用 systemd 而非 Docker

實測 `docker stats --no-stream` 在 movie-nas 上無任何輸出，
代表既有的 Jellyfin / qBittorrent **可能是原生安裝而非容器**。

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

- **必須用獨立的 project 名 `-p gemini-bot`**，不得併入既有媒體 stack 的 compose 檔
- **禁止在此主機使用 `docker compose down`** —— 會波及 Jellyfin 與 qBittorrent
- 重啟一律 `docker compose -p gemini-bot restart bot`
- 不需開啟任何 inbound port（long polling 的主要安全收益）

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
| **D6** | **`1PSID` 更換後無法跨重啟存活** —— 若整組 session 換新，重啟會退回 `.env` 的舊值。只輪替 `1PSIDTS` 的情況安全。 |
| D16 | `WORKDIR /` 是隱性耦合；若有人改回 `/app`，資料庫會**靜默**寫到非持久化位置 |
| D16 | `uv.lock` 未進 build context，image 相依性每次 build 重新解析 |

**D6 最需要留意**：日後若因封號或安全驗證而必須換整組 cookie，
記得同步更新 `.env`（或 `/etc/gemini-tg-bot.env`），不能只靠 `/setcookie`。

---

## 成本觀察

- `WebImage`（搜尋來的圖）→ URL 直傳，**egress 0**
- `GeneratedImage`（`/img` 生成的圖）→ Telegram 抓不到簽章網址，**每張都中轉計量**

免費層每月僅 1GB 北美流量，**且與 Jellyfin 串流共用**。
頻繁使用 `/img` 會顯著消耗額度，`/status` 的 egress 數字是唯一的可見性來源。
