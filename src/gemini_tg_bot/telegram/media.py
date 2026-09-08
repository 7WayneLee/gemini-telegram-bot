"""Telegram media upload preparation and egress-aware image delivery.

The default outbound route is optimistic URL delivery: Telegram fetches the
image itself and the VM sends no media bytes.  In automatic mode only the
individual image rejected by Telegram falls back to a temporary download and
metered upload; a failure never disables URL delivery for later images.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from telegram.constants import MessageLimit, ParseMode
from telegram.error import BadRequest


LOGGER = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
CAPTION_LENGTH_SAFETY_MARGIN = 16
MAX_CAPTION_VISIBLE_LENGTH = (
    MessageLimit.CAPTION_LENGTH - CAPTION_LENGTH_SAFETY_MARGIN
)
DEFAULT_MEDIA_PROMPT = "請分析這個檔案的內容。"
UPLOAD_TOO_LARGE = "檔案超過 Telegram Bot API 的 20 MB 上限，無法處理。"
UPLOAD_SIZE_UNKNOWN = "無法確認檔案大小；為避免超過 20 MB 上限，已拒絕下載。"


class DeliveryMode(StrEnum):
    """How an outbound image is handed to Telegram."""

    AUTO = "auto"
    URL = "url"
    RELAY = "relay"


class ImageSource(StrEnum):
    """Image origin, kept separate so routes can be configured independently."""

    GENERIC = "generic"
    WEB = "web"
    GENERATED = "generated"


class EgressRecorder(Protocol):
    """The minimal ``EgressMeter`` interface owned by the handlers layer."""

    def record(self, num_bytes: int) -> None: ...


class GeminiImage(Protocol):
    """The documented image surface from ``upstream-api-contract.md`` section 5."""

    url: str
    title: str
    alt: str

    async def save(
        self,
        path: str = "temp",
        filename: str | None = None,
        verbose: bool = False,
        client: Any | None = None,
        **kwargs: Any,
    ) -> str: ...


class MediaUploadError(ValueError):
    """Base class for a Telegram upload rejected before downloading it."""


class UploadTooLargeError(MediaUploadError):
    """Raised when Telegram metadata reports a file above the hard limit."""


class UploadSizeUnknownError(MediaUploadError):
    """Raised when the hard limit cannot be checked before downloading."""


class UnsupportedUploadError(MediaUploadError):
    """Raised when a message contains neither a photo nor a document."""


@dataclass(frozen=True, slots=True)
class PreparedUpload:
    """A prompt and temporary file list ready for ``send_message(files=...)``."""

    prompt: str
    files: list[str]
    num_bytes: int


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Observable result for one image delivery attempt."""

    mode: DeliveryMode
    relayed_bytes: int = 0


class _VisibleTextParser(HTMLParser):
    """Measure text after Telegram parses supported HTML entities and tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.length = 0

    def handle_data(self, data: str) -> None:
        self.length += len(data)


def caption_is_eligible(caption: str | None) -> bool:
    """Return whether rendered HTML safely fits Telegram's caption limit."""

    if not caption:
        return False
    parser = _VisibleTextParser()
    parser.feed(caption)
    parser.close()
    return parser.length <= MAX_CAPTION_VISIBLE_LENGTH


