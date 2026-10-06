import XCTest
@testable import ObservatoryApp
@testable import ObservatoryCore

/// L10N-01…03: which language the app speaks, and that every word it speaks has its Russian.
@MainActor final class LocalizationTests: XCTestCase {
    private var macos: URL { URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("../..").standardizedFileURL }
    private var resources: URL { macos.appendingPathComponent("Sources/ObservatoryCore/Resources") }

    // MARK: L10N-01 — the system decides, the switch overrides

    func testTheSystemLanguageDecidesWhenNothingIsChosen() {
        XCTAssertEqual(Localization.resolve(.system, preferred: ["ru-RU", "en-US"]), "ru")
        XCTAssertEqual(Localization.resolve(.system, preferred: ["ru"]), "ru")
        XCTAssertEqual(Localization.resolve(.system, preferred: ["ru_RU"]), "ru")
        XCTAssertEqual(Localization.resolve(.system, preferred: ["en-US", "ru-RU"]), "en", "only the FIRST language counts")
        XCTAssertEqual(Localization.resolve(.system, preferred: []), "en")
        XCTAssertEqual(Localization.resolve(.system, preferred: ["rue-SK"]), "en", "Rusyn is not Russian")
        XCTAssertEqual(Localization.resolve(.russian, preferred: ["en-US"]), "ru")
        XCTAssertEqual(Localization.resolve(.english, preferred: ["ru-RU"]), "en")
    }

    func testAnUnknownStoredValueIsSystemAndTheOldSwitchIsStillRead() {
        XCTAssertEqual(Localization.choice(stored: "fr", legacy: nil), .system)
        XCTAssertEqual(Localization.choice(stored: 42, legacy: true), .system, "a stored value of another type is not a choice")
        XCTAssertEqual(Localization.choice(stored: "", legacy: nil), .system)
        XCTAssertEqual(Localization.choice(stored: "RU", legacy: nil), .russian)
        XCTAssertEqual(Localization.choice(stored: "en", legacy: true), .english, "a choice outranks the 0.18 switch")
        XCTAssertEqual(Localization.choice(stored: nil, legacy: true), .russian)
        XCTAssertEqual(Localization.choice(stored: nil, legacy: false), .english)
        XCTAssertEqual(Localization.choice(stored: nil, legacy: nil), .system)
    }

    func testTheChoicePersistsAndPinsTheMenusLanguage() {
        let d = scratchDefaults()
        let m = Model(defaults: d, preferredLanguages: { ["ru-RU"] })
        XCTAssertEqual(m.language, .system); XCTAssertTrue(m.russian)
        XCTAssertEqual(m.t("Restart to update"), "Перезапустить для обновления")
        m.language = .english
        XCTAssertFalse(m.russian); XCTAssertEqual(m.t("Restart to update"), "Restart to update")
        XCTAssertEqual(d.string(forKey: "language"), "en")
        XCTAssertEqual(d.object(forKey: "AppleLanguages") as? [String], ["en"])
        XCTAssertEqual(Model(defaults: d, preferredLanguages: { ["ru-RU"] }).language, .english, "survives a relaunch")
        m.language = .system
        XCTAssertNil(d.object(forKey: "AppleLanguages"), "System releases the pin")
        d.set("klingon", forKey: "language")
        let unknown = Model(defaults: d, preferredLanguages: { ["en-US"] })
        XCTAssertEqual(unknown.language, .system); XCTAssertFalse(unknown.russian)
    }

    func testSystemToAnExplicitChoiceOfTheSameLanguageDoesNotReloadThePage() {
        let m = Model(defaults: scratchDefaults(), preferredLanguages: { ["ru"] })
        let seed = m.localeSeed
        m.language = .russian
        XCTAssertEqual(m.localeSeed, seed, "the language spoken did not change")
        m.language = .english
        XCTAssertEqual(m.localeSeed, seed + 1)
        m.adoptPageLocale("en")
        XCTAssertEqual(m.language, .english); XCTAssertEqual(m.localeSeed, seed + 1)
    }

    // MARK: L10N-02 — English is the key; every key has its Russian

