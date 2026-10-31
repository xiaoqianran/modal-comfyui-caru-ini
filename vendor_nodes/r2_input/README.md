# R2 直接导入 ComfyUI 图片

无需 PC2 中转：ChatGPT 附件上传至 R2 后，调用 `POST /inputs/from_url`，Modal 容器直接下载到 ComfyUI input 目录。

## 部署

用对应 Modal Profile 创建只包含 `R2_IMPORT_KEY` 的 Secret `r2-import-auth`，再部署 `comfyui.py`。密钥只存 Modal Secret 和本地忽略版本控制的 `.env`，切勿提交仓库。

## API

```http
POST /inputs/from_url
Content-Type: application/json
X-Import-Key: <R2_IMPORT_KEY>

{"url":"https://chatgpt-file-transfer-mcp.wangran.workers.dev/download/chatgpt/...?..."}
```

返回 ComfyUI 标准输入描述：`{"name":"r2_<sha256>.png","subfolder":"","type":"input"}`。将 `name` 填入工作流的 LoadImage 节点后调用已有 `POST /prompt`。

仅允许指定 R2 域名及下载路径、HTTPS、无跳转；必须认证，限制 25 MiB、3600 万像素，图片内容校验 PNG/JPEG/WebP。文件名采用内容 SHA256，相同图同一容器中会复用文件名。容器重建后 `input` 不持久化，需要重新下载。

**注意**：这是单次 HTTP 请求、单次下载，不应在客户端并发重试未返回的请求。签名链接会过期，须在有效期内调用；HTTP 401 检查密钥，HTTP 400 检查 URL 或图片格式。
