package com.leveledai.voicecapture

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import java.io.File

/**
 * One audio chunk: 16 kHz mono PCM in -> Opus in an Ogg file out (MediaCodec + MediaMuxer, API 29+).
 * Falls back to AAC in .m4a if the device has no Opus encoder. The Galaxy Watch7 has
 * c2.android.opus.encoder, so Opus is the normal path (~24 kbps, about 11 MB per hour).
 */
class ChunkWriter private constructor(val file: File, private val mime: String, muxFormat: Int,
                                      private val sampleRate: Int, bitrate: Int) {

    companion object {
        const val SAMPLE_RATE = 16000

        fun create(base: File, sampleRate: Int = SAMPLE_RATE): ChunkWriter {
            return try {
                ChunkWriter(File(base.path + ".ogg"), MediaFormat.MIMETYPE_AUDIO_OPUS,
                    MediaMuxer.OutputFormat.MUXER_OUTPUT_OGG, sampleRate, 24_000)
            } catch (e: Exception) {
                ChunkWriter(File(base.path + ".m4a"), MediaFormat.MIMETYPE_AUDIO_AAC,
                    MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4, sampleRate, 32_000)
            }
        }
    }

    private val codec: MediaCodec = MediaCodec.createEncoderByType(mime)
    private val muxer: MediaMuxer
    private val info = MediaCodec.BufferInfo()
    private var track = -1
    private var muxing = false
    private var samplesIn = 0L
    var samplesWritten = 0L
        private set

    init {
        val fmt = MediaFormat.createAudioFormat(mime, sampleRate, 1).apply {
            setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 16 * 1024)
            if (mime == MediaFormat.MIMETYPE_AUDIO_AAC)
                setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
        }
        try {
            codec.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            codec.start()
            muxer = MediaMuxer(file.path, muxFormat)
        } catch (e: Exception) {
            runCatching { codec.release() }
            file.delete()
            throw e
        }
    }

    val ext: String get() = file.extension
    val durationS: Double get() = samplesWritten.toDouble() / sampleRate

    /** pcm: little-endian 16-bit mono samples, `len` bytes. */
    fun write(pcm: ByteArray, len: Int) {
        var off = 0
        while (off < len) {
            val inIdx = codec.dequeueInputBuffer(10_000)
            if (inIdx >= 0) {
                val buf = codec.getInputBuffer(inIdx)!!
                buf.clear()
                val n = minOf(buf.remaining(), len - off)
                buf.put(pcm, off, n)
                val ptsUs = samplesIn * 1_000_000L / sampleRate
                codec.queueInputBuffer(inIdx, 0, n, ptsUs, 0)
                samplesIn += n / 2
                off += n
            }
            drain(false)
        }
        samplesWritten = samplesIn
    }

    private fun drain(eos: Boolean) {
        while (true) {
            val outIdx = codec.dequeueOutputBuffer(info, if (eos) 10_000 else 0)
            when {
                outIdx == MediaCodec.INFO_TRY_AGAIN_LATER -> if (!eos) return
                outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    track = muxer.addTrack(codec.outputFormat)
                    muxer.start()
                    muxing = true
                }
                outIdx >= 0 -> {
                    val buf = codec.getOutputBuffer(outIdx)!!
                    val config = info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0
                    if (!config && info.size > 0 && muxing) {
                        buf.position(info.offset)
                        buf.limit(info.offset + info.size)
                        muxer.writeSampleData(track, buf, info)
                    }
                    codec.releaseOutputBuffer(outIdx, false)
                    if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) return
                }
            }
        }
    }

    /** Flush and close. Returns false (and deletes the file) if nothing was recorded. */
    fun finish(): Boolean {
        var ok = samplesIn > 0
        try {
            if (ok) {
                var inIdx: Int
                do { inIdx = codec.dequeueInputBuffer(10_000); if (inIdx < 0) drain(false) } while (inIdx < 0)
                codec.queueInputBuffer(inIdx, 0, 0, samplesIn * 1_000_000L / sampleRate,
                    MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                drain(true)
            }
        } catch (e: Exception) {
            ok = false
        } finally {
            runCatching { codec.stop() }
            runCatching { codec.release() }
            if (muxing) runCatching { muxer.stop() }.onFailure { ok = false }
            runCatching { muxer.release() }
        }
        if (!ok || !muxing) file.delete()
        return ok && muxing
    }
}
