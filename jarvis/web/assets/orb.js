/* Central AI visualization.
   Purely presentational: its state is set by app.js from real backend events.
   States: offline | standby | processing | awaiting | speaking | error */
(function () {
  "use strict";

  const PALETTES = {
    standby:    { core: [95, 227, 255], ring: [42, 140, 255], energy: 0.35, speed: 1.0 },
    processing: { core: [120, 236, 255], ring: [70, 170, 255], energy: 0.95, speed: 3.2 },
    speaking:   { core: [140, 240, 255], ring: [80, 190, 255], energy: 0.75, speed: 1.6 },
    awaiting:   { core: [255, 190, 90], ring: [255, 160, 60], energy: 0.6, speed: 0.8 },
    error:      { core: [255, 100, 128], ring: [210, 60, 90], energy: 0.45, speed: 0.6 },
    offline:    { core: [80, 105, 130], ring: [55, 75, 100], energy: 0.12, speed: 0.3 },
  };

  const lerp = (a, b, t) => a + (b - a) * t;
  const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;

  class Orb {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.state = "offline";
      const p = PALETTES.offline;
      this.cur = { core: p.core.slice(), ring: p.ring.slice(), energy: p.energy, speed: p.speed };
      this.t = 0;
      this.last = performance.now();
      this.reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.particles = Array.from({ length: 150 }, () => this._particle());
      this._resize = this._resize.bind(this);
      window.addEventListener("resize", this._resize);
      this._resize();
      this._frame = this._frame.bind(this);
      requestAnimationFrame(this._frame);
    }

    _particle() {
      return {
        a: Math.random() * Math.PI * 2,
        r: 0.55 + Math.random() * 0.95,
        v: (0.04 + Math.random() * 0.12) * (Math.random() < 0.5 ? -1 : 1),
        s: 0.4 + Math.random() * 1.3,
        ph: Math.random() * Math.PI * 2,
      };
    }

    setState(state) {
      if (PALETTES[state]) this.state = state;
    }

    _resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = this.canvas.getBoundingClientRect();
      this.w = rect.width;
      this.h = rect.height;
      this.canvas.width = Math.max(1, Math.round(rect.width * dpr));
      this.canvas.height = Math.max(1, Math.round(rect.height * dpr));
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    _frame(now) {
      const dt = Math.min(0.05, (now - this.last) / 1000);
      this.last = now;
      const target = PALETTES[this.state];
      const k = 1 - Math.pow(0.02, dt); // smooth ~1s transitions
      for (let i = 0; i < 3; i++) {
        this.cur.core[i] = lerp(this.cur.core[i], target.core[i], k);
        this.cur.ring[i] = lerp(this.cur.ring[i], target.ring[i], k);
      }
      this.cur.energy = lerp(this.cur.energy, target.energy, k);
      this.cur.speed = lerp(this.cur.speed, target.speed, k);
      const motion = this.reduced ? 0.15 : 1;
      this.t += dt * this.cur.speed * motion;
      this._draw(dt * motion);
      requestAnimationFrame(this._frame);
    }

    _draw(dt) {
      const { ctx, w, h, cur } = this;
      ctx.clearRect(0, 0, w, h);
      const cx = w / 2;
      const cy = h / 2 - 10;
      const R = Math.min(w, h) * 0.36;
      const e = cur.energy;
      const t = this.t;
      const pulse = 0.5 + 0.5 * Math.sin(t * 2.2);

      // Ambient glow
      let g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 1.7);
      g.addColorStop(0, rgba(cur.core, 0.16 + 0.12 * e));
      g.addColorStop(0.45, rgba(cur.ring, 0.05 + 0.04 * e));
      g.addColorStop(1, rgba(cur.ring, 0));
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);

      ctx.lineCap = "round";

      // Outer tick ring
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(t * 0.04);
      for (let i = 0; i < 120; i++) {
        const a = (i / 120) * Math.PI * 2;
        const long = i % 10 === 0;
        const r1 = R * 1.2;
        const r2 = r1 + (long ? 9 : 4);
        ctx.strokeStyle = rgba(cur.ring, long ? 0.55 : 0.22);
        ctx.lineWidth = long ? 1.4 : 1;
        ctx.beginPath();
        ctx.moveTo(Math.cos(a) * r1, Math.sin(a) * r1);
        ctx.lineTo(Math.cos(a) * r2, Math.sin(a) * r2);
        ctx.stroke();
      }
      ctx.restore();

      // Segmented arc ring
      this._arcs(cx, cy, R * 1.06, t * 0.35, [[0, 0.9], [1.25, 2.1], [2.6, 3.3], [3.7, 5.5]], cur.ring, 0.65, 2.2);
      // Thin full ring
      ctx.strokeStyle = rgba(cur.ring, 0.18);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(cx, cy, R * 0.98, 0, Math.PI * 2);
      ctx.stroke();
      // Counter-rotating dashed ring
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(-t * 0.22);
      ctx.setLineDash([2, 7]);
      ctx.strokeStyle = rgba(cur.core, 0.45);
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(0, 0, R * 0.88, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
      ctx.setLineDash([]);

      // Inner sweeping arcs (more active when processing)
      const sweep = 0.6 + e * 2.2;
      this._arcs(cx, cy, R * 0.72, -t * 0.9, [[0, sweep], [Math.PI, Math.PI + sweep * 0.7]], cur.core, 0.25 + e * 0.6, 2.5);

      // Voice-like waveform ring (only visible with energy)
      if (e > 0.5) {
        ctx.strokeStyle = rgba(cur.core, (e - 0.5) * 0.9);
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        for (let i = 0; i <= 180; i++) {
          const a = (i / 180) * Math.PI * 2;
          const n = Math.sin(a * 6 + t * 3) * 0.5 + Math.sin(a * 11 - t * 4.3) * 0.35 + Math.sin(a * 3 + t * 1.7) * 0.15;
          const r = R * (0.55 + n * 0.035 * e);
          const x = cx + Math.cos(a) * r;
          const y = cy + Math.sin(a) * r;
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();
      }

      // Core
      const coreR = R * (0.42 + 0.02 * pulse * e);
      g = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR);
      g.addColorStop(0, rgba([235, 252, 255], 0.85 * (0.5 + e * 0.5)));
      g.addColorStop(0.18, rgba(cur.core, 0.75));
      g.addColorStop(0.55, rgba(cur.ring, 0.22 + 0.15 * e));
      g.addColorStop(1, rgba(cur.ring, 0));
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = rgba(cur.core, 0.7);
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(cx, cy, R * 0.22, 0, Math.PI * 2);
      ctx.stroke();

      // Particles
      for (const p of this.particles) {
        p.a += p.v * dt * (0.6 + cur.speed * 0.6);
        const wobble = Math.sin(t * 0.8 + p.ph) * 0.03;
        const rr = R * (p.r - e * 0.08 + wobble);
        const x = cx + Math.cos(p.a) * rr;
        const y = cy + Math.sin(p.a) * rr * 0.98;
        const tw = 0.35 + 0.65 * (0.5 + 0.5 * Math.sin(t * 1.5 + p.ph));
        ctx.fillStyle = rgba(cur.core, (0.18 + 0.5 * e) * tw);
        ctx.beginPath();
        ctx.arc(x, y, p.s, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    _arcs(cx, cy, r, rot, segments, color, alpha, width) {
      const ctx = this.ctx;
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(rot);
      ctx.strokeStyle = rgba(color, alpha);
      ctx.lineWidth = width;
      ctx.shadowColor = rgba(color, 0.8);
      ctx.shadowBlur = 8;
      for (const [a0, a1] of segments) {
        ctx.beginPath();
        ctx.arc(0, 0, r, a0, a1);
        ctx.stroke();
      }
      ctx.restore();
    }
  }

  window.JarvisOrb = Orb;
})();
