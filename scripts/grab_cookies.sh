#!/bin/sh
# 從本機 Firefox profile 抽出 Gemini cookie，直接送進剪貼簿。
#
# 用途：省去 F12 → Network → 手動複製兩個值的步驟。
# 輸出格式即 /setcookie 需要的兩行，可直接貼進 Telegram。
#
# 安全性：不印出任何 cookie 值，只回報名稱與長度。
# 貼上後按 Enter 或等待 120 秒；僅在內容未變時清除剪貼簿。
# 既有 profile 不會刪除；資料庫與 WAL/SHM 的私密暫存副本會清除。
#
# 前提：已用該 profile 透過 SSH SOCKS tunnel 登入 gemini.google.com，
# 且 cookie 的出生 IP 是 VM 的 IP。請勿登出，登出會使 cookie 失效。
# 完整自動流程請用 python3 scripts/fetch_cookies.py。
#
# 用法：
#   sh scripts/grab_cookies.sh                 # 預設 gemini-us profile
#   sh scripts/grab_cookies.sh <profile-name>  # 指定 profile

exec python3 "$(dirname "$0")/fetch_cookies.py" --extract-only --profile-match "${1:-gemini-us}"
