"""Telegram media upload preparation and egress-aware image delivery.

The default outbound route is optimistic URL delivery: Telegram fetches the
image itself and the VM sends no media bytes. In automatic mode a rejected
single image falls back individually, while a rejected media group falls back
atomically by relaying every image in that group.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from telegram import InputMediaPhoto
from telegram.constants import MediaGroupLimit, MessageLimit, ParseMode
from telegram.error import BadRequest

from .sending import send_document, send_media_group, send_photo


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
        """Send one photo or one or more Telegram media groups."""

        return await self._send_grouped_images(
            message,
            [(image, source) for image in images],
        )

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
        images: list[tuple[GeminiImage, ImageSource]] = [
            (image, ImageSource.WEB) for image in candidate.web_images
        ] + [
            (image, ImageSource.GENERATED)
            for image in candidate.generated_images
        ]
        safe_caption = caption if caption_is_eligible(caption) else None
        return await self._send_grouped_images(
            message,
            images,
            caption=safe_caption,
        )

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
        send_kwargs: dict[str, Any] = {"caption": caption}
        if caption is not None:
            send_kwargs["parse_mode"] = ParseMode.HTML

        if mode is not DeliveryMode.RELAY:
            try:
                await _send_image_reply(
                    message,
                    image.url,
                    as_document=as_document,
                    **send_kwargs,
                )
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
                await _send_image_reply(
                    message,
                    media_file,
                    as_document=as_document,
                    **send_kwargs,
                )

        self._egress_meter.record(num_bytes)
        return DeliveryResult(DeliveryMode.RELAY, relayed_bytes=num_bytes)

    async def _send_grouped_images(
        self,
        message: Any,
        images: Sequence[tuple[GeminiImage, ImageSource]],
        *,
        caption: str | None = None,
    ) -> list[DeliveryResult]:
        """Send ordered images in mode-homogeneous, size-limited batches."""

        results: list[DeliveryResult] = []
        pending_caption = caption
        for batch_index, batch in enumerate(self._image_batches(images), start=1):
            if len(batch) < MediaGroupLimit.MIN_MEDIA_LENGTH:
                image, source = batch[0]
                result = await self.send_image(
                    message,
                    image,
                    caption=pending_caption,
                    source=source,
                )
                results.append(result)
            else:
                results.extend(
                    await self._send_image_group(
                        message,
                        batch,
                        caption=pending_caption,
                        batch_index=batch_index,
                    )
                )
            pending_caption = None
        return results

    async def _send_image_group(
        self,
        message: Any,
        images: Sequence[tuple[GeminiImage, ImageSource]],
        *,
        caption: str | None,
        batch_index: int,
    ) -> list[DeliveryResult]:
        """Deliver one atomic Telegram media group by URL or measured relay."""

        mode = self._mode_for(images[0][1])
        if any(self._mode_for(source) is not mode for _, source in images):
            raise ValueError("media group contains mixed delivery modes")

        if mode is not DeliveryMode.RELAY:
            url_media = _input_media_photos(
                [image.url for image, _ in images],
                caption=caption,
            )
            try:
                await send_media_group(message, url_media)
            except BadRequest:
                if mode is DeliveryMode.URL:
                    raise
                LOGGER.warning(
                    "Telegram rejected media group URLs; falling back to VM "
                    "relay batch=%d image_count=%d",
                    batch_index,
                    len(images),
                )
            else:
                return [DeliveryResult(DeliveryMode.URL) for _ in images]

        return await self._relay_image_group(
            message,
            images,
            caption=caption,
        )

    async def _relay_image_group(
        self,
        message: Any,
        images: Sequence[tuple[GeminiImage, ImageSource]],
        *,
        caption: str | None,
    ) -> list[DeliveryResult]:
        """Download every image in a group, then upload the group atomically."""

        with TemporaryDirectory(
            prefix="gemini-tg-relay-",
            dir=self._temp_root,
        ) as temp_directory:
            saved_paths: list[Path] = []
            relayed_bytes: list[int] = []
            for index, (image, _) in enumerate(images):
                image_directory = Path(temp_directory) / str(index)
                image_directory.mkdir()
                saved_path = Path(await image.save(path=str(image_directory)))
                saved_paths.append(saved_path)
                relayed_bytes.append(saved_path.stat().st_size)

            with ExitStack() as stack:
                media_files = [
                    stack.enter_context(saved_path.open("rb"))
                    for saved_path in saved_paths
                ]
                relay_media = _input_media_photos(media_files, caption=caption)
                await send_media_group(message, relay_media)

        self._egress_meter.record(sum(relayed_bytes))
        return [
            DeliveryResult(DeliveryMode.RELAY, relayed_bytes=num_bytes)
            for num_bytes in relayed_bytes
        ]

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

    def _image_batches(
        self,
        images: Sequence[tuple[GeminiImage, ImageSource]],
    ) -> list[list[tuple[GeminiImage, ImageSource]]]:
        """Split images by effective delivery mode and Telegram group limits."""

        mode_runs: list[list[tuple[GeminiImage, ImageSource]]] = []
        for image in images:
            mode = self._mode_for(image[1])
            if not mode_runs or self._mode_for(mode_runs[-1][0][1]) is not mode:
                mode_runs.append([])
            mode_runs[-1].append(image)

        batches: list[list[tuple[GeminiImage, ImageSource]]] = []
        for mode_run in mode_runs:
            batches.extend(_split_media_groups(mode_run))
        return batches


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


async def _send_image_reply(
    message: Any,
    media: Any,
    *,
    as_document: bool,
    **kwargs: Any,
) -> Any:
    if as_document:
        return await send_document(message, media, **kwargs)
    return await send_photo(message, media, **kwargs)


def _input_media_photos(
    media: Sequence[Any],
    *,
    caption: str | None,
) -> list[InputMediaPhoto]:
    """Build a media group with an optional caption on its first item only."""

    return [
        InputMediaPhoto(
            item,
            caption=caption if index == 0 else None,
            parse_mode=ParseMode.HTML if index == 0 and caption is not None else None,
        )
        for index, item in enumerate(media)
    ]


def _split_media_groups(
    images: Sequence[tuple[GeminiImage, ImageSource]],
) -> list[list[tuple[GeminiImage, ImageSource]]]:
    """Chunk one mode run without leaving an invalid one-item media group."""

    minimum = int(MediaGroupLimit.MIN_MEDIA_LENGTH)
    maximum = int(MediaGroupLimit.MAX_MEDIA_LENGTH)
    batches: list[list[tuple[GeminiImage, ImageSource]]] = []
    start = 0
    while start < len(images):
        remaining = len(images) - start
        batch_size = min(maximum, remaining)
        trailing = remaining - batch_size
        if trailing and trailing < minimum:
            batch_size -= minimum - trailing
        batches.append(list(images[start : start + batch_size]))
        start += batch_size
    return batches


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
