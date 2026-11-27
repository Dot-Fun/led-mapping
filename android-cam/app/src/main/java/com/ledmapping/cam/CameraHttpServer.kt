package com.ledmapping.cam

import kotlinx.coroutines.*
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket

class CameraHttpServer(
    private val preferredPort: Int,
    private val controller: CameraController,
    private val infoProvider: () -> JSONObject,
    private val onHostDiscovered: (String) -> Unit = {}
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var serverSocket: ServerSocket? = null
    @Volatile
    private var running = false
    @Volatile
    var boundPort: Int = preferredPort
        private set

    fun start(): Int {
        val server = bindWithFallback()
        serverSocket = server
        boundPort = server.localPort
        running = true
        scope.launch {
            while (running) {
                try {
                    val client = server.accept()
                    launch { handleClient(client) }
                } catch (_: Throwable) {
                    if (!running) break
                }
            }
        }
        return boundPort
    }

    private fun bindWithFallback(): ServerSocket {
        for (p in preferredPort..(preferredPort + 10)) {
            try {
                return ServerSocket().apply {
                    reuseAddress = true
                    bind(InetSocketAddress(p))
                }
            } catch (_: Throwable) {
            }
        }
        return ServerSocket().apply {
            reuseAddress = true
            bind(InetSocketAddress(0))
        }
    }

    fun stop() {
        running = false
        try {
            serverSocket?.close()
        } catch (_: Throwable) {
        }
        scope.cancel()
    }

    private suspend fun handleClient(socket: Socket) {
        socket.use { sock ->
            sock.soTimeout = 10_000
            val input = BufferedInputStream(sock.getInputStream())
            val output = sock.getOutputStream()

            val headerBytes = readUntilHeadersEnd(input) ?: return
            val headerStr = String(headerBytes, Charsets.UTF_8)
            val lines = headerStr.split("\r\n")
            val requestLine = lines.firstOrNull() ?: return
            val parts = requestLine.split(" ")
            if (parts.size < 2) return
            val method = parts[0]
            val fullPath = parts[1]
            val path = fullPath.substringBefore("?")

            var contentLength = 0
            for (line in lines.drop(1)) {
                val idx = line.indexOf(':')
                if (idx > 0) {
                    val hName = line.substring(0, idx).trim()
                    val hVal = line.substring(idx + 1).trim()
                    if (hName.equals("Content-Length", ignoreCase = true)) {
                        contentLength = hVal.toIntOrNull() ?: 0
                    } else if (hName.equals("X-Studio-Host", ignoreCase = true) && hVal.startsWith("http")) {
                        onHostDiscovered(hVal)
                    }
                }
            }

            val bodyStr = if (contentLength > 0 && contentLength < 1_000_000) {
                val buf = ByteArray(contentLength)
                var readTotal = 0
                while (readTotal < contentLength) {
                    val n = input.read(buf, readTotal, contentLength - readTotal)
                    if (n <= 0) break
                    readTotal += n
                }
                String(buf, 0, readTotal, Charsets.UTF_8)
            } else {
                ""
            }

            if (method == "OPTIONS") {
                writeResponse(output, 204, "text/plain", ByteArray(0))
                return
            }

            when (path) {
                "/health" -> writeJson(output, 200, JSONObject().put("ok", true))
                "/info" -> writeJson(output, 200, infoProvider())
                "/config" -> {
                    val reqJson = if (bodyStr.isNotBlank()) JSONObject(bodyStr) else JSONObject()
                    val res = controller.applyConfigJson(reqJson)
                    writeJson(output, 200, res)
                }
                "/capture" -> {
                    try {
                        val frame = controller.captureFreshFrame()
                        val extraHeaders = mapOf(
                            "X-Capture-Timestamp-Ns" to frame.timestampNs.toString(),
                            "X-Frame-Width" to frame.width.toString(),
                            "X-Frame-Height" to frame.height.toString()
                        )
                        writeResponse(output, 200, "image/jpeg", frame.jpeg, extraHeaders)
                    } catch (t: Throwable) {
                        writeJson(output, 503, JSONObject().put("error", t.message ?: "Capture failed"))
                    }
                }
                "/preview" -> streamPreview(output)
                else -> writeJson(output, 404, JSONObject().put("error", "Not found: $path"))
            }
        }
    }

    private suspend fun streamPreview(output: OutputStream) {
        val boundary = "ledcamframe"
        val head = "HTTP/1.1 200 OK\r\n" +
            "Content-Type: multipart/x-mixed-replace; boundary=$boundary\r\n" +
            "Cache-Control: no-cache\r\n" +
            "Access-Control-Allow-Origin: *\r\n" +
            "Connection: close\r\n\r\n"
        output.write(head.toByteArray(Charsets.UTF_8))
        output.flush()

        while (running) {
            val jpg = controller.grabPreviewJpeg()
            if (jpg != null) {
                val partHead = "--$boundary\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpg.size}\r\n\r\n"
                try {
                    output.write(partHead.toByteArray(Charsets.UTF_8))
                    output.write(jpg)
                    output.write("\r\n".toByteArray(Charsets.UTF_8))
                    output.flush()
                } catch (_: Throwable) {
                    break
                }
            }
            delay(125)
        }
    }

    private fun readUntilHeadersEnd(input: BufferedInputStream): ByteArray? {
        val out = ByteArrayOutputStream()
        var matched = 0
        val target = byteArrayOf(13, 10, 13, 10) // \r\n\r\n
        while (out.size() < 65536) {
            val b = input.read()
            if (b == -1) return null
            out.write(b)
            if (b.toByte() == target[matched]) {
                matched++
                if (matched == 4) return out.toByteArray()
            } else {
                matched = if (b.toByte() == target[0]) 1 else 0
            }
        }
        return null
    }

    private fun writeJson(output: OutputStream, status: Int, json: JSONObject) {
        writeResponse(output, status, "application/json", json.toString().toByteArray(Charsets.UTF_8))
    }

    private fun writeResponse(
        output: OutputStream,
        status: Int,
        contentType: String,
        body: ByteArray,
        extraHeaders: Map<String, String> = emptyMap()
    ) {
        val sb = StringBuilder()
        sb.append("HTTP/1.1 ").append(status).append(" OK\r\n")
        sb.append("Content-Type: ").append(contentType).append("\r\n")
        sb.append("Access-Control-Allow-Origin: *\r\n")
        sb.append("Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n")
        sb.append("Access-Control-Allow-Headers: Content-Type\r\n")
        for ((k, v) in extraHeaders) {
            sb.append(k).append(": ").append(v).append("\r\n")
        }
        sb.append("Content-Length: ").append(body.size).append("\r\n")
        sb.append("Connection: close\r\n\r\n")
        output.write(sb.toString().toByteArray(Charsets.UTF_8))
        if (body.isNotEmpty()) {
            output.write(body)
        }
        output.flush()
    }
}
