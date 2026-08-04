#!/usr/bin/env python3
"""Liquidsoap を telnet 経由で制御する。

  uv run --no-sync scripts/liquidsoap_client.py status
  uv run --no-sync scripts/liquidsoap_client.py push-music "library/Queen-Bicycle Race.mp3"
  uv run --no-sync scripts/liquidsoap_client.py push-intro cache/intros/xxxx.wav
  uv run --no-sync scripts/liquidsoap_client.py skip
  uv run --no-sync scripts/liquidsoap_client.py pause / play

telnetlib は Python 3.13 で削除されたので素の socket で話す。
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

from common import load_settings, resolve_path

TIMEOUT = 5.0
# Liquidsoap の telnet は各応答を END 行で締める
END = b"END\r\n"


class LiquidsoapError(RuntimeError):
    pass


class LiquidsoapClient:
    """1コマンドごとに接続する薄いクライアント。

    常時接続にしないのは、Liquidsoap を再起動しても Python 側が壊れないため。
    コマンド頻度は曲の切り替わり程度なので接続コストは問題にならない。
    """

    def __init__(self, host: str, port: int, conf: dict | None = None):
        self.host = host
        self.port = port
        self.conf = conf or {}

    def command(self, cmd: str) -> str:
        try:
            with socket.create_connection((self.host, self.port), TIMEOUT) as sock:
                sock.settimeout(TIMEOUT)
                sock.sendall(cmd.encode("utf-8") + b"\n")
                buf = b""
                while END not in buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
        except OSError as e:
            raise LiquidsoapError(
                f"Liquidsoap に接続できません ({self.host}:{self.port}): {e}"
            ) from e
        return buf.replace(END, b"").decode("utf-8", "replace").strip()

    # ---- キュー投入 ------------------------------------------------------

    @staticmethod
    def to_uri(path: str | Path) -> str:
        """ローカルパスを push 用の文字列にする。

        Liquidsoap 2.4 は push の引数を行末までまるごと URI として扱うので、
        絶対パスをそのまま渡せばスペースも日本語も通る。逆に file:// +
        パーセントエンコードにすると %20 等が解釈されずリクエストが解決に
        失敗する(実測: 素のパスは Prepared、file:// は無言で消える)。
        """
        resolved = str(Path(path).resolve())
        if "\n" in resolved or "\r" in resolved:
            raise LiquidsoapError(f"改行を含むパスは push できません: {resolved!r}")
        return resolved

    def push(self, queue_id: str, path: str | Path) -> str:
        return self.command(f"{queue_id}.push {self.to_uri(path)}")

    def push_music(self, path: str | Path) -> str:
        return self.push(self.conf.get("music_queue", "music"), path)

    def push_intro(self, path: str | Path) -> str:
        return self.push(self.conf.get("dj_queue", "dj_intro"), path)

    def skip(self) -> str:
        return self.command(f"{self.conf.get('music_queue', 'music')}.skip")

    def flush_queues(self) -> list[str]:
        """両キューを空にして再生中のトラックも止める。

        Liquidsoap は program_service より長生きするので、サービスを再起動すると
        前回 push した曲がキューに残ったままになる。すると曲が終わってもキューが
        空にならず、終端検出(自動送り)が永久に発火しない。起動時に必ず呼ぶこと。
        """
        return [
            self.command(f"{q}.flush_and_skip")
            for q in (
                self.conf.get("music_queue", "music"),
                self.conf.get("dj_queue", "dj_intro"),
            )
        ]

    # ---- 再生制御 --------------------------------------------------------

    def _outputs(self) -> list[str]:
        return [
            self.conf.get("icecast_output", "radio_ice"),
            self.conf.get("local_output", "radio_local"),
        ]

    def set_paused(self, value: bool) -> str:
        """radio.liq の interactive.bool "paused" を切り替える。

        Liquidsoap 2.4 の output には .stop / .start が無いため、HANDOFF §4 の
        「両出力を同時に stop」は使えない。代わりに上流の switch 一箇所で
        ソースの pull を止める(radio.liq 参照)。切り替え点が1つなので、
        片方だけ止まって再生位置がずれる問題は起きない。
        """
        return self.command(f"var.set paused = {'true' if value else 'false'}")

    def pause(self) -> str:
        return self.set_paused(True)

    def play(self) -> str:
        return self.set_paused(False)

    def is_paused(self) -> bool:
        return self.command("var.get paused").strip().lower().endswith("true")

    # ---- 状態取得 --------------------------------------------------------

    def status(self) -> dict[str, str]:
        music = self.conf.get("music_queue", "music")
        dj = self.conf.get("dj_queue", "dj_intro")
        return {
            "music.queue": self.command(f"{music}.queue"),
            "dj.queue": self.command(f"{dj}.queue"),
            "paused": self.command("var.get paused"),
            "music.remaining": f"{self.remaining():.1f}",
        }

    def remaining(self) -> float:
        """音楽キューで鳴っている曲の残り秒数。何も鳴っていなければ -1。

        出力側の remaining は「いま出力しているトラック」を指すので、intro で
        ダッキング中は intro の残り時間を返してしまい曲の終端検出に使えない。
        radio.liq で生やした music.pos を使う。

        再生位置(経過秒)は DB の duration_sec からこの値を引いて求めること。
        Liquidsoap の source.elapsed はトラック開始からの実時間で、シークしても
        戻らないため位置として使えない。
        """
        raw = self.command(f"{self.conf.get('music_queue', 'music')}.pos")
        try:
            return float(raw.split()[0])
        except (ValueError, IndexError):
            return -1.0


def main() -> int:
    ap = argparse.ArgumentParser(description="Liquidsoap を telnet で制御する")
    ap.add_argument(
        "action",
        choices=[
            "status", "push-music", "push-intro", "skip",
            "pause", "play", "remaining", "raw",
        ],
    )
    ap.add_argument("arg", nargs="?", help="パス、または raw のときはコマンド文字列")
    args = ap.parse_args()

    settings = load_settings()
    conf = settings["liquidsoap"]
    client = LiquidsoapClient(conf["telnet_host"], conf["telnet_port"], conf)

    try:
        if args.action == "status":
            for k, v in client.status().items():
                print(f"{k:22} {v}")
        elif args.action == "push-music":
            if not args.arg:
                ap.error("push-music にはパスが必要です")
            print(client.push_music(resolve_path(args.arg)))
        elif args.action == "push-intro":
            if not args.arg:
                ap.error("push-intro にはパスが必要です")
            print(client.push_intro(resolve_path(args.arg)))
        elif args.action == "skip":
            print(client.skip())
        elif args.action == "pause":
            print(client.pause())
        elif args.action == "play":
            print(client.play())
        elif args.action == "remaining":
            print(f"{client.remaining():.1f} 秒")
        elif args.action == "raw":
            if not args.arg:
                ap.error("raw にはコマンド文字列が必要です")
            print(client.command(args.arg))
    except LiquidsoapError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
