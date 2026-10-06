# Jellyfin 分享点播输出

分享点播由 Jellyfin 媒体菜单创建，输出沿用参考工程的 Jellyfin 方式。参考代码固定为 [jellyfin-vrc-relay a01f281](https://github.com/Tokiichika/jellyfin-vrc-relay/tree/a01f28194d6a2158170273f7e053d87f9de98f92)，以明确设计依据。

## 生成选项

从视频详情页复制分享链接时，使用该页面当前选择的媒体版本、音轨和字幕，明确选择不显示字幕也作为有效选择保留。其他入口缺少这些选择上下文时，使用默认策略；默认策略在插件配置页面管理。分享操作直接生成并复制，不增加独立的参数选择弹窗。

## 媒体输出

转码点播使用主站生成的 HLS、MPEG-TS 分片，节点缓存和分发。默认输出 H.264 视频、AAC 至多双声道音频；主站对单声道源保留单声道。插件允许选择 HEVC、调整分辨率和码率，并提供原文件模式。选定字幕由主站烘焙，分享 HLS 强制重新编码。[使用说明](https://github.com/Tokiichika/jellyfin-vrc-relay/blob/a01f28194d6a2158170273f7e053d87f9de98f92/README.md)、[HLS 实现](https://github.com/Tokiichika/jellyfin-vrc-relay/blob/a01f28194d6a2158170273f7e053d87f9de98f92/hls.py#L163)

播放契约按标准协议、封装和编码定义。AVPro Video 可作为验证播放器，应用不绑定 VRChat 或 AVPro Video，也不为它们设置专用接口或特殊处理。
