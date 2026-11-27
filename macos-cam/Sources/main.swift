import Foundation
import AVFoundation
import CoreImage
import AppKit
import Network

// MARK: - Camera Device Manager

final class CameraStream: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let device: AVCaptureDevice
    let session = AVCaptureSession()
    private let output = AVCaptureVideoDataOutput()
    private let captureQueue: DispatchQueue
    private let lock = NSLock()
    private let ciContext = CIContext(options: [.useSoftwareRenderer: false])

    private var latestJPEG: Data?
    private var latestTimestampNs: UInt64 = 0
    private var latestWidth: Int = 0
    private var latestHeight: Int = 0
    private var frameSeq: UInt64 = 0
    private var waiters: [(minSeq: UInt64, sem: DispatchSemaphore)] = []
    private(set) var configLockedByOtherApp: Bool = false
    private(set) var lastError: String?
    private(set) var jpegQuality: Double = 0.85

    init(device: AVCaptureDevice) throws {
        self.device = device
        self.captureQueue = DispatchQueue(label: "cam.\(device.uniqueID)", qos: .userInteractive)
        super.init()

        // Probe whether another app (e.g., EMEET STUDIO) holds lockForConfiguration
        do {
            try device.lockForConfiguration()
            device.unlockForConfiguration()
            self.configLockedByOtherApp = false
        } catch {
            self.configLockedByOtherApp = true
            self.lastError = error.localizedDescription
        }

        let input = try AVCaptureDeviceInput(device: device)
        session.beginConfiguration()
        if session.canAddInput(input) {
            session.addInput(input)
        }
        output.alwaysDiscardsLateVideoFrames = true
        output.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
        ]
        output.setSampleBufferDelegate(self, queue: captureQueue)
        if session.canAddOutput(output) {
            session.addOutput(output)
        }
        session.commitConfiguration()
        session.startRunning()
    }

    func stop() {
        session.stopRunning()
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        guard let pb = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let ts = DispatchTime.now().uptimeNanoseconds
        let width = CVPixelBufferGetWidth(pb)
        let height = CVPixelBufferGetHeight(pb)

        let ci = CIImage(cvImageBuffer: pb)
        guard let cg = ciContext.createCGImage(ci, from: ci.extent) else { return }
        let rep = NSBitmapImageRep(cgImage: cg)
        guard let data = rep.representation(
            using: .jpeg,
            properties: [.compressionFactor: NSNumber(value: jpegQuality)]
        ) else { return }

        lock.lock()
        latestJPEG = data
        latestTimestampNs = ts
        latestWidth = width
        latestHeight = height
        frameSeq += 1
        let curSeq = frameSeq
        var remaining: [(minSeq: UInt64, sem: DispatchSemaphore)] = []
        for w in waiters {
            if curSeq >= w.minSeq {
                w.sem.signal()
            } else {
                remaining.append(w)
            }
        }
        waiters = remaining
        lock.unlock()
    }

    func grabFreshFrame(timeoutSec: Double = 3.0) -> (Data, UInt64, Int, Int)? {
        lock.lock()
        // Wait for at least 1 NEW frame arriving strictly after this call
        let targetSeq = frameSeq + 1
        let sem = DispatchSemaphore(value: 0)
        waiters.append((minSeq: targetSeq, sem: sem))
        lock.unlock()

        _ = sem.wait(timeout: .now() + timeoutSec)

        lock.lock()
        defer { lock.unlock() }
        guard let data = latestJPEG else { return nil }
        return (data, latestTimestampNs, latestWidth, latestHeight)
    }

    func grabLatestFrame() -> (Data, UInt64, Int, Int)? {
        lock.lock()
        defer { lock.unlock() }
        guard let data = latestJPEG else { return nil }
        return (data, latestTimestampNs, latestWidth, latestHeight)
    }

    func applyConfig(_ body: [String: Any]) -> [String: Any] {
        if let q = body["jpeg_quality"] as? Double {
            jpegQuality = max(0.3, min(0.98, q))
        }
        var applied = false
        var msg = "ok"
        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }
            self.configLockedByOtherApp = false

            if let aeLock = body["ae_lock"] as? Bool {
                if aeLock && device.isExposureModeSupported(.locked) {
                    device.exposureMode = .locked
                } else if !aeLock && device.isExposureModeSupported(.continuousAutoExposure) {
                    device.exposureMode = .continuousAutoExposure
                }
            }
            if let wbLock = body["awb_lock"] as? Bool {
                if wbLock && device.isWhiteBalanceModeSupported(.locked) {
                    device.whiteBalanceMode = .locked
                } else if !wbLock && device.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) {
                    device.whiteBalanceMode = .continuousAutoWhiteBalance
                }
            }
            if let afLock = body["af_lock"] as? Bool {
                if afLock && device.isFocusModeSupported(.locked) {
                    device.focusMode = .locked
                } else if !afLock && device.isFocusModeSupported(.continuousAutoFocus) {
                    device.focusMode = .continuousAutoFocus
                }
            }
            applied = true
        } catch {
            self.configLockedByOtherApp = true
            self.lastError = error.localizedDescription
            msg = "Device configuration locked by another app (e.g. EMEET STUDIO): \(error.localizedDescription)"
        }
        return [
            "ok": true,
            "applied_hardware_config": applied,
            "config_locked_by_other_app": configLockedByOtherApp,
            "message": msg
        ]
    }

    func infoDict() -> [String: Any] {
        lock.lock()
        let w = latestWidth > 0 ? latestWidth : Int(CMVideoFormatDescriptionGetDimensions(device.activeFormat.formatDescription).width)
        let h = latestHeight > 0 ? latestHeight : Int(CMVideoFormatDescriptionGetDimensions(device.activeFormat.formatDescription).height)
        lock.unlock()

        // Reasonable ~68 deg horizontal FOV default for EMEET PIXY / webcams (refined by ChArUco or BA)
        let fx = Double(w) / (2.0 * tan(68.0 * .pi / 360.0))
        return [
            "id": "macos:\(device.uniqueID)",
            "unique_id": device.uniqueID,
            "name": device.localizedName,
            "model": device.localizedName,
            "platform": "macos",
            "width": w,
            "height": h,
            "config_locked_by_other_app": configLockedByOtherApp,
            "supports_manual_exposure": !configLockedByOtherApp && device.isExposureModeSupported(.locked),
            "intrinsics": [
                "fx": fx,
                "fy": fx,
                "cx": Double(w) / 2.0,
                "cy": Double(h) / 2.0,
                "dist": [0.0, 0.0, 0.0, 0.0, 0.0],
                "source": "estimated_fov"
            ]
        ]
    }
}

