// WebSocket消息处理模块
import { getConfig, saveConnectionUrls } from '../../config/manager.js';
import { uiController } from '../../ui/controller.js?v=0903a';
import { log } from '../../utils/logger.js';
import { getAudioPlayer } from '../audio/player.js';
import { getAudioRecorder } from '../audio/recorder.js';
import { executeMcpTool, getMcpTools, setWebSocket as setMcpWebSocket } from '../mcp/tools.js';
import { webSocketConnect } from './ota-connector.js';

const TTS_CAPTURE_TAIL_MS = 800;

function isLoopbackPage() {
    return ['127.0.0.1', 'localhost', '::1'].includes(window.location.hostname);
}

function getWebSocketErrorMessage(error, websocket) {
    if (error && error.message) {
        return error.message;
    }

    const wsUrl = websocket?.url || document.getElementById('serverUrl')?.value || '';
    if (wsUrl.startsWith('wss://')) {
        return 'WSS连接失败：当前HTTPS证书未被系统或浏览器信任。清除浏览器数据后临时信任会失效，请将 .ssl/cert.pem 导入受信任根证书，或使用正式证书。';
    }

    return '未知错误';
}

// WebSocket处理器类
export class WebSocketHandler {
    constructor() {
        this.websocket = null;
        this.isConnecting = false;
        this.lastErrorMessage = '';
        this.onConnectionStateChange = null;
        this.onRecordButtonStateChange = null;
        this.onSessionStateChange = null;
        this.onSessionEmotionChange = null;
        this.onChatMessage = null; // 新增：聊天消息回调
        this.currentSessionId = null;
        this.isRemoteSpeaking = false;
        this.musicBargeInActive = false;
        this.ttsCaptureResumeTimer = null;
    }

    suppressCaptureForTTS() {
        if (this.ttsCaptureResumeTimer) {
            clearTimeout(this.ttsCaptureResumeTimer);
            this.ttsCaptureResumeTimer = null;
        }
        getAudioRecorder().setCaptureSuppressed(true, 'tts-playback');
    }

    scheduleCaptureResume(streamSequence, audioPlayer) {
        if (this.ttsCaptureResumeTimer) {
            clearTimeout(this.ttsCaptureResumeTimer);
        }
        this.ttsCaptureResumeTimer = setTimeout(() => {
            this.ttsCaptureResumeTimer = null;
            if (audioPlayer.streamSequence !== streamSequence || audioPlayer.streamActive) {
                return;
            }
            getAudioRecorder().setCaptureSuppressed(false, 'tts-playback');
            log(`[音频诊断][回声保护] TTS播放结束${TTS_CAPTURE_TAIL_MS}毫秒后恢复采集`, 'info');
        }, TTS_CAPTURE_TAIL_MS);
    }

    clearTTSCaptureProtection() {
        if (this.ttsCaptureResumeTimer) {
            clearTimeout(this.ttsCaptureResumeTimer);
            this.ttsCaptureResumeTimer = null;
        }
        this.musicBargeInActive = false;
        getAudioRecorder().setCaptureSuppressed(false, 'tts-playback');
    }

    // 发送hello握手消息
    async sendHelloMessage() {
        if (!this.websocket || this.websocket.readyState !== WebSocket.OPEN) return false;

        try {
            const config = getConfig();

            const helloMessage = {
                type: 'hello',
                device_id: config.deviceId,
                device_name: config.deviceName,
                device_mac: config.deviceMac,
                token: config.token,
                features: {
                    mcp: true
                }
            };

            log('发送hello握手消息', 'info');
            this.websocket.send(JSON.stringify(helloMessage));

            return new Promise(resolve => {
                const timeout = setTimeout(() => {
                    log('等待hello响应超时', 'error');
                    log('提示: 请检查连接配置是否正确', 'info');
                    resolve(false);
                }, 5000);

                const onMessageHandler = (event) => {
                    try {
                        const response = JSON.parse(event.data);
                        if (response.type === 'hello' && response.session_id) {
                            log(`服务器握手成功，会话ID: ${response.session_id}`, 'success');
                            clearTimeout(timeout);
                            this.websocket.removeEventListener('message', onMessageHandler);
                            resolve(true);
                        }
                    } catch (e) {
                        // 忽略非JSON消息
                    }
                };

                this.websocket.addEventListener('message', onMessageHandler);
            });
        } catch (error) {
            log(`发送hello消息错误: ${error.message}`, 'error');
            return false;
        }
    }

