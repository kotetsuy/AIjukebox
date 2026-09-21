#!/usr/bin/env bash
# AIjukebox 一括起動スクリプト。
#
# 起動順 (前のものが上がってから次に進む):
#   1. VOICEVOX ENGINE (docker)   :50021
#   2. llama-server (Qwen3.6)     :9931
#   3. Icecast                    :8100   ← config/icecast.xml (sudo不要)
#   4. Liquidsoap                 :1234 (telnet) → Icecast へ配信
#   5. program_service            :8765   ← 表示系 + WebSocket
#   6. Chrome で表示系を開く
#
# 各サービスは tmux セッション "aijukebox" の別ウィンドウで走る。
#   tmux attach -t aijukebox   (ログを見る)
#   ./stop_all.sh              (全部止める)
#
# ポートは config/settings.toml を唯一の情報源として読む。

set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

SESSION="aijukebox"

LLAMA_BIN="${LLAMA_BIN:-$HOME/llama.cpp/build/bin/llama-server}"
QWEN_MODEL="$HOME/AIassistant/qwen3.6/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
LLAMA_HOST="127.0.0.1"
LLAMA_PORT="9931"
LLAMA_CTX="8192"
LLAMA_NGL="99"

VOICEVOX_CONTAINER="voicevox_engine"
VOICEVOX_IMAGE="voicevox/voicevox_engine:cpu-ubuntu20.04-latest"

# Chrome / GUIアプリが PipeWire の pulse ソケットに繋がるようにする。
# Liquidsoap の output.pulseaudio もここを使う。
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PULSE_SERVER="${PULSE_SERVER:-unix:${XDG_RUNTIME_DIR}/pulse/native}"

# ---- helpers ------------------------------------------------------------

log()  { printf '\033[1;34m[launch]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[launch]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[launch]\033[0m %s\n' "$*" >&2; exit 1; }

# wait_http <name> <url> <timeout_sec>
wait_http() {
    local name="$1" url="$2" timeout="${3:-120}" start now
    start=$(date +%s)
    log "waiting for ${name} (${url}) ..."
    while true; do
        if curl -sf -o /dev/null -m 2 "$url"; then
            log "  ${name} is up"
            return 0
        fi
        now=$(date +%s)
        (( now - start > timeout )) && die "${name} が ${timeout}s で起動しませんでした"
        sleep 2
    done
}

# wait_tcp <name> <host> <port> <timeout_sec>
wait_tcp() {
    local name="$1" host="$2" port="$3" timeout="${4:-60}" start now
    start=$(date +%s)
    log "waiting for ${name} (${host}:${port}) ..."
    while true; do
        if (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null; then
            log "  ${name} is up"
            return 0
        fi
        now=$(date +%s)
        (( now - start > timeout )) && die "${name} が ${timeout}s で起動しませんでした"
        sleep 1
    done
}

# new_window <name> <command>
new_window() {
    tmux new-window -t "$SESSION" -n "$1"
    tmux send-keys -t "${SESSION}:$1" "$2" C-m
}

# ---- preflight ----------------------------------------------------------

command -v tmux       >/dev/null || die "tmux がありません"
command -v docker     >/dev/null || die "docker がありません"
command -v curl       >/dev/null || die "curl がありません"
command -v liquidsoap >/dev/null || die "liquidsoap がありません (apt install liquidsoap)"
command -v icecast2   >/dev/null || die "icecast2 がありません (apt install icecast2)"
command -v google-chrome >/dev/null || warn "google-chrome が見つかりません (自動オープンはスキップ)"

[[ -x "$LLAMA_BIN"   ]] || die "llama-server がありません: $LLAMA_BIN"
[[ -f "$QWEN_MODEL"  ]] || die "Qwen モデルがありません: $QWEN_MODEL"
[[ -x .venv/bin/python ]] || die ".venv がありません。先に 'uv sync' を実行してください"
[[ -f config/settings.toml ]] || die "config/settings.toml がありません"
[[ -f config/icecast.xml   ]] || die "config/icecast.xml がありません"
[[ -f liquidsoap/radio.liq ]] || die "liquidsoap/radio.liq がありません"
[[ -f vroid/dj.vrm ]] || warn "vroid/dj.vrm がありません (アバターが表示されません)"

# ポートは settings.toml から取る (スクリプトと設定の二重管理を避ける)。
# load_settings 経由なので settings.local.toml の上書きも効く。
eval "$(.venv/bin/python - <<'PY'
import sys
sys.path.insert(0, "scripts")
from common import load_settings
s = load_settings()
print(f'ICECAST_PORT={s["icecast"]["port"]}')
print(f'ICECAST_MOUNT={s["icecast"]["mount"]}')
print(f'TELNET_HOST={s["liquidsoap"]["telnet_host"]}')
print(f'TELNET_PORT={s["liquidsoap"]["telnet_port"]}')
print(f'WEB_PORT={s["program"]["websocket_port"]}')
PY
)"

