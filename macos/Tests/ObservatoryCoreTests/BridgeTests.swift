import XCTest
@testable import ObservatoryCore
final class BridgeTests: XCTestCase {
    func fixture(_ body: String) throws -> (URL, Backend) {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: dir) }
        let file = dir.appendingPathComponent("fixture with spaces")
        try ("#!/bin/sh\n" + body).write(to: file, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: file.path)
        return (dir, Backend(executable: file.path, workspace: dir.path))
    }
    func testJSONStdinNoShellInterpolation() async throws {
        let (_, b) = try fixture("cat\n")
        let value = "$(touch NEVER) `quoted` ; \"test\"\nРусский"
        let data = try await b.call("ask", input: ["question": value])
        XCTAssertEqual((try JSONSerialization.jsonObject(with: data) as? [String: String])?["question"], value)
    }
    func testIncompatibleStatusRefused() async throws {
        let (_, b) = try fixture("echo '{\"protocol\":\"wrong\"}'\n")
        do { _ = try await b.call("status"); XCTFail("accepted incompatible protocol") }
        catch { XCTAssertEqual(error as? BridgeError, .invalidResponse) }
    }
    func testExitFailureCannotBeSuccess() async throws {
        let (_, b) = try fixture("echo '{}'; exit 1\n")
        do { _ = try await b.call("ask"); XCTFail("accepted nonzero exit") }
        catch { XCTAssertEqual(error as? BridgeError, .backend("backend-failed")) }
    }
    func testTypedErrorSurvives() async throws {
        let (_, b) = try fixture("echo '{\"error\":\"budget-reached\"}'; exit 1\n")
        do { _ = try await b.call("ask"); XCTFail() }
        catch { XCTAssertEqual(error as? BridgeError, .backend("budget-reached")) }
    }
    func testTimeoutBoundsChild() async throws {
        let (_, b) = try fixture("exec /bin/sleep 5\n")
        let start = Date()
        do { _ = try await b.call("ask", timeout: 0.1); XCTFail() }
        catch { XCTAssertEqual(error as? BridgeError, .timeout) }
        XCTAssertLessThan(Date().timeIntervalSince(start), 2)
    }
    func testOutputLimit() async throws {
        let (_, b) = try fixture("exec /usr/bin/yes x\n")
        do { _ = try await b.call("ask", timeout: 3); XCTFail() }
        catch { XCTAssertEqual(error as? BridgeError, .oversized) }
    }
    func testRelativeExecutableRefused() async throws {
        do { _ = try await Backend(executable: "echo", workspace: "/tmp").call("status"); XCTFail() }
        catch { XCTAssertEqual(error as? BridgeError, .configuration) }
    }
}
