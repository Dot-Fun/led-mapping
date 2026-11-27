// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "macos-cam",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "macos-cam",
            path: "Sources"
        )
    ]
)