    // 处理文本消息
    handleTextMessage(message) {
        if (message.type === 'hello') {
            log(`服务器回应：${JSON.stringify(message, null, 2)}`, 'success');
            const audioParams = message.audio_params || {};
            log(`[音频诊断][协议] 服务端输出格式=${audioParams.format || '未知'}，采样率=${audioParams.sample_rate || '未知'}Hz，声道=${audioParams.channels || '未知'}，帧时长=${audioParams.frame_duration || '未知'}ms`, 'info');
            log('连接成功', 'success');
            uiController.updateDialButton(true);
            const deferLocalHybridCapture = message.access_mode === 'hybrid' && isLoopbackPage();
            uiController.startAIChatSession({
                autoStartRecording: !deferLocalHybridCapture
            });
            if (deferLocalHybridCapture) {
                log('[语音入口] hybrid本机页面保持待机，按录音按钮后使用Web麦克风', 'info');
            }
        } else if (message.type === 'tts') {
            this.handleTTSMessage(message);
        } else if (message.type === 'audio') {
            log(`收到音频控制消息: ${JSON.stringify(message)}`, 'info');
        } else if (message.type === 'stt') {
            log(`识别结果: ${message.text}`, 'info');
            // 使用新的聊天消息回调显示STT消息
            if (this.onChatMessage && message.text) {
                // 拦截服务端伪装成 stt 发送的工具调用指令 (例如 "% play_music")
                if (message.text.trim().startsWith('%')) {
                    const toolMap = {
                        'play_music':         '🎵 音乐控制',
                        'get_lunar':          '📅 正在查询农历...',
                        'get_datetime':       '🕐 正在查询时间...',
                        'handle_exit_intent': '👋 好的，下次见~',
                        'vision_analyze':     '👁️ 正在分析画面...',
                        'vehicle_control':    '🚗 正在执行设备控制...',
                        'vehicle_status':     '📊 正在查询设备状态...',
                    };
                    let displayText = message.text.replace(/(?:^|\n)\s*%\s*([a-zA-Z_][\w.-]*)/g, (match, toolName) => {
                        const friendlyName = toolMap[toolName] || '🔧 正在处理中...';
                        return (match.startsWith('\n') ? '\n' : '') + friendlyName;
                    });
                    // 工具状态属于系统/助手消息，因此 isUser 设为 false
                    this.onChatMessage(displayText, false);
                } else {
                    // 真实的用户语音识别结果
                    this.onChatMessage(message.text, true);
                }
            }
        } else if (message.type === 'llm') {
            log(`大模型回复: ${message.text}`, 'info');

            let displayText = message.text;
            // 如果文本不仅仅包含表情，才进行处理和显示
            const textWithoutEmoji = displayText ? displayText.replace(/[\u{1F300}-\u{1F9FF}]|[\u{2600}-\u{26FF}]|[\u{2700}-\u{27BF}]/gu, '').trim() : '';
            
            if (textWithoutEmoji) {
                const toolMap = {
                    'play_music':         '🎵 音乐控制',
                    'get_lunar':          '📅 正在查询农历...',
                    'get_datetime':       '🕐 正在查询时间...',
                    'handle_exit_intent': '👋 好的，下次见~',
                    'vision_analyze':     '👁️ 正在分析画面...',
                    'vehicle_control':    '🚗 正在执行设备控制...',
                    'vehicle_status':     '📊 正在查询设备状态...',
                };
                displayText = displayText.replace(/(?:^|\n)\s*%\s*([a-zA-Z_][\w.-]*)/g, (match, toolName) => {
                    const friendlyName = toolMap[toolName] || '🔧 正在处理中...';
                    return (match.startsWith('\n') ? '\n' : '') + friendlyName;
                });
                
                if (this.onChatMessage) {
                    this.onChatMessage(displayText, false);
                }
            }

            // 如果包含表情，更新sessionStatus表情并触发Live2D动作
            if (message.text && /[\u{1F300}-\u{1F9FF}]|[\u{2600}-\u{26FF}]|[\u{2700}-\u{27BF}]/u.test(message.text)) {
                // 提取表情符号
                const emojiMatch = message.text.match(/[\u{1F300}-\u{1F9FF}]|[\u{2600}-\u{26FF}]|[\u{2700}-\u{27BF}]/u);
                if (emojiMatch && this.onSessionEmotionChange) {
                    this.onSessionEmotionChange(emojiMatch[0]);
                }

            }


        } else if (message.type === 'mcp') {
            this.handleMCPMessage(message);
        } else if (message.type === 'vehicle_cmd') {
            log(`[车辆控制] 收到指令: 设备=${message.device}, 动作=${message.action}`, 'info');
            // 此消息为内部控制指令，仅记录日志，不在UI对话框中直接展示 JSON
        } else if (message.type === 'access') {
            const status = message.status || 'unknown';
            const text = message.message || '语音通道状态已更新';
            log(`[语音入口] 状态=${status}，模式=${message.mode || '未知'}，${text}`,
                status === 'busy' || status === 'denied' ? 'warning' : 'info');
            if ((status === 'busy' || status === 'denied') && this.onChatMessage) {
                this.onChatMessage(text, false);
            }
        } else {
            log(`未知消息类型: ${message.type}`, 'info');
            if (this.onChatMessage) {
                this.onChatMessage(`未知消息类型: ${message.type}\n${JSON.stringify(message, null, 2)}`, false);
            }
        }
    }

