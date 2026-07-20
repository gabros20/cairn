(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- Sticky header background ---------- */
  var header = document.getElementById("site-header");
  function updateHeader() {
    if (window.scrollY > 8) header.classList.add("is-scrolled");
    else header.classList.remove("is-scrolled");
  }
  updateHeader();
  window.addEventListener("scroll", updateHeader, { passive: true });

  /* ---------- Scroll reveal ---------- */
  var revealEls = Array.prototype.slice.call(document.querySelectorAll(".reveal"));
  if (reduceMotion || !("IntersectionObserver" in window)) {
    revealEls.forEach(function (el) { el.classList.add("is-visible"); });
  } else {
    var revealObserver = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-visible");
            revealObserver.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -60px 0px" }
    );
    revealEls.forEach(function (el) { revealObserver.observe(el); });
  }

  /* ---------- Trail gutter: markers + progress line ---------- */
  var markers = Array.prototype.slice.call(document.querySelectorAll(".trail-marker"));
  var sections = markers
    .map(function (m) { return document.getElementById(m.getAttribute("data-target")); })
    .filter(Boolean);

  markers.forEach(function (m) {
    m.addEventListener("click", function () {
      var target = document.getElementById(m.getAttribute("data-target"));
      if (target) {
        target.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
      }
    });
  });

  var lineFill = document.getElementById("trail-line-fill");
  var rail = document.getElementById("trail-rail");

  function updateTrail() {
    var doc = document.documentElement;
    var scrollTop = window.scrollY || doc.scrollTop;
    var scrollHeight = (doc.scrollHeight - window.innerHeight) || 1;
    var progress = Math.min(1, Math.max(0, scrollTop / scrollHeight));

    if (lineFill) {
      lineFill.style.strokeDasharray = "1 1";
      lineFill.style.strokeDashoffset = String(1 - progress);
    }
    if (rail) {
      rail.style.width = (progress * 100).toFixed(2) + "%";
    }

    var viewportMid = scrollTop + window.innerHeight * 0.4;
    var placedIndex = -1;
    sections.forEach(function (sec, i) {
      if (sec.offsetTop <= viewportMid) placedIndex = i;
    });
    markers.forEach(function (m, i) {
      m.classList.toggle("is-placed", i <= placedIndex);
    });
  }

  if (reduceMotion) {
    document.documentElement.classList.add("is-reduced-motion");
  }

  updateTrail();
  window.addEventListener("scroll", updateTrail, { passive: true });
  window.addEventListener("resize", updateTrail);

  /* ---------- Copy to clipboard ---------- */
  var copyButtons = Array.prototype.slice.call(document.querySelectorAll("[data-copy]"));
  copyButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy") || "";
      var done = function () {
        var original = btn.textContent;
        btn.textContent = "Copied";
        window.setTimeout(function () { btn.textContent = original; }, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, done);
      } else {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (e) { /* clipboard unsupported — silently no-op */ }
        document.body.removeChild(ta);
        done();
      }
    });
  });

  /* ---------- Pipelines tab switcher ---------- */
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".pipelines__tab"));
  var panels = Array.prototype.slice.call(document.querySelectorAll(".pipelines__panel"));
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      var targetId = tab.getAttribute("data-tab");
      tabs.forEach(function (t) {
        var active = t === tab;
        t.classList.toggle("is-active", active);
        t.setAttribute("aria-selected", active ? "true" : "false");
      });
      panels.forEach(function (p) {
        p.classList.toggle("is-active", p.id === targetId);
      });
    });
  });

  /* ---------- Hero animation graceful fallback ---------- */
  var video = document.getElementById("hero-video");
  var poster = document.getElementById("hero-poster");
  var fallback = document.getElementById("hero-fallback");

  function showPoster() {
    if (video) video.classList.add("is-hidden");
    if (poster) poster.classList.remove("is-hidden");
  }
  function showFallback() {
    if (poster) poster.classList.add("is-hidden");
    if (fallback) fallback.classList.remove("is-hidden");
  }

  if (video) {
    if (reduceMotion) {
      showPoster();
    } else {
      video.addEventListener("error", showPoster, true);
      video.addEventListener("stalled", function () {
        if (video.readyState === 0) showPoster();
      });
      var loadTimeout = window.setTimeout(function () {
        if (video.readyState === 0) showPoster();
      }, 4000);
      video.addEventListener("loadeddata", function () { window.clearTimeout(loadTimeout); });
      var playPromise = video.play();
      if (playPromise && playPromise.catch) playPromise.catch(function () { /* autoplay blocked — poster remains visible on error only */ });
    }
  }
  if (poster) {
    poster.addEventListener("error", showFallback);
  }

  /* ---------- Terminal panel overflow affordance ----------
     The artifact is the proof, so nothing may silently clip. Panels that
     genuinely need horizontal scroll (long sourced lines) get a visible
     right-edge fade + styled scrollbar instead of clipping quietly. */
  var panelBodies = Array.prototype.slice.call(document.querySelectorAll(".terminal-panel__body"));
  function updatePanel(el) {
    var scrollable = el.scrollWidth > el.clientWidth + 1;
    var atEnd = el.scrollLeft + el.clientWidth >= el.scrollWidth - 2;
    el.classList.toggle("is-scrollable", scrollable);
    var panel = el.closest(".terminal-panel");
    if (panel) panel.classList.toggle("has-fade", scrollable && !atEnd);
  }
  function updateScrollAffordance() { panelBodies.forEach(updatePanel); }
  panelBodies.forEach(function (el) {
    el.addEventListener("scroll", function () { updatePanel(el); }, { passive: true });
  });
  updateScrollAffordance();
  window.addEventListener("resize", updateScrollAffordance);
  window.addEventListener("load", updateScrollAffordance);
})();
