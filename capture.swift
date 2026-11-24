// capture.swift — record macOS system audio around AudioFlow for diagnostics.
//
// AudioFlow's signal path:
//   apps ──► "AudioFlow 2ch" (BlackHole loopback) ──► AudioFlow (amp → EQ → reverb) ──► real output device
//
// Taps:
//   pre    : raw app audio heading to the *selected* output device, before AudioFlow touches it
//            (reads the loopback's input side when the selected device is a loopback such as
//            "AudioFlow 2ch"; otherwise a global process tap excluding AudioFlow)
//   post   : Core Audio process tap on the AudioFlow backend = exactly what AudioFlow sends to the speakers
//   system : Core Audio global tap of every process (use when AudioFlow is NOT running, as a baseline)
//   both   : pre + post at the same time (default) → WAVs you can diff / compare spectra
//
// Recordings are split into one segment per track using Spotify / Apple Music now-playing metadata
// (see NowPlaying below); each segment gets <n>-<artist - title>-<stream>.wav plus a JSON sidecar,
// and the session folder gets a session.json manifest. The capture follows the system's selected
// output device (a device switch starts a new segment), and `pre` switches a loopback device to the
// track's native sample rate so it is recorded without resampling.
//
// Build:  swiftc -O capture.swift -o capture
// Run:    ./capture --duration 10
//
// Process taps need macOS 14.2+ and the "System Audio Recording" permission for the app running this
// (Terminal / iTerm / your IDE): System Settings ▸ Privacy & Security ▸ Screen & System Audio Recording.

import AudioToolbox
import CoreAudio
import Foundation

// MARK: - CLI

struct Options {
    var mode = "both"
    var duration: Double = 10
    var outDir = "recordings"
    var driverName: String? = nil      // pin pre to this device instead of following the selection
    var processName = "AudioFlow"
    var pid: pid_t? = nil
    var clock: String? = nil
    var list = false
    var track: String? = nil
    var once = false
    var keepRate = false
    var durationGiven = false
}

func usage() -> Never {
    print("""
    usage: capture [--mode pre|post|both|system] [--track TEXT [--once]] [--duration SEC] [--out DIR]
                   [--driver NAME] [--keep-rate] [--process NAME | --pid PID] [--clock DEVICE] [--list]

      --mode       pre    = raw app audio heading to the selected output device, before AudioFlow
                   post   = AudioFlow's processed output (process tap on the AudioFlow backend)
                   both   = pre + post simultaneously (default)
                   system = global tap of all processes (baseline when AudioFlow is not running)
      --track      only keep audio while the now-playing track matches TEXT
                   (substring of "artist - title", or an exact Spotify/Music track ID)
      --once       with --track: stop after the test track's segment ends
      --duration   seconds to record, 0 = until Ctrl-C (default 10, or 0 with --track)
      --out        output directory; each run gets a timestamped folder (default ./recordings)
      --driver     pin pre to this loopback device instead of following the selected output device
      --keep-rate  don't switch the pre loopback to the track's native sample rate
      --process    backend process name to tap for post (default "AudioFlow")
      --pid        tap this pid instead of looking the process up by name
      --clock      output device that clocks the tap aggregate (default: auto)
      --list       list audio devices and audio-producing processes, then exit
    """)
    exit(2)
}

func parseOptions() -> Options {
    var o = Options()
    var args = CommandLine.arguments.dropFirst().makeIterator()
    func next(_ flag: String) -> String {
        guard let v = args.next() else { print("missing value for \(flag)"); usage() }
        return v
    }
    while let a = args.next() {
        switch a {
        case "--mode": o.mode = next(a)
        case "--duration": o.duration = Double(next(a)) ?? { usage() }(); o.durationGiven = true
        case "--track": o.track = next(a)
        case "--once": o.once = true
        case "--keep-rate": o.keepRate = true
        case "--out": o.outDir = next(a)
        case "--driver": o.driverName = next(a)
        case "--process": o.processName = next(a)
        case "--pid": o.pid = pid_t(next(a)) ?? { usage() }()
        case "--clock": o.clock = next(a)
        case "--list": o.list = true
        case "-h", "--help": usage()
        default: print("unknown argument: \(a)"); usage()
        }
    }
    guard ["pre", "post", "both", "system"].contains(o.mode) else { usage() }
    if o.track != nil && !o.durationGiven { o.duration = 0 }
    if o.once && o.track == nil { print("--once needs --track"); usage() }
    return o
}

// MARK: - Core Audio helpers

struct CAError: Error, CustomStringConvertible {
    let what: String; let status: OSStatus
    var description: String {
        let be = UInt32(bitPattern: status).bigEndian
        let chars = withUnsafeBytes(of: be) { Array($0) }
        let fourCC = chars.allSatisfy { $0 >= 32 && $0 < 127 } ? " '\(String(bytes: chars, encoding: .ascii)!)'" : ""
        return "\(what) failed: \(status)\(fourCC)"
    }
}

func check(_ status: OSStatus, _ what: String) throws {
    if status != noErr { throw CAError(what: what, status: status) }
}

func addr(_ sel: AudioObjectPropertySelector,
          _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func getValue<T>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
                 _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, _ initial: T) throws -> T {
    var a = addr(sel, scope)
    var size = UInt32(MemoryLayout<T>.size)
    var value = initial
    try check(AudioObjectGetPropertyData(obj, &a, 0, nil, &size, &value), "get property \(sel)")
    return value
}

func getArray<T>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
                 _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
                 qualifier: [UInt32]? = nil) throws -> [T] {
    var a = addr(sel, scope)
    var size: UInt32 = 0
    var q = qualifier ?? []
    let qSize = UInt32(q.count * MemoryLayout<UInt32>.size)
    try check(q.withUnsafeMutableBytes { AudioObjectGetPropertyDataSize(obj, &a, qSize, $0.baseAddress, &size) },
              "get size \(sel)")
    let count = Int(size) / MemoryLayout<T>.stride
    if count == 0 { return [] }
    let buf = UnsafeMutablePointer<T>.allocate(capacity: count)
    defer { buf.deallocate() }
    try check(q.withUnsafeMutableBytes { AudioObjectGetPropertyData(obj, &a, qSize, $0.baseAddress, &size, buf) },
              "get \(sel)")
    return Array(UnsafeBufferPointer(start: buf, count: Int(size) / MemoryLayout<T>.stride))
}

