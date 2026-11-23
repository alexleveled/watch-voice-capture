package com.leveledai.voicecapture

import android.app.PendingIntent
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.graphics.drawable.Icon
import androidx.wear.watchface.complications.data.ComplicationData
import androidx.wear.watchface.complications.data.ComplicationType
import androidx.wear.watchface.complications.data.MonochromaticImage
import androidx.wear.watchface.complications.data.MonochromaticImageComplicationData
import androidx.wear.watchface.complications.data.PlainComplicationText
import androidx.wear.watchface.complications.data.ShortTextComplicationData
import androidx.wear.watchface.complications.data.SmallImage
import androidx.wear.watchface.complications.data.SmallImageComplicationData
import androidx.wear.watchface.complications.data.SmallImageType
import androidx.wear.watchface.complications.datasource.ComplicationDataSourceUpdateRequester
import androidx.wear.watchface.complications.datasource.ComplicationRequest
import androidx.wear.watchface.complications.datasource.SuspendingComplicationDataSourceService

/** Watch-face slot: tap to start/stop. Dark blue dot + "REC" while recording, mic + "AUTO" while auto mode listens, mic + "Rec" when idle. */
class RecComplicationService : SuspendingComplicationDataSourceService() {

    companion object {
        fun refresh(ctx: Context) {
            ComplicationDataSourceUpdateRequester.create(ctx, ComponentName(ctx, RecComplicationService::class.java))
                .requestUpdateAll()
        }
    }

    private fun tap(): PendingIntent = PendingIntent.getActivity(this, 0,
        Intent(this, ToggleActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    private fun build(type: ComplicationType, recording: Boolean, listening: Boolean = false): ComplicationData? {
        val iconRes = if (recording) R.drawable.ic_rec else R.drawable.ic_mic
        val icon = Icon.createWithResource(this, iconRes)
        val desc = PlainComplicationText.Builder(when {
            recording -> "Recording, tap to stop"
            listening -> "Auto mode listening, tap to record now"
            else -> "Tap to record" }).build()
        val mono = MonochromaticImage.Builder(icon).build()
        return when (type) {
            ComplicationType.SHORT_TEXT -> ShortTextComplicationData.Builder(
                PlainComplicationText.Builder(when { recording -> "REC"; listening -> "AUTO"; else -> "Rec" }).build(), desc)
                .setMonochromaticImage(mono).setTapAction(tap()).build()
            ComplicationType.MONOCHROMATIC_IMAGE -> MonochromaticImageComplicationData.Builder(mono, desc)
                .setTapAction(tap()).build()
            ComplicationType.SMALL_IMAGE -> SmallImageComplicationData.Builder(
                SmallImage.Builder(icon, SmallImageType.ICON).build(), desc).setTapAction(tap()).build()
            else -> null
        }
    }

    override suspend fun onComplicationRequest(request: ComplicationRequest): ComplicationData? =
        build(request.complicationType, AppState.recording.value, AppState.listening.value)

    override fun getPreviewData(type: ComplicationType): ComplicationData? = build(type, false)
}
