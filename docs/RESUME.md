# 專案狀態

**2026-09-08：orca-orchestration-plan.md 的 DAG 全部完成。**

30 個節點完成（原計畫 20 個 + 實機測試衍生的 10 個修正任務），
2 個標記為 failed（皆為重複建立或被取代的任務，非實際失敗）。

---

## 驗收狀態

| Gate | 結果 |
|---|---|
| **M1 / G1** 連通性 | ✅ 白名單使用者收到回覆；非白名單被拒絕並留 log |
| **M2** 渲染 | ✅ 粗體、三層巢狀清單、code block、超長切段 |
| **M3** 會話 | ✅ 多輪上下文、`/new` 重置、重啟後延續 |
| **M4** 媒體 / **T3.2a** egress | ✅ **零 egress 成立**（WebImage 直傳，`/status` 回報 0 B） |
| **M5 / G2** 韌性 | ✅ 無效 cookie → DEGRADED + 推播 → `/setcookie` 熱恢復，**process 未重啟** |
| **M6** 研究 | ✅ `/research` 立即回傳、重啟後恢復輪詢 |

測試：**213 passed**。`--dry-run` 全鏈路綠燈。

---

## 實機驗證過的行為

- 多張圖 → **單一相簿**（`sendMediaGroup`），caption 掛第一張
- `WebImage` → URL 直傳，**egress 0**；`GeneratedImage` → Telegram 回 400，自動退回中轉
- `/img` 產生真正的 AI 生成圖（非圖庫照）
- flood control → 有界等待、快速失敗，不再凍結整個 bot
- cookie 失效 → 立即 DEGRADED + 推播，不再空轉 91 秒
- `auto_refresh` 背景輪替正常運作

---

## 未償還的技術債（見 docs/decisions.md）

| 編號 | 內容 |
|---|---|
| D6 | `1PSID` **更換後無法跨重啟存活** —— 重啟會退回 `.env` 的舊值 |
| D16 | `WORKDIR /` 是隱性耦合；`DATABASE_PATH` 應改為環境變數 |
| D16 | `uv.lock` 未進 build context，image 相依性非鎖定 |
| D15 | `.env` 位於 repo 根目錄，任何展開它的工具都可能洩漏 |

**D6 對維運最關鍵**：`.env` 裡的 `1PSIDTS` 會被 `auto_refresh` 淘汰，
快取檔才是最新來源。重取 cookie 時不能只看 `.env`。

---

## 尚未驗證

- `docker compose build` 的 DoD **未經 Commander 親自執行**（驗收時本機 Docker daemon 未運行）。
  worker 回報成功並附具體輸出，`.dockerignore` 的安全性質已靜態驗證。
- **實際部署到 movie-nas 尚未進行。** 所有 movie-nas 操作皆為 HUMAN gate。
  部署前請重讀 README 的「部署」章節與 D8 的資源實測數值。
