import Foundation

/// The PassionCode design system's colour roles, the same values the dashboard's
/// pages use (`observatory/engine/dashboard/brand/passioncode-tokens.css`, pinned
/// there byte for byte). Defined ONCE here: the app's views read `Theme`, which
/// reads this, and a test compares every value with the vendored CSS and checks
/// the text pairs the app draws against WCAG AA. PassionCode is dark by design,
/// so there is one set, not a light and a dark twin: the token file's opt-in light
/// palette (1.1.0) is used by neither the app nor the dashboard pages.
public enum Palette {
    public struct RGB: Equatable, Sendable, CustomStringConvertible {
        public let hex: UInt32
        public init(_ hex: UInt32) { self.hex = hex }
        public var red: Double { Double((hex >> 16) & 0xff) / 255 }
        public var green: Double { Double((hex >> 8) & 0xff) / 255 }
        public var blue: Double { Double(hex & 0xff) / 255 }
        public var description: String { String(format: "#%06x", hex) }

        /// WCAG 2.x relative luminance of an sRGB colour.
        public var luminance: Double {
            func linear(_ c: Double) -> Double { c <= 0.04045 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4) }
            return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)
        }
        /// WCAG contrast ratio, 1…21.
        public func contrast(with other: RGB) -> Double {
            let (a, b) = (luminance, other.luminance)
            return (max(a, b) + 0.05) / (min(a, b) + 0.05)
        }
    }

    public static let bg = RGB(0x0a070d)
    public static let panel = RGB(0x110c15)
    public static let panelRaised = RGB(0x1b1420)
    public static let text = RGB(0xfff9f0)
    public static let textMuted = RGB(0xb6aabc)
    public static let border = RGB(0x342b3a)
    public static let borderStrong = RGB(0x6f5e77)
    public static let accent = RGB(0xffd21a)
    public static let accentHover = RGB(0xffdf5e)
    public static let onAccent = RGB(0x211900)
    public static let accentSoft = RGB(0x30270e)
    public static let positive = RGB(0x79d49a)
    public static let positiveSoft = RGB(0x12291c)
    public static let warning = RGB(0xf6ba75)
    public static let warningSoft = RGB(0x332314)
    public static let negative = RGB(0xff969f)
    public static let negativeSoft = RGB(0x35191f)
    public static let info = RGB(0xa6bfff)
    public static let infoSoft = RGB(0x18243d)

    /// Each role by its name in the design system's CSS, for the drift test.
    public static let byToken: [String: RGB] = [
        "--pc-bg": bg, "--pc-panel": panel, "--pc-panel-raised": panelRaised,
        "--pc-text": text, "--pc-text-muted": textMuted,
        "--pc-border": border, "--pc-border-strong": borderStrong,
        "--pc-accent": accent, "--pc-accent-hover": accentHover, "--pc-on-accent": onAccent, "--pc-accent-soft": accentSoft,
        "--pc-positive": positive, "--pc-positive-soft": positiveSoft,
        "--pc-warning": warning, "--pc-warning-soft": warningSoft,
        "--pc-negative": negative, "--pc-negative-soft": negativeSoft,
        "--pc-info": info, "--pc-info-soft": infoSoft,
    ]

    /// Radii and spacing of the same system, in points.
    public static let radiusControl: Double = 8
    public static let radiusPanel: Double = 16
    public static let radiusFeature: Double = 24
    public static let space: [Double] = [4, 8, 12, 16, 24, 32]

    /// Every foreground/background pair the app draws text with. The test holds
    /// each to WCAG AA for normal text (4.5:1); the app adds no other pair.
    public static let textPairs: [(name: String, fg: RGB, bg: RGB)] = {
        var pairs: [(String, RGB, RGB)] = []
        for (surface, s) in [("bg", bg), ("panel", panel), ("raised", panelRaised)] {
            for (role, f) in [("text", text), ("muted", textMuted), ("accent", accent), ("positive", positive),
                              ("warning", warning), ("negative", negative), ("info", info)] {
                pairs.append(("\(role) on \(surface)", f, s))
            }
        }
        pairs += [("on-accent on accent", onAccent, accent), ("on-accent on accent-hover", onAccent, accentHover),
                  ("text on accent-soft", text, accentSoft), ("accent on accent-soft", accent, accentSoft),
                  ("warning on warning-soft", warning, warningSoft), ("text on warning-soft", text, warningSoft),
                  ("muted on warning-soft", textMuted, warningSoft),
                  ("negative on negative-soft", negative, negativeSoft), ("text on negative-soft", text, negativeSoft),
                  ("muted on negative-soft", textMuted, negativeSoft),
                  ("positive on positive-soft", positive, positiveSoft), ("info on info-soft", info, infoSoft),
                  ("text on info-soft", text, infoSoft), ("muted on info-soft", textMuted, infoSoft)]
        return pairs
    }()
    /// Non-text marks that must be seen: WCAG 1.4.11, 3:1. The focus ring is held to it,
    /// and so is a control's resting edge — the design system's `--pc-border-strong`,
    /// as on the dashboard's own inputs — on every surface a field or button sits on.
    public static let uiPairs: [(name: String, fg: RGB, bg: RGB)] = [
        ("focus ring on bg", accent, bg), ("focus ring on panel", accent, panel), ("focus ring on raised", accent, panelRaised),
        ("control edge on bg", borderStrong, bg), ("control edge on panel", borderStrong, panel),
        ("control edge on raised", borderStrong, panelRaised),
    ]
}