    // 处理TTS消息
    handleTTSMessage(message) {
        if (message.state === 'start') {
            log('服务器开始发送语音', 'info');
            this.currentSessionId = message.session_id;
            this.isRemoteSpeaking = true;
            this.musicBargeInActive = false;
            this.suppressCaptureForTTS();
            getAudioPlayer().beginStream(message.session_id);
            if (this.onSessionStateChange) {
                this.onSessionStateChange(true);
            }

        } else if (message.state === 'music_start') {
            this.musicBargeInActive = true;
            if (this.ttsCaptureResumeTimer) {
                clearTimeout(this.ttsCaptureResumeTimer);
                this.ttsCaptureResumeTimer = null;
            }
            getAudioRecorder().setCaptureSuppressed(false, 'tts-playback');
            log('[音频诊断][音乐打断] 音乐开始，已恢复麦克风上行', 'success');

        } else if (message.state === 'sentence_start') {
            log(`服务器发送语音段: ${message.text}`, 'info');
            this.ttsSentenceCount = (this.ttsSentenceCount || 0) + 1;

            if (message.text && this.onChatMessage) {
                let displayText = message.text;
                const toolMap = {
                    'play_music':         '🎵 音乐控制',
                    'get_lunar':          '📅 正在查询农历...',
                    'get_datetime':       '🕐 正在查询时间...',
                    'handle_exit_intent': '👋 好的，下次见~',
                    'vision_analyze':     '👁️ 正在分析画面...',
                    'vehicle_control':    '🚗 正在执行设备控制...',
                    'vehicle_status':     '📊 正在查询设备状态...',
                };
                displayText = displayText.replace(/(?:^|\n)\s*%\s*([a-zA-Z_][\w.-]*)/g, (match, toolName) => {
                    const friendlyName = toolMap[toolName] || '🔧 正在处理中...';
                    return (match.startsWith('\n') ? '\n' : '') + friendlyName;
                });
                
                // 为了避免纯表情气泡也可以像llm一样过滤，但tts一般不发表情，这里保留原样只做翻译
                this.onChatMessage(displayText, false);
            }

        } else if (message.state === 'sentence_end') {
            log(`语音段结束: ${message.text}`, 'info');

            // 句子结束时不清除动画，等待下一个句子或最终停止
        } else if (message.state === 'stop') {
            this.musicBargeInActive = false;
            const audioPlayer = getAudioPlayer();
            const streamSequence = audioPlayer.streamSequence;
            log('[音频诊断][播放] 服务端音频传输已结束，等待浏览器播放队列排空', 'info');
            audioPlayer.endStream().then(() => {
                if (audioPlayer.streamSequence !== streamSequence || audioPlayer.streamActive) {
                    return;
                }
                this.isRemoteSpeaking = false;
                this.scheduleCaptureResume(streamSequence, audioPlayer);
                if (this.onSessionStateChange) {
                    this.onSessionStateChange(false);
                }
                this.ttsSentenceCount = 0; // 重置计数器
            });
        }
    }

