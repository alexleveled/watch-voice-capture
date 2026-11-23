package com.leveledai.voicecapture

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import java.nio.FloatBuffer
import kotlin.math.sqrt

/**
 * Silero VAD v5 (ONNX, ~2 MB, assets/silero_vad.onnx) on 32 ms frames of 16 kHz audio.
 * The model wants each 512-sample frame prefixed with the previous frame's last 64 samples,
 * plus its recurrent state carried between calls. Interface checked against the real model:
 * inputs input[1,576] float, state[2,1,128] float, sr int64 scalar; outputs output[1,1], stateN.
 */
class Vad(ctx: Context) : AutoCloseable {
    companion object {
        const val FRAME = 512
        private const val CONTEXT = 64
    }

    private val env = OrtEnvironment.getEnvironment()
    // Load from a file path, not a byte array: on the watch's 32-bit ARM, parsing from a Java
    // byte[] hits an unaligned read in ORT and dies with SIGBUS (BUS_ADRALN).
    private val session: OrtSession = env.createSession(modelFile(ctx).absolutePath,
        OrtSession.SessionOptions().apply {
            setIntraOpNumThreads(1); setInterOpNumThreads(1)
            setOptimizationLevel(OrtSession.SessionOptions.OptLevel.NO_OPT)
        })

    private fun modelFile(ctx: Context): java.io.File {
        val f = java.io.File(ctx.noBackupFilesDir, "silero_vad.onnx")
        val size = ctx.assets.openFd("silero_vad.onnx").use { it.length }
        if (!f.exists() || f.length() != size) {
            ctx.assets.open("silero_vad.onnx").use { i -> f.outputStream().use { o -> i.copyTo(o) } }
        }
        return f
    }
    private val sr = OnnxTensor.createTensor(env, 16000L)
    private var state = FloatArray(2 * 128)
    private val context = FloatArray(CONTEXT)
    private val input = FloatArray(CONTEXT + FRAME)

    fun reset() {
        state.fill(0f)
        context.fill(0f)
    }

    /** Speech probability 0..1 for one FRAME-sample frame. */
    fun prob(frame: ShortArray): Float {
        System.arraycopy(context, 0, input, 0, CONTEXT)
        for (i in 0 until FRAME) input[CONTEXT + i] = frame[i] / 32768f
        System.arraycopy(input, FRAME, context, 0, CONTEXT)   // last 64 samples become next context
        OnnxTensor.createTensor(env, FloatBuffer.wrap(input), longArrayOf(1, (CONTEXT + FRAME).toLong())).use { inT ->
            OnnxTensor.createTensor(env, FloatBuffer.wrap(state), longArrayOf(2, 1, 128)).use { stT ->
                session.run(mapOf("input" to inT, "state" to stT, "sr" to sr)).use { r ->
                    @Suppress("UNCHECKED_CAST")
                    val p = (r[0].value as Array<FloatArray>)[0][0]
                    @Suppress("UNCHECKED_CAST")
                    val sn = r[1].value as Array<Array<FloatArray>>
                    var k = 0
                    for (a in sn) for (b in a) for (v in b) state[k++] = v
                    return p
                }
            }
        }
    }

    override fun close() {
        runCatching { sr.close() }
        runCatching { session.close() }
    }
}

/** Root-mean-square level of a frame, 0..1. Cheap gate so the model only runs when there's sound. */
fun rms(frame: ShortArray, n: Int): Float {
    var acc = 0.0
    for (i in 0 until n) { val v = frame[i] / 32768.0; acc += v * v }
    return sqrt(acc / maxOf(1, n)).toFloat()
}
