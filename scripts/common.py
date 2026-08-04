"""AIjukebox 共通ユーティリティ: 設定読み込み・DB接続・スキーマ定義。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tomllib
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.toml"
# git 管理外の上書き設定。メールアドレスやパスワードなど、リポジトリに
# 入れたくない値はこちらに書く(settings.toml と同じ構造で一部だけ書けばよい)。
LOCAL_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.local.toml"

# ID3由来カラムとMusicBrainz由来カラムを分離している。
# scan_library.py は前者だけを更新し、後者(mbid / mb_release_date /
# enrichment_source)には絶対に触らない。再スキャンで補完結果を消さないため。
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tracks (
    filepath TEXT PRIMARY KEY,
    -- ID3由来(scanで毎回上書きしてよい)
    title TEXT,
    artist TEXT,
    album TEXT,
    release_date TEXT,
    genre TEXT,
    composer TEXT,
    duration_sec INTEGER,
    -- MusicBrainz由来(scanでは絶対に触らない)
    mbid TEXT,
    mb_release_date TEXT,
    -- 管理
    enrichment_source TEXT DEFAULT 'id3_only',  -- 'id3_only' | 'musicbrainz' | 'not_found'
    last_scanned TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tracks_enrichment ON tracks(enrichment_source);
"""


def _deep_merge(base: dict, override: dict) -> dict:
    """override の値で base を再帰的に上書きする(テーブル単位では潰さない)。"""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """settings.toml を読み、settings.local.toml があれば上書きして返す。"""
    with open(path or SETTINGS_PATH, "rb") as f:
        settings = tomllib.load(f)

    if path is None and LOCAL_SETTINGS_PATH.exists():
        with open(LOCAL_SETTINGS_PATH, "rb") as f:
            _deep_merge(settings, tomllib.load(f))
    return settings


def resolve_path(value: str) -> Path:
    """設定中のパスを解決する。相対パスはプロジェクトルート基準。"""
    p = Path(value).expanduser()
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def connect_db(db_path: Path) -> sqlite3.Connection:
    """DBに接続し、スキーマを適用して返す(存在すれば何もしない)。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def track_hash(filepath: str) -> str:
    """intro.wav キャッシュのキー。filepath の安定ハッシュ。"""
    return hashlib.sha1(filepath.encode("utf-8")).hexdigest()


# NFKC では吸収されない約物の揺れ。MusicBrainz は "B’z" のようにタイポグラフィ
# 引用符を使うため、これを畳まないと ID3 の "B'z" と一致しない。
# 長音符 U+30FC(ー)は日本語の音の一部なので絶対に触らない。
_PUNCT_MAP = str.maketrans(
    {
        "‘": "'", "’": "'", "‛": "'",  # ‘ ’ ‛
        "“": '"', "”": '"',                  # “ ”
        "–": "-", "—": "-", "―": "-",   # – — ―
        "·": "・", "･": "・",                 # · ･
    }
)


def normalize_text(s: str) -> str:
    """検索・照合用の正規化。NFKC + 約物の統一 + 小文字化 + 空白圧縮。"""
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_PUNCT_MAP)
    s = s.casefold()
    return re.sub(r"\s+", " ", s).strip()


def year_of(date_str: str | None) -> str | None:
    """'1998-05-27' や '1998' から発売年 '1998' を取り出す。"""
    if not date_str:
        return None
    m = re.match(r"\s*(\d{4})", date_str)
    return m.group(1) if m else None


def append_intro_log(log_path: Path, filepath: str, title: str, text: str) -> None:
    """生成したコメントをJSONLで追記する。recent_intros の復元元になる。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "filepath": filepath,
        "title": title,
        "text": text,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def recent_intros(log_path: Path, n: int) -> list[str]:
    """直近 n 件のコメント本文を古い順で返す。ログが無ければ空リスト。"""
    if not log_path.exists():
        return []
    texts: list[str] = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                texts.append(json.loads(line)["text"])
            except (json.JSONDecodeError, KeyError):
                continue  # 壊れた行があっても復元を諦めない
    return texts[-n:]


def resolve_release_year(row) -> str | None:
    """DJが読み上げる発売年を決める。ID3とMusicBrainzのうち古い方を採る。

    単純な COALESCE(mb_release_date, release_date) にはしない。MusicBrainz は
    リマスターを別録音として持つため、原盤より新しい日付を返すことがある
    (Yesterday → 2004年のリマスター盤など)。一方 ID3 側はベスト盤やiTunes
    購入日に引きずられる。どちらの誤差も「実際より新しくなる」方向なので、
    両者の古い方を採ると原盤の年に近づく。
    """
    years = [
        y
        for y in (year_of(row["mb_release_date"]), year_of(row["release_date"]))
        if y
    ]
    return min(years) if years else None
