Jellyfin Edge 2.0.0.1，兼容 Jellyfin Server / Web 12.2。

- 主站插件管理代理节点，提供原文件分块缓存与分享 HLS 缓存。
- 主站直连保留原生下载与串流地址，避免局域网下载绕行云端；普通播放沿用入口。
- 代理访问继续生成媒体范围的下载和串流链接，分享始终使用代理；支持有效期与撤销。
- ZIP 仅包含 Edge DLL 与许可声明；安装后重启主站。
- Linux amd64 镜像：`ghcr.io/lxfee/jellyfin-edge-server:2.0.0.1`、`ghcr.io/lxfee/jellyfin-edge-proxy:2.0.0.1`。

安装、部署及兼容边界见仓库 README 和 docs。独立默认主题等插件不包含在此发布中。
