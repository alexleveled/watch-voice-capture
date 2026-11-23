package com.leveledai.voicecapture

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import org.json.JSONObject
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit

/**
 * Uploads finished chunks whenever any network is up: Wear OS routes through the phone over
 * Bluetooth when it's near, or Wi-Fi when it isn't. Battery saver = wait for unmetered (Wi-Fi).
 * A file is deleted only after the server answers 200 with the same SHA-256.
 */
object Uploader {
    private const val NOW = "upload-now"
    private const val PERIODIC = "upload-periodic"

    private fun constraints(ctx: Context) = Constraints.Builder()
        .setRequiredNetworkType(
            if (AppState.prefs(ctx).getBoolean("battery_saver", false)) NetworkType.UNMETERED else NetworkType.CONNECTED)
        .build()

    fun kick(ctx: Context) {
        val req = OneTimeWorkRequestBuilder<UploadWorker>()
            .setConstraints(constraints(ctx))
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(ctx).enqueueUniqueWork(NOW, ExistingWorkPolicy.REPLACE, req)  // fresh kick skips any retry backoff; server dedupes by sha
    }

    /** Safety net: every 15 minutes (WorkManager's minimum), in case a kick was lost. */
    fun schedulePeriodic(ctx: Context) {
        val req = PeriodicWorkRequestBuilder<UploadWorker>(15, TimeUnit.MINUTES)
            .setConstraints(constraints(ctx)).build()
        WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.UPDATE, req)
    }
}

class UploadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    private val http = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .writeTimeout(120, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    private fun stamp() = SimpleDateFormat("HH:mm", Locale.US).format(Date())

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val ctx = applicationContext
        val cfg = ServerConfig.load(ctx) ?: run {
            AppState.setLastUpload(ctx, "Not set up (no server config)")
            return@withContext Result.failure()
        }
        var sent = 0
        for (item in Outbox.pending(ctx)) {
            val m = item.meta
            val type = if (item.audio.extension == "ogg") "audio/ogg" else "audio/mp4"
            val body = MultipartBody.Builder().setType(MultipartBody.FORM)
                .addFormDataPart("file", item.audio.name, item.audio.asRequestBody(type.toMediaType()))
                .apply {
                    for (k in listOf("recording_id", "idx", "is_last", "started_at", "chunk_started_at",
                                     "duration_s", "trigger", "device_id", "sha256")) {
                        if (m.has(k)) addFormDataPart(k, m.get(k).toString())
                    }
                }.build()
            val req = Request.Builder().url("${cfg.serverUrl}/api/v1/chunks").post(body)
                .header("Authorization", "Bearer ${cfg.ingestToken}")
                .apply {
                    cfg.cfClientId?.let { header("CF-Access-Client-Id", it) }
                    cfg.cfClientSecret?.let { header("CF-Access-Client-Secret", it) }
                }.build()
            try {
                http.newCall(req).execute().use { resp ->
                    val text = resp.body?.string().orEmpty()
                    when (resp.code) {
                        200 -> {
                            val got = runCatching { JSONObject(text).optString("sha256") }.getOrDefault("")
                            if (got.equals(m.getString("sha256"), ignoreCase = true)) {
                                Outbox.remove(item); sent += 1
                            } else {
                                AppState.setLastUpload(ctx, "${stamp()} server checksum differs, will retry")
                                return@withContext Result.retry()
                            }
                        }
                        400 -> return@withContext Result.retry()          // checksum mismatch in transit
                        401, 403 -> {
                            AppState.setLastUpload(ctx, "${stamp()} rejected: check tokens (${resp.code})")
                            return@withContext Result.failure()
                        }
                        409 -> AppState.setLastUpload(ctx, "${stamp()} conflict on ${item.audio.name}, kept")
                        else -> {
                            AppState.setLastUpload(ctx, "${stamp()} server error ${resp.code}, will retry")
                            return@withContext Result.retry()
                        }
                    }
                }
            } catch (e: IOException) {
                android.util.Log.w("VoiceUpload", "upload failed: ${e.message}")
                AppState.setLastUpload(ctx, "${stamp()} offline, will retry")
                AppState.pendingChunks.value = Outbox.count(ctx)
                return@withContext Result.retry()
            }
            AppState.pendingChunks.value = Outbox.count(ctx)
        }
        if (sent > 0) AppState.setLastUpload(ctx, "${stamp()} sent $sent chunk${if (sent == 1) "" else "s"}")
        // Piggyback: keep the auto-mode schedule (windows + calendar) fresh while we're online.
        if (AppState.mode(ctx) == Mode.AUTO &&
            System.currentTimeMillis() - AppState.prefs(ctx).getLong("schedule_at", 0) > 10 * 60_000L &&
            Schedule.fetch(ctx)) RecorderService.poke(ctx)
        AppState.pendingChunks.value = Outbox.count(ctx)
        Result.success()
    }
}
