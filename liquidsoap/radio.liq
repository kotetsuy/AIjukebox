# AIjukebox ミキシング・出力層 (Liquidsoap 2.x)
#
# ここは薄いミキサーに徹する。選曲は Python (program_service.py) が持ち、
# telnet 経由で music / dj_intro キューに push してくる。
# Liquidsoap 側に playlist や自動選曲を持たせると、DJが紹介した曲と実際に
# 鳴る曲がずれるため、絶対に足さないこと。
#
#   liquidsoap liquidsoap/radio.liq
#
# telnet:
#   echo 'music.push /path/to/song.mp3' | nc 127.0.0.1 1234
#   echo 'dj_intro.push /path/to/intro.wav' | nc 127.0.0.1 1234
#   echo 'radio_ice.stop' | nc 127.0.0.1 1234

# ---- telnet サーバー (Python からの制御用) -------------------------------
settings.server.telnet.set(true)
settings.server.telnet.bind_addr.set("127.0.0.1")
settings.server.telnet.port.set(1234)

# ---- キュー --------------------------------------------------------------
# Python が次曲と intro を push する。どちらも空の時間があるので fallible。
music_queue = request.queue(id="music")
dj_queue = request.queue(id="dj_intro")

# ---- 音楽キューの位置取得 ------------------------------------------------
# 出力側の radio_local.remaining は「いま出力しているトラック」の残りを返すため、
# intro でダッキング中は intro の残り時間になってしまい、曲の終端検出に使えない。
# 音楽キューだけを見るコマンドを生やす(ロジックは持たせず値を返すだけ)。
#
#   telnet: music.pos  → "<残り秒> <経過秒>"
# source.elapsed は「トラック開始からの実時間」でシークしても戻らないため、
# 再生位置には使えない。位置は Python 側で duration - remaining として出す。
server.register(
  namespace = "music",
  description = "音楽キューの残り秒",
  "pos",
  fun (_) -> "#{source.remaining(music_queue)}"
)

# ---- ダッキング ----------------------------------------------------------
# intro が入ると音楽を p 倍まで下げ、intro が終わったら戻す。
# fallback(track_sensitive=false) は使わない。曲を途中でぶった切るため。
radio = smooth_add(normal=music_queue, special=dj_queue, duration=1.5, p=0.15)

# ---- PAUSE ---------------------------------------------------------------
# Liquidsoap 2.4 の output には .stop / .start / .status が無い(1.x にはあった)。
# 代わりに「radio を pull しない」ことで一時停止する。Liquidsoap はプル型なので、
# 選択されていないソースはフレームを要求されず、再生位置ごと凍結される。
#
# ここでの track_sensitive=false は、availability を見て曲を切り替える用途では
# なく明示的な一時停止フラグを即座に反映させるためのもの。曲を途中でぶった切る
# 誤動作(HANDOFF §7 の禁止事項)は起きない。
#
# 副次的な利点として、PAUSE中も icecast には無音が流れ続けるため、
# スマホ側のリスナーが切断されない(HANDOFF が許容していた制約が解消)。
#
#   telnet: var.set paused = true / var.set paused = false
paused = interactive.bool("paused", false)

stream = switch(track_sensitive=false, [(paused, blank()), ({true}, radio)])

# ---- 出力 ----------------------------------------------------------------
# 出力は2系統あるが、PAUSE は上流の switch 1箇所で効くので、
# 片方だけ止まって位置がずれる問題は構造的に起きない。

output.icecast(
  %mp3(bitrate = 128),
  id = "radio_ice",
  host = "localhost",
  # 8000 は表示系の three-vrm が使うため 8100 (config/icecast.xml と揃える)
  port = 8100,
  password = "hackme",
  mount = "radio.mp3",
  name = "AIjukebox",
  description = "ローカルAIラジオ",
  fallible = true,
  stream
)

output.pulseaudio(id = "radio_local", fallible = true, stream)
