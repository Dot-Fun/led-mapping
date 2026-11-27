package com.ledmapping.cam

import android.Manifest
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.TextureView
import android.view.WindowManager
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.NetworkInterface
import java.net.URL

class MainActivity : AppCompatActivity() {

    private lateinit var texturePreview: TextureView
    private lateinit var txtEndpoint: TextView
    private lateinit var txtStatus: TextView
    private lateinit var txtRemote: TextView
    private lateinit var btnScan: Button
    private lateinit var btnHeatmap: Button
    private lateinit var btnUndo: Button
    private lateinit var btnFx: Button
    private lateinit var btnLedPreset: Button
    private lateinit var btnAuto: Button
    private lateinit var btnDim: Button
    private lateinit var btnUpdate: Button

    private val uiScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    @Volatile
    private var studioHostUrl: String? = null

    private var cameraController: CameraController? = null
    private var httpServer: CameraHttpServer? = null
    private var beacon: DiscoveryBeacon? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var dimmed = false

    private val preferredHttpPort = 8788
    private var activeHttpPort = 8788

    private val deviceId: String by lazy {
        val aid = Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID) ?: "unknown"
        "android:${Build.MODEL.replace(" ", "_")}-${aid.take(6)}"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(R.layout.activity_main)

        texturePreview = findViewById(R.id.texturePreview)
        txtEndpoint = findViewById(R.id.txtEndpoint)
        txtStatus = findViewById(R.id.txtStatus)
        txtRemote = findViewById(R.id.txtRemote)
        btnScan = findViewById(R.id.btnScan)
        btnHeatmap = findViewById(R.id.btnHeatmap)
        btnUndo = findViewById(R.id.btnUndo)
        btnFx = findViewById(R.id.btnFx)
        btnLedPreset = findViewById(R.id.btnLedPreset)
        btnAuto = findViewById(R.id.btnAuto)
        btnDim = findViewById(R.id.btnDim)
        btnUpdate = findViewById(R.id.btnUpdate)

        btnScan.setOnClickListener { triggerRemoteScan() }
        btnHeatmap.setOnClickListener { triggerRemoteAction("/api/map/paint-coverage", null, "Painted Accuracy Heatmap (Green/Yellow/Red)") }
        btnUndo.setOnClickListener { triggerRemoteAction("/api/rounds/undo-last", null, "Undid last round") }
        btnFx.setOnClickListener { triggerRemoteAction("/api/effects/toggle", null, "Toggled 3D FX") }
        btnLedPreset.setOnClickListener { cameraController?.applyLedScanPreset() }
        btnAuto.setOnClickListener { cameraController?.applyAutoPreset() }
        btnDim.setOnClickListener { toggleDim() }
        btnUpdate.setOnClickListener { downloadAndInstallUpdate() }

