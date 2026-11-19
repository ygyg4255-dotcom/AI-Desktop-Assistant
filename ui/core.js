/* Altron reactive core: canvas HUD ring set driven by window.AltronCore.setState(). */
(function () {
  'use strict';
  const canvas = document.getElementById('core-canvas');
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext('2d');
  const SIZE = 300, C = SIZE / 2, TAU = Math.PI * 2, BARS = 56;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = SIZE * dpr; canvas.height = SIZE * dpr;
  ctx.scale(dpr, dpr);

  // Per-state targets: ring speed, glow, bar level, halo pulse, scanner, brightness.
  const PRESETS = {
    idle:      { spin: 0.10, glow: 0.35, bars: 0.04, pulse: 0.04, scan: 0, bright: 0.65, fps: 30 },
    listening: { spin: 0.45, glow: 0.85, bars: 0.18, pulse: 0.16, scan: 0, bright: 1.0,  fps: 60 },
    thinking:  { spin: 1.10, glow: 0.60, bars: 0.07, pulse: 0.06, scan: 1, bright: 0.9,  fps: 60 },
    speaking:  { spin: 0.30, glow: 0.95, bars: 0.55, pulse: 0.30, scan: 0, bright: 1.0,  fps: 60 },
    offline:   { spin: 0,    glow: 0.05, bars: 0,    pulse: 0,    scan: 0, bright: 0.25, fps: 5 },
  };
  let target = PRESETS.idle, stateName = 'idle';
  const cur = Object.assign({}, PRESETS.idle);
  let angA = 0, angB = 0, scanAng = 0, last = 0, lastDraw = 0, running = false;

  function rgba(a, g, b) { return 'rgba(' + [255, g, b].join(',') + ',' + a + ')'; }

  // Smooth multi-frequency "voice" envelope in 0..1 (speech-like, no backend needed).
  function envelope(t, i) {
    const w = Math.sin(t * 7.3 + i * 0.9) * 0.5 + Math.sin(t * 12.1 + i * 1.7) * 0.3 +
              Math.sin(t * 3.1 + i * 0.35) * 0.2;
    const syll = 0.55 + 0.45 * Math.sin(t * 5.2);
    return Math.max(0, Math.min(1, (w * 0.5 + 0.5) * syll));
  }

  function arcs(r, n, gap, w, a, rot) {
    const step = TAU / n;
    ctx.lineWidth = w; ctx.strokeStyle = a;
    for (let i = 0; i < n; i++) {
      ctx.beginPath();
      ctx.arc(C, C, r, rot + i * step, rot + i * step + step - gap);
      ctx.stroke();
    }
  }

  function ticks(r, n, len, rot, a) {
    ctx.strokeStyle = a; ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const th = rot + (i / n) * TAU, l = i % 5 === 0 ? len * 1.8 : len;
      ctx.moveTo(C + Math.cos(th) * r, C + Math.sin(th) * r);
      ctx.lineTo(C + Math.cos(th) * (r - l), C + Math.sin(th) * (r - l));
    }
    ctx.stroke();
  }

  function draw(t) {
    const b = cur.bright, g = cur.glow;
    const dead = stateName === 'offline';
    const sat = dead ? 60 : 30; // green/blue channel: grey-ish red when offline
    ctx.clearRect(0, 0, SIZE, SIZE);
    ctx.lineCap = 'round';

    // Breathing halo
    const breathe = 1 + Math.sin(t * (stateName === 'idle' ? 1.2 : 3)) * cur.pulse * 0.5;
    const halo = ctx.createRadialGradient(C, C, 20, C, C, 110 * breathe);
    halo.addColorStop(0, rgba(0.30 * g, sat, sat + 10));
    halo.addColorStop(0.6, rgba(0.10 * g, sat, sat + 10));
    halo.addColorStop(1, rgba(0, sat, sat));
    ctx.fillStyle = halo; ctx.beginPath(); ctx.arc(C, C, 120, 0, TAU); ctx.fill();

    // Outer segmented arcs (clockwise)
    ctx.shadowColor = rgba(1, 30, 45); ctx.shadowBlur = 10 * g;
    arcs(140, 4, 0.5, 3, rgba(0.85 * b, sat, sat + 10), angA);
    arcs(131, 12, 0.22, 1.5, rgba(0.5 * b, sat, sat + 10), -angB * 0.6);
    ctx.shadowBlur = 0;

    // Dashed/ticked dial ring (counter-rotating)
    ticks(122, 90, 5, -angA * 1.4, rgba(0.55 * b, sat, sat + 10));
    ctx.setLineDash([2, 6]); ctx.lineWidth = 1;
    ctx.strokeStyle = rgba(0.35 * b, sat, sat + 10);
    ctx.beginPath(); ctx.arc(C, C, 108, 0, TAU); ctx.stroke(); ctx.setLineDash([]);

    // Thinking: sweeping scanner arc
    if (cur.scan > 0.02) {
      const sw = ctx.createConicGradient ? ctx.createConicGradient(scanAng, C, C) : null;
      if (sw) {
        sw.addColorStop(0, rgba(0.55 * cur.scan, 40, 50));
        sw.addColorStop(0.18, rgba(0, 40, 50));
        sw.addColorStop(1, rgba(0, 40, 50));
        ctx.fillStyle = sw; ctx.beginPath(); ctx.arc(C, C, 104, 0, TAU); ctx.fill();
      }
      ctx.strokeStyle = rgba(0.95 * cur.scan, 60, 70); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(C, C);
      ctx.lineTo(C + Math.cos(scanAng) * 104, C + Math.sin(scanAng) * 104); ctx.stroke();
    }

    // Radial equalizer bars
    const base = 70, maxLen = 30;
    ctx.lineWidth = 2.5; ctx.shadowColor = rgba(1, 30, 45); ctx.shadowBlur = 6 * g;
    for (let i = 0; i < BARS; i++) {
      const th = (i / BARS) * TAU - Math.PI / 2;
      let lvl;
      if (stateName === 'speaking') lvl = envelope(t, i);
      else if (stateName === 'listening') lvl = 0.5 + 0.5 * Math.sin(t * 4 - i * 0.35);
      else lvl = 0.3 + 0.3 * Math.sin(t * 1.5 + i * 0.5);
      const len = 3 + lvl * cur.bars * maxLen * 2;
      ctx.strokeStyle = rgba((0.45 + 0.5 * lvl) * b, sat + 20 * lvl, sat + 25 * lvl);
      ctx.beginPath();
      ctx.moveTo(C + Math.cos(th) * base, C + Math.sin(th) * base);
      ctx.lineTo(C + Math.cos(th) * (base + len), C + Math.sin(th) * (base + len));
      ctx.stroke();
    }
    ctx.shadowBlur = 0;

    // Inner pulse rings (listening waves / speaking expansion)
    const wave = (t * (stateName === 'speaking' ? 1.6 : 0.9)) % 1;
    if (cur.pulse > 0.08) {
      ctx.lineWidth = 1.5;
      for (let k = 0; k < 2; k++) {
        const p = (wave + k * 0.5) % 1;
        ctx.strokeStyle = rgba((1 - p) * 0.6 * b, 40, 50);
        ctx.beginPath(); ctx.arc(C, C, 40 + p * 50, 0, TAU); ctx.stroke();
      }
    }
    const speakBoost = stateName === 'speaking' ? envelope(t, 3) * 5 : 0;
    ctx.lineWidth = 2; ctx.strokeStyle = rgba(0.9 * b, sat, sat + 10);
    ctx.shadowColor = rgba(1, 30, 45); ctx.shadowBlur = 14 * g;
    ctx.beginPath(); ctx.arc(C, C, 58 + speakBoost * breathe * 0.6, 0, TAU); ctx.stroke();
    ctx.shadowBlur = 0;
    arcs(48, 3, 0.9, 2, rgba(0.8 * b, sat, sat + 10), angB * 1.5);

    // Center wordmark
    ctx.fillStyle = dead ? 'rgba(150,150,160,0.5)' : 'rgba(255,240,240,' + (0.75 + 0.25 * b) + ')';
    ctx.shadowColor = rgba(1, 30, 45); ctx.shadowBlur = 10 * g;
    ctx.font = '700 15px Consolas, "Courier New", monospace';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    if ('letterSpacing' in ctx) ctx.letterSpacing = '3px';
    ctx.fillText('ALTRON', C + 1.5, C);
    ctx.shadowBlur = 0;
  }

  function frame(ts) {
    if (!running) return;
    requestAnimationFrame(frame);
    if (ts - lastDraw < 1000 / target.fps - 2) return;
    const dt = Math.min((ts - (last || ts)) / 1000, 0.1);
    last = ts; lastDraw = ts;
    const k = 1 - Math.exp(-dt * 4);
    for (const key of ['spin', 'glow', 'bars', 'pulse', 'scan', 'bright']) {
      cur[key] += (target[key] - cur[key]) * k;
    }
    angA += cur.spin * dt; angB -= cur.spin * 0.7 * dt; scanAng += cur.scan * 3.2 * dt;
    draw(ts / 1000);
  }

  function start() { if (!running) { running = true; last = 0; requestAnimationFrame(frame); } }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) running = false; else start();
  });

  window.AltronCore = {
    setState: function (name) {
      const n = PRESETS[name] ? name : 'idle';
      stateName = n; target = PRESETS[n];
    },
  };
  start();
})();
