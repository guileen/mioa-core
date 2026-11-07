// Copyright (C) 2023-2026  Lightpanda (Selecy SAS)
//
// Francis Bouvier <francis@lightpanda.io>
// Pierre Tachoire <pierre@lightpanda.io>
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as
// published by the Free Software Foundation, either version 3 of the
// License, or (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU Affero General Public License for more details.
//
// You should have received a copy of the GNU Affero General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>.

const Config = @import("../../Config.zig");
const js = @import("../js/js.zig");
const Execution = js.Execution;

const NavigatorUAData = @This();

_pad: bool = false,

const Brand = Config.HttpHeaders.Identity.Brand;

fn getBrands(_: *const NavigatorUAData, exec: *const Execution) []const Brand {
    return &exec.session.browser.http_client.getIdentity().brands;
}

fn getMobile(_: *const NavigatorUAData, exec: *const Execution) bool {
    return exec.session.browser.http_client.getIdentity().mobile;
}

fn getPlatform(_: *const NavigatorUAData, exec: *const Execution) []const u8 {
    return exec.session.browser.http_client.getIdentity().platform;
}

pub fn toJSON(self: *const NavigatorUAData, exec: *const Execution) struct {
    brands: []const Brand,
    mobile: bool,
    platform: []const u8,
} {
    return .{
        .brands = self.getBrands(exec),
        .mobile = self.getMobile(exec),
        .platform = self.getPlatform(exec),
    };
}

fn getHighEntropyValues(_: *const NavigatorUAData, hints: []const []const u8, exec: *const Execution) !js.Promise {
    // Extra fields are permitted; every field comes from the same declaration.
    _ = hints;
    const identity = exec.session.browser.http_client.getIdentity();
    return exec.js.local.?.resolvePromise(.{
        .brands = @as([]const Brand, &identity.brands),
        .mobile = identity.mobile,
        .platform = identity.platform,
        .architecture = identity.architecture,
        .bitness = identity.bitness,
        .model = "",
        .platformVersion = "",
        .uaFullVersion = identity.full_version_list[0].version,
        .fullVersionList = @as([]const Brand, &identity.full_version_list),
        .wow64 = identity.wow64,
        .formFactors = if (identity.mobile) [_][]const u8{"Mobile"} else [_][]const u8{"Desktop"},
    });
}

pub const JsApi = struct {
    pub const bridge = js.Bridge(NavigatorUAData);

    pub const Meta = struct {
        pub const name = "NavigatorUAData";
        pub var class_id: bridge.ClassId = undefined;
        pub const empty_with_no_proto = true;
    };

    pub const brands = bridge.accessor(NavigatorUAData.getBrands, null, .{});
    pub const mobile = bridge.accessor(NavigatorUAData.getMobile, null, .{});
    pub const platform = bridge.accessor(NavigatorUAData.getPlatform, null, .{});
    pub const toJSON = bridge.function(NavigatorUAData.toJSON, .{});
    pub const getHighEntropyValues = bridge.function(NavigatorUAData.getHighEntropyValues, .{});
};