BROWSER_URL="http://localhost:${WEB_PORT}/"

if [[ ! -s db/library.db ]]; then
    warn "db/library.db がありません。'uv run --no-sync scripts/scan_library.py' を先に実行してください"
fi

# 既存セッションは作り直す
if tmux has-session -t "$SESSION" 2>/dev/null; then
    log "既存の tmux セッション ${SESSION} を終了します"
    tmux kill-session -t "$SESSION"
fi

mkdir -p logs cache/intros

# ---- 1. VOICEVOX (docker) ----------------------------------------------

log "VOICEVOX コンテナ (${VOICEVOX_CONTAINER}) を起動します"
if docker ps --format '{{.Names}}' | grep -qx "$VOICEVOX_CONTAINER"; then
    log "  すでに running"
elif docker ps -a --format '{{.Names}}' | grep -qx "$VOICEVOX_CONTAINER"; then
    docker start "$VOICEVOX_CONTAINER" >/dev/null
else
    log "  コンテナが無いので新規作成します"
    docker run -d --name "$VOICEVOX_CONTAINER" --restart unless-stopped \
        -p 50021:50021 "$VOICEVOX_IMAGE" >/dev/null
fi

tmux new-session -d -s "$SESSION" -n voicevox \
    "docker logs -f --tail 50 ${VOICEVOX_CONTAINER}"

wait_http "VOICEVOX" "http://localhost:50021/version" 60

# ---- 2. llama-server ----------------------------------------------------

# tmux サーバーに残る環境に依存せず、選択した ROCm を子プロセスへ渡す。
printf -v llama_command '%q ' env "ROCM_PATH=${ROCM_PATH:-/opt/rocm}" \
    "HIP_VISIBLE_DEVICES=${HIP_VISIBLE_DEVICES:-0}" "LLAMA_BIN=$LLAMA_BIN" \
    bash "$PWD/scripts/run_llama.sh" -m "$QWEN_MODEL" \
    --host "$LLAMA_HOST" --port "$LLAMA_PORT" \
    -ngl "$LLAMA_NGL" -c "$LLAMA_CTX" -fit off
new_window "llama" "$llama_command"

# モデルロードに時間がかかるのでタイムアウト長め
wait_http "llama-server" "http://${LLAMA_HOST}:${LLAMA_PORT}/health" 600

# ---- 3. Icecast ---------------------------------------------------------
# /etc/icecast2 は使わない。config/icecast.xml は chroot / changeowner を
# 使わないのでユーザー権限のまま起動でき、ログもプロジェクト内に出る。

new_window "icecast" "icecast2 -c config/icecast.xml"
wait_http "Icecast" "http://localhost:${ICECAST_PORT}/status.xsl" 30

# ---- 4. Liquidsoap ------------------------------------------------------

new_window "liquidsoap" "liquidsoap liquidsoap/radio.liq"
wait_tcp "Liquidsoap telnet" "$TELNET_HOST" "$TELNET_PORT" 60

# ---- 5. program_service (番組進行 + 表示系) -----------------------------

new_window "program" "uv run --no-sync scripts/program_service.py"
wait_http "program_service" "$BROWSER_URL" 120

# ---- 6. Chrome ----------------------------------------------------------

if command -v google-chrome >/dev/null; then
    log "Chrome で ${BROWSER_URL} を開きます"
    google-chrome --new-window "$BROWSER_URL" >/dev/null 2>&1 &
    disown
else
    warn "手動で ${BROWSER_URL} を開いてください"
fi

# ---- done ---------------------------------------------------------------

LAN_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')

cat <<EOF

=========================================================================
 AIjukebox が起動しました。

   表示系      : ${BROWSER_URL}   ← Chrome で自動オープン
   ネットラジオ: http://${LAN_IP:-<このマシンのIP>}:${ICECAST_PORT}/${ICECAST_MOUNT}
   VOICEVOX    : http://localhost:50021/docs
   llama-server: http://${LLAMA_HOST}:${LLAMA_PORT}/health
   Liquidsoap  : telnet ${TELNET_HOST} ${TELNET_PORT}

 tmux:
   tmux attach -t ${SESSION}   (ログを見る / Ctrl-b d でデタッチ)
   ./stop_all.sh               (全部止める)
=========================================================================
EOF
