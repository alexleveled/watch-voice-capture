package com.leveledai.voicecapture

import android.Manifest
import android.annotation.SuppressLint
import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.os.VibrationEffect
import android.os.Vibrator
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import androidx.wear.ongoing.OngoingActivity
import androidx.wear.ongoing.Status
import org.json.JSONObject
import java.io.File
import java.time.OffsetDateTime
import java.time.temporal.ChronoUnit
import java.util.UUID

/**
 * Foreground microphone service. Three states:
 *   idle       - service not running (manual mode, nothing recording)
 *   listening  - auto mode, gate open, watch worn: mic open, Silero VAD watching, nothing written
 *   recording  - writing 120 s Opus chunks to the outbox (started by speech, or by the button)
 *
 * Auto mode keeps the service in the foreground (type microphone) even while the gate is closed,
 * because Android 14+ won't let a background alarm start a microphone service from scratch. With
 * the gate closed the mic is released, no wake lock is held, and an exact alarm wakes us at the
 * next window / meeting boundary.
 *
 * One recording = one conversation. Silence ends it (auto: silence.auto_end_min, button-started:
 * silence.manual_stop_min, 0 = never). The PC stitches recordings that are minutes apart.
 */
class RecorderService : Service(), SensorEventListener {

    companion object {
        private const val TAG = "VoiceCapture"
        const val ACTION_START = "start"
        const val ACTION_STOP = "stop"
        const val ACTION_TOGGLE = "toggle"
        const val ACTION_AUTO_ON = "auto_on"
        const val ACTION_AUTO_OFF = "auto_off"
        const val ACTION_EVAL = "eval"
        const val EXTRA_TRIGGER = "trigger"
        private const val CHANNEL = "recording"
        private const val NOTIF_ID = 1
        const val CHUNK_SECONDS = 120
        private const val PREROLL_FRAMES = 63          // ~2 s of 32 ms frames
        private const val START_WINDOW = 47            // ~1.5 s
        private const val START_SPEECH_FRAMES = 12     // ~0.4 s of speech inside the window starts a recording
        private const val SPEECH_P = 0.5f
        private const val RMS_FLOOR = 0.0025f          // below this the room is quiet: skip the model

        fun intent(ctx: Context, action: String, trigger: String = "manual") =
            Intent(ctx, RecorderService::class.java).setAction(action).putExtra(EXTRA_TRIGGER, trigger)

        /** Re-check the schedule if the service is up (e.g. after a schedule refresh). Never starts it. */
        fun poke(ctx: Context) {
            if (AppState.mode(ctx) == Mode.AUTO) runCatching { ctx.startService(intent(ctx, ACTION_EVAL)) }
        }
    }

    private val main = Handler(Looper.getMainLooper())
    private var inForeground = false
    private var wakeLock: PowerManager.WakeLock? = null
    private var thread: Thread? = null
    private var sensorManager: SensorManager? = null

