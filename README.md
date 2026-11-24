# Native macOS audio capture

`capture.swift` is the supplied source file, copied unchanged.

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
swiftc -O capture.swift -o capture
./capture --mode pre --duration 0
```

Use your normal speakers or headphones as the macOS sound output to capture via a
native process tap. In this configuration, BlackHole and AudioFlow are not required.
If you deliberately select a loopback output instead, the supplied code reads that
loopback's input and its routing must already be configured.

Allow the launching terminal under **System Settings → Privacy & Security →
Screen & System Audio Recording**. Allow Spotify / Music access under **Automation**
when prompted. Relaunch the terminal if macOS requests it.

Start playing music. Press **Ctrl+C** to stop and finalize the session. Output goes
under `recordings/<timestamp>/`, with WAV segments, track JSON sidecars and a
`session.json` manifest. The supplied code writes WAV; it does not expose a 320 kbps
encoding setting.

Other commands:

```sh
./capture --list
./capture --help
```

Use `--mode pre` as shown above: the source defaults to `both`, which expects a
running AudioFlow backend for its additional post-processing tap.