func getString(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String? {
    var a = addr(sel)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var s: Unmanaged<CFString>?
    guard AudioObjectGetPropertyData(obj, &a, 0, nil, &size, &s) == noErr, let v = s else { return nil }
    return v.takeRetainedValue() as String
}

func allDevices() -> [AudioDeviceID] {
    (try? getArray(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDevices)) ?? []
}

func channelCount(_ dev: AudioDeviceID, _ scope: AudioObjectPropertyScope) -> Int {
    var a = addr(kAudioDevicePropertyStreamConfiguration, scope)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(dev, &a, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: 16)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(dev, &a, 0, nil, &size, raw) == noErr else { return 0 }
    let abl = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return abl.reduce(0) { $0 + Int($1.mNumberChannels) }
}

func nominalRate(_ dev: AudioObjectID) -> Double {
    (try? getValue(dev, kAudioDevicePropertyNominalSampleRate, kAudioObjectPropertyScopeGlobal, Float64(0))) ?? 0
}

func defaultOutputDevice() -> AudioDeviceID {
    (try? getValue(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice,
                   kAudioObjectPropertyScopeGlobal, AudioDeviceID(0))) ?? 0
}

func processObject(for pid: pid_t) -> AudioObjectID? {
    var a = addr(kAudioHardwarePropertyTranslatePIDToProcessObject)
    var p = pid
    var obj = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let st = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a,
                                        UInt32(MemoryLayout<pid_t>.size), &p, &size, &obj)
    return (st == noErr && obj != kAudioObjectUnknown) ? obj : nil
}

func pidsNamed(_ name: String) -> [pid_t] {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
    p.arguments = ["-x", name]
    let pipe = Pipe(); p.standardOutput = pipe
    try? p.run(); p.waitUntilExit()
    let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    return out.split(separator: "\n").compactMap { pid_t($0) }
}

func listEverything() {
    print("Audio devices:")
    let defOut = defaultOutputDevice()
    for d in allDevices() {
        let name = getString(d, kAudioObjectPropertyName) ?? "?"
        let mark = d == defOut ? "  <- default output" : ""
        print(String(format: "  [%3u] %@  in:%d out:%d  %.0f Hz%@", d, name,
                     channelCount(d, kAudioObjectPropertyScopeInput),
                     channelCount(d, kAudioObjectPropertyScopeOutput), nominalRate(d), mark))
    }
    print("\nProcesses known to Core Audio (running output marked *):")
    let procs: [AudioObjectID] = (try? getArray(AudioObjectID(kAudioObjectSystemObject),
                                                kAudioHardwarePropertyProcessObjectList)) ?? []
    for p in procs {
        let pid = (try? getValue(p, kAudioProcessPropertyPID, kAudioObjectPropertyScopeGlobal, pid_t(0))) ?? 0
        let running = (try? getValue(p, kAudioProcessPropertyIsRunningOutput, kAudioObjectPropertyScopeGlobal, UInt32(0))) ?? 0
        let bundle = getString(p, kAudioProcessPropertyBundleID) ?? ""
        let exe = ProcessInfo.processInfo.processIdentifier == pid ? "(self)" : (procName(pid) ?? "")
        print("  \(running != 0 ? "*" : " ") pid \(pid)  \(exe)  \(bundle)")
    }
}

func procName(_ pid: pid_t) -> String? {
    var buf = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
    guard proc_pidpath(pid, &buf, UInt32(buf.count)) > 0 else { return nil }
    return (String(cString: buf) as NSString).lastPathComponent
}
// MARK: - WAV writer (32-bit float, interleaved, split into per-track segments) + live stats

func hostNow() -> UInt64 { AudioGetCurrentHostTime() }

final class WavFile {
    let url: URL
    let channels: Int
    let sampleRate: Double
    private let handle: FileHandle
    private(set) var frames: UInt64 = 0

    /// Recorded into a hidden ".<name>.part" file and renamed to `url` by publish() once complete
    /// and tagged, so Finder/Spotlight never index (and cache a thumbnail for) a half-written file.
    let partURL: URL

    init(url: URL, channels: Int, sampleRate: Double) throws {
        self.url = url; self.channels = channels; self.sampleRate = sampleRate
        partURL = url.deletingLastPathComponent().appendingPathComponent("." + url.lastPathComponent + ".part")
        FileManager.default.createFile(atPath: partURL.path, contents: nil)
        handle = try FileHandle(forWritingTo: partURL)
        handle.write(WavFile.header(channels: channels, rate: sampleRate, frames: 0))
    }

    func write(_ samples: ArraySlice<Float>) {
        guard !samples.isEmpty else { return }
        samples.withUnsafeBytes { handle.write(Data($0)) }
        frames += UInt64(samples.count / channels)
    }

    func close() {
        handle.seek(toFileOffset: 0)
        handle.write(WavFile.header(channels: channels, rate: sampleRate, frames: frames))
        try? handle.close()
    }

    /// Give the finished file its real name.
    func publish() {
        try? FileManager.default.removeItem(at: url)
        try? FileManager.default.moveItem(at: partURL, to: url)
    }

    func discard() { try? FileManager.default.removeItem(at: partURL) }

    static func header(channels: Int, rate: Double, frames: UInt64) -> Data {
        let dataBytes = UInt32(clamping: frames * UInt64(channels) * 4)
        var d = Data()
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
        d.append(contentsOf: Array("RIFF".utf8)); u32(36 + dataBytes)
        d.append(contentsOf: Array("WAVE".utf8))
        d.append(contentsOf: Array("fmt ".utf8)); u32(16)
        u16(3)                                   // IEEE float
        u16(UInt16(channels)); u32(UInt32(rate))
        u32(UInt32(rate) * UInt32(channels) * 4) // byte rate
        u16(UInt16(channels * 4)); u16(32)
        d.append(contentsOf: Array("data".utf8)); u32(dataBytes)
        return d
    }
}

final class Recorder {
    let label: String
    let channels: Int
    var sampleRate: Double          // main thread only; changed between segments for --mode pre

    private let lock = NSLock()
    private var pending: [Float] = []
    /// Host time of the first frame of each IO callback, and where that callback starts in `pending`.
    private var marks: [(host: UInt64, index: Int)] = []
    private var file: WavFile?          // nil = audio is discarded (e.g. not the test track)

    // stats since last report (guarded by lock)
    private var peak: Float = 0
    private var sumSq: Double = 0
    private var statSamples = 0
    private var clipped = 0
    private var totalClipped = 0
    private var gaps = 0
    private var lastSampleTime: Double = -1
    private var lastFrames: Double = 0
    private var silentCallbacks = 0
    private var callbacks = 0

    init(label: String, channels: Int, sampleRate: Double) {
        self.label = label; self.channels = channels; self.sampleRate = sampleRate
        pending.reserveCapacity(Int(sampleRate) * channels)
        marks.reserveCapacity(256)
    }

