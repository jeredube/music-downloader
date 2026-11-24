#!/usr/bin/env python3
"""Continuous Core Audio capture, Spotify metadata splitting, and PCM standardization.

Audio callbacks only copy blocks into a bounded queue. A single writer owns every
audio file. Playback events use the same monotonic clock as ADC timestamps; a
short holdback lets the writer split *inside* a block at a detected transition.
AppleScript observation times are not sample-exact Spotify playback boundaries.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import os
from pathlib import Path
import platform
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterator

try:
    import numpy as np
    import soundfile as sf
    import mutagen
    from mutagen.aiff import AIFF
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC, COMM, ID3, Frames, TXXX, USLT, TextFrame
    from mutagen.wave import WAVE
except ImportError as exc:
    raise SystemExit(
        f"Missing dependency: {exc}. Run .venv/bin/python -m pip install -r requirements.txt"
    ) from exc


FORMATS = {"flac": "FLAC", "wav": "WAV", "aiff": "AIFF"}
AUDIO_EXTENSIONS = {".wav", ".wave", ".flac", ".aiff", ".aif"}
BLOCK_FRAMES = 1024
HOLDBACK_SEC = 0.5
POLL_SEC = 0.25
TAG_IDS = {
    "title": "TIT2", "artist": "TPE1", "album": "TALB",
    "tracknumber": "TRCK", "albumartist": "TPE2", "discnumber": "TPOS",
    "date": "TDRC", "genre": "TCON", "composer": "TCOM",
    "copyright": "TCOP", "isrc": "TSRC", "bpm": "TBPM",
    "encodedby": "TENC", "organization": "TPUB",
}
REVERSE_TAG_IDS = {v: k for k, v in TAG_IDS.items()}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dbfs(amplitude: float) -> float | None:
    """None represents negative infinity, keeping JSON strictly standards compliant."""
    return 20.0 * math.log10(amplitude) if amplitude > 0 else None


def safe_component(text: str, max_bytes: int = 220) -> str:
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .") or "Untitled"
    encoded = text.encode("utf-8")[:max_bytes]
    return encoded.decode("utf-8", errors="ignore").rstrip(" .") or "Untitled"


def unique_path(path: Path) -> Path:
    candidate = path
    n = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


def partial_path(path: Path) -> Path:
    # The UUID/suffix must also fit within APFS's 255-byte filename limit.
    prefix = safe_component(path.name, max_bytes=160)
    return path.with_name(f".{prefix}.{uuid.uuid4().hex}.partial")


def publish_file(partial: Path, destination: Path) -> Path:
    """Publish without overwriting even when two processes choose the same name."""
    while True:
        destination = unique_path(destination)
        try:
            os.link(partial, destination)
            partial.unlink()
            return destination
        except FileExistsError:
            continue


def atomic_json(path: Path, value: object) -> None:
    temporary = partial_path(path)
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str = "Unknown Artist"
    album: str = ""
    number: int = 0

    def filename(self, extension: str) -> str:
        return f"{safe_component(f'{self.number:02d} - {self.artist} - {self.title}')}.{extension}"

    def metadata(self) -> Metadata:
        return Metadata(values={
            "title": [self.title], "artist": [self.artist], "album": [self.album],
            "tracknumber": [str(self.number)],
        })


@dataclass
class Metadata:
    values: dict[str, list[str]] = field(default_factory=dict)
    pictures: list[Picture] = field(default_factory=list)
    id3: ID3 | None = None
    warnings: list[str] = field(default_factory=list)


def read_metadata(path: Path) -> Metadata:
    audio = mutagen.File(path)
    result = Metadata()
    if audio is None:
        raise ValueError(f"Unrecognized audio metadata container: {path}")
    if isinstance(audio, FLAC):
        result.values = {k: list(v) for k, v in (audio.tags or {}).items()}
        result.pictures = copy.deepcopy(audio.pictures)
    elif audio.tags is not None:
        result.id3 = copy.deepcopy(audio.tags)
        for frame in audio.tags.values():
            if isinstance(frame, APIC):
                pic = Picture()
                pic.type, pic.mime, pic.desc, pic.data = (
                    int(frame.type), frame.mime, frame.desc, frame.data
                )
                result.pictures.append(pic)
            elif isinstance(frame, TXXX):
                result.values.setdefault(frame.desc.lower(), []).extend(map(str, frame.text))
            elif isinstance(frame, COMM):
                result.values.setdefault("comment", []).extend(map(str, frame.text))
            elif isinstance(frame, USLT):
                result.values.setdefault("lyrics", []).append(frame.text)
            elif isinstance(frame, TextFrame):
                key = REVERSE_TAG_IDS.get(frame.FrameID, f"id3:{frame.FrameID}")
                result.values.setdefault(key, []).extend(map(str, frame.text))
            else:
                result.warnings.append(f"ID3 frame {frame.FrameID} only preserved in WAV/AIFF output")
    return result


def write_metadata(path: Path, metadata: Metadata, extension: str) -> None:
    """Retain all ID3 frames between WAV/AIFF; map text and artwork across containers."""
    if extension == "flac":
        audio = FLAC(path)
        if audio.tags is None:
            audio.add_tags()
        for key, values in metadata.values.items():
            audio[key] = values
        for pic in metadata.pictures:
            audio.add_picture(copy.deepcopy(pic))
        audio.save()
        return
    audio = WAVE(path) if extension == "wav" else AIFF(path)
    if audio.tags is None:
        audio.add_tags()
    if metadata.id3 is not None:
        audio.tags.update(copy.deepcopy(metadata.id3))
    else:
        for key, values in metadata.values.items():
            frame_id = TAG_IDS.get(key.lower())
            if key.startswith("id3:") and key[4:] in Frames:
                frame_id = key[4:]
            if frame_id and issubclass(Frames[frame_id], TextFrame):
                audio.tags.add(Frames[frame_id](encoding=3, text=values))
            elif key == "comment":
                audio.tags.add(COMM(encoding=3, lang="eng", desc="", text=values))
            elif key == "lyrics":
                audio.tags.add(USLT(encoding=3, lang="eng", desc="", text="\n".join(values)))
            else:
                audio.tags.add(TXXX(encoding=3, desc=key, text=values))
        for index, pic in enumerate(metadata.pictures):
            audio.tags.add(APIC(encoding=3, mime=pic.mime, type=pic.type,
                                desc=pic.desc or f"Cover {index + 1}", data=pic.data))
    audio.save()


@dataclass
class Meter:
    frames: int = 0
    samples: int = 0
    peak: float = 0.0
    sum_squares: float = 0.0

    def add(self, data: np.ndarray) -> None:
        if not data.size:
            return
        self.frames += len(data)
        self.samples += data.size
        self.peak = max(self.peak, float(np.max(np.abs(data))))
        self.sum_squares += float(np.sum(np.square(data, dtype=np.float64), dtype=np.float64))

    def metrics(self, rate: int) -> dict:
        return {"frames": self.frames, "duration_sec": self.frames / rate,
                "peak_dbfs": dbfs(self.peak),
                "rms_dbfs": dbfs(math.sqrt(self.sum_squares / self.samples)) if self.samples else None}


def measure_file(path: Path) -> dict:
    meter = Meter()
    with sf.SoundFile(path) as source:
        for data in source.blocks(blocksize=65536, dtype="float64", always_2d=True):
            meter.add(data)
        return {**meter.metrics(source.samplerate), "sample_rate": source.samplerate,
                "bit_depth": int(source.subtype[4:]) if source.subtype.startswith("PCM_")
                and source.subtype[4:].isdigit() else source.subtype,
                "channels": source.channels, "subtype": source.subtype}


SPOTIFY_SCRIPT = r'''
if application "Spotify" is not running then return "stopped"
tell application "Spotify"
    set playbackState to player state as text
    if playbackState is not "playing" then return playbackState
    set separatorChar to ASCII character 0
    set trackID to id of current track as text
    set trackTitle to name of current track as text
    set trackArtist to artist of current track as text
    set trackAlbum to album of current track as text
    set trackNumber to track number of current track as text
    if (id of current track as text) is not trackID then error "Track changed during metadata query; retrying"
    return playbackState & separatorChar & trackID & separatorChar & trackTitle & separatorChar & trackArtist & separatorChar & trackAlbum & separatorChar & trackNumber
end tell
'''


def spotify_snapshot() -> Track | None:
    result = subprocess.run(["/usr/bin/osascript", "-e", SPOTIFY_SCRIPT],
                            capture_output=True, text=True, timeout=3, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Spotify AppleScript failed")
    # Only strip the one newline supplied by osascript, retaining metadata whitespace.
    fields = result.stdout.removesuffix("\n").split("\0")
    if fields[0] in {"paused", "stopped"}:
        return None
    if len(fields) != 6 or fields[0] != "playing" or not fields[1]:
        raise RuntimeError(f"Unexpected Spotify response: {result.stdout!r}")
    try:
        number = max(0, int(fields[5].strip()))
    except ValueError:
        number = 0
    return Track(fields[1], fields[2] or "Untitled", fields[3] or "Unknown Artist", fields[4], number)


@dataclass(frozen=True)
class PlaybackEvent:
    at: float
    track: Track | None


@dataclass
class AudioBlock:
    at: float
    data: np.ndarray


class SpotifyWatcher(threading.Thread):
    def __init__(self, events: queue.SimpleQueue, stop: threading.Event,
                 fail: Callable[[str], None], warn: Callable[[str], None]):
        super().__init__(name="spotify-watcher", daemon=False)
        self.events, self.stop_event, self.fail, self.warn = events, stop, fail, warn

    def run(self) -> None:
        previous: Track | None = None
        first = True
        failures = 0
        deadline = time.monotonic()
        while not self.stop_event.is_set():
            try:
                track = spotify_snapshot()
                failures = 0
                # A stable ID defines a track; changes in ancillary fields do not split it.
                if first or (track.id if track else None) != (previous.id if previous else None):
                    self.events.put(PlaybackEvent(time.monotonic(), track))
                previous, first = track, False
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                failures += 1
                self.events.put(PlaybackEvent(time.monotonic(), None))
                previous, first = None, True
                self.warn(f"Spotify query failed ({failures}/3): {exc}")
                if failures >= 3:
                    self.fail("Spotify watcher failed three times. Check Automation permission and Spotify.")
                    return
            deadline += POLL_SEC
            now = time.monotonic()
            if deadline < now:
                deadline = now
            self.stop_event.wait(max(0, deadline - now))


class TrackSink:
    """Single-thread owner of a track file, metadata and recoverable partial files."""
    def __init__(self, output: Path, rate: int, channels: int, extension: str, depth: int,
                 saved: list[dict], warn: Callable[[str], None]):
        self.output, self.rate, self.channels = output, rate, channels
        self.extension, self.depth, self.saved, self.warn = extension, depth, saved, warn
        self.file: sf.SoundFile | None = None
        self.track: Track | None = None
        self.partial: Path | None = None
        self.destination: Path | None = None
        self.frames = 0

    def start(self, track: Track) -> None:
        self.finish()
        self.track = track
        self.destination = self.output / track.filename(self.extension)
        self.partial = partial_path(self.destination)
        self.file = sf.SoundFile(self.partial, mode="x", samplerate=self.rate,
                                 channels=self.channels, format=FORMATS[self.extension],
                                 subtype=f"PCM_{self.depth}")
        self.frames = 0
        print(f"Recording: {track.artist} — {track.title}", flush=True)

    def write(self, data: np.ndarray) -> None:
        if self.file is None:
            raise RuntimeError("No active track file")
        self.file.write(data)
        self.frames += len(data)

    def finish(self, incomplete: bool = False) -> None:
        if self.file is None:
            return
        handle, self.file = self.file, None
        track, partial, destination = self.track, self.partial, self.destination
        assert track is not None and partial is not None and destination is not None
        entry = {"path": str(partial.resolve()), "track": asdict(track), "frames": self.frames,
                 "duration_sec": self.frames / self.rate, "sample_rate": self.rate,
                 "bit_depth": self.depth, "channels": self.channels, "status": "incomplete"}
        try:
            handle.flush()
            handle.close()
            if not self.frames:
                partial.unlink(missing_ok=True)
                return
            if not incomplete:
                try:
                    write_metadata(partial, track.metadata(), self.extension)
                    entry["status"] = "saved"
                except Exception as exc:
                    entry["status"] = "metadata_error"
                    entry["error"] = str(exc)
                    self.warn(f"Audio saved but tagging failed for {destination.name}: {exc}")
                destination = publish_file(partial, destination)
                entry["path"] = str(destination.resolve())
            print(f"Saved: {entry['path']} ({entry['duration_sec']:.3f} s, {entry['status']})", flush=True)
        except Exception as exc:
            entry["error"] = str(exc)
            self.warn(f"Could not finalize {partial}: {exc}. Keep the partial file for recovery.")
            raise
        finally:
            if not handle.closed:
                handle.close()
            if self.frames:
                self.saved.append(entry)


class MetadataSplitter:
    def __init__(self, sink: TrackSink, rate: int, events: queue.SimpleQueue,
                 warn: Callable[[str], None]):
        self.sink, self.rate, self.events, self.warn = sink, rate, events, warn
        self.pending: deque[PlaybackEvent] = deque()
        self.current: Track | None = None
        self.last_end: float | None = None

    def collect(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if self.last_end is not None and event.at < self.last_end:
                self.warn("Late metadata event: boundary moved to the next unwritten frame.")
            self.pending.append(event)

    def transition(self, track: Track | None) -> None:
        if (self.current.id if self.current else None) != (track.id if track else None):
            self.sink.finish()
            self.current = track

    def emit(self, data: np.ndarray) -> None:
        if len(data) and self.current is not None:
            if self.sink.file is None:
                self.sink.start(self.current)
            self.sink.write(data)

    def feed(self, block: AudioBlock) -> None:
        self.collect()
        offset = 0
        end = block.at + len(block.data) / self.rate
        while self.pending and self.pending[0].at <= end:
            event = self.pending.popleft()
            index = min(len(block.data), max(offset, round((event.at - block.at) * self.rate)))
            self.emit(block.data[offset:index])
            self.transition(event.track)
            offset = index
        self.emit(block.data[offset:])
        self.last_end = end

    def finish(self) -> None:
        self.sink.finish()


class SilenceSplitter:
    """Sample-level threshold gate with a bounded pre-roll and trailing-silence buffer.

    A split happens after a continuous below-threshold run. Keep up to pad frames
    on each edge; discard the middle. Short quiet passages stay in the track.
    """
    def __init__(self, sink: TrackSink, rate: int, threshold_db: float,
                 min_silence_sec: float, pad_sec: float):
        self.sink, self.rate = sink, rate
        self.threshold = 10 ** (threshold_db / 20)
        self.minimum = max(1, round(min_silence_sec * rate))
        self.pad = round(pad_sec * rate)
        self.quiet: deque[np.ndarray] = deque()
        self.quiet_frames = 0
        self.active = False
        self.sequence = 0

    def append_quiet(self, data: np.ndarray) -> None:
        self.quiet.append(data.copy())
        self.quiet_frames += len(data)

    def trim_quiet(self, keep: int) -> None:
        while self.quiet and self.quiet_frames > keep:
            excess = self.quiet_frames - keep
            first = self.quiet.popleft()
            if len(first) > excess:
                self.quiet.appendleft(first[excess:])
                self.quiet_frames -= excess
            else:
                self.quiet_frames -= len(first)

    def write_quiet(self, limit: int | None = None) -> None:
        remaining = self.quiet_frames if limit is None else limit
        for chunk in self.quiet:
            if remaining <= 0:
                break
            self.sink.write(chunk[:remaining])
            remaining -= len(chunk)

    def clear_quiet(self) -> None:
        self.quiet.clear()
        self.quiet_frames = 0

    def feed(self, block: AudioBlock) -> None:
        # Peak across channels prevents a quiet channel from closing the gate.
        loud = np.max(np.abs(block.data), axis=1) > self.threshold
        edges = np.flatnonzero(loud[1:] != loud[:-1]) + 1
        bounds = np.concatenate(([0], edges, [len(loud)]))
        for start, end in zip(bounds[:-1], bounds[1:]):
            data = block.data[start:end]
            if loud[start]:
                if not self.active:
                    self.sequence += 1
                    self.sink.start(Track(f"silence:{self.sequence}", f"Track {self.sequence:03d}",
                                          number=self.sequence))
                    self.active = True
                self.write_quiet()
                self.clear_quiet()
                self.sink.write(data)
            else:
                self.append_quiet(data)
                if self.active and self.quiet_frames >= self.minimum:
                    self.write_quiet(self.pad)
                    self.sink.finish()
                    self.active = False
                if not self.active:
                    self.trim_quiet(self.pad)

    def finish(self) -> None:
        if self.active:
            self.write_quiet(self.pad)
        self.sink.finish()


def load_sounddevice():
    try:
        import sounddevice as sd
        return sd
    except (ImportError, OSError) as exc:
        raise RuntimeError(f"Could not load PortAudio: {exc}. Reinstall requirements; if needed, brew install portaudio.") from exc


ROUTING_HELP = (
    "Install BlackHole 2ch from https://existential.audio/blackhole/ . In Audio MIDI Setup, "
    "set BlackHole and your speakers/headphones to the requested sample rate. Create a "
    "Multi-Output Device containing both, use speakers as clock source, enable drift "
    "correction for BlackHole, then select that Multi-Output Device as macOS sound output. "
    "Grant Microphone access to the app launching Python (Terminal or Codex) and Automation "
    "access to Spotify. See README.md for the complete setup."
)


def list_devices() -> None:
    sd = load_sounddevice()
    print("Index  Input channels  Default rate  Device")
    for index, info in enumerate(sd.query_devices()):
        print(f"{index:5d}  {info['max_input_channels']:14d}  {info['default_samplerate']:12.0f}  {info['name']}")


def select_device(sd, requested: str | None, rate: int, channels: int) -> tuple[int, dict]:
    devices = sd.query_devices()
    inputs = [(i, d) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
    if requested is None:
        matches = [(i, d) for i, d in inputs if "blackhole 2ch" in d["name"].lower()]
    elif requested.isdecimal():
        matches = [(i, d) for i, d in inputs if i == int(requested)]
    else:
        matches = [(i, d) for i, d in inputs if requested.lower() == d["name"].lower()]
        if not matches:
            matches = [(i, d) for i, d in inputs if requested.lower() in d["name"].lower()]
    if not matches:
        raise ValueError(f"Input device {requested or 'BlackHole 2ch'!r} not found. Use --list-devices.\n{ROUTING_HELP}")
    if len(matches) != 1:
        raise ValueError("Ambiguous input device name; select an index with --device. Use --list-devices.")
    index, info = matches[0]
    if channels > info["max_input_channels"]:
        raise ValueError(f"{info['name']} supports {info['max_input_channels']} input channels, not {channels}.")
    try:
        sd.check_input_settings(device=index, channels=channels, samplerate=rate, dtype="float32")
    except Exception as exc:
        raise ValueError(f"Unsupported input settings on {info['name']}: {exc}\n{ROUTING_HELP}") from exc
    if not math.isclose(info["default_samplerate"], rate, abs_tol=0.5):
        print(f"Warning: {info['name']} default rate is {info['default_samplerate']:.0f} Hz; "
              f"requesting {rate} Hz. Core Audio may resample. Match both devices in Audio MIDI Setup.",
              file=sys.stderr)
    return index, info


class Recorder:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.stop = threading.Event()
        self.watcher_stop = threading.Event()
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.audio: queue.Queue = queue.Queue(maxsize=max(2, math.ceil(args.buffer_seconds * args.sample_rate / BLOCK_FRAMES)))
        self.saved: list[dict] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.callback_error: str | None = None
        self.captured_frames = 0
        self.dropped_frames = 0
        self.input_overflows = 0
        self.interrupted = False
        self.writer_failed = threading.Event()
        self.started = utc_now()
        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.output = Path(args.output).expanduser().resolve()
        self.stream_started = 0.0

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        print(f"Warning: {message}", file=sys.stderr, flush=True)

    def fail(self, message: str) -> None:
        self.errors.append(message)
        self.stop.set()

    def callback(self, data, frames, timing, status) -> None:
        # No disk access, subprocesses, printing or waiting for queue space here.
        try:
            if status.input_overflow:
                self.input_overflows += 1
                self.callback_error = "Core Audio input overflow: audio frames were lost. Session stopped."
                self.stop.set()
            elif status:
                self.callback_error = f"Core Audio callback status: {status}"
                self.stop.set()
            at = time.monotonic() + timing.inputBufferAdcTime - timing.currentTime
            self.audio.put_nowait(AudioBlock(at, data.copy()))
            self.captured_frames += frames
        except queue.Full:
            self.dropped_frames += frames
            self.callback_error = "Audio queue full: disk/writer cannot keep up. Session stopped; a block was lost."
            self.stop.set()
        except Exception as exc:
            self.callback_error = f"Audio callback failed: {exc}"
            self.stop.set()

    def writer(self) -> None:
        args = self.args
        sink = TrackSink(self.output, args.sample_rate, args.channels, args.format,
                         args.bit_depth, self.saved, self.warn)
        splitter = (SilenceSplitter(sink, args.sample_rate, args.threshold_db,
                                   args.min_silence_sec, args.pad_sec) if args.split_silence
                    else MetadataSplitter(sink, args.sample_rate, self.events, self.warn))
        pending: deque[AudioBlock] = deque()
        done = False
        silent_frames = 0
        signal_seen = False
        silence_warned = False
        try:
            while not done or pending:
                if not done:
                    try:
                        block = self.audio.get(timeout=0.05)
                        if block is None:
                            done = True
                        else:
                            pending.append(block)
                    except queue.Empty:
                        pass
                if isinstance(splitter, MetadataSplitter):
                    splitter.collect()
                cutoff = time.monotonic() - HOLDBACK_SEC
                while pending and (done or pending[0].at + len(pending[0].data) / args.sample_rate <= cutoff):
                    block = pending.popleft()
                    if not signal_seen:
                        if np.max(np.abs(block.data)) > 1e-7:
                            signal_seen = True
                        else:
                            silent_frames += len(block.data)
                        if not silence_warned and silent_frames >= args.sample_rate * 15:
                            self.warn("No input signal detected for 15 seconds. If audio is playing, check Multi-Output routing and BlackHole levels.")
                            silence_warned = True
                    splitter.feed(block)
            splitter.finish()
        except Exception as exc:
            self.writer_failed.set()
            self.fail(f"Writer failed: {type(exc).__name__}: {exc}")
            try:
                sink.finish(incomplete=True)
            except Exception as close_exc:
                self.warn(f"Partial-file recovery failed: {close_exc}")

    def on_signal(self, signum, _frame) -> None:
        self.interrupted = True
        self.stop.set()

    def run(self) -> int:
        args = self.args
        if not args.split_silence and platform.system() != "Darwin":
            raise RuntimeError("Spotify AppleScript mode requires macOS. --split-silence supports other platforms.")
        sd = load_sounddevice()
        device_index, device_info = select_device(sd, args.device, args.sample_rate, args.channels)
        self.output.mkdir(parents=True, exist_ok=True)
        # Probe writable storage before allocating the audio stream.
        probe = self.output / f".recorder-probe-{uuid.uuid4().hex}"
        with probe.open("x"):
            pass
        probe.unlink()
        if not args.split_silence:
            try:
                initial = spotify_snapshot()
            except Exception as exc:
                raise RuntimeError(f"Spotify preflight failed: {exc}\nAllow Automation access to Spotify in System Settings → Privacy & Security → Automation.") from exc
        else:
            initial = None
        old_handlers = {sig: signal.signal(sig, self.on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
        writer = threading.Thread(target=self.writer, name="audio-writer", daemon=False)
        watcher = None
        stream = None
        writer.start()
        try:
            stream = sd.InputStream(device=device_index, samplerate=args.sample_rate,
                                    channels=args.channels, dtype="float32", blocksize=BLOCK_FRAMES,
                                    latency="low", callback=self.callback)
            self.stream_started = time.monotonic()
            self.events.put(PlaybackEvent(self.stream_started, initial))
            stream.start()
            if not args.split_silence:
                watcher = SpotifyWatcher(self.events, self.watcher_stop, self.fail, self.warn)
                watcher.start()
            print(f"Listening to {device_info['name']} at {args.sample_rate} Hz / {args.channels} ch; "
                  f"{args.bit_depth}-bit {args.format.upper()} → {self.output}", flush=True)
            print("Silence splitting active. Ctrl+C to finish." if args.split_silence else
                  "Standby: press Play in Spotify. Ctrl+C to finish and save the session report.", flush=True)
            while not self.stop.wait(0.1):
                if not stream.active:
                    self.fail("Audio input stream stopped unexpectedly. Check device connection and permissions.")
                if args.max_duration and time.monotonic() - self.stream_started >= args.max_duration:
                    self.stop.set()
        except Exception as exc:
            self.fail(f"Capture failed: {exc}\n{ROUTING_HELP}")
        finally:
            # Stop production first, then the watcher, then drain every accepted block.
            self.watcher_stop.set()
            if stream is not None:
                try:
                    stream.stop()
                except Exception as exc:
                    self.fail(f"Could not stop input stream: {exc}")
                finally:
                    try:
                        stream.close()
                    except Exception as exc:
                        self.fail(f"Could not close input stream: {exc}")
            if watcher is not None:
                watcher.join()  # osascript has a 3-second timeout.
            if self.callback_error:
                self.errors.append(self.callback_error)
            if writer.is_alive():
                while writer.is_alive():
                    try:
                        self.audio.put(None, timeout=0.1)
                        break
                    except queue.Full:
                        continue
                writer.join()
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            if any(entry["status"] != "saved" for entry in self.saved):
                self.errors.append("One or more tracks could not be fully finalized/tagged; inspect track status.")
            report = self.output / f"session-{self.session_id}.json"
            atomic_json(report, {
                "schema_version": 1, "started_at": self.started, "ended_at": utc_now(),
                "status": "error" if self.errors else "interrupted" if self.interrupted else "complete",
                "device": device_info["name"], "sample_rate": args.sample_rate,
                "channels": args.channels, "format": args.format, "bit_depth": args.bit_depth,
                "mode": "silence" if args.split_silence else "spotify",
                "captured_frames": self.captured_frames,
                "queue_dropped_frames": self.dropped_frames,
                "input_overflow_count": self.input_overflows, "tracks": self.saved,
                "saved_duration_sec": sum(e["duration_sec"] for e in self.saved),
                "warnings": self.warnings, "errors": self.errors,
                "boundary_note": "Spotify boundaries are observed at 0.25-second polls plus AppleScript/Core Audio latency; not sample-exact.",
            })
            print(f"\nSession report: {report}", flush=True)
            print(f"{len(self.saved)} file(s), {sum(e['duration_sec'] for e in self.saved):.3f} seconds saved.", flush=True)
            for entry in self.saved:
                print(f"  {entry['path']} — {entry['duration_sec']:.3f} s")
            for error in self.errors:
                print(f"Error: {error}", file=sys.stderr)
        return 1 if self.errors else 0


def resampled_blocks(source: sf.SoundFile, target_rate: int,
                     block_frames: int = 2048) -> Iterator[np.ndarray]:
    """Bounded-memory, zero-phase windowed-sinc resampling using only NumPy.

    A Kaiser-windowed low-pass (64 zero crossings per side, beta=8.6) prevents
    downsampling aliases. Rational integer positions avoid accumulating drift.
    Boundary samples use endpoint extension. Output length is ceil(N * ratio).
    """
    if source.samplerate == target_rate:
        yield from source.blocks(blocksize=65536, dtype="float64", always_2d=True)
        return
    if not source.frames:
        return
    divisor = math.gcd(source.samplerate, target_rate)
    step, phases = source.samplerate // divisor, target_rate // divisor
    count = (source.frames * phases + step - 1) // step
    cutoff = 0.95 * min(1.0, target_rate / source.samplerate)
    radius = math.ceil(64 / cutoff)
    offsets = np.arange(-radius + 1, radius + 1, dtype=np.int64)
    # Bound the temporary matrix even for unusually high source sample rates.
    block_frames = min(block_frames, max(1, 1_000_000 // len(offsets)))
    norm = np.i0(8.6)
    for start in range(0, count, block_frames):
        output_indices = np.arange(start, min(count, start + block_frames), dtype=np.int64)
        centers, remainder = np.divmod(output_indices * step, phases)
        distances = offsets[None, :] - remainder[:, None] / phases
        window = np.i0(8.6 * np.sqrt(np.maximum(0, 1 - (distances / radius) ** 2))) / norm
        window[np.abs(distances) >= radius] = 0
        weights = cutoff * np.sinc(cutoff * distances) * window
        weights /= weights.sum(axis=1, keepdims=True)
        indices = np.clip(centers[:, None] + offsets, 0, source.frames - 1)
        lo, hi = int(indices.min()), int(indices.max()) + 1
        source.seek(lo)
        data = source.read(hi - lo, dtype="float64", always_2d=True)
        # One channel at a time avoids allocating a taps × frames × channels tensor.
        output = np.empty((len(centers), source.channels), dtype=np.float64)
        local = indices - lo
        for channel in range(source.channels):
            output[:, channel] = np.einsum("ij,ij->i", data[:, channel][local], weights)
        yield output


def write_process_report(output: Path, records: list[dict], report_format: str,
                         run_id: str, interrupted: bool) -> Path:
    path = output / f"report-{run_id}.{report_format}"
    if report_format == "json":
        atomic_json(path, {"schema_version": 1, "created_at": utc_now(),
                           "interrupted": interrupted, "files": records,
                           "metrics_note": "Post-conversion PCM sample peak and all-channel integrated RMS; null means -infinity dBFS. Not LUFS or true peak."})
    else:
        temporary = partial_path(path)
        fields = ["source", "path", "status", "duration_sec", "frames", "sample_rate",
                  "bit_depth", "channels", "subtype", "peak_dbfs", "rms_dbfs", "warnings", "error"]
        try:
            with temporary.open("x", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for record in records:
                    row = dict(record)
                    row["warnings"] = "; ".join(row.get("warnings", []))
                    writer.writerow(row)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def process_directory(args: argparse.Namespace) -> int:
    source_dir = Path(args.input_dir).expanduser().resolve()
    output = Path(args.output_dir).expanduser().resolve()
    if not source_dir.is_dir():
        raise ValueError(f"Input directory does not exist: {source_dir}")
    if source_dir == output:
        raise ValueError("Use a separate output directory to preserve original files.")
    sources = sorted(p for p in source_dir.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS)
    if not sources:
        raise ValueError(f"No WAV, FLAC or AIFF files found in {source_dir}")
    output.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    records: list[dict] = []
    interrupted = False
    try:
        for path in sources:
            destination = output / f"{safe_component(path.stem)}.{args.format}"
            temporary = partial_path(destination)
            record = {"source": str(path), "status": "error"}
            try:
                metadata = read_metadata(path)
                with sf.SoundFile(path) as source, sf.SoundFile(
                    temporary, mode="x", samplerate=args.sample_rate, channels=source.channels,
                    format=FORMATS[args.format], subtype=f"PCM_{args.bit_depth}"
                ) as target:
                    clipped = 0
                    for data in resampled_blocks(source, args.sample_rate):
                        if not np.isfinite(data).all():
                            raise ValueError("Source contains NaN or infinite audio samples")
                        clipped += int(np.count_nonzero((data < -1.0) | (data >= 1.0)))
                        target.write(data)
                write_metadata(temporary, metadata, args.format)
                record.update(measure_file(temporary))
                warnings = list(metadata.warnings) if args.format == "flac" else []
                if clipped:
                    warnings.append(f"{clipped} samples reached/exceeded PCM full scale during conversion")
                record["warnings"] = warnings
                destination = publish_file(temporary, destination)
                record.update(path=str(destination), status="saved")
                print(f"Processed: {destination} ({record['duration_sec']:.3f} s)", flush=True)
            except KeyboardInterrupt:
                record.update(status="interrupted", error="Conversion interrupted; source unchanged")
                records.append(record)
                raise
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
                print(f"Error processing {path.name}: {exc}", file=sys.stderr)
            finally:
                temporary.unlink(missing_ok=True)
            records.append(record)
    except KeyboardInterrupt:
        interrupted = True
        print("\nProcessing interrupted; writing report for completed/attempted files.", file=sys.stderr)
    report = write_process_report(output, records, args.report_format, run_id, interrupted)
    print(f"Report: {report}")
    return 130 if interrupted else 1 if any(r["status"] != "saved" for r in records) else 0


def positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise argparse.ArgumentTypeError("must be finite")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="listen continuously and record Spotify tracks (default)")
    record.add_argument("--list-devices", action="store_true", help="list Core Audio devices and exit")
    record.add_argument("--device", help="input device index or unique name; default: BlackHole 2ch")
    record.add_argument("--channels", type=positive_int, default=2)
    record.add_argument("--output", default="./recorded_tracks")
    record.add_argument("--split-silence", action="store_true", help="split non-Spotify input on quiet gaps; bypass AppleScript")
    record.add_argument("--threshold-db", type=finite_float, default=-45.0)
    record.add_argument("--min-silence-sec", type=finite_float, default=2.0)
    record.add_argument("--pad-sec", type=finite_float, default=0.15)
    record.add_argument("--buffer-seconds", type=finite_float, default=10.0, help="bounded audio queue capacity (default: 10 seconds)")
    record.add_argument("--max-duration", type=finite_float, default=0, help="stop after this many wall-clock seconds; 0 = indefinite")
    process = commands.add_parser("process", help="standardize files and report post-conversion sample peak/RMS")
    process.add_argument("--input-dir", required=True)
    process.add_argument("--output-dir", required=True)
    process.add_argument("--report-format", choices=("json", "csv"), default="json")
    for command in (record, process):
        command.add_argument("--sample-rate", type=positive_int, default=44100)
        command.add_argument("--format", choices=tuple(FORMATS), default="flac")
        command.add_argument("--bit-depth", type=int, choices=(16, 24), default=16)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Permit no subcommand, npm run dev -- --device ..., and explicit record/process.
    if not argv or argv[0] not in {"record", "process", "-h", "--help"}:
        argv.insert(0, "record")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "record":
        if args.list_devices:
            try:
                list_devices()
                return 0
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
        if not -160 <= args.threshold_db <= 0:
            parser.error("--threshold-db must be between -160 and 0 dBFS")
        if args.min_silence_sec <= 0 or args.pad_sec < 0 or args.pad_sec * 2 > args.min_silence_sec:
            parser.error("require --min-silence-sec > 0 and 0 <= 2 * --pad-sec <= --min-silence-sec")
        if args.buffer_seconds < 1 or args.max_duration < 0:
            parser.error("--buffer-seconds must be >= 1 and --max-duration must be >= 0")
    try:
        return Recorder(args).run() if args.command == "record" else process_directory(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
