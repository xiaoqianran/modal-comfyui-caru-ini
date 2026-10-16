# 工作流命名规范

适用于 `workflows/` 与 Cloudflare R2 的输出分类。前缀描述**工作流来源**，而非模型开发者。

## 格式

`<来源前缀>-<模型名称>[-<版本或用途>]`

- 仅用小写英文字母、数字、连字符 `-`，不使用空格或下划线。
- 多模型组合使用 `-` 连接，例如 `pixal3d-trellis2`。
- 名称确定后尽量不变，避免 R2 分类路径变动。

## 前缀

| 前缀 | 来源 | 示例 |
| --- | --- | --- |
| `comfyui-` | ComfyUI 官方发布的模板或教程 | `comfyui-pixal3d-trellis2` |
| `vendor-` | 模型作者发布的官方工作流 | `vendor-hunyuan3d21` |
| `community-` | 第三方社区作者提供 | `community-trellis2-fast` |
| `custom-` | 本项目原创或明显改造的工作流 | `custom-pixal3d-fast` |

即使只使用 ComfyUI 原生节点，也不能据此判断工作流来自 ComfyUI 官方。

## 固定文件结构

```text
workflows/
  comfyui-pixal3d-trellis2/
    workflow.json
    models.json
    plugins.json
```

## 输出映射

目录名直接用于输出分类和 R2 对象键，无需额外映射：

```text
workflows/comfyui-pixal3d-trellis2/
ComfyUI/output/comfyui-pixal3d-trellis2/
R2: comfyui/comfyui-pixal3d-trellis2/<unique-filename>.glb
```

输出文件名必须唯一，避免覆盖历史结果。R2 路径首段 `comfyui/` 是项目命名空间；第二段的 `comfyui-` 是来源前缀。

## 当前迁移状态

当前目录仍为 `workflows/pixal3d/`。本规范仅定义后续命名；尚未重命名目录，也不表示 R2 自动上传已经接通。