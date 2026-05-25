import os
import cv2
import json
import ctypes
import traceback
import threading
import queue
import numpy as np
from flask import Flask, request, jsonify

# 使用共享类型定义（消除 3 份重复代码）
from rkllm_types import (
    RKLLMParam, RKLLMInput, RKLLMResult, RKLLMInferParam,
    RKLLMMultiModalInput, RKLLMInputUnion,
    RKLLMInferMode, RKLLMInputType, LLMCallState,
    RKLLM_Handle_t, RKLLM_Callback_t, drain_queue,
)

# 加载 C 库（使用相对路径，与 llm_server.py 保持一致）
rkllm_lib = ctypes.CDLL('lib/librkllmrt.so')

# ── 线程安全的推理状态 ──────────────────────────────────────────
global_text = queue.SimpleQueue()
token_event = threading.Event()
# 修复竞态条件：用 Event 替代裸 bool，线程安全
_generating_event = threading.Event()  # set() 表示正在生成


def callback_impl(result, userdata, state):
    if state == LLMCallState.RKLLM_RUN_FINISH or state == LLMCallState.RKLLM_RUN_ERROR:
        _generating_event.clear()  # 生成结束
        token_event.set()
    elif state == LLMCallState.RKLLM_RUN_NORMAL:
        if result.contents.text is not None:
            global_text.put(result.contents.text.decode('utf-8'))
            token_event.set()
    return 0

callback = RKLLM_Callback_t(callback_impl)


