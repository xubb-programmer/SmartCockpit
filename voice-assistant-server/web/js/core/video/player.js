const CONFIG_KEY = 'smartcockpit_video_config';
const HISTORY_LIMIT = 10;
const TARGET_LATENCY_SECONDS = 0.28;
const START_BUFFER_SECONDS = 0.25;
const SOFT_LATENCY_SECONDS = 0.55;
const HARD_LATENCY_SECONDS = 0.95;
const BUFFER_HISTORY_SECONDS = 3;
const LATENCY_CHECK_INTERVAL_MS = 150;
const MAX_APPEND_BYTES = 1024 * 1024;
const MAX_QUEUE_BYTES = 1024 * 1024;
const MAX_QUEUE_DELAY_SECONDS = 0.8;
const MIN_QUEUE_BYTES_FOR_RECOVERY = 64 * 1024;

export class VideoStreamPlayer {
    constructor() {
        this.config = this.loadConfig();
        this.video = null;
        this.socket = null;
        this.mediaSource = null;
        this.sourceBuffer = null;
        this.sourceBufferUpdateEnd = null;
        this.queue = [];
        this.queueBytes = 0;
        this.objectUrl = '';
        this.manualStop = true;
        this.connectionId = 0;
        this.mediaId = 0;
        this.reconnectTimer = null;
        this.reconnectAttempt = 0;
        this.recoveryPending = false;
        this.latencyTimer = null;
        this.liveEdgeInitialized = false;
        this.playbackStarted = false;
        this.playbackStarting = false;
        this.lastTrimAt = 0;
        this.rateWindowStartedAt = 0;
        this.rateWindowBytes = 0;
        this.stats = this.createStats();
        this.onStatusChange = null;
        this.onStreamInfo = null;
    }

    createStats() {
        return {
            receivedMessages: 0,
            receivedBytes: 0,
            incomingBytesPerSecond: 0,
            appendedBatches: 0,
            appendedBytes: 0,
            maxQueueBytes: 0,
            liveEdgeSeeks: 0,
            liveRecoveries: 0,
            stalls: 0,
        };
    }

    loadConfig() {
        const defaults = { url: '', history: [], autoPlay: false, autoReconnect: true };
        try {
            const config = { ...defaults, ...JSON.parse(localStorage.getItem(CONFIG_KEY) || '{}') };
            config.history = Array.isArray(config.history)
                ? config.history.filter((url) => typeof url === 'string' && url.trim()).slice(0, HISTORY_LIMIT)
                : [];
            if (config.url && !config.history.includes(config.url)) {
                config.history.unshift(config.url);
            }
            return config;
        } catch (error) {
            return defaults;
        }
    }

    saveConfig(config) {
        this.config = { ...this.config, ...config };
        localStorage.setItem(CONFIG_KEY, JSON.stringify(this.config));
    }

    rememberUrl(url) {
        const value = String(url || '').trim();
        const history = [value, ...this.config.history.filter((item) => item !== value)]
            .slice(0, HISTORY_LIMIT);
        this.saveConfig({ url: value, history });
        return history;
    }

    removeHistoryUrl(url) {
        const value = String(url || '').trim();
        const history = this.config.history.filter((item) => item !== value);
        const currentUrl = this.config.url === value ? (history[0] || '') : this.config.url;
        this.saveConfig({ url: currentUrl, history });
        return history;
    }

    getHistory() {
        return [...this.config.history];
    }

