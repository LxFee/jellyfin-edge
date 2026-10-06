using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Jellyfin.Plugin.Edge;

// Independent from BasePlugin.Configuration and therefore from the public configuration API.
public sealed class PrivateStateStore
{
    private readonly object _gate = new();
    private readonly string _path;
    private readonly Func<DateTimeOffset> _clock;
    private State _state;

    public PrivateStateStore(string path, Func<DateTimeOffset>? clock = null)
    {
        _path = path;
        _clock = clock ?? (() => DateTimeOffset.UtcNow);
        // Corrupt state must fail closed, not silently recreate credentials.
        _state = File.Exists(path)
            ? JsonSerializer.Deserialize<State>(File.ReadAllText(path)) ?? throw new InvalidDataException("Invalid edge private state")
            : new State();
    }

    public EnrollmentToken RotateEnrollment()
    {
        lock (_gate)
        {
            var next = Clone();
            var token = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
            next.EnrollmentHash = Hash(token);
            next.EnrollmentCreated = _clock();
            Commit(next);
            return new EnrollmentToken(token, next.EnrollmentCreated, next.EnrollmentHash[..12]);
        }
    }

    public object EnrollmentStatus()
    {
        lock (_gate) return new { CreatedAt = _state.EnrollmentCreated, Fingerprint = _state.EnrollmentHash.Length == 64 ? _state.EnrollmentHash[..12] : "", HeartbeatTTLSeconds = 180 };
    }

    // Shared enrollment token is not sufficient to recover another runner's credential.
    // Retry requires the same high-entropy, persisted nonce; identity is permanently reserved.
    public PairResult? Enroll(string token, Guid instanceId, string nonce)
    {
        if (token.Length != 64 || nonce.Length != 64 || instanceId == Guid.Empty) return null;
        lock (_gate)
        {
            if (!EqualHash(_state.EnrollmentHash, Hash(token))) return null;
            var next = Clone();
            if (next.Instances.TryGetValue(instanceId, out var existing))
            {
                if (existing.Revoked || !EqualHash(existing.NonceHash, Hash(nonce)) || existing.EnrollmentHash != next.EnrollmentHash || !next.Tokens.ContainsKey(existing.NodeId)) return null;
                var retry = Derive(next, "enroll:" + instanceId + ":" + existing.NonceHash);
                // Once independently rotated, never hand out the original credential.
                return EqualHash(next.Tokens[existing.NodeId], Hash(retry)) ? new PairResult(existing.NodeId, retry) : null;
            }
            if (next.Instances.Count >= 1000) return null; // Includes permanent tombstones.
            var id = Guid.NewGuid();
            var record = new InstanceRecord { NodeId = id, NonceHash = Hash(nonce), EnrollmentHash = next.EnrollmentHash };
            next.Instances.Add(instanceId, record);
            var credential = Derive(next, "enroll:" + instanceId + ":" + record.NonceHash);
            next.Tokens[id] = Hash(credential);
            Commit(next);
            return new PairResult(id, credential);
        }
    }

    public bool HasCredential(Guid id) { lock (_gate) return _state.Tokens.ContainsKey(id); }

    public void RequestRotation(Guid id)
    {
        lock (_gate)
        {
            if (!_state.Tokens.ContainsKey(id)) throw new InvalidOperationException("Node has no active credential");
            var next = Clone();
            // Repeated admin clicks must not invalidate an already delivered candidate.
            if (!next.Rotations.ContainsKey(id)) next.Rotations[id] = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
            Commit(next);
        }
    }

    public string? RotationCredential(Guid id)
    {
        lock (_gate) return _state.Rotations.TryGetValue(id, out var nonce) ? Derive(_state, "rotate:" + id + ":" + nonce) : null;
    }

    public void Seen(Guid id, string version, string revision, string readability, string error)
    {
        lock (_gate)
        {
            var next = Clone();
            next.Heartbeats[id] = new HeartbeatRecord { SeenAt = _clock(), Version = version, Revision = revision, MediaReadability = readability, ErrorCode = error };
            Commit(next);
        }
    }

    public HeartbeatRecord? Heartbeat(Guid id) { lock (_gate) return _state.Heartbeats.GetValueOrDefault(id); }
    public bool PendingConfiguration(Guid id) { lock (_gate) return _state.Instances.Values.Any(x => x.NodeId == id && !x.Configured); }
    public void Configured(Guid id)
    {
        lock (_gate)
        {
            var next = Clone();
            foreach (var x in next.Instances.Values.Where(x => x.NodeId == id)) x.Configured = true;
            Commit(next);
        }
    }

    private static string Derive(State state, string purpose) => Convert.ToHexString(HMACSHA256.HashData(Convert.FromHexString(state.DerivationKey), Encoding.UTF8.GetBytes(purpose)));

