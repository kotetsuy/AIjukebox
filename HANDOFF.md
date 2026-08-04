# HANDOFF.md — AIjukebox: ローカルAIラジオDJ

## プロジェクト概要

ローカル音楽ライブラリをAI DJ(波音リツ)の紹介コメント付きで再生・配信するシステム。

- 曲情報はID3タグ + MusicBrainz補完(SQLiteキャッシュ)
- DJコメントはQwen 3.6(llama-server)で生成、VOICEVOXで音声合成
- Liquidsoapでダッキングミキシング、ローカル再生 + Icecastネットラジオ配信(スマホ受信)
- 表示系は既存AIassistantと同様(VRMアバター + リップシンク + トランスポートボタン)

### 動作環境

- Ubuntu 24.04 / ROCm環境(GMKtec EVO X2, Strix Halo)
- llama-server(Qwen3.6-35B-A3B)常駐済み・VOICEVOX ENGINE導入済み(既存AIzunda資産流用)
- **Liquidsoap 2.x 系を対象とする**(1.4系とAPI名が大きく異なる。実装時に公式ドキュメントで関数名を必ず確認すること)
- Icecastサーバーが別途必要(下記参照)

---

## ディレクトリ構成

```
AIjukebox/
├── library/                    # 音楽ファイル本体(mp3/flac/m4a/ogg)
├── db/
│   └── library.db              # SQLite(tracksテーブル)
├── cache/
│   └── intros/                 # 生成済みintro.wav({track_hash}.wav)
├── scripts/
│   ├── scan_library.py         # ID3抽出→DB upsert(オフライン安全)
│   ├── enrich_mb.py            # MusicBrainz補完(オンライン専用・手動実行)
│   ├── dj_prompt.py            # Qwen 3.6プロンプト構築+呼び出し
│   ├── voicevox_synth.py       # VOICEVOX音声合成ラッパー
│   ├── program_service.py      # 番組進行中枢(選曲・状態機械・WebSocket)
│   └── liquidsoap_client.py    # telnet経由でLiquidsoapを制御
├── liquidsoap/
│   └── radio.liq               # ミキシング・配信定義
├── config/
│   └── settings.toml           # パス・ポート・speaker設定など
└── logs/
    └── program.log             # 生成コメント履歴(recent_intros復元にも使用)
```

---

## 1. 曲情報パイプライン

### DBスキーマ

**重要: ID3由来とMusicBrainz由来のカラムを分離する。**
`scan_library.py`の再実行でMB補完結果が上書き消去されるのを防ぐため。

```sql
CREATE TABLE IF NOT EXISTS tracks (
    filepath TEXT PRIMARY KEY,
    -- ID3由来(scanで毎回上書きしてよい)
    title TEXT, artist TEXT, album TEXT,
    release_date TEXT, genre TEXT, composer TEXT,
    duration_sec INTEGER,
    -- MusicBrainz由来(scanでは絶対に触らない)
    mbid TEXT,
    mb_release_date TEXT,
    -- 管理
    enrichment_source TEXT DEFAULT 'id3_only',  -- 'id3_only' | 'musicbrainz' | 'not_found'
    last_scanned TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

読み出し時の発売年は `COALESCE(mb_release_date, release_date)` で解決。

### scan_library.py(オフライン安全・冪等)

- `mutagen`(`easy=True`)でmp3/flac/m4a/oggのタグを統一抽出
- `AIjukebox/library/` を再帰走査し、ID3由来カラムのみupsert
- MB由来カラム(`mbid`, `mb_release_date`)と`enrichment_source`は**更新しない**
- ネットワークアクセスなし。何度実行してもよい
- 注意: EasyMP4(m4a)はキー対応範囲が狭く`composer`等が取れない場合あり。フォーマット別の取得可否を実装時に確認

### enrich_mb.py(オンライン専用・明示実行)

- `musicbrainzngs`使用。User-Agent設定必須、**1req/秒制限厳守**(`time.sleep(1.1)`)
- 対象選択ロジック(--forceバグ修正済み仕様):

```python
if force:
    query = "SELECT filepath, title, artist FROM tracks"
else:
    query = ("SELECT filepath, title, artist FROM tracks "
             "WHERE enrichment_source NOT IN ('musicbrainz') "
             "OR enrichment_source IS NULL")
