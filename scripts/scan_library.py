#!/usr/bin/env python3
"""library/ を再帰走査し、ID3由来のタグ情報を tracks テーブルに upsert する。

オフライン安全・冪等。MusicBrainz由来カラム(mbid / mb_release_date /
enrichment_source)には一切触れないので、何度実行しても補完結果は消えない。

  uv run --no-sync scripts/scan_library.py
  uv run --no-sync scripts/scan_library.py --prune    # 消えたファイルの行も削除
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import mutagen

from common import connect_db, load_settings, resolve_path

# easy=True で拾えないフォーマット固有タグの補完マップ。
# EasyMP4 は composer(©wrt)に対応していないため m4a で必要になる。
RAW_FALLBACK: dict[str, dict[str, str]] = {
    "EasyMP4": {
        "title": "\xa9nam",
        "artist": "\xa9ART",
        "album": "\xa9alb",
        "release_date": "\xa9day",
        "genre": "\xa9gen",
        "composer": "\xa9wrt",
    },
}

# easy キー名 → DBカラム名
EASY_KEYS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "date": "release_date",
    "genre": "genre",
    "composer": "composer",
}

UPSERT_SQL = """
INSERT INTO tracks (
    filepath, title, artist, album, release_date, genre, composer,
    duration_sec, last_scanned
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
ON CONFLICT(filepath) DO UPDATE SET
    title            = excluded.title,
    artist           = excluded.artist,
    album            = excluded.album,
    release_date     = excluded.release_date,
    genre            = excluded.genre,
    composer         = excluded.composer,
    duration_sec     = excluded.duration_sec,
    last_scanned     = CURRENT_TIMESTAMP
"""


# かな・カタカナ・漢字・半角カナ
_JP_CHARS = re.compile(r"[぀-ヿ一-鿿ｦ-ﾟ]")


def demojibake(s: str) -> str:
    """latin-1として誤デコードされたCP932文字列を復元する。

    古いiTunes/ID3v1由来の日本語タグは、CP932のバイト列がlatin-1として
    読まれて「\\x83`\\x83F...」のような文字列になる。安全側に倒すため、
    (1) 全文字がlatin-1範囲 (2) 上位バイトが4割以上 (3) CP932として解釈すると
    日本語になる、の3条件が揃ったときだけ変換する。"Björk" のような
    正当なlatin-1文字列は上位バイト比率で弾かれる。
    """
    if not s or any(ord(c) > 0xFF for c in s):
        return s
    high = sum(1 for c in s if ord(c) >= 0x80)
    if high < 2 or high / len(s) < 0.4:
        return s
    try:
        decoded = s.encode("latin-1").decode("cp932")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return decoded if _JP_CHARS.search(decoded) else s


def _first(value) -> str | None:
    """mutagen の値(リスト or スカラー)から最初の非空文字列を取り出す。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    s = demojibake(str(value).strip())
    return s or None


def read_tags(path: Path) -> dict[str, object] | None:
    """1ファイルからタグとdurationを抽出する。読めなければ None。"""
    easy = mutagen.File(path, easy=True)
    if easy is None:
        return None

    tags: dict[str, object] = {col: None for col in EASY_KEYS.values()}
    if easy.tags:
        for easy_key, col in EASY_KEYS.items():
            try:
                tags[col] = _first(easy.tags.get(easy_key))
            except (KeyError, ValueError):
                # easy 実装によっては未対応キーで例外を投げる
                tags[col] = None

    # easy で埋まらなかったフィールドを生タグで補う(主に m4a の composer)
    fallback = RAW_FALLBACK.get(type(easy).__name__)
    if fallback and any(tags[col] is None for col in fallback):
        raw = mutagen.File(path)
        if raw is not None and raw.tags:
            for col, atom in fallback.items():
                if tags.get(col) is None:
                    tags[col] = _first(raw.tags.get(atom))

    # タイトルが無い曲はDJが紹介できないのでファイル名で代用する
    if not tags.get("title"):
        tags["title"] = path.stem

    length = getattr(easy.info, "length", None)
    tags["duration_sec"] = int(length) if length else None
    return tags


def is_target(path: Path, extensions: set[str]) -> bool:
    if not path.is_file() or path.suffix.lower() not in extensions:
        return False
    # macOS が残す AppleDouble (._foo.mp3) と隠しファイルは音声本体ではない
    return not path.name.startswith(".")


def scan(conn, library: Path, extensions: set[str]) -> tuple[int, list[Path]]:
    files = sorted(p for p in library.rglob("*") if is_target(p, extensions))
    ok = 0
    failed: list[Path] = []

    for path in files:
        abspath = str(path.resolve())
        try:
            tags = read_tags(path)
        except Exception as e:  # 壊れたファイルで走査全体を止めない
            print(f"  [warn] {path.name}: {e}", file=sys.stderr)
            failed.append(path)
            continue
        if tags is None:
            print(f"  [warn] {path.name}: 未対応フォーマット", file=sys.stderr)
            failed.append(path)
            continue

        conn.execute(
            UPSERT_SQL,
            (
                abspath,
                tags["title"],
                tags["artist"],
                tags["album"],
                tags["release_date"],
                tags["genre"],
                tags["composer"],
                tags["duration_sec"],
            ),
        )
        ok += 1

    conn.commit()
    return ok, failed


def prune(conn) -> int:
    """DB上にあるが実体が消えたファイルの行を削除する。"""
    rows = conn.execute("SELECT filepath FROM tracks").fetchall()
    gone = [r["filepath"] for r in rows if not Path(r["filepath"]).exists()]
    conn.executemany("DELETE FROM tracks WHERE filepath = ?", [(p,) for p in gone])
    conn.commit()
    return len(gone)


def main() -> int:
    ap = argparse.ArgumentParser(description="音楽ライブラリのタグをDBへ取り込む")
    ap.add_argument("--library", help="走査ディレクトリ(既定: settings.toml)")
    ap.add_argument("--prune", action="store_true", help="実体の無い行をDBから削除")
    args = ap.parse_args()

    settings = load_settings()
    library = resolve_path(args.library or settings["paths"]["library"])
    db_path = resolve_path(settings["paths"]["db"])
    extensions = {e.lower() for e in settings["scan"]["extensions"]}

    if not library.is_dir():
        print(f"ライブラリが見つかりません: {library}", file=sys.stderr)
        return 1

    conn = connect_db(db_path)
    try:
        print(f"走査: {library}")
        ok, failed = scan(conn, library, extensions)
        print(f"  取り込み {ok} 曲 / 失敗 {len(failed)} 件")

        if args.prune:
            removed = prune(conn)
            print(f"  削除(実体なし) {removed} 行")

        total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        by_src = conn.execute(
            "SELECT enrichment_source, COUNT(*) c FROM tracks GROUP BY 1 ORDER BY 1"
        ).fetchall()
        print(f"DB合計: {total} 曲")
        for r in by_src:
            print(f"  {r['enrichment_source']}: {r['c']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
