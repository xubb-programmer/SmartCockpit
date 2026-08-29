# Qwen3.5-2B RK3588 模型文件

来源: https://huggingface.co/Qengineering/Qwen3.5-2B-rk3588

本目录保存的是 RK3588 可用的预转换模型文件，不是 Hugging Face 官方原始 safetensors 权重。

## 文件

- `qwen3.5-2b_w8a8_rk3588.rkllm`
  - RKLLM 文本/语言模型文件
  - 大小: 2,979,493,564 bytes
  - SHA256: `3185384CE7B13805B15F93848D2CEC549F6F2E97A0B4B623398CB5835BF62172`

- `qwen3.5-2b_vision_rk3588.rknn`
  - RKNN vision encoder 文件
  - 大小: 700,379,761 bytes
  - SHA256: `1976B3F70D776B452FB41E905D20535BCCFA0D70BAA279204A1B55B32485F5DC`

## 注意

当前项目启动脚本默认使用 `qwen3.5-0.8b`，尚未把 `qwen3.5-2b` 做成脚本模型别名。

如需在 RK3588 上尝试，需要将这两个文件复制到板端模型目录，并修改启动脚本或启动参数，让 `llm_server.py` 使用新的 `.rkllm` 和 `.rknn` 路径。
