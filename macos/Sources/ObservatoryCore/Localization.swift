import Foundation

/// The language a person chose for the app (L10N-01): the system's, or one of the two
/// the app speaks. Stored in the app's defaults under `language`.
public enum AppLanguage: String, CaseIterable, Sendable {
    case system
    case english = "en"
    case russian = "ru"
}

/// Which language the app speaks, and the words it speaks in (L10N-01…03).
///
/// English is the source and the key: views write `t("Refresh")`, and the Russian
/// dictionary (`Resources/ru.lproj/Localizable.strings`) maps that English text to
/// Russian. Values go in named placeholders (`{version}`), never by concatenation. A
/// count goes through `plural`, whose forms live in `Localizable.stringsdict` — three
/// for Russian (one, few, many), two for English. A string with no Russian entry shows
/// in English, never as a key.
public enum Localization {
    /// The defaults key holding the choice (`system`, `en`, `ru`).
    public static let choiceKey = "language"
    /// The 0.18 switch, a Bool: still read when no choice is stored, so an earlier
    /// choice — or `-russian YES` on the command line — keeps working.
    public static let legacyKey = "russian"
    /// AppKit reads this key in the app's own domain to pick the bundle's language for
    /// the menus it draws itself (Quit, Hide, Window); see `pinBundleLanguage`.
    public static let appleLanguagesKey = "AppleLanguages"

    /// The stored choice. An unknown value is System, never an error.
    public static func choice(stored: Any?, legacy: Bool?) -> AppLanguage {
        if let raw = stored as? String {
            return AppLanguage(rawValue: raw.lowercased()) ?? .system
        }
        if stored != nil { return .system }
        if let legacy { return legacy ? .russian : .english }
        return .system
    }

    /// Russian when the first preferred language is Russian (`ru`, `ru-RU`, `ru_RU`),
    /// English otherwise — an empty list included.
    public static func systemLanguage(_ preferred: [String]) -> String {
        guard let first = preferred.first?.lowercased() else { return "en" }
        return first == "ru" || first.hasPrefix("ru-") || first.hasPrefix("ru_") ? "ru" : "en"
    }

    /// The language the app speaks: `en` or `ru`.
    public static func resolve(_ choice: AppLanguage, preferred: [String]) -> String {
        switch choice {
        case .system: return systemLanguage(preferred)
        case .english: return "en"
        case .russian: return "ru"
        }
    }

    /// The person's own language list, from the global domain: the app's domain may
    /// carry the `AppleLanguages` this app pinned for itself, which must not decide
    /// what "System" means.
    public static func systemPreferredLanguages() -> [String] {
        if let list = UserDefaults.standard.persistentDomain(forName: UserDefaults.globalDomain)?[appleLanguagesKey] as? [String] {
            return list
        }
        return Locale.preferredLanguages
    }

    /// Pins (or, for System, releases) the language AppKit uses for the menus it draws
    /// itself. They follow at the app's next launch; the app's own words follow at once.
    public static func pinBundleLanguage(_ choice: AppLanguage, in defaults: UserDefaults) {
        switch choice {
        case .system: defaults.removeObject(forKey: appleLanguagesKey)
        case .english, .russian: defaults.set([choice.rawValue], forKey: appleLanguagesKey)
        }
    }

    /// `{name}` placeholders in a text, in order of appearance, without repeats.
    public static func placeholders(_ text: String) -> [String] {
        var seen: [String] = []
        for m in placeholderPattern.matches(in: text, range: NSRange(text.startIndex..., in: text)) {
            if let r = Range(m.range(at: 1), in: text), !seen.contains(String(text[r])) { seen.append(String(text[r])) }
        }
        return seen
    }

