// Live viewer page logic. Served from /viewer.js so the page needs no inline
// script (CSP script-src 'self'). All server text is written with textContent.
'use strict';

(() => {
  const token = document.querySelector('meta[name="viewer-token"]').content;
  const $ = id => document.getElementById(id);
  const REFRESH_MS = 500;
  const STREAM_BACKOFF_MIN_MS = 500;
  const STREAM_BACKOFF_MAX_MS = 10000;

  function showError(error) {
    $('error').textContent = error ? String(error.message || error) : '';
  }

  function showMessage(text) {
    $('message').textContent = text;
  }

  async function api(path, body) {
    const response = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Viewer-Token': token},
      body: JSON.stringify(body || {}),
      cache: 'no-store',
      credentials: 'same-origin',
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) { payload = {}; }
    const text = payload.message || payload.error || `${response.status} ${response.statusText}`;
    showMessage(text);
    if (!response.ok) throw new Error(text);
    return payload;
  }

  // Run a handler with its button disabled; never let a rejection escape.
  function guarded(button, handler) {
    return async event => {
      if (button.disabled) return;
      button.disabled = true;
      try {
        showError('');
        await handler(event);
        await refreshOnce();
      } catch (error) {
        showError(error);
      } finally {
        button.disabled = false;
      }
    };
  }

  function render(s) {
    $('fps').textContent = Number(s.fps).toFixed(2);
    $('frames').textContent = String(s.frames);
    $('focus').textContent = Number(s.focus).toFixed(1);
    $('best').textContent = Number(s.best_focus).toFixed(1);
    $('size').textContent = `${s.width}×${s.height} · ${s.bytes} B`;
    const [r, g, b] = s.light_rgb;
    $('light').textContent = (r || g || b) ? `RGB ${r}/${g}/${b}` : 'Off';
    let laser = s.laser;
    if (s.laser === 'Off' && s.laser_off_reason) laser = `Off (${s.laser_off_reason})`;
    $('laser').textContent = laser;
    $('laser-max').textContent = String(s.laser_max_on_s);
    $('laser-heartbeat').textContent = String(s.laser_heartbeat_s);
    $('camera-automation').textContent = s.camera_automation;
    $('camera-controls').textContent = s.camera_controls || 'Not read yet';
    $('error').textContent = s.error || '';
    const banner = $('laser-banner');
    if (s.laser === 'On') {
      const remaining = s.laser_remaining_s === null ? '?' : Math.ceil(s.laser_remaining_s);
      banner.textContent = `LASER ON · switches off in ${remaining} s`;
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
  }

  async function refreshOnce() {
    const response = await fetch('/status.json', {cache: 'no-store', headers: {'X-Viewer-Token': token}});
    if (!response.ok) throw new Error(`status: ${response.status}`);
    render(await response.json());
  }

  // setTimeout chain: the next poll starts only after the previous one ended.
  async function refreshLoop() {
    try {
      await refreshOnce();
    } catch (error) {
      showError(error);
    } finally {
      window.setTimeout(refreshLoop, REFRESH_MS);
    }
  }

  function setLight(r, g, b) {
    return api('/light', {r, g, b});
  }

  function level() {
    return Number($('brightness').value);
  }

  function applySelectedColor() {
    const value = $('color').value;
    const source = [1, 3, 5].map(index => parseInt(value.slice(index, index + 2), 16));
    const peak = Math.max(...source);
    const scaled = peak ? source.map(channel => Math.round(channel * level() / peak)) : [0, 0, 0];
    return setLight(...scaled);
  }

  function laser(on) {
    if (on && !window.confirm('Switch the laser ON? Never look into the beam. Keep the manual switch within reach.')) {
      return Promise.resolve();
    }
    return api('/laser', {on});
  }

  async function shutdownViewer() {
    try { await setLight(0, 0, 0); } catch (_) { /* best effort */ }
    try { await laser(false); } catch (_) { /* best effort */ }
    await api('/shutdown', {});
  }

  // MJPEG stream with reconnect and exponential backoff.
  function startStream() {
    const image = $('stream');
    const status = $('stream-state');
    let delay = STREAM_BACKOFF_MIN_MS;
    let timer = null;
    const connect = () => {
      timer = null;
      image.src = `${image.dataset.src}?token=${encodeURIComponent(token)}&t=${Date.now()}`;
    };
    image.addEventListener('load', () => {
      delay = STREAM_BACKOFF_MIN_MS;
      status.textContent = '';
    });
    image.addEventListener('error', () => {
      if (timer !== null) return;
      status.textContent = `Stream interrupted · reconnecting in ${(delay / 1000).toFixed(1)} s`;
      timer = window.setTimeout(connect, delay);
      delay = Math.min(delay * 2, STREAM_BACKOFF_MAX_MS);
    });
    connect();
  }

  function bind(selector, handlerFor) {
    document.querySelectorAll(selector).forEach(button => {
      button.addEventListener('click', guarded(button, () => handlerFor(button)));
    });
  }

  function init() {
    bind('[data-light]', button => setLight(...button.dataset.light.split(',').map(Number)));
    bind('[data-preset]', button => setLight(...button.dataset.preset.split(',').map(value => Number(value) * level())));
    bind('[data-camera]', button => api('/camera', {action: button.dataset.camera}));
    bind('[data-laser]', button => laser(button.dataset.laser === 'on'));
    bind('#apply-color', () => applySelectedColor());
    bind('#save', () => api('/save', {}));
    bind('#shutdown', () => shutdownViewer());
    $('brightness').addEventListener('input', event => {
      $('brightness-value').value = event.target.value;
    });
    window.addEventListener('unhandledrejection', event => {
      showError(event.reason);
      event.preventDefault();
    });
    startStream();
    refreshLoop();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
