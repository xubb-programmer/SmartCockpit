import { videoStreamPlayer } from '../core/video/player.js?v=0828a';

const VISION_FRAME_UPLOAD_INTERVAL_MS = 1000;
const VISION_FRAME_MAX_EDGE = 896;
const VISION_FRAME_JPEG_QUALITY = 0.84;

class VideoUIController {
    init() {
        this.player = videoStreamPlayer;
        this.stage = document.getElementById('videoStage');
        this.video = document.getElementById('deviceVideo');
        this.url = document.getElementById('videoStreamUrl');
        this.historySelect = document.getElementById('videoHistorySelect');
        this.historyList = document.getElementById('videoStreamHistory');
        this.deleteHistoryButton = document.getElementById('deleteVideoHistoryBtn');
        this.autoPlay = document.getElementById('videoAutoPlay');
        this.autoReconnect = document.getElementById('videoAutoReconnect');
        this.frameCanvas = document.createElement('canvas');
        this.frameContext = this.frameCanvas.getContext('2d', {
            alpha: false,
            desynchronized: true,
        });
        this.frameUploadTimer = null;
        this.frameUploadInFlight = false;
        this.lastCapturedVideoTime = null;
        this.frameUploadStats = {
            uploaded: 0,
            lastUploadAt: 0,
            lastError: '',
        };
        this.player.attach(this.video);
        this.url.value = this.player.config.url;
        this.autoPlay.checked = this.player.config.autoPlay;
        this.autoReconnect.checked = this.player.config.autoReconnect;
        this.renderHistory();
        this.updateProtocol();
        this.bindEvents();
        this.player.onStatusChange = (status, message) => this.updateStatus(status, message);
        this.player.onStreamInfo = (info) => this.updateStreamInfo(info);
        window.getVideoDiagnostics = () => ({
            ...this.player.getDiagnostics(),
            visionFrame: { ...this.frameUploadStats },
        });
        if (this.player.config.autoPlay && this.player.config.url) {
            this.showStage();
            setTimeout(() => this.play(), 300);
        }
    }

    bindEvents() {
        document.getElementById('videoBtn').addEventListener('click', () => {
            const enabled = document.body.classList.toggle('video-mode');
            document.getElementById('videoBtn').classList.toggle('video-active', enabled);
            if (enabled && this.player.config.url && !this.player.socket) this.play();
        });
        document.getElementById('playVideoBtn').addEventListener('click', () => this.play());
        document.getElementById('stopVideoBtn').addEventListener('click', () => this.player.stop());
        document.getElementById('toggleVideoUrlBtn').addEventListener('click', (event) => {
            const hidden = this.url.type === 'password';
            this.url.type = hidden ? 'text' : 'password';
            event.currentTarget.textContent = hidden ? '隐藏' : '显示';
        });
        this.url.addEventListener('input', () => {
            this.updateProtocol();
            this.syncHistorySelection();
        });
        this.historySelect.addEventListener('change', () => {
            if (!this.historySelect.value) return;
            this.url.value = this.historySelect.value;
            this.updateProtocol();
            this.deleteHistoryButton.disabled = false;
        });
        this.deleteHistoryButton.addEventListener('click', () => {
            if (!this.historySelect.value) return;
            this.player.removeHistoryUrl(this.historySelect.value);
            this.url.value = this.player.config.url;
            this.renderHistory();
            this.updateProtocol();
        });
        this.autoPlay.addEventListener('change', () => this.save());
        this.autoReconnect.addEventListener('change', () => this.save());
        this.fullscreenButton = document.getElementById('videoFullscreenBtn');
        this.fullscreenButton.addEventListener('click', () => this.toggleFullscreen());
        this.video.addEventListener('dblclick', () => this.toggleFullscreen());
        document.addEventListener('fullscreenchange', () => this.updateFullscreenButton());
        window.addEventListener('beforeunload', () => {
            this.stopFrameUploads();
            this.player.stop();
        });
    }

    save() {
        this.player.saveConfig({
            url: this.url.value.trim(),
            autoPlay: this.autoPlay.checked,
            autoReconnect: this.autoReconnect.checked,
        });
    }

    play() {
        try {
            this.save();
            this.showStage();
            this.player.start(this.url.value);
            this.renderHistory();
            document.getElementById('settingsModal').style.display = 'none';
        } catch (error) {
            this.updateStatus('error', error.message);
        }
    }

    showStage() {
        document.body.classList.add('video-mode');
        document.getElementById('videoBtn').classList.add('video-active');
    }

    async toggleFullscreen() {
        try {
            if (document.fullscreenElement) {
                await document.exitFullscreen();
            } else {
                await this.stage.requestFullscreen();
            }
        } catch (error) {
            this.updateStatus('error', `无法切换全屏: ${error.message}`);
        }
    }

    updateFullscreenButton() {
        const fullscreen = document.fullscreenElement === this.stage;
        this.fullscreenButton.querySelector('.btn-text').textContent =
            fullscreen ? '默认' : '全屏';
        this.fullscreenButton.classList.toggle('is-fullscreen', fullscreen);
        this.fullscreenButton.title = fullscreen ? '恢复默认尺寸' : '全屏显示视频';
        this.fullscreenButton.setAttribute(
            'aria-label',
            fullscreen ? '恢复默认尺寸' : '全屏显示视频'
        );
    }

    maskUrl(url) {
        try {
            const parsed = new URL(url);
            if (parsed.password) {
                parsed.password = '***';
            }
            return parsed.toString();
        } catch (error) {
            return url;
        }
    }

