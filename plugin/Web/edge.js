/* Jellyfin Web 12.2 adapter. Same-origin plugin API; exports never contain account tokens. */
(function () {
    'use strict';
    if (window.JellyfinEdge) return;
    let current = null;
    function api() {
        const client = window.ApiClient;
        if (!client) throw new Error('Jellyfin API 尚未准备好');
        return client;
    }
    function value(doc, key) { return doc[key] === undefined ? doc[key[0].toLowerCase() + key.slice(1)] : doc[key]; }
    function notify(message) {
        let box = document.getElementById('jellyfin-edge-message');
        if (!box) {
            box = document.createElement('div'); box.id = 'jellyfin-edge-message'; box.setAttribute('role', 'status');
            Object.assign(box.style, { position: 'fixed', bottom: '2em', left: '1em', right: '1em', zIndex: '100000', background: '#202020', color: 'white', padding: '1em', borderRadius: '0.4em' });
            document.body.appendChild(box);
        }
        box.textContent = message; box.hidden = false;
        clearTimeout(box.edgeTimer); box.edgeTimer = setTimeout(() => { box.hidden = true; }, 6000);
    }
    function selections(context) {
        const body = { ItemId: context.itemId };
        if (context.detail) {
            const page = context.detail;
            const source = page.querySelector('.selectSource');
            if (source && source.value) body.MediaSourceId = source.value;
            for (const [selector, key] of [['.selectAudio', 'AudioIndex'], ['.selectSubtitles', 'SubtitleIndex']]) {
                const field = page.querySelector(selector);
                if (field && field.value !== '' && /^-?\d+$/.test(field.value)) body[key] = Number(field.value);
            }
        }
        return body;
    }
    async function create(context, operation) {
        const client = api();
        // Read selections at activation time, not when the menu was opened.
        const body = Object.assign(selections(context), { Operation: operation });
        const result = await client.ajax({ type: 'POST', url: client.getUrl('JellyfinEdge/exports'), data: JSON.stringify(body), contentType: 'application/json', dataType: 'json' });
        const url = value(result, 'Url');
        if (typeof url !== 'string' || !url.includes('media_token=')) throw new Error('代理链接生成失败');
        if (operation === 'download') {
            const anchor = document.createElement('a'); anchor.href = url; anchor.rel = 'noreferrer'; anchor.download = ''; document.body.appendChild(anchor); anchor.click(); anchor.remove();
            notify('代理下载已开始');
        } else {
            if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(url);
            else {
                const input = document.createElement('textarea'); input.value = url; document.body.appendChild(input); input.select();
                const copied = document.execCommand('copy'); input.remove(); if (!copied) throw new Error('无法访问剪贴板，请使用 HTTPS');
            }
            notify(operation === 'share' ? '分享点播链接已复制' : '原片串流链接已复制');
        }
        return result;
    }
    function capture(event) {
        if (!(event.target instanceof Element) || event.target.closest('.actionSheet')) return;
        const detailButton = event.target.closest('.btnMoreCommands, .btnDownload');
        if (detailButton) {
            const page = detailButton.closest('[data-role="page"]') || document;
            const params = new URLSearchParams(location.hash.split('?')[1] || location.search);
            const itemId = params.get('id');
            if (itemId) current = { itemId, detail: page, seen: Date.now() };
            return;
        }
        const card = event.target.closest('.card[data-id], .listItem[data-id], [data-itemid]');
        if (card) current = { itemId: card.dataset.id || card.dataset.itemid, seen: Date.now() };
    }
    document.addEventListener('pointerdown', capture, true);
    document.addEventListener('contextmenu', capture, true);
    document.addEventListener('click', capture, true);
    function extend(sheet) {
        if (!current || Date.now() - current.seen > 10000 || sheet.dataset.edgeHandled) return;
        // A media menu must have native playback commands. Do not alter unrelated action sheets.
        if (!sheet.querySelector('[data-id="resume"], [data-id="play"], [data-id="copy-stream"]')) return;
        sheet.dataset.edgeHandled = 'true';
        const context = { ...current };
        sheet.addEventListener('click', event => {
            const button = event.target.closest('[data-edge-operation], [data-id="download"], [data-id="copy-stream"]');
            if (!button) return;
            const operation = button.dataset.edgeOperation || (button.dataset.id === 'download' ? 'download' : 'stream');
            event.preventDefault(); event.stopImmediatePropagation();
            create(context, operation).catch(() => notify('生成失败：请检查播放/下载权限、默认节点和节点状态。'));
        }, true);
        let client;
        try { client = api(); } catch (_) { return; }
        client.getItem(client.getCurrentUserId(), context.itemId).then(item => {
            if (item.MediaType !== 'Video' || !sheet.isConnected) return;
            const first = sheet.querySelector('.actionSheetMenuItem');
            if (!first) return;
            const share = document.createElement('button'); share.type = 'button'; share.className = first.className;
            share.dataset.edgeOperation = 'share';
            const icon = document.createElement('span');
            icon.className = 'actionsheetMenuItemIcon listItemIcon listItemIcon-transparent material-icons link';
            icon.setAttribute('aria-hidden', 'true');
            const body = document.createElement('div'); body.className = 'listItemBody actionsheetListItemBody';
            const text = document.createElement('div'); text.className = 'listItemBodyText actionSheetItemText';
            text.textContent = '复制分享点播链接'; body.appendChild(text); share.append(icon, body);
            first.parentNode.insertBefore(share, first);
            // Use capture handlers so native code cannot export its account-bearing URL.
            for (const button of sheet.querySelectorAll('[data-id="download"], [data-id="copy-stream"]')) {
                button.dataset.edgeOperation = button.dataset.id === 'download' ? 'download' : 'stream';
            }
        }).catch(() => {});
    }
    new MutationObserver(() => document.querySelectorAll('.actionSheet').forEach(extend)).observe(document.documentElement, { childList: true, subtree: true });
    // Detail page download button bypasses the native media menu.
    document.addEventListener('click', event => {
        const button = event.target instanceof Element && event.target.closest('.btnDownload');
        if (!button || !current || !current.detail) return;
        event.preventDefault(); event.stopImmediatePropagation();
        create({ ...current }, 'download').catch(() => notify('生成下载链接失败，请检查下载权限和代理节点。'));
    }, true);
    window.JellyfinEdge = Object.freeze({ create, selections });
})();