    /// Called on the Core Audio IO thread. Interleaves whatever buffer layout arrives.
    func ingest(_ abl: UnsafePointer<AudioBufferList>, time: UnsafePointer<AudioTimeStamp>?, bufferIndices: Range<Int>? = nil) {
        let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: abl))
        let range = bufferIndices ?? 0..<list.count
        guard !range.isEmpty else { return }

        var srcs: [(UnsafePointer<Float>, Int)] = []   // (data, channelsInBuffer)
        var frames = Int.max
        for i in range {
            let b = list[i]
            guard let d = b.mData, b.mNumberChannels > 0 else { continue }
            let ch = Int(b.mNumberChannels)
            frames = min(frames, Int(b.mDataByteSize) / (MemoryLayout<Float>.size * ch))
            srcs.append((UnsafePointer(d.assumingMemoryBound(to: Float.self)), ch))
        }
        guard frames != Int.max, frames > 0 else { return }

        lock.lock(); defer { lock.unlock() }
        callbacks += 1

        let t = time?.pointee
        if let t, t.mFlags.contains(.sampleTimeValid) {
            if lastSampleTime >= 0 {
                let expected = lastSampleTime + lastFrames
                if abs(t.mSampleTime - expected) > 1 { gaps += 1 }
            }
            lastSampleTime = t.mSampleTime; lastFrames = Double(frames)
        }
        let host = (t.map { $0.mFlags.contains(.hostTimeValid) } ?? false) ? t!.mHostTime : hostNow()
        marks.append((host, pending.count))

        var anyNonZero = false
        for f in 0..<frames {
            var outCh = 0
            for (data, ch) in srcs {
                for c in 0..<ch where outCh < channels {
                    let s = data[f * ch + c]
                    pending.append(s)
                    let m = abs(s)
                    if m > peak { peak = m }
                    if m >= 1.0 { clipped += 1 }
                    if s != 0 { anyNonZero = true }
                    sumSq += Double(s) * Double(s)
                    outCh += 1
                }
            }
            while outCh < channels { pending.append(0); outCh += 1 }   // pad if layout is short
        }
        statSamples += frames * channels
        if !anyNonZero { silentCallbacks += 1 }
    }

    private func take() -> ([Float], [(host: UInt64, index: Int)]) {
        lock.lock()
        let chunk = pending, m = marks
        pending.removeAll(keepingCapacity: true)
        marks.removeAll(keepingCapacity: true)
        lock.unlock()
        return (chunk, m)
    }

    /// Main thread: move captured audio to the current segment file (or drop it).
    func flush() {
        let (chunk, _) = take()
        file?.write(chunk[...])
    }

    /// Main thread: end the current segment at host time `at` (sample-accurate within the captured
    /// buffers) and continue into `next` (nil = discard). Returns the closed file, if any.
    @discardableResult
    func split(at: UInt64, next: WavFile?) -> WavFile? {
        let (chunk, m) = take()
        var cut = chunk.count
        if let mark = m.last(where: { $0.host <= at }) {
            let secs = Double(AudioConvertHostTimeToNanos(at) - AudioConvertHostTimeToNanos(mark.host)) / 1e9
            cut = min(chunk.count, mark.index + Int(secs * sampleRate) * channels)
        } else if !m.isEmpty {
            cut = 0                                   // all captured audio is newer than the split
        }
        cut -= cut % channels
        file?.write(chunk[..<cut])
        let closed = file
        closed?.close()
        file = next
        file?.write(chunk[cut...])
        return closed
    }

    func report() -> String {
        lock.lock()
        let pk = peak, rms = statSamples > 0 ? sqrt(sumSq / Double(statSamples)) : 0
        let clip = clipped, g = gaps, sil = silentCallbacks, cb = callbacks
        totalClipped += clipped
        peak = 0; sumSq = 0; statSamples = 0; clipped = 0; silentCallbacks = 0; callbacks = 0
        lock.unlock()
        func db(_ v: Double) -> String { v > 0 ? String(format: "%6.1f", 20 * log10(v)) : "  -inf" }
        var s = "\(label.padding(toLength: 6, withPad: " ", startingAt: 0)) peak \(db(Double(pk))) dBFS  rms \(db(rms)) dBFS"
        if clip > 0 { s += "  CLIP x\(clip)" }
        if g > 0 { s += "  GAPS x\(g)" }
        if cb == 0 { s += "  (no callbacks!)" } else if sil == cb { s += "  (silent)" }
        if file == nil { s += "  [discarding]" }
        return s
    }

    var clippedTotal: Int { lock.lock(); defer { lock.unlock() }; return totalClipped + clipped }
}

// MARK: - Now-playing metadata (Spotify / Apple Music)
//
// The system-wide MediaRemote "Now Playing" API is entitlement-locked since macOS 15.4 (returns
// nothing to unsigned tools), so we listen to the players' own distributed notifications, which
// fire on every track change / play / pause, and ask them via AppleScript once at startup.

struct TrackInfo: Equatable {
    var player: String
    var title: String
    var artist: String
    var album: String
    var trackID: String
    var durationSec: Double?
    var positionSec: Double?
    var sampleRate: Double?      // source rate of the track, when the player exposes it
    var state: String            // "playing", "paused", "stopped"

    var isPlaying: Bool { state == "playing" }
    var display: String { artist.isEmpty ? title : "\(artist) - \(title)" }
    /// Identity of the playing item (position excluded, so seeks don't split).
    var key: String { "\(player)|\(trackID)|\(title)|\(artist)" }

    func matches(_ filter: String) -> Bool {
        let f = filter.lowercased()
        return trackID.lowercased() == f || display.lowercased().contains(f) || title.lowercased().contains(f)
    }

    var json: [String: Any] {
        var d: [String: Any] = ["player": player, "title": title, "artist": artist, "album": album,
                                "track_id": trackID, "state": state]
        if let durationSec { d["track_duration_s"] = durationSec }
        if let positionSec { d["player_position_s"] = positionSec }
        if let sampleRate { d["track_sample_rate"] = sampleRate }
        return d
    }
}

final class NowPlaying {
    private var latest: [String: (info: TrackInfo, at: Date)] = [:]
    var onChange: ((TrackInfo?) -> Void)?

    /// The playing track from the most recently active player, if any.
    var current: TrackInfo? {
        latest.values.filter { $0.info.isPlaying }.max { $0.at < $1.at }?.info
    }

