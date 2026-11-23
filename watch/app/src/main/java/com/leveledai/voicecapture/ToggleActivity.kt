package com.leveledai.voicecapture

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle

/**
 * Invisible: flips recording on/off and closes. Launched by the complication tap and by the
 * "Rec toggle" launcher entry (assign it to the Home key double press). Being a visible
 * activity at that instant is what lets a microphone foreground service start on Android 14+.
 */
class ToggleActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            startActivity(Intent(this, MainActivity::class.java))   // main screen asks for the permission
        } else {
            startForegroundService(RecorderService.intent(this, RecorderService.ACTION_TOGGLE))
        }
        finish()
    }
}
