/* ===================================================================
   mission.mhpserver.cc — dashboard front end
   Talks to /api/data only; that endpoint is itself a cache of Launch
   Library 2, so polling here is cheap and never touches the upstream
   rate limit.
   =================================================================== */
(function () {
  "use strict";

  var POLL_MS = 60000;
  var state = {
    data: null,
    view: "launches",
    operator: "all",
    confirmation: "all",
    query: "",
    site: null,
    clusters: [],
    heroLaunch: null,
    failures: 0
  };

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  /* ── Escaping ────────────────────────────────────────────────────
     Launch names, mission blurbs and agency descriptions are community
     contributed upstream, so every interpolated string is escaped. */
  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* Images are routed through the backend proxy: it caches them and keeps
     visitor IPs off the upstream CDN. */
  function img(url) {
    if (!url) return null;
    return "/api/img?u=" + encodeURIComponent(url);
  }

  /* ── Starfield ───────────────────────────────────────────────────── */

  function starfield() {
    var canvas = document.getElementById("starfield");
    if (!canvas || !canvas.getContext) return;
    var ctx = canvas.getContext("2d");
    var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var stars = [], shooting = null, w = 0, h = 0, dpr = 1, raf = null;

    function build() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = window.innerWidth; h = window.innerHeight;
      canvas.width = w * dpr; canvas.height = h * dpr;
      canvas.style.width = w + "px"; canvas.style.height = h + "px";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      // Three parallax layers: far stars are dim, small and nearly still.
      stars = [];
      var count = Math.round(Math.min(w * h / 5200, 620));
      for (var i = 0; i < count; i++) {
        var layer = i % 3;
        stars.push({
          x: Math.random() * w,
          y: Math.random() * h,
          r: [0.5, 0.85, 1.35][layer] * (0.7 + Math.random() * 0.7),
          a: [0.32, 0.55, 0.85][layer] * (0.5 + Math.random() * 0.5),
          vx: [0.004, 0.011, 0.022][layer],
          tw: Math.random() * Math.PI * 2,
          tws: 0.6 + Math.random() * 1.6,
          hue: Math.random() < 0.12 ? "190, 230, 255" : (Math.random() < 0.08 ? "200, 190, 255" : "255, 255, 255")
        });
      }
    }

    function draw(t) {
      ctx.clearRect(0, 0, w, h);
      for (var i = 0; i < stars.length; i++) {
        var s = stars[i];
        var alpha = reduced ? s.a : s.a * (0.68 + 0.32 * Math.sin(t / 1000 * s.tws + s.tw));
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(" + s.hue + "," + alpha.toFixed(3) + ")";
        ctx.fill();
        if (!reduced) {
          s.x += s.vx;
          if (s.x > w + 2) s.x = -2;
        }
      }

      if (!reduced) {
        if (!shooting && Math.random() < 0.0022) {
          shooting = { x: Math.random() * w * 0.7, y: Math.random() * h * 0.45, len: 0, max: 120 + Math.random() * 130, sp: 7 + Math.random() * 5 };
        }
        if (shooting) {
          shooting.x += shooting.sp; shooting.y += shooting.sp * 0.42;
          shooting.len = Math.min(shooting.len + shooting.sp * 1.6, shooting.max);
          var g = ctx.createLinearGradient(shooting.x, shooting.y, shooting.x - shooting.len, shooting.y - shooting.len * 0.42);
          g.addColorStop(0, "rgba(180, 240, 255, 0.9)");
          g.addColorStop(1, "rgba(180, 240, 255, 0)");
          ctx.strokeStyle = g; ctx.lineWidth = 1.6; ctx.lineCap = "round";
          ctx.beginPath();
          ctx.moveTo(shooting.x, shooting.y);
          ctx.lineTo(shooting.x - shooting.len, shooting.y - shooting.len * 0.42);
          ctx.stroke();
          if (shooting.x - shooting.len > w || shooting.y - shooting.len > h) shooting = null;
        }
      }
      raf = requestAnimationFrame(draw);
    }

    build();
    if (reduced) { draw(0); cancelAnimationFrame(raf); ctx.clearRect(0,0,w,h); drawStatic(); }
    else raf = requestAnimationFrame(draw);

    function drawStatic() {
      for (var i = 0; i < stars.length; i++) {
        var s = stars[i];
        ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(" + s.hue + "," + s.a.toFixed(3) + ")"; ctx.fill();
      }
    }

    var rt;
    window.addEventListener("resize", function () {
      clearTimeout(rt);
      rt = setTimeout(function () { build(); if (reduced) drawStatic(); }, 180);
    });

    // Pause the loop when the tab is hidden — no point animating offscreen.
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) { if (raf) cancelAnimationFrame(raf); raf = null; }
      else if (!reduced && !raf) raf = requestAnimationFrame(draw);
    });
  }

  /* ── Time helpers ────────────────────────────────────────────────── */

  function parseNet(iso) {
    if (!iso) return null;
    var d = new Date(iso);
    return isNaN(d.getTime()) ? null : d;
  }

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  /* LL2 states how precisely T-0 is known. Anything coarser than an hour is
     an estimate and the UI must not imply otherwise. */
  var PRECISION = {
    SEC: { exact: true, label: "T‑0 confirmed to the second" },
    MIN: { exact: true, label: "T‑0 confirmed to the minute" },
    HR:  { exact: true, label: "T‑0 confirmed to the hour" },
    DAY: { exact: false, label: "Date set, launch time not yet announced" },
    WEEK: { exact: false, label: "Estimated — expected some time this week" },
    MONTH: { exact: false, label: "Estimated — expected this month" },
    QUARTER: { exact: false, label: "Estimated — expected this quarter" },
    HALF: { exact: false, label: "Estimated — expected this half-year" },
    YEAR: { exact: false, label: "Estimated — expected this year" },
    FY: { exact: false, label: "Estimated — expected this fiscal year" }
  };

  function precisionOf(launch) {
    return PRECISION[launch.net_precision] || { exact: false, label: "Launch date not yet firm" };
  }

  function fmtDate(d, opts) {
    if (!d) return "TBD";
    return d.toLocaleDateString(undefined, opts || { month: "short", day: "numeric", year: "numeric" });
  }

  function fmtTime(d) {
    if (!d) return "";
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }

  function fmtDateTime(d) {
    if (!d) return "TBD";
    return d.toLocaleString(undefined, {
      weekday: "short", month: "short", day: "numeric", year: "numeric",
      hour: "2-digit", minute: "2-digit", timeZoneName: "short"
    });
  }

  function countdownParts(target) {
    var diff = target.getTime() - Date.now();
    var past = diff < 0;
    var s = Math.floor(Math.abs(diff) / 1000);
    return {
      past: past,
      d: Math.floor(s / 86400),
      h: Math.floor(s % 86400 / 3600),
      m: Math.floor(s % 3600 / 60),
      s: s % 60,
      totalSeconds: s
    };
  }

  function compactCountdown(target) {
    var c = countdownParts(target);
    var sign = c.past ? "T+" : "T‑";
    if (c.d > 0) return sign + c.d + "d " + pad(c.h) + "h";
    if (c.h > 0) return sign + pad(c.h) + ":" + pad(c.m) + ":" + pad(c.s);
    return sign + pad(c.m) + ":" + pad(c.s);
  }

  /* "P381DT6H27M33S" → "381d 6h" */
  function humanDuration(iso) {
    if (!iso || typeof iso !== "string") return null;
    var m = /^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?$/.exec(iso);
    if (!m) return null;
    var years = +(m[1] || 0), months = +(m[2] || 0), days = +(m[3] || 0), hours = +(m[4] || 0), mins = +(m[5] || 0);
    var totalDays = years * 365 + months * 30 + days;
    if (totalDays >= 1) return totalDays + "d" + (hours ? " " + hours + "h" : "");
    if (hours >= 1) return hours + "h" + (mins ? " " + mins + "m" : "");
    return mins + "m";
  }

  function relativeTime(d) {
    var diff = Date.now() - d.getTime();
    var mins = Math.round(diff / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return mins + " min ago";
    var hrs = Math.round(mins / 60);
    if (hrs < 24) return hrs + (hrs === 1 ? " hour ago" : " hours ago");
    var days = Math.round(hrs / 24);
    return days + (days === 1 ? " day ago" : " days ago");
  }

  /* ── Status mapping ──────────────────────────────────────────────── */

  /* LL2 status ids: 1 Go · 2 TBD · 3 Success · 4 Failure · 5 Hold ·
     6 In Flight · 7 Partial Failure · 8 TBC */
  function statusPill(status) {
    var id = status && status.id;
    var name = (status && status.name) || "Unknown";
    var cls = "pill-neutral";
    if (id === 1) { cls = "pill-good"; name = "Go for launch"; }
    else if (id === 3) { cls = "pill-good"; name = "Success"; }
    else if (id === 2) { cls = "pill-warning"; name = "Date TBD"; }
    else if (id === 8) { cls = "pill-warning"; name = "Date unconfirmed"; }
    else if (id === 5) { cls = "pill-warning"; name = "On hold"; }
    else if (id === 6) { cls = "pill-live"; name = "In flight"; }
    else if (id === 7) { cls = "pill-serious"; name = "Partial failure"; }
    else if (id === 4) { cls = "pill-critical"; name = "Failure"; }
    return '<span class="pill ' + cls + '"><span>' + esc(name) + "</span></span>";
  }

  function isConfirmed(launch) {
    return launch.status && (launch.status.id === 1 || launch.status.id === 6);
  }

  /* The mission name is the useful headline when there is one. When the payload
     is undisclosed LL2 still names the launch ("Long March 7A | Unknown
     Payload"), which carries more than the mission name alone. */
  function launchTitle(l) {
    var mission = l.mission && l.mission.name;
    // "Unknown Payload" is LL2's stand-in for an undisclosed payload. The full
    // launch name ("Long March 7A | Unknown Payload") says strictly more.
    if (mission && !/^unknown\b/i.test(mission)) return mission;
    return l.name || mission || "Unnamed launch";
  }

  /* An <img> whose upstream URL 404s must fall back to the initials tile rather
     than leave an empty circle, so this renders both and swaps on error. */
  function avatar(url, label, cls) {
    var fallback = '<div class="' + cls + " " + cls + '-fallback" aria-hidden="true">' + esc(label) + "</div>";
    if (!url) return fallback;
    // data-fb is swapped in by the delegated error listener in wire().
    return '<img class="' + cls + '" src="' + esc(url) + '" alt="" loading="lazy" data-fb="' + esc(fallback) + '">';
  }

  /* ── Fetching ────────────────────────────────────────────────────── */

  function load() {
    return fetch("/api/data", { headers: { Accept: "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        state.data = data;
        state.failures = 0;
        render();
        updateSync();
      })
      .catch(function (err) {
        state.failures++;
        console.warn("data fetch failed", err);
        if (!state.data) {
          $("#globalEmpty").hidden = false;
          $$(".view").forEach(function (v) { if (v.id !== "view-launches") return; });
        }
        updateSync();
      });
  }

  function updateSync() {
    var el = $("#sync"), text = $("#syncText");
    if (!state.data) {
      el.className = "sync is-down";
      text.textContent = state.failures ? "Connection lost" : "Acquiring telemetry…";
      return;
    }
    var feeds = state.data.feeds || {};
    var up = feeds.upcoming || {};
    var age = up.age_seconds;
    var stale = age === null || age === undefined || age > 3 * 3600 || state.failures > 0;
    el.className = "sync" + (stale ? " is-stale" : "");
    var label = age === null || age === undefined
      ? "no data yet"
      : (age < 90 ? "just synced" : Math.round(age / 60) + " min ago");
    text.textContent = "Live · schedule " + label;

    var meta = $("#footMeta");
    var parts = [];
    Object.keys(feeds).forEach(function (k) {
      var f = feeds[k];
      parts.push(k + " " + (f.age_seconds === null || f.age_seconds === undefined ? "—" : Math.round(f.age_seconds / 60) + "m"));
    });
    meta.textContent = "Feed age — " + parts.join(" · ") + ". The schedule refreshes every 20 minutes; crew and vehicle rosters hourly.";
  }

  /* ── Render: hero ────────────────────────────────────────────────── */

  function pickHero(data) {
    var list = data.upcoming || [];
    // Prefer something actually in flight, otherwise the soonest launch.
    for (var i = 0; i < list.length; i++) {
      if (list[i].status && list[i].status.id === 6) return list[i];
    }
    return list[0] || null;
  }

  function renderHero() {
    var hero = $("#hero");
    var launch = pickHero(state.data);
    state.heroLaunch = launch;
    if (!launch) { hero.hidden = true; return; }
    hero.hidden = false;

    var media = $("#heroMedia");
    var src = img(launch.image);
    media.style.backgroundImage = src ? 'url("' + src + '")' : "none";

    var inFlight = launch.status && launch.status.id === 6;
    var tag = $("#heroTag");
    tag.textContent = inFlight ? "In flight now" : "Next launch";
    tag.className = "tag tag-live";

    var p = launch.provider || {};
    $("#heroProvider").textContent = (p.name || "Unknown operator") +
      (p.country && p.country.length ? " · " + p.country.join(", ") : "");

    $("#heroHeading").textContent = launchTitle(launch);

    /* Built from whatever is actually known. The backend nulls out LL2's
       placeholder strings, so an unannounced payload reads "Flying on Long
       March 7A." rather than "unknown mission · to Unknown". */
    var sub = [];
    if (launch.rocket && launch.rocket.name) sub.push("Flying on " + launch.rocket.name);
    if (launch.mission && launch.mission.type) sub.push(launch.mission.type.toLowerCase() + " mission");
    if (launch.mission && launch.mission.orbit) sub.push("bound for " + launch.mission.orbit);
    $("#heroSub").textContent = sub.length ? sub.join(" · ") + "." : "Payload and mission profile not yet announced.";

    var prec = precisionOf(launch);
    var net = parseNet(launch.net);
    var note = $("#heroT0");
    if (net) {
      note.textContent = fmtDateTime(net) + " · " + prec.label;
    } else {
      note.textContent = "No launch date announced yet.";
    }

    var facts = [
      ["Status", (launch.status && launch.status.name) || "Unknown"],
      ["Launch pad", launch.pad && launch.pad.name ? launch.pad.name : "TBD"],
      ["Location", launch.pad && launch.pad.location ? launch.pad.location : "TBD"],
      ["Orbit", launch.mission && launch.mission.orbit ? launch.mission.orbit : "Not stated"]
    ];
    if (launch.probability !== null && launch.probability !== undefined && launch.probability >= 0) {
      facts.push(["Weather go", launch.probability + "%"]);
    }
    $("#heroFacts").innerHTML = facts.map(function (f) {
      return "<div><dt>" + esc(f[0]) + "</dt><dd>" + esc(f[1]) + "</dd></div>";
    }).join("");

    var watch = $("#heroWatch");
    if (launch.webcast && launch.webcast.url) {
      watch.hidden = false;
      watch.href = launch.webcast.url;
      watch.textContent = launch.webcast.live ? "Watch live now" : "Watch the stream";
    } else {
      watch.hidden = true;
    }

    tickHero();
  }

  function tickHero() {
    var launch = state.heroLaunch;
    var cd = $("#heroCountdown");
    if (!launch) return;
    var net = parseNet(launch.net);
    if (!net) {
      $$("[data-cd]", cd).forEach(function (n) { n.textContent = "--"; });
      return;
    }
    var c = countdownParts(net);
    cd.classList.toggle("is-past", c.past);
    var map = { d: c.d, h: pad(c.h), m: pad(c.m), s: pad(c.s) };
    $$("[data-cd]", cd).forEach(function (n) {
      var v = map[n.getAttribute("data-cd")];
      var next = String(v);
      if (n.textContent !== next) n.textContent = next;
    });
  }

  /* ── Render: KPIs ────────────────────────────────────────────────── */

  function renderKpis() {
    var s = state.data.stats || {};
    $("#kpiLaunches").textContent = s.upcoming_count != null ? s.upcoming_count : "—";
    $("#kpiCrew").textContent = s.humans_in_space != null ? s.humans_in_space : "—";
    $("#kpiCraft").textContent = s.spacecraft_in_space != null ? s.spacecraft_in_space : "—";
    $("#kpiOps").textContent = s.operators_count != null ? s.operators_count : "—";

    var confirmed = (state.data.upcoming || []).filter(isConfirmed).length;
    $("#kpiLaunchesFoot").textContent = confirmed + " with a confirmed T‑0";

    var crewed = (state.data.spacecraft || []).filter(function (c) {
      return c.type && /capsule|crew|spaceplane/i.test(c.type);
    }).length;
    $("#kpiCraftFoot").textContent = crewed + " crew-rated, " + Math.max(0, (s.spacecraft_in_space || 0) - crewed) + " cargo";
  }

  /* ── Render: launch list ─────────────────────────────────────────── */

  function filteredLaunches() {
    var q = state.query.trim().toLowerCase();
    return (state.data.upcoming || []).filter(function (l) {
      if (state.operator !== "all" && !(l.provider && String(l.provider.id) === state.operator)) return false;
      if (state.confirmation === "go" && !isConfirmed(l)) return false;
      if (state.confirmation === "tbd" && isConfirmed(l)) return false;
      if (!q) return true;
      var hay = [
        l.name, l.mission && l.mission.name, l.mission && l.mission.type,
        l.rocket && l.rocket.name, l.provider && l.provider.name, l.provider && l.provider.abbrev,
        l.pad && l.pad.name, l.pad && l.pad.location, l.mission && l.mission.orbit
      ].filter(Boolean).join(" ").toLowerCase();
      return hay.indexOf(q) !== -1;
    });
  }

  function launchCard(l) {
    var net = parseNet(l.net);
    var prec = precisionOf(l);
    var p = l.provider || {};

    var mon = net ? net.toLocaleDateString(undefined, { month: "short" }).toUpperCase() : "TBD";
    var day = net ? net.getDate() : "--";
    var time = net ? (prec.exact ? fmtTime(net) : net.getFullYear()) : "";

    var meta = [];
    if (l.rocket && l.rocket.name) meta.push("<span>" + esc(l.rocket.name) + "</span>");
    if (l.mission && l.mission.orbit_abbrev) meta.push("<span>" + esc(l.mission.orbit_abbrev) + "</span>");
    if (l.pad && l.pad.location) meta.push("<span>" + esc(l.pad.location) + "</span>");

    return '<button class="launch" type="button" data-launch="' + esc(l.id) + '">' +
      '<div class="l-date">' +
        '<span class="l-mon">' + esc(mon) + "</span>" +
        '<span class="l-day">' + esc(day) + "</span>" +
        '<span class="l-time">' + esc(time) + "</span>" +
      "</div>" +
      '<div class="l-main">' +
        '<div class="l-top">' +
          // No logo here on purpose: these are wordmarks, unreadable at 16px,
          // and the abbreviation beside them already identifies the operator.
          '<span class="l-op">' + esc(p.abbrev || p.name || "Unknown") + "</span>" +
          statusPill(l.status) +
        "</div>" +
        '<div class="l-name">' + esc(launchTitle(l)) + "</div>" +
        '<div class="l-meta">' + meta.join('<span aria-hidden="true">·</span>') + "</div>" +
      "</div>" +
      '<div class="l-right">' +
        '<span class="l-cd" data-net="' + esc(l.net || "") + '">—</span>' +
        '<span class="l-cd-note">' + (prec.exact ? "" : '<span class="pill pill-neutral"><span>Estimated</span></span>') + "</span>" +
      "</div>" +
    "</button>";
  }

  function renderLaunches() {
    var list = filteredLaunches();
    var host = $("#launchList");

    if (!list.length) {
      host.innerHTML = '<div class="empty"><p>No launches match those filters.</p></div>';
    } else {
      host.innerHTML = list.map(launchCard).join("");
    }

    var total = (state.data.upcoming || []).length;
    $("#resultCount").textContent = list.length === total
      ? "Showing all " + total + " scheduled launches"
      : "Showing " + list.length + " of " + total + " scheduled launches";

    tickList();
  }

  function renderChips() {
    var counts = {};
    (state.data.upcoming || []).forEach(function (l) {
      if (!l.provider || l.provider.id == null) return;
      var k = String(l.provider.id);
      if (!counts[k]) counts[k] = { n: 0, name: l.provider.abbrev || l.provider.name };
      counts[k].n++;
    });
    var keys = Object.keys(counts).sort(function (a, b) { return counts[b].n - counts[a].n; });

    var html = '<button class="chip' + (state.operator === "all" ? " is-on" : "") + '" data-op="all">All operators</button>';
    html += keys.slice(0, 8).map(function (k) {
      return '<button class="chip' + (state.operator === k ? " is-on" : "") + '" data-op="' + esc(k) + '">' +
        esc(counts[k].name) + '<span class="chip-n">' + counts[k].n + "</span></button>";
    }).join("");
    $("#opChips").innerHTML = html;
  }

  function renderRecent() {
    var list = (state.data.previous || []).slice(0, 8);
    var wrap = $("#recentWrap");
    if (!list.length) { wrap.hidden = true; return; }
    wrap.hidden = false;
    $("#recentList").innerHTML = list.map(function (l) {
      var net = parseNet(l.net);
      return '<button class="recent-item" type="button" data-launch="' + esc(l.id) + '">' +
        '<div class="recent-top">' +
          '<span class="recent-name">' + esc(launchTitle(l)) + "</span>" +
          statusPill(l.status) +
        "</div>" +
        '<div class="card-sub">' + esc((l.provider && (l.provider.abbrev || l.provider.name)) || "") +
          (l.rocket && l.rocket.name ? " · " + esc(l.rocket.name) : "") + "</div>" +
        '<div class="recent-when">' + (net ? esc(relativeTime(net)) : "") + "</div>" +
      "</button>";
    }).join("");
  }

  /* Ticks every second: the compact countdown on each visible card. */
  function tickList() {
    $$("[data-net]").forEach(function (el) {
      var iso = el.getAttribute("data-net");
      var net = parseNet(iso);
      if (!net) { el.textContent = "TBD"; return; }
      el.textContent = compactCountdown(net);
      var c = countdownParts(net);
      el.classList.toggle("is-soon", !c.past && c.d < 1);
      el.classList.toggle("is-past", c.past);
    });
  }

  /* ── Render: active missions ─────────────────────────────────────── */

  function initials(name) {
    return String(name || "?").split(/\s+/).slice(0, 2).map(function (w) { return w.charAt(0); }).join("").toUpperCase();
  }

  function renderMissions() {
    var crew = state.data.astronauts || [];
    $("#crewNum").textContent = crew.length;

    var agencies = {};
    crew.forEach(function (a) { if (a.agency) agencies[a.agency] = (agencies[a.agency] || 0) + 1; });
    var agencyText = Object.keys(agencies).sort(function (a, b) { return agencies[b] - agencies[a]; })
      .map(function (k) { return k + " ×" + agencies[k]; }).join(" · ");
    $("#crewNote").textContent = agencyText ? "Flying for " + agencyText + "." : "";

    $("#crewGrid").innerHTML = crew.map(function (a) {
      var flew = parseNet(a.in_space_since);
      return '<article class="crew">' +
        avatar(img(a.image), initials(a.name), "crew-photo") +
        '<div class="crew-name">' + esc(a.name) + "</div>" +
        '<div class="crew-agency">' + esc(a.agency || "") +
          (flew ? "<br>launched " + esc(fmtDate(flew, { month: "short", day: "numeric" })) : "") +
        "</div>" +
      "</article>";
    }).join("") || '<div class="empty"><p>Crew roster unavailable.</p></div>';

    $("#craftGrid").innerHTML = (state.data.spacecraft || []).map(function (c) {
      var media = img(c.image);
      var inSpace = humanDuration(c.time_in_space);
      var docked = humanDuration(c.time_docked);
      return '<article class="card">' +
        (media
          ? '<div class="card-media" style="background-image:url(\'' + esc(media) + '\')"></div>'
          : '<div class="card-media card-media-empty"></div>') +
        '<div class="card-body">' +
          '<div class="card-title">' + esc(c.name) + "</div>" +
          '<div class="card-sub">' + esc([c.agency, c.type].filter(Boolean).join(" · ")) + "</div>" +
          (c.description ? '<p class="card-text">' + esc(c.description) + "</p>" : "") +
          '<div class="facts">' +
            (inSpace ? '<div><div class="fact-k">In space</div><div class="fact-v">' + esc(inSpace) + "</div></div>" : "") +
            (docked ? '<div><div class="fact-k">Docked</div><div class="fact-v">' + esc(docked) + "</div></div>" : "") +
            (c.config ? '<div><div class="fact-k">Class</div><div class="fact-v">' + esc(c.config) + "</div></div>" : "") +
          "</div>" +
        "</div>" +
      "</article>";
    }).join("") || '<div class="empty"><p>No spacecraft currently reported in flight.</p></div>';

    $("#stationGrid").innerHTML = (state.data.stations || []).map(function (s) {
      var media = img(s.image);
      var since = s.founded ? new Date(s.founded) : null;
      var years = since ? Math.floor((Date.now() - since.getTime()) / 31557600000) : null;
      return '<article class="card">' +
        (media
          ? '<div class="card-media" style="background-image:url(\'' + esc(media) + '\')"></div>'
          : '<div class="card-media card-media-empty"></div>') +
        '<div class="card-body">' +
          '<div class="card-title">' + esc(s.name) + "</div>" +
          '<div class="card-sub">' + esc([s.orbit, (s.owners || []).join(", ")].filter(Boolean).join(" · ")) + "</div>" +
          (s.description ? '<p class="card-text">' + esc(String(s.description).slice(0, 260)) + (String(s.description).length > 260 ? "…" : "") + "</p>" : "") +
          '<div class="facts">' +
            (years !== null ? '<div><div class="fact-k">On orbit</div><div class="fact-v">' + years + " yrs</div></div>" : "") +
            (s.active_expeditions && s.active_expeditions.length
              ? '<div><div class="fact-k">Expedition</div><div class="fact-v">' + esc(s.active_expeditions.join(", ")) + "</div></div>"
              : "") +
            (s.status ? '<div><div class="fact-k">Status</div><div class="fact-v">' + esc(s.status) + "</div></div>" : "") +
          "</div>" +
        "</div>" +
      "</article>";
    }).join("") || '<div class="empty"><p>Station data unavailable.</p></div>';
  }

  /* ── Render: operators ───────────────────────────────────────────── */

  function renderOperators() {
    var ops = state.data.operators || [];
    $("#opGrid").innerHTML = ops.map(function (o) {
      var logo = img(o.logo);
      var rate = o.success_rate;
      var meterCls = "";
      if (rate !== null && rate !== undefined) {
        if (rate < 75) meterCls = " is-critical";
        else if (rate < 90) meterCls = " is-warning";
      }
      var next = (o.upcoming || []).slice(0, 3);

      return '<article class="card op">' +
        '<div class="op-head">' +
          avatar(logo, (o.abbrev || "?").slice(0, 4), "op-logo") +
          '<div class="op-id">' +
            '<div class="op-name">' + esc(o.name) + "</div>" +
            '<div class="op-where">' + esc([o.type, (o.country || []).join(", ")].filter(Boolean).join(" · ")) + "</div>" +
          "</div>" +
        "</div>" +

        (rate !== null && rate !== undefined
          ? '<div class="meter">' +
              '<div class="meter-top"><span class="meter-lab">All-time success rate</span>' +
              '<span class="meter-val">' + esc(rate) + "%</span></div>" +
              '<div class="meter-track"><div class="meter-fill' + meterCls + '" style="width:' + Math.max(0, Math.min(100, rate)) + '%"></div></div>' +
            "</div>"
          : "") +

        '<div class="op-stats">' +
          '<div class="op-stat"><div class="fact-k">Launches</div><div class="fact-v">' + esc(o.total_launch_count != null ? o.total_launch_count : "—") + "</div></div>" +
          '<div class="op-stat"><div class="fact-k">Failures</div><div class="fact-v">' + esc(o.failed_launches != null ? o.failed_launches : "—") + "</div></div>" +
          '<div class="op-stat"><div class="fact-k">Streak</div><div class="fact-v">' + esc(o.consecutive_successful_launches != null ? o.consecutive_successful_launches : "—") + "</div></div>" +
        "</div>" +

        (next.length
          ? '<div class="op-next">' +
              '<div class="op-next-lab">On the manifest</div>' +
              '<div class="op-next-list">' + next.map(function (u) {
                var d = parseNet(u.net);
                return '<div class="op-next-item"><span>' + esc(u.mission || u.name) + "</span>" +
                  "<time>" + esc(d ? fmtDate(d, { month: "short", day: "numeric" }) : "TBD") + "</time></div>";
              }).join("") + "</div>" +
              ((o.upcoming || []).length > 3 ? '<div class="op-where" style="margin-top:.5rem">+ ' + ((o.upcoming.length - 3)) + " more scheduled</div>" : "") +
            "</div>"
          : '<div class="op-next"><div class="op-next-lab">No launches currently on the manifest</div></div>') +
      "</article>";
    }).join("") || '<div class="empty"><p>Operator data unavailable.</p></div>';
  }

  /* ── Launch map ──────────────────────────────────────────────────────
     Plate carree, matching the pre-projected outlines baked into world.js:
     x = (lon + 180) / 360 * W, y = (90 - lat) / 180 * H. Everything drawn
     here uses the same two lines, so land, graticule, terminator and pad
     markers cannot drift out of register with each other. */

  var MAP_W = 2000, MAP_H = 1000;
  var SVG_NS = "http://www.w3.org/2000/svg";

  function projectX(lon) { return (lon + 180) / 360 * MAP_W; }
  function projectY(lat) { return (90 - lat) / 180 * MAP_H; }

  function svgEl(name, attrs) {
    var el = document.createElementNS(SVG_NS, name);
    for (var k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) el.setAttribute(k, attrs[k]);
    }
    return el;
  }

  var mapReady = false;

  function drawMapBase() {
    if (mapReady) return;
    var land = $("#mapLand");
    if (!land || !window.WORLD_PATH) return;
    land.setAttribute("d", window.WORLD_PATH);

    var g = $("#mapGraticule");
    var frag = document.createDocumentFragment();
    for (var lon = -150; lon <= 150; lon += 30) {
      var x = projectX(lon);
      frag.appendChild(svgEl("line", { x1: x, y1: 0, x2: x, y2: MAP_H }));
    }
    for (var lat = -60; lat <= 60; lat += 30) {
      var y = projectY(lat);
      var ln = svgEl("line", { x1: 0, y1: y, x2: MAP_W, y2: y });
      if (lat === 0) ln.setAttribute("class", "eq");
      frag.appendChild(ln);
    }
    g.appendChild(frag);
    mapReady = true;
  }

  /* ---- Solar geometry, for the day/night terminator ----
     Low-precision almanac formulae (good to ~0.01 deg, far beyond what a
     600px-wide map can show). Everything in degrees unless noted. */

  var RAD = Math.PI / 180;

  function solarPosition(date) {
    // Days since the J2000.0 epoch.
    var n = date.getTime() / 86400000.0 + 2440587.5 - 2451545.0;
    var L = (280.460 + 0.9856474 * n) % 360;          // mean longitude
    var g = ((357.528 + 0.9856003 * n) % 360) * RAD;  // mean anomaly
    var lambda = (L + 1.915 * Math.sin(g) + 0.020 * Math.sin(2 * g)) * RAD;
    var eps = (23.439 - 0.0000004 * n) * RAD;         // obliquity

    var dec = Math.asin(Math.sin(eps) * Math.sin(lambda)) / RAD;
    var ra = Math.atan2(Math.cos(eps) * Math.sin(lambda), Math.cos(lambda)) / RAD;

    var gmstHours = (18.697374558 + 24.06570982441908 * n) % 24;
    if (gmstHours < 0) gmstHours += 24;

    var lon = ra - gmstHours * 15;
    lon = ((lon + 540) % 360) - 180;  // normalise to [-180, 180)
    return { lat: dec, lon: lon };
  }

  function terminatorPath(sun) {
    // Where solar elevation is zero: tan(lat) = -cos(H) / tan(dec).
    var dec = sun.lat;
    // Within a few hours of an equinox tan(dec) approaches zero and the
    // terminator degenerates into the poles; clamp so the curve stays finite.
    if (Math.abs(dec) < 0.35) dec = dec >= 0 ? 0.35 : -0.35;
    var tanDec = Math.tan(dec * RAD);

    var pts = [];
    for (var lon = -180; lon <= 180; lon += 2) {
      var H = (lon - sun.lon) * RAD;
      var lat = Math.atan(-Math.cos(H) / tanDec) / RAD;
      pts.push([projectX(lon), projectY(lat)]);
    }

    var curve = "M" + pts.map(function (p) { return p[0].toFixed(1) + " " + p[1].toFixed(1); }).join("L");

    // The pole in the sun's opposite hemisphere is the one in darkness, so
    // close the shaded polygon along that edge of the map.
    var edgeY = dec > 0 ? projectY(-90) : projectY(90);
    var fill = curve +
      "L" + projectX(180).toFixed(1) + " " + edgeY +
      "L" + projectX(-180).toFixed(1) + " " + edgeY + "Z";

    return { fill: fill, line: curve };
  }

  function updateTerminator() {
    var night = $("#mapNight");
    if (!night) return;
    var sun = solarPosition(new Date());
    var term = terminatorPath(sun);
    night.setAttribute("d", term.fill);
    var line = $("#mapTermLine");
    if (line) line.setAttribute("d", term.line);

    var g = $("#mapSun");
    g.innerHTML = "";
    var x = projectX(sun.lon), y = projectY(sun.lat);
    g.appendChild(svgEl("circle", { cx: x, cy: y, r: 26, class: "map-sun-halo" }));
    g.appendChild(svgEl("circle", { cx: x, cy: y, r: 7, class: "map-sun" }));
  }

  /* ---- Site markers ---- */

  function siteRadius(count) {
    // sqrt so a site with 11 launches reads as bigger without swamping the map.
    return 6 + Math.sqrt(count) * 3;
  }

  /* Some launch sites are distinct upstream but effectively the same point at
     world scale -- Cape Canaveral SFS and Kennedy Space Center are 0.5 units
     apart on a 2000-unit canvas. Left alone they draw as two stacked dots and
     whichever lands underneath can never be clicked, silently hiding a whole
     site's manifest. Merge anything closer than CLUSTER_DIST into one marker
     that owns every member site. The nearest genuinely-distinct pair in the
     data (Andoya / Esrange) is 29 units apart, so the threshold sits in a wide
     gap rather than on a knife edge. */
  var CLUSTER_DIST = 18;

  function clusterSites(sites) {
    var clusters = [];

    sites.forEach(function (s) {
      var x = projectX(s.longitude), y = projectY(s.latitude);
      for (var i = 0; i < clusters.length; i++) {
        var c = clusters[i];
        if (Math.hypot(x - c.x, y - c.y) <= CLUSTER_DIST) {
          // Re-centre on the launch-weighted mean of the members.
          var total = c.count + s.count;
          c.x = (c.x * c.count + x * s.count) / total;
          c.y = (c.y * c.count + y * s.count) / total;
          c.count = total;
          c.sites.push(s);
          return;
        }
      }
      clusters.push({ x: x, y: y, count: s.count, sites: [s] });
    });

    clusters.forEach(function (c) {
      c.id = c.sites.map(function (s) { return s.name; }).join("|");
      c.label = c.sites.length === 1
        ? c.sites[0].name
        : c.sites.map(function (s) { return s.name.split(",")[0]; }).join(" & ");
      c.launches = [];
      c.sites.forEach(function (s) { c.launches = c.launches.concat(s.launches || []); });
      c.launches.sort(function (a, b) { return (a.net || "9999") < (b.net || "9999") ? -1 : 1; });
      c.next_net = c.launches.length ? c.launches[0].net : null;
      c.padCount = c.sites.reduce(function (n, s) { return n + (s.pads ? s.pads.length : 0); }, 0);
    });

    return clusters;
  }

  function renderMap() {
    drawMapBase();
    updateTerminator();

    var host = $("#mapSites");
    if (!host) return;
    var clusters = clusterSites(state.data.sites || []);
    state.clusters = clusters;

    // The marker flying the very next launch gets the pulse.
    var nextId = null, nextNet = null;
    clusters.forEach(function (c) {
      if (c.next_net && (!nextNet || c.next_net < nextNet)) { nextNet = c.next_net; nextId = c.id; }
    });

    host.innerHTML = "";
    var frag = document.createDocumentFragment();

    clusters.forEach(function (c) {
      var r = siteRadius(c.count);
      var isNext = c.id === nextId;

      var g = svgEl("g", {
        "class": "site" + (isNext ? " is-next" : "") + (state.site === c.id ? " is-selected" : "")
      });
      g.appendChild(svgEl("circle", { cx: c.x, cy: c.y, r: r * 3.4, fill: "url(#padGlow)", "class": "site-glow" }));
      if (isNext) g.appendChild(svgEl("circle", { cx: c.x, cy: c.y, r: r, "class": "site-pulse" }));
      g.appendChild(svgEl("circle", { cx: c.x, cy: c.y, r: r + 5, "class": "site-ring" }));
      g.appendChild(svgEl("circle", { cx: c.x, cy: c.y, r: r, "class": "site-dot" }));

      // Separate, generously sized transparent hit target: the visible dot is
      // only a few pixels across on a phone.
      var hit = svgEl("circle", {
        cx: c.x, cy: c.y, r: Math.max(r + 12, 26), "class": "site-hit",
        tabindex: "0", role: "button"
      });
      hit.setAttribute("aria-label", c.label + ", " + c.count + " scheduled " + (c.count === 1 ? "launch" : "launches"));
      hit.__cluster = c;
      g.appendChild(hit);

      frag.appendChild(g);
    });

    host.appendChild(frag);
    renderSitePanel();
  }

  function mapTipFor(cluster, target) {
    var tip = $("#mapTip");
    var next = cluster.launches && cluster.launches[0];
    var net = next ? parseNet(next.net) : null;
    tip.innerHTML =
      "<b>" + esc(cluster.label) + "</b>" +
      "<span>" + cluster.count + " scheduled " + (cluster.count === 1 ? "launch" : "launches") +
        (cluster.padCount ? " \u00b7 " + cluster.padCount + (cluster.padCount === 1 ? " pad" : " pads") : "") +
      "</span>" +
      (next ? "<em>" + esc(next.mission || next.name) + (net ? " \u00b7 " + esc(compactCountdown(net)) : "") + "</em>" : "");

    // Map SVG user units to container pixels via the live screen matrix, so the
    // tooltip tracks the marker at any responsive width.
    var svg = $("#map");
    var ctm = target.getScreenCTM();
    if (!ctm) return;
    var pt = svg.createSVGPoint();
    pt.x = parseFloat(target.getAttribute("cx"));
    pt.y = parseFloat(target.getAttribute("cy"));
    var screen = pt.matrixTransform(ctm);
    var box = $(".map-stage").getBoundingClientRect();
    tip.style.left = (screen.x - box.left) + "px";
    tip.style.top = (screen.y - box.top) + "px";
    tip.hidden = false;
  }

  function hideMapTip() {
    var tip = $("#mapTip");
    if (tip) tip.hidden = true;
  }

  function renderSitePanel() {
    var detail = $("#siteDetail"), empty = $("#siteEmpty");
    if (!detail) return;
    var clusters = state.clusters || [];
    var cluster = null;
    for (var i = 0; i < clusters.length; i++) if (clusters[i].id === state.site) cluster = clusters[i];

    if (!cluster) {
      detail.hidden = true;
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    detail.hidden = false;

    // A cluster covering more than one site lists each separately -- merging
    // them onto one dot is a drawing decision, not a claim that they are the
    // same place.
    detail.innerHTML = cluster.sites.map(function (site) {
      return '<div class="site-block">' +
        '<div class="site-head">' +
          '<span class="site-name">' + esc(site.name) + "</span>" +
          '<span class="site-meta">' + site.count + " scheduled \u00b7 " +
            esc(site.latitude.toFixed(3)) + ", " + esc(site.longitude.toFixed(3)) + "</span>" +
        "</div>" +
        (site.pads && site.pads.length
          ? '<div class="site-pads">Pads: ' + site.pads.map(esc).join(" \u00b7 ") + "</div>"
          : '<div class="site-pads">Pad not specified upstream</div>') +
        '<div class="site-launches">' +
          (site.launches || []).map(function (l) {
            return '<button class="recent-item" type="button" data-launch="' + esc(l.id) + '">' +
              '<div class="recent-top">' +
                '<span class="recent-name">' + esc(l.mission || l.name) + "</span>" +
                '<span class="l-cd" data-net="' + esc(l.net || "") + '">\u2014</span>' +
              "</div>" +
              '<div class="card-sub">' + esc([l.provider, l.rocket, l.pad].filter(Boolean).join(" \u00b7 ")) + "</div>" +
            "</button>";
          }).join("") +
        "</div>" +
      "</div>";
    }).join("");

    tickList();
  }

  /* ── Modal ───────────────────────────────────────────────────────── */

  var lastFocus = null;

  function findLaunch(id) {
    var all = (state.data.upcoming || []).concat(state.data.previous || []);
    for (var i = 0; i < all.length; i++) if (all[i].id === id) return all[i];
    return null;
  }

  function openModal(id) {
    var l = findLaunch(id);
    if (!l) return;
    lastFocus = document.activeElement;

    var net = parseNet(l.net);
    var prec = precisionOf(l);
    var media = img(l.image);
    var p = l.provider || {};

    var facts = [
      ["Launch window", net ? fmtDateTime(net) : "Not announced"],
      ["Schedule confidence", prec.label],
      ["Operator", p.name || "Unknown"],
      ["Rocket", (l.rocket && l.rocket.name) || "Unknown"],
      ["Launch pad", (l.pad && l.pad.name) || "TBD"],
      ["Location", (l.pad && l.pad.location) || "TBD"],
      ["Orbit", (l.mission && l.mission.orbit) || "Not stated"],
      ["Mission type", (l.mission && l.mission.type) || "Not stated"]
    ];
    if (l.probability !== null && l.probability !== undefined && l.probability >= 0) {
      facts.push(["Weather probability", l.probability + "% go"]);
    }
    if (l.rocket && l.rocket.total_launch_count != null) {
      facts.push(["This rocket has flown", l.rocket.total_launch_count + " times"]);
    }

    var html = "";
    if (media) html += '<div class="modal-hero" style="background-image:url(\'' + esc(media) + '\')"></div>';
    html += '<div class="modal-inner">' +
      '<div class="modal-kicker">' + statusPill(l.status) +
        (prec.exact ? "" : '<span class="pill pill-neutral"><span>Date estimated</span></span>') +
        (l.webcast && l.webcast.live ? '<span class="pill pill-live"><span>Streaming now</span></span>' : "") +
      "</div>" +
      '<h2 id="modalTitle">' + esc(launchTitle(l)) + "</h2>" +
      (l.name && l.name !== launchTitle(l) ? '<p class="card-sub">' + esc(l.name) + "</p>" : "");

    if (l.mission && l.mission.description) {
      html += '<div class="modal-sect"><h3>Mission</h3><p>' + esc(l.mission.description) + "</p></div>";
    }
    if (l.update) {
      html += '<div class="modal-sect"><h3>Latest update</h3><p>' + esc(l.update) + "</p></div>";
    }

    html += '<div class="modal-sect"><h3>Flight data</h3><div class="modal-facts">' +
      facts.map(function (f) {
        return '<div><div class="fact-k">' + esc(f[0]) + '</div><div class="fact-v">' + esc(f[1]) + "</div></div>";
      }).join("") + "</div></div>";

    if (l.weather_concerns) {
      html += '<div class="modal-sect"><h3>Weather concerns</h3><p>' + esc(l.weather_concerns) + "</p></div>";
    }
    if (l.failreason) {
      html += '<div class="modal-sect"><h3>Failure reason</h3><p>' + esc(l.failreason) + "</p></div>";
    }
    if (l.rocket && l.rocket.description) {
      html += '<div class="modal-sect"><h3>' + esc(l.rocket.name || "Launch vehicle") + "</h3><p>" + esc(l.rocket.description) + "</p></div>";
    }

    var actions = [];
    if (l.webcast && l.webcast.url) {
      actions.push('<a class="btn btn-primary" href="' + esc(l.webcast.url) + '" target="_blank" rel="noopener noreferrer">' +
        (l.webcast.live ? "Watch live" : "Watch the stream") + "</a>");
    }
    if (l.pad && l.pad.map_url) {
      actions.push('<a class="btn btn-ghost" href="' + esc(l.pad.map_url) + '" target="_blank" rel="noopener noreferrer">Pad on the map</a>');
    }
    if (l.rocket && l.rocket.wiki_url) {
      actions.push('<a class="btn btn-ghost" href="' + esc(l.rocket.wiki_url) + '" target="_blank" rel="noopener noreferrer">Rocket on Wikipedia</a>');
    }
    if (actions.length) html += '<div class="modal-actions">' + actions.join("") + "</div>";

    html += "</div>";

    $("#modalBody").innerHTML = html;
    $("#modal").hidden = false;
    document.body.style.overflow = "hidden";
    $(".modal-panel").focus();
  }

  function closeModal() {
    $("#modal").hidden = true;
    document.body.style.overflow = "";
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  /* ── View switching ──────────────────────────────────────────────── */

  function setView(name) {
    state.view = name;
    $$(".tab").forEach(function (t) {
      var on = t.getAttribute("data-view") === name;
      t.classList.toggle("is-active", on);
      if (on) t.setAttribute("aria-current", "page"); else t.removeAttribute("aria-current");
    });
    $$(".view").forEach(function (v) {
      v.hidden = v.id !== "view-" + name;
    });
    if (history.replaceState) history.replaceState(null, "", "#" + name);
  }

  /* ── Render orchestration ────────────────────────────────────────── */

  function render() {
    if (!state.data) return;
    $("#globalEmpty").hidden = true;
    renderHero();
    renderKpis();
    renderChips();
    renderLaunches();
    renderRecent();
    renderMap();
    renderMissions();
    renderOperators();
  }

  /* ── Wiring ──────────────────────────────────────────────────────── */

  function wire() {
    document.addEventListener("click", function (e) {
      var tab = e.target.closest(".tab");
      if (tab) { setView(tab.getAttribute("data-view")); return; }

      var chip = e.target.closest(".chip");
      if (chip) {
        state.operator = chip.getAttribute("data-op");
        renderChips(); renderLaunches();
        return;
      }

      var seg = e.target.closest(".seg-btn");
      if (seg) {
        state.confirmation = seg.getAttribute("data-conf");
        $$(".seg-btn").forEach(function (b) { b.classList.toggle("is-on", b === seg); });
        renderLaunches();
        return;
      }

      var hit = e.target.closest(".site-hit");
      if (hit && hit.__cluster) {
        state.site = state.site === hit.__cluster.id ? null : hit.__cluster.id;
        renderMap();
        return;
      }

      var card = e.target.closest("[data-launch]");
      if (card) { openModal(card.getAttribute("data-launch")); return; }

      if (e.target.closest("[data-close]")) { closeModal(); return; }

      if (e.target.closest("#heroDetail")) {
        if (state.heroLaunch) openModal(state.heroLaunch.id);
      }
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !$("#modal").hidden) closeModal();
    });

    // Pointer and keyboard both reach the map tooltip.
    var stage = $(".map-stage");
    if (stage) {
      stage.addEventListener("mouseover", function (e) {
        var hit = e.target.closest(".site-hit");
        if (hit && hit.__cluster) mapTipFor(hit.__cluster, hit);
      });
      stage.addEventListener("mouseout", function (e) {
        if (e.target.closest(".site-hit")) hideMapTip();
      });
      stage.addEventListener("focusin", function (e) {
        var hit = e.target.closest(".site-hit");
        if (hit && hit.__cluster) mapTipFor(hit.__cluster, hit);
      });
      stage.addEventListener("focusout", hideMapTip);
      stage.addEventListener("keydown", function (e) {
        var hit = e.target.closest(".site-hit");
        if (!hit || !hit.__cluster) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          state.site = state.site === hit.__cluster.id ? null : hit.__cluster.id;
          renderMap();
        }
      });
    }
    window.addEventListener("resize", hideMapTip);

    var searchTimer;
    $("#search").addEventListener("input", function (e) {
      clearTimeout(searchTimer);
      var v = e.target.value;
      searchTimer = setTimeout(function () {
        state.query = v;
        renderLaunches();
      }, 140);
    });

    // Broken upstream art shouldn't leave an empty box. Elements built by
    // avatar() carry a replacement tile; anything else just disappears.
    document.addEventListener("error", function (e) {
      var t = e.target;
      if (!t || t.tagName !== "IMG") return;
      var fb = t.getAttribute("data-fb");
      if (fb) {
        t.insertAdjacentHTML("afterend", fb);
        t.remove();
      } else {
        t.style.visibility = "hidden";
      }
    }, true);
  }

  /* ── Boot ────────────────────────────────────────────────────────── */

  function boot() {
    starfield();
    wire();

    var hash = (location.hash || "").replace("#", "");
    if (["launches", "map", "missions", "operators"].indexOf(hash) !== -1) setView(hash);

    $("#launchList").innerHTML = new Array(5).join("x").split("x")
      .map(function () { return '<div class="skeleton"></div>'; }).join("");

    load();
    setInterval(function () { if (!document.hidden) load(); }, POLL_MS);
    setInterval(function () { tickHero(); tickList(); }, 1000);
    setInterval(updateSync, 30000);
    setInterval(function () { if (!document.hidden) updateTerminator(); }, 60000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
