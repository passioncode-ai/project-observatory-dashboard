import Foundation
import Darwin

/// The app's side of an update (lifecycle LC-16, "Install vs activation").
///
/// The engine's hourly maintenance pass downloads and verifies a new app into the
/// workspace and swaps `/Applications/Project Observatory.app` only while the app is not
/// running; with the app open it records `app.result = "waiting-for-quit"` and the
/// `pending` version in `<workspace>/store/maintenance.json`. The app reads that record and
/// activates the update at a safe point: the person's *Restart to update*, the person's
/// quit, or a long idle with no window on screen. Activation is one detached helper
/// (`RestartHelper`) that waits for the app to exit, runs `project-observatory full
/// maintain app` — the engine's app step alone — and opens the app again.
public enum AppVersion {
    /// `X.Y.Z`, all three numeric — the only shape the engine's releases carry
    /// (`configuration.version_tuple`). Anything else is not comparable.
    public static func parse(_ text: String) -> [Int]? {
        let parts = text.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 3 else { return nil }
        let numbers = parts.compactMap { p in p.allSatisfy(\.isASCII) && p.allSatisfy(\.isNumber) && !p.isEmpty ? Int(p) : nil }
        return numbers.count == 3 ? numbers : nil
    }

    /// True only when both versions are comparable and `candidate` is strictly newer:
    /// an older or equal version is never offered, and neither is one that cannot be read.
    public static func isNewer(_ candidate: String, than current: String) -> Bool {
        guard let a = parse(candidate), let b = parse(current) else { return false }
        return b.lexicographicallyPrecedes(a)
    }
}

/// A verified app the engine staged, waiting for this app to quit.
public struct StagedUpdate: Equatable, Sendable {
    public let version: String
    public init(version: String) { self.version = version }
}

public enum UpdateState {
    /// Where the maintenance pass records what it did, per workspace.
    public static func file(workspace: String) -> URL {
        URL(fileURLWithPath: workspace).appendingPathComponent("store/maintenance.json")
    }

    /// The staged update a maintenance record names, if it is newer than the running app.
    /// Anything else — no record, another result, a malformed document, the same or an
    /// older version, a running version that cannot be read — is no update.
    public static func pending(_ data: Data?, running: String) -> StagedUpdate? {
        guard let data, let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let app = doc["app"] as? [String: Any], app["result"] as? String == "waiting-for-quit",
              let version = app["pending"] as? String, AppVersion.isNewer(version, than: running) else { return nil }
        return StagedUpdate(version: version)
    }

    /// Reads the workspace's record. A symbolic link or a file over 1 MB is not read.
    public static func read(workspace: String, running: String) -> StagedUpdate? {
        guard workspace.hasPrefix("/") else { return nil }
        let url = file(workspace: workspace)
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
              attrs[.type] as? FileAttributeType == .typeRegular,
              (attrs[.size] as? NSNumber)?.intValue ?? .max <= 1_048_576 else { return nil }
        return pending(try? Data(contentsOf: url), running: running)
    }
}

/// When an unattended app may restart itself to activate a staged update.
public enum IdleRestart {
    /// No window on screen and no input for this long.
    public static let after: TimeInterval = 30 * 60
    /// How often the app looks; the decision itself is `shouldRestart`.
    public static let every: TimeInterval = 60

    public struct Observation: Equatable, Sendable {
        public var staged: Bool
        /// A titled window of this app is visible (a minimised one is not).
        public var windowOnScreen: Bool
        /// The app is frontmost: the person is with it, window or not.
        public var active: Bool
        /// A question or a dashboard build or start is running.
        public var workInFlight: Bool
        /// The last moment a window was on screen or the app was frontmost.
        public var lastSeen: Date
        /// The last key press, click or scroll this app received.
        public var lastInput: Date
        public init(staged: Bool, windowOnScreen: Bool, active: Bool, workInFlight: Bool, lastSeen: Date, lastInput: Date) {
            self.staged = staged; self.windowOnScreen = windowOnScreen; self.active = active
            self.workInFlight = workInFlight; self.lastSeen = lastSeen; self.lastInput = lastInput
        }
    }

    public static func shouldRestart(_ o: Observation, now: Date) -> Bool {
        o.staged && !o.windowOnScreen && !o.active && !o.workInFlight
            && now.timeIntervalSince(o.lastSeen) >= after && now.timeIntervalSince(o.lastInput) >= after
    }
}

