import Foundation
import Darwin
import Security

/// One waiting secret request, as the engine's door answers it (`full assistant secret`, `{"op": "list"}`;
/// observatory/engine/secret_prompt.py, OBS-260, docs/design/PERSON-SURFACES.md D4). The agent is the node's name for
/// it, from its binding; the reason is the agent's own words and is shown quoted, never as the app's.
public struct SecretRequest: Equatable, Sendable, Identifiable {
    public let requestId: String
    public let agent: String
    public let project: String
    public let env: String
    public let name: String
    public let provider: String?
    public let reason: String
    public let replaces: Bool
    public let lastChange: String?
    public let deadline: String
    public var id: String { requestId }

    public init?(_ doc: [String: Any]) {
        guard let requestId = doc["requestId"] as? String, requestId.hasPrefix("sreq_"),
              let agent = doc["agent"] as? String, let project = doc["project"] as? String,
              let slot = doc["slot"] as? [String: Any], let env = slot["env"] as? String,
              let name = slot["name"] as? String, let reason = doc["reason"] as? String,
              let deadline = doc["deadline"] as? String else { return nil }
        self.requestId = requestId; self.agent = agent; self.project = project; self.env = env; self.name = name
        self.provider = doc["provider"] as? String
        self.reason = reason
        self.replaces = doc["replaces"] as? Bool ?? false
        self.lastChange = doc["lastChange"] as? String
        self.deadline = deadline
    }

    /// The requests inside one engine answer (`{"requests": [...]}`); a request this app cannot read is left out,
    /// never shown half.
    public static func list(answer: [String: Any]) -> [SecretRequest]? {
        guard let rows = answer["requests"] as? [[String: Any]] else { return nil }
        return rows.compactMap(SecretRequest.init)
    }

    /// Why the app shows no window for a request it read: no place for the phrase, or an engine another program of
    /// this user could change.
    public static let unavailableReasons: Set<String> = ["phrase-store-unavailable", "engine-untrusted"]

    /// The only inputs the window sends, each with the members the engine reads and nothing else. Anything else is
    /// nil, and the bridge is never called with it. The value travels only in `store`, on the child's stdin.
    public static func input(op: String, requestId: String? = nil, value: String? = nil,
                             reason: String? = nil) -> [String: String]? {
        switch op {
        case "list":
            return requestId == nil && value == nil && reason == nil ? ["op": op] : nil
        case "decline":
            guard let requestId, value == nil, reason == nil else { return nil }
            return ["op": op, "requestId": requestId]
        case "store":
            guard let requestId, let value, reason == nil, SecretValue.problem(value) == nil else { return nil }
            return ["op": op, "requestId": requestId, "value": value]
        case "unavailable":
            guard let requestId, value == nil, let reason, unavailableReasons.contains(reason) else { return nil }
            return ["op": op, "requestId": requestId, "reason": reason]
        default:
            return nil
        }
    }
}

/// What the window refuses to send before the engine sees it: the vault takes one line, not empty.
public enum SecretValue {
    public enum Problem: Equatable, Sendable { case empty, lineBreak }

    public static func problem(_ value: String) -> Problem? {
        let trimmed = value.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty { return .empty }
        if trimmed.trimmingCharacters(in: .newlines).contains(where: \.isNewline) { return .lineBreak }
        return nil
    }
}

/// The person's phrase: every genuine prompt shows it, so a window without it is not Observatory's (D4).
public enum Phrase {
    public static let minimum = 3
    public static let maximum = 64

    /// The phrase as it will be kept, or nil when it is too short, too long or more than one line.
    public static func normalized(_ text: String) -> String? {
        let phrase = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard (minimum...maximum).contains(phrase.count), !phrase.contains(where: \.isNewline) else { return nil }
        return phrase
    }
}

public enum PhraseStoreError: Error, Equatable {
    /// This build cannot keep the phrase apart from other programs: no keychain access group entitlement (an
    /// unsigned or ad hoc build). No phrase means no prompt.
    case unavailable
    case failed(Int32)
}

