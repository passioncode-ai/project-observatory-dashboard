import Foundation

public enum BridgeError: Error, LocalizedError, Equatable {
    case configuration, timeout, oversized, invalidResponse, backend(String)
    public var errorDescription: String? {
        switch self {
        case .configuration: return "backend-configuration"
        case .timeout: return "backend-timeout"
        case .oversized: return "backend-response-too-large"
        case .invalidResponse: return "backend-invalid-response"
        case .backend(let code): return code
        }
    }
}

public struct Backend: Sendable {
    public let executable: String
    public let workspace: String
    public init(executable: String, workspace: String) {
        self.executable = executable; self.workspace = workspace
    }
    public func call(_ action: String, input: [String: String] = [:], timeout: TimeInterval = 20) async throws -> Data {
        try await Task.detached {
            try self.run(action, input: input, timeout: timeout)
        }.value
    }
    private func run(_ action: String, input: [String: String], timeout: TimeInterval) throws -> Data {
        guard executable.hasPrefix("/"), workspace.hasPrefix("/"),
              FileManager.default.isExecutableFile(atPath: executable),
              ["status", "ask", "get", "list", "job", "cancel", "dashboard"].contains(action) else { throw BridgeError.configuration }
        let process = Process(), out = Pipe(), err = Pipe(), stdin = Pipe()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = ["full", "assistant", action]
        process.environment = ProcessInfo.processInfo.environment.merging(["OBSERVATORY_HOME": workspace]) { _, new in new }
        process.standardOutput = out; process.standardError = err; process.standardInput = stdin
        let capture = Capture(), group = DispatchGroup()
        try process.run()
        // Drain both pipes concurrently; never wait for process exit with full pipes.
        for (pipe, keep) in [(out, true), (err, false)] {
            group.enter()
            DispatchQueue.global().async {
                defer { group.leave() }
                while true {
                    let data = pipe.fileHandleForReading.availableData
                    if data.isEmpty { break }
                    if !capture.append(data, keep: keep) { process.terminate(); break }
                }
            }
        }
        try stdin.fileHandleForWriting.write(contentsOf: JSONSerialization.data(withJSONObject: input))
        try stdin.fileHandleForWriting.close()
        let until = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < until { Thread.sleep(forTimeInterval: 0.02) }
        if process.isRunning {
            process.terminate()
            // A broken backend may ignore TERM; kill only this bridge child, never a job runner.
            Thread.sleep(forTimeInterval: 0.1)
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            throw BridgeError.timeout
        }
        guard group.wait(timeout: .now() + 2) == .success else { throw BridgeError.timeout }
        if capture.tooLarge { throw BridgeError.oversized }
        let data = capture.data
        guard let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw BridgeError.invalidResponse }
        if let code = doc["error"] as? String { throw BridgeError.backend(code) }
        guard process.terminationStatus == 0 else { throw BridgeError.backend("backend-failed") }
        if action == "status", doc["protocol"] as? String != "observatory-assistant/1" { throw BridgeError.invalidResponse }
        return data
    }
}

private final class Capture: @unchecked Sendable {
    private let lock = NSLock()
    private var bytes = Data(), count = 0, large = false
    func append(_ data: Data, keep: Bool) -> Bool {
        lock.lock(); defer { lock.unlock() }
        count += data.count
        if count > 8 * 1024 * 1024 { large = true; return false }
        if keep { bytes.append(data) }
        return true
    }
    var data: Data { lock.lock(); defer { lock.unlock() }; return bytes }
    var tooLarge: Bool { lock.lock(); defer { lock.unlock() }; return large }
}
