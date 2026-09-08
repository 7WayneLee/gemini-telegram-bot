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
   成本約束是 P0 設計條件（月預算 US$10、免費層僅 1GB/月、且與 Jellyfin 串流共用額度），
   零 egress 是強烈偏好。
2. **Telegram 回 `BadRequest`（抓取失敗）時自動退回路徑 B（下載後上傳），並記 log。**
   退回時把位元組數累加進 `EgressMeter`。
3. 提供設定開關可強制指定路徑，供實測結果確定後鎖定。

如此一來，實際走哪條路徑由 log 與 `/status` 的 egress 計數直接觀察得出——
**實機使用本身就是這個 gate 的量測**，不需要另外安排一次人工實驗。

## 待填：實機觀察結果

使用者跑過含圖片的請求後，回填：

- [ ] 路徑 A 是否成功（log 中是否出現退回紀錄）
- [ ] 若退回，Telegram 回的確切錯誤訊息
- [ ] `/status` 的本月 egress 估算是否維持 0
- [ ] `WebImage`（搜尋來的圖）與 `GeneratedImage`（生成的圖）行為是否不同
      ——兩者 URL 來源不同，很可能其中一種可直傳、另一種不行

最後一項尤其值得注意：若兩者行為不同，設定開關需要能**分別**指定，
而不是單一全域開關。
