Jellyfin Edge 2.0.0.0，兼容 Jellyfin Server / Web 12.2。

- 主站插件管理代理节点，提供原文件分块缓存与分享 HLS 缓存。
- 网页菜单生成媒体范围的分享、下载和串流链接；支持链接有效期、分页与批量撤销。
- ZIP 仅包含 Edge DLL 与许可声明；安装后重启主站。
- Linux amd64 镜像：`ghcr.io/lxfee/jellyfin-edge-host:2.0.0.0`、`ghcr.io/lxfee/jellyfin-edge-gateway:2.0.0.0`。

安装、部署及兼容边界见仓库 README 和 docs。独立默认主题等插件不包含在此发布中。
