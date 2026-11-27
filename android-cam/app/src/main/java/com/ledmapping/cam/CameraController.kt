package com.ledmapping.cam

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.graphics.ImageFormat
import android.graphics.Matrix
import android.graphics.SurfaceTexture
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.hardware.camera2.*
import android.media.ImageReader
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Range
import android.util.Size
import android.view.OrientationEventListener
import android.view.Surface
import android.view.TextureView
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.withTimeout
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.atomic.AtomicReference

data class CapturedFrame(
    val jpeg: ByteArray,
    val timestampNs: Long,
    val width: Int,
    val height: Int
)

data class CameraSettings(
    var manualExposure: Boolean = false,
    var exposureTimeNs: Long = 4_000_000L, // 1/250s default when manual
    var iso: Int = 100,
    var evCompensation: Int = 0,
    var aeLock: Boolean = false,
    var awbLock: Boolean = false,
    var manualFocus: Boolean = false,
    var focusDistance: Float = 0.35f, // ~3m indoor default
    var jpegQuality: Int = 85
)

class CameraController(
    private val context: Context,
    private val textureView: TextureView,
    private val onStatusChange: (String) -> Unit
) {
    private val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
    private var bgThread: HandlerThread? = null
    private var bgHandler: Handler? = null

    private var cameraId: String = "0"
    private var characteristics: CameraCharacteristics? = null
    private var cameraDevice: CameraDevice? = null
    private var captureSession: CameraCaptureSession? = null
    private var imageReader: ImageReader? = null
    private var captureSize: Size = Size(1920, 1080)
    private var previewSize: Size = Size(1280, 720)

    private val pendingCapture = AtomicReference<CompletableDeferred<CapturedFrame>?>(null)
    val settings = CameraSettings()

    var hardwareLevelName: String = "UNKNOWN"
        private set
    var supportsManualAe: Boolean = false
        private set
    var supportsManualFocus: Boolean = false
        private set
    private var exposureRange: Range<Long>? = null
    private var isoRange: Range<Int>? = null
    private var evRange: Range<Int> = Range(0, 0)
    private var minFocusDist: Float = 0f
    private var sensorOrientation: Int = 90
    @Volatile
    var deviceOrientationDeg: Int = 0
        private set
    private var orientationListener: OrientationEventListener? = null
    private var sensorManager: SensorManager? = null
    private var gravityListener: SensorEventListener? = null
    // Latest phone gravity vector in Android device coords (x=right, y=up, z=out-of-screen)
    @Volatile
    private var lastGravityDevice: FloatArray? = null

    fun start() {
        bgThread = HandlerThread("Camera2Bg").also { it.start() }
        bgHandler = Handler(bgThread!!.looper)
        selectBackCamera()

        // Track hardware gravity vector so the 3D solver auto-levels to true vertical!
        try {
            val sm = context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
            sensorManager = sm
            val gravSensor = sm.getDefaultSensor(Sensor.TYPE_GRAVITY)
                ?: sm.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
            if (gravSensor != null) {
                val gl = object : SensorEventListener {
                    override fun onSensorChanged(ev: SensorEvent) {
                        val cur = lastGravityDevice
                        if (cur == null) {
                            lastGravityDevice = floatArrayOf(ev.values[0], ev.values[1], ev.values[2])
                        } else {
                            // Smooth low-pass filter so handheld jitter averages out
                            cur[0] = 0.85f * cur[0] + 0.15f * ev.values[0]
                            cur[1] = 0.85f * cur[1] + 0.15f * ev.values[1]
                            cur[2] = 0.85f * cur[2] + 0.15f * ev.values[2]
                        }
                    }
                    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
                }
                gravityListener = gl
                sm.registerListener(gl, gravSensor, SensorManager.SENSOR_DELAY_UI)
            }
        } catch (_: Throwable) {
        }

        orientationListener = object : OrientationEventListener(context) {
            override fun onOrientationChanged(orientation: Int) {
                if (orientation == ORIENTATION_UNKNOWN) return
                val snapped = (((orientation + 45) / 90) * 90) % 360
                if (snapped != deviceOrientationDeg) {
                    deviceOrientationDeg = snapped
                    onStatusChange(statusSummary())
                }
            }
        }.also { if (it.canDetectOrientation()) it.enable() }

        if (textureView.isAvailable) {
            configurePreviewTransform(textureView.width, textureView.height)
            openCamera()
        } else {
            textureView.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
                override fun onSurfaceTextureAvailable(surface: SurfaceTexture, w: Int, h: Int) {
                    configurePreviewTransform(w, h)
                    openCamera()
                }
                override fun onSurfaceTextureSizeChanged(surface: SurfaceTexture, w: Int, h: Int) {
                    configurePreviewTransform(w, h)
                }
                override fun onSurfaceTextureDestroyed(surface: SurfaceTexture): Boolean = true
                override fun onSurfaceTextureUpdated(surface: SurfaceTexture) {}
            }
        }
    }

    private fun configurePreviewTransform(viewW: Int, viewH: Int) {
        val parent = textureView.parent as? android.view.View ?: return
        val parentW = parent.width
        val parentH = parent.height
        if (parentW <= 0 || parentH <= 0) return
        // Back camera sensor is landscape (previewSize.width x previewSize.height),
        // auto-rotated 90 deg by Android in portrait Activity => natural aspect is height / width (3:4)
        val naturalAspect = previewSize.height.toFloat() / previewSize.width.toFloat().coerceAtLeast(1f)
        val parentAspect = parentW.toFloat() / parentH.toFloat()
        val targetW: Int
        val targetH: Int
        if (parentAspect > naturalAspect) {
            targetH = parentH
            targetW = (parentH * naturalAspect).toInt()
        } else {
            targetW = parentW
            targetH = (parentW / naturalAspect).toInt()
        }
        val lp = textureView.layoutParams
        if (lp.width != targetW || lp.height != targetH) {
            lp.width = targetW
            lp.height = targetH
            textureView.post {
                textureView.setTransform(null)
                textureView.layoutParams = lp
            }
        }
    }

    fun stop() {
        orientationListener?.disable()
        orientationListener = null
        try {
            gravityListener?.let { sensorManager?.unregisterListener(it) }
        } catch (_: Throwable) {
        }
        gravityListener = null
        try {
            captureSession?.close()
            captureSession = null
            cameraDevice?.close()
            cameraDevice = null
            imageReader?.close()
            imageReader = null
        } catch (_: Throwable) {
        }
        bgThread?.quitSafely()
        bgThread = null
        bgHandler = null
    }

    private fun currentJpegOrientation(): Int {
        // Back camera: sensorOrientation (typically 90) + clockwise physical device orientation
        return (sensorOrientation + deviceOrientationDeg) % 360
    }

    private fun selectBackCamera() {
        val ids = manager.cameraIdList
        var chosen = ids.firstOrNull() ?: "0"
        for (id in ids) {
            val chars = manager.getCameraCharacteristics(id)
            val facing = chars.get(CameraCharacteristics.LENS_FACING)
            if (facing == CameraCharacteristics.LENS_FACING_BACK) {
                chosen = id
                break
            }
        }
        cameraId = chosen
        val chars = manager.getCameraCharacteristics(cameraId)
        characteristics = chars

        val hwLevel = chars.get(CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL)
            ?: CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LEGACY
        hardwareLevelName = when (hwLevel) {
            CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_3 -> "LEVEL_3"
            CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_FULL -> "FULL"
            CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LIMITED -> "LIMITED"
            CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_EXTERNAL -> "EXTERNAL"
            else -> "LEGACY"
        }

        val aeModes = chars.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_MODES) ?: intArrayOf()
        exposureRange = chars.get(CameraCharacteristics.SENSOR_INFO_EXPOSURE_TIME_RANGE)
        isoRange = chars.get(CameraCharacteristics.SENSOR_INFO_SENSITIVITY_RANGE)
        evRange = chars.get(CameraCharacteristics.CONTROL_AE_COMPENSATION_RANGE) ?: Range(0, 0)
        supportsManualAe = aeModes.contains(CameraMetadata.CONTROL_AE_MODE_OFF) &&
            exposureRange != null && isoRange != null &&
            hwLevel != CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL_LEGACY

        val afModes = chars.get(CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES) ?: intArrayOf()
        minFocusDist = chars.get(CameraCharacteristics.LENS_INFO_MINIMUM_FOCUS_DISTANCE) ?: 0f
        supportsManualFocus = afModes.contains(CameraMetadata.CONTROL_AF_MODE_OFF) && minFocusDist > 0f
        sensorOrientation = chars.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 90

        val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        val jpegSizes = map?.getOutputSizes(ImageFormat.JPEG)?.toList().orEmpty()
        captureSize = chooseBestSize(jpegSizes, targetWidth = 1920, maxWidth = 2560)
        val previewSizes = map?.getOutputSizes(SurfaceTexture::class.java)?.toList().orEmpty()
        previewSize = chooseBestSize(previewSizes, targetWidth = 1280, maxWidth = 1280)
    }

    private fun chooseBestSize(sizes: List<Size>, targetWidth: Int, maxWidth: Int): Size {
        if (sizes.isEmpty()) return Size(1920, 1080)
        // Prefer 16:9 or 4:3 rectangular formats over square 1:1 crops for widest FOV
        val wide = sizes.filter {
            it.width <= maxWidth && it.width >= 640 && it.height > 0 &&
                (it.width.toDouble() / it.height.toDouble()) in 1.25..1.85
        }
        val pool = if (wide.isNotEmpty()) wide else sizes.filter { it.width <= maxWidth && it.width >= 640 }.ifEmpty { sizes }
        return pool.minByOrNull { kotlin.math.abs(it.width - targetWidth) } ?: pool.first()
    }

    @SuppressLint("MissingPermission")
    private fun openCamera() {
        try {
            onStatusChange("Opening camera $cameraId ($hardwareLevelName)...")
            manager.openCamera(cameraId, object : CameraDevice.StateCallback() {
                override fun onOpened(device: CameraDevice) {
                    cameraDevice = device
                    createSession()
                }
                override fun onDisconnected(device: CameraDevice) {
                    device.close()
                    cameraDevice = null
                    onStatusChange("Camera disconnected")
                }
                override fun onError(device: CameraDevice, error: Int) {
                    device.close()
                    cameraDevice = null
                    onStatusChange("Camera error: $error")
                }
            }, bgHandler)
        } catch (t: Throwable) {
            onStatusChange("Open camera failed: ${t.message}")
        }
    }

    private fun createSession() {
        val device = cameraDevice ?: return
        val texture = textureView.surfaceTexture ?: return
        texture.setDefaultBufferSize(previewSize.width, previewSize.height)
        val previewSurface = Surface(texture)

        imageReader?.close()
        imageReader = ImageReader.newInstance(captureSize.width, captureSize.height, ImageFormat.JPEG, 3).apply {
            setOnImageAvailableListener({ reader ->
                val img = reader.acquireLatestImage() ?: return@setOnImageAvailableListener
                try {
                    val buf = img.planes[0].buffer
                    val bytes = ByteArray(buf.remaining())
                    buf.get(bytes)
                    val frame = CapturedFrame(
                        jpeg = bytes,
                        timestampNs = SystemClock.elapsedRealtimeNanos(),
                        width = img.width,
                        height = img.height
                    )
                    pendingCapture.getAndSet(null)?.complete(frame)
                } finally {
                    img.close()
                }
            }, bgHandler)
        }

        val surfaces = listOf(previewSurface, imageReader!!.surface)
        @Suppress("DEPRECATION")
        device.createCaptureSession(surfaces, object : CameraCaptureSession.StateCallback() {
            override fun onConfigured(session: CameraCaptureSession) {
                captureSession = session
                applyRepeatingPreview()
                onStatusChange(statusSummary())
            }
            override fun onConfigureFailed(session: CameraCaptureSession) {
                onStatusChange("Camera session configure failed")
            }
        }, bgHandler)
    }

    private fun configureBuilder(builder: CaptureRequest.Builder) {
        if (settings.manualExposure && supportsManualAe) {
            val expR = exposureRange!!
            val isoR = isoRange!!
            val expClamped = settings.exposureTimeNs.coerceIn(expR.lower, expR.upper)
            val isoClamped = settings.iso.coerceIn(isoR.lower, isoR.upper)
            builder.set(CaptureRequest.CONTROL_AE_MODE, CameraMetadata.CONTROL_AE_MODE_OFF)
            builder.set(CaptureRequest.SENSOR_EXPOSURE_TIME, expClamped)
            builder.set(CaptureRequest.SENSOR_SENSITIVITY, isoClamped)
        } else {
            builder.set(CaptureRequest.CONTROL_AE_MODE, CameraMetadata.CONTROL_AE_MODE_ON)
            val evClamped = settings.evCompensation.coerceIn(evRange.lower, evRange.upper)
            builder.set(CaptureRequest.CONTROL_AE_EXPOSURE_COMPENSATION, evClamped)
            builder.set(CaptureRequest.CONTROL_AE_LOCK, settings.aeLock)
        }

        builder.set(CaptureRequest.CONTROL_AWB_LOCK, settings.awbLock)

        if (settings.manualFocus && supportsManualFocus) {
            builder.set(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_OFF)
            builder.set(CaptureRequest.LENS_FOCUS_DISTANCE, settings.focusDistance.coerceIn(0f, minFocusDist))
        } else {
            builder.set(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
        }

        // Disable stabilization during scanning so pixel coordinates stay fixed
        builder.set(CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE, CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_OFF)
        builder.set(CaptureRequest.LENS_OPTICAL_STABILIZATION_MODE, CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_OFF)
    }

    fun applyRepeatingPreview() {
        val device = cameraDevice ?: return
        val session = captureSession ?: return
        val texture = textureView.surfaceTexture ?: return
        try {
            val builder = device.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW)
            builder.addTarget(Surface(texture))
            configureBuilder(builder)
            session.setRepeatingRequest(builder.build(), null, bgHandler)
            onStatusChange(statusSummary())
        } catch (t: Throwable) {
            onStatusChange("Preview update error: ${t.message}")
        }
    }

    suspend fun captureFreshFrame(timeoutMs: Long = 3500L): CapturedFrame {
        val device = cameraDevice ?: error("Camera not open")
        val session = captureSession ?: error("Session not ready")
        val reader = imageReader ?: error("ImageReader not ready")

        val deferred = CompletableDeferred<CapturedFrame>()
        pendingCapture.getAndSet(deferred)?.cancel()

        val builder = device.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE)
        builder.addTarget(reader.surface)
        configureBuilder(builder)
        builder.set(CaptureRequest.JPEG_QUALITY, settings.jpegQuality.coerceIn(40, 98).toByte())
        builder.set(CaptureRequest.JPEG_ORIENTATION, currentJpegOrientation())

        session.capture(builder.build(), null, bgHandler)
        return withTimeout(timeoutMs) { deferred.await() }
    }

    fun grabPreviewJpeg(): ByteArray? {
        if (!textureView.isAvailable) return null
        // Preserve natural 3:4 portrait sensor aspect (never squish into 640x360!)
        val natW = previewSize.height.coerceAtLeast(1)
        val natH = previewSize.width.coerceAtLeast(1)
        val maxSide = 540f
        val scale = maxSide / maxOf(natW, natH).toFloat()
        val bw = (natW * scale).toInt().coerceAtLeast(64)
        val bh = (natH * scale).toInt().coerceAtLeast(64)
        val bmp: Bitmap = textureView.getBitmap(bw, bh) ?: return null
        val rot = deviceOrientationDeg
        val finalBmp = if (rot != 0) {
            val m = Matrix().apply { postRotate(rot.toFloat()) }
            Bitmap.createBitmap(bmp, 0, 0, bmp.width, bmp.height, m, true).also { bmp.recycle() }
        } else {
            bmp
        }
        return try {
            val out = ByteArrayOutputStream()
            finalBmp.compress(Bitmap.CompressFormat.JPEG, 70, out)
            out.toByteArray()
        } finally {
            finalBmp.recycle()
        }
    }

    fun applyLedScanPreset() {
        if (supportsManualAe) {
            settings.manualExposure = true
            settings.exposureTimeNs = 4_000_000L // 1/250s
            settings.iso = 100
        } else {
            // Graceful fallback for older LIMITED/LEGACY Samsung tablets
            settings.manualExposure = false
            settings.evCompensation = evRange.lower
            settings.aeLock = true
        }
        settings.awbLock = true
        if (supportsManualFocus) {
            settings.manualFocus = true
            settings.focusDistance = (0.35f).coerceIn(0f, minFocusDist)
        }
        applyRepeatingPreview()
    }

    fun applyAutoPreset() {
        settings.manualExposure = false
        settings.evCompensation = 0
        settings.aeLock = false
        settings.awbLock = false
        settings.manualFocus = false
        applyRepeatingPreview()
    }

    fun applyConfigJson(obj: JSONObject): JSONObject {
        if (obj.optBoolean("led_preset", false)) {
            applyLedScanPreset()
        } else if (obj.optBoolean("auto_preset", false)) {
            applyAutoPreset()
        } else {
            if (obj.has("manual_exposure")) settings.manualExposure = obj.getBoolean("manual_exposure")
            if (obj.has("exposure_time_ns")) settings.exposureTimeNs = obj.getLong("exposure_time_ns")
            if (obj.has("exposure_ms")) settings.exposureTimeNs = (obj.getDouble("exposure_ms") * 1_000_000.0).toLong()
            if (obj.has("iso")) settings.iso = obj.getInt("iso")
            if (obj.has("ev_compensation")) settings.evCompensation = obj.getInt("ev_compensation")
            if (obj.has("ae_lock")) settings.aeLock = obj.getBoolean("ae_lock")
            if (obj.has("awb_lock")) settings.awbLock = obj.getBoolean("awb_lock")
            if (obj.has("manual_focus")) settings.manualFocus = obj.getBoolean("manual_focus")
            if (obj.has("focus_distance")) settings.focusDistance = obj.getDouble("focus_distance").toFloat()
            if (obj.has("jpeg_quality")) settings.jpegQuality = obj.getInt("jpeg_quality")
            applyRepeatingPreview()
        }
        return JSONObject().apply {
            put("ok", true)
            put("manual_exposure", settings.manualExposure && supportsManualAe)
            put("exposure_time_ns", settings.exposureTimeNs)
            put("iso", settings.iso)
            put("ev_compensation", settings.evCompensation)
            put("ae_lock", settings.aeLock)
            put("awb_lock", settings.awbLock)
            put("manual_focus", settings.manualFocus && supportsManualFocus)
            put("focus_distance", settings.focusDistance)
        }
    }

    fun buildIntrinsicsJson(): JSONObject {
        val rawW = captureSize.width.toDouble()
        val rawH = captureSize.height.toDouble()
        val swapped = (currentJpegOrientation() % 180) != 0
        val outW = if (swapped) rawH else rawW
        val outH = if (swapped) rawW else rawH
        val chars = characteristics
        if (chars != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            val intr = chars.get(CameraCharacteristics.LENS_INTRINSIC_CALIBRATION)
            val activeRect = chars.get(CameraCharacteristics.SENSOR_INFO_PRE_CORRECTION_ACTIVE_ARRAY_SIZE)
                ?: chars.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)
            if (intr != null && intr.size >= 4 && intr[0] > 0f && intr[1] > 0f && activeRect != null) {
                val sx = rawW / activeRect.width().toDouble()
                val sy = rawH / activeRect.height().toDouble()
                val fxRaw = intr[0].toDouble() * sx
                val fyRaw = intr[1].toDouble() * sy
                // Note: Android Camera2 ISP applies hardware distortion correction to JPEG outputs by default,
                // and LENS_DISTORTION uses [k1, k2, k3, p1, p2] (different from OpenCV's [k1, k2, p1, p2, k3]).
                // Report zero distortion for ISP-undistorted JPEGs unless calibrated via ChArUco.
                val distArr = JSONArray(listOf(0.0, 0.0, 0.0, 0.0, 0.0))
                return JSONObject().apply {
                    put("fx", if (swapped) fyRaw else fxRaw)
                    put("fy", if (swapped) fxRaw else fyRaw)
                    put("cx", outW / 2.0)
                    put("cy", outH / 2.0)
                    put("dist", distArr)
                    put("source", "camera2_intrinsic")
                }
            }
        }

        // Fallback: focal length + physical sensor size (works down to API 21 / older tablets)
        var fx = rawW * 0.85
        var fy = rawW * 0.85
        var source = "estimated_fov"
        if (chars != null) {
            val focalLengths = chars.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)
            val physSize = chars.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE)
            if (focalLengths != null && focalLengths.isNotEmpty() && physSize != null && physSize.width > 0f && physSize.height > 0f) {
                val fMm = focalLengths[0].toDouble()
                fx = rawW * fMm / physSize.width.toDouble()
                fy = rawH * fMm / physSize.height.toDouble()
                source = "camera2_focal_sensor"
            }
        }
        return JSONObject().apply {
            put("fx", if (swapped) fy else fx)
            put("fy", if (swapped) fx else fy)
            put("cx", outW / 2.0)
            put("cy", outH / 2.0)
            put("dist", JSONArray(listOf(0.0, 0.0, 0.0, 0.0, 0.0)))
            put("source", source)
        }
    }

    fun buildInfoJson(deviceId: String, ip: String, port: Int, batteryPct: Int): JSONObject {
        val swapped = (currentJpegOrientation() % 180) != 0
        return JSONObject().apply {
            put("id", deviceId)
            put("name", "${Build.MANUFACTURER} ${Build.MODEL}".trim())
            put("model", Build.MODEL)
            put("manufacturer", Build.MANUFACTURER)
            put("platform", "android")
            put("api_level", Build.VERSION.SDK_INT)
            put("hardware_level", hardwareLevelName)
            put("ip", ip)
            put("port", port)
            put("battery", batteryPct)
            put("width", if (swapped) captureSize.height else captureSize.width)
            put("height", if (swapped) captureSize.width else captureSize.height)
            put("jpeg_orientation", currentJpegOrientation())
            put("device_orientation", deviceOrientationDeg)
            getGravityInOpenCvCamCoords()?.let { g ->
                put("gravity_cam", JSONArray(listOf(g[0].toDouble(), g[1].toDouble(), g[2].toDouble())))
            }
            put("supports_manual_exposure", supportsManualAe)
            put("supports_manual_focus", supportsManualFocus)
            put("ev_min", evRange.lower)
            put("ev_max", evRange.upper)
            put("exposure_min_ns", exposureRange?.lower ?: 100_000L)
            put("exposure_max_ns", exposureRange?.upper ?: 100_000_000L)
            put("iso_min", isoRange?.lower ?: 50)
            put("iso_max", isoRange?.upper ?: 3200)
            put("min_focus_distance", minFocusDist.toDouble())
            put("current_config", JSONObject().apply {
                put("manual_exposure", settings.manualExposure && supportsManualAe)
                put("exposure_time_ns", settings.exposureTimeNs)
                put("iso", settings.iso)
                put("ev_compensation", settings.evCompensation)
                put("ae_lock", settings.aeLock)
                put("awb_lock", settings.awbLock)
                put("manual_focus", settings.manualFocus && supportsManualFocus)
                put("focus_distance", settings.focusDistance)
            })
            put("intrinsics", buildIntrinsicsJson())
        }
    }

    /**
     * Convert Android device gravity (x=right, y=up, z=out-of-screen; points UPWARD as proper acceleration +9.8 on Z when flat)
     * into the upright OpenCV image camera frame (x=image right, y=image down, z=into scene away from back camera).
     * Returns unit vector pointing DOWNWARD (direction of gravity).
     */
    private fun getGravityInOpenCvCamCoords(): FloatArray? {
        val g = lastGravityDevice ?: return null
        val norm = kotlin.math.sqrt(g[0] * g[0] + g[1] * g[1] + g[2] * g[2])
        if (norm < 1f) return null
        // Downward gravity in portrait device frame: (-gx, -gy, -gz) / norm
        val dx = -g[0] / norm
        val dy = -g[1] / norm
        val dz = -g[2] / norm
        // In portrait upright back-camera OpenCV coords: x_cam = +dx, y_cam = -dy, z_cam = -dz
        val px = dx
        val py = -dy
        val pz = -dz
        // Rotate (px, py) by -deviceOrientationDeg to match the upright rotated JPEG axes
        return when (deviceOrientationDeg) {
            90 -> floatArrayOf(-py, px, pz)
            180 -> floatArrayOf(-px, -py, pz)
            270 -> floatArrayOf(py, -px, pz)
            else -> floatArrayOf(px, py, pz)
        }
    }

    fun statusSummary(): String {
        val mode = if (settings.manualExposure && supportsManualAe) {
            "MANUAL 1/${(1_000_000_000L / settings.exposureTimeNs.coerceAtLeast(1L))}s ISO ${settings.iso}"
        } else {
            "AUTO EV=${settings.evCompensation} AE_LOCK=${settings.aeLock}"
        }
        return "$hardwareLevelName | ${captureSize.width}x${captureSize.height} | $mode"
    }
}