/// The detached helper that activates a staged update after the app has exited.
public enum RestartHelper {
    public enum Mode: String, Sendable {
        /// The person's *Restart to update*: swap, then open the app in front.
        case restart
        /// The idle restart: swap, then open the app without a window and without focus.
        case background
        /// The person's quit: swap, open nothing.
        case quit
    }

    /// What the helper is given, every value checked before anything is spawned.
    public struct Plan: Equatable, Sendable {
        public var pid: Int32
        public var engine: String
        public var workspace: String
        public var bundle: String
        public var mode: Mode
        public var log: String
        public var opener: String
        public var version: String
        public init(pid: Int32, engine: String, workspace: String, bundle: String, mode: Mode, log: String,
                    opener: String = "/usr/bin/open", version: String) {
            self.pid = pid; self.engine = engine; self.workspace = workspace; self.bundle = bundle
            self.mode = mode; self.log = log; self.opener = opener; self.version = version
        }

        /// Why this plan cannot run, as a code; nil when it can. A mode that reopens the
        /// app needs the `.app` it runs from: `swift run` has none to open again.
        public var refusal: String? {
            if pid <= 0 { return "no-process" }
            if !engine.hasPrefix("/") || !workspace.hasPrefix("/") || !log.hasPrefix("/") || !opener.hasPrefix("/") { return "not-configured" }
            if AppVersion.parse(version) == nil { return "no-update" }
            if mode != .quit && !(bundle.hasPrefix("/") && bundle.hasSuffix(".app")) { return "not-a-bundle" }
            return nil
        }

        /// `/bin/sh -c SCRIPT NAME ARGS…`: every value travels as an argument, never
        /// inside the script's text.
        public var argv: [String] {
            ["/bin/sh", "-c", RestartHelper.script, "observatory-update", String(pid), engine, workspace, bundle,
             mode.rawValue, log, opener, version]
        }
    }

    /// The one documented command that activates the update: the engine's app step.
    public static let engineCommand = ["full", "maintain", "app"]

