# Architecture

Design notes for people modifying the bot. Not required reading to run it.

---

## Overview

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

---

## Why it must be a singleton

`auto_refresh=True` rotates `__Secure-1PSIDTS` in the background. **Two processes
sharing one cookie invalidate each other**, logging the account out repeatedly.

Hence: one process, one event loop, no extra workers or replicas. There is exactly
one client construction site in the codebase (`gemini/service.py::_create_client`),
which now builds `ThinkingLevelGeminiClient` (a `GeminiClient` subclass).

The practical consequence is that this design **cannot scale horizontally**. Higher
throughput would require a pool of accounts with cookie rotation, which is a redesign
of the cookie lifecycle rather than a configuration change.

### Thinking-level transport adapter

Gemini Web replaced its binary toggle with three thinking levels (Low, Medium, and High). Upstream `gemini-webapi` 2.1.1 only exposes a boolean `extended_thinking` parameter that sends wire values for Low (1) and High (2), with no native way to send Medium (4).

To support Medium without modifying upstream:
- `ThinkingLevelGeminiClient` subclasses `GeminiClient` and overrides only `_live_client` to intercept generation streams.
- For Medium, `thinking_request(ThinkingLevel.MEDIUM)` asks upstream for High (`extended_thinking=True`) and sets a `ContextVar`. While set, generation requests have both wire fields rewritten from 2 to 4.
- The override uses a `ContextVar` rather than mutable client state so that request scoping is isolated across concurrent async operations, leaving background tasks such as cookie rotation completely unaffected.
- The rewrite is all-or-nothing and fails safe: if the payload shape does not match expectations, a warning is logged and the request is sent unchanged as High. The adapter never breaks a user request.

---

## The DEGRADED state machine

Degradation has two causes with completely different recovery paths:

| Cause | Trigger | Recovery |
|---|---|---|
| `AUTH` | `AuthError`, or a non-`AVAILABLE` account status, **immediately** | **Only** `/setcookie`; never heals with time |
| `BLOCKED` | Three consecutive `TemporarilyBlockedError` | Half-open probe after 15 minutes, recovers on its own |

They are separated because auth problems never fix themselves and need a human, while
upstream throttling does — paging an admin for the latter is pure noise.

Two behaviours worth knowing:

- **Authentication failure at startup does not kill the process.** The bot enters
  DEGRADED, notifies the admin, and still starts polling, so `/setcookie` can rescue
  it without SSH access.
- **While degraded, requests fail immediately** rather than queueing behind an
  upstream timeout.
- **The administrator is paged once per reason, not once per failure.** `reinit()`
  clears `_state` before it retries, so a state transition is not a usable signal;
  `_notified_degraded_reason` tracks what has already been announced and is cleared on
  recovery. This is not tidiness: Telegram rate-limits a chat, and a stream of identical
  pages swallows the `/setcookie` prompt that is the only way out of AUTH degradation.
- **That suppression is also durable**, because the in-memory half dies with the
  process. `Restart=always` in front of a process that fails identically every time
  reproduces the flood one restart at a time, so `notify_admin` additionally claims a
  digest of each message in `admin_notifications` and drops repeats for 15 minutes.
  Nothing is lost by waiting: AUTH degradation never heals on its own, and a failed
  `/setcookie` answers in the chat rather than through this path.

Upstream's `init()` returns success even on an unauthenticated session, so the service
inspects `client.account_status` directly rather than relying on an exception.

### The recovery path must survive its own failure mode

`/setcookie` is a two-step interaction whose armed state lives in memory, so a restart —
or a prompt lost to flood control — can leave an administrator pasting credentials the
bot is not expecting. Plain text that carries credential material is therefore routed
into the credential path regardless of that state, which deletes the message before
doing anything else. Without it the paste would be forwarded to Gemini as a prompt and
left in the chat transcript.

Detection keys on the *value* shape (`g.a000…`, `sidts-…`) rather than the cookie names,
so asking the bot about the cookies is still an ordinary question.

---

## Image delivery and egress

The naive approach sends the same bytes over the network twice: Gemini returns an
image → the VM downloads it → the VM uploads it to Telegram.

Telegram's `sendPhoto` / `sendMediaGroup` accept a **URL string** and fetch the source
themselves, bypassing the VM entirely. Both paths are implemented, and measured
behaviour differs sharply by image source:

| Source | URL | Telegram can fetch it | Egress |
|---|---|---|---|
| `WebImage` (search results) | Ordinary third-party URLs | ✅ | **0** |
| `GeneratedImage` (`/img`) | Signed `lh3.googleusercontent.com` URL | ❌ (400) | Metered per image |

This is why the two delivery modes are **independent switches** — a single global
toggle would force you to either relay everything (wasting the free path) or pass
through everything (breaking generated images).

Fallback granularity differs by send type:

- **Single photo**: per image, so one failure does not affect the rest
- **Media group**: per group, because `sendMediaGroup` is atomic — one bad URL fails
  the whole album

`/status` reports a month-to-date estimate so the cost stays visible rather than
arriving on a bill.

See [`egress-findings.md`](egress-findings.md) for the measurements behind this.

---

## Rendering

Gemini emits standard Markdown, which is incompatible with Telegram's MarkdownV2
escaping rules — sending it directly always fails. This project converts to Telegram
HTML (only `b/i/u/s/code/pre/a/blockquote` are supported) and degrades anything else.

