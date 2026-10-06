# 缓存与媒体授权协议

## 主站接口

| 接口 | 身份及行为 |
|---|---|
| `POST JellyfinEdge/enroll` | 注册凭证与持久实例 nonce；发现默认禁用节点 |
| `GET JellyfinEdge/node/config` | 节点 Bearer；返回节点配置、缓存清空代数、有效授权记录及轮换凭据 |
| `GET JellyfinEdge/node/files/{itemId}` | 原生用户身份及 `X-Jellyfin-Edge-Node`；检查操作权限、所选版本，提供路径/大小/修改时间派生的身份 |
| `GET/HEAD JellyfinEdge/node/files/{itemId}/bytes` | 同上，额外要求匹配的缓存键；返回可 Range 读取的原文件 |
| `POST JellyfinEdge/node/files/{itemId}/audit` | 同上，并检查 Download 策略；缓存下载发出内容前复验及记录原生下载活动 |
| `POST JellyfinEdge/exports` | 原生用户身份；创建 `share`、`download` 或 `stream` 授权 |
| `GET JellyfinEdge/node/exports/{id}` | 节点 Bearer 与 `X-Jellyfin-Edge-Media`；仅返回匹配节点、媒体凭证和有效期的记录 |
| `GET/HEAD JellyfinEdge/node/exports/{id}/bytes` | 同上；仅允许该记录的原文件输出，重验来源版本 |
| `GET JellyfinEdge/node/exports/{id}/playlist` | 同上；主站原生 HLS 适配层生成固定参数的 VOD 清单 |
| `GET JellyfinEdge/node/exports/{id}/segments/{segment}` | 同上；固定媒体与输出参数，时间范围限定在影片内 |
| `GET JellyfinEdge/admin/exports?page=1&pageSize=20` | 管理员；按创建时间倒序返回 `Items`、`TotalCount`、`Page`、`PageSize`；每页 1–100 条，超过末页时返回末页 |
| `POST JellyfinEdge/admin/exports/revoke` | 管理员；JSON `Ids` 为 1–10000 个链接 ID，一次持久提交；返回新增撤销、已撤销、不存在的数量，重复 ID 只计一次 |
| `POST JellyfinEdge/admin/exports/{id}/revoke` | 管理员；保留单条撤销接口 |

管理接口使用 Jellyfin 提权策略：列出/撤销导出记录、节点配置、凭据轮换及 `clear-cache`。公共插件配置没有登录、注册或媒体 token；节点私有配置同步可取得媒体 token 的 SHA-256，供离线校验。

## 节点缓存

原文件按配置块大小填充，缓存身份包含来源键、清空代数、块大小和块序号。每个原生请求均先经主站判权；不同账号可复用相同内容块。回源 Range 的长度、总长度和 ETag 都匹配才提交完整块。不缓存原生客户端转码。

分享 HLS 的身份包含会话产物键、清空代数及清单中的分片时间/序号。公开清单只使用同一导出记录的 `media_token`，不含主站账号或节点凭据。清单与分片采用按需回源和 LRU；容量统计媒体对象，不含索引与授权元数据。当前节点使用单个网关进程处理并发请求。

关闭节点缓存后，原生请求透明转发，导出仍可在线回源播放及下载；导出不读取或写入媒体缓存，也不能离线使用。

SQLite 保存容量与访问索引；独立文件暂存完整对象，fsync 后原子替换。每个键合并并发填充，不同键可并发回源。清空命令增加代数，取消旧填充的缓存提交资格；节点变更来源身份时清空旧对象。

## 离线及撤销

节点保存最后确认的策略和媒体授权，逐次检查资源、节点、token 哈希及到期时间。导出元数据探测使用短超时，失败后短暂退避；完整缓存的原文件或完整 VOD 清单与全部分片可离线读取，缺失内容返回不可用。

在线授权检查拒绝或配置同步删除授权时，节点持久删除相应记录。撤销节点身份时，收到主站 401/403 会禁用已确认策略。主站不可达时无法得知新的撤销；授权仍受原有效期限制。普通 Jellyfin 账号请求没有离线判权能力。

HTTP 默认透明转发，保留现代认证、原始 query、压缩、Cookie 和重定向语义。外部节点/转发身份头被丢弃，网关重建可信节点及规范客户端 IP；共享 HTTP 客户端不保存用户 Cookie。可信网络和主站 KnownProxies 配置仍由部署环境提供。
