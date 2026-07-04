package com.fabscanner.app

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.Matrix
import android.os.Bundle
import android.util.Base64
import android.util.Log
import android.util.Size
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.MeteringPointFactory
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

class MainActivity : ComponentActivity() {
    private val cameraExecutor = Executors.newSingleThreadExecutor()
    // The /scan/native OCR pipeline can take several seconds; the default 10s read
    // timeout trips a SocketTimeoutException on slower scans (esp. over the tunnel).
    private val http = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .callTimeout(45, TimeUnit.SECONDS)
        .build()
    private lateinit var previewView: PreviewView
    private lateinit var status: TextView
    private lateinit var debug: TextView
    private lateinit var apiInput: EditText
    private lateinit var apiRow: LinearLayout
    private lateinit var sessionInput: EditText
    private var camera: Camera? = null
    private var cameraProvider: ProcessCameraProvider? = null
    private var cameraOn = true
    private var torchOn = false
    private var apiBase = DEFAULT_API_BASE
    private var showAdvanced = false
    private var sessionCode = ""
    private var busy = false
    private var lastAttemptMs = 0L
    private var lastHitId: String? = null
    private var lastHitMs = 0L
    private val minFooterSharpness = 8.0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = getSharedPreferences("scanner", MODE_PRIVATE)
        apiBase = cleanApiBase(intent.getStringExtra("apiBase") ?: prefs.getString("api_base", apiBase).orEmpty())
        sessionCode = prefs.getString("session_code", "").orEmpty()
        Log.i(LOG_TAG, "MainActivity starting apiBase=$apiBase")
        buildUi()
        discoverApiBase()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            Log.i(LOG_TAG, "Camera permission already granted")
            startCamera()
        } else {
            Log.i(LOG_TAG, "Requesting camera permission")
            ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.CAMERA), 10)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        Log.i(LOG_TAG, "Permission result requestCode=$requestCode granted=${grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED}")
        if (requestCode == 10 && grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            startCamera()
        } else {
            status.text = "Camera permission denied"
        }
    }

    private fun buildUi() {
        val root = FrameLayout(this)
        previewView = PreviewView(this).apply {
            scaleType = PreviewView.ScaleType.FILL_CENTER
        }
        root.addView(previewView, FrameLayout.LayoutParams(-1, -1))

        val guide = CardGuideView(this)
        root.addView(guide, FrameLayout.LayoutParams(-1, -1))

        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(20, 20, 20, 20)
            setBackgroundColor(Color.argb(145, 0, 0, 0))
        }
        status = TextView(this).apply {
            text = "Starting camera"
            setTextColor(Color.WHITE)
            textSize = 16f
        }
        debug = TextView(this).apply {
            text = lastCrashSummary() ?: "Enter the pair code from the web scanner."
            setTextColor(Color.rgb(180, 255, 200))
            textSize = 12f
        }
        apiInput = EditText(this).apply {
            setSingleLine(true)
            hint = "API URL"
            setText(apiBase)
            setTextColor(Color.WHITE)
            setHintTextColor(Color.rgb(180, 180, 180))
            setBackgroundColor(Color.argb(70, 255, 255, 255))
            setPadding(16, 6, 16, 6)
        }
        sessionInput = EditText(this).apply {
            setSingleLine(true)
            hint = "Pair code"
            setText(sessionCode)
            setTextColor(Color.WHITE)
            setHintTextColor(Color.rgb(180, 180, 180))
            setBackgroundColor(Color.argb(70, 255, 255, 255))
            setPadding(16, 6, 16, 6)
        }
        apiRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            visibility = View.GONE
        }
        apiRow.addView(apiInput, LinearLayout.LayoutParams(0, -2, 1f))
        apiRow.addView(Button(this).apply {
            text = "Use"
            setOnClickListener {
                apiBase = cleanApiBase(apiInput.text?.toString().orEmpty())
                apiInput.setText(apiBase)
                getSharedPreferences("scanner", MODE_PRIVATE)
                    .edit()
                    .putString("api_base", apiBase)
                    .apply()
                status.text = "API URL saved"
                debug.text = apiBase
            }
        })
        val sessionRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        sessionRow.addView(sessionInput, LinearLayout.LayoutParams(0, -2, 1f))
        sessionRow.addView(Button(this).apply {
            text = "Pair"
            setOnClickListener {
                sessionCode = cleanSessionCode(sessionInput.text?.toString().orEmpty())
                sessionInput.setText(sessionCode)
                getSharedPreferences("scanner", MODE_PRIVATE)
                    .edit()
                    .putString("session_code", sessionCode)
                    .apply()
                status.text = if (sessionCode.isBlank()) "Enter pair code" else "Ready to scan"
                debug.text = if (sessionCode.isBlank()) "Open the web scanner and tap Pair phone." else "Pass cards through the guide."
            }
        })
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        row.addView(Button(this).apply {
            text = if (cameraOn) "Cam On" else "Cam Off"
            setOnClickListener {
                toggleCamera()
                text = if (cameraOn) "Cam On" else "Cam Off"
            }
        })
        row.addView(Button(this).apply {
            text = "Refocus"
            setOnClickListener { focusAtCenter() }
        })
        row.addView(Button(this).apply {
            text = "Torch"
            setOnClickListener {
                torchOn = !torchOn
                camera?.cameraControl?.enableTorch(torchOn)
            }
        })
        row.addView(Button(this).apply {
            text = "Advanced"
            setOnClickListener {
                showAdvanced = !showAdvanced
                apiRow.visibility = if (showAdvanced) View.VISIBLE else View.GONE
                debug.text = if (showAdvanced) apiBase else "Pass cards through the guide."
            }
        })
        panel.addView(status)
        panel.addView(debug)
        panel.addView(apiRow)
        panel.addView(sessionRow)
        panel.addView(row)

        root.addView(panel, FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM))
        previewView.setOnTouchListener { _, event ->
            if (event.action == MotionEvent.ACTION_UP) {
                focusAt(event.x, event.y)
            }
            true
        }
        setContentView(root)
    }

    private fun startCamera() {
        Log.i(LOG_TAG, "Starting CameraX")
        val providerFuture = ProcessCameraProvider.getInstance(this)
        providerFuture.addListener({
            try {
                cameraProvider = providerFuture.get()
                if (cameraOn) bindCamera() else stopCamera()
            } catch (e: Exception) {
                Log.e(LOG_TAG, "CameraX startup failed", e)
                status.text = "Camera failed: ${e.javaClass.simpleName}"
            }
        }, ContextCompat.getMainExecutor(this))
    }

    /** Bind preview + analysis so the camera runs and frames are scanned/posted. */
    private fun bindCamera() {
        val provider = cameraProvider ?: return
        val preview = Preview.Builder()
            .setTargetResolution(Size(1920, 1080))
            .build()
            .also { it.setSurfaceProvider(previewView.surfaceProvider) }

        val analysis = ImageAnalysis.Builder()
            .setTargetResolution(Size(1920, 1080))
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .build()
            .also { it.setAnalyzer(cameraExecutor) { image -> analyze(image) } }

        provider.unbindAll()
        camera = provider.bindToLifecycle(
            this,
            CameraSelector.DEFAULT_BACK_CAMERA,
            preview,
            analysis,
        )
        camera?.cameraControl?.setZoomRatio(2.0f)
        cameraOn = true
        status.text = "Scanning"
        Log.i(LOG_TAG, "CameraX bound successfully")
        focusAtCenter()
    }

    /** Unbind everything: preview + analysis stop, so NO frames are scanned or posted. */
    private fun stopCamera() {
        cameraProvider?.unbindAll()
        camera = null
        cameraOn = false
        torchOn = false
        busy = false
        status.text = "Camera off — tap Camera to scan"
        Log.i(LOG_TAG, "CameraX unbound (camera off)")
    }

    /** Toggle the camera on/off — off means zero API traffic while you're not scanning. */
    private fun toggleCamera() {
        if (cameraProvider == null) { startCamera(); return }
        if (cameraOn) stopCamera() else bindCamera()
    }

    private fun focusAtCenter() {
        focusAt(previewView.width * 0.5f, previewView.height * 0.72f)
    }

    private fun focusAt(x: Float, y: Float) {
        val factory: MeteringPointFactory = previewView.meteringPointFactory
        val point = factory.createPoint(x, y)
        val action = FocusMeteringAction.Builder(point, FocusMeteringAction.FLAG_AF or FocusMeteringAction.FLAG_AE)
            .setAutoCancelDuration(2, TimeUnit.SECONDS)
            .build()
        camera?.cameraControl?.startFocusAndMetering(action)
        status.text = "Refocusing"
    }

    private fun analyze(image: ImageProxy) {
        try {
            if (!cameraOn) return
            if (sessionCode.isBlank()) {
                runOnUiThread { status.text = "Enter pair code" }
                return
            }
            val now = System.currentTimeMillis()
            if (busy || now - lastAttemptMs < 700) return
            lastAttemptMs = now

            val frame = image.toBitmap()
            val rotated = frame.rotate(image.imageInfo.rotationDegrees)
            val card = cropCardGuide(rotated)
            val footer = cropRelative(card, 0.02f, 0.74f, 0.96f, 0.23f)
            val sharpness = footer.sharpnessScore()
            runOnUiThread {
                if (showAdvanced) {
                    debug.text = "footer sharpness ${sharpness.toInt()} / ${minFooterSharpness.toInt()} | ${footer.width}x${footer.height}"
                }
            }
            if (sharpness < minFooterSharpness) {
                runOnUiThread { status.text = "Hold steady / refocus" }
                return
            }

            val title = cropRelative(card, 0.05f, 0.04f, 0.9f, 0.11f)
            busy = true
            postNativeScan(card, footer, title)
        } catch (e: Exception) {
            busy = false
            Log.e(LOG_TAG, "Image analysis failed", e)
            runOnUiThread { status.text = "Scan failed: ${e.javaClass.simpleName}" }
        } finally {
            image.close()
        }
    }

    private fun postNativeScan(card: Bitmap, footer: Bitmap, title: Bitmap) {
        Log.i(LOG_TAG, "Posting native scan full=${card.width}x${card.height} footer=${footer.width}x${footer.height} title=${title.width}x${title.height}")
        val body = """
            {
              "full_image": "${card.toJpegBase64(88)}",
              "footer_crop": "${footer.toJpegBase64(94)}",
              "title_crop": "${title.toJpegBase64(92)}",
              "debug_save": true,
              "session_code": "${jsonEscape(sessionCode)}"
            }
        """.trimIndent()
        val req = Request.Builder()
            .url("$apiBase/scan/native")
            .post(body.toRequestBody("application/json".toMediaType()))
            .build()
        http.newCall(req).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                busy = false
                Log.e(LOG_TAG, "Native scan request failed", e)
                runOnUiThread {
                    status.text = "Network error: ${e.javaClass.simpleName}"
                    debug.text = if (showAdvanced) "${e.message.orEmpty()} | $apiBase" else "Check connection and pair code."
                }
            }

            override fun onResponse(call: Call, response: Response) {
                val text = response.body?.string().orEmpty()
                busy = false
                Log.i(LOG_TAG, "Native scan response code=${response.code} bytes=${text.length}")
                val displayId = Regex("\"display_id\"\\s*:\\s*\"([^\"]+)\"").find(text)?.groupValues?.get(1)
                val name = Regex("\"name\"\\s*:\\s*\"([^\"]+)\"").find(text)?.groupValues?.get(1)
                val now = System.currentTimeMillis()
                if (displayId != null && (displayId != lastHitId || now - lastHitMs > 2200)) {
                    lastHitId = displayId
                    lastHitMs = now
                    runOnUiThread { status.text = "Added $displayId ${name ?: ""}".trim() }
                } else {
                    runOnUiThread { status.text = if (response.isSuccessful) "Searching for code" else "Server ${response.code}" }
                }
            }
        })
    }

    private fun cropCardGuide(src: Bitmap): Bitmap {
        val cardAspect = 5f / 7f
        var h = (src.height * 0.72f).toInt()
        var w = (h * cardAspect).toInt()
        val maxW = (src.width * 0.92f).toInt()
        if (w > maxW) {
            w = maxW
            h = (w / cardAspect).toInt()
        }
        val x = max(0, (src.width - w) / 2)
        val y = max(0, (src.height - h) / 2)
        return Bitmap.createBitmap(src, x, y, min(w, src.width - x), min(h, src.height - y))
    }

    private fun cropRelative(src: Bitmap, x: Float, y: Float, w: Float, h: Float): Bitmap {
        val px = (src.width * x).toInt()
        val py = (src.height * y).toInt()
        val pw = (src.width * w).toInt()
        val ph = (src.height * h).toInt()
        return Bitmap.createBitmap(src, px, py, min(pw, src.width - px), min(ph, src.height - py))
    }

    private fun lastCrashSummary(): String? {
        val crash = getSharedPreferences("debug", MODE_PRIVATE).getString("last_crash", null) ?: return null
        return "Previous crash: ${crash.lineSequence().firstOrNull().orEmpty()}"
    }

    private fun cleanApiBase(raw: String): String {
        return raw.trim().trimEnd('/').ifBlank { DEFAULT_API_BASE }
    }

    /**
     * Fetch the current backend URL from the discovery gist and adopt it if it changed,
     * so the app follows a rotating tunnel URL without a rebuild or manual re-entry.
     * Best-effort and async: on any failure we silently keep the stored/default URL.
     * A manual Advanced override still wins for the rest of the session.
     */
    private fun discoverApiBase() {
        val req = Request.Builder().url(ENDPOINT_DISCOVERY_URL).build()
        http.newCall(req).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.w(LOG_TAG, "Endpoint discovery failed: ${e.message}")
            }

            override fun onResponse(call: Call, response: Response) {
                val body = response.use { if (it.isSuccessful) it.body?.string().orEmpty() else "" }
                val discovered = body.lineSequence()
                    .map { it.trim() }
                    .firstOrNull { it.startsWith("http://") || it.startsWith("https://") }
                    ?.let { cleanApiBase(it) }
                if (discovered.isNullOrBlank() || discovered == apiBase) {
                    Log.i(LOG_TAG, "Endpoint discovery: no change (apiBase=$apiBase)")
                    return
                }
                Log.i(LOG_TAG, "Endpoint discovery: apiBase $apiBase -> $discovered")
                runOnUiThread {
                    apiBase = discovered
                    getSharedPreferences("scanner", MODE_PRIVATE)
                        .edit()
                        .putString("api_base", discovered)
                        .apply()
                    apiInput.setText(discovered)
                    if (showAdvanced) debug.text = discovered
                }
            }
        })
    }

    override fun onDestroy() {
        super.onDestroy()
        cameraExecutor.shutdown()
    }
}

