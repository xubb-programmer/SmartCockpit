// Audio recording module
import { log } from '../../utils/logger.js';
import { initOpusEncoder } from './opus-codec.js';
import { getAudioPlayer } from './player.js';

// Audio recorder class
export class AudioRecorder {
    constructor() {
        this.isRecording = false;
        this.audioContext = null;
        this.analyser = null;
        this.audioProcessor = null;
        this.audioProcessorType = null;
        this.audioSource = null;
        this.mediaStream = null;
        this.processorOutput = null;
        this.opusEncoder = null;
        this.pcmDataBuffer = new Int16Array();
        this.totalAudioSize = 0;
        this.visualizationRequest = null;
        this.recordingTimer = null;
        this.websocket = null;
        this.isStopping = false;
        this.workletModuleLoaded = false;
        this.stopResolver = null;
        this.stats = this.createEmptyStats();
        // Callback functions
        this.onRecordingStart = null;
        this.onRecordingStop = null;
        this.onVisualizerUpdate = null;
    }

    createEmptyStats() {
        return {
            pcmFrames: 0,
            pcmSamples: 0,
            rms: 0,
            peak: 0,
            encodedFrames: 0,
            encodedBytes: 0,
            sentFrames: 0,
            sentBytes: 0,
            sendErrors: 0
        };
    }

    // Set WebSocket instance
    setWebSocket(ws) {
        this.websocket = ws;
    }

    // Get AudioContext instance
    getAudioContext() {
        return getAudioPlayer().getAudioContext();
    }

    // Initialize encoder
    initEncoder() {
        if (!this.opusEncoder) {
            this.opusEncoder = initOpusEncoder();
        }
        return this.opusEncoder;
    }

    // PCM processor code
    getAudioProcessorCode() {
        return `
            class AudioRecorderProcessor extends AudioWorkletProcessor {
                constructor() {
                    super();
                    this.buffers = [];
                    this.frameSize = 960;
                    this.buffer = new Int16Array(this.frameSize);
                    this.bufferIndex = 0;
                    this.isRecording = false;
                    this.port.onmessage = (event) => {
                        if (event.data.command === 'start') {
                            this.isRecording = true;
                            this.port.postMessage({ type: 'status', status: 'started' });
                        } else if (event.data.command === 'stop') {
                            this.isRecording = false;
                            if (this.bufferIndex > 0) {
                                const finalBuffer = this.buffer.slice(0, this.bufferIndex);
                                this.port.postMessage({ type: 'buffer', buffer: finalBuffer });
                                this.bufferIndex = 0;
                            }
                            this.port.postMessage({ type: 'status', status: 'stopped' });
                        }
                    };
                }
                process(inputs, outputs, parameters) {
                    if (!this.isRecording) return true;
                    const input = inputs[0][0];
                    if (!input) return true;
                    for (let i = 0; i < input.length; i++) {
                        if (this.bufferIndex >= this.frameSize) {
                            this.port.postMessage({ type: 'buffer', buffer: this.buffer.slice(0) });
                            this.bufferIndex = 0;
                        }
                        this.buffer[this.bufferIndex++] = Math.max(-32768, Math.min(32767, Math.floor(input[i] * 32767)));
                    }
                    return true;
                }
            }
            registerProcessor('audio-recorder-processor', AudioRecorderProcessor);
        `;
    }

    // Create audio processor
    async createAudioProcessor() {
        this.audioContext = this.getAudioContext();
        try {
            if (this.audioContext.audioWorklet) {
                if (!this.workletModuleLoaded) {
                    const blob = new Blob([this.getAudioProcessorCode()], { type: 'application/javascript' });
                    const url = URL.createObjectURL(blob);
                    try {
                        await this.audioContext.audioWorklet.addModule(url);
                        this.workletModuleLoaded = true;
                    } finally {
                        URL.revokeObjectURL(url);
                    }
                }
                const audioProcessor = new AudioWorkletNode(this.audioContext, 'audio-recorder-processor');
                audioProcessor.port.onmessage = (event) => {
                    if (event.data.type === 'buffer') {
                        this.processPCMBuffer(event.data.buffer);
                    } else if (event.data.type === 'status' && event.data.status === 'stopped') {
                        if (this.stopResolver) {
                            this.stopResolver();
                            this.stopResolver = null;
                        }
                    }
                };
                log('使用AudioWorklet处理音频', 'success');
                const silent = this.audioContext.createGain();
                silent.gain.value = 0;
                audioProcessor.connect(silent);
                silent.connect(this.audioContext.destination);
                this.processorOutput = silent;
                return { node: audioProcessor, type: 'worklet' };
            } else {
                log('AudioWorklet不可用，使用ScriptProcessorNode作为后备方案', 'warning');
                return this.createScriptProcessor();
            }
        } catch (error) {
            log(`创建音频处理器失败: ${error.message}，尝试后备方案`, 'error');
            return this.createScriptProcessor();
        }
    }

