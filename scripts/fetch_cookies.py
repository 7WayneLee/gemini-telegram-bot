#!/usr/bin/env python3
"""Human-run Gemini cookie acquisition on macOS; never used by the bot."""

import argparse
import json
import os
from pathlib import Path
import select
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time


COOKIE_NAMES = ("__Secure-1PSID", "__Secure-1PSIDTS")
PROFILES_ROOT = Path.home() / "Library/Application Support/Firefox/Profiles"
IP_URL = "https://ifconfig.me"
SSH_OPTIONS = ["-o", "ControlMaster=no", "-o", "ControlPath=none"]


class CookieError(Exception):
    """A safe diagnostic containing no cookie values or subprocess output."""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="透過 SSH SOCKS 與一次性 Firefox profile 取得 Gemini cookie。"
    )
    parser.add_argument("--host", default="movie-nas", help="SSH 主機別名（預設：movie-nas）")
    parser.add_argument("--socks-port", type=int, default=1080, help="本機 SOCKS 埠（預設：1080）")
    parser.add_argument(
        "--firefox", default="/Applications/Firefox.app/Contents/MacOS/firefox",
        help="Firefox 執行檔路徑",
    )
    parser.add_argument("--tz", default="America/Chicago", help="Firefox 時區（預設：America/Chicago）")
    parser.add_argument("--expected-ip", help="預期出口 IP；未指定時向 VM 查詢")
    parser.add_argument("--extract-only", action="store_true", help="只從既有 Firefox profile 抽取")
    parser.add_argument("--profile-match", default="gemini-us", help="既有 profile 名稱包含的字串")
    args = parser.parse_args(argv)
    if not 1 <= args.socks_port <= 65535:
        parser.error("--socks-port 必須介於 1 與 65535")
    return args


def require_commands(commands):
    for command in commands:
        if shutil.which(command) is None:
            raise CookieError(f"找不到必要指令：{command}")


def preflight(args):
    require_commands(("ssh", "curl", "pbcopy", "pbpaste"))
    firefox = Path(args.firefox)
    if not firefox.is_file() or not os.access(firefox, os.X_OK):
        raise CookieError("找不到可執行的 Firefox；請用 --firefox 指定路徑。")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", args.socks_port))
    except OSError:
        raise CookieError(f"127.0.0.1:{args.socks_port} 已被使用或無法綁定。") from None


def start_tunnel(args):
    return subprocess.Popen([
        "ssh", *SSH_OPTIONS, "-D", f"127.0.0.1:{args.socks_port}",
        "-C", "-N", args.host,
    ], stdout=subprocess.DEVNULL)


