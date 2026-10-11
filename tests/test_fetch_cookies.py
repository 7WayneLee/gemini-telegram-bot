"""Offline cookie-tool checks: subprocesses and network access are always mocked."""

import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/fetch_cookies.py"
SPEC = importlib.util.spec_from_file_location("fetch_cookies", MODULE_PATH)
fetch_cookies = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch_cookies)

FAKE_PSID = "FAKE_1PSID_FOR_TEST"
FAKE_PSIDTS = "FAKE_1PSIDTS_FOR_TEST"
FAKE_PAYLOAD = FAKE_PSID + "\n" + FAKE_PSIDTS


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    run = Mock(side_effect=AssertionError("Subprocess must be explicitly mocked."))
    popen = Mock(side_effect=AssertionError("Subprocess must be explicitly mocked."))
    monkeypatch.setattr(fetch_cookies.subprocess, "run", run)
    monkeypatch.setattr(fetch_cookies.subprocess, "Popen", popen)
    monkeypatch.setattr(fetch_cookies.socket, "socket", Mock(
        side_effect=AssertionError("Network access is forbidden in tests."),
    ))
    monkeypatch.setattr(fetch_cookies.socket, "create_connection", Mock(
        side_effect=AssertionError("Network access is forbidden in tests."),
    ))
    return SimpleNamespace(run=run, popen=popen)


@pytest.fixture
def private_dirs(monkeypatch):
    created = []
    original = fetch_cookies.tempfile.mkdtemp

    def tracked_mkdtemp(*args, **kwargs):
        directory = Path(original(*args, **kwargs))
        assert directory.stat().st_mode & 0o777 == 0o700
        created.append(directory)
        return str(directory)

    monkeypatch.setattr(fetch_cookies.tempfile, "mkdtemp", tracked_mkdtemp)
    return created


def write_database(profile, rows=None):
    profile.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(profile / "cookies.sqlite") as connection:
        connection.execute(
            "CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, expiry INTEGER)"
        )
        connection.executemany(
            "INSERT INTO moz_cookies VALUES (?, ?, ?, ?)",
            rows if rows is not None else [
                ("__Secure-1PSID", FAKE_PSID, ".google.com", 100),
                ("__Secure-1PSIDTS", FAKE_PSIDTS, "gemini.google.com", 100),
            ],
        )


def assert_cookie_result(actual, expected):
    # Keep credential-shaped fixtures out of pytest's assertion diagnostics.
    matches = actual == expected
    assert matches, "Cookie selection or clipboard payload was incorrect."


def assert_no_cookie_output(captured):
    leaked = any(
        value in captured.out or value in captured.err
        for value in (FAKE_PSID, FAKE_PSIDTS)
    )
    assert not leaked, "Cookie values must never appear in stdout or stderr."


def test_user_js_proxy_and_leak_prevention(tmp_path):
    """Firefox must route DNS through SOCKS and prevent DoH/WebRTC IP leaks."""
    fetch_cookies.write_user_js(tmp_path, 2345)
    content = (tmp_path / "user.js").read_text()
    for preference in (
        'user_pref("network.proxy.type", 1);',
        'user_pref("network.proxy.socks", "127.0.0.1");',
        'user_pref("network.proxy.socks_port", 2345);',
        'user_pref("network.proxy.socks_version", 5);',
        'user_pref("network.proxy.socks_remote_dns", true);',
        'user_pref("network.trr.mode", 5);',
        'user_pref("media.peerconnection.enabled", false);',
        'user_pref("browser.shell.checkDefaultBrowser", false);',
        'user_pref("browser.aboutwelcome.enabled", false);',
        'user_pref("datareporting.policy.dataSubmissionEnabled", false);',
        'user_pref("toolkit.telemetry.enabled", false);',
    ):
        assert preference in content


def test_extract_newest_expiry_without_output(tmp_path, private_dirs, capsys):
    """Renewed Google cookies must win without exposing any values in diagnostics."""
    write_database(tmp_path, [
        ("__Secure-1PSID", FAKE_PSIDTS, ".google.com", 10),
        ("__Secure-1PSID", FAKE_PSID, ".google.com", 20),
        ("__Secure-1PSIDTS", FAKE_PSID, "gemini.google.com", 10),
        ("__Secure-1PSIDTS", FAKE_PSIDTS, "gemini.google.com", 20),
        ("__Secure-1PSID", FAKE_PSIDTS, "example.org", 999),
    ])
    assert_cookie_result(fetch_cookies.extract_cookies(tmp_path), (FAKE_PSID, FAKE_PSIDTS))
    assert_no_cookie_output(capsys.readouterr())
    assert len(private_dirs) == 1
    assert not private_dirs[0].exists()
    assert (tmp_path / "cookies.sqlite").exists()


