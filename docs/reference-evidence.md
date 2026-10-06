# 参考实现与兼容基线

当前插件以 Jellyfin Server / Web 12.2 和 .NET 10 为兼容基线，并不声明支持其他版本。

- Server：[`v12.2`](https://github.com/jellyfin/jellyfin/releases/tag/v12.2)，源码固定 `ca0f16eb7c195f720a1493ed469a27c4657db0c6`。
- Web：[`v12.2`](https://github.com/jellyfin/jellyfin-web/releases/tag/v12.2)，源码固定 `61b1f890365ad15138e943e694af723c1d47df2a`。
- 主站镜像：官方 Linux amd64 镜像，digest `sha256:da3cd1e48322a35e4b60f3d0a49fca2649e7acce90346dd6e42db777f94e3bbd`。
- 分享输出参考：[Jellyfin VRC Relay a01f281](https://github.com/Tokiichika/jellyfin-vrc-relay/tree/a01f28194d6a2158170273f7e053d87f9de98f92)，MIT 许可，详见[第三方说明](../THIRD_PARTY_NOTICES.md)。

原生下载 URL 的权限和登录凭证行为见[鉴权说明](jellyfin-media-auth.md)。详情页选择来自当前页面状态，因此导出适配显式读取版本、音轨和字幕。[Web 实现](https://github.com/jellyfin/jellyfin-web/blob/v12.2/src/apps/legacy/controllers/itemDetails/index.js)

插件服务注册与 ASP.NET 启动流程允许安装同源脚本引导；Jellyfin Web 尚未提供服务端插件注册媒体菜单的接口，菜单使用对应版本的 DOM 适配。[插件管理器](https://github.com/jellyfin/jellyfin/blob/v12.2/Emby.Server.Implementations/Plugins/PluginManager.cs)、[启动过滤器](https://github.com/dotnet/aspnetcore/blob/v10.0.0/src/Hosting/Hosting/src/GenericHost/GenericWebHostService.cs)
