package com.leveledai.voicecapture

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.wear.compose.foundation.lazy.ScalingLazyColumn
import androidx.wear.compose.foundation.lazy.rememberScalingLazyListState
import androidx.wear.compose.material.Button
import androidx.wear.compose.material.ButtonDefaults
import androidx.wear.compose.material.Chip
import androidx.wear.compose.material.ChipDefaults
import androidx.wear.compose.material.MaterialTheme
import androidx.wear.compose.material.Scaffold
import androidx.wear.compose.material.Text
import androidx.wear.compose.material.TimeText
import androidx.wear.compose.material.ToggleChip
import androidx.wear.compose.material.ToggleChipDefaults
import kotlinx.coroutines.delay

class MainActivity : ComponentActivity() {

    private val askPerms = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AppState.restore(this)
        Uploader.schedulePeriodic(this)
        val need = listOf(Manifest.permission.RECORD_AUDIO, Manifest.permission.POST_NOTIFICATIONS)
            .filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (need.isNotEmpty()) askPerms.launch(need.toTypedArray())
        // Opening the app (a visible activity) is the one moment Android lets us (re)start the
        // microphone service, so resume auto mode here after a reboot or update.
        if (AppState.mode(this) == Mode.AUTO && need.isEmpty()) {
            Notifications.clear(this)
            startForegroundService(RecorderService.intent(this, RecorderService.ACTION_AUTO_ON))
        }
        setContent { MaterialTheme { Screen() } }
    }

    override fun onResume() {
        super.onResume()
        AppState.restore(this)
    }

    @Composable
    private fun Screen() {
        val recording by AppState.recording.collectAsState()
        val since by AppState.recordingSince.collectAsState()
        val pending by AppState.pendingChunks.collectAsState()
        val autoStatus by AppState.autoStatus.collectAsState()
        val last by AppState.lastUpload.collectAsState()
        var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
        var mode by remember { mutableLongStateOf(if (AppState.mode(this) == Mode.AUTO) 1L else 0L) }
        var saver by remember { mutableLongStateOf(if (AppState.prefs(this).getBoolean("battery_saver", false)) 1L else 0L) }
        val configured = remember { ServerConfig.load(this) != null }
        LaunchedEffect(recording) { while (recording) { now = System.currentTimeMillis(); delay(1000) } }
        val listState = rememberScalingLazyListState()

        Scaffold(timeText = { TimeText() }) {
            ScalingLazyColumn(state = listState, modifier = Modifier.fillMaxSize(),
                horizontalAlignment = Alignment.CenterHorizontally) {
                item {
                    Box(Modifier.padding(top = 16.dp), contentAlignment = Alignment.Center) {
                        Button(
                            onClick = {
                                startForegroundService(RecorderService.intent(this@MainActivity, RecorderService.ACTION_TOGGLE))
                            },
                            modifier = Modifier.size(84.dp),
                            colors = ButtonDefaults.buttonColors(
                                backgroundColor = if (recording) Color(0xFF1E3A8A) else Color(0xFF2563EB)),
                        ) {
                            Box(Modifier.size(if (recording) 26.dp else 30.dp)
                                .clip(if (recording) androidx.compose.foundation.shape.RoundedCornerShape(4.dp) else CircleShape)
                                .background(Color.White))
                        }
                    }
                }
                item {
                    val secs = since?.let { (now - it) / 1000 } ?: 0
                    Text(if (recording) "Recording %d:%02d".format(secs / 60, secs % 60) else "Tap to record",
                        fontWeight = FontWeight.SemiBold, fontSize = 15.sp, modifier = Modifier.padding(top = 6.dp))
                }
                item {
                    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(2.dp),
                        modifier = Modifier.padding(vertical = 6.dp)) {
                        if (!configured) Text("Not set up yet", color = Color(0xFFFBBF24), fontSize = 12.sp)
                        Text(if (pending == 0) "All uploaded" else "$pending waiting to upload",
                            fontSize = 12.sp, color = Color.LightGray)
                        last?.let { Text(it, fontSize = 11.sp, color = Color.Gray, textAlign = TextAlign.Center) }
                    }
                }
                item {
                    ToggleChip(
                        checked = mode == 1L,
                        onCheckedChange = {
                            mode = if (it) 1L else 0L
                            AppState.setMode(this@MainActivity, if (it) Mode.AUTO else Mode.MANUAL)
                            startForegroundService(RecorderService.intent(this@MainActivity,
                                if (it) RecorderService.ACTION_AUTO_ON else RecorderService.ACTION_AUTO_OFF))
                        },
                        label = { Text("Auto mode") },
                        secondaryLabel = { Text(if (mode == 1L) (autoStatus ?: "Auto: starting") else "Manual: button only",
                            maxLines = 2, fontSize = 11.sp) },
                        toggleControl = { androidx.wear.compose.material.Switch(checked = mode == 1L) },
                        modifier = Modifier.fillMaxWidth(),
                        colors = ToggleChipDefaults.toggleChipColors(),
                    )
                }
                item {
                    ToggleChip(
                        checked = saver == 1L,
                        onCheckedChange = {
                            saver = if (it) 1L else 0L
                            AppState.prefs(this@MainActivity).edit().putBoolean("battery_saver", it).apply()
                            Uploader.schedulePeriodic(this@MainActivity)
                        },
                        label = { Text("Battery saver") },
                        secondaryLabel = { Text("Upload on Wi-Fi only") },
                        toggleControl = { androidx.wear.compose.material.Switch(checked = saver == 1L) },
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
                item {
                    Chip(onClick = { Uploader.kick(this@MainActivity) }, label = { Text("Upload now") },
                        colors = ChipDefaults.secondaryChipColors(), modifier = Modifier.fillMaxWidth())
                }
            }
        }
    }
}