    // Shared with the audio thread.
    @Volatile private var listenAllowed = false
    @Volatile private var gateTrigger = "auto"
    @Volatile private var manualStartRequested = false
    @Volatile private var stopRequested = false
    @Volatile private var shutdown = false
    @Volatile private var worn = true

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val auto = AppState.mode(this) == Mode.AUTO
        when (intent?.action) {
            ACTION_TOGGLE -> if (AppState.recording.value) userStop() else userStart()
            ACTION_START -> if (!AppState.recording.value) userStart()
            ACTION_STOP -> if (AppState.recording.value) userStop() else if (!auto) quit()
            ACTION_AUTO_ON -> { AppState.prefs(this).edit().remove("auto_paused_until").apply(); refreshScheduleThenEval() }
            ACTION_AUTO_OFF -> { listenAllowed = false; cancelAlarm(); unregisterSensor(); if (!AppState.recording.value) quit() }
            ACTION_EVAL -> refreshScheduleThenEval()
            else -> if (auto) refreshScheduleThenEval() else if (!AppState.recording.value) quit()
        }
        return if (auto) START_STICKY else START_NOT_STICKY
    }

    // ---- user actions -----------------------------------------------------------

    private fun userStart() {
        if (!goForeground()) return
        AppState.prefs(this).edit().remove("auto_paused_until").apply()
        stopRequested = false
        manualStartRequested = true
        ensureThread()
    }

    private fun userStop() {
        if (AppState.mode(this) == Mode.AUTO) {
            // A manual stop in auto mode means "not now": stay quiet until this window/meeting ends.
            AppState.prefs(this).edit().putLong("auto_paused_until", Schedule.pauseUntil(this)).apply()
        }
        stopRequested = true
        evaluate()
    }

    // ---- gate ---------------------------------------------------------------------

    private fun refreshScheduleThenEval() {
        if (!goForeground()) return
        registerSensor()
        evaluate()                                   // act on the cached schedule right away
        Thread({ if (Schedule.fetch(this)) main.post { evaluate() } }, "schedule").start()
    }

    private fun evaluate() {
        val auto = AppState.mode(this) == Mode.AUTO
        if (!auto) {
            listenAllowed = false
            AppState.autoStatus.value = null
            if (!AppState.recording.value && thread == null) quit()
            return
        }
        val g = Schedule.gate(this)
        gateTrigger = g.trigger
        listenAllowed = g.open && worn
        AppState.autoStatus.value = when {
            !worn -> "Auto: not on wrist"
            g.open -> "Auto: listening (${g.reason})"
            else -> "Auto: ${g.reason}"
        }
        // Mic closed: wake at the next boundary, and at least every 15 min to pick up setting /
        // calendar changes. (Mic open: the audio loop polls every 2 min.)
        setAlarm(minOf(g.nextChangeMs, System.currentTimeMillis() + 15 * 60_000L))
        if (listenAllowed) ensureThread()
        updateNotification()
        RecComplicationService.refresh(this)
    }

    // ---- foreground / notification -----------------------------------------------------

    private fun goForeground(): Boolean {
        if (inForeground) return true
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "no RECORD_AUDIO permission"); stopSelf(); return false
        }
        return try {
            startForeground(NOTIF_ID, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
            inForeground = true
            true
        } catch (e: Exception) {
            // Started from the background (boot, app update, killed service): Android refuses a
            // microphone service. Ask the wearer to tap, which opens the app and restarts it legally.
            Log.w(TAG, "cannot start microphone service from background: ${e.message}")
            Notifications.resumeAuto(this)
            stopSelf()
            false
        }
    }

    private fun quit() {
        listenAllowed = false
        shutdown = true
        cancelAlarm()
        unregisterSensor()
        AppState.autoStatus.value = null
        if (inForeground) stopForeground(STOP_FOREGROUND_REMOVE)
        inForeground = false
        stopSelf()
    }

    private fun updateNotification() {
        if (inForeground) getSystemService(NotificationManager::class.java).notify(NOTIF_ID, notification())
    }

    private fun notification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Recording", NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val stop = PendingIntent.getService(this, 1, intent(this, ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val rec = AppState.recording.value
        val title = if (rec) "Recording" else AppState.autoStatus.value ?: "Voice Capture"
        val b = NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(if (rec) R.drawable.ic_rec else R.drawable.ic_mic)
            .setContentTitle(title)
            .setContentText("Tap to open")
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_STOPWATCH)
            .setContentIntent(open)
        if (rec) b.addAction(R.drawable.ic_mic, "Stop", stop)
        // Ongoing Activity: shows an icon on the watch face and in recents while we run.
        val status = if (rec) Status.Builder().addTemplate("Recording #time#")
                .addPart("time", Status.StopwatchPart(SystemClock.elapsedRealtime())).build()
            else Status.Builder().addTemplate(title).build()
        OngoingActivity.Builder(this, NOTIF_ID, b)
            .setStaticIcon(if (rec) R.drawable.ic_rec else R.drawable.ic_mic)
            .setTouchIntent(open)
            .setStatus(status)
            .build()
            .apply(this)
        return b.build()
    }

    // ---- alarms --------------------------------------------------------------------

    private fun alarmIntent() = PendingIntent.getForegroundService(this, 7, intent(this, ACTION_EVAL),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    private fun setAlarm(atMs: Long) {
        val am = getSystemService(AlarmManager::class.java)
        val at = maxOf(atMs + 1000, System.currentTimeMillis() + 15_000)
        if (am.canScheduleExactAlarms()) am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, alarmIntent())
        else am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, alarmIntent())
    }

    private fun cancelAlarm() = getSystemService(AlarmManager::class.java).cancel(alarmIntent())

    // ---- on-wrist sensor -------------------------------------------------------------

    private fun registerSensor() {
        if (sensorManager != null) return
        val sm = getSystemService(SensorManager::class.java) ?: return
        val s = sm.getDefaultSensor(Sensor.TYPE_LOW_LATENCY_OFFBODY_DETECT, true) ?: run {
            Log.i(TAG, "no off-body sensor: treating the watch as always worn"); return
        }
        sensorManager = sm
        sm.registerListener(this, s, SensorManager.SENSOR_DELAY_NORMAL)
    }

    private fun unregisterSensor() {
        sensorManager?.unregisterListener(this)
        sensorManager = null
    }

    override fun onSensorChanged(event: SensorEvent) {
        val nowWorn = event.values.firstOrNull() == 1f
        if (nowWorn != worn) {
            worn = nowWorn
            Log.i(TAG, "on wrist: $worn")
            main.post { evaluate() }
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}

    // ---- audio thread ------------------------------------------------------------------

    private fun ensureThread() {
        if (thread?.isAlive == true) return
        shutdown = false
        wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "VoiceCapture:mic").apply { acquire(14 * 60 * 60 * 1000L) }
        thread = Thread({ audioLoop() }, "audio").apply { start() }
    }

    /** Called on the main thread once the audio thread has exited. */
    private fun audioThreadDone() {
        thread = null
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        if (AppState.mode(this) != Mode.AUTO) quit()
        else { evaluate(); if (listenAllowed) ensureThread() }
    }

    private inner class Session(val trigger: String, startedAt: OffsetDateTime) {
        val recId = UUID.randomUUID().toString().replace("-", "").take(20)
        val startedAt: OffsetDateTime = startedAt.truncatedTo(ChronoUnit.SECONDS)
        val manual = trigger == "manual"
        private val chunkSamples = ChunkWriter.SAMPLE_RATE.toLong() * CHUNK_SECONDS
        private var idx = 0
        private var chunkStart = this.startedAt
        private var writer: ChunkWriter = newWriter()
        // A finished chunk is committed only once the next one has audio, so the final chunk can
        // always carry is_last=true even when the stop lands right on a boundary.
        private var held: Pair<ChunkWriter, JSONObject>? = null
        private val bytes = ByteArray(Vad.FRAME * 2)
        private val deviceId = ServerConfig.load(this@RecorderService)?.deviceId ?: "galaxy-watch"

        private fun newWriter() = ChunkWriter.create(File(Outbox.dir(this@RecorderService), "${recId}__${"%05d".format(idx)}"))

        private fun meta(i: Int, w: ChunkWriter, start: OffsetDateTime) = JSONObject().apply {
            put("recording_id", recId); put("idx", i); put("is_last", false)
            put("started_at", startedAt.toString()); put("chunk_started_at", start.toString())
            put("duration_s", w.durationS); put("trigger", trigger); put("device_id", deviceId)
        }

        fun write(frame: ShortArray, n: Int) {
            for (i in 0 until n) { bytes[2 * i] = (frame[i].toInt() and 0xff).toByte(); bytes[2 * i + 1] = (frame[i].toInt() shr 8).toByte() }
            writer.write(bytes, n * 2)
            held?.let { (hw, hm) ->
                Outbox.commit(hw.file, hm); held = null
                AppState.pendingChunks.value = Outbox.count(this@RecorderService)
                Uploader.kick(this@RecorderService)
            }
            if (writer.samplesWritten >= chunkSamples) {
                if (writer.finish()) held = writer to meta(idx, writer, chunkStart)
                idx += 1
                chunkStart = OffsetDateTime.now().truncatedTo(ChronoUnit.SECONDS)
                writer = newWriter()
            }
        }

        fun finish() {
            val w = writer
            if (w.samplesWritten > 0 && w.finish()) {
                held?.let { (hw, hm) -> Outbox.commit(hw.file, hm) }
                Outbox.commit(w.file, meta(idx, w, chunkStart).put("is_last", true))
            } else {
                w.finish()
                held?.let { (hw, hm) -> Outbox.commit(hw.file, hm.put("is_last", true)) }
            }
            AppState.pendingChunks.value = Outbox.count(this@RecorderService)
        }
    }

    @SuppressLint("MissingPermission")
    private fun audioLoop() {
        val rate = ChunkWriter.SAMPLE_RATE
        val minBuf = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = AudioRecord(MediaRecorder.AudioSource.MIC, rate, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf * 4, rate))
        val vad = runCatching { Vad(this) }.onFailure { Log.e(TAG, "VAD load failed", it) }.getOrNull()
        val preroll = ArrayDeque<ShortArray>()
        val recent = BooleanArray(START_WINDOW)
        var recentPos = 0
        var recentCount = 0
        var speechRun = 0
        var quietFrames = 0
        var session: Session? = null
        var lastSpeechMs = 0L
        var silenceLimitMs = Long.MAX_VALUE
        // While the mic is open the CPU is awake anyway: check the PC for setting changes every
        // 2 min so dashboard edits reach the watch almost right away.
        var lastFetchMs = SystemClock.elapsedRealtime()
        val fetching = java.util.concurrent.atomic.AtomicBoolean(false)

        fun startSession(trigger: String) {
            val prerollMs = preroll.size * Vad.FRAME * 1000L / rate
            val s = Session(trigger, OffsetDateTime.now().minusNanos(prerollMs * 1_000_000))
            preroll.forEach { s.write(it, it.size) }
            preroll.clear()
            session = s
            lastSpeechMs = SystemClock.elapsedRealtime()
            val mins = Schedule.silenceMinutes(this, s.manual)
            silenceLimitMs = if (mins <= 0) Long.MAX_VALUE else mins * 60_000L
            Log.i(TAG, "recording ${s.recId} ($trigger, silence stop ${mins} min, preroll ${prerollMs} ms)")
            main.post {
                AppState.recording.value = true
                AppState.recordingSince.value = System.currentTimeMillis() - prerollMs
                buzz(start = true)
                updateNotification()
                RecComplicationService.refresh(this)
            }
        }

        fun endSession(why: String) {
            val s = session ?: return
            s.finish()
            session = null
            Log.i(TAG, "recording ${s.recId} ended: $why")
            main.post {
                AppState.recording.value = false
                AppState.recordingSince.value = null
                buzz(start = false)
                Uploader.kick(this)
                updateNotification()
                RecComplicationService.refresh(this)
            }
        }

        try {
            rec.startRecording()
            if (rec.recordingState != AudioRecord.RECORDSTATE_RECORDING) throw IllegalStateException("mic busy")
            main.post { AppState.listening.value = true; RecComplicationService.refresh(this) }
            while (true) {
                if (manualStartRequested) {
                    manualStartRequested = false
                    if (session != null && session!!.manual.not()) {
                        // Button pressed while auto was already recording: keep going, it's now a manual one.
                        silenceLimitMs = Schedule.silenceMinutes(this, true).let { if (it <= 0) Long.MAX_VALUE else it * 60_000L }
                    } else if (session == null) startSession("manual")
                }
                if (stopRequested) {
                    stopRequested = false
                    endSession("stopped by button")
                }
                if (session == null && (!listenAllowed || shutdown)) break

                val frame = ShortArray(Vad.FRAME)
                var got = 0
                while (got < Vad.FRAME) {
                    val n = rec.read(frame, got, Vad.FRAME - got)
                    if (n < 0) throw IllegalStateException("AudioRecord read error $n")
                    got += n
                }

                if (SystemClock.elapsedRealtime() - lastFetchMs >= 120_000 && fetching.compareAndSet(false, true)) {
                    lastFetchMs = SystemClock.elapsedRealtime()
                    Thread({
                        try { if (Schedule.fetch(this)) main.post { evaluate() } } finally { fetching.set(false) }
                    }, "schedule").start()
                }

                // Speech probability; the model only runs when there is some sound.
                val level = rms(frame, got)
                val p = if (vad != null && level >= RMS_FLOOR) { quietFrames = 0; vad.prob(frame) }
                        else { if (++quietFrames == 30) vad?.reset(); 0f }
                val speech = p >= SPEECH_P
                speechRun = if (speech) speechRun + 1 else 0

                val s = session
                if (s != null) {
                    s.write(frame, got)
                    val now = SystemClock.elapsedRealtime()
                    if (speechRun >= 3) lastSpeechMs = now
                    if (now - lastSpeechMs >= silenceLimitMs) endSession("silence")
                } else {
                    // Listening: keep ~2 s of pre-roll and look for sustained speech.
                    preroll.addLast(frame)
                    if (preroll.size > PREROLL_FRAMES) preroll.removeFirst()
                    if (recent[recentPos]) recentCount--
                    recent[recentPos] = speech
                    if (speech) recentCount++
                    recentPos = (recentPos + 1) % START_WINDOW
                    if (recentCount >= START_SPEECH_FRAMES && listenAllowed) {
                        recent.fill(false); recentCount = 0
                        startSession(gateTrigger)
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "audio loop stopped: ${e.message}", e)
        } finally {
            runCatching { endSession("audio loop exit") }
            runCatching { rec.stop() }
            rec.release()
            vad?.close()
            main.post {
                AppState.listening.value = false
                RecComplicationService.refresh(this)
                audioThreadDone()
            }
        }
    }

    private fun buzz(start: Boolean) {
        val v = getSystemService(Vibrator::class.java) ?: return
        val effect = if (start) VibrationEffect.createWaveform(longArrayOf(0, 60, 80, 60), -1)
                     else VibrationEffect.createOneShot(250, VibrationEffect.DEFAULT_AMPLITUDE)
        v.vibrate(effect)
    }

    override fun onDestroy() {
        shutdown = true
        listenAllowed = false
        unregisterSensor()
        super.onDestroy()
    }
}
