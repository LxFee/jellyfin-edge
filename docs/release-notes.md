Jellyfin Edge 2.0.0.2，兼容 Jellyfin Server / Web 12.2。

- 修复媒体库软链接被识别为链接本身大小，导致直接播放、下载及原文件串流分享返回 409 的问题。
- 文件大小、修改时间与缓存版本使用最终目标文件，下载文件名和媒体类型保留媒体库名称；普通文件行为保持一致。
- 发布集成覆盖软链接直接播放、下载、原文件串流与 HLS 分享；旧版生成的软链接分享需重新创建。
- ZIP 仅包含 Edge DLL 与许可声明；安装后重启主站。
- Linux amd64 镜像：`ghcr.io/lxfee/jellyfin-edge-server:2.0.0.2`、`ghcr.io/lxfee/jellyfin-edge-proxy:2.0.0.2`。本修复只涉及主站插件，已有代理可继续运行。

安装、部署及兼容边界见仓库 README 和 docs。独立默认主题等插件不包含在此发布中。
