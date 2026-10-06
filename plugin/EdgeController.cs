using System.Text.Json.Serialization;
using MediaBrowser.Common.Api;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Net;
using System.Security.Claims;
using System.Globalization;
using MediaBrowser.Model.Activity;
using Jellyfin.Database.Implementations.Entities;
using MediaBrowser.Model.Globalization;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.Edge;

[ApiController]
[Route("JellyfinEdge")]
public sealed class EdgeController : ControllerBase
{
    private Plugin Plugin => Plugin.Instance;
    private readonly ILibraryManager _libraryManager;
    private readonly IUserManager _userManager;
    private readonly IActivityManager _activityManager;
    private readonly ILocalizationManager _localization;

    public EdgeController(ILibraryManager libraryManager, IUserManager userManager, IActivityManager activityManager, ILocalizationManager localization)
    {
        _libraryManager = libraryManager;
        _userManager = userManager;
        _activityManager = activityManager;
        _localization = localization;
    }

    // Internal only: user auth and node auth are independent, neither substitutes for the other.
    [HttpGet("node/download-authorization/{itemId:guid}")]
    [Authorize] // Combine default remote/parental/device checks with download permission.
    [Authorize(Policy = Policies.Download)]
    public IActionResult DownloadAuthorization(Guid itemId)
    {
        Response.Headers.CacheControl = "no-store";
        lock (Plugin.Gate)
        {
            var tokens = Request.Headers["X-Jellyfin-Edge-Node"];
            if (tokens.Count != 1 || string.IsNullOrEmpty(tokens[0])) return Unauthorized();
            var id = Plugin.Credentials.Authenticate(tokens[0]!);
            var node = id.HasValue ? Plugin.Snapshot(id.Value) : null;
            if (node is null) return Unauthorized();
            if (!node.EnableLocalDirectPlay) return StatusCode(403);
            // v12.1 InternalClaimTypes.UserId; reject API keys without a real user identity.
            if (!Guid.TryParse(User.FindFirstValue("Jellyfin-UserId"), out var userId) || userId == Guid.Empty) return StatusCode(403);
            var user = _userManager.GetUserById(userId);
            if (user is null) return StatusCode(403);
            // Same visibility/library/parental/hidden checks and CanDownload as official GetDownload.
            var item = _libraryManager.GetItemById<BaseItem>(itemId, user);
            if (item is null) return NotFound();
            if (!item.CanDownload(user)) return StatusCode(403);
            if (!item.IsFileProtocol) return Conflict();
            return Ok(new { ItemId = item.Id.ToString("N"), Path = item.Path,
                FileName = Path.GetFileName(item.Path)?.Replace("\"", string.Empty, StringComparison.Ordinal),
                ContentType = MimeTypes.GetMimeType(item.Path), NodeId = node.NodeId });
        }
    }

    // Called only after edge opened a local file. Metadata probes and origin fallback do not log.
    [HttpPost("node/download-authorization/{itemId:guid}")]
    [Authorize]
    [Authorize(Policy = Policies.Download)]
    public async Task<IActionResult> LogLocalDownload(Guid itemId, [FromBody] DownloadAuditRequest request)
    {
        var authorization = DownloadAuthorization(itemId);
        if (authorization is not OkObjectResult) return authorization;
        var userId = Guid.Parse(User.FindFirstValue("Jellyfin-UserId")!);
        var user = _userManager.GetUserById(userId);
        var item = _libraryManager.GetItemById<BaseItem>(itemId, user);
        if (user is null || item is null || !item.CanDownload(user)) return StatusCode(403);
        if (!string.Equals(request.Path, item.Path, StringComparison.Ordinal)) return Conflict();
        // LibraryController.LogDownloadAsync is private: mirror its official activity schema
        // and best-effort failure semantics without invoking PhysicalFile/origin transfer.
        try
        {
            await _activityManager.CreateAsync(new ActivityLog(
                string.Format(CultureInfo.InvariantCulture, _localization.GetServerLocalizedString("UserDownloadingItemWithValues"), user.Username, item.Name),
                "UserDownloadingContent", userId)
            {
                ShortOverview = string.Format(CultureInfo.InvariantCulture, _localization.GetServerLocalizedString("AppDeviceValues"), User.FindFirstValue("Jellyfin-Client"), User.FindFirstValue("Jellyfin-Device")),
                ItemId = item.Id.ToString("N", CultureInfo.InvariantCulture)
            }).ConfigureAwait(false);
        }
        catch { /* Same best-effort audit behavior as official GetDownload. */ }
        return NoContent();
    }

