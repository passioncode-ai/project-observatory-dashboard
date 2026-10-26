// The lifecycle broker's NSWorkspace helper (OBS-79 step L2, docs/design/LIFECYCLE.md).
//
// Launch Services only: no Apple Events, no accessibility, no UI scripting, no shell. Every answer
// is one line of JSON on stdout. The broker's macOS adapter (`macos.py`) is its only caller and
// passes nothing a request named: the app is an enrolled target's installed place, and the pid is
// the one the broker observed.
//
//   status <app>                 running or not, with its pid, visible windows, whether it is in
//                                front and the seconds since the person last touched input
//   open <app> [--background]    open it without activating it; --background only for a target the
//                                owner enrolled as understanding it
//   quit <app> <pid>             the app's own quit path, the one Cmd+Q and the Dock use
//   watch                        one line per launch, termination and activation, until stdin closes
//   --version                    which source it was built from, so the engine runs no older build
//
// Build: `swiftc -O -o lifecycle-native native_app.swift` (macos.build_helper does it for a source
// install; the release path is in docs/design/LIFECYCLE.md).
import AppKit
import Foundation

func emit(_ object: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
       let text = String(data: data, encoding: .utf8) { print(text) }
}

// Windows the person can see. The owner pid and the layer are readable without the Screen
// Recording permission (only titles need it), so nothing here asks the person for anything.
func visibleWindows(_ pid: pid_t) -> Int {
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
        as? [[String: Any]] else { return 0 }
    return list.filter {
        ($0[kCGWindowOwnerPID as String] as? Int).map { pid_t($0) } == pid
            && ($0[kCGWindowLayer as String] as? Int) == 0
    }.count
}

// Seconds since the person last touched keyboard, mouse or trackpad, system-wide, with no
// permission needed. A frontmost app with recent input has a person in front of it.
func inputIdleSeconds() -> Double {
    CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: ~0)!)
}

// The version the engine compares before it runs this helper (OBS-222): the first 16 hex digits of the SHA-256 of
// this file with the value below left empty (`macos.source_version`). Any other change to this file changes it, and
// the engine's tests fail until the value below is the new one. A build that answers another value, or
// `invalid_argument` because it predates `--version`, is `setup_required` until `full lifecycle build-helper`.
let helperVersion = "2c5b06921f853259"

let args = CommandLine.arguments

// `--version`: answers at once and touches nothing.
if args.count == 2 && args[1] == "--version" {
    emit(["state": "version", "version": helperVersion]); exit(0)
}

// `watch`: one line of JSON per app launch, termination or activation, until stdin closes (the
// broker exited), so the helper never outlives its reader.
if args.count == 2 && args[1] == "watch" {
    setvbuf(stdout, nil, _IOLBF, 0)
    let center = NSWorkspace.shared.notificationCenter
    let names: [(NSNotification.Name, String)] = [
        (NSWorkspace.didLaunchApplicationNotification, "launched"),
        (NSWorkspace.didTerminateApplicationNotification, "terminated"),
        (NSWorkspace.didActivateApplicationNotification, "activated"),
    ]
    for (name, event) in names {
        center.addObserver(forName: name, object: nil, queue: .main) { note in
            guard let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                  let bundleId = app.bundleIdentifier else { return }
            emit(["event": event, "bundleId": bundleId, "pid": app.processIdentifier])
        }
    }
    Thread {
        while readLine() != nil {}
        exit(0)
    }.start()
    RunLoop.main.run()
}

// `quit <app> <pid>`: the app's own quit path, so unsaved work is handled the way the app handles
// it. No Apple Events permission is involved, and an app that ignores SIGTERM with its window
// closed still quits through here.
if args.count == 4 && args[1] == "quit" {
    let target = URL(fileURLWithPath: args[2]).standardizedFileURL
    guard let pid = pid_t(args[3]), let app = NSRunningApplication(processIdentifier: pid),
          app.bundleURL?.standardizedFileURL == target else {
        emit(["state": "identity_mismatch"]); exit(2)
    }
    emit(["state": app.terminate() ? "quit_requested" : "quit_refused", "pid": pid]); exit(0)
}

// `open <app> --background`: the one fixed argument a product may accept to start with no window
// and no focus until the person activates it. Nothing else is ever passed through.
let background = args.count == 4 && args[1] == "open" && args[3] == "--background"
if !(args.count == 3 || background) || !["status", "open"].contains(args[1]) {
    emit(["state": "invalid_argument"]); exit(2)
}
let url = URL(fileURLWithPath: args[2]).standardizedFileURL
let homeApplications = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications").path + "/"
if !(url.path.hasPrefix("/Applications/") || url.path.hasPrefix(homeApplications)) || url.pathExtension != "app" {
    emit(["state": "not_installed"]); exit(2)
}
guard let bundle = Bundle(url: url), let bundleId = bundle.bundleIdentifier else {
    emit(["state": "not_installed"]); exit(2)
}
let running = NSRunningApplication.runningApplications(withBundleIdentifier: bundleId)
if let app = running.first(where: { $0.bundleURL?.standardizedFileURL == url }) {
    emit([
        "state": "ready", "pid": app.processIdentifier, "bundleId": bundleId,
        "windows": visibleWindows(app.processIdentifier), "active": app.isActive, "hidden": app.isHidden,
        "inputIdleSeconds": Int(inputIdleSeconds()),
    ]); exit(0)
}
// The same bundle id running from another place is not the enrolled app.
if !running.isEmpty { emit(["state": "identity_mismatch"]); exit(2) }
if args[1] == "status" { emit(["state": "stopped", "bundleId": bundleId]); exit(0) }
let configuration = NSWorkspace.OpenConfiguration()
configuration.activates = false
configuration.createsNewApplicationInstance = false
configuration.addsToRecentItems = false
if background { configuration.arguments = ["--background"] }
var done = false
NSWorkspace.shared.openApplication(at: url, configuration: configuration) { app, _ in
    if let app = app {
        emit(["state": "accepted_by_os", "pid": app.processIdentifier, "bundleId": bundleId])
    } else { emit(["state": "delivery_unknown"]) }
    done = true
}
let deadline = Date().addingTimeInterval(10)
while !done && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
if !done { emit(["state": "delivery_unknown"]); exit(2) }
