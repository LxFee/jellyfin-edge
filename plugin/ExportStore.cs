using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Jellyfin.Plugin.Edge;

// Separate from public plugin configuration: neither user nor media bearer tokens are stored.
public sealed class ExportStore
{
    private readonly string _path;
    private readonly object _gate = new();
    private readonly Func<DateTimeOffset> _clock;
    private State _state;
    public string Namespace => _state.Namespace;
    public ExportStore(string path, Func<DateTimeOffset>? clock = null)
    {
        _path = path;
        _clock = clock ?? (() => DateTimeOffset.UtcNow);
        _state = File.Exists(path) ? JsonSerializer.Deserialize<State>(File.ReadAllText(path)) ?? throw new JsonException("Invalid export state") : new State();
        if (!File.Exists(path)) Save(_state);
    }
    public static string Hash(string value) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value))).ToLowerInvariant();
    public (ExportRecord Record, string Token) Create(ExportRecord record, int days)
    {
        lock (_gate)
        {
            var next = Clone();
            next.Records.RemoveAll(r => r.Revoked || r.ExpiresAt <= _clock());
            if (next.Records.Count >= 10000) throw new InvalidOperationException("Export limit reached; revoke unused links.");
            var token = Convert.ToHexString(RandomNumberGenerator.GetBytes(32)).ToLowerInvariant();
            record.Id = Guid.NewGuid().ToString("N");
            record.CreatedAt = _clock();
            record.ExpiresAt = days == 0 ? null : _clock().AddDays(days);
            record.TokenHash = Hash(token);
            record.ArtifactKey = Hash(JsonSerializer.Serialize(new { Schema = 1, Session = record.Id, record.File.CacheKey, record.Output }));
            next.Records.Add(record);
            Save(next);
            return (Copy(record), token);
        }
    }
    public ExportRecord? Find(string id, Guid node, string? token = null)
    {
        lock (_gate)
        {
            var r = _state.Records.SingleOrDefault(r => r.Id == id && r.NodeId == node && !r.Revoked && (r.ExpiresAt is null || r.ExpiresAt > _clock()));
            if (r is null || (token is not null && !CryptographicOperations.FixedTimeEquals(Encoding.ASCII.GetBytes(r.TokenHash), Encoding.ASCII.GetBytes(Hash(token))))) return null;
            return Copy(r);
        }
    }
    public ExportRecord[] ForNode(Guid node)
    {
        lock (_gate) return _state.Records.Where(r => r.NodeId == node && !r.Revoked && (r.ExpiresAt is null || r.ExpiresAt > _clock())).Select(Copy).ToArray();
    }
    public ExportPage List(int page = 1, int pageSize = 20)
    {
        if (page < 1) throw new ArgumentOutOfRangeException(nameof(page));
        if (pageSize is < 1 or > 100) throw new ArgumentOutOfRangeException(nameof(pageSize));
        lock (_gate)
        {
            var total = _state.Records.Count;
            page = Math.Min(page, Math.Max(1, (total + pageSize - 1) / pageSize));
            var items = _state.Records.OrderByDescending(r => r.CreatedAt).ThenBy(r => r.Id, StringComparer.Ordinal)
                .Skip((page - 1) * pageSize).Take(pageSize)
                .Select(r => new ExportSummary(r.Id, r.ItemId, r.NodeId, r.Operation, r.CreatedAt, r.ExpiresAt, r.Revoked)).ToArray();
            return new ExportPage(items, total, page, pageSize);
        }
    }
    public bool Revoke(string id) => RevokeMany([id]).MissingCount == 0;
    public RevokeExportsResult RevokeMany(IReadOnlyCollection<string> ids)
    {
        if (ids.Count is < 1 or > 10000) throw new ArgumentOutOfRangeException(nameof(ids));
        var selected = ids.ToHashSet(StringComparer.Ordinal);
        lock (_gate)
        {
            var found = _state.Records.Where(r => selected.Contains(r.Id)).ToArray();
            var changed = found.Count(r => !r.Revoked);
            if (changed > 0)
            {
                var next = Clone();
                foreach (var record in next.Records.Where(r => selected.Contains(r.Id))) record.Revoked = true;
                Save(next);
            }
            return new RevokeExportsResult(changed, found.Length - changed, selected.Count - found.Length);
        }
    }
    private static ExportRecord Copy(ExportRecord r) => JsonSerializer.Deserialize<ExportRecord>(JsonSerializer.Serialize(r))!;
    private State Clone() => JsonSerializer.Deserialize<State>(JsonSerializer.Serialize(_state))!;
    private void Save(State next)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
        if (!OperatingSystem.IsWindows()) File.SetUnixFileMode(Path.GetDirectoryName(_path)!, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        var tmp = _path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            using (var stream = new FileStream(tmp, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                if (!OperatingSystem.IsWindows()) File.SetUnixFileMode(tmp, UnixFileMode.UserRead | UnixFileMode.UserWrite);
                JsonSerializer.Serialize(stream, next);
                stream.Flush(true);
            }
            File.Move(tmp, _path, true);
            _state = next;
        }
        finally { if (File.Exists(tmp)) File.Delete(tmp); }
    }
    public sealed class State
    {
        public string Namespace { get; set; } = Guid.NewGuid().ToString("N");
        public List<ExportRecord> Records { get; set; } = [];
    }
}

