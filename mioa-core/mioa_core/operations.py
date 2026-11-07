"""Declared native/page operation subset and stable page IDs.

Derived from `cli/anti_trace/runner.py`: OPERATION_SPECS, ScriptError and
ScriptOperations only. The ATB trusted-script supervisor (`run_script`), its
socketpair protocol, budgets and process handling are intentionally excluded:
the headless service consumes page IDs and the declared operation table, not
subprocess script execution.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable
import uuid

MAX_ELEMENTS = 300
MAX_TEXT = 120

# Positional fields are listed here; page_id/locator/options are common fields.
OPERATION_SPECS = {
    "session.describe": (), "context.pages": (), "context.new_page": (),
    "context.cookies": ("urls",),
    "page.goto": ("url",), "page.evaluate": ("expression", "arg"),
    "page.title": (), "page.screenshot": (), "page.wait_for_load_state": ("state",),
    "page.wait_for_timeout": ("timeout",), "page.reload": (), "page.go_back": (),
    "page.bring_to_front": (), "page.close": (),
    "locator.click": (), "locator.fill": ("value",), "locator.press": ("key",),
    "locator.hover": (), "locator.check": (), "locator.uncheck": (),
    "locator.select_option": ("values",), "locator.wait_for": (),
    "locator.count": (), "locator.is_visible": (), "locator.is_enabled": (),
    "locator.inner_text": (), "locator.text_content": (), "locator.input_value": (),
    "locator.get_attribute": ("name",), "locator.all_text_contents": (),
    "mouse.click": ("x", "y"), "mouse.move": ("x", "y"),
    "mouse.down": (), "mouse.up": (), "mouse.wheel": ("delta_x", "delta_y"),
    "keyboard.press": ("key",), "keyboard.type": ("text",),
    "keyboard.insert_text": ("text",), "keyboard.down": ("key",),
    "keyboard.up": ("key",),
    "tools.axtree": ("max_elements", "max_text"),
}


class ScriptError(RuntimeError):
    def __init__(self, code: str, message: str, *, side_effects: str | None = None):
        super().__init__(message)
        self.code = code
        self.side_effects = side_effects


class ScriptOperations:
    """Dispatch the declared subset to real live objects; never bypass a lease.

    active_page is a zero-argument callable returning the engine's selected
    page. axtree is async ``(page, *, max_elements, max_text) -> dict`` and must
    supply source, coverage, frame_id and observation_id. The engine remains
    responsible for authentic node references, sensitivity masking, and leases.
    This object intentionally has no context/browser-close operation.
    """

    def __init__(self, context: Any, *, active_page: Callable[[], Any],
                 axtree: Callable[..., Awaitable[dict[str, Any]]] | None = None):
        self.context = context
        self.active_page = active_page
        self.axtree = axtree
        self._pages: dict[str, Any] = {}

    def page_id(self, page: Any) -> str:
        for key, existing in self._pages.items():
            if existing is page:
                return key
        key = "page-" + uuid.uuid4().hex
        self._pages[key] = page
        return key

    def page(self, page_id: str) -> Any:
        page = self._pages.get(page_id)
        if page is None or page.is_closed():
            raise ScriptError("stale_reference", f"page {page_id!r} is no longer available", side_effects="none")
        return page

    def _describe(self, page: Any) -> dict[str, Any]:
        return {"page_id": self.page_id(page), "url": page.url, "closed": page.is_closed()}

    async def execute(self, operation: str, args: dict[str, Any]) -> Any:
        if operation not in OPERATION_SPECS:
            raise ScriptError("capability_unavailable", f"script operation {operation!r} is not supported", side_effects="none")
        if operation == "session.describe":
            return {"page_id": self.page_id(self.active_page()),
                    "pages": [self._describe(page) for page in self.context.pages]}
        if operation == "context.pages":
            return [self._describe(page) for page in self.context.pages]
        if operation == "context.new_page":
            return self._describe(await self.context.new_page())
        if operation == "context.cookies":
            urls = args.get("urls")
            return await self.context.cookies() if urls is None else await self.context.cookies(urls)
        page = self.page(args["page_id"])
        if operation == "tools.axtree":
            if self.axtree is None:
                raise ScriptError("capability_unavailable", "bounded axtree is unavailable", side_effects="none")
            limit = args["max_elements"]
            text_limit = args["max_text"]
            if type(limit) is not int or not 1 <= limit <= MAX_ELEMENTS or type(text_limit) is not int or not 1 <= text_limit <= MAX_TEXT:
                raise ScriptError("invalid_config", "axtree bounds are outside the supported range", side_effects="none")
            value = await self.axtree(page, max_elements=limit, max_text=text_limit)
            required = {"frame_id", "observation_id", "source", "coverage", "elements"}
            if not isinstance(value, dict) or not required.issubset(value) or value["source"] not in {"dom", "accessibility"}:
                raise ScriptError("verification_failed", "axtree lacks authoritative source/coverage/version metadata", side_effects="none")
            if not isinstance(value["elements"], list) or len(value["elements"]) > limit:
                raise ScriptError("verification_failed", "axtree callback exceeded the element bound", side_effects="none")
            elements = []
            for element in value["elements"]:
                if not isinstance(element, dict):
                    raise ScriptError("verification_failed", "axtree elements must be objects", side_effects="none")
                row = dict(element)
                sensitive = str(row.get("type", "")).lower() == "password" or row.get("sensitive") is True
                for field in ("text", "value", "placeholder", "label", "name"):
                    if isinstance(row.get(field), str):
                        row[field] = "<redacted>" if sensitive and field in {"text", "value"} else row[field][:text_limit]
                elements.append(row)
            return {**value, "elements": elements, "page_id": args["page_id"]}
        family, method = operation.split(".", 1)
        target = page
        if family == "locator":
            target = self._locator(page, args["locator"])
        elif family in {"mouse", "keyboard"}:
            target = getattr(page, family)
        function = getattr(target, method, None)
        if function is None or not callable(function):
            raise ScriptError("capability_unavailable", f"runtime does not implement {operation}", side_effects="none")
        positional = [args[name] for name in OPERATION_SPECS[operation]]
        options = dict(args.get("options",{}))
        if operation == "page.screenshot":
            options.setdefault("caret","initial")
        value = function(*positional, **options)
        value = await value if inspect.isawaitable(value) else value
        # Playwright navigation responses are live objects, not JSON handles.
        if operation in {"page.goto", "page.reload", "page.go_back"}:
            if isinstance(value, dict):
                return value
            return None if value is None else {
                "url": value.url, "status": value.status,
                "ok": value.ok if hasattr(value, "ok") else 200 <= value.status < 400,
            }
        return value

    @staticmethod
    def _locator(page: Any, steps: list[dict[str, Any]]) -> Any:
        if not isinstance(steps, list) or not steps or len(steps) > 32:
            raise ScriptError("invalid_config", "locator requires 1..32 steps", side_effects="none")
        target = page
        for step in steps:
            kind = step.get("kind")
            if kind == "css":
                target = target.locator(step["selector"])
            elif kind == "role":
                options = {"exact": step["exact"]}
                if step.get("name") is not None:
                    options["name"] = step["name"]
                target = target.get_by_role(step["role"], **options)
            elif kind == "text":
                target = target.get_by_text(step["text"], exact=step["exact"])
            elif kind == "nth":
                target = target.nth(step["index"])
            else:
                raise ScriptError("invalid_config", f"unknown locator step {kind!r}", side_effects="none")
        return target
