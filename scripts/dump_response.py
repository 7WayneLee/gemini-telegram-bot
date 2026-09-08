#!/usr/bin/env python3
"""Capture one live Gemini ModelOutput fixture for user-run diagnostics only.

This tool intentionally performs a real request and therefore must only be run
by the human operator.  It reuses ``GeminiService`` so the process still owns
exactly one Gemini client, and excludes non-serializable live client/session
references from the resulting pydantic fixture.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from gemini_tg_bot.config import Settings
from gemini_tg_bot.gemini.service import GeminiService


FIXTURES_DIRECTORY = Path(__file__).parents[1] / "tests" / "fixtures"
MODEL_OUTPUT_FIXTURE_EXCLUDE = {
    "candidates": {
        "__all__": {
            "web_images": {"__all__": {"client"}},
            "generated_images": {"__all__": {"client", "client_ref"}},
        }
    }
}


def serialize_output(output: Any) -> str:
    """Serialize a ModelOutput without live HTTP or Gemini client references."""

    return output.model_dump_json(
        exclude=MODEL_OUTPUT_FIXTURE_EXCLUDE,
        indent=2,
    )


async def capture(prompt: str, output_path: Path) -> None:
    """Make the single user-authorized request and write its safe fixture."""

    settings = Settings()
    service = GeminiService(settings)
    try:
        await service.init()
        output = await service.execute(
            lambda client: client.generate_content(prompt)
        )
        payload = serialize_output(output)
    finally:
        await service.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(payload + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="the one prompt to send to Gemini")
    parser.add_argument(
        "--output",
        default="model-output.json",
        help="fixture filename under tests/fixtures (default: model-output.json)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing fixture",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output_name = Path(args.output)
    if output_name.name != args.output or output_name.suffix.lower() != ".json":
        raise SystemExit("--output must be a .json filename without directories")

    output_path = FIXTURES_DIRECTORY / output_name
    if output_path.exists() and not args.force:
        raise SystemExit(f"fixture already exists: {output_path.name}; use --force")

    asyncio.run(capture(args.prompt, output_path))
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
