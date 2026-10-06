#!/bin/sh
set -eu
plugin_dir="${JELLYFIN_DATA_DIR:-/config}/plugins/Jellyfin.Edge_2.0.0.0"
mkdir -p "$plugin_dir"
cp /opt/jellyfin-edge/plugin/Jellyfin.Plugin.Edge.dll "$plugin_dir/"
# Preserve enable/disable state managed by Jellyfin on subsequent restarts.
if [ ! -f "$plugin_dir/manifest.json" ]; then
    cp /opt/jellyfin-edge/plugin/manifest.json "$plugin_dir/"
fi
if [ -n "${JELLYFIN_EXTERNAL_SCRIPT_URL:-}" ]; then
    # Do not allow a caller's webdir to override the prepared copy. Other
    # arguments are preserved exactly; default (no injection) accepts all args.
    for arg in "$@"; do
        case "$arg" in
            --webdir|--webdir=*|-w|-w?*)
                echo 'jellyfin-edge: external script conflicts with webdir argument; use JELLYFIN_WEB_DIR for the source' >&2
                exit 1
                ;;
        esac
    done
    edge_web="${JELLYFIN_CACHE_DIR:-/cache}/edge-web"
    /usr/bin/python3 /usr/local/lib/jellyfin-edge/prepare-external-web.py \
        "${JELLYFIN_WEB_DIR:-/jellyfin/jellyfin-web}" "$edge_web"
    exec /jellyfin/jellyfin "$@" --webdir "$edge_web"
fi
exec /jellyfin/jellyfin "$@"
