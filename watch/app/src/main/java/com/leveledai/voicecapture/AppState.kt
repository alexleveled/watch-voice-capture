package com.leveledai.voicecapture

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.json.JSONObject
import java.io.File

/**
 * Server config is written by the PC over ADB (tools/setup_watch.py, `run-as ... files/config.json`), never typed on the
 * watch and never exposed through an exported component:
 *   {"server_url": "http://192.168.1.50:8808", "ingest_token": "...",
 *    "cf_client_id": "...", "cf_client_secret": "...", "device_id": "galaxy-watch"}
 */
data class ServerConfig(
    val serverUrl: String,
    val ingestToken: String,
    val cfClientId: String?,
    val cfClientSecret: String?,
    val deviceId: String,
) {
    companion object {
        fun load(ctx: Context): ServerConfig? {
            val f = File(ctx.filesDir, "config.json")
            if (!f.exists()) return null
            return try {
                val j = JSONObject(f.readText())
                ServerConfig(
                    serverUrl = j.getString("server_url").trimEnd('/'),
                    ingestToken = j.getString("ingest_token"),
                    cfClientId = j.optString("cf_client_id").ifBlank { null },
                    cfClientSecret = j.optString("cf_client_secret").ifBlank { null },
                    deviceId = j.optString("device_id").ifBlank { "galaxy-watch" },
                )
            } catch (e: Exception) {
                null
            }
        }
    }
}

enum class Mode { MANUAL, AUTO }

/** Process-wide UI state. The service and the upload worker push into it; the UI and complication read it. */
object AppState {
    val recording = MutableStateFlow(false)
    /** Mic open in auto mode (listening for speech or recording). */
    val listening = MutableStateFlow(false)
    /** One-line auto-mode status for the UI, null when auto mode is off. */
    val autoStatus = MutableStateFlow<String?>(null)
    val recordingSince = MutableStateFlow<Long?>(null)
    val pendingChunks = MutableStateFlow(0)
    val lastUpload = MutableStateFlow<String?>(null)

    private const val PREFS = "voice_capture"

    fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun mode(ctx: Context): Mode =
        if (prefs(ctx).getString("mode", "MANUAL") == "AUTO") Mode.AUTO else Mode.MANUAL

    fun setMode(ctx: Context, m: Mode) = prefs(ctx).edit().putString("mode", m.name).apply()

    fun setLastUpload(ctx: Context, msg: String) {
        lastUpload.value = msg
        prefs(ctx).edit().putString("last_upload", msg).apply()
    }

    fun restore(ctx: Context) {
        if (lastUpload.value == null) lastUpload.value = prefs(ctx).getString("last_upload", null)
        pendingChunks.value = Outbox.count(ctx)
    }

    val recordingFlow: StateFlow<Boolean> get() = recording
}
