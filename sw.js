// sw.js  —  Generic Service Worker for PWA
const CACHE   = 'ai-diag-v1';
const SHELL   =['/', '/index.html', '/machine.html', '/admin.html', '/manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(SHELL))