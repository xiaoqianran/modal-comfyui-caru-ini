# Qwen-Image-2.1（ComfyUI 官方工作流）

- 文生图：`image_qwen_image_2_1_t2i.json`
- 图片编辑：`image_qwen_image_2_1_image_edit.json`
- 来源：https://github.com/Comfy-Org/workflow_templates/tree/main/templates
- 权重：https://huggingface.co/Comfy-Org/Qwen-Image-2.1
- 部署：`deploy-modal.bat comfyui-qwen-image-2-1`
- 默认 GPU：RTX PRO 6000（如需在 L40S 上测试，可设置 `MODAL_GPU=L40S` 覆盖）

采用官方 workflow JSON，仅将主扩散模型与 Qwen3-VL 编码器从 INT8 改为 BF16；另两个 Qwen3.5 编码器使用官方 INT8 文件。默认 25 步，其他图结构保持官方版本。

`example-inputs/` 保存官方图像编辑示例图片；使用图片编辑工作流之前，请将这两张图片手动上传至 ComfyUI 的 input，或在 LoadImage 节点选用自行上传的图片。项目目前只自动挂载 workflow JSON，不自动上传示例图片。

注意：需要支持 Qwen-Image-2.1 的新版本 ComfyUI；本地 JSON 校验不代表远端模型下载、部署和运行测试已经完成。