final class CameraHub {
    private let lock = NSLock()
    private var streams: [String: CameraStream] = [:]

    func discoverDevices() -> [AVCaptureDevice] {
        var types: [AVCaptureDevice.DeviceType] = [.builtInWideAngleCamera]
        if #available(macOS 14.0, *) {
            types.append(.external)
        } else {
            types.append(.externalUnknown)
        }
        let discovery = AVCaptureDevice.DiscoverySession(
            deviceTypes: types,
            mediaType: .video,
            position: .unspecified
        )
        // Prefer external cameras (like EMEET PIXY) first
        return discovery.devices.sorted { a, b in
            let aExt = !a.localizedName.contains("MacBook")
            let bExt = !b.localizedName.contains("MacBook")
            if aExt != bExt { return aExt }
            return a.localizedName < b.localizedName
        }
    }

    func listDevicesJSON() -> [[String: Any]] {
        discoverDevices().map { d in
            let dims = CMVideoFormatDescriptionGetDimensions(d.activeFormat.formatDescription)
            lock.lock()
            let active = streams[d.uniqueID]
            lock.unlock()
            if let s = active {
                return s.infoDict()
            }
            var locked = false
            do {
                try d.lockForConfiguration()
                d.unlockForConfiguration()
            } catch {
                locked = true
            }
            let w = Int(dims.width)
            let h = Int(dims.height)
            let fx = Double(w) / (2.0 * tan(68.0 * .pi / 360.0))
            return [
                "id": "macos:\(d.uniqueID)",
                "unique_id": d.uniqueID,
                "name": d.localizedName,
                "model": d.localizedName,
                "platform": "macos",
                "width": w,
                "height": h,
                "config_locked_by_other_app": locked,
                "supports_manual_exposure": !locked && d.isExposureModeSupported(.locked),
                "intrinsics": [
                    "fx": fx,
                    "fy": fx,
                    "cx": Double(w) / 2.0,
                    "cy": Double(h) / 2.0,
                    "dist": [0.0, 0.0, 0.0, 0.0, 0.0],
                    "source": "estimated_fov"
                ]
            ]
        }
    }

    func getOrOpenStream(uniqueId: String?) throws -> CameraStream {
        let devices = discoverDevices()
        let target: AVCaptureDevice
        if let uid = uniqueId, !uid.isEmpty {
            let clean = uid.replacingOccurrences(of: "macos:", with: "")
            guard let found = devices.first(where: { $0.uniqueID == clean || $0.localizedName == clean }) else {
                throw NSError(domain: "macos-cam", code: 404, userInfo: [NSLocalizedDescriptionKey: "Device not found: \(uid)"])
            }
            target = found
        } else {
            guard let first = devices.first else {
                throw NSError(domain: "macos-cam", code: 404, userInfo: [NSLocalizedDescriptionKey: "No cameras found"])
            }
            target = first
        }

        lock.lock()
        if let existing = streams[target.uniqueID] {
            lock.unlock()
            return existing
        }
        lock.unlock()

        let stream = try CameraStream(device: target)
        lock.lock()
        streams[target.uniqueID] = stream
        lock.unlock()
        // Allow 1 initial frame to warm up
        _ = stream.grabFreshFrame(timeoutSec: 2.5)
        return stream
    }
}

