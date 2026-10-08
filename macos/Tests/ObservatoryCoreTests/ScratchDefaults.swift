import XCTest
import Foundation
@testable import ObservatoryApp
import ObservatoryCore

/// Defaults that live in memory only. A suite (`UserDefaults(suiteName:)`) is backed
/// by cfprefsd, and `removePersistentDomain` empties it but leaves its file — and
/// cfprefsd writes the file back when a model still holding the suite saves after
/// teardown. Every `swift test` run left a dozen empty
/// `~/Library/Preferences/observatory-tests-<uuid>.plist` in the person's own
/// preferences (344 on one machine). The app reads and writes defaults through
/// these six methods, so overriding them keeps a test entirely off the disk.
final class MemoryDefaults: UserDefaults {
    private var values: [String: Any] = [:]
    init() { super.init(suiteName: nil)! }
    override func object(forKey key: String) -> Any? { values[key] }
    override func string(forKey key: String) -> String? { values[key] as? String }
    override func bool(forKey key: String) -> Bool {
        switch values[key] {
        case let b as Bool: return b
        case let n as NSNumber: return n.boolValue
        case let s as String: return ["yes", "true", "1"].contains(s.lowercased())   // a launch argument's spelling
        default: return false
        }
    }
    override func set(_ value: Any?, forKey key: String) { values[key] = value }
    override func set(_ value: Bool, forKey key: String) { values[key] = value }
    override func removeObject(forKey key: String) { values[key] = nil }
}

extension XCTestCase {
    /// Defaults for this test only; nothing reaches the disk.
    func scratchDefaults(_ prefix: String = "observatory-tests") -> UserDefaults { MemoryDefaults() }
    /// An update watch that logs into a temporary folder and can neither spawn nor quit.
    @MainActor func scratchUpdates(_ defaults: UserDefaults? = nil) -> Updates {
        let log = AppLog(file: FileManager.default.temporaryDirectory.appendingPathComponent("obs-log-\(UUID().uuidString)/app.log"))
        return Updates(defaults: defaults ?? scratchDefaults(), log: log, spawn: { _, _ in throw BridgeError.configuration }, terminate: {},
                       badge: { _ in })
    }
}

final class ScratchDefaultsTests: XCTestCase {
    func testScratchDefaultsKeepValuesAndWriteNoPreferencesFile() async throws {
        let before = Set((try? FileManager.default.contentsOfDirectory(atPath: NSHomeDirectory() + "/Library/Preferences")) ?? [])
        let d = scratchDefaults()
        d.set("/srv/example-ws", forKey: "workspace"); d.set(true, forKey: "russian")
        XCTAssertEqual(d.string(forKey: "workspace"), "/srv/example-ws")
        XCTAssertTrue(d.bool(forKey: "russian")); XCTAssertNotNil(d.object(forKey: "russian"))
        d.set("YES", forKey: "launch"); XCTAssertTrue(d.bool(forKey: "launch"))
        XCTAssertNil(UserDefaults.standard.string(forKey: "workspace-\(ObjectIdentifier(d).hashValue)"))
        try await Task.sleep(for: .milliseconds(300))
        let after = Set((try? FileManager.default.contentsOfDirectory(atPath: NSHomeDirectory() + "/Library/Preferences")) ?? [])
        XCTAssertTrue(after.subtracting(before).filter { $0.hasPrefix("observatory-") }.isEmpty, "\(after.subtracting(before))")
    }
}
