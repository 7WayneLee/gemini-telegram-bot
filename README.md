# Gemini Telegram Bot

[![CI](https://github.com/7WayneLee/gemini-telegram-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/7WayneLee/gemini-telegram-bot/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

**English** · [正體中文](README.zh-TW.md)

A self-hosted Telegram bot that talks to Google Gemini through
[`gemini-webapi`](https://github.com/HanaokaYuzu/Gemini-API).

**This is not the official Gemini API.** Authentication is a browser cookie, not an
API key — which makes cookie handling the part that actually matters.

### What it does

- **Streaming replies** with Markdown converted to Telegram HTML — code blocks,
  nested lists, long messages split without breaking formatting
- **Images** sent as albums, passed to Telegram by URL where possible so they never
  transit your server
- **File uploads** — send a photo or PDF and ask about it
- **Dynamic model and gem selection** via `/model` and `/gem`
- **Deep Research** as a background task that survives a restart
- **Allowlist by default** — an unconfigured bot talks to nobody
- **Cookie recovery from chat** — refresh credentials with `/setcookie`, no SSH, no
  restart
- **Runs in ~150 MB** on a small shared VM

---

## ⚠️ Before you start

### The bot holds a full web session for a real Google account

Not a scoped API token — a complete logged-in session. If the account has Gemini
extensions enabled, **anyone who can talk to the bot can read that account's mailbox
via `@Gmail`**, and the same applies to `@Google Drive`.

That is why the allowlist is a core feature rather than an option. With
`ALLOWED_USER_IDS` unset the bot **rejects every message**. Do not change that
default.

### Use a separate, secondary Google account

**Do not use your primary account.** This is a reverse-engineered API, it violates
Google's Terms of Service, and the account can be restricted. See
[Known risks](#known-risks).

---

## Quick start

Requires **Python 3.12+** and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/7WayneLee/gemini-telegram-bot.git
cd gemini-telegram-bot
uv sync

cp .env.example .env && chmod 600 .env
# Fill in the five required values — see Configuration below.
# Cookies are the tricky part; see the next section.

uv run python scripts/preflight.py        # checks config, makes no Gemini request
uv run python -m gemini_tg_bot --dry-run  # end-to-end smoke test, no credentials
uv run python -m gemini_tg_bot            # run
```

`--dry-run` walks the whole pipeline with fake transports, so you can confirm your
environment works before dealing with cookies at all.

---

## Getting the cookies

### The cookie's birth IP must match its use IP

If you obtain a cookie from a browser in one country and use it on a server in
another, Google sees the session jump geographically and **invalidates it
repeatedly**. You end up re-authenticating every few hours without knowing why.

So log in **through the server's egress**. If you are running the bot on the same
machine as your browser, you can skip the tunnel.

### Steps

1. **Open a SOCKS tunnel to the server** (replace `your-vm` with your SSH alias) and
   leave it running:

   ```bash
   ssh -D 1080 -C -q -N your-vm
   ```

2. **Launch a dedicated Firefox profile**, with a timezone matching the server's
   region:

   ```bash
   TZ=America/Chicago firefox -P gemini
   ```

   **Use Firefox.** Chromium's Device Bound Session Credentials make the cookie last
   only hours and prevent refresh.

3. **Settings → Network Settings → Manual proxy**: SOCKS Host `127.0.0.1`, Port
   `1080`, **SOCKS v5**, and tick **"Proxy DNS when using SOCKS v5"** — without it,
   DNS goes out locally and leaks your real location.

4. **Verify your egress IP** before logging in to `gemini.google.com`:

   ```bash
   curl --socks5-hostname 127.0.0.1:1080 https://ifconfig.me
   ```

5. **Get the cookies.** Either read them from the profile automatically:

   ```bash
   sh scripts/grab_cookies.sh          # copies both values to the clipboard
   ```

   …or copy `__Secure-1PSID` and `__Secure-1PSIDTS` by hand from F12 → Network.

6. **Apply them**: paste into `.env`, or send `/setcookie` to the bot and paste there
   — the latter needs no restart.

> The first login from a new country will likely trigger 2FA and a new-device
> notification. That is expected, and it settles afterwards.

### Cookies rotate on their own

Once running, the bot rotates `__Secure-1PSIDTS` in the background and persists it to
`GEMINI_COOKIE_PATH`. So:

- The copy in `.env` goes stale as soon as the bot starts; the stored one is what is
  in use
- Restarting does not require new cookies — only a session Google has invalidated does
- `GEMINI_COOKIE_PATH` must be on a mounted volume, or a container rebuild loses the
  session
- `/setcookie` persists to a `0600` override that takes precedence on the next start,
  so hot updates survive restarts
- **Only `__Secure-1PSID` really matters.** A stale or even invalid `__Secure-1PSIDTS`
  does not break authentication: a valid `__Secure-1PSID` is enough for the client to
  obtain a fresh one. You need to re-acquire cookies only when Google invalidates
  `__Secure-1PSID` itself

> When authentication does fail, the upstream error blames `SECURE_1PSIDTS` and says it
> "could get expired frequently". That is misleading — the value you actually have to
> replace is almost always `__Secure-1PSID`.

### Testing locally against a remote server's IP

`GEMINI_PROXY` accepts SOCKS, so with the tunnel up you can run the bot on your own
machine while still presenting the server's egress IP:

```
GEMINI_PROXY=socks5h://127.0.0.1:1080
```

Use `socks5h`, not `socks5` — the `h` sends DNS through the tunnel too.

> If the same Google account is logged in to a browser on that machine, the client may
> authenticate from those cookies rather than the ones in `.env`, so a local run can
> succeed while the server fails. A headless server has no browser profile, so this
> only ever hides problems locally.

---

## Configuration

`.env.example` is fully commented. Five values are required:

| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from BotFather |
| `ADMIN_USER_ID` | Telegram user ID allowed to run admin commands |
| `ALLOWED_USER_IDS` | Comma-separated allowlist. **Empty means nobody is allowed** |
| `GEMINI_SECURE_1PSID` | Gemini cookie |
| `GEMINI_SECURE_1PSIDTS` | Gemini cookie |

Everything else has a safe default. Three worth knowing:

| Variable | Default | Why |
|---|---|---|
| `MAX_CONCURRENCY` | `1` | A single Gemini account has its own quota ceiling, so concurrency buys little |
| `ENABLE_VIDEO_GENERATION` | `false` | A single clip can be tens of MB |
| `ENABLE_AUDIO_GENERATION` | `false` | Same |

---

## Commands

### Everyone on the allowlist

| Command | Behaviour |
|---|---|
| `/start`, `/help` | Show available commands |
| `/new` | End the current conversation and start a fresh one |
| `/model` | Pick a model from an inline keyboard, listed dynamically |
| `/gem` | Pick a gem to apply to the conversation |
| `/temp` | Toggle temporary mode — nothing written to Gemini history |
| `/img <prompt>` | **Explicitly ask for generation.** Without that wording Gemini tends to return web search results instead of an AI-generated image |
| `/research <topic>` | Submit a Deep Research task; returns a task id immediately |
| `/research_status` | Status of this chat's research tasks |
| `/status` | Model, session id, last cookie refresh, queue depth, usage, egress estimate |
| Plain text | Answered with streaming |
| Photo / document | Passed to Gemini; the caption becomes the prompt |

### Admin only (`ADMIN_USER_ID`)

| Command | Behaviour |
|---|---|
| `/setcookie` | Accept new cookies interactively and hot-restart the client without restarting the process. The message is deleted on receipt |
| `/allow <user_id>` | Add to the allowlist, effective immediately |
| `/deny <user_id>` | Remove from the allowlist. Deny beats the static list |
| `/health` | Client health, recent errors, database status |

---

## Deployment

Long polling means **no inbound port is needed**, so nothing has to be exposed.

Two options are provided. Both apply the same resource limits; pick whichever suits
your host.

### systemd

Preferred on a small machine, or when nothing else there uses Docker.

```bash
# On the server. UV is an absolute path because sudo resets PATH, so a uv
# installed under your home directory is not on it: UV=$(command -v uv)
sudo useradd --system --home-dir /var/lib/gemini-tg-bot --shell /usr/sbin/nologin gemini-tg-bot
sudo git clone https://github.com/7WayneLee/gemini-telegram-bot.git /opt/gemini-tg-bot

# Build the venv somewhere the service account can read (not under /root)
cd /opt/gemini-tg-bot
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv-python "$UV" venv --python 3.12 .venv
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv-python "$UV" pip install --python .venv/bin/python .
sudo chmod -R a+rX /opt/uv-python /opt/gemini-tg-bot

sudo install -m 600 .env /etc/gemini-tg-bot.env
sudo cp deploy/gemini-tg-bot.service /etc/systemd/system/
sudo systemctl enable --now gemini-tg-bot
```

Keeping the environment file in `/etc` rather than the repository directory reduces
the chance of a tool reading it by accident.

To update, pull and **reinstall** — the package is copied into the venv, so new
source on disk is not new code in the service:

```bash
cd /opt/gemini-tg-bot && sudo git pull --ff-only
sudo env UV_PYTHON_INSTALL_DIR=/opt/uv-python "$UV" pip install --python .venv/bin/python .
sudo chmod -R a+rX /opt/gemini-tg-bot && sudo systemctl restart gemini-tg-bot
```

Schema migrations run at startup, so no separate step is needed.

### Docker Compose

```bash
docker compose -p gemini-bot -f deploy/docker-compose.yml up -d
```

Use a separate project name so this does not get folded into an existing stack, and
restart with `docker compose -p gemini-bot restart bot` rather than `down` if the
host runs other services.

### Resource limits

Both deployment files ship with conservative defaults:

| Setting | Default |
|---|---|
| Memory | `256m` |
| Memory + swap | `512m` |
| CPU | 50% of one core |

Typical resident memory is 120–180 MB. The limits exist so that if something goes
wrong, the bot is what the OOM killer takes rather than whatever else runs on the
host — raise them only if you actually need to.

**Keep media scratch space on disk, not tmpfs.** tmpfs consumes RAM, which defeats
the memory limit.

For a walkthrough including problems encountered in practice, see
[`docs/DEPLOY.md`](docs/DEPLOY.md).

---

## Known risks

### It violates Google's ToS and the account can be banned

`gemini-webapi` is a **reverse-engineered** wrapper around the web app. Using it
breaks Google's terms, and the account may be restricted without warning. **Use a
secondary account.**

### Cookies expire unpredictably

Google invalidates sessions on its own; IP consistency is the biggest factor you
control. The bot enters a degraded state on auth failure and notifies the admin, so
you do not need to watch logs — but you do need to act when notified.

In steady state this is rarer than it sounds, since the session keeps rotating for as
long as the bot runs. Manual work is mostly needed after a reboot, a long outage, or
an invalidation by Google.

### AGPL-3.0 obligations

`gemini-webapi` is AGPL-3.0 and this project imports it directly, so this project is
AGPL-3.0 too. That follows from the dependency rather than being a preference.

AGPL is copyleft that **extends to network use**:

- **Running it for yourself** — no additional obligation
- **Offering it to others over a network**, even just friends — §13 requires you to
  offer those users the complete corresponding source, including your changes

If you fork and modify, the obligation comes with it.

### It does not scale horizontally

Background cookie rotation means two processes sharing one account invalidate each
other, so this runs as a single process by design. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

### Upstream can change without warning

`gemini-webapi` tracks Google's internal interfaces and updates frequently. The facts
this project depends on are recorded in
[`docs/upstream-api-contract.md`](docs/upstream-api-contract.md), generated by
reflection against the installed package. **When upgrading it, re-run
`scripts/probe_upstream.py` and diff against that document.**

---

## Development

```bash
uv sync
uv run pytest -q                            # full suite
uv run python -m gemini_tg_bot --dry-run    # end-to-end, no credentials
```

All tests are mocked; none make a live request with real cookies.

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Design notes for modifying the bot |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | Deployment walkthrough and pitfalls |
| [`docs/upstream-api-contract.md`](docs/upstream-api-contract.md) | Upstream API facts, produced by reflection |
| [`docs/egress-findings.md`](docs/egress-findings.md) | Image delivery measurements |
| `CLAUDE.md` | Project invariants |

Contributions are welcome. CI runs the suite, the dry run, and a credential scan.

---

## Licence

**AGPL-3.0-or-later** — see [`LICENSE`](LICENSE) and
[Known risks](#agpl-30-obligations).
