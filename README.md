# Native macOS audio capture

`capture.swift` uses the supplied recording implementation, with clean `Artist - Title.wav` song filenames.

## Requirements

- macOS 14.2 or newer.
- Apple's Xcode Command Line Tools (Swift compiler).
- Spotify or Apple Music for track metadata.

Install the developer tools if `swiftc --version` does not work:

```sh
xcode-select --install
```

This Mac already has the Swift compiler installed. The program uses the system
AudioToolbox, CoreAudio and Foundation frameworks; there are no package dependencies.

## Build and run

```sh
cd /Users/jeremydube/Desktop/projects/repos/music-downloader
./record.command
```

The launcher builds the program when needed, then records until Ctrl+C, saving
sessions to `~/Desktop/dj/recorded-songs/<timestamp>/`. You can also double-click
`record.command` in Finder to launch it.

Use your normal speakers or headphones as the macOS sound output to capture via a
native process tap. In this configuration, BlackHole and AudioFlow are not required.
If you deliberately select a loopback output instead, the supplied code reads that
loopback's input and its routing must already be configured.

Allow the launching terminal under **System Settings → Privacy & Security →
Screen & System Audio Recording**. Allow Spotify / Music access under **Automation**
when prompted. Relaunch the terminal if macOS requests it.

Start playing music. Press **Ctrl+C** to stop and finalize the session. Output goes
under `~/Desktop/dj/recorded-songs/<timestamp>/` when using the launcher, with WAV
segments only while recording, plus a single `session.json` manifest when the
recorder exits normally (Ctrl+C or its duration limit). No per-song JSON files are
created. The final terminal line prints the session report path. Running `capture`
directly still uses its original `recordings/` default unless you supply `--out`.
The supplied code writes WAV; it does not expose a 320 kbps encoding setting.

Song files use `Artist - Title.wav`, without a track number or stream suffix.
If that name is already used, a short ` (copy …)` suffix preserves the additional
recording. Per-track metadata remains embedded in the WAV and is also included in
the final `session.json` manifest.
Finished WAV files are made visible in Finder; the launcher also repairs hidden
flags on recordings saved by earlier versions. In-progress `.part` files stay hidden.

Other commands:

```sh
./capture --list
./capture --help
```

The launcher selects `--mode pre`. Running `capture` without a mode defaults to
`both`, which expects a running AudioFlow backend for its additional post-processing tap.

## Silent recording in a library or classroom

BlackHole 2ch is a virtual output: sending sound only to it does not play sound
through physical speakers or headphones. It serves as the dummy output.

1. Install BlackHole 2ch if needed (`brew install --cask blackhole-2ch`) and restart
   the Mac after installation if the device is not listed. On this Mac the driver
   is already installed.
2. Open **Audio MIDI Setup**, select **BlackHole 2ch**, and set its format to
   **44,100 Hz**. Keep its input/output gain at **0 dB (unity)** and unmuted.
3. Open **System Settings → Sound → Output** and select **BlackHole 2ch** directly.
   This route should contain only the virtual device, with no speakers/headphones
   added through a Multi-Output Device.
4. Set Spotify's own volume slider to 100%. Because the selected output is virtual,
   this sends full-level digital audio to BlackHole without speaker playback.
5. Start `./record.command`, allow any Microphone permission requested for the
   virtual input, and press Play in Spotify. `--mode pre` automatically reads the
   selected loopback device's input; no AudioFlow application is needed.
6. Press Ctrl+C to finalize the files. Pause Spotify before switching the Mac's
   output back to your speakers/headphones; reduce Spotify's volume before resuming.

Other apps and notifications routed to BlackHole can also enter the recording.
Use Do Not Disturb and close other sound-producing apps for clean recordings.

References: [BlackHole system-output routing](https://github.com/ExistentialAudio/BlackHole/wiki/Getting-Started%3A-Sending-System-Audio-to-BlackHole)
and [recommended BlackHole levels](https://github.com/ExistentialAudio/BlackHole/discussions/840).

## Album covers in Finder versus rekordbox

The recorder embeds title/artist/album text in WAV RIFF INFO and embeds artwork in
an ID3 APIC frame. Finder can display that embedded image. Rekordbox's documented
WAV metadata reader uses RIFF INFO, so a Finder thumbnail does not guarantee the
cover will import into rekordbox. This is not caused by the JSON reports.

To keep WAV files, add the cover directly in rekordbox: select the track in
Collection, open the track information panel, select **Artwork**, then drag a JPEG
or PNG image onto the panel. The cover is associated with the rekordbox library
entry; keep a library backup. Reload Tag alone is not a reliable WAV-artwork fix.

For automatic embedded artwork import, an artwork-friendly format such as AIFF
with ID3 tags is the alternative. This project continues to output WAV as requested;
no files are converted or removed, and no artwork-image sidecars are generated.

References: [rekordbox WAV metadata formats](https://cdn.rekordbox.com/files/20251202174516/rekordbox7.2.8_manual_EN.pdf#page=13)
and [adding artwork](https://cdn.rekordbox.com/files/20251202174516/rekordbox7.2.8_manual_EN.pdf#page=26).
