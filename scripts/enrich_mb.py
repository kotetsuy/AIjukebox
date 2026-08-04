#!/usr/bin/env python3
"""MusicBrainz で発売日を補完する(オンライン専用・手動実行)。

  uv run --no-sync scripts/enrich_mb.py             # 未補完・not_found を対象
  uv run --no-sync scripts/enrich_mb.py --force     # 全曲やり直し
  uv run --no-sync scripts/enrich_mb.py --limit 20 --dry-run

MusicBrainz の 1req/秒 制限を守るため、リクエスト間に settings.toml の
request_interval_sec だけスリープする。数百曲で数分〜十数分かかる。
"""

from __future__ import annotations

import argparse
import sys
import time

import musicbrainzngs as mb

from common import connect_db, load_settings, normalize_text, resolve_path

COLUMNS = "filepath, title, artist, duration_sec"
SELECT_ALL = f"SELECT {COLUMNS} FROM tracks"

# 既定は「未補完のみ」。not_found はタグ不備が原因で再試行しても結果が変わらない
# ことが多く、毎回全件叩くと待ち時間だけ伸びるので除外する。
# タグを直した後などに再挑戦したい場合は --retry-not-found を付ける。
SELECT_PENDING = (
    f"SELECT {COLUMNS} FROM tracks "
    "WHERE enrichment_source NOT IN ('musicbrainz', 'not_found') "
    "OR enrichment_source IS NULL"
)
SELECT_PENDING_WITH_NOTFOUND = (
    f"SELECT {COLUMNS} FROM tracks "
    "WHERE enrichment_source NOT IN ('musicbrainz') OR enrichment_source IS NULL"
)

# 同一曲の別録音(ライブ・TVサイズ・再録)を弾くための再生時間の許容差(秒)
DURATION_TOLERANCE_SEC = 5

_last_request = 0.0


def throttle(interval: float) -> None:
    """MusicBrainz の 1req/秒 制限。前回リクエストからの経過分だけ待つ。"""
    global _last_request
    wait = interval - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def earliest_date(recording: dict) -> str | None:
    """録音に紐づく全リリースから最も古い発売日を返す。"""
    dates = [
        rel["date"] for rel in recording.get("release-list", []) if rel.get("date")
    ]
    if not dates:
        return None
    # 'YYYY' と 'YYYY-MM-DD' が混在するが、辞書順で最古が取れる
    return min(dates)


def fetch_earliest_date(mbid: str, interval: float) -> str | None:
    """録音IDから発売日を解決する。

    検索結果に載る release-list は一致したリリースだけの部分集合で、再発盤や
    ベスト盤しか含まないことがある(例: 迷子犬と雨のビート → 2022年のアルバム)。
    オリジナルの発売年を得るには録音の全リリースを引き直す必要がある。
    """
    throttle(interval)
    full = mb.get_recording_by_id(mbid, includes=["releases"])["recording"]
    return earliest_date(full)


def credited_artists(recording: dict) -> list[str]:
    names = []
    for credit in recording.get("artist-credit", []):
        if isinstance(credit, dict) and "artist" in credit:
            artist = credit["artist"]
            names.append(artist.get("name", ""))
            names.extend(a.get("name", "") for a in artist.get("alias-list", []))
    return [n for n in names if n]


def artist_matches(want: str, recording: dict) -> bool:
    """アーティスト名の一致確認。feat. 等の付随表記を許すため部分一致も可。"""
    w = normalize_text(want)
    if not w:
        return False
    for name in credited_artists(recording):
        n = normalize_text(name)
        if not n:
            continue
        if n == w or n in w or w in n:
            return True
    return False


def search_candidates(
    title: str, artist: str, min_score: int, interval: float
) -> tuple[list[dict], list[dict]]:
    """検索して (アーティスト一致済み候補, スコアだけ通過した候補) を返す。

    limit=1即採用はしない。後者はエイリアス照合のフォールバック用。
    """
    throttle(interval)
    result = mb.search_recordings(recording=title, artist=artist, limit=8)

    scored, qualified = [], []
    for rec in result.get("recording-list", []):
        try:
            score = int(rec.get("ext:score", 0))
        except (TypeError, ValueError):
            score = 0
        if score < min_score:
            continue  # 結果は score 降順なので以降も満たさない
        scored.append(rec)
        if artist_matches(artist, rec):
            qualified.append(rec)
    return qualified, scored


def verify_via_aliases(scored: list[dict], artist: str, interval: float) -> list[dict]:
    """MusicBrainz のエイリアスまで見てアーティスト一致を取り直す。

    ID3が「マイケル・ジャクソン」、MB名義が「Michael Jackson」のような言語違いを
    救うため。MBに実在するエイリアスとの完全一致だけを認めるので、閾値を下げる
    のと違って誤マッチは増えない。アーティストIDごとに1回だけ問い合わせる。
    """
    want = normalize_text(artist)
    checked: dict[str, bool] = {}
    matched = []

    for rec in scored:
        artist_ids = [
            c["artist"]["id"]
            for c in rec.get("artist-credit", [])
            if isinstance(c, dict) and "artist" in c and c["artist"].get("id")
        ]
        for aid in artist_ids:
            if aid not in checked:
                throttle(interval)
                info = mb.get_artist_by_id(aid, includes=["aliases"])["artist"]
                names = [info.get("name", ""), info.get("sort-name", "")]
                names += [a.get("name", "") for a in info.get("alias-list", [])]
                checked[aid] = any(normalize_text(n) == want for n in names if n)
            if checked[aid]:
                matched.append(rec)
                break
    return matched