    /// Every `t("…")` and `plural("…")` the app's sources write, as the text the
    /// dictionary must hold.
    private func usedKeys() throws -> (strings: Set<String>, plurals: Set<String>, interpolated: [String]) {
        let dir = macos.appendingPathComponent("Sources/ObservatoryApp")
        let lit = #""((?:[^"\\\n]|\\.)*)""#
        let t = try NSRegularExpression(pattern: #"\bt\(\s*"# + lit)
        let p = try NSRegularExpression(pattern: #"\bplural\(\s*"# + lit)
        var strings = Set<String>(), plurals = Set<String>(), interpolated: [String] = []
        for name in try FileManager.default.contentsOfDirectory(atPath: dir.path) where name.hasSuffix(".swift") {
            let text = try String(contentsOf: dir.appendingPathComponent(name), encoding: .utf8)
            for (re, into) in [(t, 0), (p, 1)] {
                for m in re.matches(in: text, range: NSRange(text.startIndex..., in: text)) {
                    let raw = String(text[Range(m.range(at: 1), in: text)!])
                    if raw.contains("\\(") { interpolated.append("\(name): \(raw)"); continue }
                    let key = unescape(raw)
                    if into == 0 { strings.insert(key) } else { plurals.insert(key) }
                }
            }
        }
        return (strings, plurals, interpolated)
    }
    private func unescape(_ s: String) -> String {
        var out = "", it = s.makeIterator()
        while let c = it.next() {
            guard c == "\\", let n = it.next() else { out.append(c); continue }
            switch n { case "n": out.append("\n"); case "t": out.append("\t"); default: out.append(n) }
        }
        return out
    }

    func testEveryKeyTheAppUsesHasARussianEntryWithTheSamePlaceholders() throws {
        let ru = try Catalog.load(resources.appendingPathComponent("ru.lproj"))
        let used = try usedKeys()
        XCTAssertGreaterThan(used.strings.count, 100, "the scan found the app's strings")
        XCTAssertEqual(used.interpolated, [], "a key carries {placeholders}, never Swift interpolation (L10N-02)")
        XCTAssertEqual(used.strings.subtracting(ru.strings.keys).sorted(), [], "keys with no Russian entry")
        XCTAssertEqual(Set(ru.strings.keys).subtracting(used.strings).sorted(), [], "Russian entries no code uses")
        for (key, value) in ru.strings {
            XCTAssertEqual(Set(Localization.placeholders(value)), Set(Localization.placeholders(key)), key)
        }
        XCTAssertEqual(used.plurals.subtracting(ru.plurals.keys).sorted(), [], "counts with no Russian forms")
        XCTAssertEqual(Set(ru.plurals.keys).subtracting(used.plurals).sorted(), [], "plural entries no code uses")
        // No call passes anything but a literal, except the one that forwards a key to the page's sheets.
        let dir = macos.appendingPathComponent("Sources/ObservatoryApp")
        for name in try FileManager.default.contentsOfDirectory(atPath: dir.path) where name.hasSuffix(".swift") {
            let text = try String(contentsOf: dir.appendingPathComponent(name), encoding: .utf8)
            for line in text.components(separatedBy: "\n") where line.range(of: #"\b(t|plural)\(\s*[^"\s)]"#, options: .regularExpression) != nil {
                XCTAssertTrue(line.contains("model?.t(key)") || line.contains("func t(") || line.contains("func plural(")
                              || line.contains("Localizer.shared."),
                              "\(name): a non-literal key cannot be checked: \(line.trimmingCharacters(in: .whitespaces))")
            }
        }
    }

    func testTheGlossaryTermsAreTheOrganizationsOwn() throws {
        let ru = try Catalog.load(resources.appendingPathComponent("ru.lproj")).strings
        XCTAssertEqual(ru["Restart to update"], "Перезапустить для обновления")
        XCTAssertEqual(ru["Cancel"], "Отменить")
        XCTAssertEqual(ru["Settings"], "Настройки")
        XCTAssertEqual(ru["All projects"], "Все проекты")
    }

    func testTheBuiltResourcesAreTheSourceDictionaries() throws {
        // Package.swift processes Resources into the bundle the app and the tests read.
        let dir = try XCTUnwrap(Localization.resourceDirectory(), "no packaged ru.lproj was found")
        for lang in ["ru", "en"] {
            XCTAssertEqual(try Catalog.load(dir.appendingPathComponent("\(lang).lproj")),
                           try Catalog.load(resources.appendingPathComponent("\(lang).lproj")), lang)
        }
        XCTAssertEqual(Localizer.shared.russian, try Catalog.load(resources.appendingPathComponent("ru.lproj")))
    }

    func testAMissingEntryShowsEnglishWithItsValues() {
        let l = Localizer(russian: Catalog(strings: ["Hello {name}": "Привет, {name}"]), english: Catalog())
        XCTAssertEqual(l.text("Hello {name}", language: "ru", ["name": "Ада"]), "Привет, Ада")
        XCTAssertEqual(l.text("Hello {name}", language: "en", ["name": "Ada"]), "Hello Ada")
        XCTAssertEqual(l.text("Not translated {n}", language: "ru", ["n": "3"]), "Not translated 3", "English, never the key's braces")
        XCTAssertEqual(Localization.fill("{a} and {b} and {a}", ["a": "1"]), "1 and {b} and 1", "an unknown placeholder stays visible")
        XCTAssertEqual(Localization.fill("{ not a name }", ["not a name": "x"]), "{ not a name }")
        XCTAssertEqual(Localization.placeholders("{shown} of {total}, {shown}"), ["shown", "total"])
    }

    // MARK: L10N-03 — counts use real plural forms

    func testThePluralRuleOfEachLanguage() {
        let ru = [0: "many", 1: "one", 2: "few", 4: "few", 5: "many", 11: "many", 12: "many", 14: "many",
                  21: "one", 22: "few", 25: "many", 101: "one", 111: "many", 6000: "many"]
        for (n, c) in ru { XCTAssertEqual(Localization.pluralCategory(n, language: "ru"), c, "\(n)") }
        XCTAssertEqual(Localization.pluralCategory(1, language: "en"), "one")
        XCTAssertEqual(Localization.pluralCategory(0, language: "en"), "other")
        XCTAssertEqual(Localization.pluralCategory(21, language: "en"), "other")
    }

    func testEveryPluralEntryHasEachFormOfItsLanguage() throws {
        let ru = try Catalog.load(resources.appendingPathComponent("ru.lproj")).plurals
        let en = try Catalog.load(resources.appendingPathComponent("en.lproj")).plurals
        XCTAssertFalse(ru.isEmpty)
        XCTAssertEqual(Set(ru.keys), Set(en.keys))
        for (key, forms) in ru {
            for c in ["one", "few", "many"] { XCTAssertNotNil(forms[c], "\(key): Russian \(c)") }
            for text in forms.values { XCTAssertEqual(Set(Localization.placeholders(text)), Set(Localization.placeholders(key)), key) }
        }
        for (key, forms) in en {
            for c in ["one", "other"] { XCTAssertNotNil(forms[c], "\(key): English \(c)") }
            XCTAssertEqual(forms["other"], key, "the English key is its own plural form")
        }
    }

    func testACountReadsInTheRightFormInBothLanguages() {
        let m = Model(defaults: scratchDefaults(), preferredLanguages: { ["en"] })
        func trimmed(_ shown: Int, _ total: Int) -> String {
            m.limitation(["code": "trimmed", "source": "findings", "shown": shown, "total": total])
        }
        XCTAssertEqual(trimmed(1, 1), "1 of 1 finding was included, most severe first.")
        XCTAssertEqual(trimmed(3, 40), "3 of 40 findings were included, most severe first.")
        m.language = .russian
        XCTAssertEqual(trimmed(3, 21), "Включено 3 из 21 находки, самые серьёзные первыми.")
        XCTAssertEqual(trimmed(3, 22), "Включено 3 из 22 находок, самые серьёзные первыми.")
        XCTAssertEqual(trimmed(3, 40), "Включено 3 из 40 находок, самые серьёзные первыми.")
        XCTAssertEqual(m.limitation(["code": "trimmed", "source": "projects", "shown": 1, "total": 1]),
                       "Включено 1 из 1 проекта; выберите проект для подробностей.")
        XCTAssertEqual(m.message("invalid-question"), "Вопрос должен быть от 1 до 6000 символов.")
    }

    func testAMalformedPluralFileIsRefusedNotGuessed() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("obs-l10n-\(UUID().uuidString)/ru.lproj")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir.deletingLastPathComponent()) }
        let bad = try PropertyListSerialization.data(fromPropertyList: ["x": ["NSStringLocalizedFormatKey": "%d"]], format: .xml, options: 0)
        try bad.write(to: dir.appendingPathComponent("Localizable.stringsdict"))
        XCTAssertThrowsError(try Catalog.load(dir))
        XCTAssertEqual(try Catalog.load(dir.deletingLastPathComponent().appendingPathComponent("absent.lproj")), Catalog())
    }
}