```

- 検索前にアーティスト名・曲名をNFKC正規化(表記揺れ対策)
- **limit=1即採用は禁止**。`ext:score >= 90` かつアーティスト名の一致確認を挟み、満たさなければ`not_found`
- 成功時: `mb_release_date`, `mbid`更新、`enrichment_source='musicbrainz'`
- 失敗時: `enrichment_source='not_found'`(次回--forceなしでは再試行されない)
- オプション: `--force`(全曲再補完)、`--limit N`
- 規模想定: 数百曲(初回フル実行5〜10分程度)

---

## 2. 選曲ロジック(Python主導)

**選曲権はLiquidsoapではなくPythonが持つ**(DJ紹介と実再生曲の一致を保証するため)。

```python
class TrackSelector:
    exclude_history: deque(maxlen=10)  # 直近10曲の除外リング
    play_stack: list                    # PREV用の再生履歴スタック

    def pick_next(self) -> str:
        # DBの全filepathから exclude_history を除いてランダム選択
        # 候補が空(ライブラリが11曲未満)なら全曲から選ぶ(保険)
        # 選択した曲を exclude_history に追加
```

- `exclude_history`(除外用)と`play_stack`(PREV用)は役割が違うため分離
- PREVで戻った曲は`exclude_history`に再登録しない(NEXT/自然遷移で選ばれた曲のみ積む)

---

## 3. 状態機械と先読み生成

**次曲の決定とintro生成は、現在曲の再生開始と同時に行う**(NEXT即応のため)。

```
曲N 再生開始
  ├─ 即座に: pick_next() で曲N+1 確定
  ▼
[GENERATING]  → 表示系へ通知: NEXTボタン グレーアウト
  │  1. キャッシュ確認: cache/intros/{track_hash}.wav があればスキップして即READY
  │  2. なければ Qwen 3.6でintroテキスト生成 → validate → VOICEVOX合成 → キャッシュ保存
  ▼
[NEXT_READY]  → 表示系へ通知: NEXTボタン 有効化
  ├─ NEXT押下  → music.skip → intro N+1 + 曲N+1 投入 → 先頭へ
  └─ 自然終了  → intro N+1 + 曲N+1 投入 → 先頭へ
```

- track_hashは filepath の安定ハッシュ(sha1等)
- 生成は通常、曲の再生時間内に余裕で完了するため、グレーアウトが見えるのは実質NEXT連打時のみ
- キャッシュヒット時(PREV含む)はQwen/VOICEVOXを再実行しない → PREVは常に即応

---

## 4. トランスポート制御

| ボタン | 動作 |
|---|---|
| NEXT | `NEXT_READY`時のみ有効。現在曲をskipし次曲へ。`GENERATING`中はグレーアウト |
| PREV | **3秒ルール**: 再生位置3秒以降なら現在曲を頭出し、3秒未満なら`play_stack`から前曲へ。前曲introはキャッシュ再利用 |
| PAUSE/PLAY | 下記参照 |

### PAUSE実装の注意(重要)

Liquidsoapはプル型エンジンのため素直な「一時停止」は存在しない。
**icecast出力とpulseaudio出力の両方を同時にstop**することでソース消費を止め、再生位置ごと凍結する方式を採る。

- telnet: `radio_ice.stop` + `radio_local.stop` → PLAY時に両方`start`
- **片方だけ止めるともう片方がソースを進め続けるため、必ず両方同時に操作すること**
- 制約: PAUSE中はIcecastリスナーが切断され、スマホ側は再接続が必要(個人利用のため許容)

---

## 5. Qwen 3.6 DJコメント生成

### システムプロンプト

```
あなたはローカルAIラジオのDJです。次にかける曲を紹介するコメントを生成してください。

# ルール
1. 与えられた情報(曲名・アーティスト・発売年・アルバム・ジャンル)のみを事実として扱う
2. 与えられていない具体的事実(タイアップ作品、受賞歴、エピソードなど)は絶対に断定的に言わない
   - 悪い例: 「ドラマ○○の主題歌として話題になりました」(情報にない場合)
   - 良い例: 曲の雰囲気や聴きどころなど、一般的な表現に留める
