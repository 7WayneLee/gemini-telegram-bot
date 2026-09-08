# 續跑指引（codex 額度耗盡時的中斷點）

**中斷時間**：2026-09-08
**原因**：codex worker 回報 `Usage limit reached`，兩個進行中的任務停擺。

---

## 目前進度：18 / 20 節點完成，158 測試全過

已完成並經 Commander 逐項驗收：T0.1–T0.2、T1.1–T1.4、T2.1–T2.7、T3.1、T3.2b、T3.3–T3.5、G1。

`--dry-run` 走完整正式路徑：
```
receive → allowlist → queue → service → stream → render → send → research → database: ok
```

---

## 額度回來後，直接從這裡續跑

### 1. T3.6 — 多張圖片改用 sendMediaGroup 相簿（零產出，需重派）

```bash
orca orchestration worker-start --task task_80ac48ef36ff --worktree current --agent codex --json
```

使用者實機回報：多張圖時第一張正確帶 caption，但**第二張以後各自成為獨立訊息**。
需改為 `reply_media_group`。完整規格已寫在該任務的 spec 內，重點：

- `MediaGroupLimit.MIN_MEDIA_LENGTH = 2`、`MAX_MEDIA_LENGTH = 10`（1 張仍走 `sendPhoto`，>10 拆組）
- caption 掛在第一組第一項，仍受 1024 限制
- **難點**：`sendMediaGroup` 是原子操作，一張 URL 失敗整組失敗，
  因此 T3.2b 的 per-image 退回不適用 → 改為**以組為單位**退回（整組下載後重送）

### 2. T4.1 — 部署產物（約九成完成，檔案已產出但未 commit）

```bash
orca orchestration worker-start --task task_87ee87bc4e67 --worktree current --agent codex --json
```

`deploy/` 下已有三個檔案（**未 commit，untracked**），Commander 已審閱，品質良好：

| 檔案 | 狀態 |
|---|---|
| `docker-compose.yml` | D8 數值正確（256m/512m/0.5）、無 `ports`、logging 輪替、`name: gemini-bot`、tmpfs 警告註解 |
| `Dockerfile` | python:3.12-slim、非 root UID 10001、build 時驗證目錄可寫、只 COPY `pyproject.toml` 與 `src/` |
| `gemini-tg-bot.service` | MemoryMax/MemorySwapMax/CPUQuota 正確，含完整 systemd 硬化 |

**尚缺**：
- `.dockerignore`（需排除 `.env`、`data/`、`cookies/`、`tests/`、`.git`）
- DoD 未執行：`docker compose -f deploy/docker-compose.yml build`
  （註：開發機沒有 compose plugin，只有 `docker` 二進位，本機驗不了）
- 尚未 commit

### 3. T4.2 — README ✅ 已完成（commit 4b115b7）

Commander 於等待額度期間完成。涵蓋 SSH SOCKS cookie 流程、實測資源與 egress 策略、
四項已知風險、rendering 與 DEGRADED 設計說明。每項陳述皆已對照程式碼查核。

**待補**：部署章節的指令在 T4.1 的 `docker compose build` DoD 通過前尚未驗證。

### 4. T3.7 — 補做 `/img` 指令（新建，尚未派工）

```bash
orca orchestration worker-start --task task_0aac5f272c30 --worktree current --agent codex --json
```

見 `docs/decisions.md` 的 D9：`spec.md` 列有 `/img <prompt>` 但從未被任何任務涵蓋，
是 Commander 切分 Phase 2 時的遺漏。使用者實測「台北101」拿到的是圖庫照片
（`WebImage`）而非生成圖，正是此缺口的實際影響。

---

## 待使用者決定的事項

1. **既有媒體服務是否為 Docker？** `docker stats --no-stream` 在 movie-nas 上無任何輸出。
   若 Jellyfin / qBittorrent 是原生安裝，則為 bot 引入 Docker daemon 在 969Mi 的機器上
   是額外常駐開銷 → 建議把 systemd 方案設為主要部署方式（見 D8）。

2. **T3.2a egress 實測**：目前預設樂觀走 URL 直傳。從使用者截圖看圖片有正常顯示，
   代表路徑 A 可行。請確認 `/status` 的本月 egress 估算是否維持 0，
   若是則可回填 `docs/egress-findings.md` 並關閉該 gate。

3. **G2 韌性演練**（HUMAN gate）：填入無效 cookie → 確認進入 DEGRADED 並推播管理員
   → `/setcookie` 熱更新 → 服務恢復，全程 process 未重啟。

4. **真實 fixture 擷取**（使用者執行，需 live 呼叫）：
   ```bash
   uv run python scripts/dump_response.py "Generate an image of a cat" --output image-generated.json
   uv run python scripts/dump_response.py "台北101 長什麼樣子?附上照片" --output image-web.json
   ```
   拿到後可把測試從「依格式推導」升級為「依實測樣本」，並補齊合約 §四之二的三個待確認項。
