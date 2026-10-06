# Jellyfin 下载与复制串流地址的鉴权

以下描述 Jellyfin Server / Web `v12.2` 的原生行为。

## 同一个下载入口

“下载媒体文件”和“复制串流地址”都使用 `GET /Items/{itemId}/Download`。复制操作只把下载 URL 写入剪贴板；两个菜单入口均受 `item.CanDownload` 控制。[Web 实现](https://github.com/jellyfin/jellyfin-web/blob/v12.2/src/components/itemContextMenu.js#L415)

下载接口要求 `Policies.Download`，普通用户需具备 `EnableContentDownloading` 权限，接口还检查媒体是否可下载。拥有播放权限不等于拥有下载权限。[下载接口](https://github.com/jellyfin/jellyfin/blob/v12.2/Jellyfin.Api/Controllers/LibraryController.cs)、[策略注册](https://github.com/jellyfin/jellyfin/blob/v12.2/Jellyfin.Server/Extensions/ApiServiceCollectionExtensions.cs)、[用户权限检查](https://github.com/jellyfin/jellyfin/blob/v12.2/Jellyfin.Api/Auth/UserPermissionPolicy/UserPermissionHandler.cs)

## 链接自带登录凭据

Web 使用 SDK 将当前用户的 `accessToken` 放入 URL 的 `ApiKey` 参数，例如 `/Items/{itemId}/Download?ApiKey=<用户登录 token>`。这个参数名不表示 Web 使用了管理员创建的 API 密钥。[URL 构造](https://github.com/jellyfin/jellyfin-sdk-typescript/blob/v1.0.0/src/utils/api/library-api.ts)、[参数名](https://github.com/jellyfin/jellyfin-sdk-typescript/blob/v1.0.0/src/constants.ts)

服务器从 URL 提取 token，查找其对应的设备和用户。因此接收完整链接的人通常无需再次登录，但请求仍按链接中用户的身份鉴权；删除参数且不提供其他有效凭据，不能匿名下载。这个 token 是用户登录凭据，未限定为只访问该媒体，转发链接也会暴露该凭据。[凭据解析与用户映射](https://github.com/jellyfin/jellyfin/blob/v12.2/Jellyfin.Server.Implementations/Security/AuthorizationContext.cs)

Web `v10.11.9` 同样复制下载 URL，旧客户端把 token 放在 `api_key` 参数中；`v12.2` 服务端仅在启用 `EnableLegacyAuthorization` 时接受这种旧参数。[旧 Web](https://github.com/jellyfin/jellyfin-web/blob/v10.11.9/src/components/itemContextMenu.js)、[旧 URL 构造](https://github.com/jellyfin/jellyfin-apiclient-javascript/blob/v1.11.0/src/apiClient.js)

## 有效期与撤销

原生用户 token 没有固定到期时间；鉴权按已保存的设备 token 查询，不检查创建时间或最后活动时间来决定是否过期。因此链接可以长期有效，其可用性仍依赖 token、用户权限和媒体存在。[设备凭据模型](https://github.com/jellyfin/jellyfin/blob/v12.2/src/Jellyfin.Database/Jellyfin.Database.Implementations/Entities/Security/Device.cs)、[设备查询](https://github.com/jellyfin/jellyfin/blob/v12.2/Jellyfin.Server.Implementations/Devices/DeviceManager.cs)

退出生成链接时使用的登录会删除该 token 对应的设备记录；同一用户在同一设备重新登录也会撤销旧 token 并生成新 token。管理员撤销登录、删除对应设备记录，或禁用用户，都可使旧链接无法使用。[登录与退出实现](https://github.com/jellyfin/jellyfin/blob/v12.2/Emby.Server.Implementations/Session/SessionManager.cs)、[禁用用户检查](https://github.com/jellyfin/jellyfin/blob/v12.2/Emby.Server.Implementations/HttpServer/Security/AuthService.cs)

本项目的主站转发保留账号鉴权；观众凭链接观看的分享点播属于另一个入口，见[需求边界](requirements.md)。
