# AIjukebox — a local AI radio DJ

Plays your own music library as a radio station, with an AI DJ (Namine Ritsu)
introducing every track. When a song changes, an LLM writes an intro, it gets
synthesized to speech, and it is mixed over the head of the track. A VRM avatar
lip-syncs to it, and the same audio is streamed over your LAN as internet radio
so you can listen on a phone.

Everything runs locally. The only online step is metadata enrichment via
MusicBrainz, and that is a separate command you run by hand.

日本語版は [READMEJ.md](READMEJ.md) を参照。

```
        ┌──────────────┐
        │ library/*.mp3│
        └──────┬───────┘
               │ ID3 tags + MusicBrainz
        ┌──────▼───────┐        ┌─────────────┐
        │  library.db  │───────▶│ Qwen3.6     │ writes the DJ intro
        └──────────────┘        └──────┬──────┘
                                       │
                                ┌──────▼──────┐
                                │  VOICEVOX   │ Namine Ritsu
                                └──────┬──────┘
                                       │ intro.wav
        ┌──────────────────────────────▼──────┐
        │  Liquidsoap  ducks music under intro │
        └───────┬──────────────────┬──────────┘
                │                  │
        ┌───────▼──────┐   ┌───────▼────────┐
        │  speakers    │   │ Icecast :8100  │──▶ phone
        └──────────────┘   └────────────────┘

        program_service.py owns track selection, state and control
                     └─▶ display :8765 (VRM + transport buttons)
```

---

## 1. Environment

What this was developed and verified on.

| | |
|---|---|
| OS | Ubuntu 26.04 (resolute) |
| GPU | AMD Ryzen AI Max+ 395 / Radeon 8060S (gfx1151, 48GB VRAM) |
| ROCm | 7.14.0 (`/opt/rocm`) |
| Python | 3.12 (managed by uv) |
| Liquidsoap | 2.4.0 |
| Icecast | 2.5.0 |

**Liquidsoap 2.x is required.** The 1.4 series uses substantially different
API names.

---

## 2. Prerequisites

### 2.1 apt packages

```bash
sudo apt install liquidsoap icecast2 tmux ffmpeg
```

The Icecast installer may ask for passwords — the defaults are fine. This
project does not use `/etc/icecast2`; it ships its own `config/icecast.xml`.

### 2.2 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2.3 VOICEVOX ENGINE (docker)

```bash
docker run -d --name voicevox_engine --restart unless-stopped \
  -p 50021:50021 voicevox/voicevox_engine:cpu-ubuntu20.04-latest
```

**The speaker "波音リツ / ノーマル" (Namine Ritsu / Normal) must be available.**
The speaker id is resolved by name from `GET /speakers` at startup, so it keeps
working across VOICEVOX versions.

### 2.4 llama.cpp and Qwen3.6

Build llama.cpp for gfx1151 and make `llama-server` available. The expected
model is `Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` (~22GB).

> **Important:** do not set `HSA_OVERRIDE_GFX_VERSION`. The build is native
> gfx1151, and overriding the arch breaks it.

### 2.5 A VRM model

Supply one `.vrm` avatar file. It is not included in the repository (large
binary, and distribution terms differ per model).

---

## 3. Setup

### 3.1 Clone and install dependencies

```bash
git clone https://github.com/kotetsuy/AIjukebox.git
cd AIjukebox
uv sync
```

`uv sync` creates `.venv` and installs mutagen / musicbrainzngs / httpx / aiohttp.

> Use **`uv run --no-sync`**, not bare `uv run`. The latter re-syncs on every
> invocation, which can reinstall packages you did not intend to change.

### 3.2 Local settings

Values you do not want in the repository (such as your email address) go in
`config/settings.local.toml`, which is git-ignored. It **overrides
`config/settings.toml` key by key**, recursively, so you only write what you
want to change.

```bash
cp config/settings.local.toml.example config/settings.local.toml
```

```toml
# config/settings.local.toml
[musicbrainz]
# MusicBrainz requires a contact address in the User-Agent (per its terms)
contact = "you@example.com"
```

If you never run `enrich_mb.py`, you can skip this entirely.

### 3.3 Add music and an avatar

```bash
cp -r /path/to/music/*.mp3 library/
cp /path/to/avatar.vrm vroid/dj.vrm

# Optional: backgrounds used for tracks that have no embedded artwork
mkdir -p images && cp /path/to/*.jpg images/
```

