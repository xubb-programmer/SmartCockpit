import time
import os
import numpy as np
import opuslib_next
import onnxruntime
from config.logger import setup_logging
from core.providers.vad.base import VADProviderBase
from core.utils.npu_priority import signal_text_priority

TAG = __name__
logger = setup_logging()


class VADProvider(VADProviderBase):
    def __init__(self, config):
        logger.bind(tag=TAG).info("SileroVAD", config)

        model_path = os.path.join(
            config["model_dir"], "src", "silero_vad", "data", "silero_vad.onnx"
        )
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(
            model_path, providers=["CPUExecutionProvider"], sess_options=opts
        )

        threshold = config.get("threshold", "0.5")
        threshold_low = config.get("threshold_low", "0.2")
        min_silence_duration_ms = config.get("min_silence_duration_ms", "1000")

        self.vad_threshold = float(threshold) if threshold else 0.5
        self.vad_threshold_low = float(threshold_low) if threshold_low else 0.2

        self.silence_threshold_ms = (
            int(min_silence_duration_ms) if min_silence_duration_ms else 1000
        )

        self.frame_window_threshold = 3

    def _init_connection_state(self, conn):
        """为连接初始化独立的 VAD 状态"""
        if not hasattr(conn, "_vad_opus_decoder"):
            conn._vad_opus_decoder = opuslib_next.Decoder(16000, 1)
        if not hasattr(conn, "_vad_state"):
            conn._vad_state = np.zeros((2, 1, 128), dtype=np.float32)
        if not hasattr(conn, "_vad_context"):
            conn._vad_context = np.zeros((1, 64), dtype=np.float32)

    def release_conn_resources(self, conn):
        """释放连接的 VAD 资源（连接关闭时调用）"""
        for attr in ("_vad_opus_decoder", "_vad_state", "_vad_context"):
            if hasattr(conn, attr):
                try:
                    delattr(conn, attr)
                except Exception:
                    pass

    def is_vad(self, conn, opus_packet):
        # 手动模式：直接返回True，不进行实时VAD检测，所有音频都缓存
        if conn.client_listen_mode == "manual":
            return True

        try:
            self._init_connection_state(conn)

            pcm_frame = conn._vad_opus_decoder.decode(opus_packet, 960)
            conn.client_audio_buffer.extend(pcm_frame)

            client_have_voice = False
            while len(conn.client_audio_buffer) >= 512 * 2:
                chunk = conn.client_audio_buffer[: 512 * 2]
                conn.client_audio_buffer = conn.client_audio_buffer[512 * 2 :]

                audio_int16 = np.frombuffer(chunk, dtype=np.int16)
                audio_float32 = audio_int16.astype(np.float32) / 32768.0
                pcm_rms = float(np.sqrt(np.mean(np.square(audio_float32))))
                pcm_peak = float(np.max(np.abs(audio_float32)))
                audio_input = np.concatenate(
                    [conn._vad_context, audio_float32.reshape(1, -1)], axis=1
                ).astype(np.float32)

                ort_inputs = {
                    "input": audio_input,
                    "state": conn._vad_state,
                    "sr": np.array(16000, dtype=np.int64),
                }
                out, state = self.session.run(None, ort_inputs)

                conn._vad_state = state
                conn._vad_context = audio_input[:, -64:]
                speech_prob = out.item()

                # 双阈值判断
                if speech_prob >= self.vad_threshold:
                    is_voice = True
                elif speech_prob <= self.vad_threshold_low:
                    is_voice = False
                else:
                    is_voice = conn.last_is_voice

                previous_is_voice = conn.last_is_voice
                # 声音没低于最低值则延续前一个状态，判断为有声音
                conn.last_is_voice = is_voice

                # 更新滑动窗口
                conn.client_voice_window.append(is_voice)
                client_have_voice = (
                    conn.client_voice_window.count(True) >= self.frame_window_threshold
                )

                conn.vad_pcm_chunks = getattr(conn, "vad_pcm_chunks", 0) + 1
                conn.vad_pcm_peak = max(getattr(conn, "vad_pcm_peak", 0.0), pcm_peak)
                if (
                    conn.vad_pcm_chunks == 1
                    or conn.vad_pcm_chunks % 1000 == 0
                    or previous_is_voice != is_voice
                ):
                    logger.bind(tag=TAG).info(
                        f"[音频诊断][VAD] PCM块={conn.vad_pcm_chunks}，"
                        f"RMS={pcm_rms:.4f}，峰值={pcm_peak:.4f}，"
                        f"语音概率={speech_prob:.3f}，判定={'有声' if is_voice else '静音'}"
                    )

                # 如果之前有声音，但本次没有声音，且与上次有声音的时间差已经超过了静默阈值，则认为已经说完一句话
                if conn.client_have_voice and not client_have_voice:
                    stop_duration = time.time() * 1000 - conn.vad_last_voice_time
                    if stop_duration >= self.silence_threshold_ms:
                        conn.client_voice_stop = True
                        now = time.monotonic()
                        conn.current_vad_completed_at = now
                        conn.current_speech_ended_at = now - stop_duration / 1000.0
                if client_have_voice:
                    if not conn.client_have_voice:
                        conn.current_speech_started_at = time.monotonic()
                        conn.current_speech_ended_at = None
                        conn.current_vad_completed_at = None
                        signal_text_priority(
                            getattr(conn, "session_id", "voice"),
                            ttl_seconds=4.0,
                        )
                    conn.client_have_voice = True
                    conn.vad_last_voice_time = time.time() * 1000

            return client_have_voice
        except opuslib_next.OpusError as e:
            logger.bind(tag=TAG).info(f"解码错误: {e}")
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error processing audio packet: {e}")
