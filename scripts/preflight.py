"""Operator preflight for the G1 connectivity gate.

Validates that .env is complete and coherent BEFORE any live Gemini call, so a
failed gate points at one obvious cause instead of a stack trace.

Never prints secret values. Cookies and tokens are reported only as
present/absent plus a length, never echoed.

Usage:
    uv run python scripts/preflight.py             # offline checks only
    uv run python scripts/preflight.py --check-ip  # also verify egress IP via proxy
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

FAIL = "FAIL"
WARN = "WARN"
OK = "ok  "

_problems: list[str] = []
_warnings: list[str] = []


def report(status: str, label: str, detail: str = "") -> None:
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if status == FAIL:
        _problems.append(label)
    elif status == WARN:
        _warnings.append(label)


def check_env_file() -> None:
    print("\n.env")
    path = Path(".env")
    if not path.is_file():
        report(FAIL, ".env exists", "copy .env.example to .env and fill it in")
        return
    report(OK, ".env exists")

    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        report(WARN, ".env permissions", f"{mode:o} — run: chmod 600 .env")
    else:
        report(OK, ".env permissions", f"{mode:o}")


def check_settings() -> object | None:
    print("\nconfiguration")
    try:
        from gemini_tg_bot.config import Settings

        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - operator-facing diagnostic
        report(FAIL, "settings load", f"{type(exc).__name__}: {exc}")
        return None
    report(OK, "settings load")

    placeholders = {
        "TELEGRAM_BOT_TOKEN": settings.telegram_bot_token.get_secret_value(),
        "GEMINI_SECURE_1PSID": settings.gemini_secure_1psid.get_secret_value(),
        "GEMINI_SECURE_1PSIDTS": settings.gemini_secure_1psidts.get_secret_value(),
    }
    for name, value in placeholders.items():
        if not value:
            report(FAIL, name, "empty")
        elif value.startswith("FAKE_"):
            report(FAIL, name, "still the .env.example placeholder")
        else:
            report(OK, name, f"set, length {len(value)}")

    if settings.admin_user_id:
        report(OK, "ADMIN_USER_ID", str(settings.admin_user_id))
    else:
        report(FAIL, "ADMIN_USER_ID", "not set")

    allowed = set(settings.allowed_user_ids)
    if not allowed:
        report(
            WARN,
            "ALLOWED_USER_IDS",
            "empty — everyone is denied except the admin; G1 needs at least one",
        )
    else:
        report(OK, "ALLOWED_USER_IDS", f"{len(allowed)} id(s)")

    if settings.max_concurrency != 1:
        report(WARN, "MAX_CONCURRENCY", f"{settings.max_concurrency}, expected 1")
    else:
        report(OK, "MAX_CONCURRENCY", "1")

    return settings


def check_cookie_path(settings: object) -> None:
    print("\ncookie persistence")
    path = Path(os.fspath(getattr(settings, "gemini_cookie_path")))
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report(FAIL, "cookie dir creatable", f"{type(exc).__name__}: {exc}")
        return

    probe = path / ".preflight_write_test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        report(FAIL, "cookie dir writable", f"{type(exc).__name__}: {exc}")
        return
    report(OK, "cookie dir writable", str(path))

    # The library caches as .cached_cookies_{1PSID}.json. Report only the count:
    # the filename embeds the raw cookie, so it must never be printed.
    cached = list(path.glob(".cached_cookies_*.json"))
    if cached:
        newest = max(c.stat().st_mtime for c in cached)
        from datetime import datetime

        stamp = datetime.fromtimestamp(newest).isoformat(timespec="seconds")
        report(OK, "existing cookie cache", f"{len(cached)} file(s), newest {stamp}")
    else:
        report(OK, "existing cookie cache", "none yet (written on first refresh)")


def check_proxy(settings: object, check_ip: bool) -> None:
    print("\nnetwork path")
    proxy = getattr(settings, "gemini_proxy", None)
    if not proxy:
        report(
            WARN,
            "GEMINI_PROXY",
            "empty — only correct if this process runs ON the VM that made the cookies",
        )
    else:
        report(OK, "GEMINI_PROXY", str(proxy))

    if not check_ip:
        print("       (re-run with --check-ip to verify the egress IP)")
        return

    try:
        from curl_cffi import requests as cc

        direct = cc.get("https://api.ipify.org", timeout=15).text.strip()
        report(OK, "direct egress IP", direct)
        if proxy:
            via = cc.get("https://api.ipify.org", proxy=str(proxy), timeout=20).text.strip()
            report(OK, "proxied egress IP", via)
            if via == direct:
                report(
                    FAIL,
                    "proxy actually in use",
                    "proxied IP equals direct IP — SOCKS tunnel is not carrying traffic",
                )
            else:
                report(OK, "proxy actually in use", "egress differs from direct")
            print(
                "\n  >>> Confirm the proxied IP above is your VM's public IP.\n"
                "      If it is not, the cookie's birth IP and use IP disagree and\n"
                "      Google will invalidate the session."
            )
    except Exception as exc:  # noqa: BLE001 - operator-facing diagnostic
        report(WARN, "egress IP check", f"{type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-ip",
        action="store_true",
        help="verify egress IP through the configured proxy (makes a network call)",
    )
    args = parser.parse_args()

    print("preflight — no Gemini call is made; no secret value is printed")
    check_env_file()
    settings = check_settings()
    if settings is not None:
        check_cookie_path(settings)
        check_proxy(settings, args.check_ip)

    print()
    if _problems:
        print(f"BLOCKED: {len(_problems)} problem(s): {', '.join(_problems)}")
        return 1
    if _warnings:
        print(f"READY, with {len(_warnings)} warning(s): {', '.join(_warnings)}")
        return 0
    print("READY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
