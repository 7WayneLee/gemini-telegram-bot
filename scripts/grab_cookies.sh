#!/bin/sh
# 從本機 Firefox profile 抽出 Gemini cookie，直接送進剪貼簿。
#
# 用途：省去 F12 → Network → 手動複製兩個值的步驟。
# 輸出格式即 /setcookie 需要的兩行，可直接貼進 Telegram。
#
# 安全性：不印出任何 cookie 值，只回報長度與來源。
#
# 前提：
#   1. SSH SOCKS tunnel 必須開著（cookie 的出生 IP 必須是 VM 的 IP）
#        ssh -D 1080 -C -q -N your-vm
#   2. 已用該 profile 透過 tunnel 登入過 gemini.google.com
#
# 用法：
#   sh scripts/grab_cookies.sh                 # 預設 profile
#   sh scripts/grab_cookies.sh <profile-name>  # 指定 profile

set -eu

PROFILE_MATCH="${1:-gemini-us}"
FF_DIR="$HOME/Library/Application Support/Firefox/Profiles"

python3 - "$FF_DIR" "$PROFILE_MATCH" <<'PY' | pbcopy
import glob, os, shutil, sqlite3, sys, tempfile

ff_dir, match = sys.argv[1], sys.argv[2]
candidates = [p for p in glob.glob(os.path.join(ff_dir, "*")) if match in os.path.basename(p)]
if not candidates:
    sys.stderr.write(f"找不到含 '{match}' 的 Firefox profile\n")
    raise SystemExit(1)

# 多個相符時取 cookies.sqlite 最新的那個
def mtime(p):
    f = os.path.join(p, "cookies.sqlite")
    return os.path.getmtime(f) if os.path.exists(f) else 0

profile = max(candidates, key=mtime)
src = os.path.join(profile, "cookies.sqlite")
if not os.path.exists(src):
    sys.stderr.write(f"profile 內找不到 cookies.sqlite: {os.path.basename(profile)}\n")
    raise SystemExit(1)

# 複製後再讀，避免 Firefox 執行中的資料庫鎖定
tmp = tempfile.mktemp(suffix=".sqlite")
shutil.copy2(src, tmp)
try:
    conn = sqlite3.connect(tmp)
    rows = conn.execute(
        "SELECT name, value, expiry FROM moz_cookies "
        "WHERE host LIKE '%google.com' "
        "AND name IN ('__Secure-1PSID', '__Secure-1PSIDTS') "
        "ORDER BY expiry DESC"
    ).fetchall()
    conn.close()
finally:
    os.unlink(tmp)

vals = {}
for name, value, _ in rows:
    vals.setdefault(name, value)          # 已依 expiry 排序，取最新的

psid = vals.get("__Secure-1PSID")
psidts = vals.get("__Secure-1PSIDTS")

missing = [n for n, v in (("__Secure-1PSID", psid), ("__Secure-1PSIDTS", psidts)) if not v]
if missing:
    sys.stderr.write(
        f"profile '{os.path.basename(profile)}' 缺少: {', '.join(missing)}\n"
        "請確認已透過 SOCKS tunnel 登入 gemini.google.com。\n"
    )
    raise SystemExit(1)

# 只有這兩行會進剪貼簿；stderr 的診斷訊息不含任何值
sys.stderr.write(
    f"profile     : {os.path.basename(profile)}\n"
    f"1PSID       : {len(psid)} 字元\n"
    f"1PSIDTS     : {len(psidts)} 字元\n"
)
print(psid)
print(psidts, end="")
PY

echo ""
echo "兩行 cookie 已複製到剪貼簿。"
echo ""
echo "下一步：在 Telegram 傳 /setcookie，然後直接貼上（⌘V）。"
echo ""
echo "提醒：取得前請確認 SSH tunnel 開著且出口 IP 是 VM ——"
echo "  curl --socks5-hostname 127.0.0.1:1080 https://ifconfig.me"