    func start() {
        let dnc = DistributedNotificationCenter.default()
        dnc.addObserver(forName: .init("com.spotify.client.PlaybackStateChanged"), object: nil, queue: .main) { [weak self] n in
            var info = Self.parse(player: "Spotify", n.userInfo ?? [:], durationKey: "Duration", idKey: "Track ID")
            info.sampleRate = 44100
            self?.update(info)
        }
        dnc.addObserver(forName: .init("com.apple.Music.playerInfo"), object: nil, queue: .main) { [weak self] n in
            var info = Self.parse(player: "Music", n.userInfo ?? [:], durationKey: "Total Time", idKey: "PersistentID")
            if info.isPlaying, let q = Self.query("Music"), q.title == info.title { info.sampleRate = q.sampleRate }
            self?.update(info)
        }
        for player in ["Spotify", "Music"] {
            if let info = Self.query(player) { update(info) }
        }
    }

    private func update(_ info: TrackInfo) {
        let before = current
        latest[info.player] = (info, Date())
        let after = current
        if before?.key != after?.key || before?.isPlaying != after?.isPlaying { onChange?(after) }
    }

    private static func parse(player: String, _ u: [AnyHashable: Any], durationKey: String, idKey: String) -> TrackInfo {
        func str(_ k: String) -> String { (u[k] as? String) ?? (u[k].map { "\($0)" } ?? "") }
        let ms = (u[durationKey] as? NSNumber)?.doubleValue
        return TrackInfo(player: player, title: str("Name"), artist: str("Artist"), album: str("Album"),
                         trackID: str(idKey).isEmpty ? str("Persistent ID") : str(idKey),
                         durationSec: ms.map { $0 / 1000 },
                         positionSec: (u["Playback Position"] as? NSNumber)?.doubleValue,
                         state: str("Player State").lowercased())
    }

    /// One-shot AppleScript query (only if the player is already running — never launches it).
    static func query(_ player: String) -> TrackInfo? {
        let idExpr = player == "Spotify" ? "id of current track" : "persistent ID of current track"
        let rateExpr = player == "Spotify" ? "\"44100\"" : "(sample rate of current track) as string"
        let durExpr = player == "Spotify" ? "(duration of current track) / 1000" : "duration of current track"
        let script = """
        if application "\(player)" is running then
          tell application "\(player)"
            try
              set d to ASCII character 31
              return (name of current track) & d & (artist of current track) & d & (album of current track) & d & ¬
                (\(idExpr)) & d & ((\(durExpr)) as string) & d & ((player position) as string) & d & ((player state) as string) & d & ¬
                (\(rateExpr))
            end try
          end tell
        end if
        return ""
        """
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", script]
        let out = Pipe(); p.standardOutput = out; p.standardError = Pipe()
        guard (try? p.run()) != nil else { return nil }
        p.waitUntilExit()
        let s = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let f = s.components(separatedBy: "\u{1F}")
        guard f.count == 8 else {
            if p.terminationStatus != 0 {
                print("note: couldn't query \(player) via AppleScript (Automation permission?) — waiting for its next track change")
            }
            return nil
        }
        func num(_ v: String) -> Double? { Double(v.replacingOccurrences(of: ",", with: ".")) }
        return TrackInfo(player: player, title: f[0], artist: f[1], album: f[2], trackID: f[3],
                         durationSec: num(f[4]), positionSec: num(f[5]), sampleRate: num(f[7]).flatMap { $0 > 0 ? $0 : nil },
                         state: f[6].lowercased())
    }
}
// MARK: - Sources

protocol Source: AnyObject { var recorder: Recorder { get }; var deviceDescription: String { get }; func stop() }

/// Reads the input side of a loopback device (BlackHole-based "AudioFlow 2ch").
final class DeviceInputSource: Source {
    let recorder: Recorder
    let device: AudioDeviceID
    var procID: AudioDeviceIOProcID?
    var deviceDescription: String { "\"\(getString(device, kAudioObjectPropertyName) ?? "?")\" loopback input" }

    init(device dev: AudioDeviceID, label: String = "pre") throws {
        let deviceName = getString(dev, kAudioObjectPropertyName) ?? "?"
        let ch = channelCount(dev, kAudioObjectPropertyScopeInput)
        guard ch > 0 else {
            throw NSError(domain: "capture", code: 1, userInfo: [NSLocalizedDescriptionKey:
                "device \"\(deviceName)\" has no input channels"])
        }
        device = dev
        recorder = Recorder(label: label, channels: ch, sampleRate: nominalRate(dev))
        let rec = recorder
        try check(AudioDeviceCreateIOProcIDWithBlock(&procID, dev, nil) { _, inInput, inInputTime, _, _ in
            rec.ingest(inInput, time: inInputTime)
        }, "AudioDeviceCreateIOProcIDWithBlock(pre)")
        try check(AudioDeviceStart(dev, procID), "AudioDeviceStart(pre)")
        print("\(label.padding(toLength: 6, withPad: " ", startingAt: 0)): \"\(deviceName)\" loopback input, \(ch) ch @ \(Int(nominalRate(dev))) Hz")
    }

    func stop() {
        guard let p = procID else { return }
        AudioDeviceStop(device, p)
        AudioDeviceDestroyIOProcID(device, p)
    }
}

/// Core Audio process tap (macOS 14.2+) wrapped in a private aggregate device.
final class TapSource: Source {
    let recorder: Recorder
    var tapID = AudioObjectID(kAudioObjectUnknown)
    var aggID = AudioObjectID(kAudioObjectUnknown)
    var procID: AudioDeviceIOProcID?

    let clockDevice: AudioDeviceID
    var deviceDescription: String { "process tap clocked by \"\(getString(clockDevice, kAudioObjectPropertyName) ?? "?")\"" }

