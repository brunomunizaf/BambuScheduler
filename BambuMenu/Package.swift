// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "BambuTiming",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "BambuTiming",
            path: "Sources"
        ),
    ]
)
