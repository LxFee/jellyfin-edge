using System.Security.Claims;
using Jellyfin.Api.Controllers;
using MediaBrowser.Model.Dlna;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.WebUtilities;
using Microsoft.Extensions.DependencyInjection;

namespace Jellyfin.Plugin.Edge;

// Version-pinned adapter. Inputs are a stored grant, never an arbitrary URL/query.
internal static class NativeHlsAdapter
{
    private static async Task<IActionResult> Run(HttpContext context, ExportRecord record, string suffix, Func<DynamicHlsController, Task<ActionResult>> action)
    {
        var oldUser = context.User;
        var oldPath = context.Request.Path;
        var oldQuery = context.Request.QueryString;
        try
        {
            context.User = new ClaimsPrincipal(new ClaimsIdentity([new Claim("Jellyfin-UserId", record.UserId.ToString()), new Claim("Jellyfin-DeviceId", "edge-share-" + record.Id)], "EdgeMediaGrant"));
            context.Request.Path = "/Videos/" + record.ItemId.ToString("N") + "/" + suffix;
            context.Request.QueryString = new QueryString(QueryHelpers.AddQueryString("", new Dictionary<string, string?> {
                ["MediaSourceId"] = record.MediaSourceId, ["PlaySessionId"] = record.ArtifactKey[..32],
                ["DeviceId"] = "edge-share-" + record.Id, ["VideoCodec"] = record.Output.VideoCodec,
                ["AudioCodec"] = "aac", ["SegmentContainer"] = "ts", ["SegmentLength"] = "6",
                ["VideoBitRate"] = record.Output.VideoBitRate.ToString(System.Globalization.CultureInfo.InvariantCulture),
                ["AudioBitRate"] = record.Output.AudioBitRate.ToString(System.Globalization.CultureInfo.InvariantCulture),
                ["MaxHeight"] = record.Output.MaxHeight.ToString(System.Globalization.CultureInfo.InvariantCulture),
                ["SubtitleStreamIndex"] = record.Output.SubtitleIndex.ToString(System.Globalization.CultureInfo.InvariantCulture),
                ["SubtitleMethod"] = "Encode", ["EnableAutoStreamCopy"] = "false", ["AllowVideoStreamCopy"] = "false", ["AllowAudioStreamCopy"] = "false"
            }));
            var controller = ActivatorUtilities.CreateInstance<DynamicHlsController>(context.RequestServices);
            controller.ControllerContext = new ControllerContext { HttpContext = context };
            return await action(controller).ConfigureAwait(false);
        }
        finally { context.User = oldUser; context.Request.Path = oldPath; context.Request.QueryString = oldQuery; }
    }

    public static Task<IActionResult> Playlist(HttpContext context, ExportRecord record) => Run(context, record, "main.m3u8", controller => controller.GetVariantHlsVideoPlaylist(
            itemId: record.ItemId,
            @static: false,
            @params: null,
            tag: null,
            deviceProfileId: null,
            playSessionId: record.ArtifactKey[..32],
            segmentContainer: "ts",
            segmentLength: 6,
            minSegments: 1,
            mediaSourceId: record.MediaSourceId,
            deviceId: "edge-share-" + record.Id,
            audioCodec: "aac",
            enableAutoStreamCopy: false,
            allowVideoStreamCopy: false,
            allowAudioStreamCopy: false,
            audioSampleRate: null,
            maxAudioBitDepth: null,
            audioBitRate: record.Output.AudioBitRate,
            audioChannels: record.Output.AudioChannels,
            maxAudioChannels: record.Output.AudioChannels,
            profile: null,
            level: null,
            framerate: null,
            maxFramerate: null,
            copyTimestamps: null,
            startTimeTicks: null,
            width: null,
            height: null,
            maxWidth: null,
            maxHeight: record.Output.MaxHeight,
            videoBitRate: record.Output.VideoBitRate,
            subtitleStreamIndex: record.Output.SubtitleIndex,
            subtitleMethod: SubtitleDeliveryMethod.Encode,
            maxRefFrames: null,
            maxVideoBitDepth: null,
            requireAvc: null,
            deInterlace: null,
            requireNonAnamorphic: null,
            transcodingMaxAudioChannels: null,
            cpuCoreLimit: null,
            liveStreamId: null,
            enableMpegtsM2TsMode: null,
            videoCodec: record.Output.VideoCodec,
            subtitleCodec: null,
            transcodeReasons: null,
            audioStreamIndex: record.Output.AudioIndex,
            videoStreamIndex: null,
            context: EncodingContext.Streaming,
            streamOptions: new Dictionary<string, string>(),
            enableAudioVbrEncoding: false,
            alwaysBurnInSubtitleWhenTranscoding: true));

    public static Task<IActionResult> Segment(HttpContext context, ExportRecord record, int segment, long runtimeTicks, long actualSegmentLengthTicks) => Run(context, record, "hls1/main/" + segment + ".ts", controller => controller.GetHlsVideoSegment(
            itemId: record.ItemId,
            playlistId: "main",
            segmentId: segment,
            container: "ts",
            runtimeTicks: runtimeTicks,
            actualSegmentLengthTicks: actualSegmentLengthTicks,
            @static: false,
            @params: null,
            tag: null,
            deviceProfileId: null,
            playSessionId: record.ArtifactKey[..32],
            segmentContainer: "ts",
            segmentLength: 6,
            minSegments: 1,
            mediaSourceId: record.MediaSourceId,
            deviceId: "edge-share-" + record.Id,
            audioCodec: "aac",
            enableAutoStreamCopy: false,
            allowVideoStreamCopy: false,
            allowAudioStreamCopy: false,
            audioSampleRate: null,
            maxAudioBitDepth: null,
            audioBitRate: record.Output.AudioBitRate,
            audioChannels: record.Output.AudioChannels,
            maxAudioChannels: record.Output.AudioChannels,
            profile: null,
            level: null,
            framerate: null,
            maxFramerate: null,
            copyTimestamps: null,
            startTimeTicks: null,
            width: null,
            height: null,
            maxWidth: null,
            maxHeight: record.Output.MaxHeight,
            videoBitRate: record.Output.VideoBitRate,
            subtitleStreamIndex: record.Output.SubtitleIndex,
            subtitleMethod: SubtitleDeliveryMethod.Encode,
            maxRefFrames: null,
            maxVideoBitDepth: null,
            requireAvc: null,
            deInterlace: null,
            requireNonAnamorphic: null,
            transcodingMaxAudioChannels: null,
            cpuCoreLimit: null,
            liveStreamId: null,
            enableMpegtsM2TsMode: null,
            videoCodec: record.Output.VideoCodec,
            subtitleCodec: null,
            transcodeReasons: null,
            audioStreamIndex: record.Output.AudioIndex,
            videoStreamIndex: null,
            context: EncodingContext.Streaming,
            streamOptions: new Dictionary<string, string>(),
            enableAudioVbrEncoding: false,
            alwaysBurnInSubtitleWhenTranscoding: true));

}