    /// processes == nil -> global tap of everything except this process and `exclude`.
    init(label: String, processes: [AudioObjectID]?, clockDevice: AudioDeviceID, exclude: [AudioObjectID] = []) throws {
        self.clockDevice = clockDevice
        let desc: CATapDescription
        if let processes {
            desc = CATapDescription(stereoMixdownOfProcesses: processes)
        } else {
            let me = processObject(for: ProcessInfo.processInfo.processIdentifier).map { [$0] } ?? []
            desc = CATapDescription(stereoGlobalTapButExcludeProcesses: me + exclude)
        }
        desc.name = "capture-\(label)"
        desc.uuid = UUID()
        desc.isPrivate = true
        desc.muteBehavior = .unmuted

        try check(AudioHardwareCreateProcessTap(desc, &tapID),
                  "AudioHardwareCreateProcessTap (grant System Audio Recording permission to your terminal)")

        let fmt = try getValue(tapID, kAudioTapPropertyFormat, kAudioObjectPropertyScopeGlobal, AudioStreamBasicDescription())

        guard let clockUID = getString(clockDevice, kAudioDevicePropertyDeviceUID) else {
            throw CAError(what: "get clock device UID", status: -1)
        }
        let aggDesc: [String: Any] = [
            kAudioAggregateDeviceNameKey: "capture-\(label)-agg",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: clockUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: clockUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                               kAudioSubTapUIDKey: desc.uuid.uuidString]],
        ]
        try check(AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &aggID), "AudioHardwareCreateAggregateDevice")

        let tapCh = Int(fmt.mChannelsPerFrame)
        // The tap reports its own format (often 48 kHz), but the aggregate delivers it at the
        // aggregate's rate, which follows the clock device. Label the WAV with the delivered rate.
        let deliveredRate = [nominalRate(aggID), nominalRate(clockDevice), fmt.mSampleRate].first { $0 > 0 } ?? 48000
        recorder = Recorder(label: label, channels: tapCh, sampleRate: deliveredRate)

        // The aggregate's input streams are: [clock device inputs..., tap stream(s)].
        // Skip the clock device's own input buffers so only the tap is recorded.
        let skipBuffers: Int = {
            var a = addr(kAudioDevicePropertyStreams, kAudioObjectPropertyScopeInput)
            var size: UInt32 = 0
            AudioObjectGetPropertyDataSize(clockDevice, &a, 0, nil, &size)
            return Int(size) / MemoryLayout<AudioStreamID>.size
        }()

        let rec = recorder
        try check(AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, nil) { _, inInput, inInputTime, _, _ in
            let n = Int(inInput.pointee.mNumberBuffers)
            let start = min(skipBuffers, max(n - 1, 0))
            rec.ingest(inInput, time: inInputTime, bufferIndices: start..<n)
        }, "AudioDeviceCreateIOProcIDWithBlock(\(label))")
        try check(AudioDeviceStart(aggID, procID), "AudioDeviceStart(\(label))")

        let clockName = getString(clockDevice, kAudioObjectPropertyName) ?? "?"
        print("\(label.padding(toLength: 6, withPad: " ", startingAt: 0)): process tap, \(tapCh) ch @ \(Int(deliveredRate)) Hz "
              + "(clock: \(clockName))")
    }

    func stop() {
        if let p = procID { AudioDeviceStop(aggID, p); AudioDeviceDestroyIOProcID(aggID, p) }
        if aggID != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(aggID) }
        if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
    }
}
// MARK: - Album art + tags embedded in the segment WAVs
//
// Each finished WAV gets two extra RIFF chunks after the audio:
//   LIST/INFO : title / artist / album / comment — the classic WAV tags most tools read
//   "id3 "    : an ID3v2.3 tag with the same text, TXXX diagnostics fields and the cover (APIC) —
//               read by Music, VLC, foobar2000, Mp3tag, rekordbox, Serato, exiftool, ffprobe…

func runOsascript(_ script: String) -> String? {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    p.arguments = ["-e", script]
    let out = Pipe(); p.standardOutput = out; p.standardError = Pipe()
    guard (try? p.run()) != nil else { return nil }
    p.waitUntilExit()
    guard p.terminationStatus == 0 else { return nil }
    return String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
        .trimmingCharacters(in: .whitespacesAndNewlines)
}

enum Artwork {
    /// track key -> image bytes (JPEG/PNG). Main thread only.
    static var cache: [String: Data] = [:]
    private static var inFlight: Set<String> = []

    /// Fetch the cover for `info` in the background while the segment records.
    static func fetch(for info: TrackInfo) {
        let key = info.key
        guard cache[key] == nil, !inFlight.contains(key) else { return }
        inFlight.insert(key)
        DispatchQueue.global(qos: .utility).async {
            let data = info.player == "Spotify" ? spotify(info) : music(info)
            DispatchQueue.main.async {
                inFlight.remove(key)
                if let data, mime(data) != nil { cache[key] = data }
                else { print("  (no album art available for \(info.display))") }
            }
        }
    }

