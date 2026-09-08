# Egress 路徑實測（T3.2a）

**狀態：待實機確認。預設路徑已由 Commander 裁定，不阻塞 T3.2b。**

---

## 待回答的問題

Gemini 回傳的 `Image.url` 能否被 **Telegram 伺服器**直接抓取（無認證）？

Telegram 的 `sendPhoto` / `sendDocument` 接受 URL 字串，由 Telegram 自己去抓來源。
若可行，媒體完全不經過 VM，**egress 為零**。

這只能對真實回應驗證（需 live 呼叫），因此原列為 HUMAN gate。

## 已知的相關事實（來自合約 §5，反射確認）

- `Image.url` 是必填 `str`。
- `Image` 物件另帶 `client`（`curl_cffi.requests.session.AsyncSession | None`）與 `proxy`，
  暗示上游下載時**可能**需要帶 session。這是 URL 可能無法被外部匿名抓取的訊號，但不是證據。
- `Image.save()` 是 **async**，簽章 `(path='temp', filename=None, verbose=False, client=None, **kwargs) -> str`。

## Commander 裁定：不等實測結果，實作自動偵測

spec 本來就要求「兩條路徑都要實作，用設定開關或自動偵測切換」。
因此 T3.2b 依下列規則實作，**不以本檔的實測結果為前置條件**：

1. **預設樂觀走路徑 A（URL 直傳，零 egress）。**
   成本約束是 P0 設計條件（免費層僅 1GB/月，且與同機其他服務共用額度），
   零 egress 是強烈偏好。
2. **Telegram 回 `BadRequest`（抓取失敗）時自動退回路徑 B（下載後上傳），並記 log。**
   退回時把位元組數累加進 `EgressMeter`。
3. 提供設定開關可強制指定路徑，供實測結果確定後鎖定。

如此一來，實際走哪條路徑由 log 與 `/status` 的 egress 計數直接觀察得出——
**實機使用本身就是這個 gate 的量測**，不需要另外安排一次人工實驗。

## 部分答案：真實 fixture 揭露兩種來源的網域差異（2026-09-08）

`tests/fixtures/` 的實測樣本顯示 **兩種圖片來源的 URL 性質根本不同**：

| 來源 | 實測 URL | 可被 Telegram 直抓的可能性 |
|---|---|---|
| `WebImage` | `https://www.travel.taipei/image/573628/?r=...`<br>`https://www.ctplayer.com/wp-content/uploads/2019/05/101%E4%BF%A1%E7%BE%A93.jpg` | **高** —— 就是一般第三方公開網址 |
| `GeneratedImage` | `https://lh3.googleusercontent.com/gg-dl/AAQ_wbGpmWlWuUflGFMEWGmX5Dvp46KXjvYw7DTgUPLrRVu73P2Q5od…` | **低** —— Google CDN `gg-dl` 路徑，長 token，很可能帶時效簽章 |

從使用者的 Telegram 截圖可見 `WebImage` 確實正常顯示，佐證路徑 A 對 web 圖可行。

**因此裁定維持不變且更有把握**：`web_image_mode` 與 `generated_image_mode`
**必須是獨立開關**，不可合併。極可能最終設定是
web → URL 直傳（零 egress），generated → 視實測結果決定。

`GeneratedImage` 的可抓取性仍需實測 —— 需要一次成功的圖片生成回應送達 Telegram。

## ✅ 已結案：WebImage 走 URL 直傳，零 egress 成立（2026-09-08 實機驗證）

**狀態：T3.2a 完成。** 由使用者實機執行，Commander 同步比對 bot log。

### 證據一：`/status` 回報

```
本月累計 egress 估算值：0 B
今日用量：10
服務狀態：healthy
```

### 證據二：bot log 中無任何退回紀錄

四張圖全部經 `sendMediaGroup` 送出，log 中**沒有出現**
`Telegram rejected media group URLs; falling back to VM relay`。
Telegram API 呼叫統計：

```
sendMediaGroup   × 1     ← 相簿，非個別送出
sendPhoto        × 0
sendMessage      × 1     ← placeholder
editMessageText  × 4     ← 串流節流
deleteMessage    × 1     ← placeholder 清除
```

### 實測到的 WebImage URL 網域

```
images.unsplash.com
c8.alamy.com
media.gettyimages.com
```

皆為第三方公開網址，Telegram 伺服器可直接抓取，與 fixture 的推論一致。

### 結論

- **`WebImage` → URL 直傳可行，VM egress 為零。** 預設路徑 A 正確，維持不變。
- **`GeneratedImage` 仍未驗證。** 其 URL 為 `lh3.googleusercontent.com/gg-dl/...`
  帶長 token，可能有時效簽章。需要一次成功的 `/img` 回應才能確認。
  這正是 `web_image_mode` 與 `generated_image_mode` 必須維持獨立開關的理由 ——
  兩者已證實網域性質不同，不可假設行為相同。

## ✅ 已結案：`GeneratedImage` 無法直傳，必須中轉（2026-09-08 實機驗證）

`/img 一隻貓` 的實測結果，與 `WebImage` **完全相反**：

```
url = https://lh3.googleusercontent.com/gg-dl/AAQ_wbFhSGjJPaD0FMkV5hQaXpNTlCLErpyn7iHox…
      title='[Generated Image 0]'  alt='watermarked_img_744780683117238945.jpg'

sendPhoto  "HTTP/1.1 400"    ← URL 直傳被 Telegram 拒絕
WARNING  Telegram rejected image URL; falling back to VM relay source=generated
sendPhoto  "HTTP/1.1 200"    ← 退回下載後上傳，成功
```

per-image 自動退回機制正確運作。

### 最終結論：兩種來源行為相反，獨立開關是必要的

| 來源 | URL 網域 | Telegram 直抓 | egress |
|---|---|---|---|
| `WebImage` | 第三方公開網址（unsplash / alamy / gettyimages） | ✅ 可以 | **0** |
| `GeneratedImage` | `lh3.googleusercontent.com/gg-dl/` 簽章網址 | ❌ 400 | **每張都計量** |

fixture 階段的推論（「generated 帶時效簽章，很可能抓不到」）獲得實機證實。
**`web_image_mode` 與 `generated_image_mode` 維持獨立開關的決定是正確的** ——
若當初合併成單一開關，這裡就只能二選一：要嘛所有圖都中轉（浪費 web 圖的零 egress），
要嘛所有圖都直傳（生成圖直接失敗）。

### 成本影響：`/img` 每張生成圖都吃 egress

免費層每月僅 1GB 北美流量，且**與同機其他服務共用**。
生成圖單張約數百 KB 至數 MB，頻繁使用 `/img` 會顯著消耗額度。
`/status` 的 egress 計數是唯一的可見性來源，建議定期查看。
若額度吃緊，可考慮對 `/img` 加上每日次數限制（目前未實作）。

使用者跑過含圖片的請求後，回填：

- [ ] 路徑 A 是否成功（log 中是否出現退回紀錄）
- [ ] 若退回，Telegram 回的確切錯誤訊息
- [ ] `/status` 的本月 egress 估算是否維持 0
- [ ] `WebImage`（搜尋來的圖）與 `GeneratedImage`（生成的圖）行為是否不同
      ——兩者 URL 來源不同，很可能其中一種可直傳、另一種不行

最後一項尤其值得注意：若兩者行為不同，設定開關需要能**分別**指定，
而不是單一全域開關。