- **LaTeX**: Telegram cannot typeset it. The original expression is wrapped in `<code>`
  rather than approximated in Unicode, because approximation loses information
  irreversibly on non-trivial formulas.
- **Splitting**: 4000-character safety limit, preferring `\n\n`, then `\n`, then a hard
  cut. Splits never land inside a code fence, and a block spanning segments gets its
  markers reopened in each one.
- **Nested lists**: Telegram HTML has no `<ul>`/`<li>`, so they are simulated with
  `• `, `◦ `, and `▪ ` plus **U+2007 FIGURE SPACE** indentation — ordinary spaces are
  collapsed by Telegram. Three levels; deeper items fold into the third.
- **Headings** become bold, since Telegram has no heading tags and plain text loses
  the document structure entirely.
- **Upstream artifacts**: Gemini leaves placeholders in `text` that upstream does not
  fully strip. Both known formats are cleaned; see
  [`upstream-api-contract.md`](upstream-api-contract.md).

### Streaming

Edits are throttled to every 1.5 seconds or 200 accumulated characters, whichever
comes first — Telegram applies flood control to edit frequency.

**Intermediate edits are plain text, and only the final edit applies HTML.** A
half-written tag mid-stream would otherwise cause `can't parse entities`. When the
rendered result is identical to what was already sent — which happens whenever the
reply contains no formatting — the final edit is skipped rather than rejected as
`message is not modified`.

---

## Concurrency model

The bot balances parallel processing across chats with strict ordering within each chat, bounded by upstream resource limits:

- **Update processor (`PerChatUpdateProcessor`)**: A custom Telegram `BaseUpdateProcessor` runs up to 64 updates concurrently across the bot. Updates destined for the same chat are queued behind a per-chat `asyncio.Lock` and executed strictly one at a time in arrival order, preventing race conditions and interleaving responses.
- **Lock-free commands**: Commands that do not mutate conversation history or require conversational ordering skip the per-chat lock and respond immediately even while that chat has a generation running. These include informational queries (`/start`, `/help`, `/status`, `/research_status`) and all administrative commands (`/setcookie`, `/allow`, `/deny`, `/allow_chat`, `/deny_chat`, `/access`, `/health`).
- **Gemini semaphore (`RequestQueue`)**: Access to upstream Gemini generation is guarded by a global `asyncio.Semaphore` bounded by `MAX_CONCURRENCY` (default `1`). When all slots are busy, a new request waits up to 30 seconds (`SEMAPHORE_ACQUIRE_TIMEOUT_SECONDS`) for a free slot; if none becomes available, it fails fast with the standard busy response.
- **Request time limit (`REQUEST_TIMEOUT_SEC`)**: Interactive generation requests (plain text prompts, `/gemini`, `/img`) are bounded by a total time limit (`REQUEST_TIMEOUT_SEC`, default 600 s), measured from the moment the concurrency slot is acquired. If the total duration (including any upstream stalls or retries) exceeds this deadline, the request is cancelled and the user is asked to send the prompt again. Deep Research is managed separately by `ResearchManager` and retains its dedicated `RESEARCH_TIMEOUT_SEC` (default 900 s).
- **Credential reload (`/setcookie`)**: When an administrator issues `/setcookie` to hot-reload credentials, `GeminiService.reinit()` waits for all in-flight requests to complete before closing the active client and reinitializing with the new credentials. This ensures ongoing requests are not abruptly terminated by an administrative update.

---

## Flood control

Every Telegram send goes through the shared transport in `telegram/sending.py`, which
bounds `RetryAfter` waits at 30 seconds and **releases the concurrency slot while
waiting**. Without that release, one flood-controlled request would block every other
user behind the `MAX_CONCURRENCY` semaphore (default 1).

A test in `tests/test_handlers.py` walks the AST to ensure no send site bypasses it.
If you add one, route it through `sending.py` rather than removing the guard.

---

## Storage

SQLite via `aiosqlite`, migrated with `PRAGMA user_version`:

| Table | Contents |
|---|---|
| `chat_sessions` | Telegram chat → Gemini conversation, model, gem, temporary flag, thinking level |
| `research_tasks` | Deep Research state, including the serialised plan needed to resume polling after a restart |
| `usage_log` | Per-request outcome, error kind, latency |
| `telegram_user_access` | Runtime allow/deny overrides from `/allow` and `/deny` |
| `admin_notifications` | Digest and timestamp of each administrator page, so a restart loop cannot repeat one |

Schema v9 adds `chat_sessions.thinking_level` (TEXT; `NULL` means the default, Medium). Chats that had extended thinking enabled are migrated to High, while `extended_thinking` is retained as an unused legacy column so that fresh and migrated databases match.

`ChatSession.metadata` is a `list[str | None]` and is stored as a JSON array —
positions carry meaning, so `None` entries must be preserved rather than compacted.

Cookie state lives outside the database, at `GEMINI_COOKIE_PATH`: upstream's rotated
cookie cache, plus the `0600` `runtime-credentials.json` that `/setcookie` writes.
`Settings` loads that override ahead of the environment, which is what lets a hot
credential update survive a restart — verified in production by breaking both
credentials in the environment file and watching the service authenticate anyway.

The path is resolved relative to the working directory, so a relative value in the
environment file lands somewhere other than the absolute path a systemd unit names,
and the two directories then disagree about where the session lives.
