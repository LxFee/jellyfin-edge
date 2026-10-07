'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../Web/edge.js'), 'utf8');

function page(proxy, rejectContext = false) {
    const handlers = {}, requests = [], downloads = [], copies = [];
    let nativeClicks = 0, changed;
    class Element {
        constructor(kind) { this.kind = kind; this.dataset = {}; this.style = {}; this.isConnected = true; }
        closest(selector) {
            if (selector === '.actionSheet') return this.sheet || null;
            if ((selector.includes('.btnDownload') && this.kind === 'download') || (selector.includes('.btnMoreCommands') && this.kind === 'more')) return this;
            if (selector === '[data-role="page"]') return detail;
            if (selector.includes('[data-edge-operation]') && this.sheet) return this;
            return null;
        }
        setAttribute() {}
        appendChild() {}
        append() {}
        remove() {}
        click() { if (this.kind === 'download' || this.sheet) dispatch(this); else downloads.push(this.href); }
    }
    const detail = { querySelector: () => null };
    const button = new Element('download');
    const sheet = new Element('sheet'), sheetHandlers = [], shares = [];
    const stream = new Element('stream'); stream.sheet = sheet; stream.dataset.id = 'copy-stream';
    const more = new Element('more');
    sheet.querySelector = selector => selector === '.actionSheetMenuItem' ? stream : {};
    sheet.querySelectorAll = () => [stream];
    sheet.addEventListener = (_, handler) => sheetHandlers.push(handler);
    stream.parentNode = { insertBefore(button) { button.sheet = sheet; shares.push(button); } };
    const document = {
        documentElement: {}, body: new Element('body'),
        addEventListener(type, handler) { (handlers[type] ||= []).push(handler); },
        querySelectorAll: () => [sheet], getElementById: () => null,
        createElement: tag => new Element(tag)
    };
    const ApiClient = {
        getUrl: path => '/' + path,
        getCurrentUserId: () => 'viewer', getItem: async () => ({ MediaType: 'Video' }),
        ajax(options) {
            requests.push(options);
            if (options.type === 'GET') return rejectContext ? Promise.reject(new Error('offline')) : Promise.resolve({ FromProxy: proxy });
            return Promise.resolve({ Url: 'https://cloud.example/edge/v1/exports/id/file?media_token=scoped' });
        }
    };
    const window = { ApiClient, isSecureContext: true };
    vm.runInNewContext(source, {
        window, document, Element, MutationObserver: class { constructor(callback) { changed = callback; } observe() {} },
        navigator: { clipboard: { writeText: async url => copies.push(url) } },
        location: { hash: '#/details?id=selected-media', search: '' },
        URLSearchParams, Date, setTimeout: () => 0, clearTimeout() {}
    });
    function dispatch(target) {
        let stopped = false;
        const event = { target, preventDefault() {}, stopImmediatePropagation() { stopped = true; } };
        for (const handler of handlers.click || []) { handler(event); if (stopped) break; }
        if (!stopped && target.sheet) for (const handler of sheetHandlers) { handler(event); if (stopped) break; }
        if (!stopped) nativeClicks++;
    }
    return { click: () => dispatch(button), flush: () => new Promise(resolve => setImmediate(resolve)),
        openMenu() { for (const handler of handlers.pointerdown || []) handler({ target: more }); changed(); },
        stream: () => dispatch(stream), share: () => dispatch(shares[0]),
        create: window.JellyfinEdge.create, requests, downloads, copies, get nativeClicks() { return nativeClicks; } };
}

test('direct host download reaches the native handler without a cloud export', async () => {
    const p = page(false); p.click(); await p.flush();
    assert.equal(p.nativeClicks, 1);
    assert.equal(p.requests.filter(r => r.type === 'POST').length, 0);
    assert.deepEqual(p.downloads, []);
});

test('direct host copy-stream menu preserves the native URL handler', async () => {
    const p = page(false); p.openMenu(); await p.flush(); p.stream(); await p.flush();
    assert.equal(p.nativeClicks, 1);
    assert.equal(p.requests.filter(r => r.type === 'POST').length, 0);
    assert.deepEqual(p.copies, []);
});

test('proxy copy-stream menu copies the scoped proxy export', async () => {
    const p = page(true); p.openMenu(); await p.flush(); p.stream(); await p.flush();
    assert.equal(p.nativeClicks, 0);
    assert.equal(JSON.parse(p.requests.find(r => r.type === 'POST').data).Operation, 'stream');
    assert.match(p.copies[0], /^https:\/\/cloud\.example\/edge\/v1\/exports\//);
});

test('direct host share menu still routes to the cloud', async () => {
    const p = page(false); p.openMenu(); await p.flush(); p.share(); await p.flush();
    assert.equal(p.nativeClicks, 0);
    assert.equal(JSON.parse(p.requests.find(r => r.type === 'POST').data).Operation, 'share');
    assert.match(p.copies[0], /media_token=scoped/);
});

test('proxy download stays on the proxy export and does not run the native handler', async () => {
    const p = page(true); p.click(); await p.flush();
    assert.equal(p.nativeClicks, 0);
    assert.equal(JSON.parse(p.requests.find(r => r.type === 'POST').data).Operation, 'download');
    assert.equal(p.downloads.length, 1);
    assert.match(p.downloads[0], /^https:\/\/cloud\.example\/edge\/v1\/exports\//);
});

test('direct host sharing still copies a scoped cloud URL', async () => {
    const p = page(false); await p.create({ itemId: 'media' }, 'share');
    assert.equal(p.copies.length, 1);
    assert.match(p.copies[0], /media_token=scoped/);
    assert.equal(JSON.parse(p.requests.find(r => r.type === 'POST').data).Operation, 'share');
});

test('failed access lookup does not guess a route or invoke either download', async () => {
    const p = page(false, true); p.click(); await p.flush();
    assert.equal(p.nativeClicks, 0);
    assert.equal(p.requests.filter(r => r.type === 'POST').length, 0);
    assert.deepEqual(p.downloads, []);
});