private fun ImageProxy.toBitmap(): Bitmap {
    val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
    val buffer = planes[0].buffer
    buffer.rewind()
    bitmap.copyPixelsFromBuffer(buffer)
    return bitmap
}

private fun Bitmap.rotate(degrees: Int): Bitmap {
    if (degrees == 0) return this
    val matrix = Matrix().apply { postRotate(degrees.toFloat()) }
    return Bitmap.createBitmap(this, 0, 0, width, height, matrix, true)
}

private fun Bitmap.toJpegBase64(quality: Int): String {
    val out = ByteArrayOutputStream()
    compress(Bitmap.CompressFormat.JPEG, quality, out)
    return Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)
}

private fun Bitmap.sharpnessScore(): Double {
    val small = Bitmap.createScaledBitmap(this, max(2, width / 4), max(2, height / 4), true)
    var sum = 0.0
    var count = 0
    for (y in 1 until small.height - 1) {
        for (x in 1 until small.width - 1) {
            val c = Color.red(small.getPixel(x, y))
            val lap = -4 * c +
                Color.red(small.getPixel(x - 1, y)) +
                Color.red(small.getPixel(x + 1, y)) +
                Color.red(small.getPixel(x, y - 1)) +
                Color.red(small.getPixel(x, y + 1))
            sum += abs(lap.toDouble())
            count++
        }
    }
    return if (count == 0) 0.0 else sum / count
}

private fun cleanSessionCode(raw: String): String =
    raw.uppercase().filter { it in 'A'..'Z' || it in '0'..'9' }.take(12)

private fun jsonEscape(raw: String): String =
    raw.replace("\\", "\\\\").replace("\"", "\\\"")

private const val DEFAULT_API_BASE = "https://ties-immigration-save-sitemap.trycloudflare.com"

// The backend runs behind an ephemeral trycloudflare URL that rotates on every reboot /
// new tunnel. Rather than rebuild the app each time, start_fab.py publishes the live URL
// to this public gist and we fetch it at launch. This is the always-latest raw URL, so it
// never changes even as the endpoint content does.
private const val ENDPOINT_DISCOVERY_URL =
    "https://gist.githubusercontent.com/GHT4ngo/84b51c1df1551685fb9b151f684d979d/raw/endpoint.txt"