    // Create ScriptProcessor as fallback
    createScriptProcessor() {
        try {
            const frameSize = 4096;
            const scriptProcessor = this.audioContext.createScriptProcessor(frameSize, 1, 1);
            scriptProcessor.onaudioprocess = (event) => {
                if (!this.isRecording) return;
                const input = event.inputBuffer.getChannelData(0);
                const buffer = new Int16Array(input.length);
                for (let i = 0; i < input.length; i++) {
                    buffer[i] = Math.max(-32768, Math.min(32767, Math.floor(input[i] * 32767)));
                }
                this.processPCMBuffer(buffer);
            };
            const silent = this.audioContext.createGain();
            silent.gain.value = 0;
            scriptProcessor.connect(silent);
            silent.connect(this.audioContext.destination);
            this.processorOutput = silent;
            log('使用ScriptProcessorNode作为后备方案成功', 'warning');
            return { node: scriptProcessor, type: 'processor' };
        } catch (fallbackError) {
            log(`后备方案也失败: ${fallbackError.message}`, 'error');
            return null;
        }
    }

    // Process PCM buffer data
    processPCMBuffer(buffer) {
        if (!this.isRecording && !this.isStopping) return;
        const newBuffer = new Int16Array(this.pcmDataBuffer.length + buffer.length);
        newBuffer.set(this.pcmDataBuffer);
        newBuffer.set(buffer, this.pcmDataBuffer.length);
        this.pcmDataBuffer = newBuffer;
        const samplesPerFrame = 960;
        while (this.pcmDataBuffer.length >= samplesPerFrame) {
            const frameData = this.pcmDataBuffer.slice(0, samplesPerFrame);
            this.pcmDataBuffer = this.pcmDataBuffer.slice(samplesPerFrame);
            this.recordPcmStats(frameData);
            this.encodeAndSendOpus(frameData);
        }
    }

    recordPcmStats(pcmData) {
        let sumSquares = 0;
        let peak = 0;
        for (let i = 0; i < pcmData.length; i++) {
            const sample = pcmData[i] / 32768;
            sumSquares += sample * sample;
            peak = Math.max(peak, Math.abs(sample));
        }

        this.stats.pcmFrames += 1;
        this.stats.pcmSamples += pcmData.length;
        this.stats.rms = Math.sqrt(sumSquares / pcmData.length);
        this.stats.peak = Math.max(this.stats.peak, peak);
        if (this.stats.pcmFrames === 1) {
            log(`[音频诊断][麦克风] PCM=${this.stats.pcmFrames}帧/${this.stats.pcmSamples}样本，RMS=${this.stats.rms.toFixed(4)}，峰值=${this.stats.peak.toFixed(4)}`, 'info');
        } else if (this.stats.pcmFrames % 500 === 0) {
            console.info(`[音频诊断][麦克风] PCM=${this.stats.pcmFrames}帧/${this.stats.pcmSamples}样本，RMS=${this.stats.rms.toFixed(4)}，峰值=${this.stats.peak.toFixed(4)}`);
        }
    }

    // Encode and send Opus data
    encodeAndSendOpus(pcmData = null) {
        if (!this.opusEncoder) {
            log('Opus编码器未初始化', 'error');
            return;
        }
        try {
            if (pcmData) {
                const opusData = this.opusEncoder.encode(pcmData);
                if (opusData && opusData.length > 0) {
                    this.stats.encodedFrames += 1;
                    this.stats.encodedBytes += opusData.length;
                    this.totalAudioSize += opusData.length;
                    if (this.websocket && this.websocket.readyState === WebSocket.OPEN) {
                        try {
                            this.websocket.send(opusData.buffer);
                            this.stats.sentFrames += 1;
                            this.stats.sentBytes += opusData.length;
                            if (this.stats.sentFrames === 1) {
                                log(`[音频诊断][麦克风] 已编码并发送=${this.stats.sentFrames}帧/${this.stats.sentBytes}字节，WebSocket待发送=${this.websocket.bufferedAmount}字节`, 'info');
                            } else if (this.stats.sentFrames % 500 === 0) {
                                console.info(`[音频诊断][麦克风] 已编码并发送=${this.stats.sentFrames}帧/${this.stats.sentBytes}字节，WebSocket待发送=${this.websocket.bufferedAmount}字节`);
                            }
                        } catch (error) {
                            this.stats.sendErrors += 1;
                            log(`WebSocket发送错误: ${error.message}`, 'error');
                        }
                    } else {
                        this.stats.sendErrors += 1;
                        log('[音频诊断][麦克风] Opus帧未发送：WebSocket未连接', 'error');
                    }
                } else {
                    log('Opus编码失败，未返回有效数据', 'error');
                }
            } else {
                if (this.pcmDataBuffer.length > 0) {
                    const samplesPerFrame = 960;
                    if (this.pcmDataBuffer.length < samplesPerFrame) {
                        const paddedBuffer = new Int16Array(samplesPerFrame);
                        paddedBuffer.set(this.pcmDataBuffer);
                        this.encodeAndSendOpus(paddedBuffer);
                    } else {
                        this.encodeAndSendOpus(this.pcmDataBuffer.slice(0, samplesPerFrame));
                    }
                    this.pcmDataBuffer = new Int16Array(0);
                }
            }
        } catch (error) {
            log(`Opus编码错误: ${error.message}`, 'error');
        }
    }