// MARK: - Minimal HTTP Server over Network.framework

final class HTTPServer {
    let port: UInt16
    let hub: CameraHub
    private var listener: NWListener?
    private let queue = DispatchQueue(label: "http.server", attributes: .concurrent)

    init(port: UInt16, hub: CameraHub) {
        self.port = port
        self.hub = hub
    }

    func start() throws {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        let nwPort = NWEndpoint.Port(rawValue: port)!
        listener = try NWListener(using: params, on: nwPort)
        listener?.stateUpdateHandler = { state in
            if case .ready = state {
                print("macos-cam listening on http://127.0.0.1:\(self.port)")
                fflush(stdout)
            } else if case .failed(let err) = state {
                fputs("Listener failed: \(err)\n", stderr)
                exit(1)
            }
        }
        listener?.newConnectionHandler = { [weak self] conn in
            self?.handleConnection(conn)
        }
        listener?.start(queue: queue)
    }

    private func handleConnection(_ conn: NWConnection) {
        conn.start(queue: queue)
        conn.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, _, error in
            guard let self = self, let data = data, error == nil else {
                conn.cancel()
                return
            }
            self.processRequest(data: data, conn: conn)
        }
    }

    private func processRequest(data: Data, conn: NWConnection) {
        guard let raw = String(data: data, encoding: .utf8) else {
            sendJSON(conn, status: 400, object: ["error": "Invalid UTF-8"])
            return
        }
        let parts = raw.components(separatedBy: "\r\n\r\n")
        let headerLines = parts.first?.components(separatedBy: "\r\n") ?? []
        guard let reqLine = headerLines.first else {
            conn.cancel()
            return
        }
        let tokens = reqLine.split(separator: " ")
        guard tokens.count >= 2 else {
            conn.cancel()
            return
        }
        let method = String(tokens[0])
        let fullPath = String(tokens[1])
        let bodyStr = parts.count > 1 ? parts.dropFirst().joined(separator: "\r\n\r\n") : ""

        let urlComps = URLComponents(string: "http://localhost" + fullPath)
        let path = urlComps?.path ?? fullPath
        var query: [String: String] = [:]
        for item in urlComps?.queryItems ?? [] {
            query[item.name] = item.value ?? ""
        }

        if method == "OPTIONS" {
            sendEmpty(conn, status: 204)
            return
        }

        switch path {
        case "/health":
            sendJSON(conn, status: 200, object: ["ok": true])

        case "/devices":
            sendJSON(conn, status: 200, object: ["devices": hub.listDevicesJSON()])

        case "/info":
            do {
                let stream = try hub.getOrOpenStream(uniqueId: query["device"])
                sendJSON(conn, status: 200, object: stream.infoDict())
            } catch {
                sendJSON(conn, status: 404, object: ["error": error.localizedDescription])
            }

        case "/config":
            do {
                let stream = try hub.getOrOpenStream(uniqueId: query["device"])
                var bodyDict: [String: Any] = [:]
                if let bData = bodyStr.data(using: .utf8),
                   let parsed = try? JSONSerialization.jsonObject(with: bData) as? [String: Any] {
                    bodyDict = parsed
                }
                let result = stream.applyConfig(bodyDict)
                sendJSON(conn, status: 200, object: result)
            } catch {
                sendJSON(conn, status: 500, object: ["error": error.localizedDescription])
            }

        case "/capture":
            do {
                let stream = try hub.getOrOpenStream(uniqueId: query["device"])
                let fresh = query["fresh"] != "0"
                let frame = fresh ? stream.grabFreshFrame(timeoutSec: 3.0) : stream.grabLatestFrame()
                guard let (jpeg, ts, w, h) = frame else {
                    sendJSON(conn, status: 503, object: ["error": "No frame available"])
                    return
                }
                sendJPEG(conn, data: jpeg, timestampNs: ts, width: w, height: h)
            } catch {
                sendJSON(conn, status: 500, object: ["error": error.localizedDescription])
            }

        case "/preview":
            do {
                let stream = try hub.getOrOpenStream(uniqueId: query["device"])
                streamMJPEG(conn, stream: stream)
            } catch {
                sendJSON(conn, status: 500, object: ["error": error.localizedDescription])
            }

        default:
            sendJSON(conn, status: 404, object: ["error": "Not found: \(path)"])
        }
    }

    private func sendEmpty(_ conn: NWConnection, status: Int) {
        let header = "HTTP/1.1 \(status) OK\r\nAccess-Control-Allow-Origin: *\r\nAccess-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        conn.send(content: header.data(using: .utf8), completion: .contentProcessed { _ in conn.cancel() })
    }

    private func sendJSON(_ conn: NWConnection, status: Int, object: Any) {
        let body = (try? JSONSerialization.data(withJSONObject: object, options: [])) ?? Data("{}".utf8)
        let header = "HTTP/1.1 \(status) OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n"
        var payload = Data(header.utf8)
        payload.append(body)
        conn.send(content: payload, completion: .contentProcessed { _ in conn.cancel() })
    }

    private func sendJPEG(_ conn: NWConnection, data: Data, timestampNs: UInt64, width: Int, height: Int) {
        let header = "HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nAccess-Control-Allow-Origin: *\r\nX-Capture-Timestamp-Ns: \(timestampNs)\r\nX-Frame-Width: \(width)\r\nX-Frame-Height: \(height)\r\nContent-Length: \(data.count)\r\nConnection: close\r\n\r\n"
        var payload = Data(header.utf8)
        payload.append(data)
        conn.send(content: payload, completion: .contentProcessed { _ in conn.cancel() })
    }

    private func streamMJPEG(_ conn: NWConnection, stream: CameraStream) {
        let boundary = "ledcamframe"
        let header = "HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=\(boundary)\r\nCache-Control: no-cache\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n"
        conn.send(content: header.data(using: .utf8), completion: .contentProcessed { err in
            if err != nil {
                conn.cancel()
                return
            }
            self.sendNextMJPEGFrame(conn: conn, stream: stream, boundary: boundary)
        })
    }

    private func sendNextMJPEGFrame(conn: NWConnection, stream: CameraStream, boundary: String) {
        if case .cancelled = conn.state { return }
        if case .failed = conn.state { return }
        if let (jpeg, _, _, _) = stream.grabLatestFrame() {
            let partHeader = "--\(boundary)\r\nContent-Type: image/jpeg\r\nContent-Length: \(jpeg.count)\r\n\r\n"
            var chunk = Data(partHeader.utf8)
            chunk.append(jpeg)
            chunk.append(Data("\r\n".utf8))
            conn.send(content: chunk, completion: .contentProcessed { err in
                if err != nil {
                    conn.cancel()
                    return
                }
                self.queue.asyncAfter(deadline: .now() + 0.12) {
                    self.sendNextMJPEGFrame(conn: conn, stream: stream, boundary: boundary)
                }
            })
        } else {
            queue.asyncAfter(deadline: .now() + 0.15) {
                self.sendNextMJPEGFrame(conn: conn, stream: stream, boundary: boundary)
            }
        }
    }
}

