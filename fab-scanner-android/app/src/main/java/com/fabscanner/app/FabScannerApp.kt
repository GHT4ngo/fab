package com.fabscanner.app

import android.app.Application
import android.util.Log
import java.io.PrintWriter
import java.io.StringWriter

const val LOG_TAG = "FabScanner"

class FabScannerApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Log.i(LOG_TAG, "Application starting")
        val previousHandler = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, throwable ->
            Log.e(LOG_TAG, "Uncaught exception on ${thread.name}", throwable)
            getSharedPreferences("debug", MODE_PRIVATE)
                .edit()
                .putString("last_crash", throwable.stackTraceText())
                .apply()
            previousHandler?.uncaughtException(thread, throwable)
        }
    }
}

private fun Throwable.stackTraceText(): String {
    val writer = StringWriter()
    printStackTrace(PrintWriter(writer))
    return writer.toString()
}
