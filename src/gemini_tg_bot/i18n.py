"""Dependency-free message catalog and language resolution."""

from __future__ import annotations


LANGUAGE_ENGLISH = "en"
LANGUAGE_CHINESE = "zh-hant"
DEFAULT_LANGUAGE = LANGUAGE_ENGLISH

_TRADITIONAL_CHINESE_TELEGRAM_CODES = frozenset(
    {
        "zh-tw",
        "zh-hant",
        "zh-hant-tw",
    }
)


MESSAGES: dict[str, dict[str, str]] = {
    "think.enabled": {
        LANGUAGE_ENGLISH: (
            "Extended thinking is on. Replies include an expandable thought "
            "process.\n"
            "Note: this draws on the Gemini Advanced quota, faster than "
            "normal mode."
        ),
        LANGUAGE_CHINESE: (
            "Extended Thinking 已開啟。回覆會附上可展開的思考過程。\n"
            "注意：這會消耗 Gemini Advanced 額度，用得比一般模式快。"
        ),
    },
    "think.disabled": {
        LANGUAGE_ENGLISH: "Extended thinking is off.",
        LANGUAGE_CHINESE: "Extended Thinking 已關閉。",
    },
    "generic.failure": {
        LANGUAGE_ENGLISH: (
            "An error occurred while processing your request. "
            "Please try again later."
        ),
        LANGUAGE_CHINESE: "處理請求時發生錯誤，請稍後再試。",
    },
    "new.started": {
        LANGUAGE_ENGLISH: "Started a new conversation.",
        LANGUAGE_CHINESE: "已開始新的對話。",
    },
    "auth.degraded": {
        LANGUAGE_ENGLISH: "Authentication expired. Please use /setcookie.",
        LANGUAGE_CHINESE: "認證失效，請 /setcookie",
    },
    "lang.choose": {
        LANGUAGE_ENGLISH: "Choose an interface language:",
        LANGUAGE_CHINESE: "請選擇介面語言：",
    },
    "lang.selected": {
        LANGUAGE_ENGLISH: "Interface language set to {language}.",
        LANGUAGE_CHINESE: "介面語言已設為{language}。",
    },
    "lang.invalid": {
        LANGUAGE_ENGLISH: "Invalid language. Please run /language again.",
        LANGUAGE_CHINESE: "無效的語言，請重新執行 /language。",
    },
    "help.heading": {
        LANGUAGE_ENGLISH: "Available commands:",
        LANGUAGE_CHINESE: "可用指令：",
    },
    "help.footer": {
        LANGUAGE_ENGLISH: (
            "Send text directly to continue the current conversation."
        ),
        LANGUAGE_CHINESE: "直接傳送文字即可延續目前對話。",
    },
    "command.start.description": {
        LANGUAGE_ENGLISH: "Show usage instructions",
        LANGUAGE_CHINESE: "顯示使用說明",
    },
    "command.help.description": {
        LANGUAGE_ENGLISH: "Show usage instructions",
        LANGUAGE_CHINESE: "顯示使用說明",
    },
    "command.new.description": {
        LANGUAGE_ENGLISH: "Start a new conversation",
        LANGUAGE_CHINESE: "開始新的對話",
    },
    "command.model.description": {
        LANGUAGE_ENGLISH: "Choose a Gemini model",
        LANGUAGE_CHINESE: "選擇 Gemini 模型",
    },
    "command.gem.description": {
        LANGUAGE_ENGLISH: "Choose a Gem",
        LANGUAGE_CHINESE: "選擇 Gem",
    },
    "command.temp.description": {
        LANGUAGE_ENGLISH: "Toggle temporary chat mode",
        LANGUAGE_CHINESE: "切換暫時對話模式",
    },
    "command.think.description": {
        LANGUAGE_ENGLISH: "Toggle Extended Thinking",
        LANGUAGE_CHINESE: "切換 Extended Thinking",
    },
    "command.language.description": {
        LANGUAGE_ENGLISH: "Choose interface language",
        LANGUAGE_CHINESE: "選擇介面語言",
    },
    "command.img.description": {
        LANGUAGE_ENGLISH: "Generate an image",
        LANGUAGE_CHINESE: "生成圖片",
    },
    "command.research.description": {
        LANGUAGE_ENGLISH: "Submit a Deep Research task",
        LANGUAGE_CHINESE: "提交 Deep Research 任務",
    },
    "command.research_status.description": {
        LANGUAGE_ENGLISH: "View Deep Research task status",
        LANGUAGE_CHINESE: "查看 Deep Research 任務狀態",
    },
    "command.status.description": {
        LANGUAGE_ENGLISH: "View current status",
        LANGUAGE_CHINESE: "查看目前狀態",
    },
    "command.private_only": {
        LANGUAGE_ENGLISH: "This command is only available in private chats.",
        LANGUAGE_CHINESE: "此指令僅限私訊使用。",
    },
    "language.english": {
        LANGUAGE_ENGLISH: "English",
        LANGUAGE_CHINESE: "English",
    },
    "language.chinese": {
        LANGUAGE_ENGLISH: "Traditional Chinese",
        LANGUAGE_CHINESE: "正體中文",
    },
    "generic.service_busy": {
        LANGUAGE_ENGLISH: "The service is busy. Please try again later.",
        LANGUAGE_CHINESE: "服務忙碌，請稍後再試。",
    },
    "generic.empty_response": {
        LANGUAGE_ENGLISH: "Gemini returned no text.",
        LANGUAGE_CHINESE: "Gemini 未回傳文字。",
    },
    "generic.invalid_option": {
        LANGUAGE_ENGLISH: "Invalid option. Please run the command again.",
        LANGUAGE_CHINESE: "無效的選項，請重新執行指令。",
    },
    "generic.rate_limited.one": {
        LANGUAGE_ENGLISH: (
            "Too many requests. Please try again in about 1 second."
        ),
        LANGUAGE_CHINESE: "請求過於頻繁，請約 1 秒後再試。",
    },
    "generic.rate_limited.many": {
        LANGUAGE_ENGLISH: (
            "Too many requests. Please try again in about {seconds} seconds."
        ),
        LANGUAGE_CHINESE: "請求過於頻繁，請約 {seconds} 秒後再試。",
    },
    "model.list_unavailable": {
        LANGUAGE_ENGLISH: (
            "The model list is temporarily unavailable. Please try again later."
        ),
        LANGUAGE_CHINESE: "模型清單暫時無法取得，請稍後再試。",
    },
    "model.choose": {
        LANGUAGE_ENGLISH: "Choose a model:",
        LANGUAGE_CHINESE: "請選擇模型：",
    },
    "model.invalid": {
        LANGUAGE_ENGLISH: "Invalid model. Please run /model again.",
        LANGUAGE_CHINESE: "無效的模型，請重新執行 /model。",
    },
    "model.unavailable": {
        LANGUAGE_ENGLISH: (
            "This model is no longer available. Please run /model again."
        ),
        LANGUAGE_CHINESE: "此模型已無法使用，請重新執行 /model。",
    },
    "model.selected": {
        LANGUAGE_ENGLISH: "Selected model: {model}",
        LANGUAGE_CHINESE: "已選擇模型：{model}",
    },
    "gem.list_unavailable": {
        LANGUAGE_ENGLISH: (
            "The Gem list is temporarily unavailable. Please try again later."
        ),
        LANGUAGE_CHINESE: "Gem 清單暫時無法取得，請稍後再試。",
    },
    "gem.choose": {
        LANGUAGE_ENGLISH: "Choose a Gem:",
        LANGUAGE_CHINESE: "請選擇 Gem：",
    },
    "gem.invalid": {
        LANGUAGE_ENGLISH: "Invalid Gem. Please run /gem again.",
        LANGUAGE_CHINESE: "無效的 Gem，請重新執行 /gem。",
    },
    "gem.unavailable": {
        LANGUAGE_ENGLISH: "This Gem is no longer available. Please run /gem again.",
        LANGUAGE_CHINESE: "此 Gem 已無法使用，請重新執行 /gem。",
    },
    "gem.selected": {
        LANGUAGE_ENGLISH: "Selected Gem: {gem}",
        LANGUAGE_CHINESE: "已選擇 Gem：{gem}",
    },
    "temp.enabled": {
        LANGUAGE_ENGLISH: "Temporary mode is on.",
        LANGUAGE_CHINESE: "Temporary mode 已開啟。",
    },
    "temp.disabled": {
        LANGUAGE_ENGLISH: "Temporary mode is off.",
        LANGUAGE_CHINESE: "Temporary mode 已關閉。",
    },
    "service.unavailable": {
        LANGUAGE_ENGLISH: (
            "The Gemini service is currently unavailable. Please try again later."
        ),
        LANGUAGE_CHINESE: "Gemini 服務目前無法接受請求，請稍後再試。",
    },
    "service.unavailable.auth": {
        LANGUAGE_ENGLISH: (
            "The Gemini service cannot accept requests because authentication "
            "has expired."
        ),
        LANGUAGE_CHINESE: "Gemini 服務目前處於認證失效狀態，暫時無法接受請求。",
    },
    "service.unavailable.blocked": {
        LANGUAGE_ENGLISH: (
            "The Gemini service is temporarily restricted and will retry after "
            "the cooldown."
        ),
        LANGUAGE_CHINESE: "Gemini 服務目前處於暫時受限狀態，將在冷卻後自動重試。",
    },
    "status.not_refreshed": {
        LANGUAGE_ENGLISH: "Never refreshed",
        LANGUAGE_CHINESE: "尚未刷新",
    },
    "status.on": {
        LANGUAGE_ENGLISH: "On",
        LANGUAGE_CHINESE: "開啟",
    },
    "status.off": {
        LANGUAGE_ENGLISH: "Off",
        LANGUAGE_CHINESE: "關閉",
    },
    "status.account_default": {
        LANGUAGE_ENGLISH: "Account default",
        LANGUAGE_CHINESE: "帳號預設",
    },
    "status.session_missing": {
        LANGUAGE_ENGLISH: "Not created",
        LANGUAGE_CHINESE: "尚未建立",
    },
    "status.model": {
        LANGUAGE_ENGLISH: "Current model: {model}",
        LANGUAGE_CHINESE: "目前模型：{model}",
    },
    "status.session_cid": {
        LANGUAGE_ENGLISH: "Session CID: {cid}",
        LANGUAGE_CHINESE: "Session CID：{cid}",
    },
    "status.temporary": {
        LANGUAGE_ENGLISH: "Temporary mode: {state}",
        LANGUAGE_CHINESE: "Temporary mode：{state}",
    },
    "status.thinking": {
        LANGUAGE_ENGLISH: "Extended Thinking: {state}",
        LANGUAGE_CHINESE: "Extended Thinking：{state}",
    },
    "status.service": {
        LANGUAGE_ENGLISH: "Service status: {state}{reason}",
        LANGUAGE_CHINESE: "服務狀態：{state}{reason}",
    },
    "status.account": {
        LANGUAGE_ENGLISH: "Account status: {status}",
        LANGUAGE_CHINESE: "Account status：{status}",
    },
    "status.cookie_refreshed": {
        LANGUAGE_ENGLISH: "Last cookie refresh: {time}",
        LANGUAGE_CHINESE: "Cookie 最後刷新時間：{time}",
    },
    "status.queue_depth": {
        LANGUAGE_ENGLISH: "Queue depth: {depth}",
        LANGUAGE_CHINESE: "佇列深度：{depth}",
    },
    "status.today_usage": {
        LANGUAGE_ENGLISH: "Today's usage: {usage}",
        LANGUAGE_CHINESE: "今日用量：{usage}",
    },
    "status.monthly_egress": {
        LANGUAGE_ENGLISH: "Estimated monthly egress: {egress}",
        LANGUAGE_CHINESE: "本月累計 egress 估算值：{egress}",
    },
    "research.usage": {
        LANGUAGE_ENGLISH: "Usage: /research <topic>",
        LANGUAGE_CHINESE: "用法：/research <topic>",
    },
    "research.unavailable": {
        LANGUAGE_ENGLISH: (
            "Deep Research is currently unavailable. Please try again later."
        ),
        LANGUAGE_CHINESE: "Deep Research 服務目前無法使用，請稍後再試。",
    },
    "research.submitted": {
        LANGUAGE_ENGLISH: "Deep Research task submitted: {task_id}",
        LANGUAGE_CHINESE: "Deep Research 任務已提交：{task_id}",
    },
    "research.none": {
        LANGUAGE_ENGLISH: "There are no Deep Research tasks.",
        LANGUAGE_CHINESE: "目前沒有 Deep Research 任務。",
    },
    "research.status_heading": {
        LANGUAGE_ENGLISH: "Deep Research task status:",
        LANGUAGE_CHINESE: "Deep Research 任務狀態：",
    },
    "research.status_line": {
        LANGUAGE_ENGLISH: "{task_id}: {status}",
        LANGUAGE_CHINESE: "{task_id}：{status}",
    },
    "research.completed": {
        LANGUAGE_ENGLISH: "Deep Research task {task_id} is complete.",
        LANGUAGE_CHINESE: "Deep Research 任務 {task_id} 已完成。",
    },
    "research.failed": {
        LANGUAGE_ENGLISH: "Deep Research task {task_id} failed.",
        LANGUAGE_CHINESE: "Deep Research 任務 {task_id} 執行失敗。",
    },
    "research.timeout": {
        LANGUAGE_ENGLISH: (
            "Deep Research task {task_id} timed out. Use /research_status to "
            "check again."
        ),
        LANGUAGE_CHINESE: (
            "Deep Research 任務 {task_id} 已逾時；可用 /research_status 再查。"
        ),
    },
    "image.usage": {
        LANGUAGE_ENGLISH: "Usage: /img <prompt>",
        LANGUAGE_CHINESE: "用法：/img <prompt>",
    },
    "admin.only": {
        LANGUAGE_ENGLISH: "This command is only available to administrators.",
        LANGUAGE_CHINESE: "此指令僅限管理員使用。",
    },
    "admin.not_configured": {
        LANGUAGE_ENGLISH: "Administrator features are not configured.",
        LANGUAGE_CHINESE: "管理員功能尚未設定。",
    },
    "admin.cookie_prompt": {
        LANGUAGE_ENGLISH: (
            "Paste two cookie values in your next message: __Secure-1PSID on "
            "the first line and __Secure-1PSIDTS on the second. The message "
            "will be deleted immediately."
        ),
        LANGUAGE_CHINESE: (
            "請在下一則訊息貼上兩行 cookie：第一行 __Secure-1PSID，"
            "第二行 __Secure-1PSIDTS。該訊息收到後會立即刪除。"
        ),
    },
    "admin.cookie_input_invalid": {
        LANGUAGE_ENGLISH: "Invalid cookie format. Please run /setcookie again.",
        LANGUAGE_CHINESE: "Cookie 格式無效，請重新執行 /setcookie。",
    },
    "admin.credentials_not_relayed": {
        LANGUAGE_ENGLISH: (
            "This message appears to contain Gemini credentials, so it was "
            "not sent to Gemini.\nDelete the message yourself. Only an "
            "administrator can apply credentials with /setcookie."
        ),
        LANGUAGE_CHINESE: (
            "偵測到訊息含有 Gemini 憑證，已停止處理，內容不會送往 Gemini。\n"
            "請自行刪除該則訊息。憑證只有管理者能透過 /setcookie 套用。"
        ),
    },
    "admin.credential_delete_failed": {
        LANGUAGE_ENGLISH: (
            "Could not delete the credential message. The cookie was not "
            "applied. Please try again later."
        ),
        LANGUAGE_CHINESE: "無法刪除含憑證的訊息；未套用 Cookie，請稍後再試。",
    },
    "admin.cookie_update_guidance": {
        LANGUAGE_ENGLISH: (
            "Cookie update failed and the Gemini service has not recovered.\n"
            "{guidance}"
        ),
        LANGUAGE_CHINESE: (
            "Cookie 更新失敗，Gemini 服務尚未恢復。\n{guidance}"
        ),
    },
    "admin.cookie_update_failed": {
        LANGUAGE_ENGLISH: (
            "Cookie update failed and the Gemini service has not recovered. "
            "Please run /setcookie again."
        ),
        LANGUAGE_CHINESE: (
            "Cookie 更新失敗，Gemini 服務尚未恢復。請重新執行 /setcookie。"
        ),
    },
    "admin.account_status_unknown": {
        LANGUAGE_ENGLISH: (
            "The Gemini account status could not be determined. Please try again later."
        ),
        LANGUAGE_CHINESE: "Gemini 帳號狀態無法確認，請稍後重試。",
    },
    "admin.cookie_persist_failed": {
        LANGUAGE_ENGLISH: (
            "The cookie was applied and the Gemini service recovered, but it "
            "could not be saved for restart. Please run /setcookie again."
        ),
        LANGUAGE_CHINESE: (
            "Cookie 已套用且 Gemini 服務已恢復，但無法保存供重啟使用；"
            "請重新執行 /setcookie。"
        ),
    },
    "admin.cookie_updated": {
        LANGUAGE_ENGLISH: "Cookie updated and the Gemini service restarted.",
        LANGUAGE_CHINESE: "Cookie 已更新，Gemini 服務已熱重啟。",
    },
    "admin.allow_usage": {
        LANGUAGE_ENGLISH: "Usage: /allow <user_id>",
        LANGUAGE_CHINESE: "用法：/allow <user_id>",
    },
    "admin.deny_usage": {
        LANGUAGE_ENGLISH: "Usage: /deny <user_id>",
        LANGUAGE_CHINESE: "用法：/deny <user_id>",
    },
    "admin.allowlist_update_failed": {
        LANGUAGE_ENGLISH: (
            "Could not update the allowlist. Please try again later."
        ),
        LANGUAGE_CHINESE: "白名單更新失敗，請稍後再試。",
    },
    "admin.allowed": {
        LANGUAGE_ENGLISH: "Allowed user {user_id}.",
        LANGUAGE_CHINESE: "已允許使用者 {user_id}。",
    },
    "admin.denied": {
        LANGUAGE_ENGLISH: "Denied user {user_id}.",
        LANGUAGE_CHINESE: "已拒絕使用者 {user_id}。",
    },
    "admin.allow_chat_usage": {
        LANGUAGE_ENGLISH: "Usage: /allow_chat <chat_id>",
        LANGUAGE_CHINESE: "用法：/allow_chat <chat_id>",
    },
    "admin.deny_chat_usage": {
        LANGUAGE_ENGLISH: "Usage: /deny_chat <chat_id>",
        LANGUAGE_CHINESE: "用法：/deny_chat <chat_id>",
    },
    "admin.chat_allowed": {
        LANGUAGE_ENGLISH: "Allowed group chat {chat_id}.",
        LANGUAGE_CHINESE: "已允許群組 {chat_id}。",
    },
    "admin.chat_denied": {
        LANGUAGE_ENGLISH: "Denied group chat {chat_id}.",
        LANGUAGE_CHINESE: "已拒絕群組 {chat_id}。",
    },
    "auth.group_title_unknown": {
        LANGUAGE_ENGLISH: "Unnamed group",
        LANGUAGE_CHINESE: "未命名群組",
    },
    "auth.group_access_request": {
        LANGUAGE_ENGLISH: (
            "An unapproved group tried to use the bot.\n"
            "Group: {title}\nChat ID: {chat_id}\n"
            "Approve it with /allow_chat {chat_id}."
        ),
        LANGUAGE_CHINESE: (
            "未核准的群組嘗試使用 bot。\n"
            "群組：{title}\n聊天 ID：{chat_id}\n"
            "可使用 /allow_chat {chat_id} 核准。"
        ),
    },
    "health.none": {
        LANGUAGE_ENGLISH: "None",
        LANGUAGE_CHINESE: "無",
    },
    "health.yes": {
        LANGUAGE_ENGLISH: "Yes",
        LANGUAGE_CHINESE: "是",
    },
    "health.no": {
        LANGUAGE_ENGLISH: "No",
        LANGUAGE_CHINESE: "否",
    },
    "health.client": {
        LANGUAGE_ENGLISH: "Client status: {state}",
        LANGUAGE_CHINESE: "Client 狀態：{state}",
    },
    "health.account": {
        LANGUAGE_ENGLISH: "Account status: {status}",
        LANGUAGE_CHINESE: "Account status：{status}",
    },
    "health.accepting": {
        LANGUAGE_ENGLISH: "Accepting requests: {accepting}",
        LANGUAGE_CHINESE: "接受請求：{accepting}",
    },
    "health.last_error": {
        LANGUAGE_ENGLISH: "Last error: {error}",
        LANGUAGE_CHINESE: "最近錯誤：{error}",
    },
    "health.database": {
        LANGUAGE_ENGLISH: "Database status: {state}",
        LANGUAGE_CHINESE: "DB 狀態：{state}",
    },
    "account.not_initialized": {
        LANGUAGE_ENGLISH: "Not initialized",
        LANGUAGE_CHINESE: "尚未完成初始化",
    },
    "account.location_rejected": {
        LANGUAGE_ENGLISH: (
            "{details}\nThe cookie may have been acquired from a different IP "
            "address. Acquire it again using the SSH SOCKS procedure in the README."
        ),
        LANGUAGE_CHINESE: (
            "{details}\n可能是 Cookie 取得 IP 與服務使用 IP 不符；"
            "請依 README 的 SSH SOCKS 流程重新取得 Cookie。"
        ),
    },
    "account.restricted": {
        LANGUAGE_ENGLISH: (
            "{details}\nThis is likely an account-level restriction that a new "
            "cookie cannot resolve. Check the Google account status."
        ),
        LANGUAGE_CHINESE: (
            "{details}\n這可能是帳號層級限制，換 Cookie 無法解除此限制；"
            "請檢查 Google 帳號狀態。"
        ),
    },
    "account.terms_pending": {
        LANGUAGE_ENGLISH: (
            "{details}\nAccept the latest terms in the Gemini web app and try again."
        ),
        LANGUAGE_CHINESE: "{details}\n請至 Gemini 網頁版接受最新服務條款後再試。",
    },
    "account.temporarily_unavailable": {
        LANGUAGE_ENGLISH: (
            "{details}\nGemini is temporarily restricted. The service will retry "
            "after the cooldown."
        ),
        LANGUAGE_CHINESE: "{details}\nGemini 暫時受限，服務會在冷卻後自動重試。",
    },
    "service.blocked_notification.one": {
        LANGUAGE_ENGLISH: (
            "Gemini is temporarily blocked and will retry in about 1 minute. "
            "No action is needed."
        ),
        LANGUAGE_CHINESE: (
            "Gemini 暫時封鎖，將於約 1 分鐘後自動重試，無需人工介入"
        ),
    },
    "service.blocked_notification.many": {
        LANGUAGE_ENGLISH: (
            "Gemini is temporarily blocked and will retry in about {minutes} "
            "minutes. No action is needed."
        ),
        LANGUAGE_CHINESE: (
            "Gemini 暫時封鎖，將於約 {minutes} 分鐘後自動重試，無需人工介入"
        ),
    },
    "media.default_prompt": {
        LANGUAGE_ENGLISH: "Analyze the contents of this file.",
        LANGUAGE_CHINESE: "請分析這個檔案的內容。",
    },
    "media.too_large": {
        LANGUAGE_ENGLISH: (
            "This file exceeds the Telegram Bot API 20 MB limit and cannot be processed."
        ),
        LANGUAGE_CHINESE: "檔案超過 Telegram Bot API 的 20 MB 上限，無法處理。",
    },
    "media.size_unknown": {
        LANGUAGE_ENGLISH: (
            "The file size could not be verified. The download was rejected to "
            "avoid exceeding the 20 MB limit."
        ),
        LANGUAGE_CHINESE: "無法確認檔案大小；為避免超過 20 MB 上限，已拒絕下載。",
    },
    "stream.placeholder": {
        LANGUAGE_ENGLISH: "Thinking…",
        LANGUAGE_CHINESE: "思考中…",
    },
    "stream.empty": {
        LANGUAGE_ENGLISH: "Gemini returned no text.",
        LANGUAGE_CHINESE: "Gemini 未回傳文字。",
    },
    "stream.thinking_elapsed.one": {
        LANGUAGE_ENGLISH: "{placeholder} 1 second",
        LANGUAGE_CHINESE: "{placeholder} 1 秒",
    },
    "stream.thinking_elapsed.many": {
        LANGUAGE_ENGLISH: "{placeholder} {seconds} seconds",
        LANGUAGE_CHINESE: "{placeholder} {seconds} 秒",
    },
    "thoughts.truncated": {
        LANGUAGE_ENGLISH: "…(thought process truncated)",
        LANGUAGE_CHINESE: "…（思考過程過長，已截斷）",
    },
    "thoughts.title": {
        LANGUAGE_ENGLISH: "Thought process",
        LANGUAGE_CHINESE: "思考過程",
    },
    "thoughts.title_elapsed.one": {
        LANGUAGE_ENGLISH: "Thought process (1 second)\n",
        LANGUAGE_CHINESE: "思考過程（1 秒）\n",
    },
    "thoughts.title_elapsed.many": {
        LANGUAGE_ENGLISH: "Thought process ({seconds} seconds)\n",
        LANGUAGE_CHINESE: "思考過程（{seconds} 秒）\n",
    },
}


def translate(key: str, language: str, /, **params: object) -> str:
    """Return one localized message, formatting named parameters.

    A missing translation falls back to English.  An unknown message ID is a
    programming error and deliberately raises :class:`KeyError`.
    """

    translations = MESSAGES[key]
    message = translations.get(language, translations[LANGUAGE_ENGLISH])
    return message.format(**params)


def resolve_language(stored: str | None, telegram_code: str | None) -> str:
    """Resolve a stored preference, Telegram hint, or the English default.

    A stored ``/language`` choice remains authoritative.  Automatic detection
    conservatively accepts only the exact Traditional Chinese tags supported
    by this project.  Some Traditional Chinese users may initially receive
    English and need to choose ``/language``, but this avoids incorrectly
    presenting Traditional Chinese to users whose tag denotes Simplified
    Chinese or is otherwise ambiguous.
    """

    candidate = stored if stored is not None else telegram_code
    if candidate is None:
        return DEFAULT_LANGUAGE
    if candidate.casefold() in _TRADITIONAL_CHINESE_TELEGRAM_CODES:
        return LANGUAGE_CHINESE
    return LANGUAGE_ENGLISH