public sealed record ExportSummary(string Id, Guid ItemId, Guid NodeId, string Operation, DateTimeOffset CreatedAt, DateTimeOffset? ExpiresAt, bool Revoked);
public sealed record ExportPage(ExportSummary[] Items, int TotalCount, int Page, int PageSize);
public sealed record RevokeExportsResult(int RevokedCount, int AlreadyRevokedCount, int MissingCount);

public sealed record FileIdentity(string Path, long Size, long ModifiedTicks, string CacheKey, string ContentType, string FileName)
{
    public static FileIdentity Read(string path, string sourceNamespace)
    {
        var file = new FileInfo(path);
        if (!file.Exists || (file.Attributes & FileAttributes.Directory) != 0) throw new FileNotFoundException();
        var version = new { Schema = 1, Namespace = sourceNamespace, Path = file.FullName, Size = file.Length, ModifiedTicks = file.LastWriteTimeUtc.Ticks };
        return new(file.FullName, file.Length, file.LastWriteTimeUtc.Ticks, ExportStore.Hash(JsonSerializer.Serialize(version)), MediaBrowser.Model.Net.MimeTypes.GetMimeType(path), file.Name);
    }
}
public sealed class OutputSelection
{
    public string Mode { get; set; } = "hls";
    public string VideoCodec { get; set; } = "h264";
    public int MaxHeight { get; set; } = 1080;
    public int VideoBitRate { get; set; } = 8000000;
    public int AudioBitRate { get; set; } = 192000;
    public int AudioChannels { get; set; } = 2;
    public int? AudioIndex { get; set; }
    public int SubtitleIndex { get; set; } = -1;
    public string? SubtitleVersion { get; set; }
}
public sealed class ExportRecord
{
    public string Id { get; set; } = "";
    public Guid NodeId { get; set; }
    public Guid UserId { get; set; }
    public Guid ItemId { get; set; }
    public string MediaSourceId { get; set; } = "";
    public string Operation { get; set; } = "share";
    public string TokenHash { get; set; } = "";
    public DateTimeOffset CreatedAt { get; set; }
    public DateTimeOffset? ExpiresAt { get; set; }
    public bool Revoked { get; set; }
    public FileIdentity File { get; set; } = null!;
    public OutputSelection Output { get; set; } = new();
    public string ArtifactKey { get; set; } = "";
    public long RuntimeTicks { get; set; }
}
