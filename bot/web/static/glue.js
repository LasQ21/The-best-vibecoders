/* Клей Mini App: тема Telegram, заголовок авторизації, навігація з кнопкою «Назад»,
   відкладене завантаження графіків plotly. Бізнес-логіки тут немає: HTML рендерить сервер. */
(function () {
  "use strict";
  var tg = window.Telegram && window.Telegram.WebApp;
  var inTelegram = !!(tg && tg.initData);
  var root = document.documentElement;
  var view = document.getElementById("view");
  var stack = [];
  var TABS = ["overview", "tenders", "buyers", "alerts"];
  var plotlyPromise = null;

  // --- тема ---------------------------------------------------------------
  function theme() {
    if (tg && tg.colorScheme) return tg.colorScheme;
    return window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function applyTheme() {
    root.dataset.theme = theme();
    if (tg && tg.themeParams) {
      try {
        tg.setHeaderColor(tg.themeParams.secondary_bg_color || "secondary_bg_color");
        tg.setBackgroundColor(tg.themeParams.secondary_bg_color || "secondary_bg_color");
        if (tg.setBottomBarColor) tg.setBottomBarColor(tg.themeParams.section_bg_color || tg.themeParams.bg_color);
      } catch (e) { /* старі клієнти */ }
    }
  }
  if (!inTelegram) root.classList.add("no-tg");
  applyTheme();
  if (tg) {
    tg.ready();
    tg.expand();
    try { if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); } catch (e) {}
    tg.onEvent("themeChanged", function () { applyTheme(); redrawCharts(); });
  } else if (window.matchMedia) {
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () { applyTheme(); redrawCharts(); });
  }

  function authHeader() { return "tma " + ((tg && tg.initData) || ""); }
  function haptic(kind) {
    if (!tg || !tg.HapticFeedback) return;
    try { kind === "select" ? tg.HapticFeedback.selectionChanged() : tg.HapticFeedback.impactOccurred(kind || "light"); } catch (e) {}
  }
  function toast(text) {
    var el = document.getElementById("toast");
    el.textContent = text; el.classList.add("show");
    clearTimeout(el._t); el._t = setTimeout(function () { el.classList.remove("show"); }, 2200);
  }
  window.radarToast = toast;

  // --- htmx: авторизація і помилки ------------------------------------------
  document.body.addEventListener("htmx:configRequest", function (e) {
    e.detail.headers["Authorization"] = authHeader();
  });
  document.body.addEventListener("htmx:responseError", function (e) {
    var xhr = e.detail.xhr;
    if (e.detail.target === view || (e.detail.target && view.contains(e.detail.target) && xhr.status >= 400)) {
      if (xhr.responseText && xhr.responseText.indexOf("error-box") >= 0) {
        e.detail.target.innerHTML = xhr.responseText;
      }
    }
  });
  document.body.addEventListener("htmx:sendError", function () { toast("Немає з'єднання. Спробуйте ще раз."); });
  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.detail.target === view) afterScreen();
    initCharts(e.detail.target);
  });

  // --- навігація ------------------------------------------------------------
  var skeleton = document.getElementById("skeleton-tpl").innerHTML;

  function load(path) {
    view.innerHTML = skeleton;
    window.scrollTo(0, 0);
    htmx.ajax("GET", "/app/v/" + path, { target: view, swap: "innerHTML" });
  }
  function nav(path, opts) {
    opts = opts || {};
    if (opts.root) stack = [];
    if (opts.replace && stack.length) stack.pop();
    stack.push(path);
    load(path);
  }
  function back() {
    if (stack.length > 1) { stack.pop(); load(stack[stack.length - 1]); haptic("light"); }
  }
  window.radarNav = nav;

  function afterScreen() {
    var screen = view.querySelector("[data-screen]");
    var tab = screen && screen.getAttribute("data-tab");
    document.querySelectorAll(".tabbar button").forEach(function (b) {
      b.classList.toggle("on", b.dataset.tab === tab);
    });
    if (screen && screen.dataset.title) document.title = screen.dataset.title + " · Радар тендерів";
    var canBack = stack.length > 1;
    if (tg && tg.BackButton) { canBack ? tg.BackButton.show() : tg.BackButton.hide(); }
    document.querySelectorAll(".back-fallback").forEach(function (b) { b.classList.toggle("show", canBack); });
    var ring = view.querySelector(".ring .value");
    if (ring) requestAnimationFrame(function () { ring.style.strokeDashoffset = ring.dataset.offset; });
  }
  if (tg && tg.BackButton) tg.BackButton.onClick(back);

  document.addEventListener("click", function (e) {
    var el = e.target.closest("[data-nav],[data-tab],[data-back],[data-link],[data-share],[data-fig-select]");
    if (!el) return;
    if (el.hasAttribute("data-back")) { e.preventDefault(); back(); return; }
    if (el.dataset.tab && el.closest(".tabbar")) {
      e.preventDefault(); haptic("select");
      if (stack.length === 1 && stack[0] === el.dataset.tab) { window.scrollTo({ top: 0, behavior: "smooth" }); return; }
      nav(el.dataset.tab, { root: true }); return;
    }
    if (el.dataset.nav) { e.preventDefault(); nav(el.dataset.nav); return; }
    if (el.dataset.link) {
      e.preventDefault();
      if (tg && tg.openLink) tg.openLink(el.dataset.link); else window.open(el.dataset.link, "_blank", "noopener");
      return;
    }
    if (el.dataset.share) {
      e.preventDefault();
      var url = "https://t.me/share/url?url=" + encodeURIComponent(el.dataset.share) +
        "&text=" + encodeURIComponent(el.dataset.shareText || "");
      if (tg && tg.openTelegramLink) tg.openTelegramLink(url); else window.open(url, "_blank", "noopener");
      return;
    }
    if (el.dataset.figSelect) {
      e.preventDefault(); haptic("select");
      var target = document.getElementById(el.dataset.figTarget);
      if (!target) return;
      el.parentElement.querySelectorAll("[data-fig-select]").forEach(function (b) { b.classList.remove("on", "selected"); });
      el.classList.add(el.classList.contains("item") ? "selected" : "on");
      target.dataset.src = el.dataset.figSelect;
      drawChart(target);
      if (el.classList.contains("item")) target.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  });
  document.addEventListener("change", function (e) {
    if (e.target.matches("input[type=checkbox], input[type=radio], select")) haptic("select");
  });
  // Живе значення повзунка поруч із ним.
  document.addEventListener("input", function (e) {
    if (e.target.type === "range" && e.target.dataset.out) {
      var out = document.getElementById(e.target.dataset.out);
      if (out) out.textContent = e.target.value;
    }
  });

  // --- графіки ----------------------------------------------------------------
  function ensurePlotly() {
    if (window.Plotly) return Promise.resolve(window.Plotly);
    if (!plotlyPromise) {
      plotlyPromise = new Promise(function (resolve, reject) {
        var s = document.createElement("script");
        s.src = "/vendor/plotly.min.js?v=" + (root.dataset.asset || "1");
        s.onload = function () { resolve(window.Plotly); };
        s.onerror = reject;
        document.head.appendChild(s);
      });
    }
    return plotlyPromise;
  }
  function drawChart(el) {
    var src = el.dataset.src;
    if (!src) return;
    el.classList.add("loading");
    var sep = src.indexOf("?") >= 0 ? "&" : "?";
    Promise.all([ensurePlotly(), fetch(src + sep + "theme=" + theme(), { headers: { Authorization: authHeader() } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })])
      .then(function (res) {
        el.classList.remove("loading");
        el._drawn = src;
        res[0].react(el, res[1].data, res[1].layout, { displayModeBar: false, responsive: true, locale: "uk" });
      })
      .catch(function () { el.classList.remove("loading"); el.innerHTML = '<p class="empty">Графік не завантажився</p>'; });
  }
  var observer = "IntersectionObserver" in window ? new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) { observer.unobserve(entry.target); drawChart(entry.target); }
    });
  }, { rootMargin: "200px" }) : null;
  function initCharts(scope) {
    (scope || document).querySelectorAll(".fig[data-src]").forEach(function (el) {
      if (el._watched) return;
      el._watched = true;
      observer ? observer.observe(el) : drawChart(el);
    });
  }
  function redrawCharts() {
    document.querySelectorAll(".fig[data-src]").forEach(function (el) { if (el._drawn) drawChart(el); });
  }

  // --- старт: екран з ?s=… (кнопка в сповіщенні) або огляд --------------------------
  var params = new URLSearchParams(location.search);
  var start = params.get("s") || (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param || "").replace(/^t_/, "tender/") || "";
  if (start && start.indexOf("tender/") === 0) {
    start = "tenders/" + start.slice(7);
  } else if (start && start.indexOf("buyer/") === 0) {
    start = "buyers/" + start.slice(6);
  }
  if (start && !/^[a-z]+(\/[\w-]+)?(\?[\w=&%.|-]*)?$/i.test(start)) start = "";
  if (start && TABS.indexOf(start.split(/[/?]/)[0]) < 0 && start.indexOf("about") !== 0) start = "";
  if (start && start.indexOf("/") > 0) {
    // Глибоке посилання: «Назад» веде на відповідний розділ.
    var section = start.split("/")[0];
    stack = [section];
  }
  nav(start || "overview");
})();
