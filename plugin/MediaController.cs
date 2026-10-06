using System.Security.Claims;
using System.ComponentModel.DataAnnotations;
using Jellyfin.Data.Enums;
using Jellyfin.Data;
using Jellyfin.Database.Implementations.Enums;
using Jellyfin.Database.Implementations.Entities;
using MediaBrowser.Common.Api;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.MediaInfo;
using MediaBrowser.Model.Library;
using MediaBrowser.Model.Activity;
using MediaBrowser.Model.Globalization;
using System.Globalization;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Net.Http.Headers;

namespace Jellyfin.Plugin.Edge;

[ApiController]
[Route("JellyfinEdge")]
public sealed class MediaController(ILibraryManager library, IUserManager users, IMediaSourceManager sources, IAuthorizationService authorization, IActivityManager activity, ILocalizationManager localization) : ControllerBase
{
    private Plugin Plugin => Plugin.Instance;
    private Configuration.NodeConfiguration? Node(bool bearer = false)
    {
        var value = bearer ? Request.Headers.Authorization.ToString() : Request.Headers["X-Jellyfin-Edge-Node"].ToString();
        if (bearer)
        {
            if (!value.StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase)) return null;
            value = value[7..];
        }
        lock (Plugin.Gate)
        {
            var id = Plugin.Credentials.Authenticate(value);
            return id.HasValue ? Plugin.Snapshot(id.Value) : null;
        }
    }
    private User? CurrentUser() => Guid.TryParse(User.FindFirstValue("Jellyfin-UserId"), out var id) && id != Guid.Empty ? users.GetUserById(id) : null;
    private (BaseItem Item, MediaSourceInfo Source)? Resolve(Guid id, string? sourceId, User user)
    {
        var item = library.GetItemById<BaseItem>(id, user);
        if (item is null || !item.IsFileProtocol) return null;
        var available = sources.GetStaticMediaSources(item, false, user);
        var source = string.IsNullOrEmpty(sourceId) ? available.FirstOrDefault() : available.SingleOrDefault(s => s.Id == sourceId);
        if (source is null || source.Protocol != MediaProtocol.File || source.IsInfiniteStream || source.RequiresOpening || source.RequiresLooping) return null;
        // Version membership comes from this item, but the selected alternate must also be visible.
        if (Guid.TryParse(source.Id, out var sid) && library.GetItemById<BaseItem>(sid, user) is null) return null;
        return (item, source);
    }
    private async Task<bool> Allowed(BaseItem item, User user, string operation)
    {
        if (user.HasPermission(PermissionKind.IsDisabled)) return false;
        return operation == "play" || operation == "share" ? item.GetPlayAccess(user) == PlayAccess.Full
            : item.CanDownload(user) && (await authorization.AuthorizeAsync(User, null, Policies.Download).ConfigureAwait(false)).Succeeded;
    }
    [HttpGet("context")]
    [Authorize]
    public IActionResult Context()
    {
        Response.Headers.CacheControl = "private, no-store";
        var node = Node();
        if (Request.Headers.ContainsKey("X-Jellyfin-Edge-Node") && node is null) return Unauthorized();
        return Ok(new { NodeId = node?.NodeId ?? Plugin.Configuration.DefaultNodeId, Available = Plugin.Configuration.Nodes.Any(n => n.Enabled && n.PublicUrl.Length > 0), WebVersion = "12.2" });
    }
    [HttpGet("node/files/{itemId:guid}")]
    [Authorize]
    public async Task<IActionResult> FileMetadata(Guid itemId, [FromQuery] string? mediaSourceId, [FromQuery] string operation = "play")
    {
        Response.Headers.CacheControl = "private, no-store";
        var node = Node();
        if (node is null || !node.EnableCache) return Unauthorized();
        if (operation is not ("play" or "download")) return BadRequest();
        var user = CurrentUser();
        var resolved = user is null ? null : Resolve(itemId, operation == "download" ? itemId.ToString("N") : mediaSourceId, user);
        if (resolved is null) return NotFound();
        if (!await Allowed(resolved.Value.Item, user!, operation).ConfigureAwait(false)) return StatusCode(403);
        try { return Ok(FileIdentity.Read(resolved.Value.Source.Path, Plugin.Exports.Namespace)); }
        catch (IOException) { return Conflict(); }
    }
    [HttpGet("node/files/{itemId:guid}/bytes")]
    [HttpHead("node/files/{itemId:guid}/bytes")]
    [Authorize]
    public async Task<IActionResult> FileBytes(Guid itemId, [FromQuery] string cacheKey, [FromQuery] string? mediaSourceId, [FromQuery] string operation = "play")
    {
        var metadata = await FileMetadata(itemId, mediaSourceId, operation).ConfigureAwait(false);
        if (metadata is not OkObjectResult { Value: FileIdentity identity }) return metadata;
        return Original(identity, cacheKey);
    }
    [HttpPost("node/files/{itemId:guid}/audit")]
    [Authorize]
    [Authorize(Policy = Policies.Download)]
    public async Task<IActionResult> Audit(Guid itemId, [FromQuery] string cacheKey)
    {
        var metadata = await FileMetadata(itemId, null, "download").ConfigureAwait(false);
        if (metadata is not OkObjectResult { Value: FileIdentity identity }) return metadata;
        if (identity.CacheKey != cacheKey) return Conflict();
        var user = CurrentUser()!;
        var item = library.GetItemById<BaseItem>(itemId, user)!;
        try
        {
            await activity.CreateAsync(new ActivityLog(string.Format(CultureInfo.InvariantCulture, localization.GetServerLocalizedString("UserDownloadingItemWithValues"), user.Username, item.Name), "UserDownloadingContent", user.Id)
            {
                ItemId = item.Id.ToString("N"),
                ShortOverview = string.Format(CultureInfo.InvariantCulture, localization.GetServerLocalizedString("AppDeviceValues"), User.FindFirstValue("Jellyfin-Client"), User.FindFirstValue("Jellyfin-Device"))
            }).ConfigureAwait(false);
        }
        catch { /* Same best-effort audit behavior as Jellyfin's original download action. */ }
        return NoContent();
    }
    private IActionResult Original(FileIdentity identity, string key)
    {
        if (!string.Equals(identity.CacheKey, key, StringComparison.Ordinal)) return Conflict();
        var stream = new FileStream(identity.Path, FileMode.Open, FileAccess.Read, FileShare.Read, 65536, FileOptions.Asynchronous | FileOptions.RandomAccess);
        var current = FileIdentity.Read(identity.Path, Plugin.Exports.Namespace);
        if (current.CacheKey != identity.CacheKey || stream.Length != identity.Size) { stream.Dispose(); return Conflict(); }
        return File(stream, identity.ContentType, lastModified: new DateTimeOffset(new DateTime(identity.ModifiedTicks, DateTimeKind.Utc)), entityTag: new EntityTagHeaderValue('"' + key + '"'), enableRangeProcessing: true);
    }
    [HttpPost("exports")]
    [Authorize]
    [RequestSizeLimit(4096)]
    public async Task<IActionResult> Create([FromBody] CreateExportRequest request)
    {
        Response.Headers.CacheControl = "private, no-store";
        if (request.Operation is not ("share" or "download" or "stream")) return BadRequest();
        var user = CurrentUser();
        if (user is null) return StatusCode(403);
        var resolved = Resolve(request.ItemId, request.MediaSourceId, user);
        if (resolved is null || (request.Operation == "share" && resolved.Value.Item.MediaType != MediaType.Video)) return NotFound();
        if (!await Allowed(resolved.Value.Item, user, request.Operation).ConfigureAwait(false)) return StatusCode(403);
        Configuration.NodeConfiguration? node;
        lock (Plugin.Gate)
        {
            node = Node();
            if (Request.Headers.ContainsKey("X-Jellyfin-Edge-Node") && node is null) return Unauthorized();
            node ??= Plugin.Snapshot(Plugin.Configuration.DefaultNodeId);
            if (node is null || !Plugin.ValidPublicUrl(node.PublicUrl) || !Plugin.Credentials.HasCredential(node.NodeId)
                || Plugin.Credentials.Heartbeat(node.NodeId)?.SeenAt < DateTimeOffset.UtcNow.AddSeconds(-180)
                || Plugin.Credentials.Heartbeat(node.NodeId) is null) return StatusCode(503, new { Message = "目标代理节点不可用，请检查插件配置和节点心跳。" });
        }
        var (item, source) = resolved.Value;
        var config = Plugin.Configuration;
        sources.SetDefaultAudioAndSubtitleStreamIndices(item, source, user);
        var audio = request.AudioIndex ?? source.DefaultAudioStreamIndex;
        var subtitle = request.SubtitleIndex ?? (config.UseDefaultSubtitles ? source.DefaultSubtitleStreamIndex ?? -1 : -1);
        if ((audio.HasValue && !source.MediaStreams.Any(s => s.Type == MediaStreamType.Audio && s.Index == audio))
            || (subtitle != -1 && !source.MediaStreams.Any(s => s.Type == MediaStreamType.Subtitle && s.Index == subtitle))) return BadRequest();
        try
        {
            var selectedSubtitle = source.MediaStreams.SingleOrDefault(s => s.Type == MediaStreamType.Subtitle && s.Index == subtitle);
            var output = new OutputSelection { Mode = request.Operation == "share" ? config.ShareMode : "original", VideoCodec = config.VideoCodec, MaxHeight = config.MaxHeight,
                VideoBitRate = config.VideoBitRate, AudioBitRate = config.AudioBitRate, AudioIndex = audio, SubtitleIndex = subtitle };
            if (selectedSubtitle?.IsExternal == true && !string.IsNullOrEmpty(selectedSubtitle.Path) && System.IO.File.Exists(selectedSubtitle.Path))
                output.SubtitleVersion = FileIdentity.Read(selectedSubtitle.Path, Plugin.Exports.Namespace).CacheKey;
            if (output.Mode == "hls" && source.RunTimeTicks is not > 0) return Conflict();
            var created = Plugin.Exports.Create(new ExportRecord { NodeId = node.NodeId, UserId = user.Id, ItemId = item.Id, MediaSourceId = source.Id,
                Operation = request.Operation, Output = output, RuntimeTicks = source.RunTimeTicks ?? 0, File = FileIdentity.Read(source.Path, Plugin.Exports.Namespace) }, config.LinkLifetimeDays);
            var asset = output.Mode == "hls" ? "index.m3u8" : "file";
            return Ok(new { Id = created.Record.Id, Url = node.PublicUrl.TrimEnd('/') + "/edge/v1/exports/" + created.Record.Id + "/" + asset + "?media_token=" + created.Token, created.Record.ExpiresAt });
        }
        catch (IOException) { return Conflict(); }
        catch (InvalidOperationException) { return StatusCode(409); }
    }
    private ExportRecord? Grant(string id)
    {
        Response.Headers.CacheControl = "private, no-store";
        var node = Node(true);
        var token = Request.Headers["X-Jellyfin-Edge-Media"].ToString();
        if (node is null || token.Length != 64) return null;
        return Plugin.Exports.Find(id, node.NodeId, token);
    }
    [AllowAnonymous]
    [HttpGet("node/exports/{id}")]
    public IActionResult ExportMetadata(string id)
    {
        var record = Grant(id);
        return record is null ? NotFound() : Ok(record);
    }
    private bool CurrentVersion(ExportRecord record)
    {
        try
        {
            if (FileIdentity.Read(record.File.Path, Plugin.Exports.Namespace).CacheKey != record.File.CacheKey) return false;
            if (record.Output.SubtitleVersion is null) return true;
            var source = sources.GetStaticMediaSources(library.GetItemById<BaseItem>(record.ItemId), false).SingleOrDefault(s => s.Id == record.MediaSourceId);
            var path = source?.MediaStreams.SingleOrDefault(s => s.Type == MediaStreamType.Subtitle && s.Index == record.Output.SubtitleIndex)?.Path;
            return path is not null && FileIdentity.Read(path, Plugin.Exports.Namespace).CacheKey == record.Output.SubtitleVersion;
        }
        catch (IOException) { return false; }
    }
    [AllowAnonymous]
    [HttpGet("node/exports/{id}/bytes")]
    [HttpHead("node/exports/{id}/bytes")]
    public IActionResult ExportBytes(string id)
    {
        var record = Grant(id);
        if (record is null) return NotFound();
        if (record.Output.Mode != "original") return Conflict();
        return Original(record.File, record.File.CacheKey);
    }
    [AllowAnonymous]
    [HttpGet("node/exports/{id}/playlist")]
    public async Task<IActionResult> Playlist(string id)
    {
        var record = Grant(id);
        if (record is null) return NotFound();
        if (record.Output.Mode != "hls" || !CurrentVersion(record)) return Conflict();
        return await NativeHlsAdapter.Playlist(HttpContext, record).ConfigureAwait(false);
    }
    [AllowAnonymous]
    [HttpGet("node/exports/{id}/segments/{segment:int}")]
    public async Task<IActionResult> Segment(string id, int segment, [FromQuery] long runtimeTicks, [FromQuery] long actualSegmentLengthTicks)
    {
        var record = Grant(id);
        if (record is null) return NotFound();
        if (record.Output.Mode != "hls" || !CurrentVersion(record)) return Conflict();
        if (segment < 0 || runtimeTicks < 0 || runtimeTicks >= record.RuntimeTicks || actualSegmentLengthTicks <= 0 || actualSegmentLengthTicks > 600000000
            || runtimeTicks > record.RuntimeTicks - actualSegmentLengthTicks + 10000000) return BadRequest();
        return await NativeHlsAdapter.Segment(HttpContext, record, segment, runtimeTicks, actualSegmentLengthTicks).ConfigureAwait(false);
    }
    [HttpGet("admin/exports")]
    [Authorize(Policy = Policies.RequiresElevation)]
    public IActionResult List([FromQuery, Range(1, int.MaxValue)] int page = 1, [FromQuery, Range(1, 100)] int pageSize = 20) => Ok(Plugin.Exports.List(page, pageSize));
    [HttpPost("admin/exports/revoke")]
    [Authorize(Policy = Policies.RequiresElevation)]
    [RequestSizeLimit(512 * 1024)]
    public IActionResult RevokeMany([FromBody] RevokeExportsRequest request)
    {
        if (request.Ids.Any(id => !Guid.TryParseExact(id, "N", out _))) return BadRequest();
        return Ok(Plugin.Exports.RevokeMany(request.Ids.Select(id => Guid.ParseExact(id, "N").ToString("N")).ToArray()));
    }
    [HttpPost("admin/exports/{id}/revoke")]
    [Authorize(Policy = Policies.RequiresElevation)]
    public IActionResult Revoke(string id) => Plugin.Exports.Revoke(id) ? NoContent() : NotFound();
    [HttpPost("admin/nodes/{nodeId:guid}/clear-cache")]
    [Authorize(Policy = Policies.RequiresElevation)]
    public IActionResult ClearCache(Guid nodeId)
    {
        lock (Plugin.Gate)
        {
            var config = System.Text.Json.JsonSerializer.Deserialize<Configuration.PluginConfiguration>(System.Text.Json.JsonSerializer.Serialize(Plugin.Configuration))!;
            var node = config.Nodes.SingleOrDefault(n => n.NodeId == nodeId);
            if (node is null) return NotFound();
            node.CacheEpoch = checked(node.CacheEpoch + 1);
            Plugin.UpdateConfiguration(config);
            return Ok(new { node.CacheEpoch });
        }
    }
}
public sealed record CreateExportRequest(Guid ItemId, string Operation = "share", string? MediaSourceId = null, int? AudioIndex = null, int? SubtitleIndex = null);
public sealed class RevokeExportsRequest
{
    [Required, MinLength(1), MaxLength(10000)]
    public string[] Ids { get; set; } = [];
}