    private static func spotify(_ info: TrackInfo) -> Data? {
        guard let out = runOsascript(#"tell application "Spotify" to return (id of current track) & tab & (artwork url of current track)"#)
        else { return nil }
        let parts = out.components(separatedBy: "\t")
        guard parts.count == 2, parts[0] == info.trackID,
              let url = URL(string: parts[1]), url.scheme == "https" else { return nil }
        var result: Data?
        let done = DispatchSemaphore(value: 0)
        var req = URLRequest(url: url); req.timeoutInterval = 10
        URLSession.shared.dataTask(with: req) { data, resp, _ in
            if (resp as? HTTPURLResponse)?.statusCode == 200 { result = data }
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 12)
        return result
    }

    private static func music(_ info: TrackInfo) -> Data? {
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("capture-art-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: tmp) }
        let title = info.title.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        let script = """
        tell application "Music"
          if name of current track is not "\(title)" then return "mismatch"
          if (count of artworks of current track) is 0 then return "none"
          set d to raw data of artwork 1 of current track
        end tell
        set f to open for access (POSIX file "\(tmp.path)") with write permission
        write d to f
        close access f
        return "ok"
        """
        guard runOsascript(script) == "ok" else { return nil }
        return try? Data(contentsOf: tmp)
    }

    /// Give an in-flight fetch for `key` up to `timeout` s to land (runs the main run loop).
    static func waitFor(_ key: String, timeout: Double = 5) {
        let deadline = Date().addingTimeInterval(timeout)
        while cache[key] == nil && inFlight.contains(key) && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
    }

    static func mime(_ d: Data) -> String? {
        if d.starts(with: [0xFF, 0xD8, 0xFF]) { return "image/jpeg" }
        if d.starts(with: [0x89, 0x50, 0x4E, 0x47]) { return "image/png" }
        return nil
    }
}

struct Tags {
    var title = "", artist = "", album = "", comment = ""
    var extra: [(String, String)] = []     // ID3 TXXX description/value pairs
    var cover: Data?
}

private func le32(_ v: UInt32) -> Data { withUnsafeBytes(of: v.littleEndian) { Data($0) } }
private func be32(_ v: UInt32) -> Data { withUnsafeBytes(of: v.bigEndian) { Data($0) } }

private func riffChunk(_ id: String, _ body: Data) -> Data {
    var d = Data(id.utf8) + le32(UInt32(body.count)) + body
    if body.count % 2 == 1 { d.append(0) }
    return d
}

private func listInfo(_ t: Tags) -> Data {
    var body = Data("INFO".utf8)
    for (id, v) in [("INAM", t.title), ("IART", t.artist), ("IPRD", t.album), ("ICMT", t.comment), ("ISFT", "capture")]
    where !v.isEmpty {
        body += riffChunk(id, Data(v.utf8) + Data([0]))
    }
    return riffChunk("LIST", body)
}

private func id3v23(_ t: Tags) -> Data {
    func utf16(_ s: String) -> Data { Data([0xFF, 0xFE]) + (s.data(using: .utf16LittleEndian) ?? Data()) }
    func frame(_ id: String, _ body: Data) -> Data { Data(id.utf8) + be32(UInt32(body.count)) + Data([0, 0]) + body }
    func text(_ id: String, _ v: String) -> Data { frame(id, Data([1]) + utf16(v) + Data([0, 0])) }

    var frames = Data()
    if !t.title.isEmpty { frames += text("TIT2", t.title) }
    if !t.artist.isEmpty { frames += text("TPE1", t.artist) }
    if !t.album.isEmpty { frames += text("TALB", t.album) }
    if !t.comment.isEmpty {   // COMM: encoding, language, empty description, text
        frames += frame("COMM", Data([1]) + Data("eng".utf8) + utf16("") + Data([0, 0]) + utf16(t.comment))
    }
    for (desc, value) in t.extra {
        frames += frame("TXXX", Data([1]) + utf16(desc) + Data([0, 0]) + utf16(value))
    }
    if let cover = t.cover, let mime = Artwork.mime(cover) {
        // APIC: ISO-8859-1 encoding, MIME, picture type 3 (front cover), empty description, image
        frames += frame("APIC", Data([0]) + Data(mime.utf8) + Data([0, 3, 0]) + cover)
    }
    let n = UInt32(frames.count)   // syncsafe size
    let size = Data([UInt8((n >> 21) & 0x7F), UInt8((n >> 14) & 0x7F), UInt8((n >> 7) & 0x7F), UInt8(n & 0x7F)])
    return Data("ID3".utf8) + Data([3, 0, 0]) + size + frames
}

/// Append LIST/INFO and id3 chunks to a finished WAV and fix up the RIFF size.
func embedTags(_ url: URL, _ tags: Tags) {
    guard let h = try? FileHandle(forUpdating: url) else { return }
    defer { try? h.close() }
    h.seekToEndOfFile()
    h.write(listInfo(tags))
    h.write(riffChunk("id3 ", id3v23(tags)))
    let total = h.offsetInFile
    h.seek(toFileOffset: 4)
    h.write(le32(UInt32(clamping: total - 8)))
}

// MARK: - Device sample rate (for --mode pre)

func availableRates(_ dev: AudioDeviceID) -> [AudioValueRange] {
    (try? getArray(dev, kAudioDevicePropertyAvailableNominalSampleRates)) ?? []
}

/// Sets the device's nominal rate and waits (≤1 s) for it to take effect.
func setNominalRate(_ dev: AudioDeviceID, _ rate: Double) -> Bool {
    guard availableRates(dev).contains(where: { rate >= $0.mMinimum && rate <= $0.mMaximum }) else { return false }
    var a = addr(kAudioDevicePropertyNominalSampleRate)
    var r = Float64(rate)
    guard AudioObjectSetPropertyData(dev, &a, 0, nil, UInt32(MemoryLayout<Float64>.size), &r) == noErr else { return false }
    let deadline = Date().addingTimeInterval(1)
    while nominalRate(dev) != rate && Date() < deadline { usleep(10_000) }
    usleep(50_000)   // let the IOProc settle on the new format
    return nominalRate(dev) == rate
}

// MARK: - Main

let opts = parseOptions()
if opts.list { listEverything(); exit(0) }

let fmt = DateFormatter(); fmt.dateFormat = "yyyyMMdd-HHmmss"
let iso = ISO8601DateFormatter(); iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
let sessionDir = URL(fileURLWithPath: opts.outDir, isDirectory: true).appendingPathComponent(fmt.string(from: Date()))
try? FileManager.default.createDirectory(at: sessionDir, withIntermediateDirectories: true)

var sources: [Source] = []
var preSource: Source? { sources.first { $0.recorder.label == "pre" } }
var originalRates: [AudioDeviceID: Double] = [:]     // loopback devices whose rate we changed

func restoreRates() {
    for (dev, r) in originalRates where nominalRate(dev) != r { _ = setNominalRate(dev, r) }
}
func bail(_ msg: String) -> Never {
    sources.forEach { $0.stop() }
    restoreRates()
    FileHandle.standardError.write(("error: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

func device(named name: String) -> AudioDeviceID? {
    allDevices().first { getString($0, kAudioObjectPropertyName) == name }
}
func isLoopback(_ dev: AudioDeviceID) -> Bool {
    let transport = (try? getValue(dev, kAudioDevicePropertyTransportType, kAudioObjectPropertyScopeGlobal, UInt32(0))) ?? 0
    return transport == kAudioDeviceTransportTypeVirtual && channelCount(dev, kAudioObjectPropertyScopeInput) > 0
}
func deviceName(_ dev: AudioDeviceID) -> String { getString(dev, kAudioObjectPropertyName) ?? "device \(dev)" }

let clockOverride: AudioDeviceID? = opts.clock.map { name in
    guard let d = device(named: name) else { bail("clock device \"\(name)\" not found") }
    return d
}

/// AudioFlow backend process objects (empty if it isn't running).
func audioFlowProcesses() -> [AudioObjectID] {
    (opts.pid.map { [$0] } ?? pidsNamed(opts.processName)).compactMap(processObject(for:))
}

/// pre = what apps send to the selected output device, before AudioFlow.
///  - selected device is a loopback (AudioFlow 2ch, BlackHole…): read its input side directly
///  - otherwise: global process tap of every app except AudioFlow, clocked by the selected device
func makePre(selected: AudioDeviceID) throws -> Source {
    if let pin = opts.driverName {
        guard let d = device(named: pin) else { throw CAError(what: "find device \"\(pin)\" (try --list)", status: -1) }
        return try DeviceInputSource(device: d)
    }
    if isLoopback(selected) { return try DeviceInputSource(device: selected) }
    return try TapSource(label: "pre", processes: nil, clockDevice: clockOverride ?? selected, exclude: audioFlowProcesses())
}

/// post = what AudioFlow sends to the hardware (process tap on the backend).
func makePost() throws -> Source {
    let objs = audioFlowProcesses()
    guard !objs.isEmpty else {
        throw CAError(what: "find a running \"\(opts.processName)\" registered with Core Audio (start AudioFlow or pass --pid)", status: -1)
    }
    // Clock the aggregate from the device AudioFlow is actually writing to (not a loopback).
    let used: [AudioDeviceID] = objs.flatMap {
        (try? getArray($0, kAudioProcessPropertyDevices, kAudioObjectPropertyScopeOutput) as [AudioDeviceID]) ?? []
    }
    let clock = clockOverride ?? used.first { !isLoopback($0) && channelCount($0, kAudioObjectPropertyScopeOutput) > 0 }
        ?? defaultOutputDevice()
    return try TapSource(label: "post", processes: objs, clockDevice: clock)
}

/// system = everything heading to the selected output device.
func makeSystem(selected: AudioDeviceID) throws -> Source {
    try TapSource(label: "system", processes: nil, clockDevice: clockOverride ?? selected)
}

/// Sources that depend on the selected output device (rebuilt when it changes).
func buildFollowing(selected: AudioDeviceID) throws -> [Source] {
    var out: [Source] = []
    if opts.mode == "pre" || opts.mode == "both" { out.append(try makePre(selected: selected)) }
    if opts.mode == "system" { out.append(try makeSystem(selected: selected)) }
    return out
}

var selectedDevice = defaultOutputDevice()
print("selected output device: \(deviceName(selectedDevice))")
do {
    sources = try buildFollowing(selected: selectedDevice)
    if opts.mode == "post" || opts.mode == "both" { sources.append(try makePost()) }
} catch {
    bail("\(error)")
}

// MARK: Segmentation

struct Segment {
    let index: Int
    let name: String
    let info: TrackInfo?
    let started: Date
    let startHost: UInt64
    var files: [String: (file: WavFile, offset: Double)] = [:]
}

var current: Segment?
var nextIndex = 1
var manifest: [[String: Any]] = []
var matchedSegmentsClosed = 0

func slug(_ s: String) -> String {
    let bad = CharacterSet(charactersIn: "/\\:*?\"<>|\n\r\t")
    let cleaned = s.unicodeScalars.map { bad.contains($0) ? "_" : String($0) }.joined()
    return String(cleaned.trimmingCharacters(in: .whitespaces).prefix(80))
}

func secs(_ from: UInt64, _ to: UInt64) -> Double {
    (Double(AudioConvertHostTimeToNanos(to)) - Double(AudioConvertHostTimeToNanos(from))) / 1e9
}

/// Finish `seg` (its files were already closed by Recorder.split) and write its JSON sidecar.
func finalize(_ seg: Segment, endHost: UInt64) {
    let frames = seg.files.values.reduce(UInt64(0)) { $0 + $1.file.frames }
    if frames == 0 {   // nothing captured (e.g. instant skip) — don't litter the folder
        seg.files.values.forEach { $0.file.discard() }
        return
    }
    // Tag every WAV of the segment (title/artist/album, diagnostics fields, album art).
    var cover: Data?
    if let info = seg.info { Artwork.waitFor(info.key); cover = Artwork.cache[info.key] }
    for (label, f) in seg.files where f.file.frames > 0 {
        var t = Tags()
        if let info = seg.info {
            t.title = info.title; t.artist = info.artist; t.album = info.album
            t.extra.append(("SOURCE_PLAYER", info.player))
            if !info.trackID.isEmpty { t.extra.append(("SOURCE_TRACK_ID", info.trackID)) }
            if let r = info.sampleRate { t.extra.append(("SOURCE_SAMPLE_RATE", String(Int(r)))) }
            if let pos = info.positionSec { t.extra.append(("SOURCE_START_POSITION_S", String(format: "%.3f", pos))) }
        } else {
            t.title = "untracked"
        }
        t.comment = "capture segment \(seg.index) | \(label) | \(Int(f.file.sampleRate)) Hz | started \(iso.string(from: seg.started))"
        t.extra += [("CAPTURE_STREAM", label), ("CAPTURE_SEGMENT", String(seg.index)),
                    ("CAPTURE_START_OFFSET_S", String(format: "%.4f", f.offset))]
        t.cover = cover
        embedTags(f.file.partURL, t)
    }
    seg.files.values.forEach { $0.file.publish() }

    var files: [String: Any] = [:]
    for (label, f) in seg.files {
        files[label] = ["file": f.file.url.lastPathComponent, "frames": f.file.frames,
                        "seconds": Double(f.file.frames) / f.file.sampleRate, "sample_rate": f.file.sampleRate,
                        "channels": f.file.channels, "start_offset_s": f.offset]
    }
    var entry: [String: Any] = ["index": seg.index, "name": seg.name,
                                "started_at": iso.string(from: seg.started),
                                "ended_at": iso.string(from: seg.started.addingTimeInterval(secs(seg.startHost, endHost))),
                                "wall_seconds": secs(seg.startHost, endHost), "files": files]
    if let info = seg.info { entry["track"] = info.json }
    entry["album_art_embedded"] = cover != nil
    manifest.append(entry)
    let url = sessionDir.appendingPathComponent(String(format: "%03d-%@.json", seg.index, slug(seg.name)))
    if let data = try? JSONSerialization.data(withJSONObject: entry, options: [.prettyPrinted, .sortedKeys]) {
        try? data.write(to: url)
    }
    let durs = seg.files.sorted { $0.key < $1.key }
        .map { String(format: "%@ %.1fs@%.0f", $0.key, Double($0.value.file.frames) / $0.value.file.sampleRate, $0.value.file.sampleRate) }
    print("  ✔ segment \(seg.index) \"\(seg.name)\": \(durs.joined(separator: ", "))")
    // Sanity check: recorded duration should match wall-clock time. A mismatch means the WAV's
    // sample rate label is wrong (audio would play too fast/slow) or audio was dropped.
    let wall = secs(seg.startHost, endHost)
    for (label, f) in seg.files where wall > 2 && f.file.frames > 0 {
        let recorded = Double(f.file.frames) / f.file.sampleRate + f.offset
        if abs(recorded / wall - 1) > 0.02 {
            print(String(format: "  ! %@: %.2fs of audio over %.2fs wall time (implied rate %.0f Hz vs labelled %.0f Hz)",
                         label, recorded, wall, Double(f.file.frames) / max(wall - f.offset, 0.001), f.file.sampleRate))
        }
    }
}

var lastTransitionKey: String?

/// Close the current segment at host time `at` and start the next one for `info`.
/// No-op if the playing item hasn't changed, unless `force` (e.g. output device switched).
func transition(to info: TrackInfo?, at: UInt64, force: Bool = false) {
    let playing = info?.isPlaying == true ? info : nil
    let key = playing?.key ?? "<none>"
    if !force && key == lastTransitionKey { return }
    lastTransitionKey = key
    let wanted: Bool = {
        guard let filter = opts.track else { return true }        // no filter: keep everything
        return playing?.matches(filter) ?? false
    }()
    let name = playing?.display ?? "untracked"

    var next: Segment? = wanted ? Segment(index: nextIndex, name: name, info: playing, started: Date(), startHost: at) : nil
    if next != nil { nextIndex += 1 }
    if next != nil, let p = playing { Artwork.fetch(for: p) }
    func newFile(_ r: Recorder) -> WavFile? {
        guard let n = next else { return nil }
        let url = sessionDir.appendingPathComponent(String(format: "%03d-%@-%@.wav", n.index, slug(n.name), r.label))
        return try? WavFile(url: url, channels: r.channels, sampleRate: r.sampleRate)
    }

    // pre: follow the track's native sample rate so the loopback doesn't resample it.
    var preNeedsSwitch: Double? = nil
    if let p = preSource as? DeviceInputSource, !opts.keepRate, wanted, let rate = playing?.sampleRate,
       rate != p.recorder.sampleRate {
        preNeedsSwitch = rate
    }

    for s in sources {
        let r = s.recorder
        if let rate = preNeedsSwitch, s === preSource {
            r.split(at: at, next: nil)
            let dev = (s as! DeviceInputSource).device
            if originalRates[dev] == nil { originalRates[dev] = nominalRate(dev) }
            if setNominalRate(dev, rate) {
                r.flush()                                // drop audio captured during the switch
                r.sampleRate = rate
                if let out = (sources.first { $0.recorder.label == "post" } as? TapSource)?.clockDevice,
                   nominalRate(out) != rate {
                    print("  ! \(deviceName(dev)) now at \(Int(rate)) Hz but AudioFlow's output device runs at "
                          + "\(Int(nominalRate(out))) Hz — AudioFlow does not resample, expect wrong pitch/dropouts in post")
                }
            } else {
                print("  ! couldn't switch \(deviceName(dev)) to \(Int(rate)) Hz (unsupported?) — recording pre at \(Int(r.sampleRate)) Hz")
            }
            let now = hostNow()
            if let f = newFile(r) { r.split(at: now, next: f); next?.files[r.label] = (f, secs(at, now)) }
        } else {
            let f = newFile(r)
            r.split(at: at, next: f)
            if let f { next?.files[r.label] = (f, 0) }
        }
    }

    if let seg = current {
        finalize(seg, endHost: at)
        if opts.track != nil && seg.info != nil { matchedSegmentsClosed += 1 }
    }
    current = next

    let rateNote = playing?.sampleRate.map { " @ \(Int($0)) Hz" } ?? ""
    if let p = playing {
        print("▶ \(p.player): \(p.display)\(rateNote)" + (wanted ? " → segment \(next!.index)" : "  (not the test track, discarding)"))
    } else {
        print("⏸ nothing playing" + (wanted ? " → segment \(next!.index) (untracked)" : "  (discarding)"))
    }
}

/// The user picked a different output device: close the segment, re-attach the device-following
/// sources to the new device, and continue in a fresh segment (rate/channels may differ).
func outputDeviceChanged() {
    let dev = defaultOutputDevice()
    guard dev != selectedDevice, dev != 0 else { return }
    let at = hostNow()
    print("🔀 output device: \(deviceName(selectedDevice)) → \(deviceName(dev))")
    selectedDevice = dev
    let following = sources.filter { $0.recorder.label != "post" }
    guard !following.isEmpty else { return }
    following.forEach { $0.recorder.split(at: at, next: nil); $0.stop() }
    sources.removeAll { s in following.contains { $0 === s } }
    do {
        sources.insert(contentsOf: try buildFollowing(selected: dev), at: 0)
    } catch {
        print("  ! couldn't attach to \(deviceName(dev)): \(error) — waiting for the next device change")
    }
    transition(to: nowPlaying.current, at: at, force: true)
}

var defaultOutAddr = addr(kAudioHardwarePropertyDefaultOutputDevice)
AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &defaultOutAddr, .main) { _, _ in
    outputDeviceChanged()
}

let nowPlaying = NowPlaying()
nowPlaying.onChange = { info in transition(to: info, at: hostNow()) }
nowPlaying.start()
transition(to: nowPlaying.current, at: hostNow())

if let t = opts.track { print("waiting for test track matching \"\(t)\"" + (opts.once ? " (stops after it ends)" : "")) }
print(opts.duration > 0 ? "recording for \(opts.duration)s (Ctrl-C to stop early)…" : "recording until Ctrl-C…")
print("session folder: \(sessionDir.path)")

signal(SIGINT, SIG_IGN)
let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
var stopRequested = false
sigint.setEventHandler { stopRequested = true }
sigint.resume()

let start = Date()
var lastReport = start
while !stopRequested && (opts.duration <= 0 || Date().timeIntervalSince(start) < opts.duration) {
    if opts.once && matchedSegmentsClosed > 0 { break }
    RunLoop.main.run(until: Date().addingTimeInterval(0.1))
    sources.forEach { $0.recorder.flush() }
    if Date().timeIntervalSince(lastReport) >= 1 {
        lastReport = Date()
        let t = String(format: "%5.1fs", Date().timeIntervalSince(start))
        let seg = current.map { " #\($0.index)" } ?? ""
        print("[\(t)\(seg)] " + sources.map { $0.recorder.report() }.joined(separator: "  |  "))
    }
}

let endHost = hostNow()
sources.forEach { $0.stop() }
for s in sources { s.recorder.split(at: endHost, next: nil) }
if let seg = current { finalize(seg, endHost: endHost) }
restoreRates()

let session: [String: Any] = [
    "started_at": iso.string(from: start), "mode": opts.mode, "track_filter": opts.track ?? NSNull(),
    "output_device_at_end": deviceName(selectedDevice),
    "clipped_samples": Dictionary(uniqueKeysWithValues: sources.map { ($0.recorder.label, $0.recorder.clippedTotal) }),
    "segments": manifest,
]
if let data = try? JSONSerialization.data(withJSONObject: session, options: [.prettyPrinted, .sortedKeys]) {
    try? data.write(to: sessionDir.appendingPathComponent("session.json"))
}
print("\n\(manifest.count) segment(s) written to \(sessionDir.path)")
for s in sources where s.recorder.clippedTotal > 0 {
    print("  \(s.recorder.label): \(s.recorder.clippedTotal) clipped samples")
}
