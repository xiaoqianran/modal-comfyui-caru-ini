# MOSS-Image — ComfyUI 原生工作流

- 官方模板：Comfy-Org/workflow_templates/templates/image_moss_image_t2i_bboxes.json
- 官方模型：Comfy-Org/MOSS-Image；模型路径由 models.json 声明
- 无自定义节点：plugins.json 为空；要求 ComfyUI 包含 MOSS-Image 原生支持（提交 e087d8f6 或之后），并具有相应依赖
- 模板含内置子图、布局提示词和边界框节点；不要改用旧 OpenMOSS 插件版工作流
- 部署：.\deploy-modal.bat comfyui-moss-image
- 当前仅完成静态配置；尚未验证 Modal 构建或 GPU 推理