def wait_for_tunnel(tunnel, port, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tunnel.poll() is not None:
            raise CookieError("SSH tunnel 已退出；請確認 SSH 設定與連線。")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise CookieError("等待 SSH SOCKS tunnel 超時（15 秒）。")


def check_egress(args):
    try:
        proxy_ip = subprocess.run([
            "curl", "--socks5-hostname", f"127.0.0.1:{args.socks_port}",
            "-s", "--max-time", "15", IP_URL,
        ], capture_output=True, text=True, check=True, timeout=20).stdout.strip()
        expected_ip = args.expected_ip
        if expected_ip is None:
            expected_ip = subprocess.run([
                "ssh", *SSH_OPTIONS, "-o", "ClearAllForwardings=yes", args.host,
                "curl", "-s", "--max-time", "15", IP_URL,
            ], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        expected_ip = expected_ip.strip()
    except (OSError, subprocess.SubprocessError):
        raise CookieError("無法查詢出口 IP；尚未啟動 Firefox。") from None
    print(f"SOCKS 出口 IP：{proxy_ip or '（空白）'}")
    print(f"預期出口 IP：{expected_ip or '（空白）'}")
    if not proxy_ip or not expected_ip or proxy_ip != expected_ip:
        raise CookieError("出口 IP 為空白或不一致；尚未啟動 Firefox。")


def write_user_js(profile, port):
    prefs = {
        "network.proxy.type": 1,
        "network.proxy.socks": "127.0.0.1",
        "network.proxy.socks_port": port,
        "network.proxy.socks_version": 5,
        "network.proxy.socks_remote_dns": True,
        "network.trr.mode": 5,
        "media.peerconnection.enabled": False,
        "browser.shell.checkDefaultBrowser": False,
        "browser.aboutwelcome.enabled": False,
        "browser.startup.firstrunSkipsHomepage": True,
        "browser.startup.homepage_override.mstone": "ignore",
        "startup.homepage_welcome_url": "",
        "startup.homepage_welcome_url.additional": "",
        "datareporting.policy.dataSubmissionEnabled": False,
        "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
        "datareporting.healthreport.uploadEnabled": False,
        "toolkit.telemetry.enabled": False,
        "toolkit.telemetry.unified": False,
        "toolkit.telemetry.reportingpolicy.firstRun": False,
        "browser.newtabpage.activity-stream.feeds.telemetry": False,
        "browser.newtabpage.activity-stream.telemetry": False,
    }
    content = "".join(
        f"user_pref({json.dumps(name)}, {json.dumps(value)});\n"
        for name, value in prefs.items()
    )
    (Path(profile) / "user.js").write_text(content, encoding="utf-8")


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def launch_browser(args, profile):
    browser_env = os.environ.copy()
    browser_env["TZ"] = args.tz
    print("請在 Firefox 登入 Google，等 Gemini 載入完成後，按 ⌘Q 結束 Firefox。")
    print("請勿登出帳號：登出會使 cookie 失效。")
    browser = subprocess.Popen([
        args.firefox, "-profile", str(profile), "-no-remote", "-new-instance",
        "https://gemini.google.com/app",
    ], env=browser_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if browser.wait() != 0:
            raise CookieError("Firefox 未正常結束；請重新執行並完成登入。")
    finally:
        stop_process(browser)


def extract_cookies(profile):
    source = Path(profile) / "cookies.sqlite"
    if not source.is_file():
        raise CookieError("找不到 cookies.sqlite；登入可能尚未完成。")
    private_dir = Path(tempfile.mkdtemp(prefix="gemini-cookie-db-"))
    try:
        copied_db = private_dir / "cookies.sqlite"
        for suffix in ("", "-wal", "-shm"):
            original = source.with_name(source.name + suffix)
            if original.exists():
                shutil.copy2(original, private_dir / original.name)
        connection = sqlite3.connect(copied_db)
        try:
            rows = connection.execute(
                "SELECT name, value, expiry FROM moz_cookies "
                "WHERE host LIKE '%google.com' "
                "AND name IN ('__Secure-1PSID', '__Secure-1PSIDTS') "
                "ORDER BY expiry DESC"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        raise CookieError("無法讀取 cookie 資料庫；請結束 Firefox 後重試。") from None
    finally:
        shutil.rmtree(private_dir)
    values = {}
    for name, value, _ in rows:
        values.setdefault(name, value)
    if any(not values.get(name) for name in COOKIE_NAMES):
        raise CookieError("缺少必要 cookie；登入尚未完成，請確認 Gemini 已載入。")
    return tuple(values[name] for name in COOKIE_NAMES)


def select_profile(match, root=None):
    root = PROFILES_ROOT if root is None else Path(root)
    if not root.is_dir():
        raise CookieError("找不到 Firefox Profiles 目錄。")
    candidates = [
        profile for profile in root.iterdir()
        if profile.is_dir() and match in profile.name
        and (profile / "cookies.sqlite").is_file()
    ]
    if not candidates:
        raise CookieError("找不到名稱相符且含 cookies.sqlite 的 Firefox profile。")
    return max(candidates, key=lambda profile: (profile / "cookies.sqlite").stat().st_mtime_ns)


def copy_cookies(cookies):
    payload = "\n".join(cookies)
    try:
        subprocess.run(
            ["pbcopy"], input=payload, text=True, check=True, timeout=5,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        raise CookieError("無法將 cookie 複製到剪貼簿。") from None
    for name, value in zip(COOKIE_NAMES, cookies):
        print(f"{name}：{len(value)} 字元")
    return payload


def wait_for_enter(timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([sys.stdin], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            return
        chunk = os.read(sys.stdin.fileno(), 4096)
        if not chunk or b"\n" in chunk:
            return


def clear_clipboard(payload):
    try:
        current = subprocess.run(
            ["pbpaste"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=True, timeout=5,
        ).stdout
        if current == payload.encode("utf-8"):
            subprocess.run(
                ["pbcopy"], input="", text=True, check=True, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print("已清除本次 cookie 的剪貼簿內容。")
    except (OSError, subprocess.SubprocessError):
        raise CookieError("無法確認或清除剪貼簿；請手動檢查。") from None


def hand_off(payload):
    print("兩行 cookie 已複製到剪貼簿。")
    print("請在與 bot 的私人聊天室傳送 /setcookie，然後貼上（⌘V）。")
    print("貼上後按 Enter；120 秒後也會清除剪貼簿（僅限內容仍是本次 cookie）。")
    try:
        wait_for_enter()
    finally:
        clear_clipboard(payload)


def acquire_cookies(args):
    tunnel = None
    profile = None
    try:
        preflight(args)
        tunnel = start_tunnel(args)
        wait_for_tunnel(tunnel, args.socks_port)
        check_egress(args)
        profile = Path(tempfile.mkdtemp(prefix="gemini-cookie-"))
        write_user_js(profile, args.socks_port)
        launch_browser(args, profile)
        return copy_cookies(extract_cookies(profile))
    finally:
        try:
            stop_process(tunnel)
        finally:
            if profile is not None:
                shutil.rmtree(profile)


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.extract_only:
            require_commands(("pbcopy", "pbpaste"))
            payload = copy_cookies(extract_cookies(select_profile(args.profile_match)))
        else:
            payload = acquire_cookies(args)
        hand_off(payload)
        return 0
    except CookieError as error:
        print(f"錯誤：{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中止並清理暫存資料。", file=sys.stderr)
        return 130
    except Exception:
        # Never render arbitrary exceptions: they may contain subprocess input.
        print("錯誤：操作失敗；暫存資料已執行清理。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
