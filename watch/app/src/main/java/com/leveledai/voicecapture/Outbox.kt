package com.leveledai.voicecapture

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

/**
 * Finished chunks waiting for upload: filesDir/outbox/<recordingId>__<idx>.ogg + a .json sidecar.
 * A chunk is deleted only after the server confirms it stored the same SHA-256.
 */
object Outbox {
    fun dir(ctx: Context): File = File(ctx.filesDir, "outbox").apply { mkdirs() }

    data class Item(val audio: File, val meta: JSONObject)

    fun audioFile(ctx: Context, recordingId: String, idx: Int, ext: String) =
        File(dir(ctx), "${recordingId}__${"%05d".format(idx)}.$ext")

    /** Write the sidecar last: a chunk without a sidecar is still being written and is ignored. */
    fun commit(audio: File, meta: JSONObject) {
        meta.put("sha256", sha256(audio))
        val tmp = File(audio.path + ".json.tmp")
        tmp.writeText(meta.toString())
        tmp.renameTo(File(audio.path + ".json"))
    }

    fun pending(ctx: Context): List<Item> =
        dir(ctx).listFiles { f -> f.name.endsWith(".json") && !f.name.endsWith(".tmp") }
            .orEmpty()
            .sortedBy { it.name }
            .mapNotNull { side ->
                val audio = File(side.path.removeSuffix(".json"))
                if (!audio.exists()) { side.delete(); null }
                else runCatching { Item(audio, JSONObject(side.readText())) }.getOrNull()
            }

    fun count(ctx: Context) = pending(ctx).size

    fun remove(item: Item) {
        item.audio.delete()
        File(item.audio.path + ".json").delete()
    }

    fun sha256(f: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        f.inputStream().use { s ->
            val buf = ByteArray(64 * 1024)
            while (true) {
                val n = s.read(buf)
                if (n <= 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }
}
