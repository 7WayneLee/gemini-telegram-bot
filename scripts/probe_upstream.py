#!/usr/bin/env python3
"""Reflect the installed gemini_webapi package without making network calls."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import inspect
import json
import pprint
import sys
import typing
from collections.abc import Callable

import gemini_webapi


_EMPTY = inspect.Signature.empty


def _qualified_name(value: type[object]) -> str:
    return f"{value.__module__}.{value.__qualname__}"


def _annotation_name(annotation: object) -> str | None:
    if annotation is _EMPTY:
        return None
    return inspect.formatannotation(annotation)


def _reflection_namespaces(target: object) -> tuple[dict[str, object], dict[str, object]]:
    module_name = getattr(target, "__module__", gemini_webapi.__name__)
    module = sys.modules.get(module_name)
    globalns = dict(vars(module)) if module is not None else {}
    exported = dict(vars(gemini_webapi))
    globalns.update(exported)
    return globalns, exported


def _type_hints(target: object) -> tuple[dict[str, object], str | None]:
    globalns, localns = _reflection_namespaces(target)
    try:
        return typing.get_type_hints(target, globalns=globalns, localns=localns), None
    except Exception as exc:  # Keep partial reflection useful across upstream releases.
        return inspect.get_annotations(target, eval_str=False), (
            f"{type(exc).__module__}.{type(exc).__qualname__}: {exc}"
        )


def _defined_on(cls: type[object], name: str) -> str | None:
    for owner in cls.__mro__:
        annotations = inspect.get_annotations(owner, eval_str=False)
        if name in vars(owner) or name in annotations:
            return _qualified_name(owner)
    return None


def _resolved_signature(callable_object: Callable[..., object]) -> tuple[str, dict[str, object], str | None]:
    signature = inspect.signature(callable_object)
    hints, hint_error = _type_hints(callable_object)
    parameters = [
        parameter.replace(annotation=hints.get(name, parameter.annotation))
        for name, parameter in signature.parameters.items()
    ]
    resolved = signature.replace(
        parameters=parameters,
        return_annotation=hints.get("return", signature.return_annotation),
    )
    return str(resolved), hints, hint_error


def _callable_details(callable_object: Callable[..., object]) -> dict[str, object]:
    signature, hints, hint_error = _resolved_signature(callable_object)
    details: dict[str, object] = {
        "signature": signature,
        "type_hints": {
            name: _annotation_name(annotation)
            for name, annotation in sorted(hints.items())
        },
        "is_coroutine": inspect.iscoroutinefunction(callable_object),
        "is_async_generator": inspect.isasyncgenfunction(callable_object),
    }
    if hint_error is not None:
        details["type_hints_error"] = hint_error
    return details


def _public_methods(cls: type[object]) -> dict[str, object]:
    methods: dict[str, object] = {}
    for name in sorted(dir(cls)):
        if name.startswith("_"):
            continue
        member = getattr(cls, name)
        if not inspect.isroutine(member):
            continue
        details = _callable_details(member)
        details["defined_on"] = _defined_on(cls, name)
        methods[name] = details
    return methods


def _model_fields(cls: type[object]) -> dict[str, object]:
    fields = getattr(cls, "model_fields", {})
    reflected: dict[str, object] = {}
    for name, field in sorted(fields.items()):
        field_details: dict[str, object] = {
            "type": _annotation_name(field.annotation),
            "required": field.is_required(),
        }
        if not field.is_required():
            field_details["default"] = repr(field.default)
        reflected[name] = field_details
    return reflected


def _model_output_attributes(cls: type[object]) -> dict[str, object]:
    hints, hint_error = _type_hints(cls)
    fields = _model_fields(cls)
    attributes: dict[str, object] = {}

    for name, annotation in hints.items():
        if name.startswith("_"):
            continue
        details: dict[str, object] = {
            "kind": "field" if name in fields else "annotation",
            "type": _annotation_name(annotation),
            "defined_on": _defined_on(cls, name),
        }
        if name in fields:
            details.update(fields[name])
        attributes[name] = details

    for owner in cls.__mro__:
        if not owner.__module__.startswith(gemini_webapi.__name__):
            continue
        for name, descriptor in vars(owner).items():
            if name.startswith("_") or not isinstance(descriptor, property):
                continue
            getter_hints, getter_error = _type_hints(descriptor.fget)
            details = {
                "kind": "property",
                "type": _annotation_name(getter_hints.get("return", _EMPTY)),
                "defined_on": _qualified_name(owner),
            }
            if getter_error is not None:
                details["type_hints_error"] = getter_error
            attributes[name] = details

    if hint_error is not None:
        attributes["__class_type_hints_error__"] = {"error": hint_error}
    return dict(sorted(attributes.items()))


def _exception_report() -> dict[str, object]:
    exception_module = importlib.import_module(f"{gemini_webapi.__name__}.exceptions")
    exception_classes: dict[str, type[BaseException]] = {}
    for namespace in (gemini_webapi, exception_module):
        for name in dir(namespace):
            if name.startswith("_"):
                continue
            candidate = getattr(namespace, name)
            if inspect.isclass(candidate) and issubclass(candidate, BaseException):
                exception_classes[candidate.__name__] = candidate

    return {
        name: {
            "qualified_name": _qualified_name(exception_class),
            "mro": [_qualified_name(base) for base in inspect.getmro(exception_class)],
        }
        for name, exception_class in sorted(exception_classes.items())
    }


def _runtime_type_expression(value: object) -> str:
    if not isinstance(value, list):
        return _qualified_name(type(value))

    item_types: list[str] = []
    for item in value:
        item_type = type(item)
        label = "None" if item_type is type(None) else _annotation_name(item_type)
        if label not in item_types:
            item_types.append(typing.cast(str, label))
    return f"list[{' | '.join(item_types) if item_types else 'unknown'}]"


def _chat_metadata_details(cls: type[object]) -> dict[str, object]:
    class ReflectionOnlyClient:
        pass

    descriptor = inspect.getattr_static(cls, "metadata")
    getter_hints, getter_error = _type_hints(descriptor.fget)
    init_hints, init_error = _type_hints(cls.__init__)
    sample = cls(ReflectionOnlyClient()).metadata
    element_types = list(dict.fromkeys(_qualified_name(type(item)) for item in sample))

    details: dict[str, object] = {
        "descriptor_type": _qualified_name(type(descriptor)),
        "getter_return_type_hint": _annotation_name(getter_hints.get("return", _EMPTY)),
        "constructor_parameter_type": _annotation_name(init_hints.get("metadata", _EMPTY)),
        "runtime_type": _qualified_name(type(sample)),
        "runtime_element_types": element_types,
        "runtime_length": len(sample),
        "actual_type": _runtime_type_expression(sample),
    }
    errors = [error for error in (getter_error, init_error) if error is not None]
    if errors:
        details["type_hints_errors"] = errors
    return details


def _class_signature(cls: type[object]) -> str:
    signature, _, _ = _resolved_signature(cls)
    return signature


def _image_report(cls: type[object]) -> dict[str, object]:
    save = getattr(cls, "save")
    return {
        "qualified_name": _qualified_name(cls),
        "mro": [_qualified_name(base) for base in inspect.getmro(cls)],
        "signature": _class_signature(cls),
        "attributes": _model_fields(cls),
        "save": {
            **_callable_details(save),
            "defined_on": _defined_on(cls, "save"),
        },
    }


def build_report() -> dict[str, object]:
    model_output = getattr(gemini_webapi, "ModelOutput")
    chat_session = getattr(gemini_webapi, "ChatSession")
    gemini_client = getattr(gemini_webapi, "GeminiClient")
    required_model_output_attributes = (
        "text",
        "text_delta",
        "thoughts",
        "images",
        "videos",
        "media",
        "candidates",
        "deep_research_document",
    )
    model_output_attributes = _model_output_attributes(model_output)

    return {
        "package": {
            "distribution": "gemini-webapi",
            "module": gemini_webapi.__name__,
            "version": importlib.metadata.version("gemini-webapi"),
        },
        "exceptions": _exception_report(),
        "model_output": {
            "qualified_name": _qualified_name(model_output),
            "signature": _class_signature(model_output),
            "attributes": model_output_attributes,
            "required_attributes": {
                name: {
                    "present": name in model_output_attributes or name in dir(model_output),
                    "visible_in_dir": name in dir(model_output),
                    "type": (
                        model_output_attributes.get(name, {}).get("type")
                        if isinstance(model_output_attributes.get(name), dict)
                        else None
                    ),
                }
                for name in required_model_output_attributes
            },
        },
        "chat_session": {
            "qualified_name": _qualified_name(chat_session),
            "signature": _class_signature(chat_session),
            "public_methods": _public_methods(chat_session),
            "metadata": _chat_metadata_details(chat_session),
        },
        "images": {
            name: _image_report(getattr(gemini_webapi, name))
            for name in ("Image", "WebImage", "GeneratedImage")
        },
        "gemini_client": {
            "qualified_name": _qualified_name(gemini_client),
            "public_methods": _public_methods(gemini_client),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="write JSON to stdout")
    args = parser.parse_args()
    report = build_report()

    if args.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        pprint.pp(report, sort_dicts=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
