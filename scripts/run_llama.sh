#!/usr/bin/env bash
# ROCm 10 / gfx1151 用 llama-server ランチャー。引数はそのまま転送する。
set -euo pipefail

export ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0}"
LLAMA_BIN="${LLAMA_BIN:-$HOME/llama.cpp/build/bin/llama-server}"

[[ -d "$ROCM_PATH/lib" ]] || { echo "ROCm lib がありません: $ROCM_PATH/lib" >&2; exit 1; }
[[ -x "$LLAMA_BIN" ]] || { echo "llama-server がありません: $LLAMA_BIN" >&2; exit 1; }

# ネイティブ gfx1151 ビルドを使用。tmux / shell に残った override も除去する。
unset HSA_OVERRIDE_GFX_VERSION
# ROCm 10 (TheRock) のライブラリを優先。VOICEVOX 等の環境には適用しない。
export LD_LIBRARY_PATH="$ROCM_PATH/lib:$ROCM_PATH/lib/llvm/lib:$ROCM_PATH/lib/rocm_sysdeps/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$LLAMA_BIN" "$@"
