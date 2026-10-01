// swift-tools-version: 5.9
import PackageDescription
let package = Package(name: "ProjectObservatory", platforms: [.macOS(.v14)], products: [
    .executable(name: "ProjectObservatory", targets: ["ObservatoryApp"])
], targets: [
    .target(name: "ObservatoryCore"),
    .executableTarget(name: "ObservatoryApp", dependencies: ["ObservatoryCore"]),
    .testTarget(name: "ObservatoryCoreTests", dependencies: ["ObservatoryCore", "ObservatoryApp"])
])
