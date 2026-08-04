# AIjukebox — ローカルAIラジオDJ

手持ちの音楽ライブラリを、AI DJ(波音リツ)の紹介コメント付きで流すローカルラジオ。
曲が変わるたびに LLM が紹介文を書き、音声合成して、曲の頭にかぶせて読み上げる。
VRM アバターが口を動かし、同じ音がネットラジオとして LAN に配信されるので
スマホでも聴ける。

すべてローカルで完結する。曲情報の補完(MusicBrainz)だけがオンライン処理で、
これも手動実行の別コマンドになっている。

For English, see [README.md](README.md).

```
        ┌──────────────┐
        │ library/*.mp3│
        └──────┬───────┘
               │ ID3タグ + MusicBrainz
        ┌──────▼───────┐        ┌─────────────┐
        │  library.db  │───────▶│ Qwen3.6     │ 紹介コメント生成
        └──────────────┘        └──────┬──────┘
                                       │
                                ┌──────▼──────┐
                                │  VOICEVOX   │ 波音リツ
                                └──────┬──────┘
                                       │ intro.wav
        ┌──────────────────────────────▼──────┐
        │  Liquidsoap  曲 + introをダッキング  │
        └───────┬──────────────────┬──────────┘
                │                  │
        ┌───────▼──────┐   ┌───────▼────────┐
        │ スピーカー   │   │ Icecast :8100  │──▶ スマホ
        └──────────────┘   └────────────────┘

        program_service.py が選曲・状態管理・全体の制御を持つ
                     └─▶ 表示系 :8765 (VRM + 操作ボタン)
```

---

## 1. 動作環境

開発・確認した環境。

| | |
|---|---|
| OS | Ubuntu 26.04 (resolute) |
| GPU | AMD Ryzen AI Max+ 395 / Radeon 8060S (gfx1151, 48GB VRAM) |
| ROCm | 7.14.0 (`/opt/rocm`) |
| Python | 3.12 (uv 管理) |
| Liquidsoap | 2.4.0 |
| Icecast | 2.5.0 |

Liquidsoap は **2.x 系が必須**。1.4 系とは API 名が大きく異なる。

---

## 2. 事前に必要なもの

### 2.1 apt パッケージ

```bash
sudo apt install liquidsoap icecast2 tmux ffmpeg
```

Icecast のインストール時にパスワード等を聞かれても、そのまま既定で進めてよい。
本プロジェクトは `/etc/icecast2` を使わず、`config/icecast.xml` を使う。

### 2.2 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2.3 VOICEVOX ENGINE (docker)

```bash
docker run -d --name voicevox_engine --restart unless-stopped \
  -p 50021:50021 voicevox/voicevox_engine:cpu-ubuntu20.04-latest
```

**話者「波音リツ / ノーマル」が必要。** speaker_id は起動時に
`GET /speakers` から名前で引くので、VOICEVOX のバージョンが変わっても追従する。

### 2.4 llama.cpp と Qwen3.6

llama.cpp を gfx1151 向けにビルドし、`llama-server` を用意する。
モデルは `Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf`(約22GB)を想定。

> **重要:** `HSA_OVERRIDE_GFX_VERSION` は設定しないこと。
> gfx1151 ネイティブビルドなので、arch を上書きすると動かなくなる。

### 2.5 VRM モデル

アバター用の `.vrm` ファイルを1つ用意する。リポジトリには含まれない
(サイズが大きく、配布条件がモデルごとに異なるため)。

---

## 3. セットアップ

### 3.1 クローンと依存インストール

```bash
git clone https://github.com/kotetsuy/AIjukebox.git
cd AIjukebox
uv sync
```

`uv sync` が `.venv` を作り、mutagen / musicbrainzngs / httpx / aiohttp を入れる。

> `uv run` ではなく **`uv run --no-sync`** を使うこと。素の `uv run` は
> 実行のたびに同期をかけるため、環境によっては入れ直しが走る。

### 3.2 個人設定

メールアドレスなどリポジトリに入れたくない値は、git 管理外の
`config/settings.local.toml` に書く。`config/settings.toml` を
**キー単位で再帰的に上書き**するので、変えたいものだけ書けばよい。

```bash
cp config/settings.local.toml.example config/settings.local.toml
```

```toml
# config/settings.local.toml
[musicbrainz]
# MusicBrainz は User-Agent に連絡先を要求する(規約)
contact = "you@example.com"
```

MusicBrainz を使わないなら、この設定は省いてよい(`enrich_mb.py` を
実行しなければ参照されない)。

### 3.3 音楽ファイルとアバターを置く

```bash
cp -r /path/to/music/*.mp3 library/
cp /path/to/avatar.vrm vroid/dj.vrm

# 任意: アートワークが無い曲の背景に使う画像
mkdir -p images && cp /path/to/*.jpg images/
```