@pytest.mark.parametrize("missing", fetch_cookies.COOKIE_NAMES)
def test_extract_missing_cookie_aborts(tmp_path, private_dirs, missing, capsys):
    """Incomplete Google login must fail instead of copying a partial credential."""
    write_database(tmp_path, [
        (name, value, ".google.com", 100)
        for name, value in zip(fetch_cookies.COOKIE_NAMES, (FAKE_PSID, FAKE_PSIDTS))
        if name != missing
    ])
    with pytest.raises(fetch_cookies.CookieError, match="登入尚未完成"):
        fetch_cookies.extract_cookies(tmp_path)
    assert_no_cookie_output(capsys.readouterr())
    assert all(not path.exists() for path in private_dirs)


def test_extract_wal_with_open_connection(tmp_path, private_dirs, capsys):
    """An active Firefox database may contain its newest cookies only in the WAL."""
    database = tmp_path / "cookies.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, expiry INTEGER)"
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.executemany("INSERT INTO moz_cookies VALUES (?, ?, ?, ?)", [
            ("__Secure-1PSID", FAKE_PSID, ".google.com", 100),
            ("__Secure-1PSIDTS", FAKE_PSIDTS, ".google.com", 100),
        ])
        connection.commit()
        assert database.with_name("cookies.sqlite-wal").stat().st_size > 0
        assert database.with_name("cookies.sqlite-shm").exists()
        # Ignoring the WAL proves that neither committed row reached the main DB.
        with sqlite3.connect(database.as_uri() + "?immutable=1", uri=True) as base:
            assert base.execute("SELECT COUNT(*) FROM moz_cookies").fetchone()[0] == 0
        assert_cookie_result(fetch_cookies.extract_cookies(tmp_path), (FAKE_PSID, FAKE_PSIDTS))
        assert_no_cookie_output(capsys.readouterr())
        assert all(not path.exists() for path in private_dirs)
    finally:
        connection.close()


def test_extract_copy_removed_on_database_error(tmp_path, private_dirs):
    """A malformed database must not leave a sensitive temporary copy behind."""
    (tmp_path / "cookies.sqlite").write_bytes(b"invalid database")
    with pytest.raises(fetch_cookies.CookieError, match="無法讀取"):
        fetch_cookies.extract_cookies(tmp_path)
    assert all(not path.exists() for path in private_dirs)


@pytest.mark.parametrize("proxy_ip, vm_ip", [
    ("203.0.113.1", "203.0.113.2"),
    ("", "203.0.113.1"),
    ("203.0.113.1", ""),
    ("", ""),
])
def test_bad_egress_aborts_before_browser(
    monkeypatch, offline, private_dirs, proxy_ip, vm_ip,
):
    """Wrong or empty egress must stop acquisition before Google can see the browser."""
    args = fetch_cookies.parse_args([])
    tunnel = Mock()
    tunnel.poll.return_value = None
    monkeypatch.setattr(fetch_cookies, "preflight", Mock())
    monkeypatch.setattr(fetch_cookies, "wait_for_tunnel", Mock())
    launcher = Mock()
    monkeypatch.setattr(fetch_cookies, "launch_browser", launcher)
    offline.popen.side_effect = None
    offline.popen.return_value = tunnel
    offline.run.side_effect = [SimpleNamespace(stdout=proxy_ip), SimpleNamespace(stdout=vm_ip)]
    with pytest.raises(fetch_cookies.CookieError, match="出口 IP"):
        fetch_cookies.acquire_cookies(args)
    launcher.assert_not_called()
    assert not private_dirs
    tunnel.terminate.assert_called_once()
    proxy_command, remote_command = [call.args[0] for call in offline.run.call_args_list]
    assert "--socks5-hostname" in proxy_command
    assert "ClearAllForwardings=yes" in remote_command
    assert "ControlMaster=no" in remote_command
    assert "ControlPath=none" in remote_command


