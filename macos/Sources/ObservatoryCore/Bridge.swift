import Foundation
import Darwin

public enum BridgeError: Error, LocalizedError, Equatable {
    case configuration, timeout, oversized, invalidResponse, cancelled
    /// The program answered, but it is not an engine with this protocol: an older
    /// release, or a different program. The detail is its first stderr line.
    case incompatible(String)
    /// A non-zero exit without a typed JSON error; the detail is its first stderr line.
    case failed(String)
    /// A typed engine error code, e.g. `budget-reached`.
    case backend(String)
    /// No program at the chosen absolute path: on a first launch, nothing is installed
    /// yet. The detail is the path, so the message can name where it looked.
    case missing(String)
    public var errorDescription: String? { code }
    public var code: String {
        switch self {
        case .configuration: return "backend-configuration"
        case .timeout: return "backend-timeout"
        case .oversized: return "backend-response-too-large"
        case .invalidResponse: return "backend-invalid-response"
        case .cancelled: return "backend-cancelled"
        case .incompatible: return "backend-incompatible"
        case .failed: return "backend-failed"
        case .backend(let code): return code
        case .missing: return "backend-missing"
        }
    }
    public var detail: String? {
        switch self {
        case .incompatible(let d), .failed(let d), .missing(let d): return d.isEmpty ? nil : d
        default: return nil
        }
    }
}

public struct Backend: Sendable {
    public static let protocolName = "observatory-assistant/1"
    public static let actions: Set<String> = ["status", "ask", "get", "list", "job", "cancel", "delete", "dashboard", "serve", "build"]
    public let executable: String
    public let workspace: String
    public init(executable: String, workspace: String) {
        self.executable = executable; self.workspace = workspace
    }

    /// One bounded engine call. Cancelling the calling task stops the child's whole
    /// process group, so a timed-out or abandoned `ask` cannot go on to start a job.
    public func call(_ action: String, input: [String: String] = [:], timeout: TimeInterval = 20) async throws -> Data {
        let child = Child()
        return try await withTaskCancellationHandler {
            try await Task.detached { try self.run(action, input: input, timeout: timeout, child: child) }.value
        } onCancel: { child.cancel() }
    }

    private func run(_ action: String, input: [String: String], timeout: TimeInterval, child: Child) throws -> Data {
        guard executable.hasPrefix("/"), workspace.hasPrefix("/"), Self.actions.contains(action) else { throw BridgeError.configuration }
        guard FileManager.default.fileExists(atPath: executable) else { throw BridgeError.missing(executable) }
        guard FileManager.default.isExecutableFile(atPath: executable) else { throw BridgeError.configuration }
        let payload = try JSONSerialization.data(withJSONObject: input)
        var env = ProcessInfo.processInfo.environment
        env["OBSERVATORY_HOME"] = workspace
        let result = try child.spawn([executable, "full", "assistant", action], env: env, stdin: payload, timeout: timeout)
        if result.tooLarge { throw BridgeError.oversized }
        let firstLine = String(decoding: result.stderr, as: UTF8.self)
            .split(whereSeparator: \.isNewline).first.map { String($0.prefix(240)) } ?? ""
        guard let doc = try? JSONSerialization.jsonObject(with: result.stdout) as? [String: Any] else {
            // No JSON at all: an engine without this command ("unknown step:
            // assistant", "invalid choice") is an old release, not a broken one.
            if result.status != 0 && Self.looksIncompatible(firstLine) { throw BridgeError.incompatible(firstLine) }
            if result.status != 0 { throw BridgeError.failed(firstLine) }
            throw BridgeError.invalidResponse
        }
        if let code = doc["error"] as? String { throw BridgeError.backend(code) }
        guard result.status == 0 else { throw BridgeError.failed(firstLine) }
        if action == "status", doc["protocol"] as? String != Self.protocolName {
            throw BridgeError.incompatible("protocol \(doc["protocol"] as? String ?? "missing"), expected \(Self.protocolName)")
        }
        return result.stdout
    }

    static func looksIncompatible(_ line: String) -> Bool {
        ["unknown step", "invalid choice", "unrecognized arguments", "no such command"].contains { line.localizedCaseInsensitiveContains($0) }
    }
}

/// A child in its own process group, so a timeout or Stop reaches what it spawned
/// (the CLI runs the engine as its own child). The detached job runner starts its
/// own session and is never in this group.
final class Child: @unchecked Sendable {
    struct Result { let stdout: Data; let stderr: Data; let status: Int32; let tooLarge: Bool }
    private let lock = NSLock()
    private var pid: pid_t = 0
    private var cancelled = false
    static let outputLimit = 8 * 1024 * 1024