3. 出力は音声合成で読み上げるため、記号(*, #, 括弧の多用)や改行を使わない
4. 1コメントは60〜100文字程度、口語で自然に
5. 前回までのコメントと同じ言い回しの繰り返しを避ける
6. 発売年やジャンルなどの情報が欠けている場合、無理に触れず自然に省略する

# 出力形式
コメント本文のみを出力する。前置きや説明は不要。
```

### ユーザープロンプト

- 曲情報(title/artist/発売年/album/genre)を存在するものだけ列挙
- 直近3件のコメント(`recent_intros`リングバッファ、logs/program.logから復元可)を「この言い回しは避けてください」として添付

### enrichment_source別の踏み込み方

| source | 方針 |
|---|---|
| musicbrainz | 発売年など積極的に言及OK |
| id3_only | タグ情報はそのまま信用してOK |
| not_found | 曲名・アーティスト名のみ、雰囲気重視の短文 |

### 推論設定

- Thinking mode: **無効**(レイテンシ優先、AIzunda知見)
- max_tokens: 150 / temperature: 0.7〜0.8 / 非ストリーミング
- 生成後バリデーション: 120文字トリム(文末「。」で切る)+ TTS誤読対策の記号除去

---

## 6. TTS(VOICEVOX)

- 話者: **波音リツ / ノーマル**(想定id=9)
- **speaker_idはハードコード禁止**。起動時に `GET http://localhost:50021/speakers` から
  name="波音リツ", style="ノーマル" で動的解決してキャッシュ(バージョン間のID変動対策)
- 出力: `cache/intros/{track_hash}.wav`
- 音声出力は表示系(リップシンク駆動)にも分岐(既存AIzunda同様の構造)

---

## 7. Liquidsoap(radio.liq)

Liquidsoapは**薄いミキシング・出力層**に徹する。選曲ロジックは持たない。

```liquidsoap
music_queue = request.queue(id="music")     # Pythonが次曲をpush
dj_queue    = request.queue(id="dj_intro")  # Pythonがintro.wavをpush

# ダッキング: introが入ると音楽を0.15倍に下げ、終了後復帰
radio = smooth_add(normal=music_queue, special=dj_queue,
                   duration=1.5, p=0.15)

output.icecast(%mp3(bitrate=128),
  host="localhost", port=8000, password="...",
  mount="radio.mp3", id="radio_ice", radio)

output.pulseaudio(id="radio_local", radio)

# telnetサーバー有効化(Pythonからの制御用、ポート例: 1234)
```

- `fallback(track_sensitive=false)`は**使用しない**(曲を途中でぶった切る誤動作の原因)
- 関数名は2.x系ドキュメントで実装時に要確認(`smooth_add`の引数名等はバージョンで揺れる)

### telnetキュー投入の注意

- 日本語ファイル名・スペースを含むパスは素のpushで壊れる可能性あり
- `file://` URI化 + エスケープして投入すること

---

## 8. Icecastサーバー(構成に必須)

`output.icecast`はIcecastサーバーへの**クライアント接続**であり、サーバー本体は別途必要。

- `sudo apt install icecast2` → `localhost:8000`で起動
- スマホ受信: LAN内で `http://<NucBoxのIP>:8000/radio.mp3` を開く

---

## 9. 表示系(AIassistant)連携 — WebSocket

`program_service.py`がWebSocketサーバーを持つ(ポートはsettings.tomlで定義)。

```
表示系 → program_service:
  {"cmd": "next"} / {"cmd": "prev"} / {"cmd": "pause"} / {"cmd": "play"}

program_service → 表示系:
  {"event": "state", "phase": "GENERATING"}   # NEXTグレーアウト
  {"event": "state", "phase": "NEXT_READY"}   # NEXT有効化
  {"event": "now_playing", "title": ..., "artist": ...}  # 曲名表示用
```

- リップシンクは従来どおりVOICEVOX音声出力の分岐で駆動(WebSocketは制御・表示情報のみ)
- 表示系UI: 既存AIassistant + PREV / PAUSE / PLAY / NEXT ボタン追加

---

## 10. 起動シーケンス

```bash
./start_all.sh     # 全部起動して Chrome で表示系を開く
./stop_all.sh      # 全部停止

./stop_all.sh --keep-llama      # llama-server は残す(再ロードを省く)
./stop_all.sh --keep-voicevox   # VOICEVOX コンテナは残す
tmux attach -t aijukebox        # 各サービスのログ (Ctrl-b d でデタッチ)
```

`start_all.sh` は AIassistant と同じ tmux 方式。ウィンドウは
`voicevox / llama / icecast / liquidsoap / program` の5つで、前のサービスが
上がってから次へ進む(llama のモデルロード待ちは最大600秒)。
**ポートは `config/settings.toml` を読んで使う**ので、設定を変えれば
スクリプトを直さなくても追従する。

`stop_all.sh` の残存プロセス停止は、**プロセス名(comm)とコマンドラインの
両方が一致したものだけ**を止める。`pgrep -f パターン` だけだと、そのパターンを
引数に含むだけの無関係なシェルにも当たり、起動元の端末ごと落ちる(実際に踏んだ)。
なお **icecast2 はデーモン化するので tmux セッション終了では落ちない**。
このフォールバックが実際に必要。

### 手動で起動する場合

ポートは settings.toml が正。

```bash
cd ~/AIjukebox

# 1. VOICEVOX ENGINE (docker)                      :50021
docker start voicevox_engine

# 2. llama-server (Qwen3.6-35B-A3B, Thinking無効)  :8080
ROCM_PATH=/opt/rocm HIP_VISIBLE_DEVICES=0 \
LD_LIBRARY_PATH=/usr/local/lib:/opt/rocm/lib:/opt/rocm/lib/llvm/lib \
~/llama.cpp/build/bin/llama-server \
  -m ~/AIassistant/qwen3.6/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf \
  --host 127.0.0.1 --port 8080 -ngl 99 -c 8192 -fit off &

# 3. Icecast (sudo不要・プロジェクト内設定)        :8100
icecast2 -c config/icecast.xml &

# 4. Liquidsoap (telnet 1234 / icecast へ接続)
liquidsoap liquidsoap/radio.liq &

# 5. 番組進行 + 表示系 (HTTP と WebSocket 同居)    :8765
uv run --no-sync scripts/program_service.py
```

- 表示系: ブラウザで `http://localhost:8765/`
- スマホ受信: `http://<このマシンのIP>:8100/radio.mp3`
- **HSA_OVERRIDE_GFX_VERSION は設定しない**(gfx1151ネイティブビルドなので壊れる)
- 起動順は 1〜4 が先。program_service は Liquidsoap に繋がらないと起動時に止まる

新曲追加時の運用: `scan_library.py`(即時反映可)→ オンライン時に `enrich_mb.py`

---

## 実装フェーズ案(フェーズごとに動作確認)

- **Phase 1**: DBスキーマ + scan_library.py + enrich_mb.py(CLIで単体確認)
- **Phase 2**: dj_prompt.py + voicevox_synth.py(1曲分のintro.wav生成をCLIで確認)
- **Phase 3**: Liquidsoap + Icecast(手動pushで音楽+ダッキング+配信を確認)
- **Phase 4**: program_service.py(選曲・状態機械・自動ループ、telnet統合)
- **Phase 5**: WebSocket + 表示系ボタン(NEXT/PREV/PAUSE/PLAY、グレーアウト)

## Phase 1 実装メモ(実装済み)

`scripts/common.py` / `scan_library.py` / `enrich_mb.py`、`config/settings.toml`、
`pyproject.toml`(uv, Python 3.12)。実行は `uv run --no-sync scripts/xxx.py`。

実ライブラリ(15曲)で確認した結果、設計時に想定していなかった問題への対応を入れた。

### scan_library.py

- **AppleDouble を除外**: macOS由来の `._曲名.mp3` を走査対象外に(`.` 始まりは全て除外)。
  入れないと15曲に対し15件の偽エラーが出る
- **CP932モジバケの復元** (`demojibake`): 古いID3で日本語がlatin-1として読まれる
  (`松田聖子` → `\x8f¼\x93c\x90¹\x8eq`)。上位バイト比率4割以上かつCP932解釈で
  日本語になる場合のみ変換するので、"Björk" のような正当なlatin-1は壊さない
- **m4a の composer**: EasyMP4 は `composer` 非対応(`composersort` しか無い)。
  生アトム `©wrt` へのフォールバックが必須
- `--prune` で実体の消えたファイルの行を削除できる

### enrich_mb.py

- **再生時間で録音を選ぶ**: 同名異録音(ライブ/TVサイズ/リマスター)が同スコア100で
  並ぶため、ID3の `duration_sec` に最も近い録音を優先(許容±5秒)。
  これが無いと「迷子犬と雨のビート」で414秒の別録音を掴み発売年が2022年になる
- **発売日は候補全体の最古を採る**: 検索結果の `release-list` は部分集合。選んだ録音の
  全リリースを引き直した上で、同名候補すべての日付と合わせて最古を採用する
- **エイリアス照合のフォールバック**: 名前不一致で候補ゼロのとき、`get_artist_by_id`
  の `alias-list` と完全一致するかだけ確認する(`マイケル・ジャクソン` ↔ `Michael Jackson`)。
  スコア閾値は下げないので誤マッチは増えない
- 約物の正規化: MBは `B’z`(U+2019)。NFKCでは畳まれないので `normalize_text` で変換。
  長音符 `ー`(U+30FC)は日本語の一部なので触らない
- リクエスト間隔は `throttle()` が前回リクエストからの経過で管理(1曲あたり2〜3リクエスト)
- `not_found` は既定で再試行しない。`--retry-not-found` で対象に含める

### 発売年の解決は COALESCE ではなく「古い方」

`common.resolve_release_year(row)` を使うこと。MusicBrainzはリマスターを別録音として
持つため原盤より新しい日付を返すことがあり(Yesterday→1985、銀河鉄道999→2021)、
ID3側もベスト盤やiTunes購入日に引きずられる。**どちらの誤差も「実際より新しくなる」
方向にしか出ない**ので、両者の古い方を採ると原盤の年に近づく。

実測: 15曲中12曲が正しい発売年、残り3曲もID3相当までは戻る。

## Phase 2 実装メモ(実装済み)

`scripts/dj_prompt.py` / `voicevox_synth.py`。依存に `httpx` を追加。

```bash
uv run --no-sync scripts/voicevox_synth.py --list-speakers
uv run --no-sync scripts/dj_prompt.py --title キセキ --dry-run   # プロンプトのみ
uv run --no-sync scripts/dj_prompt.py --title キセキ --synth     # intro.wav まで
uv run --no-sync scripts/dj_prompt.py --all --no-log             # 全曲の傾向確認
```

### 実測レイテンシ(gfx1151 / Qwen3.6-35B-A3B Q4_K_XL)

| 処理 | 時間 |
|---|---|
| Qwen コメント生成 | 約 1.2〜1.5 秒 |
| VOICEVOX 合成 | 約 0.25 秒 |
| キャッシュヒット | 0 秒 |

**合計2秒未満**。曲の再生時間内に余裕で終わるので、`GENERATING` のグレーアウトは
NEXT連打時以外まず見えない。HANDOFF §3 の想定どおり。

### 実データで判明した問題と対処

- **max_tokens=150 では足りない**: 100文字程度の日本語コメントが150トークンに
  収まらず、文の途中で切れて返る(「ぜひ耳を傾けてください」と句点なしで終わる)。
  256 に変更。長さは `max_chars` 側のトリムで担保する
- **曲名を和訳する**: 「Yesterday」→「昨日」と言い出した。システムプロンプトに
  「曲名とアーティスト名は与えられた表記のまま使う」を明文化して抑止
- **情報にない楽器編成を断定する**: 「ギターとピアノのシンプルな伴奏が特徴」など。
  ルール2の禁止例に楽器・歌詞の内容・制作背景を追加
- **記号がそのまま読まれる**: 「アゲ♂アゲ♂EVERY☆騎士」の ☆♂ など。`validate_intro`
  の除去対象に ☆★♪♬♩♂♀※→←↑↓○●◎△▲▽▼□■◆◇『』【】〈〉《》 を追加
- **末尾が尻切れになる**: 生成が途中で止まった場合に備え、末尾が句点でなければ
  最後の句点まで戻して切る(`_trim_to_sentence`)
- **アルバム名は読み上げない**(方針): ベスト盤名や iTunes の「- Single」「[Disc 1]」が
  多く、DJコメントとして自然にならないためプロンプトに渡さない
- 直近コメントが長いと生成も長くなる傾向がある。`recent=[]` だと 77〜99文字に収まる

### キャッシュ仕様

`cache/intros/{track_hash}.wav` に加えて **`{track_hash}.json` も保存する**
(`text` / `speaker_id` / `accent_phrases`)。表示系のリップシンクは VOICEVOX の
`accent_phrases` から viseme を組み立てる実装(既存 three-vrm/server.py と同じ)なので、
音声だけキャッシュしても PREV 時に口が動かなくなるため。

track_hash は filepath 基準なので、**一度生成した曲のコメントは固定される**。
別の言い回しにしたい場合は該当ファイルを消す。

### llama-server の呼び方

`/v1/chat/completions` に `"chat_template_kwargs": {"enable_thinking": False}` を
必ず付ける。付けないと reasoning_content に max_tokens を食われて content が空で返る
(既存 AIassistant/ttllm/server.py と同じ)。

## Phase 3 実装メモ(実装済み)

`liquidsoap/radio.liq` / `config/icecast.xml` / `scripts/liquidsoap_client.py`。
**Liquidsoap 2.4.0 / Icecast 2.5.0 (Ubuntu resolute の apt)** で検証。

```bash
icecast2 -c config/icecast.xml                       # sudo 不要
liquidsoap liquidsoap/radio.liq
uv run --no-sync scripts/liquidsoap_client.py status
uv run --no-sync scripts/liquidsoap_client.py push-music "library/Queen-Bicycle Race.mp3"
uv run --no-sync scripts/liquidsoap_client.py push-intro cache/intros/xxxx.wav
uv run --no-sync scripts/liquidsoap_client.py pause / play / skip / remaining
```

### 2.4系で HANDOFF の記述が通用しなかった点

- **`file://` URI + エスケープは失敗する**(§7の注意書きは誤り)。`push` は行末までを
  そのまま URI として扱うので、**絶対パスを素で渡せばスペースも日本語も通る**。
  逆にパーセントエンコードすると `%20` が解釈されず、リクエストが無言で消える
  (実測: 素のパス → `Prepared ... (RID 1)`、`file://`+quote → `request.trace` で
  "No such request")
- **出力に `.stop` / `.start` / `.status` が無い**(1.x にはあった)。§4の
  「両出力を同時に stop」は 2.4 では実行不能
- **`music.remaining` が無い**。`remaining` は出力側のみ
  (`radio_local.remaining`)。キューには `.queue` `.push` `.skip`
  `.flush_and_skip` しかない
- `smooth_add` の引数は `duration` で正しい(`delay` は無い)

### PAUSE の実装(方式変更)

出力を止められないので、**上流の switch でソースを pull しない**方式にした。
Liquidsoap はプル型なので、選択されていないソースはフレームを要求されず凍結する。

```liquidsoap
paused = interactive.bool("paused", false)
stream = switch(track_sensitive=false, [(paused, blank()), ({true}, radio)])
```

telnet は `var.set paused = true` / `var.set paused = false`。

- ここでの `track_sensitive=false` は §7 が禁じている「availability で曲を切る」
  用途ではなく、明示フラグの即時反映なので曲をぶった切る誤動作は起きない
- 切り替え点が1箇所なので、**片方の出力だけ止まって位置がずれる問題が構造的に消える**
- **PAUSE中も icecast には無音が流れ続けるので、リスナーが切断されない。**
  §4 が許容していた「スマホ側は再接続が必要」という制約は解消した
- 実測: PAUSE前 167.9秒 → 6秒待機 → 再開後4秒で 163.7秒。停止中は進んでいない
- PAUSE中は出力が blank を引くため `remaining` が取れない(-1 を返す)

### PREV の頭出しには `radio_local.seek`

`radio_local.seek <seconds>`(負値で巻き戻し)が使える。3秒ルールの判定は
`remaining` と DB の `duration_sec` から経過秒を出す。

### Icecast

- **ポートは 8100**。HANDOFF は 8000 としていたが、表示系の three-vrm が 8000 を
  使うため衝突する。`config/icecast.xml` と `settings.toml` の両方で 8100
- `config/icecast.xml` は chroot / changeowner を使わないので **sudo 不要**で起動でき、
  ログもプロジェクト内 (`logs/icecast-*.log`) に出る。`/etc/icecast2` は触らない
- スマホ受信: `http://<このマシンのIP>:8100/radio.mp3`

### ダッキングの実測

一定振幅の440Hzトーンを音楽として流し、intro投入前後の440Hz成分だけを
帯域通過で測った結果:

```
 11s  -36.5 dB   ← 通常
 13s  -48.3 dB   ← ダッキング (約1.5秒でフェード)
 …    -46〜-53 dB
 31s  -44.2 dB   ← 復帰フェード
 34s  -36.5 dB   ← 元に戻る
```

理論値は `p=0.15` で -16.5 dB。測定値が -11〜-16 dB に散るのは、DJ音声の
倍音が440Hz帯域に漏れて底上げされるため。フェード長は `duration=1.5` と一致。

### 無害な警告

dj_intro キューが空になるとき
`Source created multiple tracks in a single frame!` が出るが動作に影響はない。

## Phase 4 実装メモ(実装済み)

`scripts/program_service.py`。選曲・状態機械・自動ループ・Liquidsoap制御。
WebSocket は Phase 5。当面は標準入力からコマンドを受ける。

```bash
uv run --no-sync scripts/program_service.py
# next / prev / pause / play / status / quit
```

### 動作確認済み

| 項目 | 結果 |
|---|---|
| 起動→intro生成→投入→次曲先読み | GENERATING → NEXT_READY を確認 |
| NEXT | 曲を送り、即座に次曲の先読みへ |
| NEXT連打(生成中) | 2発目が `生成中です(NEXTは無効)` で拒否される |
| PREV(3秒未満) | play_stack から前曲へ。intro はキャッシュ再利用で即応 |
| PREV(3秒以降) | 現在曲を頭出し(経過 7.8秒 → 3秒に戻る) |
| PAUSE / PLAY | 位置が完全に凍結(残り 172.493106576 のまま5秒経過) |
| 自然終了の自動送り | 無操作で次曲へ遷移 |

### Liquidsoap 2.4 で追加で判明した制約

- **後方シークができない**。`source.seek(music_queue, -30)` は `-30.0` を返すが
  実際には位置が動かない(前方シーク `+60` は remaining が正確に60減るので、
  戻り値だけが嘘をつく)。出力側の `radio_local.seek -10` は `Seeked 0.00`。
  → **PREV の頭出しは「同じファイルを push して skip」で実装**した
- **`source.elapsed` は再生位置ではない**。トラック開始からの実時間なので、
  シークしても頭出ししても増え続ける。
  → 再生位置は **DBの `duration_sec` − `music.pos`(残り秒)** で求める
- 出力の `remaining` は「いま出力しているトラック」を指すため、intro で
  ダッキング中は intro の残り時間になる。曲の終端検出には使えない。
  → radio.liq に `music.pos` を生やして音楽キューの残りを直接取る

### 起動時にキューを掃除すること(重要)

Liquidsoap は program_service より長生きするため、サービスを再起動すると
**前回 push した曲が音楽キューに残る**。すると曲が終わってもキューが空にならず、
**終端検出が永久に発火しなくなる**(実際にこれで自動送りが動かなかった)。
`start()` の先頭で `flush_and_skip` を両キューに送っている。

### 実装上の注意

- **sqlite3 の接続はスレッドを跨げない**。intro生成は `asyncio.to_thread` で
  逃がすので、`build_intro` には行を渡す。中でDBを引くと
  `SQLite objects created in a thread can only be used in that same thread`
- **`GENERATING` への遷移は `_advance` の中で同期的に行う**。`create_task` した
  `_prepare_next` の中で落とすと、タスクが走り出す前に届いた NEXT が
  まだ `NEXT_READY` を見てしまい連打がすり抜ける。あわせて `cmd_next` の
  フェーズ判定はロックの内側に置く
- 曲の投入は **push → skip の順**。逆だとキューが空の瞬間ができて無音が入る
- `_advance` 直後は `ADVANCE_COOLDOWN`(4秒)の間だけ終端検出を止める。
  リクエスト解決中は位置が取れず、終端と誤検出するため

### キャッシュが効いていると連打しても止まらない

全曲キャッシュ済みだと intro 生成が同一秒で終わるため、NEXT を連打しても
`GENERATING` を踏まずに次々送れる。グレーアウトが見えるのは初回再生時だけ。

## Phase 5 実装メモ(実装済み)

`web/index.html` / `scripts/visemes.py`、および `program_service.py` への
WebSocket + 静的配信の追加。依存に `aiohttp` を追加。

```bash
uv run --no-sync scripts/program_service.py
# → http://localhost:8765/ をブラウザで開く
```

**HTTP と WebSocket は同じポート(8765)で出す**。`/` が表示系、`/ws` が制御、
`/vrm/dj.vrm` がアバター、`/libs/...` が three.js。

### AIassistant は参照しない

three.js / three-vrm は `web/libs/` に**コピーして同梱**した。`~/AIassistant`
以下は読むだけで、参照も変更もしない(パス依存を作ると片方の変更でもう片方が壊れる)。

### 表示系は音を鳴らさない

intro の音声は Liquidsoap がミックスして出しているので、ブラウザは**音声を再生せず
リップシンクだけ**を行う。既存 AIassistant の実装は `audioCtx.currentTime` を基準に
viseme を並べていたが、こちらは音を鳴らさないので AudioContext の時計が無い。
**`performance.now()` を基準に viseme スケジュールを組む**。

viseme は Phase 2 でキャッシュした `{track_hash}.json` の `accent_phrases` から
`scripts/visemes.py` で生成する(変換表は AIassistant/three-vrm と同じ)。
PREV でキャッシュを再利用したときも口が動くのはこのため。

`intro_lipsync_delay_ms`(既定300ms)は push から実際に音が出るまでのズレの補正。

### WebSocket プロトコル

```
表示系 → service:  {"cmd": "next" | "prev" | "pause" | "play"}

service → 表示系:
  {"event": "now_playing", "title":…, "artist":…, "filepath":…}
  {"event": "next_up",     "title":…, "artist":…}
  {"event": "state",       "phase": "GENERATING" | "NEXT_READY"}
  {"event": "paused",      "value": true|false}
  {"event": "intro",       "text":…, "visemes":[…], "vtimes":[…],
                           "vdurations":[…], "delay_ms":300}
  {"event": "restart",     "elapsed": 7.5}
  {"event": "error",       "detail": …}
```

- **接続直後にスナップショットを送る**(now_playing / state / paused / next_up)。
  途中から開いたブラウザでも曲名表示と NEXT の活性が正しくなる
- `now_playing` を `intro` より先に送る。逆だと紹介が始まっているのに前の曲名が
  出たままになる
- viseme 配列は数百要素あるので、コンソールには件数だけ出して WS には全部送る

### 動作確認済み(実ブラウザ)

Chrome で `http://localhost:8765/` を開いて確認:

- dj.vrm が読み込まれ、待機モーション付きで表示される。コンソールエラーなし
- 曲名 / アーティスト / `next ▸ …` が表示され、NEXT で更新される
- intro 再生に合わせて**口が動き**、字幕が出る
- PAUSE で ▶ に変わり、再押下で ⏸ に戻る(サービス側ログにも `ws> pause` が届く)

### カメラ位置

`camera.position.set(0, 1.28, 2.4)` / `lookAt(0, 1.18, 0)`。既存 three-vrm の
バストアップ設定(z=1.2)のままだと顔のアップになり、トランスポートボタンが
体に重なって見づらい。

## リポジトリに入れないもの

`.gitignore` 参照。個人情報や環境依存の値をコードに直書きしない方針。

- **`config/settings.local.toml`**(git管理外)が `settings.toml` を再帰マージで
  上書きする。MusicBrainz の連絡先メールなど、リポジトリに入れたくない値はここへ。
  雛形は `config/settings.local.toml.example`。`settings.toml` 側は
  `CHANGE_ME@example.com` のまま
- **`library/`**(音楽ファイル本体・著作物)、**`cache/`**(生成音声)、
  **`logs/`**、**`vroid/*.vrm`**(16MBのバイナリ、配布条件はモデル次第)
- **`db/`** — `library.db` は `filepath` を絶対パスで持つので環境間で共有できない。
  新しい環境では `scan_library.py` を実行し直す
- `config/icecast.xml` の `logdir` は**相対パス**(`logs`)。絶対パスを書くと
  そのマシンでしか動かない。プロジェクトルートから起動すること
- Icecast のパスワードは Icecast 既定のプレースホルダ `hackme` のまま。LAN内限定の
  前提。外に出すなら `config/icecast.xml` / `settings.toml` の `[icecast]` /
  `liquidsoap/radio.liq` の**3箇所**を揃えて変える(Liquidsoap と Icecast は
  TOML を読まないので共有できない)

## 既知の注意点まとめ

- Liquidsoap 1.4系/2.x系のAPI差異 → 2.x前提、実装時にドキュメント確認
- PAUSE中のIcecastリスナー切断(仕様として許容)
- MB検索の誤マッチ対策(score閾値 + アーティスト一致確認)
- m4aのタグ取得制限(EasyMP4)
- speaker_idのバージョン変動 → 動的解決
- scan再実行でMBデータを消さない(カラム分離で担保)