対応形式は mp3 / flac / m4a / ogg / opus。サブディレクトリも再帰的に読む。
ファイル名は日本語やスペースを含んでいてよい。

### 3.4 ライブラリのスキャン

```bash
uv run --no-sync scripts/scan_library.py
```

ID3 タグを読んで `db/library.db` を作る。**オフラインで完結し、何度実行しても安全。**

```
走査: /home/you/AIjukebox/library
  取り込み 15 曲 / 失敗 0 件
DB合計: 15 曲
  id3_only: 15
```

### 3.5 発売年の補完(任意・オンライン)

```bash
uv run --no-sync scripts/enrich_mb.py
```

MusicBrainz で発売日を引き直す。DJ が「1978年の曲です」と言えるようになる。
**1リクエスト/秒の制限を守る**ので、数百曲だと十数分かかる。

```
対象 15 曲 (min_score=90, interval=1.1s)
[1/15] Queen - Bicycle Race  → cee0c145-… date=1979 score=100
...
完了: 補完 12 / not_found 3 / skip 0 / エラー 0
```

`--dry-run` で DB を更新せず結果だけ見られる。`--limit N` で件数を絞れる。
一度 `not_found` になった曲は再実行しても飛ばされる(`--retry-not-found` で対象に戻る)。

`scan_library.py` を再実行しても、ここで補完した結果は消えない
(ID3 由来カラムと MusicBrainz 由来カラムを分けてあるため)。

---

## 4. 起動

```bash
./start_all.sh
```

tmux セッション `aijukebox` の中で、以下を順に立ち上げる。
前のサービスが応答するまで待ってから次に進む。

| # | サービス | ポート |
|---|---|---|
| 1 | VOICEVOX ENGINE (docker) | 50021 |
| 2 | llama-server (Qwen3.6) | 8080 |
| 3 | Icecast | 8100 |
| 4 | Liquidsoap (telnet 制御) | 1234 |
| 5 | program_service(番組進行 + 表示系) | 8765 |

最後に Chrome で表示系が開く。

```
=========================================================================
 AIjukebox が起動しました。

   表示系      : http://localhost:8765/   ← Chrome で自動オープン
   ネットラジオ: http://192.168.0.20:8100/radio.mp3
=========================================================================
```

- **表示系**: ブラウザで `http://localhost:8765/`
- **スマホから聴く**: 同じ LAN で `http://<このマシンのIP>:8100/radio.mp3` を開く

初回は llama-server のモデルロードに時間がかかる(最大600秒待つ)。

### ログを見る

```bash
tmux attach -t aijukebox     # Ctrl-b d でデタッチ
```

`voicevox / llama / icecast / liquidsoap / program` の5ウィンドウがある。

### 停止

```bash
./stop_all.sh

./stop_all.sh --keep-llama      # llama-server は残す(再ロードが重いので)
./stop_all.sh --keep-voicevox   # VOICEVOX コンテナは残す
```

---

## 5. 使い方

表示系にボタンが3つある。

| ボタン | 動作 |
|---|---|
| ⏮ PREV | **3秒ルール**: 再生位置が3秒以降なら現在曲を頭出し、3秒未満なら前の曲へ戻る |
| ⏸ / ▶ | 一時停止 / 再開。停止中も再生位置は保持される |
| ⏭ NEXT | 次の曲へ。紹介文を生成中はグレーアウトする |

- 曲名・アーティスト・次にかかる曲が画面上部に出る
- DJ の紹介文は字幕として表示され、アバターが口を動かす
- 一時停止してもネットラジオのリスナーは切断されない(無音が流れ続ける)
- 左下に VOICEVOX のクレジット(`VOICEVOX:波音リツ`)を表示する。
  話者名は `settings.toml` から取るので、話者を変えれば表示も変わる

### 背景

曲が変わるたびに次の順で決まる。

1. **その曲に埋め込まれたアートワーク**(mp3 の APIC / m4a の covr / FLAC の Picture)
2. アートワークが無ければ **`images/` の画像からランダム**
3. `images/` が無ければ単色 `#12121c`

`images/` に好きな jpg / png を置けばよい(`.gitignore` 済み)。
アートワークは `cache/artwork/` に取り出してキャッシュされる。

> `images/` は起動時に有無を見るので、あとから作った場合は
> program_service を再起動する。

紹介文の生成は 1.5 秒ほど、音声合成は 0.25 秒ほどで終わる。
一度作った紹介文は `cache/intros/` に残るので、2回目以降は即座に再生される。
NEXT のグレーアウトが見えるのは、初めてかける曲を連打したときくらい。

---

## 6. 曲を追加したら

```bash
uv run --no-sync scripts/scan_library.py     # すぐ反映される
uv run --no-sync scripts/enrich_mb.py        # オンライン時に
```

