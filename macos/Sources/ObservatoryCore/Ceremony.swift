import Foundation

/// The pairing screen the engine answers (`full assistant ceremony`, `node/ceremony.py`, OBS-197): one state of
/// adding a machine to the person's network, read as it is. The app decides no ceremony rule of its own: the code,
/// the wait, the refusal and its reason are the engine's, checked against the contract's vectors there.
public struct CeremonyScreen: Equatable, Sendable {
    public enum State: Equatable, Sendable {
        case notPaired, hubUnreachable, waitingForHub, codeShown, passkey, confirmed, refused, expired
        /// A state this app does not know: a newer engine. Shown as such, never as another state.
        case unknown(String)

        init(_ raw: String) {
            switch raw {
            case "not-paired": self = .notPaired
            case "hub-unreachable": self = .hubUnreachable
            case "waiting-for-hub": self = .waitingForHub
            case "code-shown": self = .codeShown
            case "passkey": self = .passkey
            case "confirmed": self = .confirmed
            case "refused": self = .refused
            case "expired": self = .expired
            default: self = .unknown(raw)
            }
        }
    }

    public struct Key: Equatable, Sendable {
        public let kid: String
        public let fingerprint: String
    }

    public let state: State
    /// `other-side` (the other machine has not answered) or `revision` (the codes matched; the revision is awaited).
    public let step: String?
    /// The hub's state when it is the wait: `not-ready` (it runs no pairing yet) or `unreachable`.
    public let hub: String?
    public let code: String?
    public let fingerprint: String?
    public let machine: String?
    public let keys: [Key]
    /// Only on `passkey`: whether the hub serves the page the passkey signs on.
    public let canSign: Bool
    /// Only on `refused`: the check's own reason, and the group a sentence words.
    public let reason: String?
    public let group: String?
    public let secondsLeft: Int?

    /// The screen inside one engine answer (`{"screen": {...}, "hub": "..."}`), or nil when it carries none.
    public init?(answer: [String: Any]) {
        guard let screen = answer["screen"] as? [String: Any], screen["schema"] as? String == "node-ceremony/1",
              let raw = screen["state"] as? String else { return nil }
        state = State(raw)
        step = screen["step"] as? String
        hub = screen["hub"] as? String
        code = screen["code"] as? String
        fingerprint = screen["fingerprint"] as? String
        machine = screen["machine"] as? String
        keys = (screen["keys"] as? [[String: Any]] ?? []).compactMap { key in
            guard let kid = key["kid"] as? String, let fingerprint = key["fingerprint"] as? String else { return nil }
            return Key(kid: kid, fingerprint: fingerprint)
        }
        canSign = screen["canSign"] as? Bool ?? false
        reason = screen["reason"] as? String
        group = screen["group"] as? String
        secondsLeft = (screen["secondsLeft"] as? NSNumber)?.intValue
    }

    /// The steps the window may send, each with the members the engine reads and nothing else: a start names its
    /// role, no other step carries one. Anything else is nil, and the bridge is never called with it.
    public static func input(op: String, role: String? = nil) -> [String: String]? {
        guard ["status", "start", "confirm", "differ", "sign", "cancel"].contains(op) else { return nil }
        if op == "start" {
            guard let role, role == "new" || role == "trusted" else { return nil }
            return ["op": op, "role": role]
        }
        return role == nil ? ["op": op] : nil
    }
}