def test_explicit_expected_ip_skips_remote_query(offline):
    """An operator-supplied egress IP must avoid an unnecessary second SSH connection."""
    args = fetch_cookies.parse_args(["--expected-ip", "203.0.113.1"])
    offline.run.side_effect = None
    offline.run.return_value = SimpleNamespace(stdout="203.0.113.1\n")
    fetch_cookies.check_egress(args)
    offline.run.assert_called_once()
    assert offline.run.call_args.args[0][0] == "curl"


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_full_cleanup_success_browser_failure_and_interrupt(
    tmp_path, monkeypatch, offline, private_dirs, failure, capsys,
):
    """Both processes and the disposable profile must be cleaned on all browser outcomes."""
    args = fetch_cookies.parse_args(["--socks-port", "2345", "--tz", "America/Chicago"])
    other_profile = tmp_path / "operator-profile"
    write_database(other_profile)
    untouched = (other_profile / "cookies.sqlite").read_bytes()
    tunnel = Mock()
    tunnel.poll.return_value = None
    browser = Mock()
    browser.poll.return_value = 0 if failure is None else None
    browser.wait.side_effect = [0] if failure is None else [failure("browser interrupted"), 0]
    monkeypatch.setattr(fetch_cookies, "preflight", Mock())
    monkeypatch.setattr(fetch_cookies, "wait_for_tunnel", Mock())

    def launch(command, **kwargs):
        if command[0] == "ssh":
            assert command == [
                "ssh", "-o", "ControlMaster=no", "-o", "ControlPath=none",
                "-D", "127.0.0.1:2345", "-C", "-N", "movie-nas",
            ]
            return tunnel
        assert command[0] == args.firefox
        assert kwargs["env"]["TZ"] == args.tz
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert command[3:] == ["-no-remote", "-new-instance", "https://gemini.google.com/app"]
        profile = Path(command[2])
        assert (profile / "user.js").exists()
        write_database(profile)
        return browser

    offline.popen.side_effect = launch
    offline.run.side_effect = [
        SimpleNamespace(stdout="203.0.113.1"),
        SimpleNamespace(stdout="203.0.113.1"),
        SimpleNamespace(returncode=0),
    ]
    if failure is None:
        assert_cookie_result(fetch_cookies.acquire_cookies(args), FAKE_PAYLOAD)
        assert_cookie_result(offline.run.call_args.kwargs["input"], FAKE_PAYLOAD)
    else:
        with pytest.raises(failure):
            fetch_cookies.acquire_cookies(args)
        browser.terminate.assert_called_once()
    tunnel.terminate.assert_called_once()
    assert private_dirs
    assert all(not path.exists() for path in private_dirs)
    assert (other_profile / "cookies.sqlite").read_bytes() == untouched
    assert_no_cookie_output(capsys.readouterr())


def test_tunnel_killed_when_termination_times_out():
    """A tunnel that ignores termination must be killed rather than outlive the tool."""
    tunnel = Mock()
    tunnel.poll.return_value = None
    tunnel.wait.side_effect = [subprocess.TimeoutExpired("ssh", 3), 0]
    fetch_cookies.stop_process(tunnel)
    tunnel.terminate.assert_called_once()
    tunnel.kill.assert_called_once()
    assert tunnel.wait.call_count == 2


def test_preflight_missing_command_fails_before_tunnel(monkeypatch, offline):
    """Missing dependencies must produce a clear error before starting any process."""
    monkeypatch.setattr(fetch_cookies.shutil, "which", lambda name: None)
    with pytest.raises(fetch_cookies.CookieError, match="必要指令：ssh"):
        fetch_cookies.acquire_cookies(fetch_cookies.parse_args([]))
    offline.popen.assert_not_called()


def test_preflight_occupied_port_fails_before_tunnel(tmp_path, monkeypatch, offline):
    """A busy local SOCKS port must fail fast instead of using another process's proxy."""
    firefox = tmp_path / "firefox"
    firefox.touch(mode=0o700)
    monkeypatch.setattr(fetch_cookies.shutil, "which", lambda name: "/fake/" + name)
    listener = Mock()
    listener.bind.side_effect = OSError("port busy")
    socket_context = Mock()
    socket_context.__enter__ = Mock(return_value=listener)
    socket_context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(fetch_cookies.socket, "socket", Mock(return_value=socket_context))
    with pytest.raises(fetch_cookies.CookieError, match="已被使用"):
        fetch_cookies.acquire_cookies(fetch_cookies.parse_args(["--firefox", str(firefox)]))
    offline.popen.assert_not_called()


@pytest.mark.parametrize("clipboard", [
    FAKE_PAYLOAD.encode(),
    b"operator's newer clipboard", 
    (FAKE_PAYLOAD + "\n").encode(),
    FAKE_PAYLOAD.replace("\n", "\r\n").encode(),
])
def test_clipboard_cleared_only_for_exact_payload(offline, clipboard, capsys):
    """Cleanup must preserve changed clipboard contents, including newline differences."""
    offline.run.side_effect = None
    offline.run.return_value = SimpleNamespace(stdout=clipboard)
    fetch_cookies.clear_clipboard(FAKE_PAYLOAD)
    cleared = clipboard == FAKE_PAYLOAD.encode()
    assert offline.run.call_count == (2 if cleared else 1)
    if cleared:
        assert offline.run.call_args.args[0] == ["pbcopy"]
        assert offline.run.call_args.kwargs["input"] == ""
    assert_no_cookie_output(capsys.readouterr())


