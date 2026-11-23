package com.leveledai.voicecapture

import android.content.Context
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.time.LocalDate
import java.time.LocalTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZonedDateTime
import java.util.concurrent.TimeUnit

/**
 * Auto-mode schedule. The PC is the source of truth (dashboard Settings + Google Calendar); the
 * watch caches the last `/api/v1/schedule` reply so auto mode keeps working offline.
 */
object Schedule {
    private const val TAG = "VoiceSchedule"
    private val http = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS).readTimeout(30, TimeUnit.SECONDS).build()
    private val DAYS = arrayOf("mon", "tue", "wed", "thu", "fri", "sat", "sun")

    /** Is auto listening allowed right now, why, and when does that answer next change. */
    data class Gate(val open: Boolean, val trigger: String, val reason: String, val nextChangeMs: Long)

    fun cached(ctx: Context): JSONObject? =
        AppState.prefs(ctx).getString("schedule_json", null)?.let { runCatching { JSONObject(it) }.getOrNull() }

    /** Blocking network call: never on the main thread. Returns true when a fresh schedule was stored. */
    fun fetch(ctx: Context): Boolean {
        val cfg = ServerConfig.load(ctx) ?: return false
        val req = Request.Builder().url("${cfg.serverUrl}/api/v1/schedule")
            .header("Authorization", "Bearer ${cfg.ingestToken}")
            .apply {
                cfg.cfClientId?.let { header("CF-Access-Client-Id", it) }
                cfg.cfClientSecret?.let { header("CF-Access-Client-Secret", it) }
            }.build()
        return try {
            http.newCall(req).execute().use { r ->
                val body = r.body?.string().orEmpty()
                if (r.code != 200) { Log.w(TAG, "schedule HTTP ${r.code}"); return false }
                JSONObject(body)  // validate before storing
                AppState.prefs(ctx).edit().putString("schedule_json", body)
                    .putLong("schedule_at", System.currentTimeMillis()).apply()
                true
            }
        } catch (e: Exception) {
            Log.w(TAG, "schedule fetch failed: ${e.message}")
            false
        }
    }

    fun silenceMinutes(ctx: Context, manual: Boolean): Int {
        val s = cached(ctx)?.optJSONObject("silence")
        return if (manual) s?.optInt("manual_stop_min", 10) ?: 10 else maxOf(1, s?.optInt("auto_end_min", 2) ?: 2)
    }

    fun gate(ctx: Context, now: ZonedDateTime = ZonedDateTime.now()): Gate {
        val nowMs = now.toInstant().toEpochMilli()
        val pausedUntil = AppState.prefs(ctx).getLong("auto_paused_until", 0L)
        val sched = cached(ctx) ?: return Gate(false, "auto", "waiting for schedule", nowMs + 10 * 60_000)
        val auto = sched.optJSONObject("auto") ?: JSONObject()
        val boundaries = mutableListOf<Long>()
        var open: Pair<String, String>? = null   // trigger to reason

        // Weekly windows, local watch time; today and tomorrow give every boundary in the next 24 h.
        // Windows are optional: with none set on any day, auto mode listens whenever the watch is worn.
        val windows = auto.optJSONObject("windows")
        val anyWindow = DAYS.any { (windows?.optJSONArray(it)?.length() ?: 0) > 0 }
        if (auto.optBoolean("always_while_worn", false) || !anyWindow) open = "auto" to "whenever worn"

        for (dayOffset in 0..1) {
            val date: LocalDate = now.toLocalDate().plusDays(dayOffset.toLong())
            val list = windows?.optJSONArray(DAYS[date.dayOfWeek.value - 1]) ?: continue
            for (i in 0 until list.length()) {
                val w = list.optJSONArray(i) ?: continue
                val start = runCatching { ZonedDateTime.of(date, LocalTime.parse(w.getString(0)), now.zone) }.getOrNull() ?: continue
                val end = runCatching { ZonedDateTime.of(date, LocalTime.parse(w.getString(1)), now.zone) }.getOrNull() ?: continue
                boundaries += start.toInstant().toEpochMilli(); boundaries += end.toInstant().toEpochMilli()
                if (!now.isBefore(start) && now.isBefore(end) && open == null) open = "auto" to "window until ${clock(w.getString(1))}"
            }
        }

        // Calendar meetings (the PC already dropped all-day, declined and solo events).
        val cal = auto.optJSONObject("calendar")
        if (cal?.optBoolean("enabled", true) != false) {
            val lead = (cal?.optInt("lead_min", 2) ?: 2).toLong()
            val meetings = sched.optJSONArray("meetings")
            for (i in 0 until (meetings?.length() ?: 0)) {
                val m = meetings!!.optJSONObject(i) ?: continue
                val s = runCatching { OffsetDateTime.parse(m.getString("start")).minusMinutes(lead) }.getOrNull() ?: continue
                val e = runCatching { OffsetDateTime.parse(m.getString("end")) }.getOrNull() ?: continue
                boundaries += s.toInstant().toEpochMilli(); boundaries += e.toInstant().toEpochMilli()
                val n = now.toOffsetDateTime()
                if (!n.isBefore(s) && n.isBefore(e)) {
                    val until = hhmm(e.toInstant().toEpochMilli())
                    open = "calendar" to "meeting until $until"   // a meeting wins: it labels the recording
                }
            }
        }

        val upcoming = boundaries.filter { it > nowMs }.minOrNull()
        val next = upcoming ?: (nowMs + 6 * 60 * 60_000L)   // nothing scheduled: just re-check later
        if (pausedUntil > nowMs && open != null) {
            return Gate(false, open.first, "paused", minOf(next, pausedUntil))
        }
        return if (open != null) Gate(true, open.first, open.second, next)
               else Gate(false, "auto", if (upcoming == null) "off, no window or meeting today" else "off until ${hhmm(next)}", next)
    }

    /** End of the current auto period, used to pause auto mode after a manual stop. */
    fun pauseUntil(ctx: Context): Long {
        val g = gate(ctx.also { AppState.prefs(it).edit().remove("auto_paused_until").apply() })
        val now = System.currentTimeMillis()
        val a = cached(ctx)?.optJSONObject("auto")
        val noWindows = DAYS.none { (a?.optJSONObject("windows")?.optJSONArray(it)?.length() ?: 0) > 0 }
        val alwaysOn = a?.optBoolean("always_while_worn", false) == true || noWindows
        return if (alwaysOn) now + 60 * 60_000L else minOf(g.nextChangeMs, now + 4 * 60 * 60_000L)
    }

    private val CLOCK = java.time.format.DateTimeFormatter.ofPattern("h:mm a", java.util.Locale.US)

    fun hhmm(ms: Long): String =
        java.time.Instant.ofEpochMilli(ms).atZone(ZoneId.systemDefault()).format(CLOCK)

    private fun clock(hhmm24: String): String =
        runCatching { LocalTime.parse(hhmm24).format(CLOCK) }.getOrDefault(hhmm24)
}