    // 启动Live2D说话动画
    startLive2DTalking() {
        try {
            // 获取Live2D管理器实例
            const live2dManager = window.chatApp?.live2dManager;
            if (live2dManager && live2dManager.live2dModel) {
                // 使用音频播放器的分析器节点
                live2dManager.startTalking();
                log('Live2D说话动画已启动', 'info');
            }
        } catch (error) {
            log(`启动Live2D说话动画失败: ${error.message}`, 'error');
        }
    }

    // 停止Live2D说话动画
    stopLive2DTalking() {
        try {
            const live2dManager = window.chatApp?.live2dManager;
            if (live2dManager) {
                live2dManager.stopTalking();
                log('Live2D说话动画已停止', 'info');
            }
        } catch (error) {
            log(`停止Live2D说话动画失败: ${error.message}`, 'error');
        }
    }

    // 初始化Live2D音频分析器
    initializeLive2DAudioAnalyzer() {
        try {
            const live2dManager = window.chatApp?.live2dManager;
            if (live2dManager) {
                // 初始化音频分析器（使用音频播放器的上下文）
                if (live2dManager.initializeAudioAnalyzer()) {
                    log('Live2D音频分析器初始化完成，已连接到音频播放器', 'success');
                } else {
                    log('Live2D音频分析器初始化失败，将使用模拟动画', 'warning');
                }
            }
        } catch (error) {
            log(`初始化Live2D音频分析器失败: ${error.message}`, 'error');
        }
    }

