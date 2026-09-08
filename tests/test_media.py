from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from curl_cffi.requests import AsyncSession
from gemini_webapi import Candidate, ModelOutput, WebImage
from pydantic import SecretStr
from telegram.constants import MessageLimit, ParseMode
from telegram.error import BadRequest, RetryAfter

from gemini_tg_bot.queue import RequestQueue
from gemini_tg_bot.telegram.handlers import EgressMeter, TelegramHandlers
from gemini_tg_bot.telegram.media import (
    DEFAULT_MEDIA_PROMPT,
    DeliveryMode,
    DeliveryResult,
    ImageSource,
    MAX_CAPTION_VISIBLE_LENGTH,
    MAX_UPLOAD_BYTES,
    MediaHandler,
    UploadSizeUnknownError,
    UploadTooLargeError,
)
from gemini_tg_bot.telegram.streaming import EMPTY_RESPONSE_TEXT, PLACEHOLDER_TEXT
from scripts.dump_response import serialize_output


class FakeImage:
    def __init__(self, url: str, payload: bytes = b"image-bytes") -> None:
        self.url = url
        self.title = "Generated image"
        self.alt = "test image"
        self.payload = payload
        self.save_calls: list[str] = []
        self.saved_path: Path | None = None

    async def save(self, path: str = "temp", **kwargs: Any) -> str:
        del kwargs
        self.save_calls.append(path)
        self.saved_path = Path(path) / "image.png"
        self.saved_path.write_bytes(self.payload)
        return str(self.saved_path)


def _message() -> SimpleNamespace:
    return SimpleNamespace(
        reply_text=AsyncMock(),
        reply_photo=AsyncMock(),
        reply_document=AsyncMock(),
    )


def _output(text: str, *, web_images: list[WebImage] | None = None) -> ModelOutput:
    return ModelOutput(
        metadata=["cid", "rid", "rcid"],
        candidates=[
            Candidate(
                rcid="rcid",
                text=text,
                web_images=web_images or [],
            )
        ],
    )


def _simple_output(
    *,
    web_images: list[FakeImage] | None = None,
    generated_images: list[FakeImage] | None = None,
) -> SimpleNamespace:
    web = web_images or []
    generated = generated_images or []
    return SimpleNamespace(
        images=[*web, *generated],
        candidates=[
            SimpleNamespace(
                web_images=web,
                generated_images=generated,
            )
        ],
        chosen=0,
    )


async def test_default_url_delivery_has_zero_egress() -> None:
    meter = EgressMeter()
    media = MediaHandler(egress_meter=meter)
    message = _message()
    image = FakeImage("https://example.test/image.png")

    result = await media.send_image(message, image)

    assert result.mode is DeliveryMode.URL
    assert result.relayed_bytes == 0
    assert meter.month_to_date_bytes == 0
    message.reply_photo.assert_awaited_once_with(image.url, caption=None)
    assert image.save_calls == []


async def test_bad_request_falls_back_to_temporary_relay_and_meters_bytes(
    tmp_path: Path,
) -> None:
    meter = EgressMeter()
    media = MediaHandler(egress_meter=meter, temp_root=tmp_path)
    message = _message()
    message.reply_photo.side_effect = [BadRequest("could not fetch URL"), None]
    image = FakeImage("https://example.test/private.png", b"1234567")

    result = await media.send_image(message, image)

    assert result.mode is DeliveryMode.RELAY
    assert result.relayed_bytes == 7
    assert meter.month_to_date_bytes == 7
    assert message.reply_photo.await_count == 2
    assert message.reply_photo.await_args_list[0] == call(image.url, caption=None)
    assert image.saved_path is not None
    assert not image.saved_path.exists()


@pytest.mark.parametrize("mode", [DeliveryMode.URL, DeliveryMode.RELAY])
async def test_flood_control_retries_both_image_delivery_paths(
    mode: DeliveryMode,
    tmp_path: Path,
) -> None:
    meter = EgressMeter()
    media = MediaHandler(
        egress_meter=meter,
        delivery_mode=mode,
        temp_root=tmp_path,
    )
    message = _message()
    message.reply_photo.side_effect = [RetryAfter(2), None]
    image = FakeImage("https://example.test/retry.png", b"retry-image")
    sleep = AsyncMock()

    with patch("gemini_tg_bot.telegram.sending.asyncio.sleep", sleep):
        result = await media.send_image(message, image)

    sleep.assert_awaited_once_with(2.0)
    assert message.reply_photo.await_count == 2
    first_media = message.reply_photo.await_args_list[0].args[0]
    second_media = message.reply_photo.await_args_list[1].args[0]
    if mode is DeliveryMode.URL:
        assert result == DeliveryResult(DeliveryMode.URL)
        assert first_media == image.url
        assert second_media == image.url
        assert meter.month_to_date_bytes == 0
        assert image.save_calls == []
    else:
        assert result == DeliveryResult(
            DeliveryMode.RELAY,
            relayed_bytes=len(image.payload),
        )
        assert first_media is second_media
        assert meter.month_to_date_bytes == len(image.payload)


