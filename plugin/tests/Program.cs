using System.Reflection;
using System.Text.Json;
using Jellyfin.Plugin.Edge;
using Jellyfin.Plugin.Edge.Configuration;
using MediaBrowser.Common.Api;
using Microsoft.AspNetCore.Authorization;

static void Check(bool test, string name) { if (!test) throw new Exception(name); Console.WriteLine("PASS " + name); }
var directory = Path.Combine(Path.GetTempPath(), "edge-tests-" + Guid.NewGuid());
Directory.CreateDirectory(directory);
try
{
    var path = Path.Combine(directory, "state.json");
    var now = DateTimeOffset.UtcNow;
    var state = new PrivateStateStore(path, () => now);
    var id = Guid.NewGuid();
    Check(state.Pair(new string('A', 24)) is null, "pairing off by default");
    var code = state.Issue(id);
    var results = new System.Collections.Concurrent.ConcurrentBag<PairResult>();
    Parallel.For(0, 20, _ => { var r = state.Pair(code.Code); if (r != null) results.Add(r); });
    Check(results.Count == 1, "concurrent code single use");
    var token = results.Single().Token;
    Check(state.Authenticate(token) == id && state.Authenticate(new string('0', 64)) is null, "trusted bearer identity");
    Check(!File.ReadAllText(path).Contains(token) && !File.ReadAllText(path).Contains(code.Code), "only hashes on disk");
    state = new PrivateStateStore(path, () => now);
    Check(state.Authenticate(token) == id, "identity persists across restart");
    var expired = state.Issue(id); now = now.AddMinutes(6);
    Check(state.Pair(expired.Code) is null, "short expiry");
    var replacement = state.Pair(state.Issue(id).Code)!;
    Check(state.Authenticate(token) is null && state.Authenticate(replacement.Token) == id, "re-pair rotates credential");
    var pending = state.Issue(id); state.Revoke(id);
    Check(state.Authenticate(replacement.Token) is null && state.Pair(pending.Code) is null, "revocation invalidates token and code");
    var json = JsonSerializer.Serialize(new NodeConfigResponse(id, true, true, true, [new MappingResponse("/media", "/edge")]), new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase });
    Check(json.Contains("\"NodeId\"") && json.Contains("\"PathMappings\"") && json.Contains("\"OriginRoot\""), "protocol casing fixed");
    Check(typeof(EdgeController).GetMethod("DownloadAuthorization")!.GetCustomAttributes<AuthorizeAttribute>().Any(a => a.Policy == Policies.Download), "download requires user download policy not elevation");
    Check(json.Contains("\"EnableLocalDirectPlay\":true") && !json.Contains("EnableLocalDownload"), "single local-read policy for playback and download");
    Check(!new NodeConfiguration().TrustProxyHeaders, "TrustProxyHeaders defaults off");
    Check(json.Contains("\"TrustProxyHeaders\":true"), "TrustProxyHeaders protocol casing and on value");
    var off = JsonSerializer.Serialize(new NodeConfigResponse(id, true, true, false, []));
    Check(off.Contains("\"TrustProxyHeaders\":false"), "TrustProxyHeaders off protocol value");
    var roundtrip = JsonSerializer.Deserialize<NodeConfiguration>(JsonSerializer.Serialize(new NodeConfiguration { TrustProxyHeaders = true }))!;
    Check(roundtrip.TrustProxyHeaders, "TrustProxyHeaders public config roundtrip");
    Check(!JsonSerializer.Serialize(new PluginConfiguration()).Contains("Token"), "public config has no credentials");
    foreach (var name in new[] { "IssueCode", "Revoke" })
        Check(typeof(EdgeController).GetMethod(name)!.GetCustomAttribute<AuthorizeAttribute>()?.Policy == Policies.RequiresElevation, name + " admin policy");
    var enrollment = state.RotateEnrollment();
    var instance = Guid.NewGuid();
    var nonce = new string('A', 64);
    var runner = state.Enroll(enrollment.Token, instance, nonce) ?? throw new Exception("enrollment rejected");
    Check( state.Enroll(enrollment.Token, instance, nonce) == runner, "enrollment durable retry same credential");
    Check(state.Enroll(enrollment.Token, instance, new string('B', 64)) is null, "impostor cannot retrieve node token");
    Check(!File.ReadAllText(path).Contains(enrollment.Token) && !File.ReadAllText(path).Contains(runner.Token), "enrollment and node plaintext absent from private state");
    var rotatedEnrollment = state.RotateEnrollment();
    Check(state.Enroll(enrollment.Token, Guid.NewGuid(), nonce) is null && state.Authenticate(runner.Token) == runner.NodeId, "global rotation blocks only new registration");
    Check(state.Enroll(rotatedEnrollment.Token, Guid.NewGuid(), nonce) != null, "new enrollment token reusable for multiple runners");
    state.RequestRotation(runner.NodeId);
    var candidate = state.RotationCredential(runner.NodeId)!;
    Check(state.Authenticate(runner.Token) == runner.NodeId, "rotation keeps active old credential before receipt");
    state = new PrivateStateStore(path, () => now);
    Check(state.RotationCredential(runner.NodeId) == candidate, "rotation delivery survives restart");
    Check(state.Authenticate(candidate) == runner.NodeId && state.Authenticate(runner.Token) is null, "new credential durable acknowledgement retires old");
    state.Revoke(runner.NodeId);
    Check(state.Authenticate(candidate) is null && state.Enroll(rotatedEnrollment.Token, instance, nonce) is null, "permanent revocation prevents rejoin replay");
    var media = Path.Combine(directory, "video.mp4");
    File.WriteAllText(media, "original bytes");
    var identity = FileIdentity.Read(media, "main-one");
    Check(identity.CacheKey != FileIdentity.Read(media, "main-two").CacheKey, "file identities include main namespace");
    File.AppendAllText(media, "changed");
    Check(identity.CacheKey != FileIdentity.Read(media, "main-one").CacheKey, "file size/modification changes version");
    var exports = new ExportStore(Path.Combine(directory, "exports.json"), () => now);
    var exported = exports.Create(new ExportRecord { NodeId = id, File = identity, Operation = "share", Output = new OutputSelection { SubtitleIndex = -1 } }, 7);
    Check(exported.Record.ExpiresAt == now.AddDays(7), "export default expiry is seven days");
    Check(exports.Find(exported.Record.Id, id, exported.Token) is not null, "bounded media grant validates");
    Check(exports.Find(exported.Record.Id, Guid.NewGuid(), exported.Token) is null && exports.Find(exported.Record.Id, id, new string('b', 64)) is null, "grant cannot cross nodes or use wrong bearer");
    Check(!File.ReadAllText(Path.Combine(directory, "exports.json")).Contains(exported.Token), "export bearer plaintext absent from disk");
    Check(state.Authenticate(exported.Token) is null, "media grant is not node authentication");
    exports = new ExportStore(Path.Combine(directory, "exports.json"), () => now);
    Check(exports.Find(exported.Record.Id, id, exported.Token)?.Output.SubtitleIndex == -1, "explicit subtitle off survives restart");
    var unlimited = exports.Create(new ExportRecord { NodeId = id, File = identity, Operation = "download", Output = new OutputSelection { Mode = "original" } }, 0);
    now = now.AddDays(8);
    Check(exports.Find(exported.Record.Id, id, exported.Token) is null && exports.Find(unlimited.Record.Id, id, unlimited.Token) is not null, "expiry enforced; zero permits no expiry");
    exports.Revoke(unlimited.Record.Id);
    Check(exports.Find(unlimited.Record.Id, id, unlimited.Token) is null && exports.ForNode(id).Length == 0, "revoked grants disappear from node synchronization");
    var batchPath = Path.Combine(directory, "batch-exports.json");
    var batch = new ExportStore(batchPath, () => now);
    Check(batch.List().TotalCount == 0 && batch.List().Page == 1 && batch.List().Items.Length == 0, "empty export list has one empty page");
    var generated = Enumerable.Range(0, 45).Select(_ =>
    {
        now = now.AddSeconds(1);
        return batch.Create(new ExportRecord { NodeId = id, File = identity }, 0);
    }).ToArray();
    var firstPage = batch.List();
    Check(firstPage.TotalCount == 45 && firstPage.PageSize == 20 && firstPage.Items.Length == 20 && firstPage.Items[0].Id == generated[^1].Record.Id, "exports paginated newest first with twenty by default");
    Check(!firstPage.Items.Select(r => r.Id).Intersect(batch.List(2).Items.Select(r => r.Id)).Any() && batch.List(3).Items.Length == 5, "export pages are disjoint and retain the final partial page");
    Check(batch.List(int.MaxValue).Page == 3 && batch.List(1, 100).Items.Length == 45, "page clamps after list shrink and supports bounded page sizes");
    foreach (var invalid in new[] { (0, 20), (1, 0), (1, 101) })
    {
        try { batch.List(invalid.Item1, invalid.Item2); throw new Exception("invalid pagination accepted"); }
        catch (ArgumentOutOfRangeException) { }
    }
    Check(!JsonSerializer.Serialize(firstPage).Contains("Token") && !JsonSerializer.Serialize(firstPage).Contains("Path"), "paginated summaries omit private media credentials and file paths");
    var missing = Guid.NewGuid().ToString("N");
    var selected = new[] { generated[0].Record.Id, generated[1].Record.Id, generated[0].Record.Id, missing };
    Check(batch.RevokeMany(selected) == new RevokeExportsResult(2, 0, 1), "batch revocation deduplicates and reports absent records");
    Check(batch.RevokeMany(selected) == new RevokeExportsResult(0, 2, 1), "batch retry is idempotent");
    batch = new ExportStore(batchPath, () => now);
    Check(batch.Find(generated[0].Record.Id, id, generated[0].Token) is null && batch.Find(generated[1].Record.Id, id, generated[1].Token) is null
        && batch.Find(generated[2].Record.Id, id, generated[2].Token) is not null && batch.ForNode(id).Length == 43, "batch revocation persists, invalidates selected grants, and preserves unselected grants");
    Check(batch.Revoke(generated[0].Record.Id) && !batch.Revoke(missing), "single revocation retains idempotent found and missing behavior");
    foreach (var name in new[] { "List", "Revoke", "RevokeMany" })
        Check(typeof(MediaController).GetMethod(name)!.GetCustomAttribute<AuthorizeAttribute>()?.Policy == Policies.RequiresElevation, name + " export management requires elevation");
    foreach (var ids in new string[][] { null!, [], new string[10001] })
    {
        var request = new RevokeExportsRequest { Ids = ids };
        Check(!System.ComponentModel.DataAnnotations.Validator.TryValidateObject(request, new System.ComponentModel.DataAnnotations.ValidationContext(request), [], true), "empty or excessive revoke selection rejected");
    }
    Check(typeof(MediaController).GetMethod("Create")!.GetCustomAttribute<AuthorizeAttribute>() is not null, "export creation requires a Jellyfin user");
    Check(typeof(MediaController).GetMethod("Audit")!.GetCustomAttributes<AuthorizeAttribute>().Any(a => a.Policy == Policies.Download), "cached downloads retain download authorization and audit");
    File.WriteAllText(path, "corrupt");
    try { _ = new PrivateStateStore(path); throw new Exception("corruption accepted"); }
    catch (JsonException) { Console.WriteLine("PASS corrupt state fails closed"); }
}
finally { Directory.Delete(directory, true); }