    // Start recording
    async start() {
        if (this.isRecording) return false;
        try {
            // Check if WebSocketHandler instance exists
            const { getWebSocketHandler } = await import('../network/websocket.js');
            const wsHandler = getWebSocketHandler();
            // If machine is speaking, send abort message
            if (wsHandler && wsHandler.isRemoteSpeaking && wsHandler.currentSessionId) {
                const abortMessage = { session_id: wsHandler.currentSessionId, type: 'abort', reason: 'wake_word_detected' };
                if (this.websocket && this.websocket.readyState === WebSocket.OPEN) {
                    this.websocket.send(JSON.stringify(abortMessage));
                    log('已发送中止消息', 'info');
                }
            }
            if (!this.initEncoder()) {
                log('无法开始录音: Opus编码器初始化失败', 'error');
                return false;
            }
            if (!this.websocket || this.websocket.readyState !== WebSocket.OPEN) {
                log('WebSocket未连接，无法开始录音', 'error');
                return false;
            }

            await this.cleanupAudioResources();
            log('请至少录制1-2秒音频以确保收集足够的数据', 'info');
            this.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, sampleRate: 16000, channelCount: 1 } });
            this.audioContext = this.getAudioContext();
            if (this.audioContext.state === 'suspended') {
                this.audioContext.resume().catch(error => {
                    log(`[音频诊断][麦克风] 恢复AudioContext失败: ${error.message}`, 'error');
                });
            }
            const processorResult = await this.createAudioProcessor();
            if (!processorResult) {
                log('无法创建音频处理器', 'error');
                await this.cleanupAudioResources();
                return false;
            }
            this.audioProcessor = processorResult.node;
            this.audioProcessorType = processorResult.type;
            this.audioSource = this.audioContext.createMediaStreamSource(this.mediaStream);
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 2048;
            this.audioSource.connect(this.analyser);
            this.audioSource.connect(this.audioProcessor);
            this.pcmDataBuffer = new Int16Array();
            this.totalAudioSize = 0;
            this.stats = this.createEmptyStats();
            this.isStopping = false;
            this.isRecording = true;
            if (this.audioProcessorType === 'worklet' && this.audioProcessor.port) {
                this.audioProcessor.port.postMessage({ command: 'start' });
            }
            this.websocket.send(JSON.stringify({ type: 'listen', state: 'start', mode: 'auto' }));

            const track = this.mediaStream.getAudioTracks()[0];
            const settings = track?.getSettings ? track.getSettings() : {};
            log(`[音频诊断][麦克风] 采集已开始，设备=${track?.label || '默认设备'}，轨道采样率=${settings.sampleRate || '未知'}Hz，声道=${settings.channelCount || '未知'}，AudioContext=${this.audioContext.sampleRate}Hz/${this.audioContext.state}`, 'success');
            log('[音频诊断][麦克风] 已发送listen/start(auto)', 'info');
            // Start visualization
            if (this.onVisualizerUpdate) {
                const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
                this.startVisualization(dataArray);
            }
            // Immediately notify recording start, update button state
            if (this.onRecordingStart) {
                this.onRecordingStart(0);
            }
            // Start recording timer
            let recordingSeconds = 0;
            this.recordingTimer = setInterval(() => {
                recordingSeconds += 0.1;
                if (this.onRecordingStart) {
                    this.onRecordingStart(recordingSeconds);
                }
            }, 100);
            log('已开始PCM直接录音', 'success');
            return true;
        } catch (error) {
            log(`直接录音启动错误: ${error.message}`, 'error');
            this.isRecording = false;
            this.isStopping = false;
            await this.cleanupAudioResources();
            return false;
        }
    }

    // Start visualization
    startVisualization(dataArray) {
        const draw = () => {
            this.visualizationRequest = requestAnimationFrame(() => draw());
            if (!this.isRecording) return;
            this.analyser.getByteFrequencyData(dataArray);
            if (this.onVisualizerUpdate) {
                this.onVisualizerUpdate(dataArray);
            }
        };
        draw();
    }

    // Stop recording
    async stop() {
        if (!this.isRecording) return false;
        try {
            this.isStopping = true;
            if (this.audioProcessor) {
                if (this.audioProcessorType === 'worklet' && this.audioProcessor.port) {
                    await new Promise(resolve => {
                        let settled = false;
                        const finish = () => {
                            if (settled) return;
                            settled = true;
                            resolve();
                        };
                        this.stopResolver = finish;
                        this.audioProcessor.port.postMessage({ command: 'stop' });
                        setTimeout(finish, 150);
                    });
                }
            }
            this.isRecording = false;
            if (this.visualizationRequest) {
                cancelAnimationFrame(this.visualizationRequest);
                this.visualizationRequest = null;
            }
            if (this.recordingTimer) {
                clearInterval(this.recordingTimer);
                this.recordingTimer = null;
            }
            // Encode and send remaining data
            this.encodeAndSendOpus();
            // The server protocol uses a listen/stop JSON message. An empty binary
            // frame is not an Opus end marker and must never enter VAD decoding.
            if (this.websocket && this.websocket.readyState === WebSocket.OPEN) {
                this.websocket.send(JSON.stringify({ type: 'listen', state: 'stop', mode: 'auto' }));
                log('[音频诊断][麦克风] 已发送listen/stop(auto)', 'info');
            }
            await this.cleanupAudioResources();
            this.isStopping = false;
            if (this.onRecordingStop) {
                this.onRecordingStop();
            }
            log(`[音频诊断][麦克风] 采集已停止，PCM=${this.stats.pcmFrames}帧，发送=${this.stats.sentFrames}帧/${this.stats.sentBytes}字节，峰值=${this.stats.peak.toFixed(4)}，发送错误=${this.stats.sendErrors}`, 'success');
            return true;
        } catch (error) {
            log(`直接录音停止错误: ${error.message}`, 'error');
            this.isRecording = false;
            this.isStopping = false;
            await this.cleanupAudioResources();
            return false;
        }
    }

    async cleanupAudioResources() {
        if (this.audioSource) {
            try {
                this.audioSource.disconnect();
            } catch (error) {
                // Already disconnected.
            }
            this.audioSource = null;
        }
        if (this.audioProcessor) {
            try {
                this.audioProcessor.disconnect();
            } catch (error) {
                // Already disconnected.
            }
            this.audioProcessor = null;
        }
        if (this.processorOutput) {
            try {
                this.processorOutput.disconnect();
            } catch (error) {
                // Already disconnected.
            }
            this.processorOutput = null;
        }
        if (this.mediaStream) {
            this.mediaStream.getTracks().forEach(track => track.stop());
            this.mediaStream = null;
        }
        this.analyser = null;
    }

    getAudioStats() {
        return {
            ...this.stats,
            isRecording: this.isRecording,
            audioContextState: this.audioContext?.state || '未创建',
            audioContextSampleRate: this.audioContext?.sampleRate || 0
        };
    }

    // Get analyser
    getAnalyser() {
        return this.analyser;
    }
}