    func cancel() {
        lock.lock(); cancelled = true; let p = pid; lock.unlock()
        if p > 0 { kill(-p, SIGTERM) }
    }
    private var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }

    func spawn(_ argv: [String], env: [String: String], stdin input: Data, timeout: TimeInterval) throws -> Result {
        var inP: [Int32] = [0, 0], outP: [Int32] = [0, 0], errP: [Int32] = [0, 0]
        guard pipe(&inP) == 0, pipe(&outP) == 0, pipe(&errP) == 0 else { throw BridgeError.failed("pipe: \(errno)") }
        var actions: posix_spawn_file_actions_t? = nil
        posix_spawn_file_actions_init(&actions); defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawn_file_actions_adddup2(&actions, inP[0], 0)
        posix_spawn_file_actions_adddup2(&actions, outP[1], 1)
        posix_spawn_file_actions_adddup2(&actions, errP[1], 2)
        var attr: posix_spawnattr_t? = nil
        posix_spawnattr_init(&attr); defer { posix_spawnattr_destroy(&attr) }
        // Own process group; inherit only the three descriptors above. And a CLEAN
        // signal state: posix_spawn passes the calling thread's mask, a Swift worker
        // thread blocks the asynchronous signals, and the app ignores SIGPIPE — so
        // without this every server and job runner started from here was deaf to
        // SIGTERM (`--stop`, Stop and launchd's own stop did nothing).
        posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT
                                              | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF))
        posix_spawnattr_setpgroup(&attr, 0)
        var noneBlocked = sigset_t(); sigemptyset(&noneBlocked)
        posix_spawnattr_setsigmask(&attr, &noneBlocked)
        var defaults = sigset_t(); sigfillset(&defaults)
        sigdelset(&defaults, SIGKILL); sigdelset(&defaults, SIGSTOP)
        posix_spawnattr_setsigdefault(&attr, &defaults)
        let cArgs = argv.map { strdup($0) } + [nil]
        let cEnv = env.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { cArgs.forEach { free($0) }; cEnv.forEach { free($0) } }
        var child: pid_t = 0
        let rc = posix_spawn(&child, argv[0], &actions, &attr, cArgs, cEnv)
        close(inP[0]); close(outP[1]); close(errP[1])
        guard rc == 0 else {
            close(inP[1]); close(outP[0]); close(errP[0])
            throw BridgeError.failed(String(cString: strerror(rc)))
        }
        lock.lock(); pid = child; let early = cancelled; lock.unlock()
        if early { kill(-child, SIGTERM) }

        let out = Drain(fd: outP[0], limit: Self.outputLimit, keep: Self.outputLimit)
        let err = Drain(fd: errP[0], limit: Self.outputLimit, keep: 4096)
        out.start(); err.start()
        // A child that exits without reading stdin must not kill the app with
        // SIGPIPE: the write then fails with EPIPE, and the exit status speaks.
        _ = fcntl(inP[1], F_SETNOSIGPIPE, 1)
        input.withUnsafeBytes { raw in
            var off = 0
            while off < raw.count {
                let n = write(inP[1], raw.baseAddress! + off, raw.count - off)
                if n <= 0 { break }
                off += n
            }
        }
        close(inP[1])

        let until = Date().addingTimeInterval(timeout)
        var status: Int32 = 0
        var exited = false
        while !exited {
            let r = waitpid(child, &status, WNOHANG)
            if r == child { exited = true; break }
            if r < 0 && errno != EINTR { exited = true; break }
            if out.overflowed { stop(child) }
            if isCancelled || Date() >= until {
                stop(child)
                _ = waitpid(child, &status, 0)
                out.join(); err.join()
                throw isCancelled ? BridgeError.cancelled : BridgeError.timeout
            }
            usleep(20_000)
        }
        out.join(); err.join()
        // A cancel's SIGTERM can end the child before the loop looks at the flag:
        // that exit is the cancellation, not a failure of the engine.
        if isCancelled { throw BridgeError.cancelled }
        let code: Int32 = (status & 0x7f) == 0 ? (status >> 8) & 0xff : 128 + (status & 0x7f)
        return Result(stdout: out.data, stderr: err.data, status: code, tooLarge: out.overflowed)
    }

    private func stop(_ child: pid_t) {
        kill(-child, SIGTERM)
        usleep(100_000)
        kill(-child, SIGKILL)
    }
}

/// Reads one descriptor to EOF on its own thread, keeping at most `keep` bytes.
final class Drain: @unchecked Sendable {
    private let fd: Int32, limit: Int, keep: Int
    private let lock = NSLock(), done = DispatchSemaphore(value: 0)
    private var bytes = Data(), count = 0, over = false
    init(fd: Int32, limit: Int, keep: Int) { self.fd = fd; self.limit = limit; self.keep = keep }
    func start() {
        Thread.detachNewThread { [self] in
            var buf = [UInt8](repeating: 0, count: 65536)
            while true {
                let n = read(fd, &buf, buf.count)
                if n < 0 && errno == EINTR { continue }
                if n <= 0 { break }
                lock.lock()
                count += n
                if bytes.count < keep { bytes.append(contentsOf: buf[0..<min(n, keep - bytes.count)]) }
                if count > limit { over = true }
                lock.unlock()
            }
            close(fd); done.signal()
        }
    }
    func join() { done.wait() }
    var data: Data { lock.lock(); defer { lock.unlock() }; return bytes }
    var overflowed: Bool { lock.lock(); defer { lock.unlock() }; return over }
}
