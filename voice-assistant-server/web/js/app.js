// 主应用入口
import { checkOpusLoaded, initOpusEncoder } from './core/audio/opus-codec.js';
import { getAudioPlayer } from './core/audio/player.js';
import { checkMicrophoneAvailability, isHttpNonLocalhost } from './core/audio/recorder.js';
import { initMcpTools } from './core/mcp/tools.js';
import { uiController } from './ui/controller.js';
import { videoUIController } from './ui/video-controller.js?v=0812c';
import { log } from './utils/logger.js';

// 应用类
class App {
    constructor() {
        this.uiController = null;
        this.audioPlayer = null;
    }

    // 初始化应用
    async init() {
        log('正在初始化应用...', 'info');
        // 初始化UI控制器
        this.uiController = uiController;
        this.uiController.init();
        videoUIController.init();
        // 检查Opus库
        checkOpusLoaded();
        // 初始化Opus编码器
        initOpusEncoder();
        // 初始化音频播放器
        this.audioPlayer = getAudioPlayer();
        await this.audioPlayer.start();
        // 初始化MCP工具
        initMcpTools();
        // 检查麦克风可用性
        await this.checkMicrophoneAvailability();
        // 关闭加载loading
        this.setModelLoadingStatus(false);
        log('应用初始化完成', 'success');
    }

    // 设置model加载状态
    setModelLoadingStatus(isLoading) {
        const modelLoading = document.getElementById('modelLoading');
        if (modelLoading) {
            modelLoading.style.display = isLoading ? 'flex' : 'none';
        }
    }

    /**
     * 检查麦克风可用性
     * 在应用初始化时调用，检查麦克风是否可用并更新UI状态
     */
    async checkMicrophoneAvailability() {
        try {
            const isAvailable = await checkMicrophoneAvailability();
            const isHttp = isHttpNonLocalhost();
            // 保存可用性状态到全局变量
            window.microphoneAvailable = isAvailable;
            window.isHttpNonLocalhost = isHttp;
            // 更新UI
            if (this.uiController) {
                this.uiController.updateMicrophoneAvailability(isAvailable, isHttp);
            }
            log(`麦克风可用性检查完成: ${isAvailable ? '可用' : '不可用'}`, isAvailable ? 'success' : 'warning');
        } catch (error) {
            log(`检查麦克风可用性失败: ${error.message}`, 'error');
            // 默认设置为不可用
            window.microphoneAvailable = false;
            window.isHttpNonLocalhost = isHttpNonLocalhost();
            if (this.uiController) {
                this.uiController.updateMicrophoneAvailability(false, window.isHttpNonLocalhost);
            }
        }
    }
}

// 创建并启动应用
const app = new App();
// 将应用实例暴露到全局，供其他模块访问
window.chatApp = app;
document.addEventListener('DOMContentLoaded', () => {
    // 初始化应用
    app.init();
});
export default app;