async def test_fallback_is_per_image_and_does_not_disable_next_url() -> None:
    meter = EgressMeter()
    media = MediaHandler(egress_meter=meter)
    message = _message()
    message.reply_photo.side_effect = [BadRequest("first URL rejected"), None, None]
    first = FakeImage("https://example.test/first.png", b"first")
    second = FakeImage("https://example.test/second.png", b"second")

    results = await media.send_images(message, [first, second])

    assert [result.mode for result in results] == [
        DeliveryMode.RELAY,
        DeliveryMode.URL,
    ]
    assert meter.month_to_date_bytes == len(first.payload)
    assert message.reply_photo.await_args_list[-1] == call(second.url, caption=None)
    assert second.save_calls == []


async def test_forced_modes_and_document_url_delivery() -> None:
    relay_meter = EgressMeter()
    relay = MediaHandler(
        egress_meter=relay_meter,
        delivery_mode=DeliveryMode.RELAY,
    )
    relay_message = _message()
    relay_image = FakeImage("https://example.test/relay.png", b"relay")

    relay_result = await relay.send_image(relay_message, relay_image)

    assert relay_result.mode is DeliveryMode.RELAY
    assert relay_message.reply_photo.await_count == 1
    assert relay_meter.month_to_date_bytes == 5

    url = MediaHandler(
        egress_meter=EgressMeter(),
        delivery_mode=DeliveryMode.URL,
    )
    url_message = _message()
    url_image = FakeImage("https://example.test/document.png")

    result = await url.send_image(url_message, url_image, as_document=True)

    assert result.mode is DeliveryMode.URL
    url_message.reply_document.assert_awaited_once_with(
        url_image.url,
        caption=None,
    )


async def test_forced_url_does_not_fallback_on_bad_request() -> None:
    media = MediaHandler(
        egress_meter=EgressMeter(),
        delivery_mode="url",
    )
    message = _message()
    message.reply_photo.side_effect = BadRequest("URL rejected")
    image = FakeImage("https://example.test/rejected.png")

    with pytest.raises(BadRequest):
        await media.send_image(message, image)

    assert image.save_calls == []


async def test_source_specific_mode_reserves_web_generated_configuration() -> None:
    media = MediaHandler(
        egress_meter=EgressMeter(),
        delivery_mode="relay",
        web_image_mode="url",
        generated_image_mode="auto",
    )
    message = _message()
    image = FakeImage("https://example.test/web.png")

    result = await media.send_image(message, image, source=ImageSource.WEB)

    assert result.mode is DeliveryMode.URL
    assert image.save_calls == []


@pytest.mark.parametrize("source", [ImageSource.WEB, ImageSource.GENERATED])
async def test_output_caption_applies_to_each_image_source(source: ImageSource) -> None:
    media = MediaHandler(egress_meter=EgressMeter())
    message = _message()
    image = FakeImage(f"https://example.test/{source.value}.png")
    output = _simple_output(
        web_images=[image] if source is ImageSource.WEB else None,
        generated_images=[image] if source is ImageSource.GENERATED else None,
    )

    await media.send_output_images(message, output, caption="<b>answer</b>")

    message.reply_photo.assert_awaited_once_with(
        image.url,
        caption="<b>answer</b>",
        parse_mode=ParseMode.HTML,
    )


async def test_output_caption_counts_visible_text_instead_of_html_tags() -> None:
    media = MediaHandler(egress_meter=EgressMeter())
    message = _message()
    image = FakeImage("https://example.test/within-limit.png")
    output = _simple_output(web_images=[image])
    caption = f"<b>{'x' * MAX_CAPTION_VISIBLE_LENGTH}</b>"

    await media.send_output_images(message, output, caption=caption)

    message.reply_photo.assert_awaited_once_with(
        image.url,
        caption=caption,
        parse_mode=ParseMode.HTML,
    )


async def test_output_caption_over_telegram_limit_is_omitted() -> None:
    media = MediaHandler(egress_meter=EgressMeter())
    message = _message()
    image = FakeImage("https://example.test/over-limit.png")
    output = _simple_output(generated_images=[image])

    await media.send_output_images(
        message,
        output,
        caption="x" * (MessageLimit.CAPTION_LENGTH + 1),
    )

    message.reply_photo.assert_awaited_once_with(image.url, caption=None)


