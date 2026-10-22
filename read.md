# CPA Image MCP：复刻入口

这个项目把已经运行的 CLIProxyAPI 生图接口包装成远程 MCP，供 Claude 等客户端使用。支持生成、编辑、返回原图、签名下载链接和自动清理。完整原理、参数和排错说明见 [README.md](README.md)。

## 从零部署

1. 准备一台使用 systemd、Python 3.10+ 的 Linux VPS，以及能正常调用的 CLIProxyAPI 图片接口。
2. 克隆仓库并执行 `sudo bash install.sh`。首次仅生成 `/etc/cpa-image-mcp.env`，不会启动服务。
3. 在服务器本地编辑环境文件，填入自己的 `CPA_API_KEY` 和 `PUBLIC_HOST`。安装器已为 MCP 请求头和下载签名分别生成随机密钥。
4. 再执行安装脚本。确认 `systemctl is-active cpa-image-mcp` 为 active，`curl http://127.0.0.1:8787/healthz` 返回 ok。
5. 在自己的 Cloudflare Tunnel 添加 HTTPS 公网域名，源站类型选 **HTTP**，地址填 `127.0.0.1:8787`；NAT VPS 不需要额外公网端口。
6. Claude 自定义连接器填写 `https://images.example.com/mcp`（换成自己的域名），Authentication 选 No sign-in，再添加 `Authorization: Bearer <自己的 MCP_TOKEN>` 请求头。

## 核心思路

- CPA 请求走本机地址，MCP 用心跳保持长时间生图响应活跃。
- 生成/编辑只附 512px 预览；需要原图时用 `get_image` 或工具附带的 `download_url`。
- 下载链接用独立 HMAC 签名，有效期默认 72 小时；图片保留默认 3 天，启动和每小时清理。
- `get_image` 默认完整发送原图后删图，链接也失效。要保留下载链接或继续编辑，请传 `delete_after_return=false`。
- 尺寸按宽×高传递，并在提示词中补充方向和比例；实际尺寸始终按图片解码结果报告。

仓库只含示例域名和空凭据配置。不要提交真实 `.env`、API key、MCP token、签名密钥或生成图片。
