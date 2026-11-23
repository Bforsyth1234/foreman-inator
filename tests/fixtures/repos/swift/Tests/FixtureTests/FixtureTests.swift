import XCTest
@testable import Fixture

final class FixtureTests: XCTestCase {
    func testSum() {
        XCTAssertEqual(sum(1, 2), 3)
    }
}