        acquireLocks()

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.CAMERA), 1001)
        } else {
            startServices()
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 1001 && grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            startServices()
        } else {
            txtStatus.text = "Camera permission required!"
        }
    }

    private fun startServices() {
        val controller = CameraController(this, texturePreview) { msg ->
            runOnUiThread { txtStatus.text = msg }
        }
        cameraController = controller
        controller.start()

        val server = CameraHttpServer(
            preferredHttpPort,
            controller,
            infoProvider = { controller.buildInfoJson(deviceId, getLocalIp(), activeHttpPort, getBatteryPct()) },
            onHostDiscovered = { hostUrl ->
                if (studioHostUrl != hostUrl) {
                    studioHostUrl = hostUrl
                    runOnUiThread { txtRemote.text = "Connected to Studio: $hostUrl" }
                    checkForUpdate(hostUrl)
                }
            }
        )
        httpServer = server
        activeHttpPort = server.start()

        val safeName = "ledcam-${Build.MODEL.replace(Regex("[^A-Za-z0-9-]"), "").take(16)}"
        val disc = DiscoveryBeacon(this, safeName, activeHttpPort) {
            JSONObject().apply {
                put("type", "ledcam_beacon")
                put("id", deviceId)
                put("name", "${Build.MANUFACTURER} ${Build.MODEL}".trim())
                put("model", Build.MODEL)
                put("ip", getLocalIp())
                put("port", activeHttpPort)
                put("hardware_level", controller.hardwareLevelName)
                put("battery", getBatteryPct())
                put("api_level", Build.VERSION.SDK_INT)
            }
        }
        beacon = disc
        disc.start()

        val ip = getLocalIp()
        txtEndpoint.text = "http://$ip:$activeHttpPort  ($deviceId)"
    }

    private fun toggleDim() {
        dimmed = !dimmed
        val lp = window.attributes
        lp.screenBrightness = if (dimmed) 0.02f else WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE
        window.attributes = lp
        btnDim.text = if (dimmed) "Bright" else "Dim"
    }

    private fun acquireLocks() {
        try {
            val pm = getSystemService(POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "LEDMapCam::ScanLock").apply {
                acquire(6 * 60 * 60 * 1000L) // up to 6 hours
            }
            val wm = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
            @Suppress("DEPRECATION")
            wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "LEDMapCam::WifiLock").apply {
                acquire()
            }
        } catch (_: Throwable) {
        }
    }

    private fun getLocalIp(): String {
        try {
            val ifaces = NetworkInterface.getNetworkInterfaces()
            while (ifaces.hasMoreElements()) {
                val iface = ifaces.nextElement()
                if (!iface.isUp || iface.isLoopback) continue
                val addrs = iface.inetAddresses
                while (addrs.hasMoreElements()) {
                    val addr = addrs.nextElement()
                    if (addr is Inet4Address && !addr.isLoopbackAddress) {
                        return addr.hostAddress ?: continue
                    }
                }
            }
        } catch (_: Throwable) {
        }
        return "127.0.0.1"
    }

    private fun getBatteryPct(): Int {
        val intent = registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED)) ?: return -1
        val level = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, 100)
        return if (level >= 0 && scale > 0) (level * 100) / scale else -1
    }

    private fun triggerRemoteScan() {
        val host = studioHostUrl
        if (host == null) {
            txtRemote.text = "Not linked to Studio yet — wait 2s or tap Refresh Fleet"
            return
        }
        btnScan.isEnabled = false
        txtRemote.text = "⚡ Locking AE & scanning all cameras..."
        uiScope.launch {
            try {
                // Ensure all cameras are locked for LEDs, then run a fast Ternary ECC scan!
                postJson("$host/api/cameras/lock-all", "{}")
                val body = JSONObject().apply {
                    put("mode", "ternary_ecc")
                    put("brightness", 165)
                    put("settle_ms", 140)
                }.toString()
                val resp = postJson("$host/api/scan/start", body, timeoutMs = 45_000)
                val summary = resp.optString("summary_text", "Scan complete!")
                // Immediately paint 3D accuracy heatmap so you see Green/Yellow/Red on the lights!
                try {
                    postJson("$host/api/map/paint-coverage", "{}")
                } catch (_: Throwable) {
                }
                runOnUiThread {
                    txtRemote.text = "✓ $summary"
                    btnScan.isEnabled = true
                }
            } catch (t: Throwable) {
                runOnUiThread {
                    txtRemote.text = "Scan error: ${t.message}"
                    btnScan.isEnabled = true
                }
            }
        }
    }

    private fun triggerRemoteAction(path: String, jsonBody: String?, fallbackMsg: String) {
        val host = studioHostUrl
        if (host == null) {
            txtRemote.text = "Not linked to Studio yet"
            return
        }
        txtRemote.text = "Sending $path..."
        uiScope.launch {
            try {
                val resp = postJson("$host$path", jsonBody ?: "{}")
                val msg = resp.optString("summary_text", fallbackMsg)
                runOnUiThread { txtRemote.text = "✓ $msg" }
            } catch (t: Throwable) {
                runOnUiThread { txtRemote.text = "Error: ${t.message}" }
            }
        }
    }

    private fun postJson(urlStr: String, body: String, timeoutMs: Int = 10_000): JSONObject {
        val conn = (URL(urlStr).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = 5000
            readTimeout = timeoutMs
            doOutput = true
            setRequestProperty("Content-Type", "application/json")
        }
        conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
        val code = conn.responseCode
        val stream = if (code in 200..299) conn.inputStream else conn.errorStream
        val text = stream?.bufferedReader()?.readText() ?: "{}"
        conn.disconnect()
        if (code !in 200..299) {
            val detail = try { JSONObject(text).optString("detail", text) } catch (_: Throwable) { text }
            throw RuntimeException("HTTP $code: $detail")
        }
        return try { JSONObject(text) } catch (_: Throwable) { JSONObject() }
    }

    private fun checkForUpdate(host: String) {
        uiScope.launch {
            try {
                val conn = (URL("$host/api/apk-version").openConnection() as HttpURLConnection).apply {
                    connectTimeout = 3000
                    readTimeout = 3000
                }
                val text = conn.inputStream.bufferedReader().readText()
                conn.disconnect()
                val obj = JSONObject(text)
                val serverBuild = obj.optString("build_id", "")
                val prefs = getSharedPreferences("ledmap_update", MODE_PRIVATE)
                val lastInstalled = prefs.getString("last_build_id", "") ?: ""
                if (serverBuild.isNotEmpty() && lastInstalled.isNotEmpty() && serverBuild != lastInstalled) {
                    runOnUiThread {
                        btnUpdate.text = "⬆ Update!"
                        btnUpdate.setBackgroundColor(0xFFD97706.toInt())
                    }
                } else if (serverBuild.isNotEmpty() && lastInstalled.isEmpty()) {
                    prefs.edit().putString("last_build_id", serverBuild).apply()
                }
            } catch (_: Throwable) {
            }
        }
    }

    private fun downloadAndInstallUpdate() {
        val host = studioHostUrl
        if (host == null) {
            txtRemote.text = "Not linked to Studio yet — wait 2s"
            return
        }
        btnUpdate.isEnabled = false
        txtRemote.text = "Downloading latest APK over Wi-Fi..."
        uiScope.launch {
            try {
                // Record server build_id before launching installer
                try {
                    val vConn = URL("$host/api/apk-version").openConnection() as HttpURLConnection
                    val vObj = JSONObject(vConn.inputStream.bufferedReader().readText())
                    vConn.disconnect()
                    val bid = vObj.optString("build_id", "")
                    if (bid.isNotEmpty()) {
                        getSharedPreferences("ledmap_update", MODE_PRIVATE)
                            .edit().putString("last_build_id", bid).apply()
                    }
                } catch (_: Throwable) {
                }

                val apkFile = File(cacheDir, "ledmap-cam-update.apk")
                val conn = (URL("$host/download/ledmap-cam.apk").openConnection() as HttpURLConnection).apply {
                    connectTimeout = 5000
                    readTimeout = 20_000
                }
                conn.inputStream.use { input ->
                    apkFile.outputStream().use { output -> input.copyTo(output) }
                }
                conn.disconnect()

                val apkUri = FileProvider.getUriForFile(
                    this@MainActivity,
                    "${packageName}.fileprovider",
                    apkFile
                )
                val installIntent = Intent(Intent.ACTION_VIEW).apply {
                    setDataAndType(apkUri, "application/vnd.android.package-archive")
                    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                }
                runOnUiThread {
                    txtRemote.text = "Tap Update in installer prompt!"
                    btnUpdate.isEnabled = true
                    startActivity(installIntent)
                }
            } catch (t: Throwable) {
                runOnUiThread {
                    txtRemote.text = "Update failed: ${t.message}"
                    btnUpdate.isEnabled = true
                }
            }
        }
    }

    override fun onDestroy() {
        uiScope.cancel()
        beacon?.stop()
        httpServer?.stop()
        cameraController?.stop()
        try {
            if (wakeLock?.isHeld == true) wakeLock?.release()
            if (wifiLock?.isHeld == true) wifiLock?.release()
        } catch (_: Throwable) {
        }
        super.onDestroy()
    }
}
