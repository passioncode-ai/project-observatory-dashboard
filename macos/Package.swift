// swift-tools-version: 5.9
import PackageDescription
// English is the source language (L10N-02); ObservatoryCore carries the Russian
// dictionary and both languages' plural forms in Resources/<lang>.lproj.
let package = Package(name: "ProjectObservatory", defaultLocalization: "en", platforms: [.macOS(.v14)], products: [
    .executable(name: "ProjectObservatory", targets: ["ObservatoryApp"])
], targets: [
    .target(name: "ObservatoryCore", resources: [.process("Resources")]),
    .executableTarget(name: "ObservatoryApp", dependencies: ["ObservatoryCore"]),
    .testTarget(name: "ObservatoryCoreTests", dependencies: ["ObservatoryCore", "ObservatoryApp"])
])
