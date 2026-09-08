# Gemini Telegram Bot

[![CI](https://github.com/7WayneLee/gemini-telegram-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/7WayneLee/gemini-telegram-bot/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

**English** · [正體中文](README.zh-TW.md)

A Telegram bot that talks to Google Gemini through
[`gemini-webapi`](https://github.com/HanaokaYuzu/Gemini-API). It is built to **run
unattended for long stretches on a small VM you share with other services** — the
memory ceiling, the zero-egress image path, and the cookie lifecycle handling all
follow from that premise.

**This is not the official Gemini API.** Authentication is a browser cookie, not an
API key.

---

## ⚠️ Read this before you start

This section is about real operational risk, not legal boilerplate.

### The bot holds a full web session for a real Google account

Not a scoped API token — a complete logged-in session. That means:

- If the account has Gemini extensions enabled, **anyone who can talk to the bot can
  read that account's mailbox via `@Gmail`**, and the same applies to `@Google Drive`.
- That is why the allowlist is a P0 feature rather than a later refinement. When
  `ALLOWED_USER_IDS` is unset the bot **rejects every message**, replies once with an
  explanation, and logs the attempt. Do not change that default.

### Use a separate, secondary Google account

**Do not use your primary account.** This is a reverse-engineered API, it violates
Google's Terms of Service, and the account can be restricted (see
[Known risks](#known-risks)).

### Cookies must never reach version control or an image

- `.env` is `chmod 600`
- `.gitignore` covers `.env`, `data/`, and `cookies/`
- The `Dockerfile` copies only `pyproject.toml` and `src/`, so no secret enters an
  image layer
- The cookie cache **filename itself contains the `__Secure-1PSID` value**
  (`.cached_cookies_{1PSID}.json`), so that path is never logged and `/status`
  reports only the refresh time, never the path

---

## Quick start

Requires **Python 3.12+** and [uv](https://docs.astral.sh/uv/).

```bash
# 1. Get the code and install dependencies
git clone https://github.com/7WayneLee/gemini-telegram-bot.git
cd gemini-telegram-bot
uv sync

# 2. Create the config file
cp .env.example .env && chmod 600 .env

# 3. Fill in the five required values (see Configuration below)
#    TELEGRAM_BOT_TOKEN / ADMIN_USER_ID / ALLOWED_USER_IDS
#    GEMINI_SECURE_1PSID / GEMINI_SECURE_1PSIDTS
#    How to obtain the cookies is the next section — it is the step that matters most

# 4. Preflight (makes no Gemini request and prints no secret value)
uv run python scripts/preflight.py

# 5. Credential-free end-to-end smoke test
uv run python -m gemini_tg_bot --dry-run

# 6. Run
uv run python -m gemini_tg_bot
```

`preflight.py` checks that `.env` is complete and correctly permissioned, that the
cookie directory is writable, and — with `--check-ip` — **what your egress IP
actually is**. That last one governs how long your cookies survive; see below.

---

## Getting the cookies

### Why you cannot just copy them from your browser

The cookie's **birth IP** must match its **use IP**.

If you obtain a cookie from a browser in one country and then use it on a VM in
another, Google sees the session jump geographically, triggers a security check, and
**invalidates the cookie repeatedly**. You end up re-authenticating every few hours
without understanding why.

The fix is to make the browser log in **through the VM's egress**, so the session is
born and used at the same IP.

### Steps

1. **Open a SOCKS tunnel to the VM** (replace `your-vm` with your SSH alias):

   ```bash
   ssh -D 1080 -C -q -N your-vm
   ```

   Leave this running.

2. **Create a dedicated Firefox profile**, launched with a timezone matching the VM's
   region to reduce fingerprint mismatch:

   ```bash
   TZ=America/Chicago /Applications/Firefox.app/Contents/MacOS/firefox -P gemini-us
   ```

   **Use Firefox.** Upstream notes that Chromium's Device Bound Session Credentials
   make the cookie last only hours and prevent refresh.

3. **Firefox Settings → Network Settings → Manual proxy**:
   - SOCKS Host `127.0.0.1`, Port `1080`, **SOCKS v5**
   - **Tick "Proxy DNS when using SOCKS v5"** — without it DNS goes out locally and
     leaks your real location

4. **Confirm your egress IP is the VM's** before logging in to `gemini.google.com`:

   ```bash
   curl --socks5-hostname 127.0.0.1:1080 https://ifconfig.me
   ```

5. **F12 → Network → copy `__Secure-1PSID` and `__Secure-1PSIDTS`**, then close the
   private window immediately (see upstream issue #6).

6. Put them in `.env`, or inject them with the bot's `/setcookie` command — no process
   restart needed.

### Faster: `scripts/grab_cookies.sh`

Step 5's manual copying can be skipped. With the tunnel up and the session logged in:

```bash
sh scripts/grab_cookies.sh
```

It reads `cookies.sqlite` from the Firefox profile directly and puts both values **on
the clipboard**, ready to paste into `/setcookie`. The terminal prints only their
lengths, never the values.

Use a different profile with `sh scripts/grab_cookies.sh <profile-name>` (default
`gemini-us`).

**The tunnel is still required.** A more convenient way to fetch the cookie does not
change the fact that its birth IP must be the VM's. Verify first:

```bash
curl --socks5-hostname 127.0.0.1:1080 https://ifconfig.me
```

> **Expected**: the first login from a new country will very likely trigger 2FA and a
> new-device notification. That is normal, and it settles down afterwards.

### Testing locally without touching the server

`GEMINI_PROXY` accepts SOCKS. With the tunnel up you can run the bot on your own
machine while still presenting the VM's egress IP:

```
GEMINI_PROXY=socks5h://127.0.0.1:1080
```

Use `socks5h`, not `socks5` — the `h` sends DNS through the tunnel too, matching the
"Proxy DNS" box in step 3. Local DNS resolution leaks your real location.

### Cookies rotate on their own

Once running, `auto_refresh` rotates `__Secure-1PSIDTS` in the background (every 600
seconds by default) and writes it to `GEMINI_COOKIE_PATH`. So:

- **The copy in `.env` goes stale as soon as the bot starts.** The cache is what is
  actually in use.
- Restarting does not require re-fetching cookies; only a session Google has
  invalidated does.
- This is why `GEMINI_COOKIE_PATH` must live on a mounted volume — without one, a
  container rebuild loses the session.
- `/setcookie` writes its credentials to a `0600` runtime override that **takes
  precedence over `.env` on the next start**, so a hot update survives a restart.

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

Everything else has a safe default. Three are worth knowing about:

| Variable | Default | Why |
|---|---|---|
| `MAX_CONCURRENCY` | `1` | Memory is the constraint, and a single Gemini account has its own quota ceiling, so concurrency buys nothing |
| `ENABLE_VIDEO_GENERATION` | `false` | A single clip can be tens of MB and eat a month's free egress |
| `ENABLE_AUDIO_GENERATION` | `false` | Same |

---

## Commands

### Everyone on the allowlist

| Command | Behaviour |
|---|---|
| `/start`, `/help` | Show available commands |
| `/new` | End the current conversation and start a fresh `ChatSession` |
| `/model` | List models **dynamically** via `client.list_models()`, pick from an inline keyboard |
| `/gem` | List available gems and apply one to the conversation |
| `/temp` | Toggle temporary mode (nothing written to Gemini history) |
| `/img <prompt>` | **Explicitly ask for generation.** Without that wording Gemini tends to return web search results rather than an AI-generated image |
| `/research <topic>` | Submit a Deep Research task and **return a task id immediately** |
| `/research_status` | Status of this chat's research tasks |
| `/status` | Current model, session cid, last cookie refresh, queue depth, today's usage, month-to-date egress estimate |
| Plain text | Sent to the current `ChatSession`, answered with **streaming** |
| Photo / document | Passed as `files=[...]`; the caption becomes the prompt |

Model lists are always fetched at runtime and **never hard-coded** — upstream has
deprecated the `Model` enum and removed `Model.from_name` / `Model.from_dict`.

### Admin only (`ADMIN_USER_ID`)

| Command | Behaviour |
|---|---|
| `/setcookie` | Accept new cookies interactively, **hot-restart the client without restarting the process**, and delete the message on receipt |
| `/allow <user_id>` | Add to the allowlist (persisted, effective immediately) |
| `/deny <user_id>` | Remove from the allowlist. **Deny beats the static allowlist** |
| `/health` | Client health, recent errors, database status |

---

## Deployment

> This project assumes your host is **shared with other services** (a media server, a
> download client, that sort of thing). On a dedicated host you can relax the limits.

### Measure your own host first

**Do not copy any example numbers.** On the target host:

```bash
nproc && free -h && df -h / && swapon --show
docker stats --no-stream    # if the existing services are containers
```

### How to derive the resource limits

The point of the limits is **not** to make the bot fast. It is to make the bot the
thing the OOM killer takes, rather than the services around it. So set them
"comfortably enough for the bot", not "as much as available".

For reference, this project's resident set is roughly **120–180 MiB** on Python 3.12
with `MAX_CONCURRENCY=1` and the URL-direct image path.

A worked example from a real deployment — 2 vCPU, 969 MiB RAM, 2 GB swap, already
running other services, with only 441 MiB available:

| Setting | Value used | Reasoning |
|---|---|---|
| `mem_limit` / `MemoryMax` | `256m` | Above the typical RSS ceiling, well under what is available; when it trips, the bot is what dies |
| `memswap_limit` / `MemorySwapMax` | `512m` | Twice `mem_limit` |
| `cpus` / `CPUQuota` | `0.5` / `50%` | A quarter of a 2-core box; the bot is I/O-bound anyway |
| Media scratch cap | `256m` | Uploads cap at 20 MB and concurrency is 1, so real usage is far lower |

Even on a roomy host, a high `mem_limit` is not useful — it just makes the OOM
victim less predictable.

**Media scratch space must be on disk, never tmpfs.** tmpfs consumes RAM, which is
exactly the resource the limit exists to protect.

### systemd or Docker?

If memory is tight and the existing services are not containers, **prefer systemd**
(`deploy/gemini-tg-bot.service`) — running a Docker daemon for one bot is a poor
trade on a small box. If you already run Docker, Compose is less work.

The limits are equivalent either way:

| | Docker Compose | systemd |
|---|---|---|
| Memory | `mem_limit` | `MemoryMax` |
| Swap | `memswap_limit` | `MemorySwapMax` |
| CPU | `cpus` | `CPUQuota` |

Step-by-step instructions, including the traps hit during a real deployment, are in
[`docs/DEPLOY.md`](docs/DEPLOY.md).

### Docker Compose

```bash
docker compose -p gemini-bot -f deploy/docker-compose.yml up -d
```

- **Use a separate project name** (`-p gemini-bot`); do not fold this into an existing
  stack's compose file
- **Avoid `docker compose down` on a shared host** — it is easy to stop more than you
  meant to. Restart with `docker compose -p gemini-bot restart bot`
- **Do not set any `ports`** — long polling needs no inbound port

### Firewall

**No inbound port is required.** That is the main security benefit of long polling
over webhooks, and it leaves your existing services' ports untouched.

---

## Cost and egress

**Egress is the dominant cost.** On GCP's free tier, for example, you get 1 GB of
North America egress per month and pay about US$0.12/GB beyond it — and that
allowance is **shared across the whole machine**, so a media server on the same host
leaves less headroom than you would expect.

### The zero-egress image path

The naive approach sends the same bytes over the network twice: Gemini returns an
image → the VM downloads it → the VM uploads it to Telegram.

Telegram's `sendPhoto` / `sendMediaGroup` accept a **URL string** and fetch the
source themselves, bypassing the VM entirely. Both paths are implemented:

| Path | Egress | When |
|---|---|---|
| **A. URL passthrough** | **0** | Tried first |
| **B. Download and relay** | Metered | Automatically, when Telegram returns `BadRequest` |

Measured behaviour differs sharply by image source:

| Source | URL | Telegram can fetch it | Egress |
|---|---|---|---|
| `WebImage` (search results) | Ordinary third-party URLs | ✅ | **0** |
| `GeneratedImage` (`/img`) | Signed `lh3.googleusercontent.com` URL | ❌ (400) | Metered per image |

This is why the two delivery modes are independent switches — a single global toggle
would force you to either relay everything (wasting the free path) or pass through
everything (breaking generated images).

Fallback is decided **per image**, so one failure does not affect the rest. `/status`
reports the month-to-date estimate so the cost stays visible rather than arriving on
a bill.

### Other cost controls

- Video and audio generation are **off by default** behind explicit env vars
- Uploads are capped at 20 MB (Telegram's limit) and **rejected up front** rather
  than failing halfway through a download
- Media scratch files are deleted after use
- Log rotation at `max-size: 5m`, `max-file: 2`

---

## Architecture

```
Telegram long polling
        │
   ┌────▼──────────────────────────┐
   │ Auth middleware (deny by default)
   └────┬──────────────────────────┘
   ┌────▼──────────────────────────┐
   │ Command router / handlers     │
   └────┬──────────────────────────┘
   ┌────▼──────────────────────────┐
   │ RequestQueue                  │
   │  semaphore + per-user bucket  │
   └────┬──────────────────────────┘
   ┌────▼──────────────────────────┐
   │ GeminiService (singleton)     │
   │  lifecycle / DEGRADED machine │
   └────┬──────────────────────────┘
   ┌────▼──────────────────────────┐
   │ gemini_webapi.GeminiClient    │
   └───────────────────────────────┘

Alongside: SQLite (aiosqlite), cookie store, admin notifier
```

### Why it must be a singleton

`auto_refresh=True` rotates `__Secure-1PSIDTS` in the background. **Two processes
sharing one cookie invalidate each other**, logging the account out repeatedly. Hence:
one process, one event loop, no extra workers or replicas.

There is exactly one `GeminiClient(...)` construction site in the codebase
(`gemini/service.py`), enforced deliberately.

### The DEGRADED state machine

Degradation has two causes with completely different recovery paths:

| Cause | Trigger | Recovery |
|---|---|---|
| `AUTH` | `AuthError`, or a non-`AVAILABLE` account status, **immediately** | **Only** `/setcookie`; it never heals with time |
| `BLOCKED` | Three consecutive `TemporarilyBlockedError` | Half-open probe after 15 minutes, recovers on its own |

They are separated because auth problems never fix themselves and need a human,
while upstream throttling does — paging an admin for the latter is pure noise.

Authentication failure **at startup does not kill the process**. The bot enters
DEGRADED, notifies the admin, and still starts polling, so `/setcookie` can rescue it
without SSH access.

### Rendering

Gemini emits standard Markdown, which is incompatible with Telegram's MarkdownV2
escaping rules — sending it directly always fails. This project converts to Telegram
HTML (only `b/i/u/s/code/pre/a/blockquote` are supported) and degrades anything else.

- **LaTeX**: Telegram cannot typeset it. The original expression is wrapped in
  `<code>` rather than approximated in Unicode, because approximation loses
  information irreversibly on non-trivial formulas.
- **Splitting**: 4000-character safety limit, preferring `\n\n`, then `\n`, then a
  hard cut. Splits never land inside a code fence, and a block spanning segments gets
  its markers reopened in each one.
- **Nested lists**: Telegram HTML has no `<ul>`/`<li>`, so they are simulated with
  `• `, `◦ `, and `▪ ` plus **U+2007 FIGURE SPACE** indentation — ordinary spaces are
  collapsed by Telegram. Three levels; deeper items fold into the third.
- **Streaming**: edits are throttled to every 1.5 seconds or 200 accumulated
  characters, whichever comes first. **Intermediate edits are plain text, and only the
  final edit applies HTML** — otherwise a half-written tag mid-stream causes
  `can't parse entities`.
- **Upstream artifacts**: Gemini leaves placeholders in `text` that upstream does not
  fully strip. Both known formats are cleaned; see
  [`docs/upstream-api-contract.md`](docs/upstream-api-contract.md).

---

## Known risks

### 1. It violates Google's ToS and the account can be banned

`gemini-webapi` is a **reverse-engineered** wrapper around the web app, not an
official API. Using it breaks Google's terms. The account may be restricted or
suspended, without warning.

**Use a secondary account. Do not use your primary one.**

### 2. Cookies expire, and not on a predictable schedule

Google invalidates sessions on its own. In practice the biggest factor is **IP
consistency** — a mismatch between the cookie's birth IP and its use IP raises the
invalidation rate sharply (see [Getting the cookies](#getting-the-cookies)).

Even with everything right, expect to re-authenticate periodically. The bot enters
DEGRADED on `AuthError` and notifies the admin, so you do not need to watch logs — but
you do need to act when the notification arrives.

In steady state this is rarer than it sounds: `auto_refresh` keeps rotating the
session for as long as the bot keeps running. Manual intervention is mostly needed
after a reboot, a long outage, or an invalidation by Google.

### 3. AGPL-3.0 obligations

`gemini-webapi` is **AGPL-3.0**, this project imports it directly, and so this project
is AGPL-3.0 too. That is a consequence of the dependency, not a preference.

AGPL is copyleft that **extends to network use**:

- **Running it for yourself** — no additional obligation
- **Offering it to anyone else over a network**, even just friends — §13 requires you
  to offer those users the complete corresponding source, including your changes

If you fork and modify, the obligation comes with it.

### 4. The singleton design does not scale horizontally

The singleton constraint (see [above](#why-it-must-be-a-singleton)) means this
architecture **cannot be scaled out**. No replicas, no Kubernetes multi-pod
deployment, no throughput gained by adding machines.

Higher throughput would require **a pool of accounts with cookie rotation**, which is
a redesign of the cookie lifecycle rather than a configuration change. This project
deliberately does not attempt it.

### 5. The upstream API can change without warning

`gemini_webapi` tracks Google's internal interfaces and updates frequently. Every
upstream fact this project relies on is recorded in
[`docs/upstream-api-contract.md`](docs/upstream-api-contract.md), produced by
reflection against the installed package rather than from documentation.

**When upgrading `gemini-webapi`, re-run `scripts/probe_upstream.py` and diff it
against the contract.** That document also records upstream defects found in
practice — an incomplete artifact-stripping regex, a cache that silently takes
precedence over supplied credentials, and an `init()` that reports success on an
unauthenticated session.

---

## Development

```bash
uv sync
uv run pytest -q                            # full suite
uv run python -m gemini_tg_bot --dry-run    # credential-free end-to-end smoke test
```

`--dry-run` walks the whole chain with a fake client and fake transport —
`receive → allowlist → queue → service → streaming → render → send → research → DB` —
and exits 0 when the integration layer is healthy. **All development tests are
mocked; none of them make a live request with real cookies.**

A test in `tests/test_handlers.py` walks the AST to ensure every Telegram send goes
through the shared flood-control transport in `telegram/sending.py`. If you add a
send site, route it through there rather than removing the guard.

### Documentation

| File | Contents |
|---|---|
| `CLAUDE.md` | Project invariants (security prohibitions, implementation discipline) |
| [`docs/upstream-api-contract.md`](docs/upstream-api-contract.md) | Source of truth for upstream API facts, produced by reflection |
| [`docs/egress-findings.md`](docs/egress-findings.md) | Egress path measurements and the resulting decisions |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | Deployment steps and the traps hit in practice |

### Licence

This project is licensed under **AGPL-3.0-or-later**; see [`LICENSE`](LICENSE) and
[Known risks §3](#3-agpl-30-obligations).
