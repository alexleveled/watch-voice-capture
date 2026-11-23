package com.leveledai.voicecapture

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat

object Notifications {
    private const val CHANNEL = "auto_resume"
    private const val ID = 2

    /** Tapping opens the app, and the app (a visible activity) is allowed to restart the mic service. */
    fun resumeAuto(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Auto mode", NotificationManager.IMPORTANCE_DEFAULT))
        val open = PendingIntent.getActivity(ctx, 3, Intent(ctx, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        nm.notify(ID, NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(R.drawable.ic_mic)
            .setContentTitle("Auto mode is paused")
            .setContentText("Tap to turn auto recording back on")
            .setAutoCancel(true)
            .setContentIntent(open)
            .build())
    }

    fun clear(ctx: Context) = ctx.getSystemService(NotificationManager::class.java).cancel(ID)
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (AppState.mode(ctx) == Mode.AUTO) Notifications.resumeAuto(ctx)
    }
}