    renderHistory() {
        const history = this.player.getHistory();
        this.historySelect.replaceChildren();
        this.historyList.replaceChildren();
        const emptyOption = document.createElement('option');
        emptyOption.value = '';
        emptyOption.textContent = history.length ? '请选择历史地址' : '暂无历史地址';
        this.historySelect.appendChild(emptyOption);
        history.forEach((url) => {
            const label = this.maskUrl(url);
            const option = document.createElement('option');
            option.value = url;
            option.textContent = label;
            this.historySelect.appendChild(option);

            const suggestion = document.createElement('option');
            suggestion.value = url;
            suggestion.label = label;
            this.historyList.appendChild(suggestion);
        });
        this.syncHistorySelection();
    }

    syncHistorySelection() {
        const value = this.url.value.trim();
        const exists = this.player.getHistory().includes(value);
        this.historySelect.value = exists ? value : '';
        this.deleteHistoryButton.disabled = !exists;
    }

    updateProtocol() {
        const value = this.url.value.trim().toLowerCase();
        document.getElementById('videoProtocolText').textContent =
            value.startsWith('rtsp') ? 'RTSP' : value.startsWith('rtmp') ? 'RTMP' : '未识别';
    }

    updateStatus(status, message) {
        const names = {
            idle: '视频未连接', connecting: '视频连接中',
            reconnecting: '视频重连中', playing: '视频播放中', error: '视频异常',
        };
        const dot = document.getElementById('videoStatusDot');
        dot.className = `video-status-dot ${status}`;
        document.getElementById('videoStatusText').textContent = names[status] || names.idle;
        document.getElementById('videoSettingStatus').textContent = message;
        const state = document.getElementById('videoState');
        state.classList.toggle('hidden', status === 'playing');
        document.getElementById('videoStateTitle').textContent = names[status] || names.idle;
        document.getElementById('videoStateMessage').textContent = message;
        document.getElementById('videoFullscreenBtn').disabled = status !== 'playing';
        if (status === 'playing') {
            this.startFrameUploads();
        } else {
            this.stopFrameUploads();
        }
    }

    startFrameUploads() {
        if (this.frameUploadTimer) return;
        void this.captureAndUploadFrame();
        this.frameUploadTimer = window.setInterval(
            () => void this.captureAndUploadFrame(),
            VISION_FRAME_UPLOAD_INTERVAL_MS,
        );
    }

    stopFrameUploads() {
        if (this.frameUploadTimer) {
            window.clearInterval(this.frameUploadTimer);
            this.frameUploadTimer = null;
        }
        this.lastCapturedVideoTime = null;
    }

    async captureAndUploadFrame() {
        if (this.frameUploadInFlight || !this.frameContext) return;
        const haveCurrentData = this.video?.HAVE_CURRENT_DATA || 2;
        if (!this.video || this.video.readyState < haveCurrentData) return;

        const sourceWidth = this.video.videoWidth;
        const sourceHeight = this.video.videoHeight;
        const videoTime = this.video.currentTime;
        if (!sourceWidth || !sourceHeight || !Number.isFinite(videoTime)) return;
        if (this.lastCapturedVideoTime !== null &&
            Math.abs(videoTime - this.lastCapturedVideoTime) < 0.001) return;

        const scale = Math.min(
            1,
            VISION_FRAME_MAX_EDGE / Math.max(sourceWidth, sourceHeight),
        );
        const frameWidth = Math.max(1, Math.round(sourceWidth * scale));
        const frameHeight = Math.max(1, Math.round(sourceHeight * scale));
        if (this.frameCanvas.width !== frameWidth) this.frameCanvas.width = frameWidth;
        if (this.frameCanvas.height !== frameHeight) this.frameCanvas.height = frameHeight;

        this.frameUploadInFlight = true;
        try {
            this.frameContext.drawImage(this.video, 0, 0, frameWidth, frameHeight);
            const frameBlob = await new Promise((resolve) => {
                this.frameCanvas.toBlob(
                    resolve,
                    'image/jpeg',
                    VISION_FRAME_JPEG_QUALITY,
                );
            });
            if (!frameBlob) throw new Error('浏览器无法生成视频截图');

            const response = await fetch('/video/frame', {
                method: 'POST',
                headers: { 'Content-Type': 'image/jpeg' },
                body: frameBlob,
                cache: 'no-store',
                credentials: 'same-origin',
            });
            if (!response.ok) {
                const details = await response.text();
                throw new Error(`上传失败 ${response.status}: ${details}`);
            }

            this.lastCapturedVideoTime = videoTime;
            this.frameUploadStats.uploaded += 1;
            this.frameUploadStats.lastUploadAt = Date.now();
            this.frameUploadStats.lastError = '';
        } catch (error) {
            this.frameUploadStats.lastError = error.message;
            const now = Date.now();
            if (!this.lastFrameUploadWarningAt || now - this.lastFrameUploadWarningAt > 30000) {
                console.warn(`[视频截图] ${error.message}`);
                this.lastFrameUploadWarningAt = now;
            }
        } finally {
            this.frameUploadInFlight = false;
        }
    }

    updateStreamInfo(info) {
        const size = info.width && info.height ? `${info.width}×${info.height}` : '';
        const fps = Number(info.fps) > 0 ? `${Math.round(Number(info.fps))} FPS` : '';
        const mode = info.transcoding ? '转码' : '直通';
        document.getElementById('videoStreamInfo').textContent =
            [size, fps, String(info.sourceCodec).toUpperCase(), mode].filter(Boolean).join(' · ');
    }
}

export const videoUIController = new VideoUIController();