@pytest.mark.parametrize("interrupted", [False, True])
def test_handoff_clears_on_enter_timeout_or_interrupt(monkeypatch, offline, interrupted, capsys):
    """The clipboard hand-off must clean up even if the operator presses Ctrl+C."""
    wait = Mock(side_effect=KeyboardInterrupt if interrupted else None)
    monkeypatch.setattr(fetch_cookies, "wait_for_enter", wait)
    offline.run.side_effect = None
    offline.run.return_value = SimpleNamespace(stdout=FAKE_PAYLOAD.encode())
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            fetch_cookies.hand_off(FAKE_PAYLOAD)
    else:
        fetch_cookies.hand_off(FAKE_PAYLOAD)
    wait.assert_called_once_with()
    assert offline.run.call_count == 2
    output = capsys.readouterr()
    assert "私人聊天室" in output.out
    assert "/setcookie" in output.out
    assert_no_cookie_output(output)


def test_wait_for_enter_uses_120_second_timeout(monkeypatch):
    """The hand-off must return after 120 seconds when the operator supplies no input."""
    monkeypatch.setattr(fetch_cookies.time, "monotonic", lambda: 0)
    select = Mock(return_value=([], [], []))
    monkeypatch.setattr(fetch_cookies.select, "select", select)
    fetch_cookies.wait_for_enter()
    assert select.call_args.args[3] == 120


def test_extract_only_newest_profile_stays_untouched(
    tmp_path, monkeypatch, offline, private_dirs, capsys,
):
    """Legacy extraction must pick the newest matching database without altering any profile."""
    older = tmp_path / "older.gemini-us"
    newest = tmp_path / "newest.gemini-us"
    unrelated = tmp_path / "other.default"
    empty = tmp_path / "empty.gemini-us"
    empty.mkdir()
    write_database(older, [
        ("__Secure-1PSID", FAKE_PSIDTS, ".google.com", 100),
        ("__Secure-1PSIDTS", FAKE_PSID, ".google.com", 100),
    ])
    write_database(newest)
    write_database(unrelated)
    for index, profile in enumerate((older, newest, unrelated), start=1):
        os.utime(profile / "cookies.sqlite", (index * 100, index * 100))
    before = {path: path.read_bytes() for path in tmp_path.glob("*/cookies.sqlite")}
    monkeypatch.setattr(fetch_cookies, "PROFILES_ROOT", tmp_path)
    monkeypatch.setattr(fetch_cookies.shutil, "which", lambda name: "/fake/" + name)
    monkeypatch.setattr(fetch_cookies, "wait_for_enter", Mock())
    launcher = Mock()
    monkeypatch.setattr(fetch_cookies, "launch_browser", launcher)
    offline.run.side_effect = [
        SimpleNamespace(returncode=0),
        SimpleNamespace(stdout=b"operator's newer clipboard"),
    ]
    assert fetch_cookies.main(["--extract-only", "--profile-match", "gemini-us"]) == 0
    assert_cookie_result(offline.run.call_args_list[0].kwargs["input"], FAKE_PAYLOAD)
    assert offline.run.call_args_list[0].args[0] == ["pbcopy"]
    assert len(offline.run.call_args_list) == 2
    offline.popen.assert_not_called()
    launcher.assert_not_called()
    assert all(path.read_bytes() == content for path, content in before.items())
    assert empty.is_dir()
    assert all(not path.exists() for path in private_dirs)
    assert_no_cookie_output(capsys.readouterr())


def test_main_does_not_render_subprocess_exception(monkeypatch, offline, tmp_path, capsys):
    """A subprocess failure carrying sensitive output must yield only a safe diagnostic."""
    write_database(tmp_path)
    monkeypatch.setattr(fetch_cookies, "select_profile", lambda match: tmp_path)
    monkeypatch.setattr(fetch_cookies.shutil, "which", lambda name: "/fake/" + name)
    offline.run.side_effect = subprocess.TimeoutExpired("pbcopy", 5, output=FAKE_PAYLOAD)
    assert fetch_cookies.main(["--extract-only"]) == 1
    assert_no_cookie_output(capsys.readouterr())
