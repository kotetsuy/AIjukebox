#!/usr/bin/env python3
"""Qwen 3.6 で DJ の紹介コメントを生成する。

  uv run --no-sync scripts/dj_prompt.py --title キセキ        # 部分一致で1曲
  uv run --no-sync scripts/dj_prompt.py --title キセキ --synth # intro.wav まで作る
  uv run --no-sync scripts/dj_prompt.py --all --dry-run       # プロンプトだけ確認

Thinking mode は無効(レイテンシ優先)。生成後に文字数トリムと記号除去をかける。
"""

from __future__ import annotations

import argparse
import re
import sys

import httpx

from common import (
    append_intro_log,
    connect_db,
    load_settings,
    recent_intros,
    resolve_path,
    resolve_release_year,
    track_hash,
)

SYSTEM_PROMPT = """あなたはローカルAIラジオのDJです。次にかける曲を紹介するコメントを生成してください。

# ルール
1. 与えられた情報(曲名・アーティスト・発売年・ジャンル)のみを事実として扱う
2. 与えられていない具体的事実は絶対に断定的に言わない
   - タイアップ作品、受賞歴、エピソード、歌詞の内容、使われている楽器、制作の背景
   - 悪い例: 「ドラマ○○の主題歌として話題になりました」(情報にない場合)
   - 悪い例: 「ギターとピアノのシンプルな伴奏が特徴です」(楽器は情報にない)
   - 良い例: 曲の雰囲気や聴きどころなど、一般的な表現に留める
3. 曲名とアーティスト名は与えられた表記のまま使う。翻訳・意訳・略称への言い換えをしない
   - 悪い例: 「Yesterday」を「昨日」と言う
4. 出力は音声合成で読み上げるため、記号(*, #, ☆, ♪, 括弧の多用)や改行を使わない
5. 1コメントは60〜100文字。100文字を超えてはいけない
6. 前回までのコメントと同じ言い回しの繰り返しを避ける
7. 発売年やジャンルなどの情報が欠けている場合、無理に触れず自然に省略する
8. 必ず「。」で終わる完結した文にする

# 出力形式
コメント本文のみを出力する。前置きや説明は不要。"""

# enrichment_source ごとの踏み込み方(HANDOFF §5)
GUIDANCE = {
    "musicbrainz": "発売年などの情報は確度が高いので、積極的に触れて構いません。",
    "id3_only": "曲の情報はそのまま信用して構いません。",
    "not_found": (
        "この曲は曲名とアーティスト名しか確かな情報がありません。"
        "年号や作品名には触れず、雰囲気重視の短めのコメントにしてください。"
    ),
}

# 音声合成が「ほし」「おんぷ」などと読んでしまう記号。「」は無音で読まれるので残す。
_STRIP_CHARS = re.compile(
    r"[*#`~_<>\[\]{}|\\/^=+☆★♪♬♩♂♀※→←↑↓○●◎△▲▽▼□■◆◇『』【】〈〉《》]"
)
# 「コメント:」「紹介文:」のような前置き
_PREAMBLE = re.compile(r"^\s*(コメント|紹介文?|出力|回答)\s*[:：]\s*")
# 完結した文の終わり
_SENTENCE_END = ("。", "!", "?", "!", "?")


def build_user_prompt(row, recent: list[str]) -> str:
    """曲情報のうち存在するものだけを列挙したユーザープロンプトを組む。"""
    source = row["enrichment_source"] or "id3_only"

    facts = [f"曲名: {row['title']}"]
    if row["artist"]:
        facts.append(f"アーティスト: {row['artist']}")

    # not_found は確証が無いので曲名・アーティスト以外を渡さない
    if source != "not_found":
        year = resolve_release_year(row)
        if year:
            facts.append(f"発売年: {year}年")
        # アルバム名は読み上げない。ベスト盤や「- Single」のような
        # iTunes 由来の表記が多く、DJコメントとして自然にならないため。
        if row["genre"]:
            facts.append(f"ジャンル: {row['genre']}")

    parts = ["# 次にかける曲", "\n".join(facts), "", GUIDANCE.get(source, "")]

    if recent:
        parts += [
            "",
            "# 直近のコメント(同じ言い回しは避けてください)",
            "\n".join(f"- {t}" for t in recent),
        ]
    return "\n".join(parts).strip()