Supported formats: mp3 / flac / m4a / ogg / opus. Subdirectories are scanned
recursively. Filenames may contain spaces and non-ASCII characters.

### 3.4 Scan the library

```bash
uv run --no-sync scripts/scan_library.py
```

Reads ID3 tags into `db/library.db`. **Fully offline and safe to re-run.**

```
走査: /home/you/AIjukebox/library
  取り込み 15 曲 / 失敗 0 件
DB合計: 15 曲
  id3_only: 15
```

### 3.5 Enrich release dates (optional, online)

```bash
uv run --no-sync scripts/enrich_mb.py
```

Looks up release dates on MusicBrainz so the DJ can say "this one is from 1978".
**It honours the 1 request/second limit**, so a few hundred tracks takes on the
order of ten minutes.

```
対象 15 曲 (min_score=90, interval=1.1s)
[1/15] Queen - Bicycle Race  → cee0c145-… date=1979 score=100
...
完了: 補完 12 / not_found 3 / skip 0 / エラー 0
```

`--dry-run` shows results without touching the DB. `--limit N` caps the count.
Tracks that come back `not_found` are skipped on later runs
(`--retry-not-found` puts them back in scope).

Re-running `scan_library.py` never discards this data — ID3-derived and
MusicBrainz-derived columns are kept separate.

---

## 4. Running

```bash
./start_all.sh
```

Starts the following inside a tmux session named `aijukebox`, waiting for each
service to respond before moving to the next.

| # | Service | Port |
|---|---|---|
| 1 | VOICEVOX ENGINE (docker) | 50021 |
| 2 | llama-server (Qwen3.6) | 8080 |
| 3 | Icecast | 8100 |
| 4 | Liquidsoap (telnet control) | 1234 |
| 5 | program_service (show runner + display) | 8765 |

It then opens the display in Chrome.

```
=========================================================================
 AIjukebox が起動しました。

   表示系      : http://localhost:8765/   ← Chrome で自動オープン
   ネットラジオ: http://192.168.0.20:8100/radio.mp3
=========================================================================
```

- **Display**: `http://localhost:8765/`
- **Listen from a phone**: open `http://<this machine's IP>:8100/radio.mp3` on
  the same LAN

The first start is slow because llama-server has to load the model (it waits up
to 600 seconds).

### Watching logs

```bash
tmux attach -t aijukebox     # Ctrl-b d to detach
```

There are five windows: `voicevox / llama / icecast / liquidsoap / program`.

### Stopping

```bash
./stop_all.sh

./stop_all.sh --keep-llama      # leave llama-server up (reloading is slow)
./stop_all.sh --keep-voicevox   # leave the VOICEVOX container up
```

---

## 5. Using it

The display has three buttons.

| Button | Behaviour |
|---|---|
| ⏮ PREV | **3-second rule**: past 3 seconds it restarts the current track, before that it goes back to the previous one |
| ⏸ / ▶ | Pause / resume. Playback position is preserved while paused |
| ⏭ NEXT | Skip to the next track. Greyed out while an intro is being generated |

- Title, artist and the upcoming track are shown at the top
- The DJ intro appears as a subtitle while the avatar lip-syncs to it
- Pausing does **not** disconnect internet-radio listeners (silence keeps flowing)
- The VOICEVOX credit (`VOICEVOX:波音リツ`) is shown in the bottom-left. The
  speaker name comes from `settings.toml`, so it follows if you change speakers

### Background

Chosen fresh for every track, in this order:

1. **Artwork embedded in the track** (mp3 `APIC` / m4a `covr` / FLAC `Picture`)
2. Otherwise **a random image from `images/`**
3. If `images/` does not exist, the flat colour `#12121c`

Drop any jpg / png into `images/` (it is git-ignored). Extracted artwork is
cached under `cache/artwork/`.

> `images/` is checked for at startup, so restart program_service if you create
> it later.

Generating an intro takes about 1.5 s and synthesis about 0.25 s. Intros are
cached in `cache/intros/`, so replays are instant. In practice you only see the
NEXT button grey out if you hammer it on tracks that have never been played.

---

## 6. After adding music

```bash
uv run --no-sync scripts/scan_library.py     # takes effect immediately
uv run --no-sync scripts/enrich_mb.py        # when you are online
```

