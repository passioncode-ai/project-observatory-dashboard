import XCTest
@testable import ObservatoryCore

/// The app is one PassionCode product with its dashboard: the same colour values,
/// defined once, readable, and no system colour slipping in beside them.
final class PaletteTests: XCTestCase {
    private var root: URL { URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("../../..").standardizedFileURL }

    func testEveryRoleIsTheVendoredDesignSystemValue() throws {
        let css = try String(contentsOf: root.appendingPathComponent("observatory/engine/dashboard/brand/passioncode-tokens.css"), encoding: .utf8)
        var declared: [String: UInt32] = [:]
        for line in css.components(separatedBy: "\n") {
            let parts = line.trimmingCharacters(in: .whitespaces).components(separatedBy: ":")
            guard parts.count == 2, parts[0].hasPrefix("--pc-"),
                  let hash = parts[1].firstIndex(of: "#") else { continue }
            let hex = parts[1][parts[1].index(after: hash)...].prefix { $0.isHexDigit }
            if hex.count == 6, let v = UInt32(hex, radix: 16) { declared[parts[0]] = v }
        }
        XCTAssertGreaterThanOrEqual(declared.count, 19, "the token file was not read")
        for (token, rgb) in Palette.byToken {
            XCTAssertEqual(declared[token].map { Palette.RGB($0) }, rgb, token)
        }
        // A colour role the design system adds is one the app has to decide on.
        let unmapped = Set(declared.keys).subtracting(Palette.byToken.keys).subtracting(["--pc-brand-plum", "--pc-brand-magenta"])
        XCTAssertEqual(unmapped, [], "new colour roles in the design system")
    }

    func testEveryTextPairPassesWCAGAA() {
        for p in Palette.textPairs {
            XCTAssertGreaterThanOrEqual(p.fg.contrast(with: p.bg), 4.5, "\(p.name): \(p.fg) on \(p.bg)")
        }
        for p in Palette.uiPairs {
            XCTAssertGreaterThanOrEqual(p.fg.contrast(with: p.bg), 3.0, "\(p.name): \(p.fg) on \(p.bg)")
        }
        // The reference points of the formula itself.
        XCTAssertEqual(Palette.RGB(0x000000).contrast(with: Palette.RGB(0xffffff)), 21, accuracy: 0.01)
        XCTAssertEqual(Palette.RGB(0x777777).contrast(with: Palette.RGB(0xffffff)), 4.48, accuracy: 0.01)
    }

    /// The views use `Theme` and nothing else for colour: a system colour (blue
    /// selection, a `.bordered` button, `.secondary` grey) is how the app stopped
    /// looking like its dashboard before.
    func testNoViewUsesASystemColour() throws {
        let dir = root.appendingPathComponent("macos/Sources/ObservatoryApp")
        let files = try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".swift") && $0 != "Theme.swift" }
        XCTAssertFalse(files.isEmpty)
        let banned = try NSRegularExpression(pattern:
            #"\.(red|green|blue|orange|yellow|purple|pink|gray|grey|secondary|primary|tertiary|quaternary|accentColor)\b(?!\s*:)|NSColor\.|Color\(|\.borderedProminent|\.bordered\b|\.regularMaterial|\.thinMaterial|\.ultraThinMaterial"#)
        for name in files {
            let text = try String(contentsOf: dir.appendingPathComponent(name), encoding: .utf8)
            for (n, line) in text.components(separatedBy: "\n").enumerated() {
                let code = line.components(separatedBy: "//")[0]
                let hits = banned.matches(in: code, range: NSRange(code.startIndex..., in: code))
                XCTAssertTrue(hits.isEmpty, "\(name):\(n + 1): \(line.trimmingCharacters(in: .whitespaces))")
            }
        }
    }
}