def pick_recording(candidates: list[dict], duration_sec: int | None) -> dict:
    """候補から実際に鳴らす録音を選ぶ。

    同名異録音(ライブ版・TVサイズ・リマスター)が同スコアで並ぶため、ID3の
    再生時間に最も近い録音を優先する。これが無いと別バージョンを掴む。
    """
    if duration_sec is None:
        return candidates[0]

    def diff(rec: dict) -> float:
        length = rec.get("length")
        if not length:
            return float("inf")
        return abs(int(length) / 1000 - duration_sec)

    closest = min(candidates, key=diff)
    # 許容差に収まる候補が無ければ再生時間は判断材料にせず最高スコアを採る
    return closest if diff(closest) <= DURATION_TOLERANCE_SEC else candidates[0]


def resolve_date(candidates: list[dict], chosen: dict, interval: float) -> str | None:
    """曲としての発売日を求める。

    MusicBrainz はリマスターを別の recording として持つため、選んだ録音の
    リリースだけを見ると原盤より新しい日付になる(例: Yesterday → 2004年の
    リマスター盤)。そこで「同一アーティストの同名録音すべてに紐づく日付」の
    最古を採る。検索結果の release-list は部分集合なので、選んだ録音については
    全リリースを引き直して補強する。
    """
    dates = [
        rel["date"]
        for rec in candidates
        for rel in rec.get("release-list", [])
        if rel.get("date")
    ]
    full = fetch_earliest_date(chosen["id"], interval)
    if full:
        dates.append(full)
    return min(dates) if dates else None


def main() -> int:
    ap = argparse.ArgumentParser(description="MusicBrainz で発売日を補完する")
    ap.add_argument("--force", action="store_true", help="補完済みも含めて全曲やり直す")
    ap.add_argument(
        "--retry-not-found",
        action="store_true",
        help="前回 not_found だった曲も対象に含める",
    )
    ap.add_argument("--limit", type=int, help="処理する最大曲数")
    ap.add_argument("--dry-run", action="store_true", help="DBを更新せず結果だけ表示")
    args = ap.parse_args()

    settings = load_settings()
    conf = settings["musicbrainz"]
    db_path = resolve_path(settings["paths"]["db"])
    interval = float(conf["request_interval_sec"])
    min_score = int(conf["min_score"])

    mb.set_useragent(conf["app_name"], conf["app_version"], conf["contact"])

    conn = connect_db(db_path)
    try:
        if args.force:
            query = SELECT_ALL
        elif args.retry_not_found:
            query = SELECT_PENDING_WITH_NOTFOUND
        else:
            query = SELECT_PENDING
        rows = conn.execute(query).fetchall()
        if args.limit:
            rows = rows[: args.limit]
        if not rows:
            print("補完対象がありません。")
            return 0

        print(f"対象 {len(rows)} 曲 (min_score={min_score}, interval={interval}s)")
        found = notfound = skipped = errors = 0

        for i, row in enumerate(rows, 1):
            title = (row["title"] or "").strip()
            artist = (row["artist"] or "").strip()
            label = f"[{i}/{len(rows)}] {artist} - {title}"

            if not title or not artist:
                # アーティスト不明では誤マッチしか起きないので検索しない
                print(f"{label}  → skip (title/artist 不足)")
                skipped += 1
                continue

            # 正規化した文字列で検索(表記揺れ対策)
            try:
                candidates, scored = search_candidates(
                    normalize_text(title), normalize_text(artist), min_score, interval
                )
                if not candidates and scored:
                    candidates = verify_via_aliases(scored, artist, interval)
                rec = pick_recording(candidates, row["duration_sec"]) if candidates else None
                date = resolve_date(candidates, rec, interval) if rec else None
            except mb.NetworkError as e:
                # 通信断は「見つからない」ではないので not_found を書かない
                print(f"{label}  → 通信エラー: {e}", file=sys.stderr)
                errors += 1
                continue
            except mb.MusicBrainzError as e:
                print(f"{label}  → MBエラー: {e}", file=sys.stderr)
                errors += 1
                continue

            if rec is None:
                print(f"{label}  → not_found")
                notfound += 1
                if not args.dry_run:
                    conn.execute(
                        "UPDATE tracks SET enrichment_source='not_found' WHERE filepath=?",
                        (row["filepath"],),
                    )
                    conn.commit()
            else:
                print(f"{label}  → {rec['id']} date={date or '-'} score={rec.get('ext:score')}")
                found += 1
                if not args.dry_run:
                    conn.execute(
                        "UPDATE tracks SET mbid=?, mb_release_date=?, "
                        "enrichment_source='musicbrainz' WHERE filepath=?",
                        (rec["id"], date, row["filepath"]),
                    )
                    conn.commit()

        print(
            f"\n完了: 補完 {found} / not_found {notfound} / skip {skipped} / エラー {errors}"
            + ("  (dry-run: DB未更新)" if args.dry_run else "")
        )
    except KeyboardInterrupt:
        print("\n中断しました(ここまでの結果はDBに反映済み)。")
        return 130
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
