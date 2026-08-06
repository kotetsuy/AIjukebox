#!/usr/bin/env python3
"""VOICEVOX 音声合成ラッパー。

  uv run --no-sync scripts/voicevox_synth.py --list-speakers
  uv run --no-sync scripts/voicevox_synth.py --text "こんばんは" --out /tmp/a.wav

speaker_id はハードコードせず、起動時に /speakers から名前で解決する
(VOICEVOX のバージョン間でIDが変わるため)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from common import load_settings, resolve_path

TIMEOUT = 60.0

# 解決済み speaker_id のプロセス内キャッシュ。(name, style) -> id
_speaker_cache: dict[tuple[str, str], int] = {}


def list_speakers(base_url: str) -> list[dict]:
    r = httpx.get(f"{base_url}/speakers", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def resolve_speaker_id(base_url: str, speaker_name: str, style_name: str) -> int:
    """話者名 + スタイル名から speaker_id を引く。

    ID直書きだと VOICEVOX の更新で別の話者を喋らせてしまうため、必ずここを通す。
    """
    key = (speaker_name, style_name)
    if key in _speaker_cache:
        return _speaker_cache[key]

    speakers = list_speakers(base_url)
    for sp in speakers:
        if sp.get("name") != speaker_name:
            continue
        for style in sp.get("styles", []):
            if style.get("name") == style_name:
                _speaker_cache[key] = int(style["id"])
                return _speaker_cache[key]

    available = ", ".join(
        f"{sp.get('name')}/{st.get('name')}"
        for sp in speakers
        for st in sp.get("styles", [])
    )
    raise LookupError(
        f"話者が見つかりません: {speaker_name} / {style_name}\n利用可能: {available}"
    )


def synthesize(
    text: str, speaker_id: int, base_url: str, volume_scale: float = 1.0
) -> tuple[bytes, dict]:
    """テキストを合成して (wavバイト列, audio_query) を返す。

    audio_query も返すのは、表示系のリップシンクが accent_phrases から
    viseme を組み立てるため。キャッシュヒット時に再問い合わせしなくて済む。

    volume_scale は audio_query の volumeScale をそのまま差し替える。
    Liquidsoap 側のダッキング (radio.liq の p) ではなくここで上げるのは、
    音楽を下げるとリスナー側の体感音量ごと下がるため。
    """
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.post(
            f"{base_url}/audio_query", params={"text": text, "speaker": speaker_id}
        )
        r.raise_for_status()
        query = r.json()
        query["volumeScale"] = volume_scale

        r = client.post(
            f"{base_url}/synthesis",
            params={"speaker": speaker_id},
            json=query,
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.content, query


def synth_to_cache(text: str, track_hash: str, settings: dict | None = None) -> Path:
    """intro を cache/intros/{track_hash}.wav に合成する。既にあれば何もしない。

    リップシンク用に同名の .json (accent_phrases とテキスト) も併せて保存する。
    PREV でキャッシュを再利用するとき、音声だけあっても口が動かないため。
    """
    settings = settings or load_settings()
    cache_dir = resolve_path(settings["paths"]["intro_cache"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    wav_path = cache_dir / f"{track_hash}.wav"
    meta_path = cache_dir / f"{track_hash}.json"

    if wav_path.exists() and meta_path.exists():
        return wav_path

    vv = settings["voicevox"]
    speaker_id = resolve_speaker_id(
        vv["base_url"], vv["speaker_name"], vv["style_name"]
    )
    wav, query = synthesize(
        text, speaker_id, vv["base_url"], vv.get("volume_scale", 1.0)
    )

    # 生成途中で落ちても壊れたwavが残らないよう、書き切ってから差し替える
    tmp = wav_path.with_suffix(".wav.tmp")
    tmp.write_bytes(wav)
    tmp.replace(wav_path)
    meta_path.write_text(
        json.dumps(
            {
                "text": text,
                "speaker_id": speaker_id,
                "accent_phrases": query.get("accent_phrases", []),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return wav_path


def main() -> int:
    ap = argparse.ArgumentParser(description="VOICEVOX で音声合成する")
    ap.add_argument("--text", help="合成するテキスト('-' で標準入力)")
    ap.add_argument("--out", help="出力wavパス")
    ap.add_argument("--list-speakers", action="store_true", help="話者一覧を表示")
    args = ap.parse_args()

    settings = load_settings()
    vv = settings["voicevox"]

    try:
        if args.list_speakers:
            for sp in list_speakers(vv["base_url"]):
                styles = ", ".join(
                    f"{st['name']}(id={st['id']})" for st in sp.get("styles", [])
                )
                print(f"{sp['name']}: {styles}")
            return 0

        if not args.text:
            ap.error("--text か --list-speakers が必要です")
        text = sys.stdin.read().strip() if args.text == "-" else args.text

        speaker_id = resolve_speaker_id(
            vv["base_url"], vv["speaker_name"], vv["style_name"]
        )
        print(f"話者: {vv['speaker_name']} / {vv['style_name']} → id={speaker_id}")
        wav, _ = synthesize(
            text, speaker_id, vv["base_url"], vv.get("volume_scale", 1.0)
        )

        out = Path(args.out) if args.out else Path("intro.wav")
        out.write_bytes(wav)
        print(f"出力: {out} ({len(wav):,} bytes)")
    except httpx.HTTPError as e:
        print(f"VOICEVOX に接続できません ({vv['base_url']}): {e}", file=sys.stderr)
        return 1
    except LookupError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
