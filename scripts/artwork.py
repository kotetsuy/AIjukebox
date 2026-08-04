"""音楽ファイルに埋め込まれたアートワークを取り出してキャッシュする。

表示系の背景に使う。フォーマットごとに格納場所がまったく違うので、
それぞれ個別に取りに行く。

  FLAC / Ogg  : Picture ブロック
  MP4 / M4A   : covr アトム
  MP3 ほか    : ID3 の APIC フレーム
"""

from __future__ import annotations

import base64
from pathlib import Path

import mutagen

from common import track_hash

# MIME → 拡張子。判別できないものは jpg 扱いにする(実害がない)
_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# アートワークが無いことを覚えておく印。毎回タグを読み直さないため
# (Queen の APIC は 662KB あり、曲が変わるたびに読むのは無駄)
NONE_MARKER = ".none"


def _from_flac_pictures(f) -> tuple[bytes, str] | None:
    pictures = getattr(f, "pictures", None)
    if not pictures:
        return None
    pic = pictures[0]
    return pic.data, _EXT.get(pic.mime, ".jpg")


def _from_mp4(tags) -> tuple[bytes, str] | None:
    covers = tags.get("covr")
    if not covers:
        return None
    cover = covers[0]
    # MP4Cover.imageformat: 13=JPEG, 14=PNG
    ext = ".png" if getattr(cover, "imageformat", 13) == 14 else ".jpg"
    return bytes(cover), ext


def _from_id3(tags) -> tuple[bytes, str] | None:
    # APIC はキーが "APIC:", "APIC:cover" のように可変
    keys = [k for k in tags.keys() if k.startswith("APIC")]
    if not keys:
        return None
    frame = tags[keys[0]]
    return frame.data, _EXT.get(getattr(frame, "mime", ""), ".jpg")


def _from_vorbis(tags) -> tuple[bytes, str] | None:
    """Ogg は Picture ブロックを base64 にしてコメントへ入れる。"""
    from mutagen.flac import Picture

    blocks = tags.get("metadata_block_picture")
    if not blocks:
        return None
    try:
        pic = Picture(base64.b64decode(blocks[0]))
    except Exception:
        return None
    return pic.data, _EXT.get(pic.mime, ".jpg")


def extract(filepath: str | Path) -> tuple[bytes, str] | None:
    """(画像バイト列, 拡張子) を返す。埋め込みが無ければ None。"""
    try:
        f = mutagen.File(filepath)
    except Exception:
        return None
    if f is None:
        return None

    result = _from_flac_pictures(f)
    if result:
        return result

    tags = f.tags
    if tags is None:
        return None

    for getter in (_from_mp4, _from_id3, _from_vorbis):
        try:
            result = getter(tags)
        except Exception:
            result = None
        if result and result[0]:
            return result
    return None


def ensure_cached(filepath: str, cache_dir: Path) -> Path | None:
    """アートワークを cache_dir に取り出してパスを返す。無ければ None。

    ブロッキングIOなので asyncio からは to_thread 経由で呼ぶこと。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    stem = track_hash(filepath)

    if (cache_dir / (stem + NONE_MARKER)).exists():
        return None
    for existing in cache_dir.glob(stem + ".*"):
        if existing.suffix != NONE_MARKER:
            return existing

    result = extract(filepath)
    if result is None:
        (cache_dir / (stem + NONE_MARKER)).touch()
        return None

    data, ext = result
    path = cache_dir / (stem + ext)
    tmp = path.with_suffix(ext + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path