    [HttpPost("admin/nodes/{nodeId:guid}/pair-code")]
    [Authorize(Policy = Policies.RequiresElevation)]
    public ActionResult<PairCodeResponse> IssueCode(Guid nodeId)
    {
        lock (Plugin.Gate)
        {
            if (Plugin.Snapshot(nodeId) is null) return NotFound();
            var result = Plugin.Credentials.Issue(nodeId);
            Response.Headers.CacheControl = "no-store";
            return Ok(new PairCodeResponse(result.Code, result.ExpiresAt));
        }
    }

    [HttpPost("admin/nodes/{nodeId:guid}/revoke")]
    [Authorize(Policy = Policies.RequiresElevation)]
    public IActionResult Revoke(Guid nodeId)
    {
        lock (Plugin.Gate) Plugin.Credentials.Revoke(nodeId);
        return NoContent();
    }

    [AllowAnonymous]
    [HttpPost("pair")]
    [RequestSizeLimit(4096)]
    public ActionResult<PairResponse> Pair([FromBody] PairRequest request)
    {
        Response.Headers.CacheControl = "no-store";
        if (string.IsNullOrWhiteSpace(request.Code) || string.IsNullOrWhiteSpace(request.Name) || request.Name.Length > 100)
            return BadRequest();
        lock (Plugin.Gate)
        {
            var result = Plugin.Credentials.Pair(request.Code);
            if (result is null) return Unauthorized();
            if (Plugin.Snapshot(result.NodeId) is null)
            {
                Plugin.Credentials.Revoke(result.NodeId);
                return Unauthorized();
            }
            // Request Name is informational, never an identity or an admin-controlled node rename.
            return Ok(new PairResponse(result.NodeId, result.Token));
        }
    }

    [HttpPost("admin/enrollment-token/rotate")]
    [Authorize(Policy = Policies.RequiresElevation)]
    public IActionResult RotateEnrollment()
    {
        Response.Headers.CacheControl = "no-store";
        lock (Plugin.Gate) return Ok(Plugin.Credentials.RotateEnrollment());
    }

    [HttpGet("admin/status")]
    [Authorize(Policy = Policies.RequiresElevation)]
    public IActionResult Status()
    {
        Response.Headers.CacheControl = "no-store";
        lock (Plugin.Gate) return Ok(new {
            Enrollment = Plugin.Credentials.EnrollmentStatus(),
            Nodes = Plugin.Configuration.Nodes.Select(n => {
                var h = Plugin.Credentials.Heartbeat(n.NodeId);
                var valid = Plugin.Credentials.HasCredential(n.NodeId);
                var state = !valid ? "invalidnode" : Plugin.Credentials.PendingConfiguration(n.NodeId) ? "pendingconfig" : !n.Enabled ? "disabled" : h is null ? "noheartbeats" : h.SeenAt < DateTimeOffset.UtcNow.AddSeconds(-180) ? "offline" : h.Revision != Plugin.Revision(n) || h.ErrorCode.Length > 0 ? "configerror" : "online";
                return new { n.NodeId, State = state, RecentSeen = h?.SeenAt, Version = h?.Version, ConfigAppliedRevision = h?.Revision, Revision = Plugin.Revision(n), MediaReadability = h?.MediaReadability ?? "unknown", ErrorCode = h?.ErrorCode ?? "NO_HEARTBEAT" };
            }).ToArray()
        });
    }