    /// The text with each `{name}` it has a value for filled in; an unknown one stays as written.
    public static func fill(_ text: String, _ args: [String: String]) -> String {
        guard !args.isEmpty, text.contains("{") else { return text }
        var out = "", rest = text[...]
        while let open = rest.firstIndex(of: "{") {
            out += rest[..<open]
            guard let close = rest[open...].firstIndex(of: "}") else { break }
            let name = String(rest[rest.index(after: open)..<close])
            if let value = args[name], isName(name) { out += value } else { out += rest[open...close] }
            rest = rest[rest.index(after: close)...]
        }
        return out + rest
    }
    private static func isName(_ s: String) -> Bool { s.range(of: #"^[a-z][a-zA-Z0-9_]*$"#, options: .regularExpression) != nil }
    private static let placeholderPattern = try! NSRegularExpression(pattern: #"\{([a-z][a-zA-Z0-9_]*)\}"#)

    /// CLDR's plural category for a whole number.
    public static func pluralCategory(_ n: Int, language: String) -> String {
        let a = abs(n)
        if language == "ru" {
            let d = a % 10, h = a % 100
            if d == 1 && h != 11 { return "one" }
            if (2...4).contains(d) && !(12...14).contains(h) { return "few" }
            return "many"
        }
        return a == 1 ? "one" : "other"
    }

    /// The folder holding `ru.lproj` and `en.lproj`: the app bundle's Resources, where
    /// build-app.sh puts them, else SwiftPM's resource bundle beside the executable
    /// (`swift run`). Never `Bundle.module`, whose accessor ends the process when its
    /// bundle is missing.
    public static func resourceDirectory(main: Bundle = .main) -> URL? {
        let bundleName = "ProjectObservatory_ObservatoryCore.bundle"
        var candidates: [URL] = []
        if let r = main.resourceURL { candidates.append(r) }
        // The test bundle (or the app) this code is linked into: `swift test` runs from a
        // host executable elsewhere, with the resource bundle beside the `.xctest`.
        let linked = Bundle(for: BundleMarker.self).bundleURL.deletingLastPathComponent()
        for base in [main.bundleURL, main.executableURL?.deletingLastPathComponent(), linked].compactMap({ $0 }) {
            candidates.append(base.appendingPathComponent(bundleName))
            candidates.append(base.appendingPathComponent(bundleName).appendingPathComponent("Contents/Resources"))
        }
        return candidates.first {
            FileManager.default.fileExists(atPath: $0.appendingPathComponent("ru.lproj/Localizable.strings").path)
        }
    }
}

private final class BundleMarker {}

/// One language's entries: plain strings and plural entries (category → text).
public struct Catalog: Sendable, Equatable {
    public var strings: [String: String]
    public var plurals: [String: [String: String]]
    public init(strings: [String: String] = [:], plurals: [String: [String: String]] = [:]) {
        self.strings = strings; self.plurals = plurals
    }

    /// Reads `<lproj>/Localizable.strings` and `<lproj>/Localizable.stringsdict`. A
    /// missing file is an empty part; an unreadable one throws.
    public static func load(_ lproj: URL) throws -> Catalog {
        var catalog = Catalog()
        let strings = lproj.appendingPathComponent("Localizable.strings")
        if FileManager.default.fileExists(atPath: strings.path) {
            guard let doc = try PropertyListSerialization.propertyList(from: Data(contentsOf: strings), format: nil) as? [String: String] else {
                throw CocoaError(.propertyListReadCorrupt)
            }
            catalog.strings = doc
        }
        let dict = lproj.appendingPathComponent("Localizable.stringsdict")
        if FileManager.default.fileExists(atPath: dict.path) {
            guard let doc = try PropertyListSerialization.propertyList(from: Data(contentsOf: dict), format: nil) as? [String: Any] else {
                throw CocoaError(.propertyListReadCorrupt)
            }
            for (key, value) in doc {
                // `%#@n@` names the variable whose dictionary holds the forms.
                guard let entry = value as? [String: Any], let format = entry["NSStringLocalizedFormatKey"] as? String,
                      format.hasPrefix("%#@"), format.hasSuffix("@"), format.count > 4,
                      let forms = entry[String(format.dropFirst(3).dropLast())] as? [String: Any] else {
                    throw CocoaError(.propertyListReadCorrupt)
                }
                catalog.plurals[key] = forms.compactMapValues { $0 as? String }
                    .filter { ["zero", "one", "two", "few", "many", "other"].contains($0.key) }
            }
        }
        return catalog
    }
}

/// The app's words in its two languages.
public struct Localizer: Sendable {
    public let russian: Catalog
    public let english: Catalog
    public init(russian: Catalog, english: Catalog) { self.russian = russian; self.english = english }

    /// The packaged dictionaries; with none found (a broken bundle), English alone —
    /// readable, and the Russian check in the tests is what keeps it from shipping.
    public static let shared: Localizer = {
        guard let dir = Localization.resourceDirectory() else { return Localizer(russian: Catalog(), english: Catalog()) }
        return Localizer(russian: (try? Catalog.load(dir.appendingPathComponent("ru.lproj"))) ?? Catalog(),
                         english: (try? Catalog.load(dir.appendingPathComponent("en.lproj"))) ?? Catalog())
    }()

    public func text(_ key: String, language: String, _ args: [String: String] = [:]) -> String {
        let found = language == "ru" ? russian.strings[key] : nil
        return Localization.fill(found ?? key, args)
    }

    /// A sentence about `count` things, its form chosen by the language's plural rule.
    /// `args` fills the other placeholders; the count's own placeholder is named in the key.
    public func plural(_ key: String, count: Int, language: String, _ args: [String: String] = [:]) -> String {
        let forms = (language == "ru" ? russian : english).plurals[key] ?? english.plurals[key] ?? [:]
        let category = Localization.pluralCategory(count, language: language == "ru" && russian.plurals[key] != nil ? "ru" : "en")
        return Localization.fill(forms[category] ?? forms["other"] ?? key, args)
    }
}
