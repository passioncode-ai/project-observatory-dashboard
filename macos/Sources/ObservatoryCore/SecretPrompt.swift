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

    /// Why the app shows no window for a request it read: no place for the phrase.
    public static let unavailableReasons: Set<String> = ["phrase-store-unavailable"]

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

/// The only engine the secret prompt ever runs (OBS-260, the review of #382): the one inside this app, started by an
/// absolute path the app computes from its own bundle, and only after the app's own static code signature checks out,
/// resources sealed, against the team its sealed Info.plist names (`ObservatoryTeamID`). The `executable` preference, PATH and any launcher are never
/// used for it: a program of the same user can change those, and whatever the app runs could read the queue, answer
/// requests and see a typed value. No engine is bundled in this release, so `resolve` answers nil and the node answers
/// every request `no-client`, reason `engine-untrusted`, before filing it.
public enum SecretEngine {
    /// Where the bundled engine will live, inside the sealed resources.
    public static let relativePath = "Contents/Resources/engine/bin/project-observatory"
    public static let bundleId = "ai.passioncode.observatory"
    /// The Info.plist key build-app.sh writes the signing team into; the plist is sealed by the signature.
    public static let teamKey = "ObservatoryTeamID"

    /// An Apple-issued Developer ID certificate of this team, for this bundle id; nil for a team that is not one.
    public static func requirement(team: String?) -> String? {
        guard let team, team.count == 10, team.allSatisfy({ $0.isASCII && ($0.isUppercase || $0.isNumber) }) else { return nil }
        return "anchor apple generic and identifier \"\(bundleId)\" and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = \"\(team)\""
    }

    /// The engine's absolute path, or nil: no team in the sealed Info.plist, a signature that does not check out, or no engine inside.
    /// Called immediately before every secret call, off the main thread.
    public static func resolve(bundle: URL, team: String?, signatureValid: (URL, String) -> Bool,
                               isExecutable: (String) -> Bool) -> String? {
        // The cheap checks first: with no team or no engine inside (every release so far), no bundle is hashed.
        guard let requirement = requirement(team: team) else { return nil }
        let engine = bundle.appendingPathComponent(relativePath).path
        guard isExecutable(engine), signatureValid(bundle, requirement) else { return nil }
        return engine
    }

    /// This bundle's static signature: every architecture, strict validation, nested code and sealed resources, and
    /// the requirement built from the team in its sealed Info.plist (not compiled in: a re-signed bundle carries its own).
    public static func ownSignatureValid(_ bundle: URL, _ requirement: String) -> Bool {
        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(bundle as CFURL, [], &code) == errSecSuccess, let code else { return false }
        var req: SecRequirement?
        guard SecRequirementCreateWithString(requirement as CFString, [], &req) == errSecSuccess, let req else { return false }
        let flags = SecCSFlags(rawValue: UInt32(kSecCSCheckAllArchitectures) | UInt32(kSecCSStrictValidate)
                                         | UInt32(kSecCSCheckNestedCode))
        return SecStaticCodeCheckValidity(code, flags, req) == errSecSuccess
    }

    /// The live answer for this running app.
    public static func live() -> String? {
        resolve(bundle: Bundle.main.bundleURL, team: Bundle.main.object(forInfoDictionaryKey: teamKey) as? String,
                signatureValid: ownSignatureValid, isExecutable: { FileManager.default.isExecutableFile(atPath: $0) })
    }
}
