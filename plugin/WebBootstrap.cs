using System.Text;
using MediaBrowser.Common.Net;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Configuration;
using MediaBrowser.Controller.Plugins;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;

namespace Jellyfin.Plugin.Edge;

public sealed class ServiceRegistrator : IPluginServiceRegistrator
{
    public void RegisterServices(IServiceCollection services, IServerApplicationHost applicationHost) => services.AddTransient<IStartupFilter, WebBootstrap>();
}

public sealed class WebBootstrap(IServerConfigurationManager config) : IStartupFilter
{
    public Action<IApplicationBuilder> Configure(Action<IApplicationBuilder> next) => app =>
    {
        app.Use(async (context, continuation) =>
        {
            var prefix = config.GetNetworkConfiguration().BaseUrl.TrimEnd('/');
            var path = context.Request.Path.Value;
            var scriptPath = prefix + "/JellyfinEdge/edge.js";
            if (context.Request.Method is not ("GET" or "HEAD")) { await continuation(context).ConfigureAwait(false); return; }
            byte[]? body = null;
            if (path == scriptPath)
            {
                using var stream = typeof(WebBootstrap).Assembly.GetManifestResourceStream("Jellyfin.Plugin.Edge.Web.edge.js")!;
                using var buffer = new MemoryStream();
                await stream.CopyToAsync(buffer, context.RequestAborted).ConfigureAwait(false);
                body = buffer.ToArray();
                context.Response.ContentType = "text/javascript; charset=utf-8";
            }
            else if (path == prefix + "/web/" || path == prefix + "/web/index.html")
            {
                var index = Path.Combine(config.ApplicationPaths.WebPath, "index.html");
                if (System.IO.File.Exists(index) && new FileInfo(index).Length <= 1024 * 1024)
                {
                    var html = await System.IO.File.ReadAllTextAsync(index, context.RequestAborted).ConfigureAwait(false);
                    var position = html.LastIndexOf("</body>", StringComparison.OrdinalIgnoreCase);
                    if (position >= 0)
                    {
                        html = html.Insert(position, "<script defer src=\"" + System.Net.WebUtility.HtmlEncode(scriptPath) + "\"></script>");
                        body = Encoding.UTF8.GetBytes(html);
                        context.Response.ContentType = "text/html; charset=utf-8";
                    }
                }
            }
            if (body is null) { await continuation(context).ConfigureAwait(false); return; }
            context.Response.Headers.CacheControl = "no-store";
            context.Response.ContentLength = body.Length;
            if (context.Request.Method == "GET") await context.Response.Body.WriteAsync(body, context.RequestAborted).ConfigureAwait(false);
        });
        next(app);
    };
}
