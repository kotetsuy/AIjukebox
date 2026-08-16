#!/usr/bin/env python3
"""番組進行の中枢。選曲・状態機械・自動ループ・Liquidsoap制御。

  uv run --no-sync scripts/program_service.py

標準入力からコマンドを受け付ける(Phase 5 で WebSocket に置き換え/追加する)。
  next / prev / pause / play / status / quit

状態機械(HANDOFF §3):

  曲N 再生開始
    ├─ 即座に pick_next() で曲N+1 を確定
    ▼
  [GENERATING]   NEXT は無効
    ├─ キャッシュがあれば即 NEXT_READY
    └─ 無ければ Qwen → validate → VOICEVOX → キャッシュ保存
    ▼
  [NEXT_READY]   NEXT 有効
    ├─ NEXT押下 → 曲N+1 を投入して skip
    └─ 自然終了 → 曲N+1 を投入
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import deque
from pathlib import Path

import aiohttp
import httpx
from aiohttp import web

import artwork
import dj_prompt
import voicevox_synth
from common import (
    append_intro_log,
    connect_db,
    load_settings,
    recent_intros,
    resolve_path,
    stream_url,
    track_hash,
)
from liquidsoap_client import LiquidsoapClient, LiquidsoapError
from visemes import mora_to_visemes

PROJECT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_DIR / "web"
VRM_DIR = PROJECT_DIR / "vroid"
# 背景のフォールバック用画像置き場。無くてもよい(単色になる)
IMAGES_DIR = PROJECT_DIR / "images"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# three.js / three-vrm は web/libs に同梱している(AIassistant は参照しない)

GENERATING = "GENERATING"
NEXT_READY = "NEXT_READY"

POLL_INTERVAL = 0.5
# 残りがこれ以下になったら曲の終わりとみなして次を投入する。
# 0 まで待つと無音の隙間ができるため少し手前で動く。
END_THRESHOLD = 1.0
# 投入直後はリクエスト解決中で位置が取れず、終端と誤検出するので待つ
ADVANCE_COOLDOWN = 4.0


class TrackSelector:
    """選曲はLiquidsoapではなくPythonが持つ(DJ紹介と実再生曲の一致を保証)。"""

    def __init__(self, conn, exclude_n: int):
        self.conn = conn
        # 直近曲の除外リング。PREV用の履歴とは役割が違うので分ける
        self.exclude_history: deque[str] = deque(maxlen=exclude_n)
        self.play_stack: list[str] = []

    def all_filepaths(self) -> list[str]:
        return [r["filepath"] for r in self.conn.execute("SELECT filepath FROM tracks")]

    def pick_next(self) -> str:
        paths = self.all_filepaths()
        if not paths:
            raise RuntimeError("ライブラリが空です。先に scan_library.py を実行してください")

        candidates = [p for p in paths if p not in self.exclude_history]
        # ライブラリが除外数以下しかない場合の保険
        if not candidates:
            candidates = paths

        chosen = random.choice(candidates)
        self.exclude_history.append(chosen)
        return chosen

    def row(self, filepath: str):
        return self.conn.execute(
            "SELECT * FROM tracks WHERE filepath = ?", (filepath,)
        ).fetchone()


class ProgramService:
    def __init__(self, settings: dict):
        self.settings = settings
        self.conn = connect_db(resolve_path(settings["paths"]["db"]))
        self.log_path = resolve_path(settings["paths"]["log"])
        self.cache_dir = resolve_path(settings["paths"]["intro_cache"])
        self.artwork_dir = resolve_path(
            settings["paths"].get("artwork_cache", "cache/artwork")
        )

        prog = settings["program"]
        self.selector = TrackSelector(self.conn, prog["exclude_history"])
        self.prev_threshold = float(prog["prev_restart_threshold_sec"])

        ls = settings["liquidsoap"]
        self.client = LiquidsoapClient(ls["telnet_host"], ls["telnet_port"], ls)

        self.phase = GENERATING
        self.current: str | None = None
        self.next_track: str | None = None
        self.next_intro: Path | None = None
        self.paused = False
        self.recent: list[str] = recent_intros(
            self.log_path, settings["llm"]["recent_intros"]
        )

        self._loop_started = 0.0
        self._prepare_task: asyncio.Task | None = None
        self._advancing = asyncio.Lock()

        self.clients: set[web.WebSocketResponse] = set()

    # ---- 表示系への通知 --------------------------------------------------

    def emit(self, event: dict) -> None:
        # viseme 配列は数百要素あるのでコンソールには出さない(WSには全部送る)
        shown = event
        if event.get("event") == "intro":
            shown = {
                "event": "intro",
                "text": event["text"],
                "visemes": f"<{len(event['visemes'])}個>",
            }
        print(f"[{time.strftime('%H:%M:%S')}] {shown}", flush=True)
        if self.clients:
            asyncio.create_task(self.broadcast(event))

    async def broadcast(self, event: dict) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send_str(payload)
            except (ConnectionError, RuntimeError):
                self.clients.discard(ws)

    def snapshot(self) -> list[dict]:
        """接続してきた表示系に現在の状態を渡す。

        途中から繋いだブラウザでも曲名表示とNEXTボタンの活性が正しくなるように、
        now_playing / state / paused を作り直して送る。
        """
        vv = self.settings["voicevox"]
        events: list[dict] = [
            {"event": "state", "phase": self.phase},
            {"event": "paused", "value": self.paused},
            # VOICEVOX の利用規約で求められるクレジット表示
            {"event": "credit", "text": f"VOICEVOX:{vv['speaker_name']}"},
            # 同じ LAN のスマホから聴けるように、配信 URL を画面に出す。
            # 起動時に start_all.sh が表示するものと同じ URL になる。
            {"event": "stream_url", "url": stream_url(self.settings)},
        ]
        if self.current:
            row = self.selector.row(self.current)
            events.insert(
                0,
                {
                    "event": "now_playing",
                    "title": row["title"],
                    "artist": row["artist"],
                    "filepath": self.current,
                    # 再接続時も同じ背景に戻す(アートワークがあれば同一、
                    # images/ からのランダム選択は選び直しになる)
                    "background": self.background_url(self.current),
                },
            )
        if self.next_track:
            nxt = self.selector.row(self.next_track)
            events.append(
                {"event": "next_up", "title": nxt["title"], "artist": nxt["artist"]}
            )
        return events

    def _set_phase(self, phase: str) -> None:
        if self.phase != phase:
            self.phase = phase
            self.emit({"event": "state", "phase": phase})

    # ---- intro の用意 ----------------------------------------------------

    def _intro_paths(self, filepath: str) -> tuple[Path, Path]:
        h = track_hash(filepath)
        return self.cache_dir / f"{h}.wav", self.cache_dir / f"{h}.json"

    def build_intro(self, row) -> Path:
        """intro.wav を用意する。キャッシュがあれば Qwen も VOICEVOX も呼ばない。

        ブロッキングIOなので必ず asyncio.to_thread 経由で呼ぶこと。sqlite3 の
        接続はスレッドを跨げないため、row は呼び出し側(メインスレッド)で
        取得して渡すこと。ここでDBを引いてはいけない。
        """
        filepath = row["filepath"]
        wav, meta = self._intro_paths(filepath)
        if wav.exists() and meta.exists():
            return wav

        text = dj_prompt.generate_intro_text(row, self.settings, self.recent)
        append_intro_log(self.log_path, filepath, row["title"], text)
        self.recent = (self.recent + [text])[-self.settings["llm"]["recent_intros"] :]
        return voicevox_synth.synth_to_cache(text, track_hash(filepath), self.settings)

    # ---- 背景 ------------------------------------------------------------

    def background_url(self, filepath: str) -> str | None:
        """表示系の背景に使う画像のURL。

        優先順位は 曲のアートワーク → images/ からランダム → None(単色 #12121c)。
        ブロッキングIOなので to_thread 経由で呼ぶこと。
        """
        art = artwork.ensure_cached(filepath, self.artwork_dir)
        if art is not None:
            return f"/artwork/{art.name}"

        if IMAGES_DIR.is_dir():
            candidates = [
                p for p in IMAGES_DIR.iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
            ]
            if candidates:
                return f"/images/{random.choice(candidates).name}"
        return None

    def emit_intro(self, filepath: str) -> None:
        """intro を Liquidsoap に流したので、表示系にリップシンク用データを送る。

        音声はブラウザでは鳴らさない(Liquidsoap がミックスして出している)。
        表示系は viseme のタイムラインだけを受け取って口を動かす。
        キャッシュした {track_hash}.json の accent_phrases を使うので、
        PREV でキャッシュを再利用したときも口が動く。
        """
        _, meta_path = self._intro_paths(filepath)
        if not meta_path.exists():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        visemes, vtimes, vdurations = mora_to_visemes(meta.get("accent_phrases", []))
        self.emit(
            {
                "event": "intro",
                "text": meta.get("text", ""),
                "visemes": visemes,
                "vtimes": vtimes,
                "vdurations": vdurations,
                # push から実際に音が出るまでのズレ。環境に合わせて調整する
                "delay_ms": self.settings["program"].get("intro_lipsync_delay_ms", 300),
            }
        )

    async def _prepare_next(self) -> None:
        """現在曲の再生開始と同時に次曲を確定し、introを先読み生成する。

        GENERATING への遷移は呼び出し元(_advance)が同期的に済ませている。
        """
        try:
            self.next_track = self.selector.pick_next()
            row = self.selector.row(self.next_track)
            self.emit(
                {"event": "next_up", "title": row["title"], "artist": row["artist"]}
            )
            self.next_intro = await asyncio.to_thread(self.build_intro, row)
        except (httpx.HTTPError, RuntimeError) as e:
            # 生成に失敗しても番組は止めない。introなしで次に進めるようにする
            self.emit({"event": "error", "where": "prepare_next", "detail": str(e)})
            self.next_intro = None
        self._set_phase(NEXT_READY)

    # ---- 曲の投入 --------------------------------------------------------

    async def _advance(
        self, filepath: str, intro: Path | None, *, skip_current: bool, push_stack: bool
    ) -> None:
        """次の曲を投入する。

        音楽を先に push してから skip する。逆順だと queue が空の瞬間ができて
        無音の隙間が入る。
        """
        row = self.selector.row(filepath)
        await asyncio.to_thread(self.client.push_music, filepath)
        if skip_current:
            await asyncio.to_thread(self.client.skip)
        if intro is not None:
            await asyncio.to_thread(self.client.push_intro, intro)

        if push_stack and self.current:
            self.selector.play_stack.append(self.current)
        self.current = filepath
        self._loop_started = asyncio.get_running_loop().time()

        background = await asyncio.to_thread(self.background_url, filepath)
        self.emit(
            {
                "event": "now_playing",
                "title": row["title"],
                "artist": row["artist"],
                "filepath": filepath,
                "background": background,
            }
        )
        # 曲名を先に出してから字幕とリップシンクを流す(逆だと紹介より先に
        # 前の曲名が出たままになる)
        if intro is not None:
            self.emit_intro(filepath)

        # 次曲情報はここで無効化し、GENERATING も「同期的に」落とす。
        # create_task の中で落とすと、タスクが走り出す前に届いた NEXT が
        # まだ NEXT_READY を見てしまい、連打がすり抜ける。
        self.next_track = None
        self.next_intro = None
        self._set_phase(GENERATING)

        # 曲の開始と同時に次曲を確定して先読み生成に入る
        self._prepare_task = asyncio.create_task(self._prepare_next())

    # ---- コマンド --------------------------------------------------------

    async def cmd_next(self) -> str:
        # 判定はロックの中で行う。外に出すと、先行する NEXT が _advance の
        # 途中(まだ GENERATING を落とす前)のときに連打がすり抜ける。
        async with self._advancing:
            if self.phase != NEXT_READY or self.next_track is None:
                return "生成中です(NEXTは無効)"
            await self._advance(
                self.next_track, self.next_intro, skip_current=True, push_stack=True
            )
        return "next"

    async def elapsed(self) -> float:
        """再生位置(秒)。DBの尺から残り時間を引いて求める。

        Liquidsoap の source.elapsed はトラック開始からの実時間なので、
        頭出し後も増え続けて位置として使えない。
        """
        if not self.current:
            return 0.0
        row = self.selector.row(self.current)
        duration = row["duration_sec"]
        rem = await asyncio.to_thread(self.client.remaining)
        if duration is None or rem < 0:
            return 0.0
        return max(0.0, duration - rem)

    async def restart_current(self) -> None:
        """現在曲を頭から鳴らし直す。

        Liquidsoap は後方シークができない(music.seek は -30 を受け付けて
        -30.0 を返すが、実際には位置が動かない)。同じファイルを push して
        skip することで頭出しする。intro は鳴らし直さない。
        """
        async with self._advancing:
            await asyncio.to_thread(self.client.push_music, self.current)
            await asyncio.to_thread(self.client.skip)
            self._loop_started = asyncio.get_running_loop().time()

    async def cmd_prev(self) -> str:
        elapsed = await self.elapsed()

        # 3秒ルール: 再生位置が3秒以降なら現在曲を頭出しする
        if elapsed >= self.prev_threshold:
            await self.restart_current()
            self.emit({"event": "restart", "elapsed": round(elapsed, 1)})
            return f"頭出し({elapsed:.1f}秒経過)"

        if not self.selector.play_stack:
            return "これ以上戻れません"

        async with self._advancing:
            target = self.selector.play_stack.pop()
            intro, meta = self._intro_paths(target)
            # 前曲のintroは必ずキャッシュ済みなので即応する
            if not (intro.exists() and meta.exists()):
                intro = await asyncio.to_thread(
                    self.build_intro, self.selector.row(target)
                )
            # PREVで戻った曲は exclude_history に再登録しない(HANDOFF §2)
            await self._advance(target, intro, skip_current=True, push_stack=False)
        return "prev"

    async def cmd_pause(self) -> str:
        await asyncio.to_thread(self.client.pause)
        self.paused = True
        self.emit({"event": "paused", "value": True})
        return "pause"

    async def cmd_play(self) -> str:
        await asyncio.to_thread(self.client.play)
        self.paused = False
        self.emit({"event": "paused", "value": False})
        return "play"

    async def cmd_status(self) -> str:
        rem = await asyncio.to_thread(self.client.remaining)
        elapsed = await self.elapsed()
        cur = self.selector.row(self.current) if self.current else None
        nxt = self.selector.row(self.next_track) if self.next_track else None
        return (
            f"phase={self.phase} paused={self.paused}\n"
            f"  再生中: {cur['artist'] + ' - ' + cur['title'] if cur else '-'}"
            f"  (経過 {elapsed:.0f}s / 残り {rem:.0f}s)\n"
            f"  次曲  : {nxt['artist'] + ' - ' + nxt['title'] if nxt else '-'}"
            f"  intro={'あり' if self.next_intro else 'なし'}\n"
            f"  履歴  : {len(self.selector.play_stack)} 曲 / 除外リング "
            f"{len(self.selector.exclude_history)} 曲"
        )

    # ---- 自動ループ ------------------------------------------------------

    async def start(self) -> None:
        """最初の1曲を用意して投入する。"""
        # 前回実行時の押し込みがキューに残っていると、曲が終わってもキューが
        # 空にならず自動送りが発火しなくなるので、必ず掃除してから始める
        await asyncio.to_thread(self.client.flush_queues)
        first = self.selector.pick_next()
        self.emit({"event": "state", "phase": GENERATING})
        intro = await asyncio.to_thread(self.build_intro, self.selector.row(first))
        await self._advance(first, intro, skip_current=False, push_stack=False)

    async def poll_loop(self) -> None:
        """曲の終端を監視して次曲を投入する。"""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            if self.paused or self._advancing.locked():
                continue
            # 投入直後はリクエスト解決中で位置が取れないため見送る
            if loop.time() - self._loop_started < ADVANCE_COOLDOWN:
                continue

            try:
                rem = await asyncio.to_thread(self.client.remaining)
            except LiquidsoapError as e:
                self.emit({"event": "error", "where": "poll", "detail": str(e)})
                await asyncio.sleep(2)
                continue

            ended = rem < 0 or rem <= END_THRESHOLD
            if not ended:
                continue
            if self.phase != NEXT_READY or self.next_track is None:
                # 生成が間に合っていない。次のポーリングで拾う
                continue

            async with self._advancing:
                await self._advance(
                    self.next_track, self.next_intro, skip_current=False, push_stack=True
                )

    # ---- WebSocket / 表示系サーバー --------------------------------------

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.clients.add(ws)
        for event in self.snapshot():
            await ws.send_str(json.dumps(event, ensure_ascii=False))

        handlers = {
            "next": self.cmd_next,
            "prev": self.cmd_prev,
            "pause": self.cmd_pause,
            "play": self.cmd_play,
        }
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    cmd = json.loads(msg.data).get("cmd")
                except json.JSONDecodeError:
                    continue
                handler = handlers.get(cmd)
                if handler is None:
                    await ws.send_str(
                        json.dumps({"event": "error", "detail": f"不明なcmd: {cmd}"})
                    )
                    continue
                result = await handler()
                print(f"[{time.strftime('%H:%M:%S')}] ws> {cmd}: {result}", flush=True)
        finally:
            self.clients.discard(ws)
        return ws

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_get(
            "/", lambda _: web.FileResponse(WEB_DIR / "index.html")
        )
        app.router.add_static("/vrm/", VRM_DIR)
        self.artwork_dir.mkdir(parents=True, exist_ok=True)
        app.router.add_static("/artwork/", self.artwork_dir)
        # images/ は無くてもよい。その場合は背景が単色になる
        if IMAGES_DIR.is_dir():
            app.router.add_static("/images/", IMAGES_DIR)
        # /libs/... は web/libs/... に解決される
        app.router.add_static("/", WEB_DIR)
        return app

    async def serve(self) -> None:
        prog = self.settings["program"]
        runner = web.AppRunner(self.build_app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, prog["websocket_host"], prog["websocket_port"])
        await site.start()
        host = prog["websocket_host"]
        shown = "localhost" if host in ("0.0.0.0", "") else host
        print(f"表示系: http://{shown}:{prog['websocket_port']}/", flush=True)

    async def stdin_loop(self) -> None:
        """標準入力からのコマンド。Phase 5 で WebSocket を並列に足す。"""
        reader = asyncio.StreamReader()
        await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        handlers = {
            "next": self.cmd_next,
            "prev": self.cmd_prev,
            "pause": self.cmd_pause,
            "play": self.cmd_play,
            "status": self.cmd_status,
        }
        while True:
            line = await reader.readline()
            if not line:  # パイプ入力が閉じても番組は流し続ける
                return
            cmd = line.decode().strip().lower()
            if not cmd:
                continue
            if cmd in ("quit", "exit"):
                raise KeyboardInterrupt
            handler = handlers.get(cmd)
            if handler is None:
                print(f"不明なコマンド: {cmd} ({'/'.join(handlers)}/quit)", flush=True)
                continue
            print(f"[{time.strftime('%H:%M:%S')}] > {cmd}: {await handler()}", flush=True)


async def amain(args) -> int:
    settings = load_settings()
    service = ProgramService(settings)

    try:
        await asyncio.to_thread(service.client.command, "uptime")
    except LiquidsoapError as e:
        print(e, file=sys.stderr)
        print("先に liquidsoap liquidsoap/radio.liq を起動してください", file=sys.stderr)
        return 1

    await service.serve()
    await service.start()
    tasks = [asyncio.create_task(service.poll_loop())]
    if not args.no_stdin:
        tasks.append(asyncio.create_task(service.stdin_loop()))
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="AIjukebox 番組進行サービス")
    ap.add_argument("--no-stdin", action="store_true", help="標準入力コマンドを使わない")
    args = ap.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\n終了します。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
