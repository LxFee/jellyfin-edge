using MediaBrowser.Model.Plugins;

namespace Jellyfin.Plugin.Edge.Configuration;

// Public Jellyfin configuration: never add credentials or pairing codes here.
public sealed class PluginConfiguration : BasePluginConfiguration
{
    public List<NodeConfiguration> Nodes { get; set; } = [];
    public Guid DefaultNodeId { get; set; }
    // Zero explicitly means no expiry.
    public int LinkLifetimeDays { get; set; } = 7;
    public string ShareMode { get; set; } = "hls";
    public string VideoCodec { get; set; } = "h264";
    public int MaxHeight { get; set; } = 1080;
    public int VideoBitRate { get; set; } = 8000000;
    public int AudioBitRate { get; set; } = 192000;
    public bool UseDefaultSubtitles { get; set; } = true;
}

public sealed class NodeConfiguration
{
    public Guid NodeId { get; set; }
    public string Name { get; set; } = "";
    public bool Enabled { get; set; } // Explicit administrator opt-in.
    public string PublicUrl { get; set; } = "";
    public bool EnableCache { get; set; } = true;
    public long CacheMaxBytes { get; set; } = 50L * 1024 * 1024 * 1024;
    public int CacheBlockBytes { get; set; } = 2 * 1024 * 1024;
    public long CacheEpoch { get; set; }
    public bool AllowOriginFallback { get; set; } = true;
    public bool TrustProxyHeaders { get; set; } // Default false; only behind a sanitizing Traefik ingress.
    public bool EnableLocalDirectPlay { get; set; } // Legacy mounts, disabled in new configurations.
    public List<PathMapping> Mappings { get; set; } = [];
}

public sealed class PathMapping
{
    public string OriginRoot { get; set; } = "";
    public string EdgeRoot { get; set; } = "";
}