async def test_output_caption_is_attached_only_to_first_image() -> None:
    media = MediaHandler(egress_meter=EgressMeter())
    message = _message()
    web_image = FakeImage("https://example.test/web.png")
    generated_image = FakeImage("https://example.test/generated.png")
    output = _simple_output(
        web_images=[web_image],
        generated_images=[generated_image],
    )

    await media.send_output_images(message, output, caption="answer")

    assert message.reply_photo.await_args_list == [
        call(web_image.url, caption="answer", parse_mode=ParseMode.HTML),
        call(generated_image.url, caption=None),
    ]


def test_video_and_audio_generation_are_disabled_unless_explicitly_enabled() -> None:
    default = MediaHandler(egress_meter=EgressMeter())
    enabled = MediaHandler(
        egress_meter=EgressMeter(),
        enable_video_generation=True,
        enable_audio_generation=True,
    )

    assert default.video_generation_enabled is False
    assert default.audio_generation_enabled is False
    assert enabled.video_generation_enabled is True
    assert enabled.audio_generation_enabled is True


async def test_prepare_document_uses_caption_and_removes_temporary_file(
    tmp_path: Path,
) -> None:
    payload = b"pdf-data"

    async def download_to_drive(*, custom_path: Path) -> Path:
        custom_path.write_bytes(payload)
        return custom_path

    telegram_file = SimpleNamespace(download_to_drive=AsyncMock(side_effect=download_to_drive))
    document = SimpleNamespace(
        file_size=len(payload),
        file_name="../report.pdf",
        get_file=AsyncMock(return_value=telegram_file),
    )
    message = SimpleNamespace(
        caption="summarize this",
        photo=[],
        document=document,
    )
    media = MediaHandler(egress_meter=EgressMeter(), temp_root=tmp_path)

    async with media.prepare_upload(message) as upload:
        uploaded_path = Path(upload.files[0])
        assert upload.prompt == "summarize this"
        assert upload.num_bytes == len(payload)
        assert uploaded_path.name == "report.pdf"
        assert uploaded_path.read_bytes() == payload

    assert not uploaded_path.exists()
    document.get_file.assert_awaited_once_with()


async def test_prepare_photo_uses_default_prompt() -> None:
    payload = b"jpeg"

    async def download_to_drive(*, custom_path: Path) -> Path:
        custom_path.write_bytes(payload)
        return custom_path

    telegram_file = SimpleNamespace(download_to_drive=AsyncMock(side_effect=download_to_drive))
    photo = SimpleNamespace(
        file_size=len(payload),
        file_unique_id="photo-id",
        get_file=AsyncMock(return_value=telegram_file),
    )
    message = SimpleNamespace(caption=None, photo=[photo], document=None)
    media = MediaHandler(egress_meter=EgressMeter())

    async with media.prepare_upload(message) as upload:
        assert upload.prompt == DEFAULT_MEDIA_PROMPT
        assert Path(upload.files[0]).suffix == ".jpg"


async def test_upload_limit_is_checked_before_get_file_or_download() -> None:
    oversized = SimpleNamespace(
        file_size=MAX_UPLOAD_BYTES + 1,
        file_name="large.bin",
        get_file=AsyncMock(),
    )
    message = SimpleNamespace(caption=None, photo=[], document=oversized)
    media = MediaHandler(egress_meter=EgressMeter())

    with pytest.raises(UploadTooLargeError, match="20 MB"):
        async with media.prepare_upload(message):
            pytest.fail("oversized upload must not enter the context")

    oversized.get_file.assert_not_awaited()


async def test_unknown_upload_size_is_rejected_before_download() -> None:
    attachment = SimpleNamespace(
        file_size=None,
        file_name="unknown.bin",
        get_file=AsyncMock(),
    )
    message = SimpleNamespace(caption=None, photo=[], document=attachment)
    media = MediaHandler(egress_meter=EgressMeter())

    with pytest.raises(UploadSizeUnknownError, match="無法確認"):
        async with media.prepare_upload(message):
            pytest.fail("unknown-size upload must not enter the context")

    attachment.get_file.assert_not_awaited()


