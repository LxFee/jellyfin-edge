using System.Text.Json;
using Jellyfin.Plugin.Edge.Configuration;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace Jellyfin.Plugin.Edge;

public sealed class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
{
    public static Plugin Instance { get; private set; } = null!;
    public object Gate { get; } = new();
    internal PrivateStateStore Credentials { get; }
    internal ExportStore Exports { get; }

    public Plugin(IApplicationPaths paths, IXmlSerializer serializer) : base(paths, serializer)
    {
        Credentials = new PrivateStateStore(Path.Combine(paths.PluginConfigurationsPath, "Jellyfin.Edge.private", "state.json"));
        Exports = new ExportStore(Path.Combine(paths.PluginConfigurationsPath, "Jellyfin.Edge.private", "exports.json"));
        Instance = this;
    }
    public override Guid Id => new("5ed3af2c-a62a-4fc1-b470-cefa5820ac92");
    public override string Name => "Jellyfin Edge";
    public override string Description => "Plugin-managed proxy nodes, original-file cache and scoped media sharing.";
    public override string ConfigurationFileName => "Jellyfin.Plugin.Edge.xml";
    public IEnumerable<PluginPageInfo> GetPages()
    {
        yield return new PluginPageInfo { Name = "JellyfinEdge", DisplayName = Name,
            EmbeddedResourcePath = "Jellyfin.Plugin.Edge.Configuration.config.html" };
    }

    public override void UpdateConfiguration(BasePluginConfiguration configuration)
    {
        lock (Gate)
        {
            var config = (PluginConfiguration)configuration;
            Validate(config);
            // Remove identities before deleting/disabling nodes. A stale config cannot revive a token.
            foreach (var old in Configuration.Nodes)
                if (!config.Nodes.Any(n => n.NodeId == old.NodeId)) Credentials.Revoke(old.NodeId);
            foreach (var node in config.Nodes.Where(n => n.Enabled)) Credentials.Configured(node.NodeId);
            base.UpdateConfiguration(config);
        }
    }

    public NodeConfiguration? Snapshot(Guid id) => Configuration.Nodes.Where(n => n.NodeId == id && n.Enabled)
        .Select(n => JsonSerializer.Deserialize<NodeConfiguration>(JsonSerializer.Serialize(n))!).SingleOrDefault();

    public NodeConfiguration? ControlSnapshot(Guid id) => Configuration.Nodes.SingleOrDefault(n => n.NodeId == id);
    public static string Revision(NodeConfiguration node) => Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(JsonSerializer.Serialize(node))));

    private static void Validate(PluginConfiguration config)
    {
        if (config.LinkLifetimeDays is < 0 or > 36500 || config.ShareMode is not ("hls" or "original")
            || config.VideoCodec is not ("h264" or "hevc") || config.MaxHeight is < 144 or > 4320
            || config.VideoBitRate is < 100000 or > 200000000 || config.AudioBitRate is < 32000 or > 512000)
            throw new ArgumentException("Invalid share defaults.");
        if (config.Nodes is null || config.Nodes.Count > 100 || config.Nodes.Select(n => n.NodeId).Distinct().Count() != config.Nodes.Count)
            throw new ArgumentException("Nodes must be unique and at most 100.");
        foreach (var n in config.Nodes)
        {
            if (n.CacheBlockBytes is < 65536 or > 16777216 || n.CacheMaxBytes < n.CacheBlockBytes || n.CacheEpoch < 0
                || (n.PublicUrl.Length > 0 && !ValidPublicUrl(n.PublicUrl))) throw new ArgumentException("Invalid cache or public URL.");
            if (n.NodeId == Guid.Empty || string.IsNullOrWhiteSpace(n.Name) || n.Name.Length > 100 || n.Mappings is null || n.Mappings.Count > 100)
                throw new ArgumentException("Invalid node.");
            foreach (var m in n.Mappings)
                if (!AbsoluteRoot(m.OriginRoot) || !AbsoluteRoot(m.EdgeRoot)) throw new ArgumentException("Mappings require absolute roots without traversal.");
        }
    }
    internal static bool ValidPublicUrl(string value) => Uri.TryCreate(value, UriKind.Absolute, out var uri)
        && uri.Scheme is "http" or "https" && string.IsNullOrEmpty(uri.UserInfo) && string.IsNullOrEmpty(uri.Query)
        && string.IsNullOrEmpty(uri.Fragment) && !uri.AbsolutePath.Split('/').Any(p => p is "." or "..");
    private static bool AbsoluteRoot(string root) => !string.IsNullOrWhiteSpace(root) && root.Length <= 4096
        && (root.StartsWith('/') || (root.Length > 2 && char.IsAsciiLetter(root[0]) && root[1] == ':' && (root[2] == '\\' || root[2] == '/')))
        && !root.Split(['/', '\\']).Any(p => p is ".." or ".") && !root.Any(char.IsControl);
}