// Create singleton instance
let audioRecorderInstance = null;

export function getAudioRecorder() {
    if (!audioRecorderInstance) {
        audioRecorderInstance = new AudioRecorder();
    }
    return audioRecorderInstance;
}

/**
 * Check if microphone is available
 * @returns {Promise<boolean>} Returns true if available, false if not available
 */
export async function checkMicrophoneAvailability() {
    // Check if browser supports getUserMedia API
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        log('浏览器不支持getUserMedia API', 'warning');
        return false;
    }
    try {
        // Try to access microphone
        const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, sampleRate: 16000, channelCount: 1 } });
        // Immediately stop all tracks to release microphone
        stream.getTracks().forEach(track => track.stop());
        log('麦克风可用性检查成功', 'success');
        return true;
    } catch (error) {
        log(`麦克风不可用: ${error.message}`, 'warning');
        return false;
    }
}

/**
 * Check if it is HTTP non-localhost access
 * @returns {boolean} Returns true if it is HTTP non-localhost access
 */
export function isHttpNonLocalhost() {
    const protocol = window.location.protocol;
    const hostname = window.location.hostname;
    // Check if it is HTTP protocol
    if (protocol !== 'http:') {
        return false;
    }
    // localhost and 127.0.0.1 can use microphone (browser secure context)
    if (hostname === 'localhost' || hostname === '127.0.0.1') {
        return false;
    }
    // All other HTTP access (including private IPs like 10.x, 192.168.x)
    // are NOT secure contexts and cannot use getUserMedia
    return true;
}