class MediaHandler:
    """Prepare inbound files and deliver Gemini images through Telegram."""

    def __init__(
        self,
        *,
        egress_meter: EgressRecorder,
        delivery_mode: DeliveryMode | str = DeliveryMode.AUTO,
        web_image_mode: DeliveryMode | str | None = None,
        generated_image_mode: DeliveryMode | str | None = None,
        temp_root: Path | None = None,
        enable_video_generation: bool = False,
        enable_audio_generation: bool = False,
    ) -> None:
        self._egress_meter = egress_meter
        self._delivery_mode = DeliveryMode(delivery_mode)
        self._source_modes = {
            ImageSource.WEB: (
                None if web_image_mode is None else DeliveryMode(web_image_mode)
            ),
            ImageSource.GENERATED: (
                None
                if generated_image_mode is None
                else DeliveryMode(generated_image_mode)
            ),
        }
        self._temp_root = temp_root
        self._enable_video_generation = enable_video_generation
        self._enable_audio_generation = enable_audio_generation

    @property
    def video_generation_enabled(self) -> bool:
        """Whether the caller explicitly enabled generated-video handling."""

        return self._enable_video_generation

    @property
    def audio_generation_enabled(self) -> bool:
        """Whether the caller explicitly enabled generated-audio handling."""

        return self._enable_audio_generation

    async def send_images(
        self,
        message: Any,
        images: Sequence[GeminiImage],
        *,
        source: ImageSource = ImageSource.GENERIC,
    ) -> list[DeliveryResult]:
        """Send images independently so one fallback does not affect the next."""

        return [
            await self.send_image(message, image, source=source)
            for image in images
        ]

    async def send_output_images(
        self,
        message: Any,
        output: Any,
        *,
        caption: str | None = None,
    ) -> list[DeliveryResult]:
        """Send the chosen output candidate's web and generated images in order.

        ``Candidate.images`` is contractually ``web_images + generated_images``.
        Keeping the two lists separate here reserves independent route settings
        without relying on runtime class-name checks.
        """

        candidate = output.candidates[output.chosen]
        images = [
            (image, ImageSource.WEB) for image in candidate.web_images
        ] + [
            (image, ImageSource.GENERATED)
            for image in candidate.generated_images
        ]
        safe_caption = caption if caption_is_eligible(caption) else None
        return [
            await self.send_image(
                message,
                image,
                caption=safe_caption if index == 0 else None,
                source=source,
            )
            for index, (image, source) in enumerate(images)
        ]

    async def send_image(
        self,
        message: Any,
        image: GeminiImage,
        *,
        caption: str | None = None,
        source: ImageSource = ImageSource.GENERIC,
        as_document: bool = False,
    ) -> DeliveryResult:
        """Deliver one image by URL or by a measured temporary relay."""

        mode = self._mode_for(source)
        sender = message.reply_document if as_document else message.reply_photo
        send_kwargs: dict[str, Any] = {"caption": caption}
        if caption is not None:
            send_kwargs["parse_mode"] = ParseMode.HTML

        if mode is not DeliveryMode.RELAY:
            try:
                await sender(image.url, **send_kwargs)
            except BadRequest:
                if mode is DeliveryMode.URL:
                    raise
                LOGGER.warning(
                    "Telegram rejected image URL; falling back to VM relay "
                    "source=%s",
                    source.value,
                )
            else:
                return DeliveryResult(DeliveryMode.URL)

        with TemporaryDirectory(
            prefix="gemini-tg-relay-",
            dir=self._temp_root,
        ) as temp_directory:
            saved_path = Path(await image.save(path=temp_directory))
            num_bytes = saved_path.stat().st_size
            with saved_path.open("rb") as media_file:
                await sender(media_file, **send_kwargs)

        self._egress_meter.record(num_bytes)
        return DeliveryResult(DeliveryMode.RELAY, relayed_bytes=num_bytes)

    @asynccontextmanager
    async def prepare_upload(self, message: Any) -> AsyncIterator[PreparedUpload]:
        """Download one checked Telegram photo/document and always remove it.

        The Telegram-provided size is validated before ``get_file`` or any
        download begins.  Unknown sizes are rejected because they cannot meet
        the contract's early-check guarantee.
        """

        attachment, filename = _select_upload(message)
        file_size = getattr(attachment, "file_size", None)
        if isinstance(file_size, bool) or not isinstance(file_size, int):
            raise UploadSizeUnknownError(UPLOAD_SIZE_UNKNOWN)
        if file_size > MAX_UPLOAD_BYTES:
            raise UploadTooLargeError(UPLOAD_TOO_LARGE)

        prompt = getattr(message, "caption", None) or DEFAULT_MEDIA_PROMPT
        with TemporaryDirectory(
            prefix="gemini-tg-upload-",
            dir=self._temp_root,
        ) as temp_directory:
            destination = Path(temp_directory) / filename
            telegram_file = await attachment.get_file()
            downloaded_path = Path(
                await telegram_file.download_to_drive(custom_path=destination)
            )
            yield PreparedUpload(
                prompt=prompt,
                files=[str(downloaded_path)],
                num_bytes=downloaded_path.stat().st_size,
            )

    def record_upload(self, upload: PreparedUpload) -> None:
        """Count bytes relayed from the VM to Gemini after a successful send."""

        self._egress_meter.record(upload.num_bytes)

    def _mode_for(self, source: ImageSource) -> DeliveryMode:
        specific = self._source_modes.get(source)
        return specific if specific is not None else self._delivery_mode


def _select_upload(message: Any) -> tuple[Any, str]:
    photos = getattr(message, "photo", None)
    if photos:
        photo = photos[-1]
        unique_id = getattr(photo, "file_unique_id", None) or "photo"
        return photo, f"{_safe_filename(str(unique_id))}.jpg"

    document = getattr(message, "document", None)
    if document is not None:
        raw_name = getattr(document, "file_name", None) or "document"
        return document, _safe_filename(str(raw_name))

    raise UnsupportedUploadError("message contains no photo or document")


def _safe_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name
    return name if name not in {"", ".", ".."} else "upload"


__all__ = [
    "CAPTION_LENGTH_SAFETY_MARGIN",
    "DEFAULT_MEDIA_PROMPT",
    "DeliveryMode",
    "DeliveryResult",
    "ImageSource",
    "MAX_UPLOAD_BYTES",
    "MAX_CAPTION_VISIBLE_LENGTH",
    "MediaHandler",
    "MediaUploadError",
    "PreparedUpload",
    "UPLOAD_SIZE_UNKNOWN",
    "UPLOAD_TOO_LARGE",
    "UnsupportedUploadError",
    "UploadSizeUnknownError",
    "UploadTooLargeError",
    "caption_is_eligible",
]
