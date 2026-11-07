"""Opaque document/view/node references for the owned native runtime.

Derived from `cli/anti_trace/actions.py` (RefStore slice). The only source
change removes the unused `playwright.async_api.Page` annotation import, so the
core runtime has no ATB or Playwright package dependency. Runtime behavior and
error codes are unchanged.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

MAX_ELEMENTS = 300
MAX_TEXT = 120

SNAPSHOT_JS = r"""options => {
  if (typeof MutationObserver !== 'function') throw new Error('capability_unavailable: MutationObserver');
  const state = {document, epoch: 0, nodes: [], rows: [], total: 0,
    title: document.title, url: location.href,
    view: [scrollX, scrollY, innerWidth, innerHeight], roots: 0};
  state.observer = new MutationObserver(records => { state.epoch += records.length; });
  const roots = [document];
  for (let i = 0; i < roots.length; i++) {
    const root = roots[i];
    state.observer.observe(root, {subtree:true,childList:true,attributes:true,characterData:true});
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
  }
  state.roots = roots.length;
  if (!options.limit) return state;
  const selector = 'a,button,input,textarea,select,summary,[role="button"],[role="link"],'
    + '[role="checkbox"],[role="radio"],[role="tab"],[role="menuitem"],[role="switch"],'
    + '[role="textbox"],[role="combobox"],[contenteditable=""],[contenteditable="true"],'
    + '[tabindex]:not([tabindex="-1"])';
  const entries = [];
  for (const root of roots) for (const el of root.querySelectorAll(selector)) {
    const style = getComputedStyle(el), rect = el.getBoundingClientRect();
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0'
        || rect.width <= 0 || rect.height <= 0) continue;
    const sensitive = (el.type || '').toLowerCase() === 'password';
    const text = sensitive ? '<redacted>' : String(el.getAttribute('aria-label') || el.innerText
      || el.value || el.placeholder || el.getAttribute('title') || '').trim().replace(/\s+/g,' ');
    entries.push({el, in_viewport: rect.bottom > 0 && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth,
      row: {tag:el.tagName.toLowerCase(),role:el.getAttribute('role') || el.tagName.toLowerCase(),
        type:el.getAttribute('type'),name:el.getAttribute('name'),text:text.slice(0,options.text),
        placeholder:(el.getAttribute('placeholder') || '').slice(0,options.text),
        value:sensitive ? '<redacted>' : (typeof el.value === 'string' ? el.value.slice(0,options.text) : undefined),
        disabled:!!el.disabled,checked:typeof el.checked === 'boolean' ? el.checked : undefined,sensitive}});
  }
  entries.sort((a,b) => Number(b.in_viewport)-Number(a.in_viewport));
  state.total = entries.length;
  for (const entry of entries.slice(0,options.limit)) {
    state.nodes.push(entry.el);
    state.rows.push({...entry.row,in_viewport:entry.in_viewport});
  }
  return state;
}"""

_CHECK_JS = """(s, checkMutations) => {
  if (checkMutations) s.epoch += s.observer.takeRecords().length;
  return document === s.document && (!checkMutations || s.epoch === 0)
    && s.view.every((value,index) => value === [scrollX,scrollY,innerWidth,innerHeight][index]);
}"""


class ActionError(RuntimeError):
    def __init__(self, message: str, *, code: str = "invalid_config"):
        super().__init__(message)
        self.code, self.side_effects = code, "none"


class RefStore:
    """Opaque IDs bound to real documents and original nodes, never re-queries."""
    def __init__(self, page_id: Callable[[Any], str] | None = None,
                 versions: Callable[[], Any] | None = None):
        self._pages: dict[Any, str] = {}
        self._frames: dict[Any, str] = {}
        self.page_id = page_id or self._page_id
        self.versions = versions or (lambda: None)
        self.references: dict[str, tuple[dict[str, Any], int]] = {}
        self.snapshot: dict[str, Any] | None = None
        self.visual: dict[str, Any] | None = None
        self.next_ref = 1

    def _page_id(self, page: Any) -> str:
        return self._pages.setdefault(page, "page-" + uuid.uuid4().hex)

    def frame_id(self, frame: Any) -> str:
        actual = getattr(frame, "frame_id", None)
        return actual or self._frames.setdefault(frame, "frame-" + uuid.uuid4().hex)

    async def _release(self, record: dict[str, Any] | None) -> None:
        if record:
            for child in record.get("children",[]):
                await self._release(child)
            for node in record.get("node_handles", {}).values():
                try:
                    await node.dispose()
                except Exception:
                    pass
            try:
                await record["handle"].evaluate("s => s.observer.disconnect()")
            except Exception:
                pass  # A navigated/destroyed realm has already released its observer.
            try:
                await record["handle"].dispose()
            except Exception:
                pass

    async def clear(self) -> None:
        self.references.clear()
        await self._release(self.snapshot)
        await self._release(self.visual)
        self.snapshot = self.visual = None

    async def _capture(self, page: Any, frame: Any, limit: int, text: int) -> dict[str, Any]:
        handle = await frame.evaluate_handle(SNAPSHOT_JS, {"limit":limit,"text":text})
        return {"page":page,"frame":frame,"handle":handle,"page_id":self.page_id(page),
                "frame_id":self.frame_id(frame),"observation_id":"observation-"+uuid.uuid4().hex,
                "versions":self.versions(),"node_handles":{},"children":[]}

    async def capture(self, page: Any, *, max_elements: int = MAX_ELEMENTS,
                      max_text: int = MAX_TEXT, frame: str | None = None) -> dict[str, Any]:
        if not 1 <= max_elements <= MAX_ELEMENTS or not 1 <= max_text <= MAX_TEXT:
            raise ActionError("axtree bounds outside supported range")
        target = getattr(page, "main_frame", page)
        if frame:
            element = await page.query_selector(frame)
            if element is None:
                raise ActionError("frame selector did not match", code="stale_reference")
            try:
                target = await element.content_frame()
            finally:
                await element.dispose()
            if target is None:
                raise ActionError("selected element has no frame", code="stale_reference")
        record = await self._capture(page, target, max_elements, max_text)
        try:
            data = await record["handle"].evaluate("""s => ({title:s.title,url:s.url,elements:s.rows,
              total:s.total,truncated:s.total>s.rows.length,shadow_roots:s.roots-1,
              frames:document.querySelectorAll('iframe,frame').length})""")
            await self.validate(record,page)
        except BaseException:
            await self._release(record)
            raise
        await self._release(self.snapshot)
        self.snapshot = record
        self.references.clear()
        for index, row in enumerate(data["elements"]):
            ref = "@" + str(self.next_ref)
            self.next_ref += 1
            self.references[ref] = (record, index)
            row.update(ref=ref,frame_id=record["frame_id"])
        return {"status":"ok","action":"axtree",**data,
                **{key:record[key] for key in ("page_id","frame_id","observation_id")},
                "source":"dom","coverage":{"selected_frame":True,"subframes":"explicit --frame selection",
                  "open_shadow_dom":True,"closed_shadow_dom":False,"accessibility_tree":False}}

    async def validate(self, record: dict[str, Any] | None, page: Any, *, check_mutations: bool = True) -> None:
        """Keep realm/view/lease checks; only native SHM may defer mutations to its atomic guard."""
        if not record or record["page"] is not page or page.is_closed() or record["versions"] != self.versions():
            raise ActionError("page or control ownership changed; observe again", code="stale_reference")
        try:
            valid = await record["handle"].evaluate(_CHECK_JS, check_mutations)
        except Exception as exc:
            raise ActionError("document or frame was replaced; observe again", code="stale_reference") from exc
        if not valid:
            raise ActionError("DOM, scroll or viewport changed; observe again", code="stale_reference")
        for child in record.get("children",[]):
            await self.validate(child,page,check_mutations=check_mutations)

    async def resolve(self, page: Any, reference: str) -> Any:
        item = self.references.get(reference)
        if item is None:
            raise ActionError("unknown or expired reference; run axtree again", code="stale_reference")
        record, index = item
        await self.validate(record, page)
        element = record["node_handles"].get(index)
        if element is None:
            node = await record["handle"].evaluate_handle("(s,index) => s.nodes[index]", index)
            element = node.as_element()
            if element is not None:
                record["node_handles"][index] = element
        if element is None:
            await node.dispose()
            raise ActionError("reference no longer resolves to an original node", code="stale_reference")
        if not await element.evaluate("el => el.isConnected && el.ownerDocument === document"):
            await element.dispose()
            raise ActionError("original node was detached", code="stale_reference")
        return element

    async def viewport(self, page: Any, *, raw_destination: tuple[str, int] | None = None) -> dict[str, Any]:
        # Native SHM pixels and semantics are guarded atomically at dispatch.
        # Observer delivery is not their authority; document/view/control still are.
        check_mutations = raw_destination is None
        record = self.visual
        try:
            await self.validate(record,page,check_mutations=check_mutations)
        except ActionError:
            record = await self._capture(page,getattr(page,"main_frame",page),0,0)
            try:
                documents = await page.frame_documents() if hasattr(page,"frame_documents") else page.frames
                for frame in documents:
                    if frame is not record["frame"]:
                        record["children"].append(await self._capture(page,frame,0,0))
            except BaseException:
                await self._release(record)
                raise
        try:
            if raw_destination is None:
                pixels = {"png": await page.screenshot(full_page=False, timeout=20000, caret="initial")}
            else:
                pixels = await page.capture_raw_frame(*raw_destination)
            await self.validate(record,page,check_mutations=check_mutations)
            dimensions = await record["handle"].evaluate("s => ({width:s.view[2],height:s.view[3]})")
        except BaseException:
            if record is not self.visual:
                await self._release(record)
            raise
        if record is not self.visual:
            await self._release(self.visual)
            self.visual = record
        return {**pixels,"viewport":dimensions,
                **{key:record[key] for key in ("page_id","frame_id","observation_id")}}
