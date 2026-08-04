# TECHNICAL.md — AIjukebox internals

Internal structure and design decisions, plus the problems hit during
implementation and how they were solved.

Setup instructions are in [README.md](README.md).
日本語版は [TECHNICALJ.md](TECHNICALJ.md)、元の設計仕様は [HANDOFF.md](HANDOFF.md)。

---

## Contents

1. [Architecture](#1-architecture)
2. [Track metadata pipeline](#2-track-metadata-pipeline)
3. [DJ intro generation](#3-dj-intro-generation)
4. [Speech synthesis and caching](#4-speech-synthesis-and-caching)
5. [Mixing and streaming](#5-mixing-and-streaming)
6. [Show state machine](#6-show-state-machine)
7. [Display](#7-display)
8. [Problems hit, and how they were solved](#8-problems-hit-and-how-they-were-solved)
9. [Measured performance](#9-measured-performance)
10. [Known limitations](#10-known-limitations)

---

## 1. Architecture

### 1.1 Responsibilities

| Component | Responsibility |
|---|---|
| `scan_library.py` | ID3 tags → DB (offline, idempotent) |
| `enrich_mb.py` | MusicBrainz enrichment (online, manual) |
| `dj_prompt.py` | track info → prompt → Qwen → cleanup |
| `voicevox_synth.py` | text → wav + accent_phrases |
| `artwork.py` | embedded cover art → cache |
| `liquidsoap_client.py` | thin telnet layer over Liquidsoap |
| `program_service.py` | **selection, state machine, run loop, WebSocket, display** |
| `radio.liq` | mixing and output only — holds no logic |

### 1.2 Python owns track selection

**Liquidsoap is deliberately given no playlist and no selection logic.** This
was the first design decision and the most important one: the DJ's introduction
and the track that actually plays must match.

If Liquidsoap picked tracks, Python would be writing an introduction without
knowing what comes next, and the two would drift apart by construction. Instead
Python calls `pick_next()`, generates the intro for that track, and **pushes
both into Liquidsoap's queues** — a strictly one-way flow.

Liquidsoap only holds two `request.queue`s:

```liquidsoap
music_queue = request.queue(id="music")     # Python pushes the next track
dj_queue    = request.queue(id="dj_intro")  # Python pushes intro.wav
```

### 1.3 Process layout

```
program_service.py (asyncio)
 ├─ aiohttp: HTTP (display) + WebSocket   :8765
 ├─ poll_loop: checks remaining time every 0.5 s to detect end of track
 └─ to_thread: LLM calls / synthesis / telnet (blocking IO moved off the loop)
        │
        └─ telnet ─▶ Liquidsoap :1234
                        ├─ output.icecast  ─▶ Icecast :8100 ─▶ phone
                        └─ output.pulseaudio ─▶ speakers
```

It is a single process. Blocking work is pushed through `asyncio.to_thread`, so
polling and WebSocket traffic keep flowing during the ~1.5 s LLM call.

---

## 2. Track metadata pipeline

### 2.1 Schema — ID3-derived and MusicBrainz-derived columns are separate

```sql
CREATE TABLE tracks (
    filepath TEXT PRIMARY KEY,
    -- from ID3 (safe to overwrite on every scan)
    title, artist, album, release_date, genre, composer, duration_sec,
    -- from MusicBrainz (never touched by scan)
    mbid TEXT, mb_release_date TEXT,
    -- bookkeeping
    enrichment_source TEXT DEFAULT 'id3_only',  -- id3_only | musicbrainz | not_found
    last_scanned TIMESTAMP
);
```

They are split so that **re-running `scan_library.py` cannot destroy the
MusicBrainz results**. Mixed into one upsert, every tag re-read would wipe the
release dates that took minutes of rate-limited requests to collect.

`scan_library.py`'s `ON CONFLICT DO UPDATE SET` only ever lists ID3 columns.

### 2.2 MusicBrainz matching

Taking the first hit is forbidden. Matching is three stages:

1. `search_recordings(recording=…, artist=…, limit=8)`
2. keep only candidates with `ext:score >= 90` **and a matching artist name**
3. among those, pick the recording **closest to the ID3 duration** (±5 s)

Stage 3 matters because live versions, TV-size edits and remasters all come back
with score 100 under the same title. For "迷子犬と雨のビート" the API returns
recordings of 414 s / 138 s / 296 s / 301 s at equal score; without comparing
against the ID3 duration of 296 s you land on a 2022 album version.

Artist and title are NFKC-normalized before searching, plus punctuation folding
(see 8.4).

### 2.3 Release year: not COALESCE, but "whichever is older"

Reads go through `common.resolve_release_year(row)`.

```python
years = [year_of(row["mb_release_date"]), year_of(row["release_date"])]
return min(y for y in years if y)
```

The spec called for `COALESCE(mb_release_date, release_date)`, but real data
showed that **MusicBrainz sometimes returns a newer year than the tag**.

| Track | ID3 | MusicBrainz | Used | Actual |
|---|---|---|---|---|
| Yesterday | 1965 | 1985 | **1965** | 1965 |
| Bicycle Race | 1978 | 1979 | **1978** | 1978 |
| 銀河鉄道999 | 1990 | 2021 | **1990** | 1979 |
| ultra soul | 2008 | 2001 | **2001** | 2001 |

MusicBrainz models remasters as separate recordings, so it skews later than the
original release; ID3 skews later too, dragged by compilations and iTunes
purchase dates. **Both error modes only ever point at a newer year**, so taking
the older of the two moves toward the original. 12 of 15 tracks come out exact,
and the other 3 fall back to the ID3 value.

---

## 3. DJ intro generation

### 3.1 Guarding against fabrication

The system prompt is built around "treat only the given information as fact".
Invented tie-ins or awards would be fatal for something presented as radio.

Failures observed in practice are written back into the prompt as counterexamples:

```
2. Never state specifics that were not provided
   - tie-ins, awards, anecdotes, lyric content, instrumentation, production background
   - Bad: "This became famous as the theme of drama X" (when not given)
   - Bad: "It features a simple guitar and piano arrangement" (instruments were not given)
3. Use the given spelling of the title and artist. Do not translate or paraphrase
   - Bad: rendering "Yesterday" as 「昨日」
```

The instrumentation and translation rules only surfaced after actually
generating text.

### 3.2 Confidence drives how far the DJ goes

The user prompt varies with `enrichment_source`.

| source | Policy |
|---|---|
| `musicbrainz` | free to mention the release year |
| `id3_only` | tag data may be trusted as-is |
| `not_found` | **only title and artist are provided**; short, mood-focused |

For `not_found`, year and genre are withheld from the prompt entirely.
Handing over unverified fields gives the model a seed to embellish from.

### 3.3 Album names are not passed to the model

Compilation names and iTunes decorations (`- Single`, `[Disc 1]`) dominate, and
they read badly out loud — "from the album Kick Back minus Single". They are
left out of the prompt.

### 3.4 Post-generation validation

```python
text = _PREAMBLE.sub("", text)      # strip "Comment:"-style preambles
text = _STRIP_CHARS.sub("", text)   # strip ☆★♪♂♀『』【】…
text = text[:max_chars]             # cut at 120 characters
text = _trim_to_sentence(text)      # if it does not end in 。, back up to the last one
```

Symbol stripping exists because the synthesizer reads them aloud — this was
noticed when "アゲ♂アゲ♂EVERY☆騎士" was pronounced with the symbols.

`_trim_to_sentence` protects against generation stopping mid-sentence at
`max_tokens`.

### 3.5 Avoiding repeated phrasing

Generated intros are appended to `logs/program.log` as JSONL, and the three most
recent are attached to the prompt as "avoid these phrasings". They are restored
from the log on startup, so a restart does not immediately reuse a phrase.

---

## 4. Speech synthesis and caching

### 4.1 The speaker id is never hard-coded

```python
def resolve_speaker_id(base_url, speaker_name, style_name) -> int:
    # look up name="波音リツ", style="ノーマル" from GET /speakers
```

Ids can shift between VOICEVOX releases. Hard-coding one means that some day a
completely different character starts talking. The measured value is `id=9`,
and it is still not written down in the code.

### 4.2 The cache holds two files per track

```
cache/intros/{sha1(filepath)}.wav    audio
cache/intros/{sha1(filepath)}.json   text / speaker_id / accent_phrases
```

**Storing the json matters.** The display builds visemes from VOICEVOX's
`accent_phrases`; if only the audio were cached, replaying a cached intro (via
PREV) would leave the avatar's mouth still.

`track_hash` is the SHA-1 of the filepath, which means **a track's intro is
fixed once generated**. That is the price of PREV always being instant. Delete
the cache files to get a different one.

---

## 5. Mixing and streaming

### 5.1 Ducking

```liquidsoap
radio = smooth_add(normal=music_queue, special=dj_queue, duration=1.5, p=0.15)
```

When audio enters `dj_queue`, the music fades to 0.15× over 1.5 s and comes back
afterwards.

Measured by playing a constant-amplitude 440 Hz tone as the music and
band-passing only the 440 Hz component before and after pushing an intro (so
that musical dynamics could not be mistaken for ducking):

```
 11s  -36.5 dB   ← normal
 13s  -48.3 dB   ← ducked (~1.5 s fade)
 …    -46 to -53 dB
 31s  -44.2 dB   ← fading back
 34s  -36.5 dB   ← restored
```

Theory says `20·log10(0.15) = -16.5 dB`. The spread of -11 to -16 dB is the DJ
voice's harmonics leaking into the 440 Hz band and lifting the floor.

### 5.2 Pause

Liquidsoap is a pull-based engine, so there is no natural "pause". The trick is
that **a source that is not selected is never asked for frames, so its position
does not advance**.

```liquidsoap
paused = interactive.bool("paused", false)
stream = switch(track_sensitive=false, [(paused, blank()), ({true}, radio)])
```

Sending `var.set paused = true` over telnet switches to `blank()`; `radio` stops
being pulled and freezes exactly where it was.

Measured: 167.9 s remaining before pausing → wait 6 s → 163.7 s after resuming
and playing for 4 s. Nothing advanced while paused.

This approach has two side benefits.

- **There is a single switching point**, so "one output stopped and the other
  drifted" cannot happen structurally
- **Icecast keeps receiving silence while paused**, so phone listeners are not
  disconnected

### 5.3 Icecast runs without sudo

`config/icecast.xml` uses neither `chroot` nor `changeowner`, so it starts as a
normal user. `/etc/icecast2` is left alone and logs land inside the project.

`logdir` is a **relative path** (`logs`) — an absolute one would only work on
one machine. It assumes you start from the project root.

The port is **8100**, not the default 8000, which collides with the existing
three-vrm display server.

---

## 6. Show state machine

### 6.1 Generating one track ahead

```
track N starts playing
  ├─ immediately: pick_next() decides track N+1
  ▼
[GENERATING]   NEXT disabled (greyed out)
  ├─ cache hit → straight to NEXT_READY
  └─ otherwise Qwen → validate → VOICEVOX → write cache
  ▼
[NEXT_READY]   NEXT enabled
  ├─ NEXT pressed  → push track N+1, then skip
  └─ natural end   → push track N+1
```

The key is that **selecting the next track and generating its intro happen the
moment the current track starts**. Starting after the current track ends would
mean a multi-second wait on every NEXT press.

### 6.2 Selection

```python
class TrackSelector:
    exclude_history: deque(maxlen=10)  # recently played, excluded from picks
    play_stack: list                    # history for PREV
```

These are separate on purpose:

- `exclude_history` prevents recently played tracks from being picked again
- `play_stack` remembers where PREV should go back to

A track reached via PREV is **not** re-added to `exclude_history` — going back
is not a new selection.

There is a fallback to the whole library if the candidate set comes out empty
(a library of fewer than 11 tracks).

### 6.3 Push order

```python
await push_music(filepath)      # queue it first
if skip_current: await skip()   # then cut the current track
await push_intro(intro)
```

The reverse order leaves a moment with an empty queue, which is audible as a gap.

### 6.4 End-of-track detection

The remaining time of the music queue is polled every 0.5 s; the next track is
pushed once it drops below 1.0 s. Waiting for 0 leaves a silent gap.

Detection is suppressed for 4 s after a push (`ADVANCE_COOLDOWN`) — while a
request is still resolving, position is unavailable and reads as "ended".

### 6.5 The PREV 3-second rule

```python
elapsed = row["duration_sec"] - client.remaining()
if elapsed >= 3.0:
    restart_current()          # back to the top of this track
else:
    play_stack.pop()           # go to the previous track
```

This follows the convention of ordinary music players. The restart is
implemented by pushing the same file and skipping (see 8.6d for why).

---

## 7. Display

### 7.1 The browser plays no audio

The intro is already being mixed and played by Liquidsoap, so **playing it in
the browser would double it**. The display receives only the viseme timeline and
moves the mouth.

That rules out the approach used by the existing AIassistant, which schedules
visemes against `audioCtx.currentTime`. With no audio there is no AudioContext
clock, so scheduling runs off **`performance.now()`** instead.

```javascript
const base = performance.now() + (msg.delay_ms ?? 0);
for (let i = 0; i < visemes.length; i++) {
  const t0 = base + vtimes[i];
  items.push({ time: t0,                 name: visemes[i] });
  items.push({ time: t0 + vdurations[i], name: "sil" });
}
```

`delay_ms` (300 by default) compensates for the lag between pushing to
Liquidsoap and audio actually coming out.

### 7.2 WebSocket protocol

HTTP and WebSocket share one port (8765); aiohttp serves both.

```
display → service:  {"cmd": "next" | "prev" | "pause" | "play"}

service → display:
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

- **A snapshot is sent on connect** (now_playing / state / paused / next_up /
  credit), so a browser that joins mid-song still shows the right title and the
  right NEXT button state
- `now_playing` is sent before `intro`; the other way round leaves the previous
  title on screen while the new introduction is already being read
- Viseme arrays run to several hundred entries, so the console log prints only
  the count while the full payload goes over the socket

### 7.3 The background lives in the DOM, not in WebGL

Putting a texture in `scene.background` means computing `repeat` / `offset` by
hand to emulate a cover fit for images of arbitrary aspect ratio. **Leaving the
canvas transparent (`alpha: true`) and putting a `#bg` element behind it** lets
CSS `background-size: cover` do that work, which is both shorter and more
reliable.

The choice is made server-side and shipped in `now_playing`:

1. artwork embedded in the track → `/artwork/{hash}.jpg`
2. otherwise a random file from `images/` → `/images/xxx.jpg`
3. if `images/` does not exist → `null` → flat `#12121c`

Embedded artwork is typically 300–500 px square and shows its seams when
stretched full-screen. The blur radius is chosen from `img.naturalWidth`:
**7 px for low-resolution artwork, 2 px for large images.** The blur hides the
upscaling and improves legibility of the text on top. `transform: scale(1.06)`
keeps the blur from letting the page edges show through.

The new image is only swapped in after it has loaded; setting the URL first
flashes the background colour for a frame.

### 7.4 AIassistant is not referenced

three.js and three-vrm are vendored under `web/libs/`. Pointing at the existing
AIassistant project's paths would mean a change on either side could break the
other.

---

## 8. Problems hit, and how they were solved

Each entry is symptom → cause → fix. **Many of these are cases where following
the written spec did not work.**

### 8.1 macOS AppleDouble files produced 15 errors

**Symptom**: scanning a 15-track library reported 15 "unsupported format" errors.

**Cause**: `._track.mp3` resource-fork stubs left by macOS were being opened as
audio.

**Fix**: skip files whose name starts with `.`.

```python
return not path.name.startswith(".")
```

### 8.2 Japanese tags came out as mojibake

**Symptom**: 「松田聖子 / チェリーブラッサム」 read as
`\x8f¼\x93c\x90¹\x8eq` / `\x83`\x83F\x83\x8a\x81[…`.

**Cause**: old ID3 frames declare latin-1 but contain CP932 bytes, and mutagen
decodes as declared.

**Fix**: re-decode as CP932, but only when three conditions hold at once.

```python
def demojibake(s):
    if any(ord(c) > 0xFF for c in s): return s          # (1) all chars in latin-1 range
    high = sum(1 for c in s if ord(c) >= 0x80)
    if high < 2 or high / len(s) < 0.4: return s        # (2) >=40% high bytes
    decoded = s.encode("latin-1").decode("cp932")
    return decoded if _JP_CHARS.search(decoded) else s  # (3) result is Japanese
```

Condition (2) is what keeps legitimate latin-1 text intact: 0xF6 is a CP932 lead
byte, so an unconditional conversion would turn `"Björk"` into kanji.

### 8.3 Every m4a had a null composer

**Symptom**: `composer` was missing on all six m4a files.

**Cause**: mutagen's `EasyMP4` has no `composer` key (only `composersort`).

**Fix**: fall back to the raw `©wrt` atom. Note that the format is identified by
`type(easy).__name__`, which returns **`"EasyMP4"`, not `"MP4"`** — getting that
wrong meant the fallback never fired at all.

### 8.4 `B'z` could not be found on MusicBrainz

**Symptom**: candidates came back at score 100 yet the result was `not_found`.

**Cause**: MusicBrainz spells it `B’z` with U+2019, which does not match the
ID3 `B'z` (U+0027). **NFKC does not fold this.**

**Fix**: an explicit punctuation table.

```python
_PUNCT_MAP = str.maketrans({
    "‘": "'", "’": "'", "‛": "'",   # ‘ ’ ‛
    "“": '"', "”": '"',                # “ ”
    "–": "-", "—": "-", "―": "-",   # – — ―
})
```

The prolonged sound mark `ー` (U+30FC) is **never** touched — it is part of
Japanese words.

For a different flavour of the same problem (`マイケル・ジャクソン` vs
`Michael Jackson`), a fallback checks `get_artist_by_id(includes=["aliases"])`
but only when no candidate matched. Since the score threshold is untouched, this
does not add false matches.

### 8.5 `max_tokens=150` truncated sentences

**Symptom**: intros ended mid-sentence, e.g. "ぜひ耳を傾けてください" with no
closing 。

**Cause**: roughly 100 Japanese characters do not fit in 150 tokens.

**Fix**: raise `max_tokens` to 256 and enforce length by character trimming
instead. A guard also backs up to the last full stop when the text does not end
in one.

### 8.6 Four things Liquidsoap 2.4 does not do the way the spec assumed

This consumed the most time. **The API differs substantially between versions.**

#### (a) `file://` URIs with percent-encoding fail

The spec said to push non-ASCII filenames as escaped `file://` URIs. The
opposite is true.

| Push form | Result |
|---|---|
| `music.push /path/to/曲 名.mp3` | `Prepared "…" (RID 1)` ✓ |
| `music.push file:///path/to/%E6%9B%B2…` | request silently disappears (`request.trace` says "No such request") |

`push` treats everything to end-of-line as the URI, so **a plain absolute path
handles spaces and non-ASCII fine**. `%20` is never decoded.

#### (b) Outputs have no `.stop` / `.start` / `.status`

They existed in 1.x. The spec's "stop the icecast and pulseaudio outputs
together" is simply not executable on 2.4 → replaced by the switch approach in
5.2.

#### (c) There is no `music.remaining`

`remaining` exists only on outputs. Worse, an output's `remaining` refers to
**whatever it is currently outputting**, so while an intro is ducking it returns
the intro's remaining time — useless for detecting the end of a track.

**Fix**: expose a music-queue-specific command from `radio.liq`.

```liquidsoap
server.register(namespace="music", "pos",
  fun (_) -> "#{source.remaining(music_queue)}")
```

#### (d) Backward seeking does not work — and the return value lies

`source.seek(music_queue, -30)` returns `-30.0` while the position does not
move. Forward seeks work correctly.

| Operation | Return value | Actual change in remaining |
|---|---|---|
| `seek +60` | `60.0` | 313.26 → 253.05 (exactly 60 s) ✓ |
| `seek -30` | `-30.0` | 250.91 → 250.82 (**no change**) ✗ |

The output-level `radio_local.seek -10` is at least honest: `Seeked 0.00`.

**Fix**: PREV's restart pushes the same file and skips.

### 8.7 `source.elapsed` is not a playback position

**Symptom**: restarting a track did not reduce the elapsed time.

**Cause**: `source.elapsed` is wall-clock time since the track started. It keeps
increasing across seeks and restarts.

**Fix**: derive position as **`duration_sec` from the DB minus remaining**.

### 8.8 Auto-advance never fired

**Symptom**: tracks finished and nothing happened, with no error logged.

**Cause**: `music.queue` held `[27 29]` — two leftovers. **Liquidsoap outlives
program_service**, so every service restart left previously pushed tracks in the
queue. The queue therefore never emptied, remaining never reached zero, and the
end-of-track check never triggered.

**Fix**: flush both queues at startup.

```python
await asyncio.to_thread(self.client.flush_queues)   # flush_and_skip on music/dj_intro
```

Frequent restarts during debugging surfaced this, but it would occur in normal
operation on any restart.

### 8.9 sqlite3 connections cannot cross threads

**Symptom**: `SQLite objects created in a thread can only be used in that same
thread`

**Cause**: intro generation was moved to `asyncio.to_thread` to avoid blocking,
and it queried the DB from inside that thread.

**Fix**: rather than silencing it with `check_same_thread=False`, **fetch the
row on the main thread and pass it in**.

```python
def build_intro(self, row) -> Path:   # takes a row, not a filepath
```

Keeping DB access confined to the event-loop thread is harder to break later.

### 8.10 Rapid NEXT presses slipped through

**Symptom**: NEXT is supposed to be disabled during generation, yet pressing it
twice 0.3 s apart executed both.

**Cause**: two of them.

1. the transition to `GENERATING` happened inside a `create_task`'d coroutine,
   so a NEXT arriving before that task started still saw `NEXT_READY`
2. the phase check sat **outside** the lock, so it passed while a previous NEXT
   was still being processed

**Fix**: make the transition synchronous inside `_advance`, and move the check
inside the lock.

```python
async def cmd_next(self):
    async with self._advancing:              # check inside the lock
        if self.phase != NEXT_READY: return "生成中です(NEXTは無効)"
```

**Timestamped logs were what made this diagnosable.** At first it looked like
the rejection was broken, when in fact **a cache hit had completed generation
within the same second** and the second press legitimately saw `NEXT_READY`
again. Reproducing the real bug required emptying the cache first.

### 8.11 `pgrep -f` killed the shell that launched the script

**Symptom**: running `stop_all.sh` killed the terminal it was started from.

**Cause**: `pgrep -f "scripts/program_service.py"` also matches **any unrelated
shell that merely has that string in its arguments**.

**Fix**: require both the process name (comm) and the command line to match.

```bash
for pid in $(pgrep -x "$comm"); do
    [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
    cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline")
    [[ "$cmdline" == *"$pat"* ]] && pids+=("$pid")
done
```

`pgrep -x` matches the process name exactly, so a bash process can never match.

### 8.12 icecast2 survives `tmux kill-session`

**Symptom**: everything else died with the session; Icecast stayed up.

**Cause**: it daemonizes away from its parent, so SIGHUP never reaches it.

**Fix**: this is exactly why `stop_all.sh` keeps the leftover-process sweep from
8.11. It shows up in the log as `停止: Icecast (pid=…)`.

### 8.13 Port collision with an existing project

Icecast's default 8000 was already taken by three-vrm, the display server this
project was meant to reuse. It was **moved to 8100**, with `settings.toml` as
the single source of truth that `start_all.sh` also reads.

---

## 9. Measured performance

On gfx1151 (Ryzen AI Max+ 395) with Qwen3.6-35B-A3B-UD-Q4_K_XL.

| Step | Time |
|---|---|
| Intro generation (Qwen, thinking disabled) | 1.2–1.5 s |
| Speech synthesis (VOICEVOX) | 0.25 s |
| Cache hit | 0 s |
| **Total (first play)** | **under 2 s** |

That finishes comfortably inside a track's runtime, so the `GENERATING` grey-out
is effectively invisible. With a warm cache, NEXT never blocks at all.

MusicBrainz enrichment costs 2–3 requests per track at 1 request/second: about
40 seconds for 15 tracks, or somewhere over ten minutes for a few hundred.

Generated intros run 80–115 characters, roughly 20 seconds of speech.

### Calling llama-server

```python
payload = {
    ...,
    "chat_template_kwargs": {"enable_thinking": False},
}
```

**Without this**, Qwen3 emits thinking by default, `reasoning_content` consumes
`max_tokens`, and `content` comes back empty.

---

## 10. Known limitations

- **An intro is fixed per track.** `track_hash` derives from the filepath, so
  the same song always gets the same introduction. That is the trade for PREV
  being instant
- **`library.db` is not portable between machines** — `filepath` is absolute
- **The Icecast password lives in three places**, because Liquidsoap and Icecast
  do not read TOML
- **Backward seeking is impossible**, so arbitrary-position seeking cannot be
  built (restart is emulated with push + skip)
- `images/` is only checked for at startup; creating it later needs a restart
- Draining the dj_intro queue logs
  `Source created multiple tracks in a single frame!`, which is harmless