    attach(video) {
        this.video = video;
        this.video.muted = true;
        this.video.playsInline = true;
        this.video.preload = 'auto';
        this.video.disablePictureInPicture = true;
        this.video.addEventListener('waiting', () => this.handlePlaybackStall());
        this.video.addEventListener('stalled', () => this.handlePlaybackStall());
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden && !this.manualStop) this.maintainLiveEdge();
        });
    }

    start(url = this.config.url) {
        const value = String(url || '').trim();
        if (!/^(rtsps?|rtmps?):\/\//i.test(value)) {
            throw new Error('请输入有效的 RTSP 或 RTMP 视频流地址');
        }
        if (!window.MediaSource) throw new Error('当前浏览器不支持实时视频播放');
        this.rememberUrl(value);
        this.stats = this.createStats();
        this.rateWindowStartedAt = performance.now();
        this.rateWindowBytes = 0;
        this.manualStop = false;
        this.reconnectAttempt = 0;
        this.openSocket();
    }

    stop(manual = true) {
        this.manualStop = manual;
        this.connectionId += 1;
        clearTimeout(this.reconnectTimer);
        this.reconnectTimer = null;
        clearInterval(this.latencyTimer);
        this.latencyTimer = null;
        this.recoveryPending = false;
        const socket = this.socket;
        this.socket = null;
        if (socket) {
            socket.onopen = null;
            socket.onmessage = null;
            socket.onerror = null;
            socket.onclose = null;
            if (socket.readyState === WebSocket.OPEN) {
                socket.send(JSON.stringify({ type: 'stop' }));
            }
            socket.close();
        }
        this.resetMedia();
        if (manual) this.setStatus('idle', '视频已停止');
    }

    openSocket() {
        this.stop(false);
        this.setStatus('connecting', '正在连接视频源...');
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const connectionId = ++this.connectionId;
        const socket = new WebSocket(`${protocol}//${location.host}/video/ws`);
        this.socket = socket;
        socket.binaryType = 'arraybuffer';
        socket.onopen = () => {
            if (!this.isCurrentConnection(socket, connectionId)) return;
            socket.send(JSON.stringify({ type: 'start', url: this.config.url, profile: 'smooth' }));
        };
        socket.onmessage = (event) => {
            if (this.isCurrentConnection(socket, connectionId)) {
                this.handleMessage(event, connectionId);
            }
        };
        socket.onerror = () => {
            if (this.isCurrentConnection(socket, connectionId)) {
                this.fail('视频网关连接失败');
            }
        };
        socket.onclose = () => {
            if (!this.isCurrentConnection(socket, connectionId)) return;
            this.socket = null;
            if (!this.manualStop) this.scheduleReconnect('视频连接已断开');
        };
    }

    isCurrentConnection(socket, connectionId) {
        return this.socket === socket && this.connectionId === connectionId;
    }

    handleMessage(event, connectionId) {
        if (typeof event.data === 'string') {
            let message;
            try {
                message = JSON.parse(event.data);
            } catch (error) {
                this.fail('视频网关返回了无效数据');
                return;
            }
            if (message.type === 'reconnecting') {
                this.setStatus(
                    'reconnecting',
                    `视频源断开，正在重连 (${message.attempt}/${message.maxAttempts})...`,
                );
            } else if (message.type === 'stream-started' || message.type === 'stream-reconnected') {
                this.onStreamInfo?.(message);
                this.createMedia(message.codec, connectionId);
            } else if (message.type === 'error') {
                this.fail(message.message || '视频播放失败');
            }
            return;
        }
        const chunk = new Uint8Array(event.data);
        this.queue.push(chunk);
        this.queueBytes += chunk.byteLength;
        this.stats.receivedMessages += 1;
        this.stats.receivedBytes += chunk.byteLength;
        this.stats.maxQueueBytes = Math.max(this.stats.maxQueueBytes, this.queueBytes);
        this.updateIncomingRate(chunk.byteLength);
        if (this.isBacklogExcessive()) {
            this.recoverLiveEdge('视频积压，正在追赶实时画面');
            return;
        }
        this.flush();
    }

    updateIncomingRate(bytes) {
        const now = performance.now();
        if (!this.rateWindowStartedAt) this.rateWindowStartedAt = now;
        this.rateWindowBytes += bytes;
        const elapsed = now - this.rateWindowStartedAt;
        if (elapsed < 1000) return;
        this.stats.incomingBytesPerSecond = this.rateWindowBytes * 1000 / elapsed;
        this.rateWindowStartedAt = now;
        this.rateWindowBytes = 0;
    }

    isBacklogExcessive() {
        if (this.queueBytes > MAX_QUEUE_BYTES) return true;
        const bytesPerSecond = this.stats.incomingBytesPerSecond;
        return this.queueBytes > MIN_QUEUE_BYTES_FOR_RECOVERY
            && bytesPerSecond > 0
            && this.queueBytes / bytesPerSecond > MAX_QUEUE_DELAY_SECONDS;
    }

    createMedia(codec, connectionId) {
        this.resetMedia();
        const mediaId = this.mediaId;
        const mediaSource = new MediaSource();
        this.mediaSource = mediaSource;
        this.objectUrl = URL.createObjectURL(mediaSource);
        this.video.src = this.objectUrl;
        mediaSource.addEventListener('sourceopen', () => {
            if (!this.isCurrentMedia(mediaSource, mediaId, connectionId)) return;
            const mime = `video/mp4; codecs="${codec}"`;
            if (!MediaSource.isTypeSupported(mime)) {
                this.fail(`浏览器不支持视频编码 ${codec}`);
                return;
            }
            try {
                const sourceBuffer = mediaSource.addSourceBuffer(mime);
                this.sourceBuffer = sourceBuffer;
                sourceBuffer.mode = 'segments';
                this.sourceBufferUpdateEnd = () => {
                    if (!this.isCurrentSourceBuffer(sourceBuffer, mediaSource, mediaId)) return;
                    this.tryStartPlayback(mediaSource, mediaId, connectionId);
                    this.maintainLiveEdge();
                    this.flush();
                };
                sourceBuffer.addEventListener('updateend', this.sourceBufferUpdateEnd);
                this.startLatencyController();
                this.setStatus('buffering', '正在缓冲实时画面...');
                this.flush();
            } catch (error) {
                if (this.isCurrentMedia(mediaSource, mediaId, connectionId)) {
                    this.fail(`视频初始化失败: ${error.message}`);
                }
            }
        }, { once: true });
    }

    isCurrentMedia(mediaSource, mediaId, connectionId) {
        return this.mediaSource === mediaSource
            && this.mediaId === mediaId
            && this.connectionId === connectionId;
    }

    isCurrentSourceBuffer(sourceBuffer, mediaSource, mediaId) {
        return this.sourceBuffer === sourceBuffer
            && this.mediaSource === mediaSource
            && this.mediaId === mediaId
            && mediaSource.readyState === 'open';
    }

    flush() {
        const sourceBuffer = this.sourceBuffer;
        const mediaSource = this.mediaSource;
        const mediaId = this.mediaId;
        if (!sourceBuffer || !mediaSource || sourceBuffer.updating || !this.queue.length) return;
        if (!this.isCurrentSourceBuffer(sourceBuffer, mediaSource, mediaId)) return;
        try {
            const chunks = [];
            let totalBytes = 0;
            while (this.queue.length) {
                const next = this.queue[0];
                if (chunks.length && totalBytes + next.byteLength > MAX_APPEND_BYTES) break;
                chunks.push(this.queue.shift());
                totalBytes += next.byteLength;
                this.queueBytes -= next.byteLength;
            }
            let data = chunks[0];
            if (chunks.length > 1) {
                data = new Uint8Array(totalBytes);
                let offset = 0;
                chunks.forEach((chunk) => {
                    data.set(chunk, offset);
                    offset += chunk.byteLength;
                });
            }
            this.stats.appendedBatches += 1;
            this.stats.appendedBytes += totalBytes;
            sourceBuffer.appendBuffer(data);
        } catch (error) {
            if (!this.isCurrentSourceBuffer(sourceBuffer, mediaSource, mediaId)
                || error.name === 'InvalidStateError') return;
            if (error.name === 'QuotaExceededError') {
                this.recoverLiveEdge('视频缓冲已满，正在恢复实时画面');
                return;
            }
            this.fail(`视频数据解码失败: ${error.message}`);
        }
    }

    startLatencyController() {
        clearInterval(this.latencyTimer);
        this.latencyTimer = setInterval(() => this.maintainLiveEdge(), LATENCY_CHECK_INTERVAL_MS);
    }

    tryStartPlayback(mediaSource, mediaId, connectionId) {
        if (this.playbackStarted || this.playbackStarting || !this.video?.buffered.length) return;
        const buffered = this.video.buffered;
        const start = buffered.start(0);
        const end = buffered.end(buffered.length - 1);
        if (end - start < START_BUFFER_SECONDS) return;

        this.playbackStarting = true;
        this.video.currentTime = Math.max(start, end - TARGET_LATENCY_SECONDS);
        this.video.play().then(() => {
            if (!this.isCurrentMedia(mediaSource, mediaId, connectionId)) return;
            this.playbackStarted = true;
            this.playbackStarting = false;
            this.liveEdgeInitialized = true;
            this.stats.liveEdgeSeeks += 1;
            this.reconnectAttempt = 0;
            this.setStatus('playing', '视频播放中');
        }).catch((error) => {
            if (this.isCurrentMedia(mediaSource, mediaId, connectionId)) {
                this.playbackStarting = false;
                this.fail(error.message);
            }
        });
    }

    maintainLiveEdge() {
        if (!this.video || !this.sourceBuffer || !this.playbackStarted || !this.video.buffered.length) return;
        const buffered = this.video.buffered;
        const end = buffered.end(buffered.length - 1);
        const start = buffered.start(0);
        const latency = Math.max(0, end - this.video.currentTime);

        try {
            if (!this.liveEdgeInitialized || latency > HARD_LATENCY_SECONDS) {
                this.video.currentTime = Math.max(start, end - TARGET_LATENCY_SECONDS);
                this.video.playbackRate = 1;
                this.liveEdgeInitialized = true;
                this.stats.liveEdgeSeeks += 1;
            } else if (latency > SOFT_LATENCY_SECONDS) {
                this.video.playbackRate = 1.08;
            } else if (latency > TARGET_LATENCY_SECONDS + 0.15) {
                this.video.playbackRate = 1.03;
            } else if (this.video.playbackRate !== 1) {
                this.video.playbackRate = 1;
            }
            if (this.mediaSource?.setLiveSeekableRange) {
                this.mediaSource.setLiveSeekableRange(Math.max(start, end - BUFFER_HISTORY_SECONDS), end);
            }
        } catch (error) {
            // The media timeline may not be seekable until the first complete fragment arrives.
        }

        const now = performance.now();
        if (this.sourceBuffer.updating || this.queueBytes > MAX_APPEND_BYTES / 2
            || now - this.lastTrimAt < 1000) return;
        const removeEnd = this.video.currentTime - BUFFER_HISTORY_SECONDS;
        if (removeEnd > start) {
            this.lastTrimAt = now;
            this.sourceBuffer.remove(start, removeEnd);
        }
    }

    handlePlaybackStall() {
        if (this.manualStop) return;
        this.stats.stalls += 1;
        this.maintainLiveEdge();
    }

    recoverLiveEdge(reason) {
        if (this.manualStop || this.recoveryPending) return;
        this.recoveryPending = true;
        this.stats.liveRecoveries += 1;
        clearTimeout(this.reconnectTimer);
        const socket = this.socket;
        this.socket = null;
        this.connectionId += 1;
        if (socket) {
            socket.onopen = null;
            socket.onmessage = null;
            socket.onerror = null;
            socket.onclose = null;
            socket.close();
        }
        this.resetMedia();
        this.setStatus('reconnecting', reason);
        this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            this.recoveryPending = false;
            this.openSocket();
        }, 250);
    }

    resetMedia() {
        this.mediaId += 1;
        this.queue = [];
        this.queueBytes = 0;
        this.liveEdgeInitialized = false;
        this.playbackStarted = false;
        this.playbackStarting = false;
        this.lastTrimAt = 0;
        clearInterval(this.latencyTimer);
        this.latencyTimer = null;
        if (this.sourceBuffer && this.sourceBufferUpdateEnd) {
            this.sourceBuffer.removeEventListener('updateend', this.sourceBufferUpdateEnd);
        }
        this.sourceBuffer = null;
        this.sourceBufferUpdateEnd = null;
        const oldObjectUrl = this.objectUrl;
        this.objectUrl = '';
        this.mediaSource = null;
        if (this.video) {
            this.video.pause();
            this.video.playbackRate = 1;
            this.video.removeAttribute('src');
            this.video.load();
        }
        if (oldObjectUrl) URL.revokeObjectURL(oldObjectUrl);
    }

    fail(message) {
        this.setStatus('error', message);
        if (this.socket) this.socket.close();
        else this.scheduleReconnect(message);
    }

    scheduleReconnect(reason) {
        if (this.manualStop || !this.config.autoReconnect || this.reconnectTimer) return;
        const delay = Math.min(30000, 2000 * (2 ** this.reconnectAttempt++));
        this.setStatus('reconnecting', `${reason}，${delay / 1000} 秒后重连`);
        this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            this.openSocket();
        }, delay);
    }

    setStatus(status, message) {
        this.onStatusChange?.(status, message);
    }

    getDiagnostics() {
        let bufferedStart = 0;
        let bufferedEnd = 0;
        if (this.video?.buffered.length) {
            bufferedStart = this.video.buffered.start(0);
            bufferedEnd = this.video.buffered.end(this.video.buffered.length - 1);
        }
        const quality = this.video?.getVideoPlaybackQuality?.();
        const incomingBytesPerSecond = this.stats.incomingBytesPerSecond;
        return {
            ...this.stats,
            queueBytes: this.queueBytes,
            estimatedQueueDelay: incomingBytesPerSecond > 0
                ? this.queueBytes / incomingBytesPerSecond
                : 0,
            currentTime: this.video?.currentTime || 0,
            bufferedStart,
            bufferedEnd,
            liveLatency: Math.max(0, bufferedEnd - (this.video?.currentTime || 0)),
            playbackRate: this.video?.playbackRate || 1,
            readyState: this.video?.readyState || 0,
            totalVideoFrames: quality?.totalVideoFrames || 0,
            droppedVideoFrames: quality?.droppedVideoFrames || 0,
        };
    }
}

export const videoStreamPlayer = new VideoStreamPlayer();
