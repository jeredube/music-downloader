# Native macOS audio capture

`capture.swift` records 24-bit PCM AIFF with clean `Artist - Title.aiff` song filenames
and embedded artwork for rekordbox.

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
under `~/Desktop/dj/recorded-songs/<timestamp>/` when using the launcher, with AIFF
segments only while recording, plus a single `session.json` manifest when the
recorder exits normally (Ctrl+C or its duration limit). No per-song JSON files are
created. The final terminal line prints the session report path. Running `capture`
directly still uses its original `recordings/` default unless you supply `--out`.
Audio is uncompressed 24-bit PCM AIFF at the captured sample rate; there is no
320 kbps encoding setting. Compared with the previous 32-bit float WAVs, audio
uses about 25% less space, with only negligible quantization differences.

Song files use `Artist - Title.aiff`, without a track number or stream suffix.
If that name is already used, a short ` (copy …)` suffix preserves the additional
recording. Per-track metadata remains embedded in the AIFF and is also included in
the final `session.json` manifest.
Finished AIFF files are made visible in Finder; the launcher also repairs hidden
flags on recordings saved by earlier versions. In-progress `.part` files stay hidden.
Restart the recorder after updating to use AIFF for a new session; an already-running
session continues using its original executable. Existing recordings are not changed.

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
4. Set Spotify's volume to a suitable recording level. If the recorder reports
   repeated `CLIP` warnings, lower it slightly to leave headroom. The virtual
   output sends digital audio to BlackHole without speaker playback.
5. Start `./record.command`, allow any Microphone permission requested for the
   virtual input, and press Play in Spotify. `--mode pre` automatically reads the
   selected loopback device's input; no AudioFlow application is needed.
6. Press Ctrl+C to finalize the files. Pause Spotify before switching the Mac's
   output back to your speakers/headphones; reduce Spotify's volume before resuming.

Other apps and notifications routed to BlackHole can also enter the recording.
Use Do Not Disturb and close other sound-producing apps for clean recordings.

References: [BlackHole system-output routing](https://github.com/ExistentialAudio/BlackHole/wiki/Getting-Started%3A-Sending-System-Audio-to-BlackHole)
and [recommended BlackHole levels](https://github.com/ExistentialAudio/BlackHole/discussions/840).

## Album covers and rekordbox

New recordings include title, artist, album and an ID3 APIC front-cover image in
the AIFF itself. The cover is fetched while the song records and embedded when
the file is finalized. Artwork requires an available cover from Spotify or Apple
Music; failed or unavailable artwork does not prevent audio from being saved.

Import the finished `.aiff` files into rekordbox and analyze them. For the XDJ-RX3,
use 44.1 kHz or 48 kHz recordings and export the playlist to USB with **Device
Library** included. These AIFFs use the RX3-supported 24-bit PCM format. Higher
sample rates captured from another source are preserved and must be converted
before use on the RX3.

Older WAVs can have embedded artwork that Finder displays while rekordbox ignores
it, because rekordbox's WAV reader uses RIFF INFO. Converting those recordings to
AIFF must copy their ID3 tags and artwork; renaming the extension is not conversion.
No artwork-image sidecars or per-track JSON reports are generated.

References: [rekordbox WAV metadata formats](https://cdn.rekordbox.com/files/20251202174516/rekordbox7.2.8_manual_EN.pdf#page=13)
and [adding artwork](https://cdn.rekordbox.com/files/20251202174516/rekordbox7.2.8_manual_EN.pdf#page=26),
[XDJ-RX3 formats](https://downloads.support.alphatheta.com/manuals/XDJ_RX3_DRI1702C_manual.pdf#page=10),
and [USB library compatibility](https://alphatheta.com/en/information/important-notice-for-customers-using-usb-devices-with-our-dj-equipment/).
