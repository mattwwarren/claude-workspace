WASM_TARGET := wasm32-wasip1
RELEASE_DIR := target/$(WASM_TARGET)/release
WASM_FILE := $(RELEASE_DIR)/cw_status.wasm
INSTALL_DIR := $(HOME)/.config/zellij/plugins

.PHONY: build install clean

build:
	cargo build --target $(WASM_TARGET) --release

install: build
	mkdir -p $(INSTALL_DIR)
	cp $(WASM_FILE) $(INSTALL_DIR)/cw_status.wasm

clean:
	cargo clean