async def test_handler_cleans_artifact_only_text_sends_image_and_logs_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    image = WebImage(
        url="https://example.test/generated.png",
        title="Generated",
        alt="A generated test image",
    )
    output = _output("_551", web_images=[image])
    session = SimpleNamespace()
    sessions = AsyncMock()
    sessions.get_state.return_value = SimpleNamespace(
        model=None,
        temporary=False,
    )
    sessions.get_or_create.return_value = session

    async def generate() -> Any:
        yield output

    client = SimpleNamespace(
        generate_content_stream=MagicMock(return_value=generate()),
    )

    async def execute(operation: Any) -> Any:
        return await operation(client)

    service = MagicMock()
    service.execute = AsyncMock(side_effect=execute)
    message = _message()
    placeholder = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
    message.reply_text.return_value = placeholder
    message.text = "generate an image"
    message.photo = []
    message.document = None
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=101),
        effective_chat=SimpleNamespace(id=202),
        effective_message=message,
    )
    handlers = TelegramHandlers(
        service=service,
        sessions=sessions,
        request_queue=RequestQueue(
            max_concurrency=1,
            user_rate_limit_per_min=10,
        ),
        usage_dao=None,
        egress_meter=EgressMeter(),
        cookie_path=Path("unused"),
        secure_1psid=SecretStr("FAKE_1PSID_FOR_TEST"),
    )

    with caplog.at_level(logging.DEBUG, logger="gemini_tg_bot.telegram.handlers"):
        await handlers.text_message(update, SimpleNamespace())

    message.reply_text.assert_awaited_once_with(PLACEHOLDER_TEXT)
    placeholder.edit_text.assert_awaited_once_with(
        EMPTY_RESPONSE_TEXT,
        parse_mode=None,
    )
    placeholder.delete.assert_awaited_once_with()
    assert "_551" not in str(placeholder.edit_text.await_args_list)
    message.reply_photo.assert_awaited_once_with(image.url, caption=None)
    assert "text='_551' image_count=1" in caplog.text
    assert image.url in caplog.text
    assert "title='Generated'" in caplog.text
    assert "alt='A generated test image'" in caplog.text


async def test_handler_passes_upload_as_files_and_cleans_it_after_send(
    tmp_path: Path,
) -> None:
    payload = b"user-image"
    seen_path: Path | None = None

    async def download_to_drive(*, custom_path: Path) -> Path:
        custom_path.write_bytes(payload)
        return custom_path

    telegram_file = SimpleNamespace(download_to_drive=AsyncMock(side_effect=download_to_drive))
    photo = SimpleNamespace(
        file_size=len(payload),
        file_unique_id="photo-id",
        get_file=AsyncMock(return_value=telegram_file),
    )
    output = _output("analysis complete")

    async def send_message(prompt: str, **kwargs: Any) -> ModelOutput:
        nonlocal seen_path
        seen_path = Path(kwargs["files"][0])
        assert seen_path.exists()
        assert prompt == DEFAULT_MEDIA_PROMPT
        assert kwargs["temporary"] is True
        return output

    session = SimpleNamespace(send_message=AsyncMock(side_effect=send_message))
    sessions = AsyncMock()
    sessions.get_state.return_value = SimpleNamespace(
        model="dynamic-model",
        temporary=True,
    )
    sessions.get_or_create.return_value = session

    async def execute(operation: Any) -> Any:
        return await operation(MagicMock())

    service = MagicMock()
    service.execute = AsyncMock(side_effect=execute)
    meter = EgressMeter()
    media = MediaHandler(egress_meter=meter, temp_root=tmp_path)
    message = _message()
    message.text = None
    message.caption = None
    message.photo = [photo]
    message.document = None
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=101),
        effective_chat=SimpleNamespace(id=202),
        effective_message=message,
    )
    handlers = TelegramHandlers(
        service=service,
        sessions=sessions,
        request_queue=RequestQueue(
            max_concurrency=1,
            user_rate_limit_per_min=10,
        ),
        usage_dao=None,
        egress_meter=meter,
        cookie_path=Path("unused"),
        secure_1psid=SecretStr("FAKE_1PSID_FOR_TEST"),
        media_handler=media,
    )

    await handlers.media_message(update, SimpleNamespace())

    assert seen_path is not None
    assert not seen_path.exists()
    assert meter.month_to_date_bytes == len(payload)
    sessions.persist.assert_awaited_once_with(202, session)
    message.reply_text.assert_awaited_once_with(
        "analysis complete",
        parse_mode="HTML",
    )


async def test_fixture_serializer_round_trips_a_real_model_output() -> None:
    client = AsyncSession()
    try:
        output = _output(
            "decoded & text",
            web_images=[
                WebImage(
                    url="https://example.test/image.png",
                    title="Title",
                    alt="Alt",
                    client=client,
                )
            ],
        )

        payload = serialize_output(output)
        restored = ModelOutput.model_validate_json(payload)

        assert restored.text == "decoded & text"
        assert restored.images[0].url == "https://example.test/image.png"
        assert restored.images[0].title == "Title"
        assert restored.images[0].alt == "Alt"
        assert restored.images[0].client is None
        assert "FAKE_1PSID_FOR_TEST" not in payload
    finally:
        await client.close()
