"""Deterministic signal and lifecycle tests; no real audio device is opened."""
import argparse
import contextlib
import io
import json
import math
from pathlib import Path
import queue
import signal
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf
from mutagen.flac import Picture

import auto_recorder as ar


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.log)
        self.stderr = contextlib.redirect_stderr(self.log)
        self.stdout.__enter__()
        self.stderr.__enter__()

    def tearDown(self):
        self.stderr.__exit__(None, None, None)
        self.stdout.__exit__(None, None, None)
        self.temp.cleanup()

    def sink(self, rate=1000, extension="flac", depth=16):
        saved, warnings = [], []
        sink = ar.TrackSink(self.root, rate, 2, extension, depth, saved, warnings.append)
        return sink, saved, warnings

    def args(self, *extra):
        return ar.build_parser().parse_args(["record", "--output", str(self.root), *extra])

    def test_metadata_boundaries_pause_resume_and_no_frame_loss(self):
        sink, saved, warnings = self.sink()
        events = queue.SimpleQueue()
        a = ar.Track("a", "First", "Artist", "Album", 1)
        b = ar.Track("b", "Second", "Artist", "Album", 2)
        for at, track in [(100, a), (100.135, b), (100.250, None), (100.300, a)]:
            events.put(ar.PlaybackEvent(at, track))
        splitter = ar.MetadataSplitter(sink, 1000, events, warnings.append)
        # Exactly representable PCM values let us check every retained sample.
        signal_data = (np.arange(350 * 2).reshape(-1, 2) - 350) / 32768
        for start in range(0, 350, 100):
            splitter.feed(ar.AudioBlock(100 + start / 1000, signal_data[start:start + 100]))
        splitter.finish()
        self.assertEqual([e["frames"] for e in saved], [135, 115, 50])
        self.assertIn("(2)", saved[2]["path"])
        result = np.concatenate([sf.read(e["path"], always_2d=True)[0] for e in saved])
        np.testing.assert_array_equal(result, np.concatenate([signal_data[:250], signal_data[300:]]))
        self.assertEqual(warnings, [])
        self.assertEqual(ar.read_metadata(Path(saved[1]["path"])).values["title"], ["Second"])

    def test_same_id_does_not_split_and_exact_block_edge(self):
        sink, saved, warnings = self.sink()
        events = queue.SimpleQueue()
        events.put(ar.PlaybackEvent(0, ar.Track("same", "Title")))
        events.put(ar.PlaybackEvent(0.1, ar.Track("same", "Updated title")))
        events.put(ar.PlaybackEvent(0.2, ar.Track("next", "Next")))
        splitter = ar.MetadataSplitter(sink, 1000, events, warnings.append)
        for at in [0, 0.1, 0.2]:
            splitter.feed(ar.AudioBlock(at, np.full((100, 2), 0.25)))
        splitter.finish()
        self.assertEqual([e["frames"] for e in saved], [200, 100])

    def test_silence_split_padding_and_short_quiet_passage(self):
        sink, saved, _ = self.sink()
        gate = ar.SilenceSplitter(sink, 1000, -40, 0.1, 0.02)
        z = lambda n: np.zeros((n, 2))
        a = lambda n: np.full((n, 2), 0.25)
        data = np.concatenate([z(100), a(80), z(30), a(70), z(200), a(50), z(150)])
        for start in range(0, len(data), 37):
            gate.feed(ar.AudioBlock(start / 1000, data[start:start + 37]))
        gate.finish()
        self.assertEqual([e["frames"] for e in saved], [220, 90])
        first = sf.read(saved[0]["path"], always_2d=True)[0]
        np.testing.assert_array_equal(first, np.concatenate([z(20), a(80), z(30), a(70), z(20)]))

    def test_all_silence_produces_no_file_and_bounded_preroll(self):
        sink, saved, _ = self.sink()
        gate = ar.SilenceSplitter(sink, 1000, -40, 0.1, 0.02)
        for n in range(100):
            gate.feed(ar.AudioBlock(n, np.zeros((1000, 2))))
            self.assertLessEqual(gate.quiet_frames, 20)
        gate.finish()
        self.assertEqual(saved, [])

    def test_tagging_all_formats_and_bit_depths(self):
        for extension in ar.FORMATS:
            for depth in (16, 24):
                with self.subTest(extension=extension, depth=depth):
                    sink, saved, warnings = self.sink(extension=extension, depth=depth)
                    track = ar.Track("id", "A/B: song", "Éléphant", "Test album", 7)
                    sink.start(track)
                    sink.write(np.full((123, 2), 0.25))
                    sink.finish()
                    result = ar.read_metadata(Path(saved[0]["path"]))
                    for key, value in track.metadata().values.items():
                        self.assertEqual(result.values[key], value)
                    self.assertEqual(sf.info(saved[0]["path"]).subtype, f"PCM_{depth}")
                    self.assertEqual(saved[0]["status"], "saved")
                    self.assertEqual(warnings, [])

    def test_tag_failure_preserves_audio_and_records_error(self):
        sink, saved, warnings = self.sink()
        sink.start(ar.Track("id", "test"))
        sink.write(np.full((100, 2), 0.25))
        with patch.object(ar, "write_metadata", side_effect=OSError("tag failure")):
            sink.finish()
        self.assertEqual(saved[0]["status"], "metadata_error")
        self.assertTrue(Path(saved[0]["path"]).exists())
        self.assertEqual(sf.info(saved[0]["path"]).frames, 100)
        self.assertTrue(warnings)

    def test_incomplete_file_is_retained_for_recovery(self):
        sink, saved, _ = self.sink()
        sink.start(ar.Track("id", "test"))
        sink.write(np.full((100, 2), 0.25))
        sink.finish(incomplete=True)
        self.assertEqual(saved[0]["status"], "incomplete")
        self.assertTrue(saved[0]["path"].endswith(".partial"))
        self.assertEqual(sf.info(saved[0]["path"]).frames, 100)

    def test_publish_collision_never_overwrites(self):
        path = self.root / "test.flac"
        path.write_bytes(b"original")
        temporary = ar.partial_path(path)
        temporary.write_bytes(b"new")
        result = ar.publish_file(temporary, path)
        self.assertNotEqual(result, path)
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(result.read_bytes(), b"new")

    def test_unicode_filenames_stay_within_macos_component_limit(self):
        track = ar.Track("id", "音" * 500, "a/b\x00c", number=1)
        name = track.filename("aiff")
        self.assertLessEqual(len(name.encode("utf-8")), 225)
        self.assertNotIn("/", name)
        self.assertNotIn("\x00", name)
        sink, saved, _ = self.sink(extension="aiff")
        sink.start(track)
        sink.write(np.zeros((10, 2)))
        sink.finish()
        self.assertTrue(Path(saved[0]["path"]).exists())
        self.assertLessEqual(len(ar.partial_path(self.root / name).name.encode("utf-8")), 255)

    def test_exact_rms_peak_and_silence_metrics(self):
        path = self.root / "metric.wav"
        sf.write(path, np.full((1000, 2), 0.5), 1000, subtype="PCM_16")
        result = ar.measure_file(path)
        self.assertEqual(result["duration_sec"], 1)
        self.assertAlmostEqual(result["peak_dbfs"], -6.020599913279624)
        self.assertAlmostEqual(result["rms_dbfs"], result["peak_dbfs"])
        sf.write(path, np.zeros((100, 2)), 1000, subtype="PCM_16")
        self.assertIsNone(ar.measure_file(path)["rms_dbfs"])
        self.assertIsNone(ar.measure_file(path)["peak_dbfs"])

    def test_resampler_frame_count_dc_stability_and_chunk_continuity(self):
        for source_rate, target_rate in [(48000, 44100), (22050, 44100), (44100, 44100)]:
            with self.subTest(source_rate=source_rate):
                path = self.root / "dc.wav"
                sf.write(path, np.full((5003, 2), 0.25), source_rate, subtype="PCM_24")
                with sf.SoundFile(path) as source:
                    result = np.concatenate(list(ar.resampled_blocks(source, target_rate, block_frames=101)))
                self.assertEqual(len(result), math.ceil(5003 * target_rate / source_rate))
                np.testing.assert_allclose(result, 0.25, atol=1e-12)

    def test_resampler_passband_and_antialias(self):
        rate, target = 96000, 44100
        n = np.arange(19200)
        path = self.root / "tone.wav"
        for frequency, expected in [(1000, "pass"), (30000, "reject")]:
            sf.write(path, 0.5 * np.sin(2 * np.pi * frequency * n / rate), rate, subtype="PCM_24")
            with sf.SoundFile(path) as source:
                result = np.concatenate(list(ar.resampled_blocks(source, target)))[:, 0]
            interior = result[400:-400]
            rms = np.sqrt(np.mean(interior ** 2))
            if expected == "pass":
                self.assertAlmostEqual(rms, 0.5 / math.sqrt(2), delta=0.001)
            else:
                self.assertLess(rms, 1e-4)

    def test_resampler_empty_input(self):
        path = self.root / "empty.wav"
        sf.write(path, np.empty((0, 2)), 48000, subtype="PCM_16")
        with sf.SoundFile(path) as source:
            self.assertEqual(list(ar.resampled_blocks(source, 44100)), [])

    def test_cross_format_custom_tags_and_artwork(self):
        source = self.root / "source.flac"
        sf.write(source, np.ones((100, 2)) * 0.1, 44100)
        pic = Picture()
        pic.data, pic.mime, pic.type, pic.desc = b"example image payload", "image/png", 3, "Front"
        metadata = ar.Metadata({"title": ["Title"], "artist": ["One", "Two"], "custom_field": ["Value"]}, [pic])
        ar.write_metadata(source, metadata, "flac")
        wave = self.root / "destination.wav"
        sf.write(wave, np.ones((100, 2)) * 0.1, 44100)
        ar.write_metadata(wave, ar.read_metadata(source), "wav")
        result = ar.read_metadata(wave)
        self.assertEqual(result.values["custom_field"], ["Value"])
        self.assertEqual(result.values["artist"], ["One", "Two"])
        self.assertEqual(result.pictures[0].data, pic.data)
        back = self.root / "back.flac"
        sf.write(back, np.ones((100, 2)) * 0.1, 44100)
        ar.write_metadata(back, result, "flac")
        self.assertEqual(ar.read_metadata(back).values, metadata.values)

    def test_process_reports_actual_pcm_and_handles_bad_file(self):
        incoming, outgoing = self.root / "in", self.root / "out"
        incoming.mkdir()
        source = incoming / "tone.WAV"
        sf.write(source, np.full((4800, 2), 0.5), 48000, subtype="PCM_24")
        ar.write_metadata(source, ar.Track("id", "Fixture", number=3).metadata(), "wav")
        (incoming / "broken.flac").write_bytes(b"not audio")
        args = ar.build_parser().parse_args(["process", "--input-dir", str(incoming),
                                           "--output-dir", str(outgoing)])
        self.assertEqual(ar.process_directory(args), 1)
        report = json.loads(next(outgoing.glob("report-*.json")).read_text())
        self.assertEqual(len(report["files"]), 2)
        good = next(e for e in report["files"] if e["status"] == "saved")
        self.assertEqual(good["sample_rate"], 44100)
        self.assertEqual(good["bit_depth"], 16)
        self.assertAlmostEqual(good["rms_dbfs"], 20 * math.log10(0.5))
        self.assertEqual(ar.read_metadata(Path(good["path"])).values["title"], ["Fixture"])
        self.assertTrue(source.exists())
        self.assertFalse(list(outgoing.glob("*.partial")))

    def test_csv_report_and_silence_representation(self):
        record = {"source": "s", "path": "p", "status": "saved", "peak_dbfs": None,
                  "rms_dbfs": None, "warnings": ["warning,with comma"]}
        report = ar.write_process_report(self.root, [record], "csv", "test", False)
        import csv
        with report.open() as handle:
            row = list(csv.DictReader(handle))[0]
        self.assertEqual(row["rms_dbfs"], "")
        self.assertEqual(row["warnings"], "warning,with comma")

    def test_callback_queue_full_stops_without_blocking(self):
        recorder = ar.Recorder(self.args())
        recorder.audio = queue.Queue(maxsize=1)
        class NoFlags:
            input_overflow = False
            def __bool__(self):
                return False
        timing = SimpleNamespace(inputBufferAdcTime=1, currentTime=1)
        block = np.zeros((1024, 2), dtype=np.float32)
        recorder.callback(block, 1024, timing, NoFlags())
        recorder.callback(block, 1024, timing, NoFlags())
        self.assertTrue(recorder.stop.is_set())
        self.assertIn("queue full", recorder.callback_error)
        self.assertEqual(recorder.captured_frames, 1024)
        self.assertEqual(recorder.dropped_frames, 1024)

    def test_writer_drains_queued_audio_before_finalizing(self):
        recorder = ar.Recorder(self.args("--sample-rate", "1000"))
        now = time.monotonic()
        recorder.events.put(ar.PlaybackEvent(now, ar.Track("id", "Drain test")))
        for n in range(3):
            recorder.audio.put(ar.AudioBlock(now + n / 10, np.full((100, 2), 0.25)))
        recorder.audio.put(None)
        recorder.writer()
        self.assertEqual(recorder.errors, [])
        self.assertEqual(recorder.saved[0]["frames"], 300)

    def test_writer_failure_stops_session_and_keeps_partial(self):
        recorder = ar.Recorder(self.args("--sample-rate", "1000"))
        now = time.monotonic()
        recorder.events.put(ar.PlaybackEvent(now, ar.Track("id", "Disk failure")))
        recorder.audio.put(ar.AudioBlock(now, np.full((100, 2), 0.25)))
        recorder.audio.put(ar.AudioBlock(now + 0.1, np.full((100, 2), 0.25)))
        recorder.audio.put(None)
        original = ar.TrackSink.write
        calls = 0
        def failing_write(sink, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            original(sink, data)
        with patch.object(ar.TrackSink, "write", failing_write):
            recorder.writer()
        self.assertTrue(recorder.stop.is_set())
        self.assertIn("disk full", recorder.errors[0])
        self.assertEqual(recorder.saved[0]["status"], "incomplete")
        self.assertTrue(Path(recorder.saved[0]["path"]).exists())

    def test_batch_interrupt_cleans_partial_and_writes_report(self):
        incoming, outgoing = self.root / "in", self.root / "out"
        incoming.mkdir()
        sf.write(incoming / "source.wav", np.full((100, 2), 0.25), 44100)
        args = ar.build_parser().parse_args(["process", "--input-dir", str(incoming),
                                           "--output-dir", str(outgoing)])
        with patch.object(ar, "resampled_blocks", side_effect=KeyboardInterrupt):
            self.assertEqual(ar.process_directory(args), 130)
        report = json.loads(next(outgoing.glob("report-*.json")).read_text())
        self.assertTrue(report["interrupted"])
        self.assertEqual(report["files"][0]["status"], "interrupted")
        self.assertFalse(any(p.name.endswith(".partial") for p in outgoing.iterdir()))

    def test_recorder_stream_close_error_still_drains_and_reports(self):
        recorder = ar.Recorder(self.args("--split-silence", "--max-duration", "0.01"))
        sd = Mock()
        stream = sd.InputStream.return_value
        stream.active = True
        stream.close.side_effect = OSError("device disconnected")
        with patch.object(ar, "load_sounddevice", return_value=sd), patch.object(
            ar, "select_device", return_value=(0, {"name": "Fake input"})
        ):
            self.assertEqual(recorder.run(), 1)
        report = json.loads(next(self.root.glob("session-*.json")).read_text())
        self.assertEqual(report["status"], "error")
        self.assertIn("device disconnected", report["errors"][0])

    def test_recorder_sigint_stops_stream_drains_and_reports(self):
        args = self.args("--split-silence", "--sample-rate", "1000", "--pad-sec", "0")
        recorder = ar.Recorder(args)
        lifecycle = []
        class Flags:
            input_overflow = False
            def __bool__(self):
                return False
        class Stream:
            active = True
            def start(self):
                lifecycle.append("start")
                recorder.callback(np.full((100, 2), 0.25, dtype=np.float32), 100,
                                  SimpleNamespace(inputBufferAdcTime=1, currentTime=1), Flags())
                recorder.on_signal(signal.SIGINT, None)
            def stop(self):
                lifecycle.append("stop")
                recorder.callback(np.full((50, 2), 0.25, dtype=np.float32), 50,
                                  SimpleNamespace(inputBufferAdcTime=1, currentTime=1), Flags())
            def close(self):
                lifecycle.append("close")
        sd = Mock()
        sd.InputStream.return_value = Stream()
        previous = signal.getsignal(signal.SIGINT)
        with patch.object(ar, "load_sounddevice", return_value=sd), patch.object(
            ar, "select_device", return_value=(0, {"name": "Fake input"})
        ):
            self.assertEqual(recorder.run(), 0)
        self.assertEqual(lifecycle, ["start", "stop", "close"])
        self.assertIs(signal.getsignal(signal.SIGINT), previous)
        report = json.loads(next(self.root.glob("session-*.json")).read_text())
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual(report["tracks"][0]["frames"], 150)

    def test_device_selection_defaults_blackhole_and_rejects_microphone_fallback(self):
        sd = Mock()
        microphone = {"name": "Built-in microphone", "max_input_channels": 1, "default_samplerate": 48000}
        blackhole = {"name": "BlackHole 2ch", "max_input_channels": 2, "default_samplerate": 44100}
        sd.query_devices.return_value = [microphone, blackhole]
        self.assertEqual(ar.select_device(sd, None, 44100, 2), (1, blackhole))
        sd.query_devices.return_value = [microphone]
        with self.assertRaisesRegex(ValueError, "not found"):
            ar.select_device(sd, None, 44100, 2)
        with self.assertRaisesRegex(ValueError, "channels"):
            ar.select_device(sd, "0", 44100, 2)

    def test_spotify_snapshot_parses_metadata_and_does_not_require_playback(self):
        for output, expected in [("paused\n", None), ("stopped\n", None)]:
            with patch.object(ar.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=output)):
                self.assertEqual(ar.spotify_snapshot(), expected)
        # Join avoids treating the number as an octal escape in Python strings.
        snapshot = "\0".join(["playing", "spotify:track:id", "Title", "Artist", "Album", "7"]) + "\n"
        with patch.object(ar.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=snapshot)):
            self.assertEqual(ar.spotify_snapshot(), ar.Track("spotify:track:id", "Title", "Artist", "Album", 7))

    def test_watcher_fails_closed_after_three_errors(self):
        events, stop, failures, warnings = queue.SimpleQueue(), threading.Event(), [], []
        watcher = ar.SpotifyWatcher(events, stop, failures.append, warnings.append)
        with patch.object(ar, "spotify_snapshot", side_effect=RuntimeError("permission denied")), patch.object(ar, "POLL_SEC", 0):
            watcher.run()
        self.assertEqual(len(warnings), 3)
        self.assertEqual(len(failures), 1)
        self.assertTrue(all(events.get_nowait().track is None for _ in range(3)))

    def test_invalid_arguments_and_inplace_conversion(self):
        for argv in [["--pad-sec", "2"], ["--buffer-seconds", "0"], ["--threshold-db", "nan"]]:
            with self.assertRaises(SystemExit) as result:
                ar.main(argv)
            self.assertEqual(result.exception.code, 2)
        args = argparse.Namespace(input_dir=str(self.root), output_dir=str(self.root))
        with self.assertRaisesRegex(ValueError, "separate"):
            ar.process_directory(args)


if __name__ == "__main__":
    unittest.main()
