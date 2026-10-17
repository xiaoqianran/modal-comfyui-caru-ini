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

## 配置与工作流文件

```text
workflows/community-sharp/
  models.json
  plugins.json
  sharp_basic.json
  sharp_exif.json
  pano.json
  vanilla_vs_image_depth_gaussians.json
```

每个环境必须有 `models.json`、`plugins.json` 与至少一个顶层工作流 JSON。
一个目录只部署一个 Modal ComfyUI 环境，但可包含多个 UI 格式的工作流。
插件仅由 `plugins.json` 配置；工作流 JSON 通过 `copy=False` 运行时挂载到
`ComfyUI/user/default/workflows/<环境名>/`，不触发镜像构建。
编辑 JSON 后重新执行 `modal deploy` 更新运行时文件，但并非实时同步。
多个 JSON 使用未安装的节点时仍需更新 `plugins.json` 并重新构建插件层。

## 输出映射

目录名直接用于输出分类和 R2 对象键，无需额外映射：

```text
workflows/comfyui-pixal3d-trellis2/
ComfyUI/output/3d/<filename>.glb
R2: comfyui/comfyui-pixal3d-trellis2/3d/<filename>.glb
```

输出文件名必须唯一，避免覆盖历史结果。R2 路径首段 `comfyui/` 是项目命名空间；第二段的 `comfyui-` 是来源前缀。

## 实现状态

文件识别规则已接入部署代码；`ComfyUI/output/3d` 通过 Modal CloudBucketMount 直连 R2。需要先配置 `cloudflare-r2` Modal Secret，实际远端写入仍需部署验证。