    // 处理MCP消息
    handleMCPMessage(message) {
        const payload = message.payload || {};
        log(`服务器下发: ${JSON.stringify(message)}`, 'info');

        if (payload.method === 'tools/list') {
            const tools = getMcpTools();

            const replyMessage = JSON.stringify({
                "session_id": message.session_id || "",
                "type": "mcp",
                "payload": {
                    "jsonrpc": "2.0",
                    "id": payload.id,
                    "result": {
                        "tools": tools
                    }
                }
            });
            log(`客户端上报: ${replyMessage}`, 'info');
            this.websocket.send(replyMessage);
            log(`回复MCP工具列表: ${tools.length} 个工具`, 'info');

        } else if (payload.method === 'tools/call') {
            const toolName = payload.params?.name;
            const toolArgs = payload.params?.arguments;

            log(`调用工具: ${toolName} 参数: ${JSON.stringify(toolArgs)}`, 'info');

            executeMcpTool(toolName, toolArgs).then(result => {
                const replyMessage = JSON.stringify({
                    "session_id": message.session_id || "",
                    "type": "mcp",
                    "payload": {
                        "jsonrpc": "2.0",
                        "id": payload.id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": JSON.stringify(result)
                                }
                            ],
                            "isError": false
                        }
                    }
                });

                log(`客户端上报: ${replyMessage}`, 'info');
                this.websocket.send(replyMessage);
            }).catch(error => {
                log(`工具执行失败: ${error.message}`, 'error');
                const errorReply = JSON.stringify({
                    "session_id": message.session_id || "",
                    "type": "mcp",
                    "payload": {
                        "jsonrpc": "2.0",
                        "id": payload.id,
                        "error": {
                            "code": -32603,
                            "message": error.message
                        }
                    }
                });
                this.websocket.send(errorReply);
            });
        } else if (payload.method === 'initialize') {
            log(`收到工具初始化请求: ${JSON.stringify(payload.params)}`, 'info');
            // 保存视觉分析接口地址
            const visionUrl = document.getElementById('visionUrl');
            const visionConfig = payload?.params?.capabilities?.vision;
            if (visionConfig && typeof visionConfig === 'object' && visionConfig.url && visionConfig.token) {
                const visionConfigStr = JSON.stringify(visionConfig);
                localStorage.setItem('xz_tester_vision', visionConfigStr);
                if (visionUrl) visionUrl.value = visionConfig.url;
            } else {
                localStorage.removeItem('xz_tester_vision');
                if (visionUrl) visionUrl.value = '';
            }

            const replyMessage = JSON.stringify({
                "session_id": message.session_id || "",
                "type": "mcp",
                "payload": {
                    "jsonrpc": "2.0",
                    "id": payload.id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "tools": {}
                        },
                        "serverInfo": {
                            "name": "voice-assistant-web-client",
                            "version": "2.1.0"
                        }
                    }
                }
            });
            log(`回复初始化响应`, 'info');
            this.websocket.send(replyMessage);
        } else {
            log(`未知的MCP方法: ${payload.method}`, 'warning');
        }
    }

    // 处理二进制消息
    async handleBinaryMessage(data) {
        try {
            let arrayBuffer;
            if (data instanceof ArrayBuffer) {
                arrayBuffer = data;
            } else if (data instanceof Blob) {
                arrayBuffer = await data.arrayBuffer();
                log(`收到Blob音频数据，大小: ${arrayBuffer.byteLength}字节`, 'debug');
            } else {
                log(`收到未知类型的二进制数据: ${typeof data}`, 'warning');
                return;
            }

            const opusData = new Uint8Array(arrayBuffer);
            const audioPlayer = getAudioPlayer();
            audioPlayer.enqueueAudioData(opusData);
        } catch (error) {
            log(`处理二进制消息出错: ${error.message}`, 'error');
        }
    }

    // 连接WebSocket服务器
    async connect() {
        const config = getConfig();
        this.lastErrorMessage = '';
        log('正在检查OTA状态...', 'info');
        saveConnectionUrls();

        try {
            const otaUrl = document.getElementById('otaUrl').value.trim();
            const ws = await webSocketConnect(otaUrl, config);
            if (ws === undefined) {
                return false;
            }
            this.websocket = ws;

            // 设置接收二进制数据的类型为ArrayBuffer
            this.websocket.binaryType = 'arraybuffer';

            // 设置 MCP 模块的 WebSocket 实例
            setMcpWebSocket(this.websocket);

            // 设置录音器的WebSocket
            const audioRecorder = getAudioRecorder();
            audioRecorder.setWebSocket(this.websocket);

            this.isConnecting = true;
            this.setupEventHandlers();
            const opened = await this.waitForOpen();
            this.isConnecting = false;

            if (!opened) {
                if (this.websocket && this.websocket.readyState !== WebSocket.CLOSED) {
                    this.websocket.close();
                }
                return false;
            }

            return true;
        } catch (error) {
            this.isConnecting = false;
            this.lastErrorMessage = error.message || '连接错误';
            log(`连接错误: ${error.message}`, 'error');
            if (this.onConnectionStateChange) {
                this.onConnectionStateChange(false);
            }
            return false;
        }
    }

    waitForOpen(timeoutMs = 10000) {
        if (!this.websocket) return Promise.resolve(false);
        if (this.websocket.readyState === WebSocket.OPEN) return Promise.resolve(true);
        if (this.websocket.readyState === WebSocket.CLOSING || this.websocket.readyState === WebSocket.CLOSED) {
            return Promise.resolve(false);
        }

        return new Promise(resolve => {
            let settled = false;
            let timeoutId = null;

            const cleanup = () => {
                clearTimeout(timeoutId);
                this.websocket.removeEventListener('open', onOpen);
                this.websocket.removeEventListener('error', onError);
                this.websocket.removeEventListener('close', onClose);
            };
            const resolveOnce = (result) => {
                if (settled) return;
                settled = true;
                cleanup();
                resolve(result);
            };
            const onOpen = () => resolveOnce(true);
            const onError = (event) => {
                this.lastErrorMessage = getWebSocketErrorMessage(event, this.websocket);
                resolveOnce(false);
            };
            const onClose = () => {
                if (!this.lastErrorMessage) {
                    this.lastErrorMessage = 'WebSocket连接已关闭';
                }
                resolveOnce(false);
            };

            timeoutId = setTimeout(() => {
                this.lastErrorMessage = 'WebSocket连接超时';
                resolveOnce(false);
            }, timeoutMs);

            this.websocket.addEventListener('open', onOpen);
            this.websocket.addEventListener('error', onError);
            this.websocket.addEventListener('close', onClose);
        });
    }

    getLastErrorMessage() {
        return this.lastErrorMessage;
    }

    // 设置事件处理器
    setupEventHandlers() {
        this.websocket.onopen = async () => {
            const url = document.getElementById('serverUrl').value;
            log(`已连接到服务器: ${url}`, 'success');

            if (this.onConnectionStateChange) {
                this.onConnectionStateChange(true);
            }

            // 连接成功后，默认状态为聆听中
            this.clearTTSCaptureProtection();
            this.isRemoteSpeaking = false;
            if (this.onSessionStateChange) {
                this.onSessionStateChange(false);
            }

            // 在WebSocket连接成功时初始化Live2D音频分析器
            this.initializeLive2DAudioAnalyzer();

            await this.sendHelloMessage();
        };

        this.websocket.onclose = () => {
            log('已断开连接', 'info');

            if (this.onConnectionStateChange) {
                this.onConnectionStateChange(false);
            }

            const audioRecorder = getAudioRecorder();
            this.clearTTSCaptureProtection();
            audioRecorder.stop();
            getAudioPlayer().clearAllAudio('WebSocket已断开');
            this.isRemoteSpeaking = false;
            this.currentSessionId = null;

        };

        this.websocket.onerror = (error) => {
            const errorMessage = getWebSocketErrorMessage(error, this.websocket);
            this.lastErrorMessage = errorMessage;
            log(`WebSocket错误: ${errorMessage}`, 'error');
            if (!this.isConnecting) {
                uiController.addChatMessage(`⚠️ WebSocket错误: ${errorMessage}`, false);
            }
            if (this.onConnectionStateChange) {
                this.onConnectionStateChange(false);
            }
        };

        this.websocket.onmessage = (event) => {
            try {
                if (typeof event.data === 'string') {
                    const message = JSON.parse(event.data);
                    this.handleTextMessage(message);
                } else {
                    this.handleBinaryMessage(event.data);
                }
            } catch (error) {
                log(`WebSocket消息处理错误: ${error.message}`, 'error');
                // 不再使用旧的addMessage函数，因为conversationDiv元素不存在
                // 错误消息将通过其他方式显示
            }
        };
    }

    // 断开连接
    disconnect() {
        if (!this.websocket) return;

        this.websocket.close();
        const audioRecorder = getAudioRecorder();
        this.clearTTSCaptureProtection();
        audioRecorder.stop();
        getAudioPlayer().clearAllAudio('用户断开连接');

    }

    // 发送文本消息
    sendTextMessage(text) {
        if (text === '' || !this.websocket || this.websocket.readyState !== WebSocket.OPEN) {
            return false;
        }

        try {
            // 如果对方正在说话，先发送打断消息
            if (this.isRemoteSpeaking && this.currentSessionId) {
                const abortMessage = {
                    session_id: this.currentSessionId,
                    type: 'abort',
                    reason: 'wake_word_detected'
                };
                this.websocket.send(JSON.stringify(abortMessage));
                log('发送打断消息', 'info');
            }

            const listenMessage = {
                type: 'listen',
                state: 'detect',
                text: text
            };

            this.websocket.send(JSON.stringify(listenMessage));
            log(`发送文本消息: ${text}`, 'info');

            return true;
        } catch (error) {
            log(`发送消息错误: ${error.message}`, 'error');
            return false;
        }
    }


    // 获取WebSocket实例
    getWebSocket() {
        return this.websocket;
    }

    // 检查是否已连接
    isConnected() {
        return this.websocket && this.websocket.readyState === WebSocket.OPEN;
    }
}

// 创建单例
let wsHandlerInstance = null;

export function getWebSocketHandler() {
    if (!wsHandlerInstance) {
        wsHandlerInstance = new WebSocketHandler();
    }
    return wsHandlerInstance;
}