    [HttpPost("admin/nodes/{nodeId:guid}/rotate-credential")]
    [Authorize(Policy = Policies.RequiresElevation)]
    public IActionResult RotateCredential(Guid nodeId)
    {
        Response.Headers.CacheControl = "no-store";
        lock (Plugin.Gate)
        {
            if (!Plugin.Credentials.HasCredential(nodeId)) return NotFound();
            Plugin.Credentials.RequestRotation(nodeId);
            return Ok(new { State = "awaiting-durable-receipt" });
        }
    }

    private static readonly object EnrollmentGate = new();
    private static DateTimeOffset EnrollmentWindow;
    private static int EnrollmentAttempts;

    [AllowAnonymous]
    [HttpPost("enroll")]
    [RequestSizeLimit(4096)]
    public IActionResult Enroll([FromBody] EnrollmentRequest request)
    {
        Response.Headers.CacheControl = "no-store";
        // Bounded global limiter, including rejected attempts; no attacker-controlled key map.
        lock (EnrollmentGate)
        {
            if (DateTimeOffset.UtcNow > EnrollmentWindow.AddMinutes(1)) { EnrollmentWindow = DateTimeOffset.UtcNow; EnrollmentAttempts = 0; }
            if (++EnrollmentAttempts > 60) return StatusCode(429);
        }
        if (request.InstanceId == Guid.Empty || !SafeText(request.Name, 100) || !SafeText(request.Version, 64)
            || request.Nonce is null || !System.Text.RegularExpressions.Regex.IsMatch(request.Nonce, "\\A[0-9A-Fa-f]{64}\\z")) return BadRequest();
        var authorization = Request.Headers.Authorization;
        if (authorization.Count != 1 || !authorization.ToString().StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase)) return Unauthorized();
        lock (Plugin.Gate)
        {
            if (Plugin.Configuration.Nodes.Count >= 100) return Conflict();
            var result = Plugin.Credentials.Enroll(authorization.ToString()[7..], request.InstanceId, request.Nonce);
            if (result is null) return Unauthorized();
            if (Plugin.ControlSnapshot(result.NodeId) is null)
            {
                var config = System.Text.Json.JsonSerializer.Deserialize<Configuration.PluginConfiguration>(System.Text.Json.JsonSerializer.Serialize(Plugin.Configuration))!;
                config.Nodes.Add(new Configuration.NodeConfiguration { NodeId = result.NodeId, Name = request.Name.Trim(), Enabled = false, EnableLocalDirectPlay = false, AllowOriginFallback = true });
                Plugin.UpdateConfiguration(config);
            }
            return Ok(new PairResponse(result.NodeId, result.Token));
        }
    }

    private static bool SafeText(string? value, int limit) => !string.IsNullOrWhiteSpace(value) && value.Length <= limit && !value.Any(c => char.IsControl(c) || c is '<' or '>' or '\\');

    [AllowAnonymous]
    [HttpPost("node/heartbeat")]
    [RequestSizeLimit(4096)]
    public IActionResult Heartbeat([FromBody] HeartbeatRequest request)
    {
        Response.Headers.CacheControl = "no-store";
        if (!SafeText(request.Version, 64) || request.Revision is null || request.Revision.Length > 64
            || request.MediaReadability is not ("unknown" or "readable" or "unreadable" or "no-paths")
            || request.ErrorCode is null || request.ErrorCode.Length > 64 || request.ErrorCode.Any(c => !char.IsAsciiLetterOrDigit(c) && c != '_')) return BadRequest();
        var authorization = Request.Headers.Authorization;
        if (authorization.Count != 1 || !authorization.ToString().StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase)) return Unauthorized();
        lock (Plugin.Gate)
        {
            var id = Plugin.Credentials.Authenticate(authorization.ToString()[7..]);
            if (!id.HasValue || Plugin.ControlSnapshot(id.Value) is null) return Unauthorized();
            Plugin.Credentials.Seen(id.Value, request.Version, request.Revision, request.MediaReadability, request.ErrorCode);
            return NoContent();
        }
    }

    // Deliberately bypass Jellyfin's user authentication: ONLY our hashed node bearer is accepted.
    [AllowAnonymous]
    [HttpGet("node/config")]
    public ActionResult<NodeConfigResponse> NodeConfig()
    {
        Response.Headers.CacheControl = "no-store";
        var authorization = Request.Headers.Authorization.ToString();
        if (!authorization.StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase)) return Unauthorized();
        lock (Plugin.Gate)
        {
            var id = Plugin.Credentials.Authenticate(authorization[7..]);
            var node = id.HasValue ? Plugin.ControlSnapshot(id.Value) : null;
            if (node is null) return Unauthorized();
            return Ok(new NodeConfigResponse(node.NodeId, node.Enabled && node.EnableLocalDirectPlay, node.Enabled && node.AllowOriginFallback, node.TrustProxyHeaders,
                (node.Enabled ? node.Mappings : []).Select(m => new MappingResponse(m.OriginRoot, m.EdgeRoot)).ToArray(), node.Enabled, Plugin.Revision(node), Plugin.Credentials.RotationCredential(node.NodeId),
                node.EnableCache, node.CacheMaxBytes, node.CacheBlockBytes, node.CacheEpoch, Plugin.Exports.Namespace, Plugin.Exports.ForNode(node.NodeId)));
        }
    }
}

