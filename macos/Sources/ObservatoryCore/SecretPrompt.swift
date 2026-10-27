import Foundation
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
            guard let requestId, value == nil, reason == "phrase-store-unavailable" else { return nil }
            return ["op": op, "requestId": requestId, "reason": "phrase-store-unavailable"]
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