`program_service.py` reads the DB on every pick, so new tracks enter the
rotation without a restart.

To drop rows whose files no longer exist:

```bash
uv run --no-sync scripts/scan_library.py --prune
```

---

## 7. Running pieces individually

Each stage works on its own, without bringing up the services.

```bash
# List speakers (to see Namine Ritsu's id)
uv run --no-sync scripts/voicevox_synth.py --list-speakers

# Build the prompt for one track without calling the LLM
uv run --no-sync scripts/dj_prompt.py --title キセキ --dry-run

# Generate the intro text and synthesize it
uv run --no-sync scripts/dj_prompt.py --title キセキ --synth

# Generate for every track to inspect the tone (without logging)
uv run --no-sync scripts/dj_prompt.py --all --no-log

# Inspect or drive Liquidsoap by hand
uv run --no-sync scripts/liquidsoap_client.py status
uv run --no-sync scripts/liquidsoap_client.py push-music "library/song.mp3"
uv run --no-sync scripts/liquidsoap_client.py pause
```

To get a different intro for a track, delete its cache entry and it is rebuilt:

```bash
rm cache/intros/*.wav cache/intros/*.json
```

---

## 8. Configuration

`config/settings.toml`, with personal values in `config/settings.local.toml`.

| Section | Main keys |
|---|---|
| `[paths]` | library, DB, cache and log locations |
| `[scan]` | file extensions to scan |
| `[musicbrainz]` | contact, request interval, score threshold |
| `[llm]` | llama-server URL, temperature, max characters, recent-intro count |
| `[voicevox]` | URL, speaker name, style name |
| `[liquidsoap]` | telnet target, queue ids, output ids |
| `[icecast]` | port, mount, password |
| `[program]` | display port, exclusion ring size, PREV threshold |

`start_all.sh` reads ports from here too, so changing one does not require
editing the script.

> The **Icecast password is the one exception** — it has to be written in three
> places (`config/icecast.xml`, `[icecast]` in `settings.toml`, and
> `liquidsoap/radio.liq`), because Liquidsoap and Icecast do not read TOML.
> It ships as Icecast's own placeholder `hackme`, on the assumption that the
> stream stays inside your LAN.

---

## 9. Troubleshooting

### No avatar in the display

Check that `vroid/dj.vrm` exists. The browser console will show a load error.

### program_service exits with "Liquidsoap に接続できません"

Liquidsoap has to be up first. `./start_all.sh` handles the ordering; if you
started things by hand, check the liquidsoap window via
`tmux attach -t aijukebox`.

### Tracks finish but nothing advances

The Liquidsoap queue probably still holds entries pushed by a previous run.
`program_service.py` flushes the queues at startup, so restarting it fixes this.

```bash
uv run --no-sync scripts/liquidsoap_client.py status   # is music.queue empty?
```

### No intros are generated / the show runs silently

llama-server or VOICEVOX is down. The program window logs
`{'event': 'error', 'where': 'prepare_next', ...}`.
**Music keeps playing when generation fails** — it simply moves on without an
intro. That is intentional.

### Cannot listen from a phone

- Check that port 8100 is not blocked by a firewall
- Use `http://`, not `https`
- First confirm `http://localhost:8100/radio.mp3` plays on the machine itself

### Track titles are mojibake

Old ID3 tags that store Japanese as CP932 are repaired automatically by
`scan_library.py`. If text is still garbled, the tag itself is likely corrupt.

---

## 10. Documentation

- **[TECHNICAL.md](TECHNICAL.md)** — internals, design decisions, and the
  problems hit during implementation with their fixes
- **[READMEJ.md](READMEJ.md)** / **[TECHNICALJ.md](TECHNICALJ.md)** — Japanese versions
- **[HANDOFF.md](HANDOFF.md)** — the original design spec plus per-phase
  implementation notes (Japanese)

## Credits

- Speech synthesis: [VOICEVOX](https://voicevox.hiroshiba.jp/) — `VOICEVOX:波音リツ`
- 3D rendering: [three.js](https://threejs.org/) and
  [@pixiv/three-vrm](https://github.com/pixiv/three-vrm) (MIT, vendored under `web/libs/`)
- Metadata: [MusicBrainz](https://musicbrainz.org/)