    public PairCode Issue(Guid nodeId)
    {
        lock (_gate)
        {
            var next = Clone();
            next.Codes.RemoveAll(c => c.NodeId == nodeId || c.ExpiresAt <= _clock());
            var code = Convert.ToHexString(RandomNumberGenerator.GetBytes(12));
            var expires = _clock().AddMinutes(5);
            next.Codes.Add(new CodeRecord { NodeId = nodeId, Hash = Hash(code), ExpiresAt = expires });
            Commit(next);
            return new PairCode(code, expires);
        }
    }

    public PairResult? Pair(string code)
    {
        if (code.Length != 24) return null;
        lock (_gate)
        {
            var next = Clone();
            var record = next.Codes.SingleOrDefault(c => EqualHash(c.Hash, Hash(code)) && c.ExpiresAt > _clock());
            if (record is null) return null;
            next.Codes.Remove(record);
            // A new pairing rotates the node's previous identity.
            var token = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
            next.Tokens[record.NodeId] = Hash(token);
            next.Rotations.Remove(record.NodeId);
            Commit(next); // Token is returned ONLY after durable consumption of the code.
            return new PairResult(record.NodeId, token);
        }
    }

    public Guid? Authenticate(string token)
    {
        if (token.Length != 64) return null;
        lock (_gate)
        {
            var hash = Hash(token);
            foreach (var entry in _state.Tokens)
                if (EqualHash(entry.Value, hash)) return entry.Key;
            foreach (var entry in _state.Rotations)
            {
                if (!EqualHash(Hash(Derive(_state, "rotate:" + entry.Key + ":" + entry.Value)), hash)) continue;
                // The runner proves durable receipt by using the new credential. Only now
                // retire the old token. Lost responses are retried with the persisted new token.
                var next = Clone();
                next.Tokens[entry.Key] = hash;
                next.Rotations.Remove(entry.Key);
                Commit(next);
                return entry.Key;
            }
            return null;
        }
    }

    public void Revoke(Guid nodeId)
    {
        lock (_gate)
        {
            var next = Clone();
            next.Tokens.Remove(nodeId);
            next.Rotations.Remove(nodeId);
            foreach (var record in next.Instances.Values.Where(x => x.NodeId == nodeId)) record.Revoked = true;
            next.Codes.RemoveAll(c => c.NodeId == nodeId);
            Commit(next);
        }
    }

    private static string Hash(string value) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value)));
    private static bool EqualHash(string a, string b) => CryptographicOperations.FixedTimeEquals(Encoding.ASCII.GetBytes(a), Encoding.ASCII.GetBytes(b));
    private State Clone() => JsonSerializer.Deserialize<State>(JsonSerializer.Serialize(_state))!;
    private void Commit(State next)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
        if (!OperatingSystem.IsWindows()) File.SetUnixFileMode(Path.GetDirectoryName(_path)!, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        var temporary = _path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                if (!OperatingSystem.IsWindows()) File.SetUnixFileMode(temporary, UnixFileMode.UserRead | UnixFileMode.UserWrite);
                JsonSerializer.Serialize(stream, next);
                stream.Flush(flushToDisk: true);
            }
            File.Move(temporary, _path, overwrite: true);
            _state = next;
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }

    public sealed class State
    {
        public string EnrollmentHash { get; set; } = "";
        public DateTimeOffset? EnrollmentCreated { get; set; }
        public string DerivationKey { get; set; } = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
        public Dictionary<Guid, InstanceRecord> Instances { get; set; } = [];
        public Dictionary<Guid, string> Rotations { get; set; } = [];
        public Dictionary<Guid, HeartbeatRecord> Heartbeats { get; set; } = [];
        public Dictionary<Guid, string> Tokens { get; set; } = [];
        public List<CodeRecord> Codes { get; set; } = [];
    }
    public sealed class InstanceRecord
    {
        public Guid NodeId { get; set; }
        public string NonceHash { get; set; } = "";
        public string EnrollmentHash { get; set; } = "";
        public bool Revoked { get; set; }
        public bool Configured { get; set; }
    }
    public sealed class HeartbeatRecord
    {
        public DateTimeOffset SeenAt { get; set; }
        public string Version { get; set; } = "";
        public string Revision { get; set; } = "";
        public string MediaReadability { get; set; } = "unknown";
        public string ErrorCode { get; set; } = "";
    }
    public sealed class CodeRecord
    {
        public Guid NodeId { get; set; }
        public string Hash { get; set; } = "";
        public DateTimeOffset ExpiresAt { get; set; }
    }
}

public sealed record EnrollmentToken(string Token, DateTimeOffset? CreatedAt, string Fingerprint);
public sealed record PairCode(string Code, DateTimeOffset ExpiresAt);
public sealed record PairResult(Guid NodeId, string Token);
