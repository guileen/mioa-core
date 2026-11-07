// Copyright (C) 2023-2026 Lightpanda (Selecy SAS)
// Francis Bouvier <francis@lightpanda.io>
// Pierre Tachoire <pierre@lightpanda.io>
// SPDX-License-Identifier: AGPL-3.0-or-later

const context2d = @import("context2d.zig");
const Canvas = @import("OffscreenCanvas.zig");
const Self = @This();
_canvas: *Canvas,
_drawing: context2d.Drawing,
const Impl = context2d.Context(Self, Canvas, "OffscreenCanvasRenderingContext2D");
pub const init = Impl.init;
pub const resize = Impl.resize;
pub const JsApi = Impl.JsApi;
