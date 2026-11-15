"""Human-facing labels for dynamically listed Gemini models."""

from __future__ import annotations

import re
from typing import Any


# A version such as "4", "3.5" or "3.1" standing alone between whitespace or
# the string edges, as in "4 Hydrogen" or "3.5 Flash-Lite".
_VERSION_TOKEN = re.compile(r"(?<!\S)\d+(?:\.\d+)?(?!\S)")


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _has_version(value: str) -> bool:
    return _VERSION_TOKEN.search(value) is not None


def model_label(model: Any) -> str:
    """Return the real model name to show a person.

    Upstream ``AvailableModel.from_rpc`` fills ``display_name`` and
    ``description`` from two RPC fields whose meanings Google swapped on
    2026-11-15: the model name ("4 Hydrogen") moved from ``display_name`` to
    ``description`` and the task blurb ("Everyday help") moved the other way.
    When exactly one of the two carries a version number it is the model name;
    otherwise ``display_name`` is preferred, then ``description``, then the
    stable ``model_name``.
    """

    display_name = _text(getattr(model, "display_name", None))
    description = _text(getattr(model, "description", None))
    versioned = [
        candidate
        for candidate in (display_name, description)
        if candidate is not None and _has_version(candidate)
    ]
    if len(versioned) == 1:
        return versioned[0]
    if display_name is not None:
        return display_name
    if description is not None:
        return description
    return str(getattr(model, "model_name", ""))