`program_service.py` は毎回 DB を読むので、再起動しなくても次の選曲から入る。

消えたファイルの行を DB から消すには `--prune` を付ける。

```bash
uv run --no-sync scripts/scan_library.py --prune
```

---

## 7. 個別に動かす

サービスを立てずにコマンド単体で試せる。

```bash
# 話者一覧(波音リツ の id を確認する)
uv run --no-sync scripts/voicevox_synth.py --list-speakers

# 1曲ぶんの紹介文を生成(LLM は呼ばずプロンプトだけ見る)
uv run --no-sync scripts/dj_prompt.py --title キセキ --dry-run

# 紹介文の生成 + 音声合成まで
uv run --no-sync scripts/dj_prompt.py --title キセキ --synth

# 全曲ぶん生成して傾向を見る(ログには残さない)
uv run --no-sync scripts/dj_prompt.py --all --no-log

# Liquidsoap の状態を見る / 手で操作する
uv run --no-sync scripts/liquidsoap_client.py status
uv run --no-sync scripts/liquidsoap_client.py push-music "library/曲.mp3"
uv run --no-sync scripts/liquidsoap_client.py pause
```

紹介文の言い回しを変えたいときは、その曲のキャッシュを消せば作り直される。

```bash
rm cache/intros/*.wav cache/intros/*.json
```

---

## 8. 設定

`config/settings.toml`。個人的な値は `config/settings.local.toml` へ。

| セクション | 主なキー |
|---|---|
| `[paths]` | ライブラリ・DB・キャッシュ・ログの場所 |
| `[scan]` | 走査する拡張子 |
| `[musicbrainz]` | 連絡先、リクエスト間隔、採用スコアの閾値 |
| `[llm]` | llama-server の URL、温度、最大文字数、直近コメント数 |
| `[voicevox]` | URL、話者名、スタイル名 |
| `[liquidsoap]` | telnet の接続先、キュー名、出力名 |
| `[icecast]` | ポート、マウント、パスワード |
| `[program]` | 表示系のポート、除外リング数、PREV の閾値 |

ポートは `start_all.sh` もここから読むので、変更してもスクリプトを直す必要はない。

> ただし **Icecast のパスワードだけは3箇所**(`config/icecast.xml`,
> `settings.toml` の `[icecast]`, `liquidsoap/radio.liq`)に書く必要がある。
> Liquidsoap と Icecast は TOML を読まないため。既定は Icecast のプレースホルダ
> `hackme` のままで、LAN 内限定の想定。

---

## 9. うまく動かないとき

### 表示系にアバターが出ない

`vroid/dj.vrm` があるか確認する。ブラウザのコンソールに読み込みエラーが出る。

### 「Liquidsoap に接続できません」で program_service が起動しない

Liquidsoap が先に立ち上がっている必要がある。`./start_all.sh` は順番を守るが、
手動で起動した場合は `tmux attach -t aijukebox` で liquidsoap ウィンドウの
エラーを確認する。

### 曲が終わっても次に進まない

Liquidsoap のキューに前回実行時の曲が残っている可能性がある。
`program_service.py` は起動時にキューを掃除するので、program_service を
再起動すれば直る。

```bash
uv run --no-sync scripts/liquidsoap_client.py status   # music.queue が空か確認
```

### 紹介文が生成されない・番組が無言で進む

llama-server か VOICEVOX が落ちている。表示系のログ(tmux の program ウィンドウ)に
`{'event': 'error', 'where': 'prepare_next', ...}` が出る。
**生成に失敗しても曲は流れ続ける**(紹介なしで次へ進む)設計になっている。

### スマホから聴けない

- ファイアウォールで 8100 番が塞がれていないか
- `http://` であること(`https` ではない)
- PC 側で `http://localhost:8100/radio.mp3` が鳴るかまず確認する

### 曲名が文字化けしている

古い ID3 タグの日本語が CP932 で書かれている場合、`scan_library.py` が自動で
復元する。それでも化ける場合はタグ自体が壊れている可能性が高い。

---

## 10. ドキュメント

- **[TECHNICALJ.md](TECHNICALJ.md)** — 内部構造、設計判断、実装中に詰まった点と解決策
- **[README.md](README.md)** / **[TECHNICAL.md](TECHNICAL.md)** — English versions
- **[HANDOFF.md](HANDOFF.md)** — 元の設計仕様と、フェーズごとの実装メモ

## クレジット

- 音声合成: [VOICEVOX](https://voicevox.hiroshiba.jp/) — `VOICEVOX:波音リツ`
- 3D描画: [three.js](https://threejs.org/) と
  [@pixiv/three-vrm](https://github.com/pixiv/three-vrm)(MIT、`web/libs/` に同梱)
- 曲情報: [MusicBrainz](https://musicbrainz.org/)