/// Where the phrase is kept. Never a vault slot: any agent could read a slot and draw a convincing imitation.
public protocol PhraseStore: AnyObject {
    func read() throws -> String?
    func write(_ phrase: String) throws
}

/// The data protection keychain, in the app's own access group (its first `keychain-access-groups` entitlement,
/// `<team>.ai.passioncode.observatory`): another program of the same user cannot read it. A legacy login-keychain
/// item is never used, because a `security` prompt the person approves can release it to an agent.
public final class KeychainPhraseStore: PhraseStore {
    public static let service = "ai.passioncode.observatory.secret-prompt-phrase"
    public static let account = "phrase"
    public init() {}

    /// The item's identity. No `kSecAttrAccessGroup`: the item lands in the app's default group, which only the
    /// signed app's entitlement names; a build without it gets `errSecMissingEntitlement`.
    public static func query() -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account,
         kSecUseDataProtectionKeychain as String: true]
    }

    public static func error(_ status: OSStatus) -> PhraseStoreError {
        status == errSecMissingEntitlement || status == errSecNotAvailable ? .unavailable : .failed(status)
    }

    public func read() throws -> String? {
        var q = Self.query()
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &out)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = out as? Data else { throw Self.error(status) }
        return String(data: data, encoding: .utf8)
    }

    public func write(_ phrase: String) throws {
        let data = Data(phrase.utf8)
        let status = SecItemUpdate(Self.query() as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecSuccess { return }
        guard status == errSecItemNotFound else { throw Self.error(status) }
        var add = Self.query()
        add[kSecValueData as String] = data
        add[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        let added = SecItemAdd(add as CFDictionary, nil)
        guard added == errSecSuccess else { throw Self.error(added) }
    }
}

/// What the window shows for a request, decided from the phrase store alone so it can be tested without a window.
public enum SecretPromptStage: Equatable, Sendable {
    /// No place for the phrase on this Mac: no window; the request is answered `no-client` with the vault step.
    case unavailable
    /// The engine the app runs could be changed by another program of this user: no window, `engine-untrusted`.
    case untrustedEngine
    /// First use: the person sets a phrase before any prompt is shown.
    case needsPhrase
    /// The prompt, showing the phrase.
    case prompt(phrase: String)

    public static func of(_ store: PhraseStore) -> SecretPromptStage {
        do {
            guard let phrase = try store.read(), let kept = Phrase.normalized(phrase) else { return .needsPhrase }
            return .prompt(phrase: kept)
        } catch PhraseStoreError.unavailable {
            return .unavailable
        } catch {
            return .unavailable
        }
    }
}

/// Whether the engine the app runs for the secret prompt is one no other program of this user can change (OBS-260,
/// the review of #382). The app's door answers only its own children, so whatever the app runs could read the queue,
/// answer requests and see a typed value: a per-user install (a virtual environment, Homebrew, `~/.local/bin`) or a
/// `defaults write` of the program's path would let any program of the same user do that. The prompt is therefore
/// shown only when the program is inside the signed app, or when the program, its interpreter and the folder trees
/// they live in are owned by the system and writable by nobody but it. Anything else is `engine-untrusted`.
public enum EngineTrust {
    public enum Verdict: Equatable, Sendable {
        case bundled
        case systemOwned
        /// Why not, as a short code: `outside-bundle-user-writable`, `interpreter-unresolved`, `too-large`.
        case untrusted(String)
        public var trusted: Bool { if case .untrusted = self { return false }; return true }
    }

    /// One entry as `lstat` sees it.
    public struct Entry: Equatable, Sendable {
        public let uid: UInt32
        public let mode: UInt16
        public let directory: Bool
        public init(uid: UInt32, mode: UInt16, directory: Bool) { self.uid = uid; self.mode = mode; self.directory = directory }
        /// Owned by the system and writable by nobody else.
        public var sealed: Bool { uid == 0 && mode & 0o022 == 0 }
    }

    /// The file system, so the rule can be tested without one.
    public struct FileSystem: Sendable {
        public var realpath: @Sendable (String) -> String?
        public var lstat: @Sendable (String) -> Entry?
        public var children: @Sendable (String) -> [String]?
        public var firstLine: @Sendable (String) -> String?
        public init(realpath: @escaping @Sendable (String) -> String?, lstat: @escaping @Sendable (String) -> Entry?,
                    children: @escaping @Sendable (String) -> [String]?, firstLine: @escaping @Sendable (String) -> String?) {
            self.realpath = realpath; self.lstat = lstat; self.children = children; self.firstLine = firstLine
        }

        public static let live = FileSystem(
            realpath: { path in
                guard let r = Darwin.realpath(path, nil) else { return nil }
                defer { free(r) }
                return String(cString: r)
            },
            lstat: { path in
                var st = stat()
                guard Darwin.lstat(path, &st) == 0 else { return nil }
                return Entry(uid: st.st_uid, mode: UInt16(st.st_mode & 0o7777), directory: (st.st_mode & S_IFMT) == S_IFDIR)
            },
            children: { path in try? FileManager.default.contentsOfDirectory(atPath: path) },
            firstLine: { path in
                guard let h = FileHandle(forReadingAtPath: path) else { return nil }
                defer { try? h.close() }
                let data = (try? h.read(upToCount: 512)) ?? Data()
                return String(decoding: data, as: UTF8.self).split(separator: "\n", maxSplits: 1).first.map(String.init)
            })
    }

    /// How many entries one check reads at most; a tree larger than this is not vouched for.
    public static let walkLimit = 60_000

    public static func verdict(executable: String, bundle: String, fs: FileSystem = .live) -> Verdict {
        guard let program = fs.realpath(executable) else { return .untrusted("program-unresolved") }
        let inside = (fs.realpath(bundle) ?? bundle) + "/"
        if program.hasPrefix(inside) { return .bundled }
        var budget = walkLimit
        guard sealed(program, fs) else { return .untrusted("outside-bundle-user-writable") }
        var trees = [prefix(of: program)]
        if let line = fs.firstLine(program), line.hasPrefix("#!") {
            // A script: its interpreter is checked the same way. `env` would pick it from PATH, so it is not vouched for.
            let words = line.dropFirst(2).split(separator: " ").map(String.init)
            guard let first = words.first, !first.hasSuffix("/env"), let interpreter = fs.realpath(first) else {
                return .untrusted("interpreter-unresolved")
            }
            guard sealed(interpreter, fs) else { return .untrusted("outside-bundle-user-writable") }
            trees.append(prefix(of: interpreter))
        }
        for tree in Set(trees) {
            switch walk(tree, fs, &budget) {
            case .some(false): return .untrusted("outside-bundle-user-writable")
            case .none: return .untrusted("too-large")
            case .some(true): continue
            }
        }
        return .systemOwned
    }

    /// The program's install prefix: the folder above its `bin`.
    static func prefix(of program: String) -> String {
        let bin = (program as NSString).deletingLastPathComponent
        let up = (bin as NSString).deletingLastPathComponent
        return up.isEmpty ? "/" : up
    }

    /// The file and every folder above it are sealed.
    static func sealed(_ path: String, _ fs: FileSystem) -> Bool {
        var p = path
        while true {
            guard let e = fs.lstat(p), e.sealed else { return false }
            if p == "/" { return true }
            p = (p as NSString).deletingLastPathComponent
            if p.isEmpty { p = "/" }
        }
    }

    /// Every entry of the tree is sealed: true, false, or nil past the budget.
    static func walk(_ root: String, _ fs: FileSystem, _ budget: inout Int) -> Bool? {
        guard sealed(root, fs) else { return false }
        var todo = [root]
        while let dir = todo.popLast() {
            guard let names = fs.children(dir) else { return false }
            for name in names {
                budget -= 1
                if budget < 0 { return nil }
                let path = dir + "/" + name
                guard let e = fs.lstat(path), e.sealed else { return false }
                if e.directory { todo.append(path) }
            }
        }
        return true
    }
}
