const APP_ROOT = new URL("./", self.location.href);
// 避開舊 Worker 的 personal-overtime-shell- 清理範圍，並依部署路徑隔離。
const CACHE_PREFIX = "personal-overtime-app-" + encodeURIComponent(APP_ROOT.pathname) + "-";
const CACHE_NAME = CACHE_PREFIX + "v45";
const APP_SHELL = [
  "./index.html",
  "./manifest.json",
  "./icon.png",
  "./icon-192.png",
  "./icon-512.png",
  "./icon-maskable-192.png",
  "./icon-maskable-512.png",
  "./privacy.html",
  "./terms.html",
  "./RemachineScript_Personal_Use.ttf",
  "./data/dgpa_closures.json"
];
const INSTALL_ASSETS = new Set([
  "./manifest.json", "./icon.png", "./icon-192.png", "./icon-512.png",
  "./icon-maskable-192.png", "./icon-maskable-512.png"
].map(function (path) { return new URL(path, APP_ROOT).pathname; }));

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(function (cache) {
        // 必要素材全部成功才啟用；失敗時讓舊 Worker 繼續服務。
        return cache.addAll(APP_SHELL.map(function (path) {
          return new Request(new URL(path, APP_ROOT).href, { cache: "reload" });
        }));
      })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) { return Promise.all(keys.filter(function (key) { return key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME; }).map(function (key) { return caches.delete(key); })); })
      .then(function () { return self.clients.claim(); })
  );
});

async function putCachedResponse(cache, key, response) {
  try {
    await cache.put(key, response.clone());
  } catch (error) {
    console.warn("離線快取更新失敗。", error);
  }
}

async function networkFirst(request, fallbackUrl, cacheUrl, bypassHttpCache) {
  const cache = await caches.open(CACHE_NAME);
  const cacheKey = cacheUrl || request;
  try {
    const response = await fetch(request, bypassHttpCache ? { cache: "no-store" } : undefined);
    if (response && response.ok) {
      await putCachedResponse(cache, cacheKey, response);
      return response;
    }
    return (await cache.match(cacheKey)) || (fallbackUrl ? await cache.match(fallbackUrl) : null) || response;
  } catch (error) {
    return (await cache.match(cacheKey)) || (fallbackUrl ? await cache.match(fallbackUrl) : null) || Response.error();
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response && response.ok) await putCachedResponse(cache, request, response);
    return response;
  } catch (error) {
    return Response.error();
  }
}

self.addEventListener("fetch", function (event) {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== APP_ROOT.origin || !url.pathname.startsWith(APP_ROOT.pathname)) return;
  const canonicalUrl = new URL(url.pathname, APP_ROOT.origin).href;
  const appIndex = new URL("./index.html", APP_ROOT).href;
  if (request.mode === "navigate") {
    const isHomepage = url.pathname === APP_ROOT.pathname || canonicalUrl === appIndex;
    event.respondWith(networkFirst(request, appIndex, isHomepage ? appIndex : null));
    return;
  }
  if (INSTALL_ASSETS.has(url.pathname)) {
    event.respondWith(networkFirst(request, canonicalUrl, canonicalUrl, true));
    return;
  }
  if (canonicalUrl === new URL("./data/dgpa_closures.json", APP_ROOT).href) {
    event.respondWith(networkFirst(request, canonicalUrl, canonicalUrl));
    return;
  }
  event.respondWith(cacheFirst(request));
});