public sealed record EnrollmentRequest(Guid InstanceId, string Name, string Version, string Nonce);
public sealed record HeartbeatRequest(string Version, string Revision, string MediaReadability, string ErrorCode);
public sealed record DownloadAuditRequest([property: JsonPropertyName("Path")] string Path);

// Explicit casing independent of the Jellyfin host's global JSON serializer conventions.
public sealed record PairRequest(
    [property: JsonPropertyName("Code")] string Code,
    [property: JsonPropertyName("Name")] string Name);
public sealed record PairResponse(
    [property: JsonPropertyName("NodeId")] Guid NodeId,
    [property: JsonPropertyName("Token")] string Token);
public sealed record PairCodeResponse(
    [property: JsonPropertyName("Code")] string Code,
    [property: JsonPropertyName("ExpiresAt")] DateTimeOffset ExpiresAt);
public sealed record NodeConfigResponse(
    [property: JsonPropertyName("NodeId")] Guid NodeId,
    [property: JsonPropertyName("EnableLocalDirectPlay")] bool EnableLocalDirectPlay,
    [property: JsonPropertyName("AllowOriginFallback")] bool AllowOriginFallback,
    [property: JsonPropertyName("TrustProxyHeaders")] bool TrustProxyHeaders,
    [property: JsonPropertyName("PathMappings")] MappingResponse[] PathMappings,
    [property: JsonPropertyName("Enabled")] bool Enabled = true,
    [property: JsonPropertyName("Revision")] string Revision = "",
    [property: JsonPropertyName("NewCredential")] string? NewCredential = null,
    [property: JsonPropertyName("EnableCache")] bool EnableCache = false,
    [property: JsonPropertyName("CacheMaxBytes")] long CacheMaxBytes = 53687091200,
    [property: JsonPropertyName("CacheBlockBytes")] int CacheBlockBytes = 2097152,
    [property: JsonPropertyName("CacheEpoch")] long CacheEpoch = 0,
    [property: JsonPropertyName("SourceNamespace")] string SourceNamespace = "",
    [property: JsonPropertyName("Exports")] ExportRecord[]? Exports = null);
public sealed record MappingResponse(
    [property: JsonPropertyName("OriginRoot")] string OriginRoot,
    [property: JsonPropertyName("EdgeRoot")] string EdgeRoot);
