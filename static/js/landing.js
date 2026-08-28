/* DreamLens marketing landing — probability plane + optical lens */
(function () {
  "use strict";

  var reduce =
    window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function clamp(n, min, max) {
    return Math.min(max, Math.max(min, n));
  }

  function initPlane() {
    var hero = document.querySelector(".dl-landing-hero");
    var plane = document.querySelector("[data-landing-plane]");
    var yesEl = plane && plane.querySelector("[data-landing-yes]");
    var noEl = plane && plane.querySelector("[data-landing-no]");
    var split = document.querySelector("[data-landing-split]");
    var optic = document.querySelector("[data-landing-optic]");
    if (!hero || !yesEl || !noEl) return;

    var yes = 0.41;

    function paint(y) {
      var n = 1 - y;
      hero.style.setProperty("--yes", String(y));
      hero.style.setProperty("--no", String(n));
      yesEl.textContent = "$" + y.toFixed(2);
      noEl.textContent = "$" + n.toFixed(2);
      if (split) {
        split.style.left = y * 100 + "%";
      }
    }

    paint(yes);
    if (reduce) return;

    var t0 = performance.now();
    var ptrX = 0.62;
    var ptrY = 0.42;
    var targetX = ptrX;
    var targetY = ptrY;

    if (optic && window.matchMedia("(pointer: fine)").matches) {
      hero.addEventListener(
        "pointermove",
        function (e) {
          var rect = hero.getBoundingClientRect();
          targetX = clamp((e.clientX - rect.left) / rect.width, 0.2, 0.88);
          targetY = clamp((e.clientY - rect.top) / rect.height, 0.18, 0.72);
        },
        { passive: true }
      );
    }

    function frame(now) {
      var elapsed = (now - t0) / 1000;
      yes = clamp(
        0.41 + Math.sin(elapsed * 0.42) * 0.08 + Math.sin(elapsed * 0.95) * 0.03,
        0.28,
        0.58
      );
      paint(yes);

      if (optic) {
        var idleX = 0.58 + Math.sin(elapsed * 0.35) * 0.06;
        var idleY = 0.4 + Math.cos(elapsed * 0.28) * 0.05;
        ptrX += (targetX - ptrX) * 0.045;
        ptrY += (targetY - ptrY) * 0.045;
        var x = idleX * 0.55 + ptrX * 0.45;
        var y = idleY * 0.55 + ptrY * 0.45;
        optic.style.setProperty("--ox", x * 100 + "%");
        optic.style.setProperty("--oy", y * 100 + "%");
        var scale = 1 + Math.sin(elapsed * 0.6) * 0.035;
        optic.style.setProperty("--oscale", String(scale.toFixed(4)));
      }

      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function initReveal() {
    var nodes = document.querySelectorAll("[data-landing-reveal]");
    if (!nodes.length) return;
    nodes.forEach(function (el) {
      el.classList.add("dl-landing-reveal");
    });
    if (reduce || !("IntersectionObserver" in window)) {
      nodes.forEach(function (el) {
        el.classList.add("is-in");
      });
      return;
    }
    var io = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("is-in");
          io.unobserve(entry.target);
        });
      },
      { rootMargin: "0px 0px -10% 0px", threshold: 0.12 }
    );
    nodes.forEach(function (el) {
      io.observe(el);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initPlane();
    initReveal();
  });
})();