    /// Waits ≤ 60 s for the app to exit; runs the app step (≤ 15 min, retried while
    /// another maintenance pass holds the lock, ≤ 5 min); logs codes only; opens the app
    /// again as the mode says — the new one when it was swapped, the old one when not.
    public static let script = #"""
    set -u
    pid=$1 engine=$2 home=$3 bundle=$4 mode=$5 log=$6 opener=$7 version=$8
    note() {
      printf '{"at":"%s","code":"%s","event":"%s","reason":"%s","trigger":"%s","version":"%s"}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$2" "$1" "${3:-}" "$mode" "$version" >> "$log" 2>/dev/null
    }
    reopen() {
      case "$mode" in
        restart) "$opener" "$bundle" >/dev/null 2>&1 ;;
        background) "$opener" -g "$bundle" --args --background >/dev/null 2>&1 ;;
      esac
    }
    n=0
    while kill -0 "$pid" 2>/dev/null; do
      n=$((n + 1))
      if [ "$n" -ge 600 ]; then note update_install failed app-still-running; exit 1; fi
      sleep 0.1
    done
    note update_install started
    out=$(mktemp "${TMPDIR:-/tmp}/observatory-update.XXXXXX") || { note update_install failed no-temp-file; reopen; exit 1; }
    tries=0 rc=0 timedout=0
    while :; do
      OBSERVATORY_HOME="$home" "$engine" full maintain app >"$out" 2>/dev/null &
      child=$!
      t=0
      while kill -0 "$child" 2>/dev/null; do
        t=$((t + 1))
        if [ "$t" -ge 4500 ]; then timedout=1; kill "$child" 2>/dev/null; break; fi
        sleep 0.2
      done
      wait "$child"; rc=$?
      status=$(/usr/bin/plutil -extract status raw -o - "$out" 2>/dev/null || true)
      if [ "$timedout" = 0 ] && [ "$status" = busy ] && [ "$tries" -lt 20 ]; then
        tries=$((tries + 1)); sleep 15; continue
      fi
      break
    done
    result=$(/usr/bin/plutil -extract app.result raw -o - "$out" 2>/dev/null || true)
    rm -f "$out"
    case "$result" in *[!a-z-]*) result=other ;; esac
    if [ "$timedout" = 1 ]; then note update_install timeout engine-timeout
    elif [ "$rc" = 0 ] && { [ "$result" = updated ] || [ "$result" = current ]; }; then note update_install installed
    elif [ "$status" = busy ]; then note update_install failed busy
    else note update_install failed "${result:-engine-failed}"
    fi
    reopen
    """#

    /// Starts `argv` in a session of its own, detached from this app: it outlives the
    /// app, a quit's signals do not reach it, and it inherits no descriptor but
    /// /dev/null on 0, 1 and 2.
    public static func spawnDetached(_ argv: [String], env: [String: String]) throws -> pid_t {
        guard let path = argv.first, path.hasPrefix("/") else { throw BridgeError.configuration }
        var actions: posix_spawn_file_actions_t? = nil
        posix_spawn_file_actions_init(&actions); defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_addopen(&actions, 1, "/dev/null", O_WRONLY, 0)
        posix_spawn_file_actions_addopen(&actions, 2, "/dev/null", O_WRONLY, 0)
        var attr: posix_spawnattr_t? = nil
        posix_spawnattr_init(&attr); defer { posix_spawnattr_destroy(&attr) }
        // A clean signal state, as in `Child.spawn`: a Swift thread's blocked mask and
        // the app's ignored SIGPIPE must not reach the helper or the engine it starts.
        posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_SETSID | POSIX_SPAWN_CLOEXEC_DEFAULT
                                              | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF))
        var none = sigset_t(); sigemptyset(&none)
        posix_spawnattr_setsigmask(&attr, &none)
        var defaults = sigset_t(); sigfillset(&defaults)
        sigdelset(&defaults, SIGKILL); sigdelset(&defaults, SIGSTOP)
        posix_spawnattr_setsigdefault(&attr, &defaults)
        let cArgs = argv.map { strdup($0) } + [nil]
        let cEnv = env.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { cArgs.forEach { free($0) }; cEnv.forEach { free($0) } }
        var pid: pid_t = 0
        let rc = posix_spawn(&pid, path, &actions, &attr, cArgs, cEnv)
        guard rc == 0 else { throw BridgeError.failed(String(cString: strerror(rc))) }
        return pid
    }
}

/// The app's own log, `~/Library/Logs/Project Observatory/app.log` (LC-12, LC-16 "Log
/// events"): one JSON line per event, UTC time, codes only — a field whose value is not
/// code-shaped is left out, so no path, message or value can reach it.
public struct AppLog: Sendable {
    public static let events: [String: Set<String>] = [
        "update_restart": ["requested", "refused"],
        "update_install": ["started", "installed", "failed", "timeout"],
    ]
    /// Kept under 1 MB; the previous generation is `app.log.1`.
    public static let cap = 1_048_576
    public let file: URL
    public init(file: URL) { self.file = file }

    public static func standard(home: String = NSHomeDirectory()) -> AppLog {
        AppLog(file: URL(fileURLWithPath: home).appendingPathComponent("Library/Logs/Project Observatory/app.log"))
    }

    /// The line for one event, or nil for an event or code outside the shared list.
    public static func line(event: String, code: String, fields: [String: String] = [:], at: Date) -> String? {
        guard events[event]?.contains(code) == true else { return nil }
        var doc: [String: String] = ["at": stamp(at), "event": event, "code": code]
        for (k, v) in fields where isCode(k) && isCode(v) && doc[k] == nil { doc[k] = v }
        guard let data = try? JSONSerialization.data(withJSONObject: doc, options: [.sortedKeys]) else { return nil }
        return String(decoding: data, as: UTF8.self)
    }

    public static func isCode(_ s: String) -> Bool {
        !s.isEmpty && s.count <= 64 && s.range(of: #"^[A-Za-z0-9][A-Za-z0-9._-]*$"#, options: .regularExpression) != nil
    }

    static func stamp(_ at: Date) -> String {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime]; f.timeZone = TimeZone(identifier: "UTC")
        return f.string(from: at)
    }

    /// Appends one event. A log that cannot be written never stops the app.
    @discardableResult
    public func record(_ event: String, _ code: String, _ fields: [String: String] = [:], at: Date = Date()) -> Bool {
        guard let line = Self.line(event: event, code: code, fields: fields, at: at) else { return false }
        let fm = FileManager.default
        let dir = file.deletingLastPathComponent()
        do {
            try fm.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            if let size = (try? fm.attributesOfItem(atPath: file.path))?[.size] as? NSNumber, size.intValue > Self.cap {
                let old = file.appendingPathExtension("1")
                try? fm.removeItem(at: old)
                try fm.moveItem(at: file, to: old)
            }
            let fd = open(file.path, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW, 0o600)
            guard fd >= 0 else { return false }
            defer { close(fd) }
            let bytes = Array((line + "\n").utf8)
            return bytes.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) } == bytes.count
        } catch { return false }
    }
}