// MARK: - UDP LAN Discovery Beacon (so any MacBook on the network auto-discovers!)

final class MacOSDiscoveryBeacon {
    private let port: UInt16
    private let beaconPort: UInt16
    private let hub: CameraHub
    private var timer: DispatchSourceTimer?

    init(httpPort: UInt16, beaconPort: UInt16 = 8089, hub: CameraHub) {
        self.port = httpPort
        self.beaconPort = beaconPort
        self.hub = hub
    }

    func start() {
        let q = DispatchQueue(label: "macos.beacon")
        let t = DispatchSource.makeTimerSource(queue: q)
        t.schedule(deadline: .now() + 1.0, repeating: 2.5)
        t.setEventHandler { [weak self] in self?.broadcastOnce() }
        t.resume()
        self.timer = t
    }

    private func localIPv4() -> String? {
        var ifaddr: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ifaddr) == 0, let first = ifaddr else { return nil }
        defer { freeifaddrs(ifaddr) }
        var ptr: UnsafeMutablePointer<ifaddrs>? = first
        while let cur = ptr {
            let flags = Int32(cur.pointee.ifa_flags)
            if (flags & (IFF_UP | IFF_RUNNING | IFF_LOOPBACK)) == (IFF_UP | IFF_RUNNING),
               let sa = cur.pointee.ifa_addr, sa.pointee.sa_family == UInt8(AF_INET) {
                var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
                if getnameinfo(sa, socklen_t(sa.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 {
                    let ip = String(cString: host)
                    if !ip.hasPrefix("169.254.") && !ip.hasPrefix("127.") {
                        return ip
                    }
                }
            }
            ptr = cur.pointee.ifa_next
        }
        return nil
    }

    private func broadcastOnce() {
        guard let ip = localIPv4() else { return }
        let hostName = Host.current().localizedName ?? ProcessInfo.processInfo.hostName
        let payload: [String: Any] = [
            "type": "ledcam_macos_host",
            "host_name": hostName,
            "ip": ip,
            "port": Int(port),
            "platform": "macos",
            "device_count": hub.discoverDevices().count
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: []) else { return }

        let fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        guard fd >= 0 else { return }
        defer { close(fd) }
        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_BROADCAST, &yes, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = beaconPort.bigEndian
        addr.sin_addr.s_addr = inet_addr("255.255.255.255")

        data.withUnsafeBytes { rawBuf in
            guard let base = rawBuf.baseAddress else { return }
            withUnsafePointer(to: &addr) { aPtr in
                aPtr.withMemoryRebound(to: sockaddr.self, capacity: 1) { saPtr in
                    _ = sendto(fd, base, data.count, 0, saPtr, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
    }
}

// MARK: - Main

var port: UInt16 = 8781
if let idx = CommandLine.arguments.firstIndex(of: "--port"), idx + 1 < CommandLine.arguments.count,
   let parsed = UInt16(CommandLine.arguments[idx + 1]) {
    port = parsed
}

let hub = CameraHub()
let server = HTTPServer(port: port, hub: hub)
let beacon = MacOSDiscoveryBeacon(httpPort: port, hub: hub)
do {
    try server.start()
    beacon.start()
} catch {
    fputs("Failed to start macos-cam: \(error)\n", stderr)
    exit(1)
}
RunLoop.main.run()
