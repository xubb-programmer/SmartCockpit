// 配置管理模块

// 生成随机MAC地址
function generateRandomMac() {
    const hexDigits = '0123456789ABCDEF';
    let mac = '';
    for (let i = 0; i < 6; i++) {
        if (i > 0) mac += ':';
        for (let j = 0; j < 2; j++) {
            mac += hexDigits.charAt(Math.floor(Math.random() * 16));
        }
    }
    return mac;
}

function getDefaultOtaUrl() {
    const { protocol, hostname, host } = window.location;
    if (protocol === 'https:') {
        return `${protocol}//${host}/voice_assistant/ota/`;
    }
    if (protocol === 'http:' && hostname && hostname !== 'localhost' && hostname !== '127.0.0.1') {
        return `http://${hostname}:18003/voice_assistant/ota/`;
    }
    return 'http://127.0.0.1:18003/voice_assistant/ota/';
}

function normalizeOtaUrl(otaUrl) {
    if (!otaUrl) {
        return getDefaultOtaUrl();
    }
    if (window.location.protocol !== 'https:') {
        return otaUrl;
    }
    try {
        const parsed = new URL(otaUrl);
        if (parsed.protocol === 'http:' && parsed.pathname.startsWith('/voice_assistant/ota/')) {
            return getDefaultOtaUrl();
        }
    } catch (error) {
        return getDefaultOtaUrl();
    }
    return otaUrl;
}

// 加载配置
export function loadConfig() {
    const deviceMacInput = document.getElementById('deviceMac');
    const deviceNameInput = document.getElementById('deviceName');
    const clientIdInput = document.getElementById('clientId');
    const otaUrlInput = document.getElementById('otaUrl');

    // 从localStorage加载MAC地址，如果没有则生成新的
    let savedMac = localStorage.getItem('xz_tester_deviceMac');
    if (!savedMac) {
        savedMac = generateRandomMac();
        localStorage.setItem('xz_tester_deviceMac', savedMac);
    }
    deviceMacInput.value = savedMac;

    // 从localStorage加载其他配置
    const savedDeviceName = localStorage.getItem('xz_tester_deviceName');
    if (savedDeviceName) {
        deviceNameInput.value = savedDeviceName;
    }

    const savedClientId = localStorage.getItem('xz_tester_clientId');
    if (savedClientId) {
        clientIdInput.value = savedClientId;
    }

    const savedOtaUrl = localStorage.getItem('xz_tester_otaUrl');
    otaUrlInput.value = normalizeOtaUrl(savedOtaUrl);
    localStorage.setItem('xz_tester_otaUrl', otaUrlInput.value);
}

// 保存配置
export function saveConfig() {
    const deviceMacInput = document.getElementById('deviceMac');
    const deviceNameInput = document.getElementById('deviceName');
    const clientIdInput = document.getElementById('clientId');

    localStorage.setItem('xz_tester_deviceMac', deviceMacInput.value);
    localStorage.setItem('xz_tester_deviceName', deviceNameInput.value);
    localStorage.setItem('xz_tester_clientId', clientIdInput.value);
}

// 获取配置值
export function getConfig() {
    // 从DOM获取值
    const deviceMac = document.getElementById('deviceMac')?.value.trim() || '';
    const deviceName = document.getElementById('deviceName')?.value.trim() || '';
    const clientId = document.getElementById('clientId')?.value.trim() || '';

    return {
        deviceId: deviceMac,  // 使用MAC地址作为deviceId
        deviceName,
        deviceMac,
        clientId
    };
}

// 保存连接URL
export function saveConnectionUrls() {
    const otaUrl = document.getElementById('otaUrl').value.trim();
    const wsUrl = document.getElementById('serverUrl').value.trim();
    localStorage.setItem('xz_tester_otaUrl', otaUrl);
    localStorage.setItem('xz_tester_wsUrl', wsUrl);
}
