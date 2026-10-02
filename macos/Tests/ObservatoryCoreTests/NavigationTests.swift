import XCTest
@testable import ObservatoryCore
final class NavigationTests: XCTestCase {
    func testLiveOriginKeepsItsOwnServerAndSendsTheRestOut() throws {
        let o = try XCTUnwrap(DashboardOrigin.live(URL(string: "http://127.0.0.1:47311/dashboard/index.html")!))
        XCTAssertEqual(o.decide(URL(string: "http://127.0.0.1:47311/dashboard/projects.html#project:alpha-web")!), .allow)
        XCTAssertEqual(o.decide(URL(string: "http://127.0.0.1:47311/health")!), .allow)
        XCTAssertEqual(o.decide(URL(string: "http://127.0.0.1:8787/jobs/1")!), .openExternally)      // another local service
        XCTAssertEqual(o.decide(URL(string: "https://example.com/")!), .openExternally)
        XCTAssertEqual(o.decide(URL(string: "mailto:ops@example.com")!), .openExternally)
        XCTAssertEqual(o.decide(URL(string: "someapp://open?x=1")!), .deny)          // no app is launched by a page
        XCTAssertEqual(o.decide(URL(string: "file:///etc/hosts")!), .deny)
        XCTAssertEqual(o.decide(URL(string: "javascript:void(0)")!), .deny)
        XCTAssertEqual(o.decide(URL(string: "about:blank")!), .allow)
    }
    func testOnlyALoopbackDashboardAddressIsLive() {
        XCTAssertNil(DashboardOrigin.live(URL(string: "http://example.com:47311/dashboard/index.html")!))
        XCTAssertNil(DashboardOrigin.live(URL(string: "https://127.0.0.1:47311/dashboard/index.html")!))
        XCTAssertNil(DashboardOrigin.live(URL(string: "http://user@127.0.0.1:47311/dashboard/index.html")!))
        XCTAssertNil(DashboardOrigin.live(URL(string: "http://127.0.0.1:47311/other.html")!))
    }
    func testFileOriginStaysInsideThePagesDirectory() throws {
        let o = try XCTUnwrap(DashboardOrigin.files("/srv/example-ws/docs/dashboard/index.html", workspace: "/srv/example-ws"))
        XCTAssertEqual(o.decide(URL(fileURLWithPath: "/srv/example-ws/docs/dashboard/findings.html")), .allow)
        XCTAssertEqual(o.decide(URL(fileURLWithPath: "/srv/example-ws/docs/dashboard/../../private/x")), .deny)
        XCTAssertEqual(o.decide(URL(fileURLWithPath: "/srv/example-ws/config/settings.json")), .deny)
        XCTAssertEqual(o.decide(URL(string: "https://example.com")!), .openExternally)
        XCTAssertNil(DashboardOrigin.files("/srv/example-ws/../other/docs/dashboard/index.html", workspace: "/srv/example-ws"))
        XCTAssertNil(DashboardOrigin.files("relative/index.html", workspace: "/srv/example-ws"))
    }
}
