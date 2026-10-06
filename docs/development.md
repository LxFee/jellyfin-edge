# 构建与发布

使用 .NET 10 SDK、Python 3.12 与 Linux Docker。准备固定 Jellyfin 源码并构建：

```bash
git clone --depth 1 --branch v12.2 https://github.com/jellyfin/jellyfin.git jellyfin
test "$(git -C jellyfin rev-parse HEAD)" = ca0f16eb7c195f720a1493ed469a27c4657db0c6
dotnet build plugin/Jellyfin.Plugin.Edge.csproj -c Release
dotnet run --project plugin/tests/Edge.Tests.csproj -c Release
python -m pip install -r edge/requirements.txt -r edge/requirements-dev.txt
python -m pytest -q edge
python -m unittest discover -s plugin/tests -p test_external_web.py
```

插件 ZIP 只包含 Edge DLL 与许可说明，不分发 Jellyfin 主站程序集、PDB、账号配置或节点凭证。两个镜像使用仓库根目录作为构建上下文，分别指定 `plugin/Dockerfile.host` 和 `edge/Dockerfile`；构建白名单只包含代码、插件与许可说明。

## 发布版本

同步修改 `.csproj` 与 `plugin/manifest.json` 的版本、兼容 ABI 和更新说明，再提交并推送 `v<四段版本>` 标签，例如 `v2.0.0.0`。

GitHub Actions 在测试、构建镜像与隔离 HTTP 集成检查通过后发布：

- GitHub Release：插件 ZIP、`SHA256SUMS`、该版本目录记录。
- GHCR：`jellyfin-edge-server` 与 `jellyfin-edge-proxy`，标签包括四段版本、完整提交和 `latest`。
- `plugin-repository` 分支：插件目录，保留已发布版本，使用实际 ZIP 的 MD5 与固定下载 URL。

MD5 用于 Jellyfin 安装器协议校验，SHA256SUMS 用于独立下载核对。发布使用 Actions 的 `GITHUB_TOKEN`，不需要在源码中设置发布密钥。首次发布后须确认两个 GHCR 包的可见性为 Public，支持匿名拉取。

## 截图

文档图片来自隔离 Jellyfin 演示实例，使用合成视频、示例节点与地址。截图不包含注册 token、用户凭证、真实媒体库或私有部署拓扑；生成脚本和临时凭证保存在忽略目录中。
