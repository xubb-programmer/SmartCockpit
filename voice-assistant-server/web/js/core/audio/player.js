// Audio playback module
import { log } from '../../utils/logger.js';

const INITIAL_BUFFER_PACKETS = 3;
const START_DELAY_SECONDS = 0.03;

export class AudioPlayer {
    constructor() {
        // Opus can resample while decoding. Keeping the browser side at 16 kHz also
        // lets the recorder safely share this user-unlocked AudioContext.
        this.SAMPLE_RATE = 16000;
        this.CHANNELS = 1;
        this.FRAME_SIZE = 960;

        this.audioContext = null;
        this.analyser = null;
        this.opusDecoder = null;
        this.pendingPcmChunks = [];
        this.pendingSamples = 0;
        this.activeSources = new Set();
        this.nextStartTime = 0;
        this.playbackStarted = false;
        this.streamActive = false;
        this.streamEnded = true;
        this.streamSequence = 0;
        this.completionPromise = Promise.resolve();
        this.resolveCompletion = null;
        this.stats = this.createEmptyStats();
    }

    createEmptyStats() {
        return {
            receivedPackets: 0,
            receivedBytes: 0,
            decodedPackets: 0,
            decodedSamples: 0,
            decodeErrors: 0,
            peak: 0,
            rms: 0,
            scheduledChunks: 0,
            scheduledSamples: 0,
            completedChunks: 0,
            completedSamples: 0
        };
    }