# ==============================================================================
# Model Wrapper Class
# ==============================================================================
class MultimodalModel:
    def __init__(self, rknn_model_path, rkllm_model_path, npu_core=1):
        print("[Init] Loading Vision Encoder...")
        try:
            from rknnlite.api import RKNNLite
        except ImportError:
            print("ERROR: Please install rknn-toolkit-lite2 on the board")
            raise

        # 1. Init RKNN (Vision)
        self.rknn = RKNNLite()
        ret = self.rknn.load_rknn(rknn_model_path)
        if ret != 0:
            raise ValueError("Load RKNN model failed")
        
        # NPU 核心分配
        if npu_core == 0:
            core_mask = RKNNLite.NPU_CORE_0
        elif npu_core == 1:
            core_mask = RKNNLite.NPU_CORE_1
        elif npu_core == 2:
            core_mask = RKNNLite.NPU_CORE_2
        elif npu_core == 12:
            core_mask = RKNNLite.NPU_CORE_0_1
        elif npu_core == 3:
            core_mask = RKNNLite.NPU_CORE_0_1_2
        else:
            core_mask = RKNNLite.NPU_CORE_AUTO
        
        ret = self.rknn.init_runtime(core_mask=core_mask)
        if ret != 0:
            raise ValueError("Init RKNN runtime failed")

        # Qwen3-VL-2B 默认输入为 448x448
        self.model_width = 448
        self.model_height = 448
        print(f"[Init] Vision model input: {self.model_width}x{self.model_height}")

        # 通过一次 dummy 推理获取输出形状
        print("[Init] Running dummy inference to detect output shape...")
        dummy_input = np.zeros((1, self.model_height, self.model_width, 3), dtype=np.uint8)
        dummy_outputs = self.rknn.inference(inputs=[dummy_input])
        
        self.n_output = len(dummy_outputs)
        out_shape = dummy_outputs[0].shape
        flat_shape = list(out_shape)
        self.n_image_tokens = 0
        self.embed_size = 0
        for i in range(len(flat_shape)):
            if flat_shape[i] > 1:
                self.n_image_tokens = flat_shape[i]
                if i + 1 < len(flat_shape):
                    self.embed_size = flat_shape[i + 1]
                break

        if self.n_image_tokens == 0:
            print("[Init] WARNING: Could not determine n_image_tokens, using default 3136")
            self.n_image_tokens = 3136
            self.embed_size = flat_shape[-1] if len(flat_shape) > 0 else 1536

        print(f"[Init] Vision output shape: {[list(o.shape) for o in dummy_outputs]}")
        print(f"[Init] n_image_tokens={self.n_image_tokens}, "
              f"embed_size={self.embed_size}, n_output={self.n_output}")

        print("[Init] Loading LLM Model...")
        # 2. Init RKLLM (Text)
        param = RKLLMParam()
        param.model_path = bytes(rkllm_model_path, 'utf-8')
        param.max_context_len = 4096
        param.max_new_tokens = 96
        param.top_k = 1
        param.n_keep = -1
        param.top_p = 0.9
        param.temperature = 0.8
        param.repeat_penalty = 1.1
        param.frequency_penalty = 0.0
        param.presence_penalty = 0.0
        param.mirostat = 0
        param.mirostat_tau = 5.0
        param.mirostat_eta = 0.1
        param.skip_special_token = True
        param.is_async = False
        
        # Qwen3-VL tags
        param.img_start = b"<|vision_start|>"
        param.img_end = b"<|vision_end|>"
        param.img_content = b"<|image_pad|>"

        param.extend_param.base_domain_id = 1
        param.extend_param.embed_flash = 1
        param.extend_param.n_batch = 1
        param.extend_param.use_cross_attn = 0
        param.extend_param.enabled_cpus_num = 4
        param.extend_param.enabled_cpus_mask = (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7)

        self.handle = RKLLM_Handle_t()
        ret = rkllm_lib.rkllm_init(ctypes.byref(self.handle), ctypes.byref(param), callback)
        if ret != 0:
            raise ValueError("RKLLM Init Failed")
            
        print("[Init] Models loaded successfully!")
        self.lock = threading.Lock()

        # Embedding 缓存
        self._embed_cache = None
        self._embed_cache_key = None

    def process_image(self, img_path):
        if not os.path.exists(img_path):
            raise FileNotFoundError(f"Image not found: {img_path}")

        # 图片 embedding 缓存：同一张图片跳过 ~500ms 的 RKNN 推理
        img_mtime = os.path.getmtime(img_path)
        cache_key = (img_path, img_mtime)
        if self._embed_cache is not None and self._embed_cache_key == cache_key:
            print(f"[Cache] Reusing cached embedding for {img_path}")
            return self._embed_cache

        img = cv2.imread(img_path)
        if img is None:
            raise ValueError(f"Failed to read image: {img_path}")
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        
        # Expand to square
        h, w = img.shape[:2]
        if h == w:
            square = img.copy()
        else:
            size = max(h, w)
            square = np.full((size, size, 3), 128, dtype=np.uint8)
            y_off = (size - h) // 2
            x_off = (size - w) // 2
            square[y_off:y_off+h, x_off:x_off+w] = img
        
        resized = cv2.resize(square, (self.model_width, self.model_height), interpolation=cv2.INTER_LINEAR)
        nhwc = resized[np.newaxis, :, :, :].copy()
        
        outputs = self.rknn.inference(inputs=[nhwc])
        
        if len(outputs) == 1:
            embed = outputs[0].flatten().astype(np.float32)
        else:
            n_tokens = self.n_image_tokens
            embed_size = self.embed_size
            n_out = len(outputs)
            total_embed_len = n_tokens * embed_size * n_out
            embed = np.zeros(total_embed_len, dtype=np.float32)
            
            for i in range(n_tokens):
                for j in range(n_out):
                    out_flat = outputs[j].flatten().astype(np.float32)
                    src_offset = i * embed_size
                    dst_offset = i * n_out * embed_size + j * embed_size
                    embed[dst_offset:dst_offset + embed_size] = out_flat[src_offset:src_offset + embed_size]

        self._embed_cache = embed.copy()
        self._embed_cache_key = cache_key
        print(f"[Cache] Cached embedding for {img_path} (mtime={img_mtime:.0f})")

        return embed

    def chat(self, prompt_text, image_path):
        with self.lock:
            drain_queue(global_text)
            _generating_event.set()  # 标记生成中
            
            embed = self.process_image(image_path)
            embed_ptr = embed.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            
            inp = RKLLMInput()
            inp.role = b"user"
            inp.enable_thinking = False
            inp.input_type = RKLLMInputType.RKLLM_INPUT_MULTIMODAL
            
            inp.input_data.multimodal_input.prompt = bytes("<image>" + prompt_text, 'utf-8')
            inp.input_data.multimodal_input.image_embed = embed_ptr
            inp.input_data.multimodal_input.n_image_tokens = self.n_image_tokens
            inp.input_data.multimodal_input.n_image = 1
            inp.input_data.multimodal_input.image_width = self.model_width
            inp.input_data.multimodal_input.image_height = self.model_height

            infer_param = RKLLMInferParam()
            ctypes.memset(ctypes.byref(infer_param), 0, ctypes.sizeof(RKLLMInferParam))
            infer_param.mode = RKLLMInferMode.RKLLM_INFER_GENERATE
            infer_param.keep_history = 0

            rkllm_lib.rkllm_run(self.handle, ctypes.byref(inp), ctypes.byref(infer_param), None)
            
            output_str = ""
            while _generating_event.is_set() or not global_text.empty():
                token_event.wait(0.1)
                token_event.clear()
                while not global_text.empty():
                    try:
                        output_str += global_text.get_nowait()
                    except Exception:
                        break
                    
            return output_str.strip()


