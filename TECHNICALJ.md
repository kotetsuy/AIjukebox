# TECHNICALJ.md — AIjukebox 技術資料

内部構造と設計判断、そして実装中に詰まった点とその解決策。
セットアップ手順は [READMEJ.md](READMEJ.md)、元の設計仕様は [HANDOFF.md](HANDOFF.md)。

---

## 目次

1. [全体構成](#1-全体構成)
2. [曲情報パイプライン](#2-曲情報パイプライン)
3. [DJコメント生成](#3-djコメント生成)
4. [音声合成とキャッシュ](#4-音声合成とキャッシュ)
5. [ミキシングと配信](#5-ミキシングと配信)
6. [番組進行の状態機械](#6-番組進行の状態機械)
7. [表示系](#7-表示系)
8. [詰まった点と解決策](#8-詰まった点と解決策)
9. [性能実測値](#9-性能実測値)

---

## 1. 全体構成

### 1.1 責務の分割

| コンポーネント | 責務 |
|---|---|
| `scan_library.py` | ID3タグ → DB(オフライン・冪等) |
| `enrich_mb.py` | MusicBrainz 補完(オンライン・手動) |
| `dj_prompt.py` | 曲情報 → プロンプト → Qwen → 整形 |
| `voicevox_synth.py` | テキスト → wav + accent_phrases |
| `liquidsoap_client.py` | telnet で Liquidsoap を叩く薄い層 |
| `program_service.py` | **選曲・状態機械・自動ループ・WebSocket・表示系配信** |
| `radio.liq` | ミキシングと出力だけ。ロジックを持たない |

### 1.2 選曲権は Python が持つ

**Liquidsoap に playlist や自動選曲を持たせない。** これは最初から意図的に決めた
最重要の設計方針で、理由は「DJ が紹介した曲と、実際に鳴る曲を一致させる」ため。

Liquidsoap 側で選曲すると、Python は「次に何がかかるか」を知らないまま紹介文を
作ることになり、原理的にズレる。Python が `pick_next()` で決め、その曲の紹介文を
作り、両方を Liquidsoap の**キューに push する**という一方向の流れにしてある。

Liquidsoap 側は `request.queue` を2本持つだけ:

```liquidsoap
music_queue = request.queue(id="music")     # Python が次曲を push
dj_queue    = request.queue(id="dj_intro")  # Python が intro.wav を push
```

### 1.3 プロセス構成

```
program_service.py (asyncio)
 ├─ aiohttp: HTTP(表示系) + WebSocket   :8765
 ├─ poll_loop: 0.5秒ごとに曲の残り時間を見て終端を検出
 └─ to_thread: LLM呼び出し / 音声合成 / telnet (ブロッキングIOを逃がす)
        │
        └─ telnet ─▶ Liquidsoap :1234
                        ├─ output.icecast  ─▶ Icecast :8100 ─▶ スマホ
                        └─ output.pulseaudio ─▶ スピーカー
```

すべて1プロセス。`asyncio.to_thread` でブロッキング処理を逃がしているので、
LLM が 1.5 秒かかっている間もポーリングと WebSocket は動き続ける。

---

## 2. 曲情報パイプライン

### 2.1 DBスキーマ — ID3由来と MusicBrainz由来を分離する

```sql
CREATE TABLE tracks (
    filepath TEXT PRIMARY KEY,
    -- ID3由来(scanで毎回上書きしてよい)
    title, artist, album, release_date, genre, composer, duration_sec,
    -- MusicBrainz由来(scanでは絶対に触らない)
    mbid TEXT, mb_release_date TEXT,
    -- 管理
    enrichment_source TEXT DEFAULT 'id3_only',  -- id3_only | musicbrainz | not_found
    last_scanned TIMESTAMP
);
```

カラムを分けているのは、**`scan_library.py` の再実行で MusicBrainz の補完結果を
消さないため**。同じテーブルに混ぜて `UPSERT` すると、タグを読み直すたびに
苦労して集めた発売年が消える。

`scan_library.py` の `ON CONFLICT DO UPDATE SET` は ID3 由来カラムしか列挙しない。

### 2.2 MusicBrainz のマッチング

「1件目を採用」は禁止。次の3段構えにしてある。

1. `search_recordings(recording=…, artist=…, limit=8)`
2. `ext:score >= 90` かつ **アーティスト名が一致**したものだけを候補にする
3. 候補の中から **ID3 の再生時間に最も近い録音**(±5秒)を選ぶ

3 が効くのは、同じ曲名でライブ版・TVサイズ・リマスターが同スコア 100 で並ぶため。
実例として「迷子犬と雨のビート」は 414秒 / 138秒 / 296秒 / 301秒 の4つが
同スコアで返り、ID3 の 296秒 と突き合わせないと 2022年のアルバム版を掴む。

検索前にアーティスト名・曲名を NFKC 正規化し、さらに約物を畳む(後述)。

### 2.3 発売年は COALESCE ではなく「古い方」

読み出し時は `common.resolve_release_year(row)` を使う。

```python
years = [year_of(row["mb_release_date"]), year_of(row["release_date"])]
return min(y for y in years if y)
```

仕様では `COALESCE(mb_release_date, release_date)` としていたが、実データで
**MusicBrainz の方が新しい年を返すことがある**とわかった。

| 曲 | ID3 | MusicBrainz | 採用 | 実際 |
|---|---|---|---|---|
| Yesterday | 1965 | 1985 | **1965** | 1965 |
| Bicycle Race | 1978 | 1979 | **1978** | 1978 |
| 銀河鉄道999 | 1990 | 2021 | **1990** | 1979 |
| ultra soul | 2008 | 2001 | **2001** | 2001 |

MusicBrainz はリマスターを別 recording として持つため原盤より新しくなり、
ID3 側はベスト盤や iTunes の購入日に引きずられて新しくなる。
**どちらの誤差も「実際より新しい」方向にしか出ない**ので、古い方を採ると原盤に近づく。
15曲中12曲が正確、残り3曲も ID3 相当まで戻る。

---

## 3. DJコメント生成

### 3.1 幻覚対策

システムプロンプトの中心は「与えられた情報だけを事実として扱う」こと。
特にタイアップ作品や受賞歴を捏造されると、ラジオとして致命的になる。

実際に出た問題を禁止例として明記している:

```
2. 与えられていない具体的事実は絶対に断定的に言わない
   - タイアップ作品、受賞歴、エピソード、歌詞の内容、使われている楽器、制作の背景
   - 悪い例: 「ドラマ○○の主題歌として話題になりました」(情報にない場合)
   - 悪い例: 「ギターとピアノのシンプルな伴奏が特徴です」(楽器は情報にない)
3. 曲名とアーティスト名は与えられた表記のまま使う。翻訳・意訳をしない
   - 悪い例: 「Yesterday」を「昨日」と言う
```

「楽器」と「翻訳」の2つは、実際に生成させてみて初めて出てきた失敗。

### 3.2 情報の確度によって踏み込み方を変える

`enrichment_source` に応じてユーザープロンプトを変える。

| source | 方針 |
|---|---|
| `musicbrainz` | 発売年などに積極的に触れてよい |
| `id3_only` | タグ情報はそのまま信用してよい |
| `not_found` | **曲名とアーティスト名しか渡さない**。雰囲気重視の短文 |

`not_found` のときは年号やジャンルをプロンプトに含めない。確証のない情報を
渡すと、そこを起点に話を膨らませてしまうため。

### 3.3 アルバム名は渡さない

ベスト盤名や iTunes の `- Single` / `[Disc 1]` が多く、「アルバム、キックバック
マイナス シングルに収録された」のような不自然な読み上げになるため、
プロンプトに載せないことにした。

### 3.4 生成後のバリデーション

```python
text = _PREAMBLE.sub("", text)      # 「コメント:」等の前置きを除去
text = _STRIP_CHARS.sub("", text)   # ☆★♪♂♀『』【】… を除去
text = text[:max_chars]             # 120文字で切る
text = _trim_to_sentence(text)      # 末尾が句点でなければ最後の句点まで戻す
```

記号除去は音声合成が「ほし」「おんぷ」と読んでしまうため。
「アゲ♂アゲ♂EVERY☆騎士」がそのまま読み上げられて気づいた。

`_trim_to_sentence` は、生成が `max_tokens` に達して文の途中で切れた場合の保険。

### 3.5 直近コメントの重複回避

`logs/program.log`(JSONL)に生成コメントを追記し、直近3件をプロンプトに
「この言い回しは避けてください」として添える。プロセスを再起動しても
ログから復元されるので、再起動直後に同じ言い回しが出ない。

---

## 4. 音声合成とキャッシュ

### 4.1 speaker_id を固定しない

```python
def resolve_speaker_id(base_url, speaker_name, style_name) -> int:
    # GET /speakers から name="波音リツ", style="ノーマル" で引く
```

VOICEVOX のバージョン間で id が変わりうるため。id を直書きすると、
ある日突然まったく別の話者が喋りだす。実測では `id=9` だが、コードには書かない。

### 4.2 キャッシュは wav と json の2つ

```
cache/intros/{sha1(filepath)}.wav    音声
cache/intros/{sha1(filepath)}.json   text / speaker_id / accent_phrases
```

**json も保存するのが重要。** 表示系のリップシンクは VOICEVOX の
`accent_phrases` から viseme を組み立てる方式なので、音声だけキャッシュすると
PREV でキャッシュを再利用したときに口が動かなくなる。

`track_hash` は filepath の SHA-1。つまり **一度生成した曲のコメントは固定される**。
PREV が常に即応するという要件と引き換えに、同じ曲は毎回同じ紹介になる。
変えたければキャッシュファイルを消す。

---

## 5. ミキシングと配信

### 5.1 ダッキング

```liquidsoap
radio = smooth_add(normal=music_queue, special=dj_queue, duration=1.5, p=0.15)
```

`dj_queue` に音が入ると音楽を 0.15 倍まで 1.5 秒かけて下げ、終わったら戻す。

一定振幅の 440Hz トーンを音楽として流し、intro 投入前後の 440Hz 成分だけを
帯域通過フィルタで取り出して実測した(曲の抑揚と区別するため):

```
 11s  -36.5 dB   ← 通常
 13s  -48.3 dB   ← ダッキング(約1.5秒でフェード)
 …    -46〜-53 dB
 31s  -44.2 dB   ← 復帰フェード
 34s  -36.5 dB   ← 元に戻る
```

理論値は `20·log10(0.15) = -16.5 dB`。測定値が -11〜-16 dB に散るのは、
DJ 音声の倍音が 440Hz 帯域に漏れて底上げされるため。

### 5.2 一時停止

Liquidsoap はプル型エンジンなので、素直な「一時停止」がない。
**選択されていないソースはフレームを要求されず、位置が進まない**性質を使う。

```liquidsoap
paused = interactive.bool("paused", false)
stream = switch(track_sensitive=false, [(paused, blank()), ({true}, radio)])
```

telnet で `var.set paused = true` を送ると `blank()` に切り替わり、
`radio` が引かれなくなって再生位置ごと凍結する。

実測: 一時停止前 167.9秒 → 6秒待機 → 再開後4秒で 163.7秒。停止中は進んでいない。

この方式には副次的な利点が2つある。

- **切り替え点が1箇所**なので、「片方の出力だけ止まって位置がずれる」問題が
  構造的に起きない
- **一時停止中も Icecast には無音が流れ続ける**ため、スマホのリスナーが切断されない

### 5.3 Icecast は sudo なしで動かす

`config/icecast.xml` は `chroot` と `changeowner` を使わないので、ユーザー権限の
まま起動できる。`/etc/icecast2` には触らない。ログもプロジェクト内に出る。

`logdir` は**相対パス**(`logs`)にしてある。絶対パスを書くとそのマシンでしか
動かなくなるため。プロジェクトルートから起動する前提。

ポートは 8000 ではなく **8100**。8000 は既存の three-vrm 表示系が使っていて衝突する。

---

## 6. 番組進行の状態機械

### 6.1 先読み生成

```
曲N 再生開始
  ├─ 即座に pick_next() で曲N+1 を確定
  ▼
[GENERATING]   NEXT は無効(グレーアウト)
  ├─ キャッシュがあれば即 NEXT_READY
  └─ 無ければ Qwen → validate → VOICEVOX → キャッシュ保存
  ▼
[NEXT_READY]   NEXT 有効
  ├─ NEXT押下  → 曲N+1 を push して skip
  └─ 自然終了  → 曲N+1 を push
```

「次曲の決定と紹介文の生成を、現在曲の**再生開始と同時に**行う」のが肝。
曲が終わってから作り始めると、NEXT を押すたびに数秒待たされる。

### 6.2 選曲

```python
class TrackSelector:
    exclude_history: deque(maxlen=10)  # 直近10曲の除外リング
    play_stack: list                    # PREV用の再生履歴スタック
```

この2つは役割が違うので分けてある。

- `exclude_history` は「最近かかった曲を選ばない」ためのもの
- `play_stack` は「PREV で戻る先」を覚えるためのもの

PREV で戻った曲は `exclude_history` に**再登録しない**。戻る操作は
「新しく選んだ」わけではないため。

候補が空になったら(ライブラリが11曲未満)全曲から選ぶ保険を入れてある。

### 6.3 曲の投入順

```python
await push_music(filepath)   # 先にキューへ入れる
if skip_current: await skip()  # それから現在曲を打ち切る
await push_intro(intro)
```

逆順にするとキューが空の瞬間ができて無音が入る。

### 6.4 終端検出

0.5秒ごとに音楽キューの残り時間を見て、1.0秒を切ったら次を投入する。
0 まで待つと無音の隙間ができる。

投入直後 4 秒間は検出を止める(`ADVANCE_COOLDOWN`)。リクエスト解決中は
位置が取れず、終端と誤検出するため。

### 6.5 PREV の3秒ルール

```python
elapsed = row["duration_sec"] - client.remaining()
if elapsed >= 3.0:
    restart_current()          # 頭出し
else:
    play_stack.pop()           # 前の曲へ
```

音楽プレイヤーの慣習に合わせたもの。頭出しは**同じファイルを push して skip** で
実装している(理由は 8.6 節)。

---

## 7. 表示系

### 7.1 ブラウザは音を鳴らさない

紹介音声は Liquidsoap がミックスして出しているので、**ブラウザで再生すると二重になる**。
表示系は viseme のタイムラインだけを受け取って口を動かす。

このため、既存 AIassistant の実装(`audioCtx.currentTime` を基準に viseme を
並べる)は使えない。音を鳴らさない以上 AudioContext の時計がないので、
**`performance.now()` を基準**にスケジュールを組んでいる。

```javascript
const base = performance.now() + (msg.delay_ms ?? 0);
for (let i = 0; i < visemes.length; i++) {
  const t0 = base + vtimes[i];
  items.push({ time: t0,                name: visemes[i] });
  items.push({ time: t0 + vdurations[i], name: "sil" });
}
```

`delay_ms`(既定300ms)は push から実際に音が出るまでのズレの補正。

### 7.2 WebSocket プロトコル

HTTP と WebSocket を同じポート(8765)で出す。aiohttp が両方を扱う。

```
表示系 → service:  {"cmd": "next" | "prev" | "pause" | "play"}

service → 表示系:
  {"event": "now_playing", "title":…, "artist":…, "filepath":…,
                           "background": "/artwork/….jpg" | "/images/….jpg" | null}
  {"event": "next_up",     "title":…, "artist":…}
  {"event": "state",       "phase": "GENERATING" | "NEXT_READY"}
  {"event": "paused",      "value": true|false}
  {"event": "intro",       "text":…, "visemes":[…], "vtimes":[…],
                           "vdurations":[…], "delay_ms":300}
  {"event": "credit",      "text": "VOICEVOX:波音リツ"}
  {"event": "restart",     "elapsed": 7.5}
  {"event": "error",       "detail": …}
```

- **接続直後にスナップショットを送る**(now_playing / state / paused / next_up)。
  途中から開いたブラウザでも曲名表示と NEXT の活性が正しくなる
- `now_playing` を `intro` より先に送る。逆だと紹介が始まっているのに
  前の曲名が出たままになる
- viseme 配列は数百要素あるので、コンソールには件数だけ出して WS には全部送る

### 7.3 背景は WebGL ではなく DOM 側で持つ

`scene.background` にテクスチャを入れると、アスペクト比の違う画像を
cover 相当で収めるのに `repeat` / `offset` の計算が要る。
**canvas を `alpha: true` のまま透過させ、その後ろに `#bg` を敷いて
CSS の `background-size: cover` に任せる**方が確実で短い。

背景の決定はサーバ側で行い、`now_playing` に URL を載せる。

1. 曲に埋め込まれたアートワーク → `/artwork/{hash}.jpg`
2. 無ければ `images/` からランダム → `/images/xxx.jpg`
3. `images/` も無ければ `null` → 単色 `#12121c`

アートワークは 300〜500px 角のものが多く、全画面に伸ばすと粗が出る。
`img.naturalWidth` を見て**低解像度なら強めに(7px)、大きい画像なら軽く(2px)**
ぼかす。ぼかしは粗を隠すと同時に、手前の文字を読みやすくする効果もある。
`transform: scale(1.06)` はぼかしで画面の縁が透けるのを防ぐため。

画像は読み込み完了を待ってから差し替える。先に URL を入れると
切り替えの瞬間に地の色が一瞬見える。

### 7.4 AIassistant を参照しない

three.js / three-vrm は `web/libs/` にコピーして同梱している。
既存の AIassistant プロジェクトのパスを参照すると、片方の変更でもう片方が壊れる。

---

## 8. 詰まった点と解決策

実装中に踏んだ問題を、原因と対処のセットで残す。
**仕様書どおりに書いたら動かなかったもの**が多い。

### 8.1 macOS の AppleDouble ファイルが15件のエラーになった

**症状**: 15曲のライブラリをスキャンしたら、15件の「未対応フォーマット」エラー。

**原因**: macOS からコピーした際の `._曲名.mp3`(リソースフォーク)を
音声ファイルとして開こうとしていた。

**対処**: `.` で始まるファイルを走査対象から除外。

```python
return not path.name.startswith(".")
```

### 8.2 日本語タグが文字化けした

**症状**: 「松田聖子 / チェリーブラッサム」が
`\x8f¼\x93c\x90¹\x8eq` / `\x83`\x83F\x83\x8a\x81[…` になる。

**原因**: 古い ID3 が CP932 のバイト列を latin-1 として宣言しており、
mutagen が宣言どおり latin-1 でデコードしていた。

**対処**: 条件を3つ揃えたときだけ CP932 で再デコードする。

```python
def demojibake(s):
    if any(ord(c) > 0xFF for c in s): return s          # (1) 全文字が latin-1 範囲
    high = sum(1 for c in s if ord(c) >= 0x80)
    if high < 2 or high / len(s) < 0.4: return s        # (2) 上位バイトが4割以上
    decoded = s.encode("latin-1").decode("cp932")
    return decoded if _JP_CHARS.search(decoded) else s  # (3) 日本語になる
```

条件 (2) が重要で、これがないと `"Björk"` のような正当な latin-1 文字列を
壊してしまう(0xF6 は CP932 の先行バイトなので、無条件に変換すると漢字化する)。

### 8.3 m4a の作曲者が全部 None だった

**症状**: m4a 6曲すべてで `composer` が取れない。

**原因**: `mutagen` の `EasyMP4` は `composer` キーに対応していない
(`composersort` はあるのに)。

**対処**: 生アトム `©wrt` へのフォールバックを実装。フォーマット判定は
`type(easy).__name__` で行うが、**`"MP4"` ではなく `"EasyMP4"`** が返る点に注意
(ここを間違えてフォールバックが一度も発火していなかった)。

### 8.4 `B'z` が MusicBrainz で見つからなかった

**症状**: スコア100の候補が返っているのに `not_found` になる。

**原因**: MusicBrainz 側の表記が `B’z`(U+2019 のアポストロフィ)で、
ID3 の `B'z`(U+0027)と一致しない。**NFKC 正規化では畳まれない。**

**対処**: 約物の対応表を追加。

```python
_PUNCT_MAP = str.maketrans({
    "‘": "'", "’": "'", "‛": "'",   # ‘ ’ ‛
    "“": '"', "”": '"',                # “ ”
    "–": "-", "—": "-", "―": "-",   # – — ―
})
```

長音符 `ー`(U+30FC)は日本語の音の一部なので**絶対に触らない**。

同様の表記差(`マイケル・ジャクソン` ↔ `Michael Jackson`)に対しては、
候補ゼロのときだけ `get_artist_by_id(includes=["aliases"])` でエイリアスを
確認するフォールバックを入れた。スコア閾値を下げないので誤マッチは増えない。

### 8.5 `max_tokens=150` で文が途中で切れた

**症状**: 生成コメントが「ぜひ耳を傾けてください」と句点なしで終わる。

**原因**: 100文字程度の日本語が150トークンに収まらない。

**対処**: `max_tokens` を 256 に。長さは文字数側のトリムで担保する。
あわせて、末尾が句点でなければ最後の句点まで戻す保険を入れた。

### 8.6 Liquidsoap 2.4 で仕様どおりに動かなかった4点

もっとも時間を使った領域。**バージョン間の API 差が大きい**。

#### (a) `file://` URI + パーセントエンコードは失敗する

仕様には「日本語ファイル名は `file://` URI 化 + エスケープして投入」とあったが、
実際は逆だった。

| 投入方法 | 結果 |
|---|---|
| `music.push /path/to/曲 名.mp3` | `Prepared "…" (RID 1)` ✓ |
| `music.push file:///path/to/%E6%9B%B2…` | リクエストが無言で消える(`request.trace` で "No such request") |

`push` は行末までをまるごと URI として扱うので、**絶対パスをそのまま渡せば
スペースも日本語も通る**。`%20` は解釈されない。

#### (b) 出力に `.stop` / `.start` / `.status` がない

1.x にはあった。仕様の「icecast出力とpulseaudio出力の両方を同時に stop する」は
2.4 では実行不能。→ 5.2 節の switch 方式に変更した。

#### (c) `music.remaining` がない

`remaining` は**出力側にしかない**。しかも出力の `remaining` は「いま出力している
トラック」を指すので、intro でダッキング中は **intro の残り時間**を返してしまい、
曲の終端検出に使えない。

**対処**: `radio.liq` に音楽キュー専用のコマンドを生やした。

```liquidsoap
server.register(namespace="music", "pos",
  fun (_) -> "#{source.remaining(music_queue)}")
```

#### (d) 後方シークができない(戻り値が嘘をつく)

`source.seek(music_queue, -30)` は `-30.0` を返すが、実際には位置が動かない。
前方シークは正常に効く。

| 操作 | 戻り値 | 実際の remaining の変化 |
|---|---|---|
| `seek +60` | `60.0` | 313.26 → 253.05(正確に60秒減)✓ |
| `seek -30` | `-30.0` | 250.91 → 250.82(**変化なし**)✗ |

出力側の `radio_local.seek -10` は `Seeked 0.00` と正直に返す。

**対処**: PREV の頭出しは「同じファイルを push して skip」で実装した。

### 8.7 `source.elapsed` は再生位置ではない

**症状**: 頭出ししたのに経過秒が減らない。

**原因**: `source.elapsed` は「トラック開始からの実時間」で、シークしても
頭出ししても増え続ける。

**対処**: 再生位置は **DBの `duration_sec` − 残り秒** で求める。

### 8.8 自動送りが永久に発火しなかった

**症状**: 曲が終わっても次に進まない。ログにもエラーが出ない。

**原因**: `music.queue` を見たら `[27 29]` と2件残っていた。
**Liquidsoap は program_service より長生きする**ので、サービスを再起動するたびに
前回 push した曲がキューに残り、曲が終わってもキューが空にならない。
→ 残り時間が 0 にならない → 終端検出が発火しない。

**対処**: `start()` の先頭で両キューを掃除する。

```python
await asyncio.to_thread(self.client.flush_queues)   # music/dj_intro に flush_and_skip
```

デバッグ中に何度も再起動していたせいで表面化した問題だが、
実運用でも再起動すれば必ず起きる。

### 8.9 sqlite3 の接続がスレッドを跨げない

**症状**: `SQLite objects created in a thread can only be used in that same thread`

**原因**: 紹介文の生成をブロッキングさせないため `asyncio.to_thread` に逃がしたが、
その中で DB を引いていた。

**対処**: `check_same_thread=False` で黙らせるのではなく、
**行をメインスレッドで取得して渡す**設計に変えた。

```python
def build_intro(self, row) -> Path:   # filepath ではなく row を受ける
```

DB アクセスをイベントループのスレッドに閉じ込める方が、あとから壊れにくい。

### 8.10 NEXT 連打がすり抜けた

**症状**: 生成中は NEXT を無効にしているはずなのに、0.3秒間隔で2回押すと
両方通ってしまう。

**原因**: 2つあった。

1. `GENERATING` への遷移を `create_task` した非同期タスクの中で行っていたため、
   タスクが走り出す前に届いた NEXT がまだ `NEXT_READY` を見ていた
2. フェーズ判定がロックの**外**にあったため、先行する NEXT が処理中でも
   判定だけ先に通ってしまった

**対処**: 遷移を `_advance` 内で同期的に行い、判定をロックの内側に移した。

```python
async def cmd_next(self):
    async with self._advancing:              # 判定もロックの中で
        if self.phase != NEXT_READY: return "生成中です(NEXTは無効)"
```

**なお、この症状の切り分けにはログのタイムスタンプが決定的だった。**
最初は「拒否されていない」と見えたが、実際は**キャッシュヒットで生成が同一秒に
完了**しており、2発目が届いた時点では正当に `NEXT_READY` に戻っていただけだった。
本当のバグを見るにはキャッシュを空にして再現させる必要があった。

### 8.11 `pgrep -f` が起動元の端末を巻き込んだ

**症状**: `stop_all.sh` を実行したら、実行していたシェルごと落ちた。

**原因**: `pgrep -f "scripts/program_service.py"` は、**そのパターンを引数に
含んでいるだけの無関係なシェル**にもマッチする。

**対処**: プロセス名(comm)とコマンドラインの**両方**が一致したものだけを止める。

```bash
for pid in $(pgrep -x "$comm"); do
    [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
    cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline")
    [[ "$cmdline" == *"$pat"* ]] && pids+=("$pid")
done
```

`pgrep -x` はプロセス名の完全一致なので、bash がマッチすることはない。

### 8.12 icecast2 は tmux セッション終了では落ちない

**症状**: `tmux kill-session` の後も Icecast だけ残る。

**原因**: デーモン化して親から切り離れるため、SIGHUP が届かない。

**対処**: `stop_all.sh` の残存プロセス停止(8.11 の仕組み)が実際に必要。
停止ログに `停止: Icecast (pid=…)` として出る。

### 8.13 ポートが既存プロジェクトと衝突した

Icecast の既定 8000 は、表示系として流用する予定だった three-vrm が使っていた。
**8100 に変更**し、`settings.toml` を唯一の情報源にして
`start_all.sh` もそこから読むようにした。

---

## 9. 性能実測値

gfx1151 (Ryzen AI Max+ 395) / Qwen3.6-35B-A3B-UD-Q4_K_XL。

| 処理 | 時間 |
|---|---|
| 紹介文生成 (Qwen, thinking無効) | 1.2〜1.5 秒 |
| 音声合成 (VOICEVOX) | 0.25 秒 |
| キャッシュヒット | 0 秒 |
| **合計(初回)** | **2秒未満** |

曲の再生時間内に余裕で終わるため、`GENERATING` によるグレーアウトは
実質的に見えない。全曲キャッシュ済みなら NEXT を連打しても止まらない。

MusicBrainz の補完は 1曲あたり2〜3リクエスト、1リクエスト/秒制限のため
15曲で約40秒。数百曲なら十数分。

生成される intro は 80〜115文字、音声にして約20秒。

### llama-server の呼び方

```python
payload = {
    ...,
    "chat_template_kwargs": {"enable_thinking": False},
}
```

**これを付けないと** Qwen3 系は既定で thinking を吐き、`reasoning_content` に
`max_tokens` を食われて `content` が空で返る。

---

## 10. 既知の制限

- **紹介文は曲ごとに固定**。`track_hash` が filepath 基準なので、同じ曲は毎回
  同じ紹介になる。PREV の即応性と引き換え
- **`library.db` は環境間で共有できない**。`filepath` を絶対パスで持つため
- **Icecast のパスワードは3箇所**に書く必要がある。Liquidsoap と Icecast は
  TOML を読まないため
- **後方シークができない**ので、任意位置へのシーク機能は作れない
  (頭出しは push + skip で代用)
- dj_intro キューが空になるとき
  `Source created multiple tracks in a single frame!` が出るが、動作に影響はない