def _trim_to_sentence(text: str) -> str:
    """末尾の未完成な文を落とす。無理なら「。」で締める。"""
    if text.endswith(_SENTENCE_END):
        return text
    cut = max(text.rfind(c) for c in _SENTENCE_END)
    if cut >= 0:
        return text[: cut + 1]
    return text.rstrip("、 ") + "。"


def validate_intro(text: str, max_chars: int) -> str:
    """生成結果をTTSに流せる形に整える。

    記号を落とし、max_chars を超える場合は文末で切る。max_tokens 到達で
    文の途中で切れて返ることがあるので、末尾が句点でなければそこも落とす
    (読み上げが尻切れになるのを防ぐ)。
    """
    text = _PREAMBLE.sub("", text.strip())
    text = _STRIP_CHARS.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("　 「」\"'")

    if len(text) > max_chars:
        text = text[:max_chars]
    return _trim_to_sentence(text)


def call_llm(system: str, user: str, conf: dict) -> str:
    payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": conf["temperature"],
        "max_tokens": conf["max_tokens"],
        "stream": False,
        # Qwen3 系は既定で thinking を吐き、reasoning_content に max_tokens を
        # 食われて content が空で返る。chat template 側で切る(AIzunda 知見)。
        "chat_template_kwargs": {"enable_thinking": False},
    }
    r = httpx.post(
        f"{conf['base_url']}/v1/chat/completions", json=payload, timeout=120.0
    )
    r.raise_for_status()
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"llama-server の応答が想定外です: {data}") from e


def generate_intro_text(row, settings: dict, recent: list[str] | None = None) -> str:
    """1曲ぶんの紹介コメントを生成して整形したものを返す。"""
    conf = settings["llm"]
    log_path = resolve_path(settings["paths"]["log"])
    if recent is None:
        recent = recent_intros(log_path, conf["recent_intros"])

    user = build_user_prompt(row, recent)
    raw = call_llm(SYSTEM_PROMPT, user, conf)
    return validate_intro(raw, conf["max_chars"])


def find_tracks(conn, title: str | None, all_tracks: bool):
    if all_tracks:
        return conn.execute("SELECT * FROM tracks ORDER BY artist").fetchall()
    if title:
        return conn.execute(
            "SELECT * FROM tracks WHERE title LIKE ? OR artist LIKE ? ORDER BY artist",
            (f"%{title}%", f"%{title}%"),
        ).fetchall()
    return conn.execute("SELECT * FROM tracks ORDER BY RANDOM() LIMIT 1").fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(description="DJ紹介コメントを生成する")
    ap.add_argument("--title", help="曲名/アーティスト名の部分一致(省略時はランダム1曲)")
    ap.add_argument("--all", action="store_true", help="全曲ぶん生成する")
    ap.add_argument("--synth", action="store_true", help="VOICEVOXでintro.wavまで作る")
    ap.add_argument("--dry-run", action="store_true", help="プロンプトを表示してLLMは呼ばない")
    ap.add_argument("--no-log", action="store_true", help="logs/program.log に追記しない")
    args = ap.parse_args()

    settings = load_settings()
    log_path = resolve_path(settings["paths"]["log"])
    conn = connect_db(resolve_path(settings["paths"]["db"]))

    try:
        rows = find_tracks(conn, args.title, args.all)
        if not rows:
            print("該当する曲がありません。", file=sys.stderr)
            return 1

        # 直近コメントはログから復元し、生成のたびにこのプロセス内でも積む
        recent = recent_intros(log_path, settings["llm"]["recent_intros"])

        for row in rows:
            print(f"\n=== {row['artist']} - {row['title']} [{row['enrichment_source']}]")
            if args.dry_run:
                print(build_user_prompt(row, recent))
                continue

            try:
                text = generate_intro_text(row, settings, recent)
            except httpx.HTTPError as e:
                print(
                    f"llama-server に接続できません ({settings['llm']['base_url']}): {e}",
                    file=sys.stderr,
                )
                return 1

            print(f"コメント({len(text)}文字): {text}")
            recent = (recent + [text])[-settings["llm"]["recent_intros"] :]

            if not args.no_log:
                append_intro_log(log_path, row["filepath"], row["title"], text)

            if args.synth:
                import voicevox_synth

                try:
                    path = voicevox_synth.synth_to_cache(
                        text, track_hash(row["filepath"]), settings
                    )
                except httpx.HTTPError as e:
                    print(f"VOICEVOX に接続できません: {e}", file=sys.stderr)
                    return 1
                print(f"音声: {path}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
