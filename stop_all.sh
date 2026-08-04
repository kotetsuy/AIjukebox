#!/usr/bin/env bash
# AIjukebox 停止スクリプト。
#
#   ./stop_all.sh                 → tmux セッション + VOICEVOX コンテナを停止
#   ./stop_all.sh --keep-voicevox → VOICEVOX は動かしたまま残す
#   ./stop_all.sh --keep-llama    → llama-server は残す (再ロードが重いので)
#
# Chrome は閉じない (ユーザの操作を奪わない)。必要なら手で閉じる。

set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

SESSION="aijukebox"
VOICEVOX_CONTAINER="voicevox_engine"
KEEP_VOICEVOX=0
KEEP_LLAMA=0

for arg in "$@"; do
    case "$arg" in
        --keep-voicevox) KEEP_VOICEVOX=1 ;;
        --keep-llama)    KEEP_LLAMA=1 ;;
        -h|--help)       sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

log()  { printf '\033[1;34m[stop]\033[0m %s\n' "$*"; }

# kill_proc <プロセス名(カンマ区切り)> <コマンドラインに含まれる文字列> <表示名>
#
# プロセス名(comm)と コマンドライン の両方が一致したものだけを止める。
# `pgrep -f パターン` だけだと、そのパターンを引数に含んでいるだけの無関係な
# シェルにも当たり、このスクリプトを起動した端末ごと落としてしまう。
kill_proc() {
    local comms="$1" pat="$2" name="$3" pids=() pid cmdline
    for comm in ${comms//,/ }; do
        for pid in $(pgrep -x "$comm" 2>/dev/null || true); do
            [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
            cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
            [[ "$cmdline" == *"$pat"* ]] && pids+=("$pid")
        done
    done
    (( ${#pids[@]} == 0 )) && return 0

    log "停止: ${name} (pid=${pids[*]})"
    kill "${pids[@]}" 2>/dev/null || true
    sleep 1
    for pid in "${pids[@]}"; do
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    done
    return 0
}

# ---- 1. tmux セッションを落とす ------------------------------------------

if tmux has-session -t "$SESSION" 2>/dev/null; then
    log "tmux セッション ${SESSION} を終了します"
    tmux kill-session -t "$SESSION"
else
    log "tmux セッション ${SESSION} は起動していません"
fi

# ---- 2. 取りこぼしプロセスを止める ---------------------------------------
# kill-session でウィンドウ内のプロセスも SIGHUP で落ちるはずだが、
# tmux を使わず手で起動した場合もあるので保険で拾う。

kill_proc "python,python3" "scripts/program_service.py" "program_service"
kill_proc "liquidsoap"     "radio.liq"                   "Liquidsoap"
kill_proc "icecast2"       "icecast.xml"                 "Icecast"

if (( KEEP_LLAMA == 0 )); then
    kill_proc "llama-server" "llama-server" "llama-server"
else
    log "llama-server は残します (--keep-llama)"
fi

# ---- 3. VOICEVOX docker --------------------------------------------------

if (( KEEP_VOICEVOX == 0 )); then
    if docker ps --format '{{.Names}}' | grep -qx "$VOICEVOX_CONTAINER"; then
        log "VOICEVOX コンテナ (${VOICEVOX_CONTAINER}) を停止します"
        docker stop "$VOICEVOX_CONTAINER" >/dev/null
    else
        log "VOICEVOX コンテナは既に停止しています"
    fi
else
    log "VOICEVOX は残します (--keep-voicevox)"
fi

log "停止完了"
