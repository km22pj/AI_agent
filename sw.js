// Healthlingo service worker: makes the site installable and lets the app shell open offline.
// Privacy: only the static app shell is cached. API responses (records, photos, settings) are
// never stored, so health data does not stay on the device through this cache.
const CACHE = "healthlingo-shell-v1";
const SHELL = ["/", "/app.js", "/manifest.webmanifest", "/icons/icon-192.png", "/icons/icon-512.png", "/icons/apple-touch-icon.png"];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // One by one: the page is behind the invite check, so a 403 must not break installation.
    await Promise.all(SHELL.map(async (url) => {
      try {
        const res = await fetch(url, { cache: "no-store" });
        if (res.ok) await cache.put(url, res);
      } catch { /* offline during install: try again on the next visit */ }
    }));
    self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});

const OFFLINE_PAGE = `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Healthlingo — 오프라인</title></head><body style="margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;box-sizing:border-box;
background:#f7f6f2;color:#20283a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans KR',sans-serif;text-align:center">
<main><h1 style="font-size:20px">인터넷에 연결되어 있지 않아요</h1><p style="color:#72798a;font-size:14px;line-height:1.6">기록은 서버에 저장돼서, 연결되면 다시 볼 수 있어요.</p>
<button onclick="location.reload()" style="margin-top:12px;padding:10px 18px;border:0;border-radius:12px;background:#5365c7;color:#fff;font-size:14px">다시 시도</button></main></body></html>`;

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return; // never cache personal data: straight to the network

  const isShell = req.mode === "navigate" || url.pathname === "/app.js";
  if (isShell) {
    // Network first, so a new version shows up right away; the cached shell is only an offline fallback.
    event.respondWith((async () => {
      try {
        const res = await fetch(req);
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req.mode === "navigate" ? "/" : url.pathname, copy));
        }
        return res;
      } catch {
        const cached = await caches.match(req.mode === "navigate" ? "/" : url.pathname);
        return cached || new Response(OFFLINE_PAGE, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } });
      }
    })());
    return;
  }
  if (url.pathname.startsWith("/icons/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(caches.match(url.pathname).then((hit) => hit || fetch(req)));
  }
});
