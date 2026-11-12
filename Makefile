# Single quality controller. Source builds require explicit NATIVE_INPUTS; no download, retry or implicit acceptance.
.DEFAULT_GOAL := test
PYTHON ?= mioa-headless/.venv/bin/python
ZIG ?= zig
BRIDGE_CONTRACT ?= runtime/.local/bridge-contract
LEVEL ?= 1
NATIVE_OUTPUT ?= runtime/.local/test-build
ifneq ($(strip $(NATIVE_INPUTS)),)
ifeq ($(filter 2 3,$(LEVEL)),)
$(error NATIVE_INPUTS requires LEVEL=2 or LEVEL=3)
endif
ifneq ($(strip $(BINARY)$(RECEIPT)),)
$(error NATIVE_INPUTS cannot be combined with BINARY or RECEIPT)
endif
BINARY := $(NATIVE_OUTPUT)/runtime/engine/zig-out/bin/lightpanda
RECEIPT := $(NATIVE_OUTPUT)/receipt.json
endif

.PHONY: test bridge-contract release clean
test: ## Quality controller: LEVEL=1 basic, 2 runtime, 3 delivery; HELP=1 explains
ifeq ($(HELP),1)
	@printf '%s\n' \
	  'make test: LEVEL=1 by default; mandatory source boundary -> unit regressions -> coverage/gap report.' \
	  'Failure stops acceptance, not implementation; next_action lists evidence, edit locations and repair steps.' \
	  'Missing OS capability: define mioa-os API contract, implement in mioa-os, migrate Core; no waiver or direct fallback.' \
	  'LEVEL=2: basic checks plus real runtime cases; provide BINARY=... [RECEIPT=...].' \
	  'LEVEL=3: complete candidate-bound delivery gate; BINARY=... RECEIPT=... required.' \
	  'FIX_ZIG_FORMAT=1 NATIVE_INPUTS=... formats the reviewed StyleManager file with the pinned Zig before building.' \
	  'NATIVE_INPUTS=/machine/inputs.json explicitly builds the engine before LEVEL=2/3 checks; NATIVE_OUTPUT=runtime/.local/test-build.' \
	  'Source builds use the pinned offline builder and automatically bind BINARY/RECEIPT; do not supply those together.' \
	  'MODULE="network storage" narrows levels 1/2; level 3 always covers every module.' \
	  'PLAN=1 lists the selected checks without execution; CHECKLIST=1 lists standards only.' \
	  'CHECKLIST=1 filters: SPEC=... MODULE=... PRIORITY=P0 REPORT=/path/report.toon.' \
	  'Options: PYTHON=... OUTPUT=/new/path TIMEOUT=seconds FORMAT=toon|json KEEP_GOING=1.' \
	  'KEEP_GOING=1 collects repair diagnostics even after boundary failure; FAIL and nonzero exit remain.' \
	  'Reviewed delivery exceptions only: ACCEPTANCE=/path/acceptance.json. No source/unit waiver.' \
	  'No level means whole-product certification; read scope and remaining gaps.'
else ifeq ($(CHECKLIST),1)
	@$(PYTHON) tests/standards.py $(if $(SPEC),--spec "$(SPEC)") $(if $(MODULE),--module "$(MODULE)") $(if $(PRIORITY),--priority "$(PRIORITY)") $(if $(REPORT),--report "$(REPORT)") $(if $(FORMAT),--format "$(FORMAT)")
else
ifneq ($(PLAN),1)
ifneq ($(strip $(FIX_ZIG_FORMAT)),)
ifneq ($(FIX_ZIG_FORMAT),1)
$(error FIX_ZIG_FORMAT accepts only 1)
endif
ifeq ($(strip $(NATIVE_INPUTS)),)
$(error FIX_ZIG_FORMAT=1 requires NATIVE_INPUTS)
endif
	@node runtime/build.mjs format --inputs "$(NATIVE_INPUTS)" --file runtime/engine/src/browser/StyleManager.zig
endif
endif
ifneq ($(strip $(NATIVE_INPUTS)),)
	@node runtime/build.mjs build --inputs "$(NATIVE_INPUTS)" --output-root "$(NATIVE_OUTPUT)" --target engine
endif
ifneq ($(PLAN),1)
	@$(MAKE) --no-print-directory bridge-contract
endif
	@$(PYTHON) tests/ci.py --level "$(LEVEL)" $(foreach module,$(MODULE),--module $(module)) $(if $(BINARY),--binary "$(BINARY)") $(if $(RECEIPT),--receipt "$(RECEIPT)") $(if $(OUTPUT),--output "$(OUTPUT)") $(if $(TIMEOUT),--timeout "$(TIMEOUT)") $(if $(FORMAT),--format "$(FORMAT)") $(if $(ACCEPTANCE),--acceptance "$(ACCEPTANCE)") $(if $(filter 1,$(PLAN)),--list) $(if $(filter 1,$(KEEP_GOING)),--keep-going)
endif

# Capability contract for the native C-ABI bridges: a bridge that reports
# success must actually parse, lay out, rasterize or transcode. Runs without
# V8 so a silent stub fails `make test` at LEVEL=1.
bridge-contract:
	@mkdir -p runtime/.local
	@$(ZIG) c++ -std=c++17 -O1 -I runtime/engine/src/c \
		runtime/engine/src/c/html_bridge.cpp \
		runtime/engine/src/c/render_bridge.cpp \
		tests/bridge_contract.cpp -o $(BRIDGE_CONTRACT)
	@$(BRIDGE_CONTRACT)

# Release build: engine only, pinned Zig toolchain, no Rust/cargo invocation.
RELEASE_INPUTS ?= runtime/.local/inputs.json
RELEASE_OUTPUT ?= runtime/.local/release

release: ## Build the engine with pinned Zig inputs (no Rust/cargo)
	@node runtime/build.mjs build --inputs "$(RELEASE_INPUTS)" --output-root "$(RELEASE_OUTPUT)" --target engine

clean: ## Remove build artifacts; pinned inputs under runtime/.local/input-cache are kept
	@rm -rf runtime/engine/zig-out runtime/engine/.zig-cache
	@rm -rf runtime/.local/build runtime/.local/release runtime/.local/test-build
	@rm -f $(BRIDGE_CONTRACT)
