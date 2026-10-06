# 第三方项目与许可

Jellyfin Edge 自有代码使用 [MIT License](LICENSE)。依赖及容器内的软件保留各自的许可证。

- [Jellyfin Server](https://github.com/jellyfin/jellyfin) 与 [Jellyfin Web](https://github.com/jellyfin/jellyfin-web)：提供服务端插件 API、网页和转码能力，以对应项目发布版本的 LICENSE 为准。主站镜像基于 Jellyfin 官方镜像，插件 ZIP 不包含 Jellyfin 程序集。
- [Jellyfin VRC Relay](https://github.com/Tokiichika/jellyfin-vrc-relay)：参考其分享点播、主站 HLS 转码与字幕烘焙方案，参考版本为 [a01f281](https://github.com/Tokiichika/jellyfin-vrc-relay/tree/a01f28194d6a2158170273f7e053d87f9de98f92)。该项目使用 MIT License，Copyright (c) 2026 Jellyfin VRC Relay contributors。Jellyfin Edge 的插件管理、多节点和缓存实现由本项目维护。
- 代理运行时依赖 Python、Starlette、HTTPX、Uvicorn 与 websockets，许可证随其发行包保留。

## Jellyfin VRC Relay 的 MIT 许可声明

Copyright (c) 2026 Jellyfin VRC Relay contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
