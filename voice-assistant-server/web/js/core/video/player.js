const CONFIG_KEY = 'smartcockpit_video_config';
const HISTORY_LIMIT = 10;

export class VideoStreamPlayer {
    constructor() {
        this.config = this.loadConfig();
        this.video = null;
        this.socket = null;
        this.mediaSource = null;
        this.sourceBuffer = null;
        this.sourceBufferUpdateEnd = null;
        this.queue = [];
        this.objectUrl = '';
        this.manualStop = true;
        this.connectionId = 0;
        this.mediaId = 0;
        this.reconnectTimer = null;
        this.reconnectAttempt = 0;
        this.onStatusChange = null;
        this.onStreamInfo = null;
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
    }

    start(url = this.config.url) {
        const value = String(url || '').trim();
        if (!/^(rtsps?|rtmps?):\/\//i.test(value)) {
            throw new Error('请输入有效的 RTSP 或 RTMP 视频流地址');
        }
        if (!window.MediaSource) throw new Error('当前浏览器不支持实时视频播放');
        this.rememberUrl(value);
        this.manualStop = false;
        this.reconnectAttempt = 0;
        this.openSocket();
    }

    stop(manual = true) {
        this.manualStop = manual;
        this.connectionId += 1;
        clearTimeout(this.reconnectTimer);
        this.reconnectTimer = null;
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
            socket.send(JSON.stringify({ type: 'start', url: this.config.url }));
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
            const message = JSON.parse(event.data);
            if (message.type === 'stream-started') {
                this.onStreamInfo?.(message);
                this.createMedia(message.codec, connectionId);
            } else if (message.type === 'error') {
                this.fail(message.message || '视频播放失败');
            }
            return;
        }
        this.queue.push(new Uint8Array(event.data));
        this.flush();
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
                    this.trimBuffer();
                    this.flush();
                };
                sourceBuffer.addEventListener('updateend', this.sourceBufferUpdateEnd);
                this.video.play().then(() => {
                    if (!this.isCurrentMedia(mediaSource, mediaId, connectionId)) return;
                    this.reconnectAttempt = 0;
                    this.setStatus('playing', '视频播放中');
                }).catch((error) => {
                    if (this.isCurrentMedia(mediaSource, mediaId, connectionId)) {
                        this.fail(error.message);
                    }
                });
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
            sourceBuffer.appendBuffer(this.queue.shift());
        } catch (error) {
            if (!this.isCurrentSourceBuffer(sourceBuffer, mediaSource, mediaId)
                || error.name === 'InvalidStateError') return;
            this.fail(`视频数据解码失败: ${error.message}`);
        }
    }

    trimBuffer() {
        if (!this.sourceBuffer || this.sourceBuffer.updating || !this.video.buffered.length) return;
        const end = this.video.buffered.end(this.video.buffered.length - 1);
        if (end - this.video.currentTime > 1.5) this.video.currentTime = end - 0.25;
        const removeEnd = this.video.currentTime - 4;
        if (removeEnd > 0 && this.sourceBuffer.buffered.length) {
            const start = this.sourceBuffer.buffered.start(0);
            if (removeEnd > start) this.sourceBuffer.remove(start, removeEnd);
        }
    }

    resetMedia() {
        this.mediaId += 1;
        this.queue = [];
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
}

export const videoStreamPlayer = new VideoStreamPlayer();