    getAudioContext() {
        if (!this.audioContext || this.audioContext.state === 'closed') {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)({
                sampleRate: this.SAMPLE_RATE,
                latencyHint: 'interactive'
            });
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 256;
            this.analyser.connect(this.audioContext.destination);
            log(`[音频诊断][播放] AudioContext已创建，请求采样率=${this.SAMPLE_RATE}Hz，实际采样率=${this.audioContext.sampleRate}Hz，状态=${this.audioContext.state}`, 'info');
        }
        return this.audioContext;
    }

    async resume() {
        const audioContext = this.getAudioContext();
        if (audioContext.state === 'suspended') {
            await audioContext.resume();
        }
        log(`[音频诊断][播放] AudioContext状态=${audioContext.state}，实际采样率=${audioContext.sampleRate}Hz`, audioContext.state === 'running' ? 'success' : 'warning');
        return audioContext.state === 'running';
    }

    async initOpusDecoder() {
        if (this.opusDecoder) return this.opusDecoder;

        try {
            if (typeof window.ModuleInstance === 'undefined') {
                if (typeof Module !== 'undefined') {
                    window.ModuleInstance = Module;
                } else {
                    throw new Error('Opus库未加载');
                }
            }

            const mod = window.ModuleInstance;
            this.opusDecoder = {
                channels: this.CHANNELS,
                rate: this.SAMPLE_RATE,
                frameSize: this.FRAME_SIZE,
                module: mod,
                decoderPtr: null,

                init() {
                    if (this.decoderPtr) return true;
                    const decoderSize = mod._opus_decoder_get_size(this.channels);
                    this.decoderPtr = mod._malloc(decoderSize);
                    if (!this.decoderPtr) throw new Error('无法分配Opus解码器内存');

                    const result = mod._opus_decoder_init(
                        this.decoderPtr,
                        this.rate,
                        this.channels
                    );
                    if (result < 0) {
                        this.destroy();
                        throw new Error(`Opus解码器初始化失败: ${result}`);
                    }
                    return true;
                },

                decode(opusData) {
                    if (!this.decoderPtr) this.init();

                    let opusPtr = 0;
                    let pcmPtr = 0;
                    try {
                        opusPtr = mod._malloc(opusData.length);
                        pcmPtr = mod._malloc(this.frameSize * this.channels * 2);
                        if (!opusPtr || !pcmPtr) throw new Error('无法分配Opus解码缓冲区');

                        mod.HEAPU8.set(opusData, opusPtr);
                        const decodedSamples = mod._opus_decode(
                            this.decoderPtr,
                            opusPtr,
                            opusData.length,
                            pcmPtr,
                            this.frameSize,
                            0
                        );
                        if (decodedSamples < 0) {
                            throw new Error(`Opus解码失败: ${decodedSamples}`);
                        }

                        const decodedData = new Int16Array(decodedSamples);
                        decodedData.set(mod.HEAP16.subarray(
                            pcmPtr >> 1,
                            (pcmPtr >> 1) + decodedSamples
                        ));
                        return decodedData;
                    } finally {
                        if (opusPtr) mod._free(opusPtr);
                        if (pcmPtr) mod._free(pcmPtr);
                    }
                },

                destroy() {
                    if (this.decoderPtr) {
                        this.module._free(this.decoderPtr);
                        this.decoderPtr = null;
                    }
                }
            };

            this.opusDecoder.init();
            log(`[音频诊断][播放] Opus解码器已就绪，输出=${this.SAMPLE_RATE}Hz/${this.CHANNELS}声道`, 'success');
            return this.opusDecoder;
        } catch (error) {
            this.opusDecoder = null;
            log(`[音频诊断][播放] Opus解码器初始化失败: ${error.message}`, 'error');
            throw error;
        }
    }

    async start() {
        this.getAudioContext();
        await this.initOpusDecoder();
    }

    beginStream(sessionId = '') {
        if (this.streamActive && !this.streamEnded) {
            log('[音频诊断][播放] 收到新的TTS开始消息，上一段传输尚未结束', 'warning');
        }

        this.streamSequence += 1;
        this.streamActive = true;
        this.streamEnded = false;
        this.playbackStarted = this.activeSources.size > 0;
        this.stats = this.createEmptyStats();
        this.completionPromise = new Promise(resolve => {
            this.resolveCompletion = resolve;
        });

        const audioContext = this.getAudioContext();
        log(`[音频诊断][播放] TTS流开始，序号=${this.streamSequence}，会话=${sessionId || '未知'}，AudioContext=${audioContext.state}`, 'info');
    }

    enqueueAudioData(opusData) {
        if (!opusData || opusData.length === 0) {
            log('[音频诊断][播放] 忽略空音频帧', 'warning');
            return false;
        }
        if (!this.opusDecoder) {
            log('[音频诊断][播放] 收到音频时Opus解码器尚未就绪', 'error');
            this.stats.decodeErrors += 1;
            return false;
        }
        if (!this.streamActive) {
            this.beginStream('隐式');
        }

        this.stats.receivedPackets += 1;
        this.stats.receivedBytes += opusData.length;

        try {
            const pcm16 = this.opusDecoder.decode(opusData);
            if (!pcm16.length) throw new Error('解码结果为空');

            const pcm32 = new Float32Array(pcm16.length);
            let sumSquares = 0;
            let peak = 0;
            for (let i = 0; i < pcm16.length; i++) {
                const sample = pcm16[i] / 32768;
                pcm32[i] = sample;
                sumSquares += sample * sample;
                peak = Math.max(peak, Math.abs(sample));
            }

            this.stats.decodedPackets += 1;
            this.stats.decodedSamples += pcm32.length;
            this.stats.rms = Math.sqrt(sumSquares / pcm32.length);
            this.stats.peak = Math.max(this.stats.peak, peak);
            this.pendingPcmChunks.push(pcm32);
            this.pendingSamples += pcm32.length;

            if (this.stats.receivedPackets === 1 || this.stats.receivedPackets % 20 === 0) {
                log(`[音频诊断][播放] 已接收=${this.stats.receivedPackets}帧/${this.stats.receivedBytes}字节，已解码=${this.stats.decodedPackets}帧，RMS=${this.stats.rms.toFixed(4)}，峰值=${this.stats.peak.toFixed(4)}`, 'info');
            }

            this.scheduleAvailable(false);
            return true;
        } catch (error) {
            this.stats.decodeErrors += 1;
            log(`[音频诊断][播放] 第${this.stats.receivedPackets}帧Opus解码失败: ${error.message}`, 'error');
            return false;
        }
    }

    scheduleAvailable(force) {
        if (!this.pendingPcmChunks.length) return;

        const initialSamples = INITIAL_BUFFER_PACKETS * this.FRAME_SIZE;
        if (!this.playbackStarted && !force && this.pendingSamples < initialSamples) {
            return;
        }

        const audioContext = this.getAudioContext();
        if (!this.playbackStarted) {
            this.nextStartTime = Math.max(
                audioContext.currentTime + START_DELAY_SECONDS,
                this.nextStartTime
            );
            this.playbackStarted = true;
            log(`[音频诊断][播放] 开始调度，首批样本=${this.pendingSamples}，AudioContext=${audioContext.state}`, audioContext.state === 'running' ? 'success' : 'warning');
        }

        while (this.pendingPcmChunks.length) {
            const pcm = this.pendingPcmChunks.shift();
            this.pendingSamples -= pcm.length;

            const audioBuffer = audioContext.createBuffer(this.CHANNELS, pcm.length, this.SAMPLE_RATE);
            audioBuffer.copyToChannel(pcm, 0);
            const source = audioContext.createBufferSource();
            source.buffer = audioBuffer;
            source.connect(this.analyser);

            const startTime = Math.max(this.nextStartTime, audioContext.currentTime + 0.005);
            this.nextStartTime = startTime + audioBuffer.duration;
            this.activeSources.add(source);
            this.stats.scheduledChunks += 1;
            this.stats.scheduledSamples += pcm.length;

            source.onended = () => {
                this.activeSources.delete(source);
                try {
                    source.disconnect();
                } catch (error) {
                    // The source may already be disconnected during an abort.
                }
                this.stats.completedChunks += 1;
                this.stats.completedSamples += pcm.length;
                this.checkPlaybackComplete();
            };
            source.start(startTime);
        }
    }

    endStream() {
        if (!this.streamActive) return Promise.resolve();

        this.streamEnded = true;
        this.scheduleAvailable(true);
        log(`[音频诊断][播放] TTS传输结束，接收=${this.stats.receivedPackets}帧/${this.stats.receivedBytes}字节，解码失败=${this.stats.decodeErrors}，待播源=${this.activeSources.size}`, 'info');
        this.checkPlaybackComplete();
        return this.completionPromise;
    }

    checkPlaybackComplete() {
        if (!this.streamEnded || this.pendingPcmChunks.length || this.activeSources.size) {
            return;
        }

        this.streamActive = false;
        this.playbackStarted = false;
        this.nextStartTime = this.audioContext ? this.audioContext.currentTime : 0;
        log(`[音频诊断][播放] TTS已实际播放完成，完成=${this.stats.completedChunks}块/${this.stats.completedSamples}样本，峰值=${this.stats.peak.toFixed(4)}`, 'success');

        if (this.resolveCompletion) {
            const resolve = this.resolveCompletion;
            this.resolveCompletion = null;
            resolve(this.getAudioStats());
        }
    }

    getAudioStats() {
        return {
            ...this.stats,
            pendingDecode: 0,
            pendingPlay: this.pendingPcmChunks.length + this.activeSources.size,
            totalPending: this.pendingPcmChunks.length + this.activeSources.size,
            audioContextState: this.audioContext?.state || '未创建',
            audioContextSampleRate: this.audioContext?.sampleRate || 0
        };
    }

    getAnalyser() {
        return this.analyser;
    }

    clearAllAudio(reason = '中止') {
        this.pendingPcmChunks = [];
        this.pendingSamples = 0;
        this.streamEnded = true;
        this.streamActive = false;
        this.playbackStarted = false;

        for (const source of this.activeSources) {
            source.onended = null;
            try {
                source.stop();
                source.disconnect();
            } catch (error) {
                // Ignore sources that have already ended.
            }
        }
        this.activeSources.clear();
        this.nextStartTime = this.audioContext ? this.audioContext.currentTime : 0;

        if (this.resolveCompletion) {
            const resolve = this.resolveCompletion;
            this.resolveCompletion = null;
            resolve(this.getAudioStats());
        }
        log(`[音频诊断][播放] 已中止并清空音频，原因=${reason}`, 'warning');
    }
}

let audioPlayerInstance = null;

export function getAudioPlayer() {
    if (!audioPlayerInstance) {
        audioPlayerInstance = new AudioPlayer();
    }
    return audioPlayerInstance;
}
