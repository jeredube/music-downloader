# Autonomous macOS audio recorder

A local command-line recorder for Apple Silicon Macs. Start it, then press Play in
the Spotify **desktop app**. It watches Spotify every 0.25 seconds, records the
selected Core Audio input, and creates a tagged file whenever the track ID changes.
Pause or stop Spotify to finalize the current file and return to standby. Resume
creates another file, including when resuming the same song. No browser, Spotify
API token, frontend, Node packages, or FFmpeg is needed.

**Timing limitation:** AppleScript reports observations, not sample-accurate audio
boundaries. Detection can lag by a poll interval plus AppleScript, playback, and
Core Audio latency; under load, it can take longer. Continuous capture and a
half-second writer buffer prevent intentional gaps between detected track changes,
but cannot recover an exact boundary or an opening already played before launch.
Disable Spotify crossfade and Automix for cleaner splits. Crossfaded songs cannot
be separated into isolated originals from a mixed loopback signal. Launch before
playback; starting partway through a song records the remainder. Repeating the
same Spotify ID without an observed pause does not trigger another split.

## 1. Install and route audio (one time)

You need Python **3.10 or newer**, Node/npm for the optional launch wrapper, the
Spotify desktop app for metadata mode, and
[BlackHole 2ch](https://existential.audio/blackhole/). Use the native Apple Silicon
Python build. This project is intended for macOS Sequoia / Apple Silicon; the
actual audio route and macOS permissions must be verified on your Mac.

1. Install BlackHole **2ch** using its installer. Quit and reopen audio applications
   if the new device does not appear.
2. Open **Audio MIDI Setup** using Spotlight. Choose **Window → Show Audio
   Devices** if needed.
3. Select **BlackHole 2ch** in the sidebar. Set its format to **44,100 Hz**. Select
   **MacBook Pro Speakers**, built-in headphones, or your wired audio interface and
   set that device to **44,100 Hz** too. A different recording rate requires both
   devices to match it.
4. Click **+** at the bottom left and choose **Create Multi-Output Device**.
5. Enable **Use** for both **BlackHole 2ch** and your speakers/headphones. Rename
   the new device, for example **Speakers + Recorder**.
6. Choose your speakers or wired headphones as **Primary Device / Clock Source**.
   Enable **Drift Correction** for BlackHole and every other device except that
   clock source. Keep the built-in output enabled and first where macOS requires
   it. Prefer wired output; Bluetooth can add latency and change sample rates.
7. In **System Settings → Sound → Output**, select **Speakers + Recorder**. Leave
   the recorder's input set to **BlackHole 2ch**, not the Multi-Output Device.
8. Set speaker/headphone volume before switching output. macOS may disable its
   normal volume keys for Multi-Output Devices; adjust individual device volumes
   in Audio MIDI Setup instead.
9. Open Spotify and turn off **Crossfade** and **Automix** in its playback settings.
   Use Do Not Disturb and avoid other audio applications: BlackHole captures
   everything routed to it, including alerts and other apps.

Spotify now sends audio to both your speakers and the recorder. The recorder itself
does not play or monitor audio, so it creates no feedback loop. You can reduce the
physical speaker volume without muting BlackHole.

The first launch may prompt for **Microphone** permission for the launching app
(Terminal, your IDE, or Codex), even though BlackHole is a virtual input. Allow it
under **System Settings → Privacy & Security → Microphone**. Also allow the
launching app to control Spotify under **Privacy & Security → Automation**.
Initial permission prompts and routing require user action once; subsequent
sessions run unattended. The tool does not change system audio routing itself.

These routing steps follow the [BlackHole Multi-Output setup guide](https://github.com/ExistentialAudio/BlackHole/wiki/Multi-Output-Device).

## 2. Set up Python

From this project directory:

```sh
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

The virtual environment lives inside this project. npm runs its Python directly,
so activating the environment is unnecessary after installation. There is no
`npm install` step. The SoundFile wheel includes libsndfile on supported Macs and
the sounddevice wheel supplies PortAudio; if PortAudio cannot load, install it with
`brew install portaudio` and reinstall the requirements.

## 3. Record

```sh
npm run dev
# Equivalent:
npm run start
# Without npm:
.venv/bin/python auto_recorder.py
```

Wait for **Standby**, then press Play on a playlist in Spotify. Files appear in
`./recorded_tracks`. Press **Ctrl+C** to stop: the input closes, queued audio drains,
the current file is finalized and tagged, and a session JSON report is saved.
The terminal prints each saved file's absolute path and duration. SIGTERM uses the
same graceful shutdown. Keep this terminal running and prevent the Mac from
sleeping during a session. For example:

```sh
caffeinate -i npm run start
```

List devices and choose an input:

```sh
npm run dev -- --list-devices
npm run dev -- --device "BlackHole 2ch"
npm run dev -- --device 3 --sample-rate 44100 --channels 2
```

Indexes can change after reconnecting devices. The default selects BlackHole 2ch
by name and fails with setup instructions if it is missing; it does not silently
fall back to your microphone. A supported but different device default sample rate
prints a warning: Core Audio may resample unless you match the Audio MIDI settings.
The software validates device availability, channel count, and supported stream
settings. It cannot prove that Spotify's output has been routed correctly; a
15-second silent-input warning helps diagnose that case.

Choose file specifications or an output location:

```sh
npm run start -- --format flac --bit-depth 24 --output "$HOME/Music/Recorded Tracks"
npm run start -- record --format aiff --bit-depth 16
npm run start -- --format wav --max-duration 3600
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--device` | auto-detect BlackHole 2ch | Input device index or unambiguous name |
| `--sample-rate` | `44100` | Requested stream rate, in Hz |
| `--channels` | `2` | Input channel count |
| `--format` | `flac` | `flac`, `wav`, or `aiff` |
| `--bit-depth` | `16` | `16` or `24` bit integer PCM |
| `--output` | `./recorded_tracks` | Recording destination |
| `--buffer-seconds` | `10` | Audio queue capacity, excluding the 0.5-second holdback |
| `--max-duration` | `0` | Wall-clock limit including standby; `0` runs until stopped |
| `--list-devices` | off | Print device indexes, input channels and default rates, then exit |

Names use `01 - Artist - Title.flac`, with unsafe characters replaced and long
UTF-8 names shortened safely. Track numbers come from the album, not the playlist
position. Repeated names receive ` (2)`, ` (3)`, etc.; existing files are never
overwritten. Spotify Title, Artist, Album, and Track Number are embedded as FLAC
Vorbis comments or WAV/AIFF ID3 tags. WAV ID3 support varies across players; FLAC
and AIFF usually offer more predictable library tagging. Album art is not provided
by the watcher.

FLAC compresses the captured PCM losslessly; it does not improve or restore
information absent from the source stream. At 44.1 kHz stereo, uncompressed 16-bit
audio uses about 635 MB/hour and 24-bit audio about 953 MB/hour; FLAC size varies.
Keep enough free disk space for the entire session.

### Non-Spotify sources: silence splitting

```sh
npm run dev -- --split-silence --device "BlackHole 2ch" \
  --threshold-db -45 --min-silence-sec 2 --pad-sec 0.15
```

This mode bypasses AppleScript and waits for audio above the threshold. A
continuous below-threshold interval of `--min-silence-sec` finalizes a track. It
retains up to `--pad-sec` before and after detected audio and discards excess
silence between tracks. Short quiet passages remain in the recording. The
threshold uses the largest absolute sample across channels, not an LUFS or RMS
gate. Keep `0 <= 2 * pad-sec <= min-silence-sec`. Quiet music can split early and
gapless music cannot split reliably; tune the flags for your source. Files receive
sequential generic titles because no Spotify metadata is requested.

## 4. Standardize a folder and report loudness

```sh
npm run start -- process --input-dir ./recorded_tracks \
  --output-dir ./processed_tracks --format flac --sample-rate 44100 \
  --bit-depth 16 --report-format json

.venv/bin/python auto_recorder.py process --input-dir ./recorded_tracks \
  --output-dir ./travel_wav --format wav --bit-depth 24 --report-format csv
```

The processor scans immediate files in the input folder (`.wav`, `.wave`, `.flac`,
`.aiff`, `.aif`, case-insensitively). It preserves the channel count, writes new
files to a separate folder, and never edits originals. Matching-rate files stream
directly. Other rates use a NumPy windowed-sinc low-pass resampler with a Kaiser
window, rational sample positions, endpoint extension, and bounded working memory.
Its cutoff is 95% of the lower Nyquist frequency; this trades a small amount of
top-end bandwidth for alias suppression. Output frame count is
`ceil(input_frames * target_rate / source_rate)`.

There is no gain normalization or added dither. Conversion to a lower bit depth
quantizes samples. Samples outside the integer PCM range clip and are counted in
report warnings. The processor rereads the **actual output PCM** to calculate:

- Exact frame-count duration, sample rate, integer bit depth and channels.
- Sample peak: `20 * log10(max(abs(samples)))` dBFS.
- Integrated RMS over every sample in every channel:
  `20 * log10(sqrt(sum(samples**2) / sample_count))` dBFS, using float64 sums.

These are sample peak and unweighted RMS, **not** true peak or EBU R128 / LUFS.
Digital silence is negative infinity: represented as `null` in JSON or an empty
CSV cell, so reports remain valid standard JSON/CSV. Reports use unique timestamped
names. Unreadable files are reported individually and processing continues; a
failed file makes the final exit status nonzero. Ctrl+C preserves completed
outputs, removes the unfinished conversion, and writes a report for attempted files.

Metadata preservation includes arbitrary FLAC text fields and embedded pictures;
WAV ↔ AIFF retains native ID3 frames. Cross-container conversion maps common text
fields, comments, lyrics, other ID3 text frames, custom text, and pictures. ID3
binary/link frames without a FLAC equivalent generate a warning when targeting
FLAC. Container-specific non-tag chunks such as broadcast-WAV data, cue sheets,
and application chunks are not copied. No ReplayGain or normalization is applied;
existing loudness tags are retained as source metadata.

## Reliability and troubleshooting

- The callback copies float32 blocks to a bounded `queue.Queue`; the dedicated
  writer streams PCM to SoundFile. At default settings the audio queue plus
  holdback is about 3.8 MB, independent of playlist length. Session entries and
  reports grow with the number of tracks. Silence-mode buffering also depends on
  the configured silence/padding length. Python callback execution is best effort,
  not a hard real-time guarantee.
- An input overflow, full queue, failed disk write, disconnected device or repeated
  AppleScript failures stops capture with a nonzero exit status. Errors and warnings
  appear in the session report. Audio already accepted by the queue is drained
  when possible. Device frame losses cannot be repaired.
- A single failed Spotify query finalizes the track to avoid assigning unknown
  audio to it; the watcher retries. Three consecutive failures stop the session.
- Files are written under hidden `.partial` names and published without replacing
  an existing file after close/tagging. A tag failure preserves the audio with
  `metadata_error` in the report. A write/finalization failure may leave a partial
  file whose path is recorded for recovery. Do not delete that file if you need
  the audio. Power loss or SIGKILL cannot run graceful shutdown or guarantee a
  valid header/session report.
- **Silent recordings:** confirm macOS output is the Multi-Output Device, input is
  BlackHole 2ch, BlackHole levels are up, Spotify is playing locally, and microphone
  permission is granted. The tool cannot capture a remote Spotify Connect speaker.
- **Crackles or drifting audio:** match sample rates, enable drift correction on
  non-clock devices, avoid Bluetooth, and close other heavy workloads.
- **AppleScript errors:** enable Automation access to Spotify for the launching
  application, open the Spotify desktop app, and relaunch the command. Permission
  prompts must be handled before unattended recording.
- `record` requires macOS for Spotify control; silence capture works wherever
  PortAudio is supported. `process` does not access audio hardware or AppleScript.
- Exit statuses: `0` clean completion/graceful recorder stop, `1` runtime or file
  errors, `2` invalid CLI arguments, `130` interrupted batch conversion.

## Verification

```sh
npm test
```

Tests use generated audio and mocked devices to exercise splitting, queue draining,
metadata, collision handling, resampling and metrics without playing Spotify or
changing your audio settings. For a hardware acceptance check: route audio as above,
start recording before Play, play two tracks, pause, resume, then press Ctrl+C.
Check filenames/tags, listen around each transition, and inspect the session report
for errors. Hardware routing, Automation permissions, long-session stability and
Spotify transition timing require this real-device check on your Mac.

Implementation references: [sounddevice streams](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html),
[SoundFile](https://python-soundfile.readthedocs.io/en/latest/),
[Mutagen FLAC](https://mutagen.readthedocs.io/en/latest/api/flac.html),
[Mutagen WAV](https://mutagen.readthedocs.io/en/latest/api/wave.html),
[Mutagen AIFF](https://mutagen.readthedocs.io/en/latest/api/aiff.html).
