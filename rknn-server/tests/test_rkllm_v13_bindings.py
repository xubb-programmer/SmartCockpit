import ctypes
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent


def load_rkllm_types():
    spec = importlib.util.spec_from_file_location("rkllm_types_under_test", ROOT / "rkllm_types.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def field_names(struct_type):
    return [name for name, _ in struct_type._fields_]


def test_rkllm_param_matches_v13_header_layout():
    types = load_rkllm_types()

    assert field_names(types.RKLLMParam) == [
        "model_path",
        "max_context_len",
        "max_new_tokens",
        "top_k",
        "n_keep",
        "top_p",
        "temperature",
        "repeat_penalty",
        "frequency_penalty",
        "presence_penalty",
        "mirostat",
        "mirostat_tau",
        "mirostat_eta",
        "skip_special_token",
        "ignore_eos_token",
        "is_async",
        "extend_param",
    ]
    assert "img_start" not in field_names(types.RKLLMParam)
    assert "img_end" not in field_names(types.RKLLMParam)
    assert "img_content" not in field_names(types.RKLLMParam)


def test_multimodal_input_uses_v13_nested_image_fields():
    types = load_rkllm_types()

    assert field_names(types.RKLLMMultiModalInput) == ["prompt", "image", "video"]
    assert field_names(types.RKLLMImageInput) == [
        "image_embed",
        "n_image_tokens",
        "n_image",
        "image_start",
        "image_end",
        "image_content",
        "image_width",
        "image_height",
    ]
    assert field_names(types.RKLLMVideoInput) == [
        "video_embed",
        "n_frame_tokens",
        "n_frame_per_video",
        "n_video",
        "video_start",
        "video_end",
        "video_content",
        "frame_width",
        "frame_height",
    ]


def test_infer_param_and_callback_match_v13_header_layout():
    types = load_rkllm_types()

    assert field_names(types.RKLLMInferParam) == [
        "mode",
        "lora_params",
        "prompt_cache_params",
        "sampling_params",
        "keep_history",
        "max_new_tokens",
    ]
    assert field_names(types.RKLLMCallback) == [
        "result_callback",
        "result_userdata",
        "tokenizer_callback",
        "tokenizer_userdata",
        "embed_callback",
        "embed_userdata",
    ]

    callback = types.RKLLM_Callback_t(lambda result, userdata, state: 0)
    wrapper = types.make_rkllm_callback(callback)
    assert isinstance(wrapper, types.RKLLMCallback)
    assert ctypes.cast(wrapper.result_callback, ctypes.c_void_p).value
    assert not ctypes.cast(wrapper.tokenizer_callback, ctypes.c_void_p).value
    assert not ctypes.cast(wrapper.embed_callback, ctypes.c_void_p).value


def test_llm_server_uses_official_qwen35_multimodal_arguments():
    text = (ROOT / "llm_server.py").read_text(encoding="utf-8")

    assert "parser.add_argument('--vision_rknn_model_path'" in text
    assert "default=48" in text
    assert "default='<|vision_start|>'" in text
    assert "default='<|vision_end|>'" in text
    assert "default='<|image_pad|>'" in text
    assert "base_domain_id = 1" in text
    assert "@app.route('/vision/analyze'" in text
    assert "@app.route('/rkllm_abort'" in text
    assert "_active_call_cancelled" in text
    assert "self.rkllm_abort.argtypes = [RKLLM_Handle_t]" in text
    assert "self.rkllm_is_running.argtypes = [RKLLM_Handle_t]" in text
    assert "multimodal_input.image.image_embed" in text
    assert "multimodal_input.image.image_start" in text
    assert "multimodal_input.image.image_end" in text
    assert "multimodal_input.image.image_content" in text


def test_start_scripts_use_official_single_instance_multimodal_architecture():
    expected = [
        "Qwen3.5-0.8B_vision_rk3588.rknn",
        "Qwen3.5-0.8B_w8a8_rk3588.rkllm",
        "VISION_MAX_NEW_TOKENS",
        "VISION_NPU_CORE",
        "VISION_PREENCODE_INTERVAL",
        "--vision_rknn_model_path",
    ]
    scripts = [
        ROOT / "start_llm.sh",
        PROJECT_ROOT / "voice-assistant-server" / "scripts" / "deploy" / "voice-assistant.sh",
    ]

    for script in scripts:
        text = script.read_text(encoding="utf-8")
        for needle in expected:
            assert needle in text, f"{needle} missing from {script}"
        assert "python3 vision_server.py" not in text

    assert "RKLLM_MODEL_BASENAME" in (ROOT / "start_llm.sh").read_text(encoding="utf-8")
    start_llm_text = (ROOT / "start_llm.sh").read_text(encoding="utf-8")
    assert "是纯文本或未知模型，不支持 RKLLM_ENABLE_VISION=true" in start_llm_text
    assert "qwen3.5-2b_w8a8_rk3588.rkllm" in start_llm_text

    for script in scripts[1:]:
        text = script.read_text(encoding="utf-8")
        assert "PROMPT_CACHE_ARGS=()" in text
        assert "Qwen3.5 RKLLM 不支持保存 prompt cache" in text
        assert 'if [[ "${ENABLE_VISION}" == "true" ]]; then' in text
        assert '"${VISION_ARGS[@]}"' in text

    llm_server_text = (ROOT / "llm_server.py").read_text(encoding="utf-8")
    assert "conversation_id is required to isolate RKLLM KV state" in llm_server_text
    assert "同步接口切换会话" in llm_server_text
    assert "--text_max_new_tokens" in llm_server_text
    for script in scripts:
        assert "--text_max_new_tokens" in script.read_text(encoding="utf-8")
        assert "--vision_preencode_interval" in script.read_text(encoding="utf-8")

    deploy_text = scripts[1].read_text(encoding="utf-8")
    config_text = (
        PROJECT_ROOT / "voice-assistant-server" / "data" / ".config.yaml"
    ).read_text(encoding="utf-8")
    tool_text = (ROOT / "tools" / "test_vision_qwen35.py").read_text(encoding="utf-8")
    assert "LEGACY_VISION_RKLLM_PORT=18081" in deploy_text
    assert "VISION_RKLLM_LOG" not in deploy_text
    assert deploy_text.count("python3 llm_server.py") == 1
    assert '"${WARMUP_ARGS[@]}" \\' in deploy_text
    assert "http://127.0.0.1:18080/vision/analyze" in config_text
    assert 'default="http://127.0.0.1:18080"' in tool_text
    assert 'RKLLM_ENABLE_VISION="${RKLLM_ENABLE_VISION:-false}"' in scripts[0].read_text(encoding="utf-8")
    service_text = (
        PROJECT_ROOT / "voice-assistant-server" / "scripts" / "deploy"
        / "voice-assistant.service"
    ).read_text(encoding="utf-8")
    assert 'MODEL_SIZE="${VOICE_ASSISTANT_MODEL:-qwen3-4b}"' in deploy_text
    assert "Environment=VOICE_ASSISTANT_MODEL=qwen3-4b" in service_text
    assert "VOICE_ASSISTANT_VISION_MODEL" not in service_text
    assert "VISION_MODEL_SIZE" not in deploy_text
    assert '--rkllm_model_path "${VISION_MODEL_PATH}"' not in deploy_text
    assert "local ENABLE_VISION=false" in deploy_text
    assert "--vision)      ENABLE_VISION=true" in deploy_text
    assert "是纯文本模型，不支持 --vision" in deploy_text
    assert "同一多模态实例" in deploy_text
    assert "--no-vision" not in deploy_text

    assert not (ROOT / "vision_server.py").exists()
    assert not (ROOT / "start_vision.sh").exists()


def test_prompt_cache_matches_runtime_vision_mode():
    shell_text = (
        PROJECT_ROOT / "voice-assistant-server" / "scripts" / "deploy" / "generate_prompt_cache.sh"
    ).read_text(encoding="utf-8")
    start_text = (ROOT / "start_llm.sh").read_text(encoding="utf-8")
    py_text = (ROOT / "generate_prompt_cache.py").read_text(encoding="utf-8")

    assert 'MODEL_SIZE="qwen3.5-0.8b"' in shell_text
    assert "PROMPT_CACHE_SUPPORTED=false" in shell_text
    assert "voice_assistant_static_prompt_${PROMPT_MODE}.txt" in shell_text
    assert "启动脚本会使用静态 prompt 预热 set_function_tools" in shell_text
    assert "voice_assistant_static_prompt_${PROMPT_MODE}.txt" in start_text
    assert '[[ "${RKLLM_ENABLE_VISION}" == "true" ]] && PROMPT_MODE="vision"' in start_text
    assert 'parser.add_argument("--multimodal"' in py_text
    assert "base_domain_id = 1" in py_text


def test_qwen35_vision_validation_tool_is_documented():
    tool_path = ROOT / "tools" / "test_vision_qwen35.py"
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
    tool_text = tool_path.read_text(encoding="utf-8")

    assert tool_path.exists()
    assert "EXPECTED_HEALTH" in tool_text
    assert "http://127.0.0.1:18080" in tool_text
    assert "Qwen3.5" in readme_text
    assert "不支持保存 prompt cache" in readme_text
    assert "test_vision_qwen35.py" in readme_text


def test_llm_server_skips_slow_prefill_for_stale_delta():
    text = (ROOT / "llm_server.py").read_text(encoding="utf-8")
    prepare_start = text.index("def _prepare_inference")
    prepare_end = text.index("# ── RKLLM 模型封装")
    prepare_text = text[prepare_start:prepare_end]

    assert "跳过慢速prefill" in prepare_text
    assert "len(new_msgs) > 1" in prepare_text
    assert "clear_kv_cache(keep_system_prompt=1)" in prepare_text
    assert "_prefill_event.set()" not in prepare_text
    assert "_recover_after_run_error" in text
    assert "流式推理失败" in text


def test_llm_server_waits_for_sync_runtime_before_releasing_handle():
    text = (ROOT / "llm_server.py").read_text(encoding="utf-8")

    assert "INFERENCE_LOCK_WAIT_SECONDS = 30.0" in text
    assert text.count("lock.acquire(timeout=INFERENCE_LOCK_WAIT_SECONDS)") >= 2
    # 当前 RKLLM runtime 的同步 run 不能可靠地被 abort 立即打断；
    # 未等到底层线程退出就释放锁会再次制造并发 RunError。
    assert "model_thread.join(timeout=5.0)" not in text
    assert "model_thread.join(timeout=2)" not in text


def test_cancel_callback_does_not_suspend_rkllm_handle():
    text = (ROOT / "llm_server.py").read_text(encoding="utf-8")
    callback_start = text.index("def callback_impl")
    cancel_start = text.index("if _active_call_cancelled.is_set():", callback_start)
    prefill_start = text.index("if _prefill_event.is_set():", cancel_start)
    cancel_block = text[cancel_start:prefill_start]

    # RKLLM v1.3 callback return 1 means pause/resume, not cancellation.
    assert "return 1" not in cancel_block
    assert "return 0" in cancel_block
    assert "rkllm_abort" in cancel_block
    assert "lock_wait=" in text
    assert "rkllm_model.abort()" not in text
    assert '"status": "cancelling"' in text


def test_offline_vision_config_matches_server_token_cap():
    config_path = PROJECT_ROOT / "voice-assistant-server" / "data" / ".config.yaml"
    if not config_path.exists():
        return
    text = config_path.read_text(encoding="utf-8")
    vision_start = text.index("rknn_vision:")
    vision_end = text.find("\n\n", vision_start)
    vision_block = text[vision_start:] if vision_end == -1 else text[vision_start:vision_end]
    assert "max_tokens: 48" in vision_block


def test_text_priority_pauses_background_vision_preencoding():
    text = (ROOT / "llm_server.py").read_text(encoding="utf-8")

    assert "NPU_PRIORITY_DIR" in text
    assert "def _text_priority_active" in text
    assert "priority_lease = _acquire_text_priority" in text
    assert text.count("_release_text_priority(priority_lease)") >= 3
    assert "and not _text_priority_active()" in text


def test_vehicle_prompt_uses_compact_protocol_and_stays_small():
    profile_text = (
        PROJECT_ROOT / "voice-assistant-server" / "data" / "vehicle_profiles"
        / "z1_scout.yaml"
    ).read_text(encoding="utf-8")
    prompt_start = profile_text.index("prompt: |")
    prompt_end = profile_text.index("tool_calling_rules:", prompt_start)
    prompt = profile_text[prompt_start:prompt_end]

    assert len(prompt) < 3000
    for token in ("@C|target|action", "@S|target", "@V|问题", "@M|歌名", "@D|all", "@X"):
        assert token in prompt
    for action in (
        "focus_out", "brightness_up", "enhance_on", "scan_circle",
        "digital_guide", "recon_start",
    ):
        assert action in prompt
    assert "@C|vis_cam|digital_zoom|zoom_level=4" in prompt
    assert "顺时针严格用direction=cw" in prompt
    assert "逆时针严格用direction=ccw" in prompt
    assert "@C|gimbal|scan_circle|direction=cw" in prompt
    assert "@C|gimbal|scan_circle|direction=ccw" in prompt
    assert "@C|gimbal|digital_guide|azimuth=82.67,pitch=10.5" in prompt
    assert "稳台扇扫=>当前车型不支持该功能" in prompt
    assert "未列出的动作不得近似或替换为已有动作" in prompt
    assert "scan_circle仅用于用户明确要求周扫或全周扫描" in prompt
    assert "每个动作单独一行@C" in prompt


def test_vehicle_runtime_has_no_raw_query_control_correction():
    control_text = (
        PROJECT_ROOT / "voice-assistant-server" / "plugins_func" / "functions"
        / "vehicle_control.py"
    ).read_text(encoding="utf-8")
    connection_text = (
        PROJECT_ROOT / "voice-assistant-server" / "core" / "connection.py"
    ).read_text(encoding="utf-8")
    profile_text = (
        PROJECT_ROOT / "voice-assistant-server" / "data" / "vehicle_profiles"
        / "z1_scout.yaml"
    ).read_text(encoding="utf-8")

    for legacy_name in (
        "_correct_action_from_query",
        "_validate_explicit_control_intent",
        "reorder_explicit_control_calls",
        "required_in_query",
        "action_aliases",
    ):
        assert legacy_name not in control_text
        assert legacy_name not in profile_text
    assert "reorder_explicit_control_calls" not in connection_text


def run_all_tests():
    tests = [
        value for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"tests passed: {len(tests)}")


if __name__ == "__main__":
    run_all_tests()