# ==============================================================================
# Flask App
# ==============================================================================
app = Flask(__name__)
model_instance = None

# 路径遍历防护：白名单目录
ALLOWED_IMAGE_DIRS = ["/tmp", "/data/images", "/var/tmp"]


def _is_path_allowed(path):
    """检查路径是否在白名单目录内"""
    real_path = os.path.realpath(path)
    return any(real_path.startswith(os.path.realpath(d)) for d in ALLOWED_IMAGE_DIRS)


@app.route('/vision/analyze', methods=['POST'])
def analyze():
    data = request.json
    if not data or 'image_path' not in data or 'question' not in data:
        return jsonify({"error": "Missing image_path or question"}), 400
        
    img_path = data['image_path']
    question = data['question']

    # 路径遍历防护
    if not _is_path_allowed(img_path):
        return jsonify({"error": f"Path not allowed. Allowed dirs: {ALLOWED_IMAGE_DIRS}"}), 403
    
    try:
        print(f"Analyzing image: {img_path} with question: {question}")
        answer = model_instance.chat(question, img_path)
        return jsonify({"result": answer})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """健康检查接口"""
    if model_instance is None:
        return jsonify({"status": "not_ready"}), 503
    return jsonify({
        "status": "ok",
        "model_width": model_instance.model_width,
        "model_height": model_instance.model_height,
        "n_image_tokens": model_instance.n_image_tokens,
        "embed_size": model_instance.embed_size,
        "n_output": model_instance.n_output
    })

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--rknn_model', required=True, help='Path to vision.rknn')
    parser.add_argument('--rkllm_model', required=True, help='Path to model.rkllm')
    parser.add_argument('--npu_core', type=int, default=1, help='NPU core: 0/1/2 for single core, 3 for triple, 12 for dual')
    parser.add_argument('--port', type=int, default=int(os.environ.get('VISION_PORT', 18090)))
    parser.add_argument('--allowed_dirs', type=str, default=None, help='Comma-separated list of allowed image directories')
    args = parser.parse_args()

    if args.allowed_dirs:
        ALLOWED_IMAGE_DIRS.clear()
        ALLOWED_IMAGE_DIRS.extend(d.strip() for d in args.allowed_dirs.split(','))

    model_instance = MultimodalModel(args.rknn_model, args.rkllm_model, npu_core=args.npu_core)
    app.run(host='0.0.0.0', port=args.port, threaded=True)
