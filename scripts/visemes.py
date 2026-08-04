"""VOICEVOX の accent_phrases を VRM のリップシンク用 viseme 列に変換する。

既存 AIassistant/three-vrm/server.py と同じ対応表・同じ出力形式
(visemes / vtimes[ms] / vdurations[ms])を使う。表示系のアニメーション側を
そのまま流用できるようにするため。

AIjukebox では intro の音声を Liquidsoap が鳴らすのでブラウザは音を出さない。
viseme だけを受け取って口を動かす。
"""

from __future__ import annotations

VOWEL_TO_VISEME = {
    "a": "aa", "i": "I", "u": "U", "e": "E", "o": "O",
    "N": "nn", "cl": "sil", "pau": "sil",
}

CONSONANT_TO_VISEME = {
    "p": "PP",  "b": "PP",  "m": "PP",
    "py": "PP", "by": "PP", "my": "PP",
    "f": "FF",
    "s": "SS",  "z": "SS",  "sh": "SS",
    "t": "DD",  "d": "DD",  "ts": "DD",
    "k": "kk",  "g": "kk",  "ky": "kk", "gy": "kk",
    "ch": "CH", "j": "CH",
    "n": "nn",  "ny": "nn",
    "r": "RR",  "ry": "RR",
    "h": "sil", "hy": "sil", "w": "sil", "y": "sil",
}


def mora_to_visemes(accent_phrases: list) -> tuple[list[str], list[int], list[int]]:
    """accent_phrases を (viseme名, 開始ms, 継続ms) の3配列に変換する。"""
    visemes: list[str] = []
    vtimes: list[int] = []
    vdurations: list[int] = []
    t = 0.0

    for phrase in accent_phrases:
        for mora in phrase.get("moras", []):
            c = mora.get("consonant")
            cl = mora.get("consonant_length") or 0.0
            v = mora.get("vowel", "pau")
            vl = mora.get("vowel_length") or 0.0

            if c and cl > 0:
                visemes.append(CONSONANT_TO_VISEME.get(c, "sil"))
                vtimes.append(int(t * 1000))
                vdurations.append(max(1, int(cl * 1000)))
                t += cl

            if v and vl > 0:
                visemes.append(VOWEL_TO_VISEME.get(v, "sil"))
                vtimes.append(int(t * 1000))
                vdurations.append(max(1, int(vl * 1000)))
                t += vl

        pause = phrase.get("pause_mora")
        if pause:
            pl = pause.get("vowel_length") or 0.0
            if pl > 0:
                visemes.append("sil")
                vtimes.append(int(t * 1000))
                vdurations.append(max(1, int(pl * 1000)))
                t += pl

    return visemes, vtimes, vdurations
