/**
 * DreamLens frontend — wallet, Lens chat, trade modal, event lenses, countdowns, charts.
 */
(function () {
  "use strict";

  const DreamLens = {
    tradeState: {},
    chart: null,
  };

  /* ── CSRF helper ── */
  function getCsrfToken() {
    const match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
    if (match) return decodeURIComponent(match[1]);
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.getAttribute("content") || "";
    const input = document.querySelector("[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  function csrfFetch(url, options = {}) {
    const headers = Object.assign(
      { "X-CSRFToken": getCsrfToken(), "Content-Type": "application/json" },
      options.headers || {}
    );
    return fetch(url, Object.assign({ credentials: "same-origin" }, options, { headers }));
  }

  async function readJsonResponse(res) {
    const text = await res.text();
    if (!text) {
      if (!res.ok) throw new Error("Request failed (" + res.status + ").");
      return {};
    }
    try {
      return JSON.parse(text);
    } catch (err) {
      if (text.trim().charAt(0) === "<") {
        throw new Error(
          "Claim failed on the server (" + res.status + "). Reload and try again."
        );
      }
      throw new Error(text.slice(0, 180) || "Claim failed.");
    }
  }

  /* ── Wallet connect / disconnect ── */
  const WALLET_KEY = "dreamlens_wallet";
  const eip6963Providers = [];
  let eip6963Listening = false;

  function listenForWalletProviders() {
    if (eip6963Listening || typeof window === "undefined" || !window.addEventListener) return;
    eip6963Listening = true;
    window.addEventListener("eip6963:announceProvider", function (event) {
      const detail = event && event.detail;
      if (!detail || !detail.provider) return;
      const rdns = (detail.info && detail.info.rdns) || "";
      const exists = eip6963Providers.some(function (item) {
        return ((item.info && item.info.rdns) || "") === rdns;
      });
      if (!exists) eip6963Providers.push(detail);
    });
    window.dispatchEvent(new Event("eip6963:requestProvider"));
  }

  function pickMetaMaskProvider() {
    listenForWalletProviders();
    for (let i = 0; i < eip6963Providers.length; i += 1) {
      const rdns = String((eip6963Providers[i].info && eip6963Providers[i].info.rdns) || "");
      if (rdns === "io.metamask" || rdns === "io.metamask.flask" || rdns.indexOf("io.metamask") === 0) {
        return eip6963Providers[i].provider;
      }
    }
    const eth = window.ethereum;
    if (!eth) return null;
    if (eth.providers && eth.providers.length) {
      for (let j = 0; j < eth.providers.length; j += 1) {
        const candidate = eth.providers[j];
        if (candidate && candidate.isMetaMask && !candidate.isBraveWallet) return candidate;
      }
    }
    if (eth.isMetaMask) return eth;
    return eth;
  }

  function getEthereumProvider() {
    if (DreamLens.provider && DreamLens.provider.request) return DreamLens.provider;
    const picked = pickMetaMaskProvider();
    if (picked) DreamLens.provider = picked;
    return picked || window.ethereum || null;
  }

  function shortAddress(addr) {
    if (!addr || addr.length < 10) return addr;
    return addr.slice(0, 6) + "…" + addr.slice(-4);
  }

  function isEvmAddress(value) {
    return /^0x[a-fA-F0-9]{40}$/.test((value || "").trim());
  }

  function copyApiError(data, fallback) {
    if (!data) return fallback;
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail) && data.detail.length) return String(data.detail[0]);
    const field = data.wallet_address || data.trader_id;
    if (Array.isArray(field) && field.length) return String(field[0]);
    if (typeof field === "string") return field;
    return fallback;
  }

  function walletButton() {
    return document.getElementById("wallet-connect");
  }

  function walletLabel() {
    return document.getElementById("wallet-label");
  }

  function getConnectedAddress() {
    return DreamLens.walletAddress || null;
  }

  function isCorrectNetwork() {
    const target = networkConfig().chainId;
    return DreamLens.chainId != null && DreamLens.chainId === target;
  }

  function knownWrongNetwork() {
    // Only true when we have actually read chainId from the wallet.
    return DreamLens.chainId != null && !isCorrectNetwork();
  }

  function renderDisconnected() {
    DreamLens.walletAddress = null;
    DreamLens.chainId = null;
    const label = walletLabel();
    const btn = walletButton();
    if (label) label.textContent = "Connect";
    if (btn) {
      btn.classList.remove("is-connected", "is-wrong-network");
      btn.setAttribute("aria-label", "Connect wallet");
      btn.title = "Connect wallet";
    }
  }

  function clearWalletSession() {
    localStorage.removeItem(WALLET_KEY);
    renderDisconnected();
  }

  function setConnectedUI(address) {
    const normalized = String(address);
    DreamLens.walletAddress = normalized;
    localStorage.setItem(WALLET_KEY, normalized);
    const label = walletLabel();
    const btn = walletButton();
    const wrong = knownWrongNetwork();
    if (label) {
      label.textContent = wrong ? "Wrong network" : shortAddress(normalized);
    }
    if (btn) {
      btn.classList.add("is-connected");
      btn.classList.toggle("is-wrong-network", wrong);
      if (wrong) {
        const cfg = networkConfig();
        btn.setAttribute("aria-label", "Switch wallet to " + cfg.chainName);
        btn.title = "Switch to " + cfg.chainName + " (click). Alt+click to disconnect.";
      } else {
        btn.setAttribute("aria-label", "Disconnect wallet " + shortAddress(normalized));
        btn.title = "Click to disconnect";
      }
    }
  }

  function revealWalletGatedContent() {
    const gated = document.getElementById("wallet-gated-empty");
    const connectedEmpty = document.getElementById("wallet-connected-empty");
    if (!getConnectedAddress() || !gated) return;
    gated.hidden = true;
    if (connectedEmpty) connectedEmpty.hidden = false;
  }

  async function syncWalletSession(address, options) {
    if (!address) return false;
    const opts = options || {};
    try {
      const cfg = networkConfig();
      const res = await csrfFetch("/api/auth/wallet/", {
        method: "POST",
        body: JSON.stringify({
          address: address,
          chain_id: DreamLens.chainId || cfg.chainId,
        }),
      });
      if (!res.ok) {
        console.warn("Wallet session login failed", res.status);
        if (res.status === 403) {
          toast("Could not start a session — refresh the page, then connect again", "error");
        }
        return false;
      }
      revealWalletGatedContent();
      if (
        !opts.skipReload &&
        document.getElementById("wallet-gated-empty") &&
        !sessionStorage.getItem("dreamlens_authed_reload")
      ) {
        sessionStorage.setItem("dreamlens_authed_reload", "1");
        window.location.reload();
      } else if (!opts.skipReload) {
        sessionStorage.removeItem("dreamlens_authed_reload");
      }
      return true;
    } catch (err) {
      console.warn("Wallet session login failed", err);
      return false;
    }
  }

  async function clearWalletSessionRemote() {
    try {
      await csrfFetch("/api/auth/logout/", { method: "POST", body: "{}" });
    } catch (err) {
      console.warn("Wallet logout failed", err);
    }
  }

  async function disconnectWallet() {
    // Clear our session first. Skip wallet_revokePermissions — it opens a
    // confirmation popup and is unnecessary for in-app disconnect.
    await clearWalletSessionRemote();
    clearWalletSession();
    sessionStorage.removeItem("dreamlens_authed_reload");
    if (document.querySelector(".dl-following-page, .dl-portfolio-page")) {
      window.location.reload();
    }
  }

  function waitForEthereum(timeoutMs) {
    const limit = timeoutMs || 4000;
    listenForWalletProviders();
    const immediate = getEthereumProvider();
    if (immediate && immediate.request) {
      return Promise.resolve(immediate);
    }
    return new Promise(function (resolve) {
      var settled = false;
      function done(provider) {
        if (settled) return;
        settled = true;
        window.removeEventListener("ethereum#initialized", onInit);
        clearInterval(timer);
        resolve(provider || null);
      }
      function onInit() {
        done(getEthereumProvider());
      }
      window.addEventListener("ethereum#initialized", onInit, { once: true });
      var started = Date.now();
      var timer = setInterval(function () {
        const provider = getEthereumProvider();
        if (provider && provider.request) {
          done(provider);
        } else if (Date.now() - started >= limit) {
          done(null);
        }
      }, 50);
    });
  }

  function networkConfig() {
    const cfg = window.DreamLensConfig || {};
    return {
      chainId: Number(cfg.chainId || 50312),
      chainIdHex: cfg.chainIdHex || ("0x" + Number(cfg.chainId || 50312).toString(16)),
      chainName: cfg.chainName || "Somnia Shannon Testnet",
      rpcUrl: cfg.rpcUrl || "https://api.infra.testnet.somnia.network",
      explorerUrl: cfg.explorerUrl || "https://shannon-explorer.somnia.network",
      nativeCurrency: cfg.nativeCurrency || {
        name: "Somnia Test Token",
        symbol: "STT",
        decimals: 18,
      },
    };
  }

  async function getWalletChainId() {
    const eth = getEthereumProvider();
    if (!eth || !eth.request) throw new Error("No wallet found.");
    const hex = await eth.request({ method: "eth_chainId" });
    return parseInt(hex, 16);
  }

  async function ensureCorrectNetwork(options) {
    const opts = options || {};
    const eth = getEthereumProvider();
    if (!eth || !eth.request) return false;

    const cfg = networkConfig();
    const target = cfg.chainId;

    try {
      const current = await getWalletChainId();
      DreamLens.chainId = current;
      if (current === target) {
        return true;
      }

      try {
        await eth.request({
          method: "wallet_switchEthereumChain",
          params: [{ chainId: cfg.chainIdHex }],
        });
      } catch (switchError) {
        const code =
          switchError &&
          (switchError.code ||
            (switchError.data &&
              switchError.data.originalError &&
              switchError.data.originalError.code));
        const msg = String((switchError && switchError.message) || "").toLowerCase();
        if (
          code === 4902 ||
          code === -32603 ||
          msg.includes("unrecognized chain") ||
          msg.includes("not been added")
        ) {
          await eth.request({
            method: "wallet_addEthereumChain",
            params: [
              {
                chainId: cfg.chainIdHex,
                chainName: cfg.chainName,
                nativeCurrency: cfg.nativeCurrency,
                rpcUrls: [cfg.rpcUrl],
                blockExplorerUrls: [cfg.explorerUrl],
              },
            ],
          });
        } else {
          throw switchError;
        }
      }

      const after = await getWalletChainId();
      DreamLens.chainId = after;
      if (after !== target) {
        console.warn("Wallet is still on chain", after, "expected", target);
        return false;
      }
      return true;
    } catch (err) {
      console.warn("Failed to switch to DreamLens network", err);
      if (!opts.silent) {
        alert(
          "Please switch your wallet to " +
            cfg.chainName +
            " (chain ID " +
            cfg.chainId +
            ") to use DreamLens."
        );
      }
      return false;
    }
  }

  var walletListenersBound = false;

  function initWalletListeners(eth) {
    const provider = eth || getEthereumProvider() || window.ethereum;
    if (!provider || !provider.on || walletListenersBound) return;
    walletListenersBound = true;

    provider.on("accountsChanged", function (accounts) {
      if (!accounts || !accounts.length) {
        // Keep local session so navigation still shows the address.
        const saved = localStorage.getItem(WALLET_KEY);
        if (saved) setConnectedUI(saved);
        return;
      }
      if (localStorage.getItem(WALLET_KEY)) {
        setConnectedUI(accounts[0]);
      }
    });

    provider.on("chainChanged", function (chainIdHex) {
      DreamLens.chainId = parseInt(chainIdHex, 16);
      const addr = getConnectedAddress() || localStorage.getItem(WALLET_KEY);
      if (addr) setConnectedUI(addr);
    });
  }

  async function connectWallet(event) {
    const label = walletLabel();
    if (!label) return;

    // Already connected in our UI.
    if (getConnectedAddress()) {
      if (event && event.altKey) {
        await disconnectWallet();
        return;
      }
      // Only prompt a network switch when we know the chain is wrong.
      if (knownWrongNetwork()) {
        const eth = await waitForEthereum(2000);
        if (eth) initWalletListeners(eth);
        const ok = await ensureCorrectNetwork();
        if (ok && getConnectedAddress()) setConnectedUI(getConnectedAddress());
        return;
      }
      await disconnectWallet();
      return;
    }

    const eth = await waitForEthereum(2000);
    if (!eth) {
      label.textContent = "No wallet";
      return;
    }

    try {
      // Prompt only on explicit Connect click.
      const accounts = await eth.request({ method: "eth_requestAccounts" });
      if (accounts && accounts[0]) {
        DreamLens.provider = eth;
        setConnectedUI(accounts[0]);
        initWalletListeners(eth);
        try {
          DreamLens.chainId = await getWalletChainId();
        } catch (e) {
          /* ignore */
        }
        // Ask to switch network only during this user-initiated connect.
        await ensureCorrectNetwork();
        if (getConnectedAddress()) {
          setConnectedUI(getConnectedAddress());
          await syncWalletSession(getConnectedAddress());
        }
      } else {
        clearWalletSession();
      }
    } catch (err) {
      console.warn("Wallet connect declined", err);
      if (!localStorage.getItem(WALLET_KEY)) renderDisconnected();
    }
  }

  function restoreWallet() {
    // Pure localStorage restore — do NOT touch window.ethereum on navigation.
    // Any ethereum.request / provider.on here can open a wallet popup on some
    // extensions (MetaMask + multi-wallet injectors) on every full page load.
    const saved = localStorage.getItem(WALLET_KEY);
    if (!saved) {
      renderDisconnected();
      return;
    }
    setConnectedUI(saved);
    revealWalletGatedContent();
    syncWalletSession(saved);
  }


  /* ── Lens chat ── */
  const LENS_CHAT_STORAGE_KEY = "dreamlens_lens_thread";

  function loadLensThread() {
    try {
      const raw = sessionStorage.getItem(LENS_CHAT_STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      return [];
    }
  }

  function saveLensThread(thread) {
    try {
      sessionStorage.setItem(LENS_CHAT_STORAGE_KEY, JSON.stringify(thread.slice(-40)));
    } catch (e) {
      /* ignore quota */
    }
  }

  function cloneMarketReader(liveText) {
    const tpl = document.getElementById("market-reader-template");
    const fallback = liveText || "DreamLens is reading this market…";
    if (!tpl || !tpl.content || !tpl.content.firstElementChild) {
      const p = document.createElement("p");
      p.textContent = fallback;
      return p;
    }
    const node = tpl.content.firstElementChild.cloneNode(true);
    const live = node.querySelector(".dl-market-reader__live");
    if (live) live.textContent = fallback;
    return node;
  }

  function appendLensBubble(threadEl, role, text, extra) {
    const wrap = document.createElement("div");
    wrap.className =
      "dl-lens-bubble dl-lens-bubble--" + (role === "user" ? "user" : "assistant");
    if (extra && extra.pending) wrap.classList.add("is-pending");
    if (extra && extra.error) wrap.classList.add("is-error");
    if (extra && extra.pending) {
      wrap.appendChild(cloneMarketReader(text || "Looking at live markets and news…"));
    } else {
      const p = document.createElement("p");
      p.textContent = text || "";
      wrap.appendChild(p);
    }
    const events = extra && extra.events;
    if (events && events.length) {
      const links = document.createElement("div");
      links.className = "dl-lens-links";
      events.forEach(function (event) {
        if (!event || !event.id) return;
        const a = document.createElement("a");
        a.href = "/events/" + event.id + "/";
        a.className = "dl-btn dl-btn--ghost dl-btn--sm";
        a.textContent = "View " + (event.title || "event");
        links.appendChild(a);
      });
      if (links.childNodes.length) wrap.appendChild(links);
    }
    threadEl.appendChild(wrap);
    wrap.scrollIntoView({ block: "end", behavior: "smooth" });
    return wrap;
  }

  function renderLensThread(threadEl, emptyEl, thread) {
    threadEl.querySelectorAll(".dl-lens-bubble").forEach(function (node) {
      node.remove();
    });
    if (emptyEl) emptyEl.hidden = thread.length > 0;
    thread.forEach(function (item) {
      appendLensBubble(threadEl, item.role, item.content, { events: item.events });
    });
  }

  async function sendLensMessage(text, threadEl, emptyEl, input, submitBtn) {
    const message = (text || "").trim();
    if (!message) return;
    const thread = loadLensThread();
    thread.push({ role: "user", content: message });
    saveLensThread(thread);
    if (emptyEl) emptyEl.hidden = true;
    appendLensBubble(threadEl, "user", message);
    const pending = appendLensBubble(threadEl, "assistant", "Looking at live markets and news…", {
      pending: true,
    });
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = "Asking…";
    }
    const history = thread.slice(0, -1).map(function (item) {
      return { role: item.role, content: item.content };
    });
    const eventId = Number(new URLSearchParams(window.location.search).get("event") || 0) || null;
    try {
      const payload = { message: message, history: history };
      if (eventId) payload.event_id = eventId;
      const res = await csrfFetch("/api/ai/lens/", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        const detail =
          (data && (data.detail || data.message)) || "Lens could not answer.";
        throw new Error(typeof detail === "string" ? detail : "Lens could not answer.");
      }
      if (data.prepare_params || (data.tool_results && data.tool_results.prepare_params)) {
        throw new Error("Lens does not prepare trades.");
      }
      const reply = data.reply || "No reply from Lens.";
      const events = (data.tool_results && data.tool_results.events) || [];
      pending.remove();
      appendLensBubble(threadEl, "assistant", reply, { events: events });
      thread.push({ role: "assistant", content: reply, events: events });
      saveLensThread(thread);
    } catch (err) {
      pending.remove();
      const errText = err.message || "Ask failed. Try again.";
      appendLensBubble(threadEl, "assistant", errText, { error: true });
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = "Ask";
      }
    }
  }

  function initLensChat() {
    const threadEl = document.getElementById("lens-thread");
    if (!threadEl) return;
    const emptyEl = document.getElementById("lens-empty");
    const form = document.getElementById("lens-form");
    const input = document.getElementById("lens-input");
    const submitBtn = document.getElementById("lens-submit");
    renderLensThread(threadEl, emptyEl, loadLensThread());
    if (form && input) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        const text = input.value;
        input.value = "";
        sendLensMessage(text, threadEl, emptyEl, input, submitBtn);
      });
    }
    document.querySelectorAll("[data-lens-chip]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        const q = chip.getAttribute("data-lens-chip");
        sendLensMessage(q, threadEl, emptyEl, input, submitBtn);
      });
    });
  }

  /* ── Lens tab switching ── */
  const LENS_STORAGE_KEY = "dreamlens_last_lens";

  function activateLens(lens) {
    const tabs = document.querySelectorAll(".dl-lens-tab");
    if (!tabs.length) return;
    tabs.forEach(function (t) {
      const active = t.getAttribute("data-lens") === lens;
      t.classList.toggle("is-active", active);
      t.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll(".dl-lens-panel").forEach(function (panel) {
      const isTarget = panel.id === "lens-" + lens;
      panel.classList.toggle("is-active", isTarget);
      panel.hidden = !isTarget;
    });
    try {
      sessionStorage.setItem(LENS_STORAGE_KEY, lens);
    } catch (e) {
      /* ignore */
    }
  }

  function initLensTabs() {
    const tabs = document.querySelectorAll(".dl-lens-tab");
    if (!tabs.length) return;

    let saved = null;
    try {
      saved = sessionStorage.getItem(LENS_STORAGE_KEY);
    } catch (e) {
      saved = null;
    }
    if (saved && document.getElementById("lens-" + saved)) {
      activateLens(saved);
    }

    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        activateLens(tab.getAttribute("data-lens"));
      });
    });

    document.querySelectorAll("[data-switch-lens]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const lens = btn.getAttribute("data-switch-lens");
        if (lens) activateLens(lens);
      });
    });
  }

  /* ── Event Radar filters ── */
  function initRadarFilters() {
    const grid = document.getElementById("event-radar");
    const tiles = document.querySelectorAll("[data-radar]");
    const blurb = document.getElementById("radar-blurb");
    const clearBtn = document.getElementById("radar-clear");
    const empty = document.getElementById("market-empty");
    if (!tiles.length) return;

    function cards() {
      return document.querySelectorAll(".dl-event-card");
    }

    function parseIds(tile) {
      return (tile.getAttribute("data-event-ids") || "")
        .split(",")
        .map(function (s) {
          return s.trim();
        })
        .filter(Boolean);
    }

    function applyRadar(signalType, eventIds, blurbText) {
      const list = cards();
      const present = {};
      list.forEach(function (card) {
        present[String(card.getAttribute("data-event-id") || "")] = true;
      });
      const liveIds = (eventIds || []).filter(function (id) {
        return present[id];
      });
      const useIds = liveIds.length > 0;
      const needle = String(signalType || "").toUpperCase();

      let visibleCount = 0;
      list.forEach(function (card) {
        const id = String(card.getAttribute("data-event-id") || "");
        const signals = (card.getAttribute("data-signals") || "").toUpperCase();
        let show = true;
        if (needle) {
          const byId = useIds && liveIds.indexOf(id) !== -1;
          const bySignal = signals.indexOf(needle) !== -1;
          show = byId || bySignal;
        }
        card.hidden = !show;
        if (show) visibleCount += 1;
      });
      if (blurb) {
        if (blurbText) {
          blurb.hidden = false;
          blurb.textContent = blurbText;
        } else {
          blurb.hidden = true;
          blurb.textContent = "";
        }
      }
      if (clearBtn) clearBtn.hidden = !signalType;
      if (empty) empty.hidden = visibleCount > 0;
    }

    function clearRadar() {
      tiles.forEach(function (t) {
        t.classList.remove("is-active");
        t.setAttribute("aria-pressed", "false");
      });
      applyRadar(null, [], "");
    }

    function selectTile(tile) {
      if (tile.disabled) return;
      const wasActive =
        tile.classList.contains("is-active") ||
        tile.getAttribute("aria-pressed") === "true";
      tiles.forEach(function (t) {
        t.classList.remove("is-active");
        t.setAttribute("aria-pressed", "false");
      });
      if (wasActive) {
        applyRadar(null, [], "");
        return;
      }
      tile.classList.add("is-active");
      tile.setAttribute("aria-pressed", "true");
      applyRadar(
        tile.getAttribute("data-radar"),
        parseIds(tile),
        tile.getAttribute("data-blurb") || ""
      );
    }

    if (grid) {
      grid.addEventListener("click", function (e) {
        const tile = e.target.closest("[data-radar]");
        if (!tile || !grid.contains(tile)) return;
        selectTile(tile);
      });
    } else {
      tiles.forEach(function (tile) {
        tile.addEventListener("click", function () {
          selectTile(tile);
        });
      });
    }

    if (clearBtn) clearBtn.addEventListener("click", clearRadar);
    document.querySelectorAll("[data-radar-reset]").forEach(function (btn) {
      btn.addEventListener("click", clearRadar);
    });
  }

  /* ── Countdown timers ── */
  function formatCountdown(iso) {
    const end = new Date(iso).getTime();
    const now = Date.now();
    const diff = end - now;
    if (diff <= 0) return "";
    const mins = Math.floor(diff / 60000);
    const secs = Math.floor((diff % 60000) / 1000);
    if (mins >= 60) {
      const hrs = Math.floor(mins / 60);
      return hrs + "h " + (mins % 60) + "m";
    }
    return mins + "m " + secs + "s";
  }

  function formatWindowLine(iso, closedLabel) {
    const left = iso ? formatCountdown(iso) : "";
    return left ? "Ends in " + left : closedLabel || "Trading ended · settling";
  }

  function tickCountdowns() {
    document.querySelectorAll(".dl-window[data-expiry]").forEach(function (el) {
      const iso = el.getAttribute("data-expiry");
      if (!iso) return;
      const left = formatCountdown(iso);
      if (left) {
        const span = el.querySelector(".dl-countdown");
        if (span) span.textContent = left;
        else el.textContent = "Ends in " + left;
        el.classList.remove("is-closed");
      } else {
        el.textContent = el.getAttribute("data-closed-label") || "Trading ended · settling";
        el.classList.add("is-closed");
      }
    });
    document.querySelectorAll(".dl-countdown[data-expiry]").forEach(function (el) {
      if (el.closest(".dl-window")) return;
      const iso = el.getAttribute("data-expiry");
      if (iso) el.textContent = formatCountdown(iso) || "Trading ended · settling";
    });
  }

  function initMarketFilters() {
    const filters = document.querySelectorAll(".dl-filter");
    const markets = document.querySelectorAll(".dl-market");
    if (!filters.length || !markets.length) return;

    filters.forEach(function (btn) {
      btn.addEventListener("click", function () {
        const mode = btn.getAttribute("data-filter") || "all";
        filters.forEach(function (f) {
          f.classList.toggle("is-active", f === btn);
        });
        const soonLimit = Date.now() + 2 * 60 * 60 * 1000;
        markets.forEach(function (row) {
          const asset = (row.getAttribute("data-asset") || "").toUpperCase();
          const ends = new Date(row.getAttribute("data-ends-at") || 0).getTime();
          let show = true;
          if (mode === "BTC" || mode === "ETH") show = asset === mode;
          else if (mode === "soon") show = ends > Date.now() && ends <= soonLimit;
          row.hidden = !show;
        });
      });
    });
  }

  function initCountdowns() {
    tickCountdowns();
    setInterval(tickCountdowns, 1000);
  }

  /* ── Trade modal ── */
  function formatUsd(price) {
    const p = parseFloat(price);
    if (Number.isNaN(p)) return "—";
    return "$" + p.toFixed(2);
  }

  function formatCents(price) {
    return formatUsd(price);
  }

  function payoutParts(pay, price) {
    const stake = parseFloat(pay) || 0;
    const p = parseFloat(price);
    if (!p || p <= 0 || stake <= 0) {
      return { pay: stake, payout: 0, profit: 0, loss: stake };
    }
    const payout = stake / p;
    return { pay: stake, payout: payout, profit: payout - stake, loss: stake };
  }

  function calcPayout(amount, price) {
    return payoutParts(amount, price).payout.toFixed(2);
  }

  function renderTradeMath() {
    const state = DreamLens.tradeState || {};
    const amountInput = document.getElementById("modal-amount");
    const amount = parseFloat(amountInput && amountInput.value) || 5;
    const parts = payoutParts(amount, state.entryPrice);
    const setText = function (id, text) {
      const el = document.getElementById(id);
      if (el) el.textContent = text;
    };
    setText("modal-pay", formatUsd(parts.pay));
    setText("modal-payout", formatUsd(parts.payout));
    setText("modal-profit", formatUsd(parts.profit));
    setText("modal-loss", formatUsd(parts.loss));
    setText("review-risk", formatUsd(parts.loss));
    setText("review-payout", formatUsd(parts.payout));
    setText("review-event", state.eventTitle || "—");
    setText("review-expires", state.eventExpiry ? formatWindowLine(state.eventExpiry) : "—");
    setText("review-buying", state.outcome || "—");
    setText("review-needs", state.eventTitle || "—");
    setText("modal-needs", "What needs to happen? " + (state.eventTitle || "—"));
    const modalWindow = document.getElementById("modal-window");
    if (modalWindow) {
      if (state.eventExpiry) {
        modalWindow.setAttribute("data-expiry", state.eventExpiry);
        modalWindow.textContent = formatWindowLine(state.eventExpiry);
      } else {
        modalWindow.removeAttribute("data-expiry");
        modalWindow.textContent = "—";
      }
    } else {
      setText("modal-ends", state.eventExpiry ? formatWindowLine(state.eventExpiry) : "—");
    }
    const outcomeLine = document.getElementById("modal-outcome-line");
    if (outcomeLine) {
      outcomeLine.textContent = (state.outcome || "") + " " + formatCents(state.entryPrice);
    }
    const beginner = document.getElementById("modal-beginner-line");
    if (beginner) {
      beginner.textContent =
        (state.outcome || "").toUpperCase() === "NO"
          ? "NO = you think this does not happen. Price is what you pay now."
          : "YES = you think this happens. Price is what you pay now.";
    }
  }

  function showTradeStep(n) {
    DreamLens.tradeStep = n;
    document.querySelectorAll("[data-trade-step]").forEach(function (el) {
      const match = Number(el.getAttribute("data-trade-step")) === n;
      el.hidden = !match;
    });
    const title = document.getElementById("trade-modal-title");
    const nextBtn = document.getElementById("modal-next");
    const backBtn = document.getElementById("modal-back");
    const confirmBtn = document.getElementById("modal-confirm-trade");
    const state = DreamLens.tradeState || {};
    const agentReady = Boolean(window.DreamLensConfig && window.DreamLensConfig.agentCanTrade);
    if (title) {
      title.textContent = n === 1 ? "Buy " + (state.outcome || "") : n === 2 ? "Review trade" : "Trade Check";
    }
    if (nextBtn) {
      nextBtn.hidden = n === 3 || (n === 2 && agentReady);
      nextBtn.textContent = n === 1 ? "Review trade" : "Continue to Trade Check";
    }
    if (backBtn) backBtn.hidden = n === 1;
    if (confirmBtn) {
      confirmBtn.hidden = !(n === 3 || (n === 2 && agentReady));
      const amt = (document.getElementById("modal-amount") || {}).value || "5";
      const side = (DreamLens.tradeState || {}).outcome || "";
      if (agentReady) {
        confirmBtn.textContent = "Place $" + amt + " " + side;
      } else {
        confirmBtn.textContent = "Place trade";
      }
    }
    if (n === 3) {
      const funded = agentReady || Boolean(getConnectedAddress());
      const list = document.getElementById("trade-check-list");
      if (list) {
        list.innerHTML =
          "<li>✓ Event understood</li>" +
          "<li>✓ Event still active</li>" +
          "<li>✓ Amount within limit</li>" +
          "<li>✓ Maximum loss shown</li>" +
          "<li>" + (funded ? "✓" : "○") + " Trading account ready</li>";
      }
    }
  }

  function openTradeModal(opts) {
    const modal = document.getElementById("trade-modal");
    if (!modal) return;

    DreamLens.tradeState = {
      eventId: opts.eventId,
      outcome: opts.outcome,
      entryPrice: opts.price,
      eventTitle: opts.eventTitle,
      eventExpiry: opts.eventExpiry || "",
      copyExecutionId: opts.copyExecutionId || null,
    };

    const titleEl = document.getElementById("modal-event-title");
    if (titleEl) titleEl.textContent = opts.eventTitle || "—";
    const outcomeEl = document.getElementById("modal-outcome");
    if (outcomeEl) outcomeEl.textContent = opts.outcome || "—";
    const entryEl = document.getElementById("modal-entry");
    if (entryEl) entryEl.textContent = formatCents(opts.price);

    const amountInput = document.getElementById("modal-amount");
    if (amountInput) {
      amountInput.value = String(opts.amount != null ? opts.amount : 5);
      amountInput.readOnly = Boolean(opts.copyExecutionId);
    }
    document.querySelectorAll("[data-trade-amount]").forEach(function (chip) {
      chip.classList.toggle("is-active", chip.getAttribute("data-trade-amount") === String(amountInput && amountInput.value));
    });
    const understand = document.getElementById("trade-understand");
    if (understand) understand.checked = false;
    const details = document.getElementById("modal-tx-status");
    if (details) details.textContent = "Ready when you are.";

    renderTradeMath();
    showTradeStep(1);
    modal.classList.add("is-open");
    modal.setAttribute("aria-hidden", "false");
  }

  function closeTradeModal() {
    const modal = document.getElementById("trade-modal");
    if (modal) {
      modal.classList.remove("is-open");
      modal.setAttribute("aria-hidden", "true");
    }
  }

  function toHexQuantity(value) {
    if (value === undefined || value === null || value === "") return "0x0";
    if (typeof value === "string" && value.indexOf("0x") === 0) return value;
    try {
      return "0x" + BigInt(value).toString(16);
    } catch (err) {
      return "0x0";
    }
  }

  function toHexChainId(value) {
    if (value === undefined || value === null || value === "") {
      return networkConfig().chainIdHex;
    }
    if (typeof value === "string" && value.indexOf("0x") === 0) return value;
    return toHexQuantity(value);
  }

  function parseUnits(amount, decimals) {
    const s = String(amount == null ? "" : amount).trim();
    if (!s) throw new Error("Enter an amount greater than 0");
    if (s.startsWith("-")) throw new Error("Amount must be positive");
    const parts = s.split(".");
    const whole = parts[0] || "0";
    const frac = parts[1] || "";
    if (!/^\d+$/.test(whole) || (frac && !/^\d+$/.test(frac))) {
      throw new Error("Invalid amount");
    }
    if (parts.length > 2) throw new Error("Invalid amount");
    const fracPadded = (frac + "0".repeat(decimals)).slice(0, decimals);
    const wei = BigInt(whole) * (10n ** BigInt(decimals)) + BigInt(fracPadded || "0");
    if (wei <= 0n) throw new Error("Enter an amount greater than 0");
    return wei;
  }

  async function ensureWalletForTrade() {
    const eth = await waitForEthereum(2000);
    if (!eth) {
      throw new Error("No wallet found.");
    }
    DreamLens.provider = eth;
    initWalletListeners(eth);

    // Prefer the already-authorized MetaMask account. Calling eth_requestAccounts
    // here steals the Confirm click and shows a "sign in" popup instead of the tx.
    let accounts = [];
    try {
      accounts = await eth.request({ method: "eth_accounts" });
    } catch (err) {
      accounts = [];
    }
    let address = accounts && accounts[0];
    if (!address) {
      accounts = await eth.request({ method: "eth_requestAccounts" });
      address = accounts && accounts[0];
    }
    if (!address) {
      throw new Error("Connect MetaMask to trade.");
    }
    setConnectedUI(address);

    const onNetwork = await ensureCorrectNetwork({ silent: true });
    if (!onNetwork) {
      const switched = await ensureCorrectNetwork();
      if (!switched) {
        throw new Error("Switch to Somnia Shannon Testnet to trade.");
      }
    }
    const sessionOk = await syncWalletSession(address, { skipReload: true });
    if (!sessionOk) {
      throw new Error("Could not start a DreamLens session. Refresh, connect MetaMask, and try again.");
    }
    return { eth: eth, address: address };
  }

  function walletTxParams(tx, from) {
    if (!tx || !tx.to) {
      throw new Error("Missing unsigned transaction.");
    }
    // Omit chainId — MetaMask uses the selected network. Decimal/hex chainId
    // in eth_sendTransaction is a common reason the sign popup never appears.
    // Shannon feeHistory often reports 0 priority rewards; without explicit
    // EIP-1559 fees MetaMask shows "Network fee: Unavailable" and blocks Confirm.
    const params = {
      from: from,
      to: tx.to,
      data: tx.data || "0x",
      value: toHexQuantity(tx.value || 0),
      maxFeePerGas: toHexQuantity(tx.maxFeePerGas || "0x2cb417800"),
      maxPriorityFeePerGas: toHexQuantity(tx.maxPriorityFeePerGas || "0x3b9aca00"),
      type: "0x2",
    };
    if (tx.gas) {
      params.gas = toHexQuantity(tx.gas);
    }
    return params;
  }

  function bumpHexGas(hex, bps) {
    const n = BigInt(toHexQuantity(hex));
    const bumped = (n * BigInt(10000 + bps)) / 10000n;
    if (bumped <= 0n) return "0x30d40";
    return "0x" + bumped.toString(16);
  }

  async function waitForWalletReceipt(eth, txHash, timeoutMs) {
    const started = Date.now();
    while (Date.now() - started < (timeoutMs || 120000)) {
      const receipt = await eth.request({
        method: "eth_getTransactionReceipt",
        params: [txHash],
      });
      if (receipt) return receipt;
      await new Promise(function (resolve) {
        setTimeout(resolve, 1500);
      });
    }
    throw new Error("Timed out waiting for the wallet transaction.");
  }

  async function sendWalletTx(eth, tx, from, options) {
    const params = walletTxParams(tx, from);
    const fallbackGas = options && options.fallbackGas;
    // DreamDEX settlement writes use a fixed 10M gas ceiling (SDK never estimates).
    // eth_estimateGas often returns "execution reverted" until a prior step is
    // mined, or under-estimates finalize-if-needed inside redeem.
    if (fallbackGas) {
      params.gas = toHexQuantity(fallbackGas);
    } else {
      try {
        const estimated = await eth.request({
          method: "eth_estimateGas",
          params: [
            {
              from: params.from,
              to: params.to,
              data: params.data,
              value: params.value,
            },
          ],
        });
        params.gas = bumpHexGas(estimated, 2500);
      } catch (err) {
        if (params.gas) {
          // Keep backend-prepared gas (e.g. redeem metadata.gas).
        } else {
          const nested = err && err.data && (err.data.message || err.data.cause);
          const msg =
            nested ||
            (err && (err.message || err.reason)) ||
            "This transaction would fail on Somnia. Refresh and pick a still-open window.";
          throw new Error(msg);
        }
      }
    }
    try {
      return await eth.request({
        method: "eth_sendTransaction",
        params: [params],
      });
    } catch (err) {
      const code = err && err.code;
      if (code === 4001) {
        throw new Error("MetaMask rejected the transaction.");
      }
      const nested = err && err.data && (err.data.message || err.data.cause);
      const msg = nested || (err && (err.message || err.reason)) || "Wallet transaction failed";
      throw new Error(msg);
    }
  }

  async function prepareTrade() {
    const amount = parseFloat(document.getElementById("modal-amount").value) || 10;
    const state = DreamLens.tradeState;
    const details = document.getElementById("modal-tx-status");
    const address = getConnectedAddress();

    document.getElementById("modal-payout").textContent =
      formatUsd(calcPayout(amount, state.entryPrice));

    if (!address) {
      if (details) details.textContent = "Connect your wallet, then confirm to send the order on-chain.";
      return null;
    }

    try {
      await syncWalletSession(address, { skipReload: true });
      const walletAddress = getConnectedAddress() || address;
      let res;
      if (state.copyExecutionId) {
        res = await csrfFetch(
          "/api/copy/executions/" + state.copyExecutionId + "/prepare/",
          {
            method: "POST",
            body: JSON.stringify({ wallet_address: walletAddress }),
          }
        );
      } else {
        res = await csrfFetch("/api/trades/prepare/", {
          method: "POST",
          body: JSON.stringify({
            event_id: Number(state.eventId),
            outcome: state.outcome,
            amount: amount,
            wallet_address: walletAddress,
          }),
        });
      }
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || data.error || "Prepare failed");
      }
      DreamLens.tradeState.tradeId = data.trade_id || (data.trade && data.trade.id);
      DreamLens.tradeState.unsignedTx = data.unsigned_tx || null;
      DreamLens.tradeState.approvalTx = data.approval_tx || null;

      if (details) {
        details.textContent = data.approval_tx
          ? "Approve dollars, then place the trade."
          : "Order is ready. Confirm to place it.";
      }
      return data;
    } catch (err) {
      if (details) details.textContent = "Prepare failed: " + err.message;
      throw err;
    }
  }

  async function confirmTrade() {
    const btn = document.getElementById("modal-confirm-trade");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Preparing…";
    }

    try {
      const agentTrade = Boolean(window.DreamLensConfig && window.DreamLensConfig.agentCanTrade);
      if (agentTrade) {
        if (btn) btn.textContent = "DreamLens is placing this trade…";
        const amount = parseFloat((document.getElementById("modal-amount") || {}).value) || 1;
        const res = await csrfFetch("/api/agent/trade/", {
          method: "POST",
          body: JSON.stringify({
            event_id: Number(DreamLens.tradeState.eventId),
            outcome: DreamLens.tradeState.outcome,
            amount: amount,
          }),
        });
        const data = await readJsonResponse(res);
        if (!res.ok) {
          throw new Error(data.detail || data.error || "DreamLens could not place this trade.");
        }
        toast("Trade placed in your trading account.", "ok");
        closeTradeModal();
        if (document.body.getAttribute("data-onboarding") === "1") {
          window.location.href = "/start/";
        } else {
          window.location.reload();
        }
        return;
      }

      const wallet = await ensureWalletForTrade();
      if (btn) btn.textContent = "Preparing…";
      const prepared = await prepareTrade();
      const unsigned = prepared && prepared.unsigned_tx;
      if (!unsigned) {
        throw new Error("Could not build this order. Finish setup at Start trading, or try again.");
      }

      if (prepared.approval_tx) {
        if (btn) btn.textContent = "Approve dollars…";
        const approveHash = await sendWalletTx(wallet.eth, prepared.approval_tx, wallet.address);
        if (btn) btn.textContent = "Waiting for approval…";
        const approveReceipt = await waitForWalletReceipt(wallet.eth, approveHash, 120000);
        const approveOk =
          approveReceipt.status === "0x1" ||
          approveReceipt.status === 1 ||
          approveReceipt.status === "1";
        if (!approveOk) {
          throw new Error("Approval did not go through. Check your wallet and try again.");
        }
      }

      if (btn) btn.textContent = "Sign order…";
      const txHash = await sendWalletTx(wallet.eth, unsigned, wallet.address);
      if (!txHash) {
        throw new Error("Wallet did not return a transaction hash.");
      }

      if (btn) btn.textContent = "Recording…";
      const res = await csrfFetch("/api/trades/confirm/", {
        method: "POST",
        body: JSON.stringify({
          trade_id: DreamLens.tradeState.tradeId,
          tx_hash: txHash,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || data.error || "Confirm failed");
      }

      toast("Trade submitted.", "ok");
      closeTradeModal();
      window.location.reload();
    } catch (err) {
      console.warn("On-chain trade failed", err);
      toast(err.message || "Trade failed. Check your wallet and try again.", "error");
    } finally {
      if (btn) {
        btn.disabled = false;
        const amt = (document.getElementById("modal-amount") || {}).value || "5";
        const side = (DreamLens.tradeState || {}).outcome || "";
        if (window.DreamLensConfig && window.DreamLensConfig.agentCanTrade) {
          btn.textContent = "Place $" + amt + " " + side;
        } else {
          btn.textContent = "Place trade";
        }
      }
    }
  }

  function initTradeModal() {
    document.querySelectorAll(".dl-trade-trigger, [data-trade]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        openTradeModal({
          eventId: btn.getAttribute("data-event-id"),
          outcome: btn.getAttribute("data-outcome"),
          price: btn.getAttribute("data-price"),
          eventTitle: btn.getAttribute("data-event-title"),
          eventExpiry: btn.getAttribute("data-event-expiry"),
          amount: btn.getAttribute("data-amount"),
        });
      });
    });

    document.querySelectorAll("[data-modal-close]").forEach(function (el) {
      el.addEventListener("click", closeTradeModal);
    });

    document.querySelectorAll("[data-trade-amount]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        document.querySelectorAll("[data-trade-amount]").forEach(function (c) {
          c.classList.remove("is-active");
        });
        chip.classList.add("is-active");
        const amountInput = document.getElementById("modal-amount");
        if (amountInput) amountInput.value = chip.getAttribute("data-trade-amount");
        renderTradeMath();
      });
    });

    const amountInput = document.getElementById("modal-amount");
    if (amountInput) {
      amountInput.addEventListener("input", renderTradeMath);
      amountInput.addEventListener("change", function () {
        renderTradeMath();
      });
    }

    const nextBtn = document.getElementById("modal-next");
    if (nextBtn) {
      nextBtn.addEventListener("click", function () {
        const step = DreamLens.tradeStep || 1;
        if (step === 1) {
          showTradeStep(2);
          return;
        }
        const understand = document.getElementById("trade-understand");
        if (understand && !understand.checked) {
          toast("Confirm that you understand you can lose the amount you paid.", "error");
          return;
        }
        if (window.DreamLensConfig && window.DreamLensConfig.agentCanTrade) {
          return;
        }
        showTradeStep(3);
      });
    }
    const backBtn = document.getElementById("modal-back");
    if (backBtn) {
      backBtn.addEventListener("click", function () {
        showTradeStep(Math.max(1, (DreamLens.tradeStep || 2) - 1));
      });
    }

    const confirmBtn = document.getElementById("modal-confirm-trade");
    if (confirmBtn) confirmBtn.addEventListener("click", confirmTrade);
  }

  /* ── Copy wizard (event lens) ── */
  function initCopyForm() {
    const form = document.getElementById("copy-form");
    if (!form) return;

    let step = 1;
    let selectedTraderId = null;

    function showStep(n) {
      step = n;
      form.querySelectorAll("[data-wizard-step]").forEach(function (panel) {
        const match = Number(panel.getAttribute("data-wizard-step")) === n;
        panel.hidden = !match;
        panel.classList.toggle("is-active", match);
      });
      form.querySelectorAll("[data-step-indicator]").forEach(function (dot) {
        const active = Number(dot.getAttribute("data-step-indicator")) <= n;
        dot.classList.toggle("is-active", active);
      });
    }

    form.querySelectorAll(".dl-preset[data-amount]").forEach(function (preset) {
      preset.addEventListener("click", function () {
        form.querySelectorAll(".dl-preset[data-amount]").forEach(function (p) {
          p.classList.remove("is-active");
        });
        preset.classList.add("is-active");
        const hidden = document.getElementById("copy-amount");
        if (hidden) hidden.value = preset.getAttribute("data-amount");
      });
    });

    form.querySelectorAll("[data-wizard-next]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        showStep(Math.min(3, step + 1));
      });
    });
    form.querySelectorAll("[data-wizard-back]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        showStep(Math.max(1, step - 1));
      });
    });

    document.querySelectorAll("[data-copy-trader]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const idAttr = btn.getAttribute("data-copy-trader");
        if (idAttr && /^\d+$/.test(idAttr)) {
          selectedTraderId = Number(idAttr);
        } else {
          const row = btn.closest(".dl-trader-row, .dl-trader-card");
          const follow = row && row.querySelector("[data-follow-trader]");
          if (follow && follow.getAttribute("data-follow-trader")) {
            selectedTraderId = Number(follow.getAttribute("data-follow-trader"));
          }
        }
        const lens = btn.getAttribute("data-switch-lens");
        if (lens) activateLens(lens);
      });
    });

    const showWizardBtn = document.getElementById("show-event-copy-wizard");
    if (showWizardBtn) {
      showWizardBtn.addEventListener("click", function () {
        form.hidden = false;
        showWizardBtn.hidden = true;
        const banner = document.getElementById("smart-copy-on-banner");
        if (banner) banner.hidden = true;
      });
    }

    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!getConnectedAddress()) {
        await connectWallet();
        return;
      }
      const modeEl = form.querySelector('input[name="copy_mode"]:checked');
      const amountEl = document.getElementById("copy-amount");
      const dailyEl = document.getElementById("copy-daily-limit");
      const perTradeEl = document.getElementById("copy-per-trade-limit");
      const followBtn = document.querySelector(
        ".dl-trader-row [data-follow-trader], .dl-trader-card [data-follow-trader]"
      );
      let traderId = selectedTraderId;
      if (!traderId && followBtn && followBtn.getAttribute("data-follow-trader")) {
        traderId = Number(followBtn.getAttribute("data-follow-trader"));
      }

      const rawMode = modeEl ? modeEl.value : "SMART";
      let copyMode = "SMART";
      let autoExecute = false;
      if (rawMode === "SMART_AUTO") {
        copyMode = "SMART";
        autoExecute = true;
      } else if (rawMode === "CONSENSUS") {
        copyMode = "CONSENSUS";
      }

      const payload = {
        copy_mode: copyMode,
        max_daily: dailyEl ? Number(dailyEl.value) || 50 : 50,
        max_per_trade: perTradeEl
          ? Number(perTradeEl.value) || Number(amountEl && amountEl.value) || 10
          : Number(amountEl && amountEl.value) || 10,
        auto_execute: autoExecute,
        status: "ACTIVE",
      };

      if (traderId) {
        payload.trader_id = Number(traderId);
      } else {
        alert("Pick a trader in the Traders lens, then start copying.");
        activateLens("traders");
        return;
      }

      const submitBtn = form.querySelector('button[type="submit"]');
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "Starting…";
      }
      try {
        await syncWalletSession(getConnectedAddress());
        const res = await csrfFetch("/api/copy/", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        const data = await res.json().catch(function () {
          return {};
        });
        if (!res.ok) {
          throw new Error(data.detail || data.error || "Could not start copy.");
        }
        alert("Smart Copy started — DreamLens will review trades before copying.");
        window.location.href = "/following/";
      } catch (err) {
        alert(err.message || "Could not start copy. Try again.");
      } finally {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = "Start Smart Copy";
        }
      }
    });
  }

  function initFollowButtons() {
    document.querySelectorAll("[data-follow-trader]").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        if (!getConnectedAddress()) {
          await connectWallet();
          return;
        }
        btn.disabled = true;
        try {
          await syncWalletSession(getConnectedAddress());
          const res = await csrfFetch("/api/copy/", {
            method: "POST",
            body: JSON.stringify({
              trader_id: Number(btn.getAttribute("data-follow-trader")),
              copy_mode: "SMART",
              auto_execute: false,
              status: "ACTIVE",
            }),
          });
          if (res.ok) {
            window.location.reload();
            return;
          }
          alert("Could not follow this trader. Try connecting your wallet again.");
        } catch (err) {
          alert("Could not follow this trader. Please try again.");
        } finally {
          btn.disabled = false;
        }
      });
    });
  }

  function initFollowWalletForm() {
    const form = document.getElementById("follow-wallet-form");
    if (!form) return;
    const input = document.getElementById("follow-wallet-address");
    const followBtn = document.getElementById("follow-wallet-follow");
    const smartBtn = document.getElementById("follow-wallet-smart");
    const errorEl = document.getElementById("follow-wallet-error");

    function showError(msg) {
      if (!errorEl) return;
      if (!msg) {
        errorEl.hidden = true;
        errorEl.textContent = "";
        return;
      }
      errorEl.hidden = false;
      errorEl.textContent = msg;
    }

    function readAddress() {
      return ((input && input.value) || "").trim();
    }

    async function followAddress() {
      const address = readAddress();
      if (!isEvmAddress(address)) {
        showError("Paste a valid 0x wallet address.");
        if (input) input.focus();
        return;
      }
      showError("");
      if (!getConnectedAddress()) {
        await connectWallet();
        return;
      }
      if (followBtn) followBtn.disabled = true;
      try {
        await syncWalletSession(getConnectedAddress());
        const res = await csrfFetch("/api/copy/", {
          method: "POST",
          body: JSON.stringify({
            wallet_address: address,
            copy_mode: "SMART",
            auto_execute: false,
            status: "ACTIVE",
          }),
        });
        const data = await res.json().catch(function () {
          return {};
        });
        if (!res.ok) {
          throw new Error(copyApiError(data, "Could not follow this wallet."));
        }
        window.location.reload();
      } catch (err) {
        showError(err.message || "Could not follow this wallet.");
      } finally {
        if (followBtn) followBtn.disabled = false;
      }
    }

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      followAddress();
    });
    if (smartBtn) {
      smartBtn.addEventListener("click", function () {
        const address = readAddress();
        if (!isEvmAddress(address)) {
          showError("Paste a valid 0x wallet address.");
          if (input) input.focus();
          return;
        }
        showError("");
        if (typeof DreamLens.openSmartCopyWizard === "function") {
          DreamLens.openSmartCopyWizard("", shortAddress(address), address);
        }
      });
    }
  }

  /* ── Smart Copy trader wizard ── */
  function initSmartCopyWizard() {
    const modal = document.getElementById("smart-copy-wizard");
    const form = document.getElementById("smart-copy-wizard-form");
    if (!modal || !form) return;

    let step = 1;

    function showStep(n) {
      step = n;
      form.querySelectorAll("[data-scw-step]").forEach(function (panel) {
        const match = Number(panel.getAttribute("data-scw-step")) === n;
        panel.hidden = !match;
        panel.classList.toggle("is-active", match);
      });
      form.querySelectorAll("[data-scw-step-indicator]").forEach(function (dot) {
        dot.classList.toggle(
          "is-active",
          Number(dot.getAttribute("data-scw-step-indicator")) <= n
        );
      });
    }

    function openWizard(traderId, traderName, walletAddress) {
      const idInput = document.getElementById("scw-trader-id");
      const walletInput = document.getElementById("scw-wallet-address");
      if (idInput) idInput.value = traderId || "";
      if (walletInput) walletInput.value = walletAddress || "";
      const title = document.getElementById("smart-copy-wizard-title");
      if (title) {
        title.textContent = traderName ? "Smart Copy · " + traderName : "Start Smart Copy";
      }
      showStep(1);
      modal.classList.add("is-open");
      modal.setAttribute("aria-hidden", "false");
    }
    DreamLens.openSmartCopyWizard = openWizard;

    function closeWizard() {
      modal.classList.remove("is-open");
      modal.setAttribute("aria-hidden", "true");
    }

    document.querySelectorAll("[data-open-smart-copy]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        openWizard(
          btn.getAttribute("data-trader-id"),
          btn.getAttribute("data-trader-name")
        );
      });
    });
    modal.querySelectorAll("[data-smart-wizard-close]").forEach(function (el) {
      el.addEventListener("click", closeWizard);
    });

    form.querySelectorAll("[data-scw-amount]").forEach(function (preset) {
      preset.addEventListener("click", function () {
        form.querySelectorAll("[data-scw-amount]").forEach(function (p) {
          p.classList.remove("is-active");
        });
        preset.classList.add("is-active");
        const val = preset.getAttribute("data-scw-amount");
        const amount = document.getElementById("scw-amount");
        const maxTrade = document.getElementById("scw-max-trade");
        if (amount) amount.value = val;
        if (maxTrade) maxTrade.value = val;
      });
    });

    form.querySelectorAll("[data-scw-next]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        showStep(Math.min(3, step + 1));
      });
    });
    form.querySelectorAll("[data-scw-back]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        showStep(Math.max(1, step - 1));
      });
    });

    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!getConnectedAddress()) {
        await connectWallet();
        return;
      }
      const traderId = document.getElementById("scw-trader-id").value;
      const walletAddress = (
        (document.getElementById("scw-wallet-address") || {}).value || ""
      ).trim();
      if (!traderId && !isEvmAddress(walletAddress)) {
        alert("Paste a wallet address or pick a trader.");
        return;
      }

      const consider = {
        trader_confidence: true,
        historical_performance: true,
        liquidity: true,
        market_movement: true,
        consensus: true,
        copy_every: false,
      };
      form.querySelectorAll('input[name="consider"]').forEach(function (cb) {
        consider[cb.value] = cb.checked;
      });

      const actionEl = form.querySelector('input[name="scw_action"]:checked');
      let copyMode = "SMART";
      let autoExecute = actionEl ? actionEl.value === "auto" : false;

      const minWrEl = document.getElementById("scw-min-wr");
      const minConsEl = document.getElementById("scw-min-cons");
      const minWr = Number(minWrEl && minWrEl.value) || 65;
      const minCons = Number(minConsEl && minConsEl.value) || 60;

      const payload = {
        copy_mode: copyMode,
        auto_execute: autoExecute,
        status: "ACTIVE",
        max_per_trade:
          Number(document.getElementById("scw-max-trade").value) ||
          Number(document.getElementById("scw-amount").value) ||
          10,
        max_daily: Number(document.getElementById("scw-max-daily").value) || 50,
        min_copy_score: Number(document.getElementById("scw-min-score").value) || 70,
        min_win_rate: minWr / 100,
        min_completed_events:
          Number(document.getElementById("scw-min-events").value) || 30,
        min_liquidity: Number(document.getElementById("scw-min-liq").value) || 1000,
        min_consensus: minCons / 100,
        consider_json: consider,
      };
      if (traderId) payload.trader_id = Number(traderId);
      if (!traderId && walletAddress) payload.wallet_address = walletAddress;

      const submitBtn = form.querySelector('button[type="submit"]');
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "Starting…";
      }
      try {
        await syncWalletSession(getConnectedAddress());
        const res = await csrfFetch("/api/copy/", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        const data = await res.json().catch(function () {
          return {};
        });
        if (!res.ok) {
          throw new Error(copyApiError(data, "Could not start Smart Copy."));
        }
        closeWizard();
        window.location.reload();
      } catch (err) {
        alert(err.message || "Could not start Smart Copy.");
      } finally {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = "Activate Smart Copy";
        }
      }
    });
  }

  /* ── Smart Copy alert poll ── */
  function initSmartCopyAlert() {
    const root = document.getElementById("smart-copy-alert");
    if (!root) return;

    let current = null;
    let polling = false;
    let shownIds = {};

    function hideAlert() {
      root.hidden = true;
      current = null;
    }

    function scoreLabel(score) {
      if (score >= 80) return "STRONG";
      if (score >= 65) return "SOLID";
      if (score >= 50) return "MIXED";
      return "WEAK";
    }

    function fillList(el, items, className) {
      if (!el) return;
      el.innerHTML = "";
      (items || []).slice(0, 4).forEach(function (text) {
        const li = document.createElement("li");
        li.className = className || "";
        li.textContent = text;
        el.appendChild(li);
      });
    }

    function showAlert(exec) {
      current = exec;
      const traderName =
        (exec.trader && (exec.trader.display_name || exec.trader.wallet_address)) ||
        "A trader";
      const short =
        traderName.length > 14 ? traderName.slice(0, 6) + "…" + traderName.slice(-4) : traderName;
      document.getElementById("sca-lead").textContent =
        short + " just opened a position.";
      document.getElementById("sca-event").textContent =
        (exec.event && exec.event.title) || "—";
      const price = formatUsd(exec.entry_price);
      document.getElementById("sca-side").textContent =
        (exec.outcome || "—") + " @ " + price;

      const score = exec.copy_score != null ? Number(exec.copy_score) : null;
      document.getElementById("sca-score").textContent =
        score != null ? String(Math.round(score)) : "—";
      document.getElementById("sca-score-label").textContent =
        score != null ? scoreLabel(score) : "—";

      const pillars = exec.score_json || {};
      document.getElementById("sca-p-trader").textContent = pillars.trader != null ? pillars.trader : "—";
      document.getElementById("sca-p-event").textContent = pillars.event != null ? pillars.event : "—";
      document.getElementById("sca-p-market").textContent = pillars.market != null ? pillars.market : "—";
      document.getElementById("sca-p-consensus").textContent =
        pillars.consensus != null ? pillars.consensus : "—";
      document.getElementById("sca-p-risk").textContent = pillars.risk != null ? pillars.risk : "—";

      fillList(document.getElementById("sca-why"), exec.why_json, "is-why");
      fillList(document.getElementById("sca-risks"), exec.risks_json, "is-risk");

      const amt = Math.round(parseFloat(exec.amount) || 10);
      const copyBtn = document.getElementById("sca-copy");
      if (copyBtn) copyBtn.textContent = "Copy $" + amt;

      root.hidden = false;
    }

    document.getElementById("smart-copy-alert-close").addEventListener("click", hideAlert);

    document.getElementById("sca-score-btn").addEventListener("click", function () {
      const breakdown = document.getElementById("sca-breakdown");
      const open = breakdown.hidden;
      breakdown.hidden = !open;
      this.setAttribute("aria-expanded", open ? "true" : "false");
    });

    document.getElementById("sca-skip").addEventListener("click", async function () {
      if (!current) return;
      const btn = this;
      btn.disabled = true;
      try {
        await syncWalletSession(getConnectedAddress());
        const res = await csrfFetch("/api/copy/executions/" + current.id + "/skip/", {
          method: "POST",
          body: "{}",
        });
        if (!res.ok) {
          const data = await res.json().catch(function () {
            return {};
          });
          throw new Error(data.detail || "Skip failed");
        }
        shownIds[current.id] = true;
        hideAlert();
      } catch (err) {
        alert(err.message || "Could not skip.");
      } finally {
        btn.disabled = false;
      }
    });

    document.getElementById("sca-copy").addEventListener("click", async function () {
      if (!current) return;
      if (!getConnectedAddress()) {
        await connectWallet();
        return;
      }
      const amt = Math.round(parseFloat(current.amount) || 10);
      hideAlert();
      openTradeModal({
        eventId: current.event && current.event.id,
        outcome: current.outcome,
        price: current.entry_price,
        eventTitle: current.event && current.event.title,
        amount: amt,
        copyExecutionId: current.id,
      });
      shownIds[current.id] = true;
    });

    function canPollAlerts() {
      return document.body.getAttribute("data-authenticated") === "1" || !!getConnectedAddress();
    }

    async function poll() {
      if (polling) return;
      if (!canPollAlerts()) return;
      polling = true;
      try {
        const res = await csrfFetch("/api/copy/pending/");
        if (res.status === 403 || res.status === 401) {
          if (getConnectedAddress()) {
            await syncWalletSession(getConnectedAddress(), { skipReload: true });
          }
          return;
        }
        if (!res.ok) return;
        const data = await res.json();
        const results = (data && data.results) || [];
        const next = results.find(function (r) {
          return r && r.id && !shownIds[r.id];
        });
        if (next && (!current || current.id !== next.id) && root.hidden) {
          showAlert(next);
        }
      } catch (err) {
        /* silent — poll again */
      } finally {
        polling = false;
      }
    }

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) poll();
    });
    setInterval(poll, 8000);
    setTimeout(poll, 400);
  }

  /* ── Following pause / resume / settings ── */
  function initCopyManage() {
    document.querySelectorAll("[data-copy-pause]").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        const id = btn.getAttribute("data-copy-pause");
        if (!getConnectedAddress()) {
          await connectWallet();
          return;
        }
        btn.disabled = true;
        try {
          await syncWalletSession(getConnectedAddress());
          const res = await csrfFetch("/api/copy/" + id + "/", {
            method: "PATCH",
            body: JSON.stringify({ status: "PAUSED" }),
          });
          if (!res.ok) throw new Error("Pause failed");
          window.location.reload();
        } catch (err) {
          alert(err.message || "Could not pause.");
          btn.disabled = false;
        }
      });
    });

    document.querySelectorAll("[data-copy-resume]").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        const id = btn.getAttribute("data-copy-resume");
        if (!getConnectedAddress()) {
          await connectWallet();
          return;
        }
        btn.disabled = true;
        try {
          await syncWalletSession(getConnectedAddress());
          const res = await csrfFetch("/api/copy/" + id + "/", {
            method: "PATCH",
            body: JSON.stringify({ status: "ACTIVE" }),
          });
          if (!res.ok) throw new Error("Resume failed");
          window.location.reload();
        } catch (err) {
          alert(err.message || "Could not resume.");
          btn.disabled = false;
        }
      });
    });

    document.querySelectorAll("[data-copy-action]").forEach(function (input) {
      input.addEventListener("change", async function () {
        if (!input.checked) return;
        const id = input.getAttribute("data-copy-action");
        const auto = input.value === "auto";
        const section = input.closest("[data-agent-running]");
        const agentOn = section && section.getAttribute("data-agent-running") === "1";
        if (!getConnectedAddress()) {
          await connectWallet();
          return;
        }
        input.closest(".dl-follow-action")?.querySelectorAll("input").forEach(function (el) {
          el.disabled = true;
        });
        try {
          await syncWalletSession(getConnectedAddress());
          const res = await csrfFetch("/api/copy/" + id + "/", {
            method: "PATCH",
            body: JSON.stringify({ auto_execute: auto, copy_mode: "SMART" }),
          });
          const data = await res.json().catch(function () {
            return {};
          });
          if (!res.ok) throw new Error(data.detail || "Could not save this follow.");
          const line = document.querySelector("[data-copy-action-line='" + id + "']");
          if (line) {
            const waiting = (line.textContent || "").indexOf("waiting") !== -1
              ? " · " + ((line.textContent.match(/\d+ waiting/) || [])[0] || "")
              : "";
            const paused = (line.textContent || "").indexOf("Paused") !== -1 ? " · Paused" : "";
            line.textContent = auto
              ? "DreamAgent copies when they trade" + waiting + paused
              : "Alerts on Telegram and DreamLens" + waiting + paused;
          }
          if (auto && !agentOn) {
            toast("Saved. DreamAgent must be Active to copy immediately — you'll get alerts until then.", "ok");
          } else {
            toast(auto ? "DreamAgent will copy this trader immediately." : "You'll get Telegram and DreamLens alerts instead.", "ok");
          }
        } catch (err) {
          toast(err.message || "Could not save this follow.");
          const revert = input.closest(".dl-follow-action")?.querySelector(
            'input[value="' + (auto ? "notify" : "auto") + '"]'
          );
          if (revert) revert.checked = true;
        } finally {
          input.closest(".dl-follow-action")?.querySelectorAll("input").forEach(function (el) {
            el.disabled = false;
          });
        }
      });
    });

    const settingsForm = document.getElementById("smart-copy-settings-form");
    if (settingsForm) {
      settingsForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        if (!getConnectedAddress()) {
          await connectWallet();
          return;
        }
        const id = settingsForm.getAttribute("data-rel-id");
        const exec = settingsForm.querySelector('input[name="set_exec"]:checked');
        const auto = exec && exec.value === "auto";
        const minWr = Number(document.getElementById("set-min-wr").value) || 65;
        const payload = {
          max_per_trade: Number(document.getElementById("set-max-trade").value) || 10,
          max_daily: Number(document.getElementById("set-max-daily").value) || 50,
          min_copy_score: Number(document.getElementById("set-min-score").value) || 70,
          min_win_rate: minWr / 100,
          min_liquidity: Number(document.getElementById("set-min-liq").value) || 1000,
          auto_execute: auto,
          copy_mode: "SMART",
        };
        const submitBtn = settingsForm.querySelector('button[type="submit"]');
        if (submitBtn) submitBtn.disabled = true;
        try {
          await syncWalletSession(getConnectedAddress());
          const res = await csrfFetch("/api/copy/" + id + "/", {
            method: "PATCH",
            body: JSON.stringify(payload),
          });
          const data = await res.json().catch(function () {
            return {};
          });
          if (!res.ok) throw new Error(data.detail || "Save failed");
          alert("Settings saved.");
        } catch (err) {
          alert(err.message || "Could not save settings.");
        } finally {
          if (submitBtn) submitBtn.disabled = false;
        }
      });
    }
  }

  /* ── Chart theming ──
     Chart.js draws to canvas, which cannot read CSS custom properties, and
     @kurkle/color cannot parse oklch(). So resolve each token through a probe
     canvas: the browser converts it to an rgb/rgba string Chart.js understands,
     and the palette stays defined in one place (:root). */
  let colorProbe = null;

  function resolveColor(value, fallback) {
    if (!value) return fallback;
    if (!colorProbe) {
      const c = document.createElement("canvas");
      c.width = 1;
      c.height = 1;
      colorProbe = c.getContext("2d", { willReadFrequently: true });
    }
    // Assigning an unparseable value leaves fillStyle at the sentinel.
    colorProbe.fillStyle = "#ff00ff";
    colorProbe.fillStyle = value;
    if (colorProbe.fillStyle === "#ff00ff") return fallback;
    // Canvas keeps oklch() verbatim, so read back a pixel to get real channels.
    colorProbe.clearRect(0, 0, 1, 1);
    colorProbe.fillRect(0, 0, 1, 1);
    const px = colorProbe.getImageData(0, 0, 1, 1).data;
    return "rgba(" + px[0] + ", " + px[1] + ", " + px[2] + ", " + (px[3] / 255).toFixed(3) + ")";
  }

  function chartToken(name, fallback) {
    const raw = getComputedStyle(document.documentElement).getPropertyValue(name);
    return resolveColor(raw.trim(), fallback);
  }

  function chartTheme() {
    const subtle = chartToken("--dl-subtle", "#9a8fa3");
    const grid = chartToken("--dl-chart-grid", "rgba(255,255,255,0.06)");
    return {
      subtle,
      grid,
      ticks: { color: subtle, font: { size: 11 } },
      tooltip: {
        backgroundColor: chartToken("--dl-surface", "#1c1520"),
        borderColor: chartToken("--dl-line", "rgba(255,255,255,0.08)"),
        borderWidth: 1,
        titleColor: chartToken("--dl-ink", "#ffffff"),
        bodyColor: chartToken("--dl-muted", "#c9c2cc"),
      },
    };
  }

  /* ── Chart.js price history ── */
  function initChart() {
    const canvas = document.getElementById("price-chart");
    if (!canvas || typeof Chart === "undefined") return;

    const data = (window.DreamLens && window.DreamLens.chartData) || {
      labels: ["T-4", "T-3", "T-2", "T-1", "Now"],
      prices: [0.4, 0.41, 0.42, 0.43, 0.43],
    };

    const theme = chartTheme();
    const yes = chartToken("--dl-yes", "#6ec8a0");
    const ctx = canvas.getContext("2d");
    const gradient = ctx.createLinearGradient(0, 0, 0, 220);
    gradient.addColorStop(0, chartToken("--dl-chart-area", "rgba(110, 200, 160, 0.22)"));
    gradient.addColorStop(1, chartToken("--dl-chart-area-fade", "rgba(110, 200, 160, 0)"));

    DreamLens.chart = new Chart(ctx, {
      type: "line",
      data: {
        labels: data.labels,
        datasets: [
          {
            label: "YES price",
            data: data.prices,
            borderColor: yes,
            backgroundColor: gradient,
            fill: true,
            tension: 0.35,
            pointRadius: 3,
            pointHoverRadius: 6,
            pointBackgroundColor: yes,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: Object.assign({}, theme.tooltip, {
            callbacks: {
              label: function (ctx) {
                return " $" + Number(ctx.parsed.y).toFixed(2);
              },
            },
          }),
        },
        scales: {
          x: {
            grid: { color: theme.grid },
            ticks: theme.ticks,
          },
          y: {
            grid: { color: theme.grid },
            ticks: {
              color: theme.subtle,
              font: { size: 11 },
              callback: function (v) {
                return "$" + Number(v).toFixed(2);
              },
            },
            min: 0,
            max: 1,
          },
        },
      },
    });
  }

  function initTraderVolumeChart() {
    const node = document.getElementById("trader-volume-chart");
    const canvas = document.getElementById("trader-volume-canvas");
    if (!node || !canvas || typeof Chart === "undefined") return;
    let data;
    try {
      data = JSON.parse(node.textContent);
    } catch (err) {
      return;
    }
    if (!data || !data.labels || !data.labels.length) return;

    const ctx = canvas.getContext("2d");
    const theme = chartTheme();
    new Chart(ctx, {
      type: "bar",
      data: {
        labels: data.labels,
        datasets: [
          {
            label: "Volume",
            data: data.volumes || [],
            backgroundColor: chartToken("--dl-chart-bar", "rgba(120, 190, 220, 0.45)"),
            borderColor: chartToken("--dl-accent", "#78bedc"),
            borderWidth: 1,
            borderRadius: 4,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: Object.assign({}, theme.tooltip, {
            callbacks: {
              label: function (item) {
                const n = Number(item.parsed.y) || 0;
                return " $" + n.toLocaleString(undefined, { maximumFractionDigits: 0 });
              },
            },
          }),
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: theme.subtle, font: { size: 11 }, maxRotation: 0 },
          },
          y: {
            grid: { color: theme.grid },
            ticks: {
              color: theme.subtle,
              font: { size: 11 },
              callback: function (v) {
                return "$" + Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
              },
            },
            beginAtZero: true,
          },
        },
      },
    });
  }

  function initTraderTableFilter() {
    const input = document.getElementById("trader-table-filter");
    const table = document.getElementById("active-traders-table");
    const empty = document.getElementById("trader-table-empty");
    const count = document.getElementById("trader-table-count");
    if (!input || !table) return;
    const rows = Array.from(table.querySelectorAll("tbody tr[data-trader-filter]"));
    const total = rows.length;

    function apply() {
      const q = (input.value || "").trim().toLowerCase().replace(/^0x/, "");
      let shown = 0;
      rows.forEach(function (row) {
        const hay = (row.getAttribute("data-trader-filter") || "").replace(/^0x/, "");
        const match = !q || hay.indexOf(q) !== -1;
        row.hidden = !match;
        if (match) shown += 1;
      });
      if (empty) empty.hidden = shown !== 0;
      if (count) {
        count.textContent =
          shown === total
            ? total + " wallet" + (total === 1 ? "" : "s")
            : shown + " of " + total;
      }
    }

    input.addEventListener("input", apply);
  }

  function initCopyTextButtons() {
    document.querySelectorAll("[data-copy-text]").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        const text = btn.getAttribute("data-copy-text") || "";
        if (!text) return;
        try {
          await navigator.clipboard.writeText(text);
          const prev = btn.textContent;
          btn.textContent = "Copied";
          setTimeout(function () {
            btn.textContent = prev;
          }, 1400);
        } catch (err) {
          toast("Could not copy address");
        }
      });
    });
  }

  /* ── Portfolio wallet balances ── */
  function initPortfolioBalances() {
    const root = document.getElementById("portfolio-wallet-balance");
    if (!root) return;

    const gated = document.getElementById("portfolio-balance-gated");
    const grid = document.getElementById("portfolio-balance-grid");
    const meta = document.getElementById("portfolio-balance-meta");
    const refreshBtn = document.getElementById("portfolio-balance-refresh");
    const collEl = document.getElementById("bal-collateral");
    const collSym = document.getElementById("bal-collateral-sym");
    const nativeEl = document.getElementById("bal-native");
    const nativeSym = document.getElementById("bal-native-sym");
    const addrEl = document.getElementById("bal-address");
    const statusEl = document.getElementById("bal-status");

    function formatAmount(value, decimals) {
      const n = Number(value);
      if (!Number.isFinite(n)) return "—";
      if (decimals === 4) {
        return n.toLocaleString(undefined, {
          minimumFractionDigits: 2,
          maximumFractionDigits: 4,
        });
      }
      return n.toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    }

    function showConnected(connected) {
      if (gated) gated.hidden = connected;
      if (grid) grid.hidden = !connected;
      if (refreshBtn) refreshBtn.hidden = !connected;
      if (meta) meta.hidden = !connected;
    }

    async function loadBalances() {
      const address = getConnectedAddress();
      const sessionAuthed = root.dataset.authenticated === "1";
      if (!address && !sessionAuthed) {
        showConnected(false);
        return;
      }
      showConnected(true);
      if (statusEl) statusEl.textContent = "Loading…";
      if (refreshBtn) refreshBtn.disabled = true;
      try {
        if (address) {
          await syncWalletSession(address);
        }
        const res = await csrfFetch("/api/portfolio/balances/");
        const data = await res.json().catch(function () {
          return {};
        });
        if (!res.ok) {
          throw new Error(data.detail || "Could not load balances.");
        }
        if (collEl) {
          collEl.textContent =
            data.collateral_balance != null
              ? "$" + formatAmount(data.collateral_balance, 2)
              : "—";
        }
        if (collSym && data.collateral_symbol) {
          collSym.textContent = data.collateral_symbol;
        }
        if (nativeEl) {
          nativeEl.textContent =
            data.native_balance != null
              ? formatAmount(data.native_balance, 4)
              : "—";
        }
        if (nativeSym && data.native_symbol) {
          nativeSym.textContent = data.native_symbol;
        }
        if (addrEl && data.address) {
          addrEl.textContent = shortAddress(data.address);
        }
        const copyBtn = root.querySelector("[data-copy-wallet]");
        if (copyBtn && data.address) {
          copyBtn.setAttribute("data-copy-text", data.address);
        }
        const explorer = document.getElementById("bal-explorer");
        if (explorer && data.address) {
          const cfg = window.DreamLensConfig || {};
          if (cfg.explorerUrl) {
            explorer.href = cfg.explorerUrl + "/address/" + data.address;
            explorer.hidden = false;
          }
        }
        if (meta && data.address) {
          meta.hidden = false;
        }
        if (statusEl) {
          statusEl.textContent = data.error || "";
        }
        root.dataset.authenticated = "1";
      } catch (err) {
        if (statusEl) statusEl.textContent = err.message || "Balance unavailable.";
        if (meta) meta.hidden = false;
      } finally {
        if (refreshBtn) refreshBtn.disabled = false;
      }
    }

    if (refreshBtn) {
      refreshBtn.addEventListener("click", function () {
        loadBalances();
      });
    }

    // After restoreWallet / connect, refresh when address is known.
    setTimeout(loadBalances, 400);
    document.getElementById("wallet-connect")?.addEventListener("click", function () {
      setTimeout(loadBalances, 1200);
    });
  }

  function initTelegramLink() {
    const form = document.getElementById("telegram-link-form");
    const submit = document.getElementById("telegram-link-submit");
    if (!form || !submit) return;
    const input = document.getElementById("telegram-chat-input");
    const statusEl = document.getElementById("telegram-link-status");
    const active = document.getElementById("telegram-link-active");
    const unlinkBtn = document.getElementById("telegram-unlink");
    const chatIdEl = document.getElementById("telegram-chat-id");
    const pendingChatEl = document.getElementById("telegram-pending-chat");
    const pending = document.getElementById("telegram-pending");
    const pill = document.getElementById("telegram-pill");
    const openWrap = document.getElementById("telegram-open-wrap");
    const openBot = document.getElementById("telegram-open-bot");
    const card = document.getElementById("telegram-link-card");
    let pollTimer = null;

    function showStatus(msg, kind) {
      if (!statusEl) return;
      statusEl.textContent = msg || "";
      statusEl.classList.toggle("is-error", kind === "error");
    }

    function setPill(status) {
      if (!pill) return;
      pill.className = "dl-smart-pill";
      if (status === "ACTIVE") {
        pill.classList.add("is-on");
        pill.innerHTML = '<span class="dl-smart-pill__dot" aria-hidden="true"></span> Linked';
      } else if (status === "PENDING") {
        pill.classList.add("is-paused");
        pill.textContent = "Confirm in Telegram";
      } else {
        pill.textContent = "Not linked";
      }
    }

    function stopPoll() {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    function startPoll() {
      if (pollTimer) return;
      pollTimer = setInterval(async function () {
        try {
          const res = await csrfFetch("/api/telegram/link/");
          if (!res.ok) return;
          const data = await res.json();
          render(data);
          if (data.status === "ACTIVE") {
            showStatus("Telegram linked. Send /events in the bot.");
            stopPoll();
          } else if (data.status !== "PENDING") {
            stopPoll();
          }
        } catch (_err) {}
      }, 2500);
    }

    function render(data) {
      const status = (data && data.status) || "";
      const linked = status === "ACTIVE";
      const waiting = status === "PENDING";
      if (card) card.setAttribute("data-telegram-status", status);
      if (form) form.hidden = linked || waiting;
      if (pending) pending.hidden = !waiting;
      if (active) active.hidden = !linked;
      if (openWrap) openWrap.hidden = linked;
      setPill(status);
      if (chatIdEl && data && data.chat_id) chatIdEl.textContent = data.chat_id;
      if (pendingChatEl && data && data.chat_id) pendingChatEl.textContent = data.chat_id;
      if (openBot && data && data.bot_url) {
        const start = data.start_param || "chatid";
        openBot.href = data.bot_url + "?start=" + encodeURIComponent(start);
        openBot.hidden = false;
      }
      if (waiting) {
        showStatus("Waiting for Confirm in Telegram.");
        startPoll();
      } else if (linked) {
        stopPoll();
      }
    }

    function submitLink() {
      const chatId = (input && input.value ? input.value : "").trim();
      if (!/^-?\d+$/.test(chatId)) {
        showStatus("Enter the numeric chat ID from the bot.", "error");
        return;
      }
      submit.disabled = true;
      showStatus("Sending Confirm to Telegram…");
      csrfFetch("/api/telegram/link/", {
        method: "POST",
        body: JSON.stringify({ chat_id: chatId }),
      })
        .then(function (res) {
          return res.json().then(function (data) {
            if (!res.ok) throw new Error(data.detail || "Could not link Telegram");
            return data;
          });
        })
        .then(function (data) {
          render(data);
          if (data.status === "ACTIVE") showStatus("Telegram linked. Send /events in the bot.");
        })
        .catch(function (err) {
          showStatus(err.message || "Could not link Telegram", "error");
        })
        .finally(function () {
          submit.disabled = false;
        });
    }

    submit.addEventListener("click", submitLink);
    if (input) {
      input.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          submitLink();
        }
      });
    }

    if (unlinkBtn) {
      unlinkBtn.addEventListener("click", async function () {
        unlinkBtn.disabled = true;
        try {
          const res = await csrfFetch("/api/telegram/link/", { method: "DELETE" });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || "Could not unlink");
          render(data);
          showStatus("Telegram unlinked.");
          stopPoll();
          if (input) input.value = "";
        } catch (err) {
          showStatus(err.message || "Could not unlink", "error");
        } finally {
          unlinkBtn.disabled = false;
        }
      });
    }

    csrfFetch("/api/telegram/link/")
      .then(function (res) {
        return res.ok ? res.json() : null;
      })
      .then(function (data) {
        if (data) render(data);
      })
      .catch(function () {});
  }

  async function claimViaDreamAgent(positionId) {
    const res = await csrfFetch("/api/portfolio/claim/", {
      method: "POST",
      body: JSON.stringify({ position_id: Number(positionId) }),
    });
    const prepared = await readJsonResponse(res);
    if (!res.ok) {
      throw new Error(prepared.detail || prepared.error || "DreamAgent could not claim.");
    }
    const n = Number(prepared.claimed || 0);
    if (!n) {
      throw new Error(prepared.detail || "DreamAgent could not claim this win. Re-sign the grant at Activate.");
    }
    return prepared;
  }

  async function claimAllViaDreamAgent(btn) {
    const original = btn.textContent;
    btn.disabled = true;
    try {
      btn.textContent = "DreamAgent claiming…";
      const res = await csrfFetch("/api/portfolio/claim/", {
        method: "POST",
        body: JSON.stringify({}),
      });
      const data = await readJsonResponse(res);
      if (!res.ok) {
        throw new Error(data.detail || data.error || "DreamAgent could not claim.");
      }
      const n = Number(data.claimed || 0);
      if (n === 0) {
        throw new Error("Nothing for DreamAgent to claim. MetaMask fills still use Claim on Portfolio.");
      }
      toast(
        n === 1
          ? "DreamAgent claimed a win into your Smart Account."
          : "DreamAgent claimed " + n + " wins into your Smart Account.",
        "ok"
      );
      window.location.reload();
    } catch (err) {
      console.warn("DreamAgent claim failed", err);
      toast(err.message || "DreamAgent could not claim.", "error");
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  async function claimPosition(btn) {
    const positionId = btn.getAttribute("data-claim-position");
    const expectedWallet = (btn.getAttribute("data-claim-wallet") || "").toLowerCase();
    const viaAgent = btn.getAttribute("data-claim-agent") === "1";
    if (!positionId) return;
    const original = btn.textContent;
    btn.disabled = true;
    try {
      if (viaAgent) {
        btn.textContent = "DreamAgent claiming…";
        await claimViaDreamAgent(positionId);
        toast("Winnings claimed into your Smart Account.", "ok");
        window.location.reload();
        return;
      }
      const wallet = await ensureWalletForTrade();
      if (expectedWallet && wallet.address.toLowerCase() !== expectedWallet) {
        throw new Error("Switch MetaMask to the wallet that holds these outcome tokens.");
      }
      btn.textContent = "Preparing…";
      const res = await csrfFetch("/api/portfolio/positions/" + positionId + "/redeem/", {
        method: "POST",
        body: JSON.stringify({ wallet_address: wallet.address }),
      });
      const prepared = await readJsonResponse(res);
      if (!res.ok) {
        throw new Error(prepared.detail || prepared.error || "Could not build the claim.");
      }
      if (prepared.claimed) {
        toast(
          prepared.via_smart_account
            ? "Winnings claimed into your Smart Account."
            : "Winnings claimed. Collateral is in this wallet.",
          "ok"
        );
        window.location.reload();
        return;
      }
      const claimGas = 10000000;
      async function sendClaimStep(label, tx) {
        if (!tx) return;
        btn.textContent = label;
        const hash = await sendWalletTx(wallet.eth, tx, wallet.address, { fallbackGas: claimGas });
        const receipt = await waitForWalletReceipt(wallet.eth, hash, 120000);
        const stepOk =
          receipt.status === "0x1" || receipt.status === 1 || receipt.status === "1";
        if (!stepOk) {
          throw new Error(label + " reverted on Shannon.");
        }
      }
      await sendClaimStep("Asking oracle…", prepared.poke_tx);
      await sendClaimStep("Finalizing market…", prepared.finalize_tx);
      await sendClaimStep("Syncing settlement…", prepared.sync_tx);
      await sendClaimStep("Approve tokens…", prepared.approval_tx);
      if (!prepared.unsigned_tx) {
        throw new Error("Could not build the DreamDEX redeem.");
      }
      btn.textContent = "Claim on-chain…";
      const txHash = await sendWalletTx(wallet.eth, prepared.unsigned_tx, wallet.address, {
        fallbackGas: claimGas,
      });
      if (!txHash) {
        throw new Error("Wallet did not return a transaction hash.");
      }
      btn.textContent = "Confirming…";
      const receipt = await waitForWalletReceipt(wallet.eth, txHash, 120000);
      const ok =
        receipt.status === "0x1" || receipt.status === 1 || receipt.status === "1";
      if (!ok) {
        throw new Error("Redeem reverted on DreamDEX. Winnings were not paid.");
      }
      const confirmRes = await csrfFetch(
        "/api/portfolio/positions/" + positionId + "/redeem/confirm/",
        {
          method: "POST",
          body: JSON.stringify({ tx_hash: txHash }),
        }
      );
      const confirmData = await readJsonResponse(confirmRes).catch(function () {
        return {};
      });
      if (!confirmRes.ok) {
        throw new Error(confirmData.detail || "Claim sent, but DreamLens could not record it.");
      }
      toast(prepared.via_smart_account
        ? "Winnings claimed into your Smart Account."
        : "Winnings claimed. Collateral is in this wallet.", "ok");
      window.location.reload();
    } catch (err) {
      console.warn("Claim failed", err);
      toast(err.message || "Claim failed. Check MetaMask and try again.", "error");
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  async function closePosition(btn) {
    const positionId = btn.getAttribute("data-close-position");
    const expectedWallet = (btn.getAttribute("data-close-wallet") || "").toLowerCase();
    if (!positionId) return;
    const original = btn.textContent;
    btn.disabled = true;
    try {
      const wallet = await ensureWalletForTrade();
      if (expectedWallet && wallet.address.toLowerCase() !== expectedWallet) {
        throw new Error("Switch MetaMask to the wallet that holds these outcome tokens.");
      }
      btn.textContent = "Preparing…";
      const res = await csrfFetch("/api/portfolio/positions/" + positionId + "/close/", {
        method: "POST",
        body: JSON.stringify({ wallet_address: wallet.address }),
      });
      const prepared = await readJsonResponse(res);
      if (!res.ok) {
        throw new Error(prepared.detail || prepared.error || "Could not build the close.");
      }
      if (prepared.approval_tx) {
        btn.textContent = "Approve tokens…";
        const approveHash = await sendWalletTx(wallet.eth, prepared.approval_tx, wallet.address);
        const approveReceipt = await waitForWalletReceipt(wallet.eth, approveHash, 120000);
        const approveOk =
          approveReceipt.status === "0x1" ||
          approveReceipt.status === 1 ||
          approveReceipt.status === "1";
        if (!approveOk) {
          throw new Error("Outcome token approval reverted on Shannon.");
        }
      }
      if (!prepared.unsigned_tx) {
        throw new Error("Could not build the DreamDEX sell.");
      }
      btn.textContent = "Selling on-chain…";
      const txHash = await sendWalletTx(wallet.eth, prepared.unsigned_tx, wallet.address);
      if (!txHash) {
        throw new Error("Wallet did not return a transaction hash.");
      }
      btn.textContent = "Confirming…";
      const receipt = await waitForWalletReceipt(wallet.eth, txHash, 120000);
      const ok =
        receipt.status === "0x1" || receipt.status === 1 || receipt.status === "1";
      if (!ok) {
        throw new Error("Sell reverted on DreamDEX. The position was not closed.");
      }
      const confirmBody = { tx_hash: txHash };
      if (prepared.trade_id) confirmBody.trade_id = prepared.trade_id;
      const confirmRes = await csrfFetch(
        "/api/portfolio/positions/" + positionId + "/close/confirm/",
        {
          method: "POST",
          body: JSON.stringify(confirmBody),
        }
      );
      const confirmData = await readJsonResponse(confirmRes).catch(function () {
        return {};
      });
      if (!confirmRes.ok) {
        throw new Error(confirmData.detail || "Sell sent, but DreamLens could not record it.");
      }
      toast("Position closed. Collateral is back in this wallet.", "ok");
      window.location.reload();
    } catch (err) {
      console.warn("Close failed", err);
      alert(err.message || "Could not close this trade. Check MetaMask and try again.");
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  function initClaimPositions() {
    document.querySelectorAll("[data-claim-position]").forEach(function (btn) {
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        claimPosition(btn);
      });
    });
    document.querySelectorAll("[data-claim-agent-all]").forEach(function (btn) {
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        claimAllViaDreamAgent(btn);
      });
    });
    document.querySelectorAll("[data-close-position]").forEach(function (btn) {
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        closePosition(btn);
      });
    });
  }

  function initViewMode() {
    const stored = sessionStorage.getItem("dreamlens_view") || "simple";
    const body = document.body;
    const btn = document.getElementById("view-mode-toggle");
    function apply(mode) {
      body.setAttribute("data-view", mode);
      sessionStorage.setItem("dreamlens_view", mode);
      if (btn) {
        btn.textContent = mode === "advanced" ? "Advanced" : "Simple";
        btn.setAttribute("aria-pressed", mode === "advanced" ? "true" : "false");
      }
    }
    apply(stored === "advanced" ? "advanced" : "simple");
    if (btn) {
      btn.addEventListener("click", function () {
        const next = body.getAttribute("data-view") === "advanced" ? "simple" : "advanced";
        apply(next);
      });
    }
  }

  function initIntentFilters() {
    const chips = document.querySelectorAll("[data-intent-filter]");
    if (!chips.length) return;
    const empty = document.getElementById("market-empty");
    function cards() {
      return document.querySelectorAll(".dl-event-card");
    }
    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        chips.forEach(function (c) {
          c.classList.remove("is-active");
          c.setAttribute("aria-pressed", "false");
        });
        chip.classList.add("is-active");
        chip.setAttribute("aria-pressed", "true");
        const intent = chip.getAttribute("data-intent-filter") || "all";
        let visible = 0;
        cards().forEach(function (card) {
          const tags = (card.getAttribute("data-intent") || "").toLowerCase();
          const show = intent === "all" || tags.indexOf(intent) !== -1;
          card.hidden = !show;
          if (show) visible += 1;
        });
        if (empty) empty.hidden = visible > 0;
      });
    });
  }

  function renderHeadlineList(listEl, headlines) {
    if (!listEl) return;
    listEl.replaceChildren();
    if (!headlines || !headlines.length) {
      const empty = document.createElement("li");
      empty.className = "dl-feed__empty";
      empty.textContent = "No headlines right now. Check back in a few minutes.";
      listEl.appendChild(empty);
      return;
    }
    headlines.forEach(function (row) {
      const li = document.createElement("li");
      li.className = "dl-feed__item";
      const link = document.createElement("a");
      link.href = row.url || "#";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = row.title || "Headline";
      const meta = document.createElement("div");
      meta.className = "dl-feed__meta";
      const bits = [row.source, row.ago].filter(Boolean);
      if (row.assets && row.assets.length) bits.push(row.assets.join(" · "));
      meta.textContent = bits.join(" · ");
      li.appendChild(link);
      li.appendChild(meta);
      listEl.appendChild(li);
    });
  }

  async function fetchHeadlines(asset, limit) {
    const params = new URLSearchParams();
    if (asset) params.set("asset", asset);
    if (limit) params.set("limit", String(limit));
    const url = "/api/news/" + (params.toString() ? "?" + params.toString() : "");
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    const data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error("Could not load headlines.");
    return data.headlines || [];
  }

  function initMarketFeed() {
    const feeds = document.querySelectorAll("[data-market-feed]");
    if (!feeds.length) return;

    function loadFeed(root) {
      const list = root.querySelector("[data-feed-list]");
      const live = root.querySelector("[data-feed-live]");
      const asset = (root.getAttribute("data-feed-asset") || "").toUpperCase();
      const limit = root.getAttribute("data-feed-limit");
      fetchHeadlines(asset || null, limit)
        .then(function (headlines) {
          renderHeadlineList(list, headlines);
          if (live) live.hidden = headlines.length === 0;
        })
        .catch(function () {
          renderHeadlineList(list, []);
          if (live) live.hidden = true;
        });
    }

    feeds.forEach(loadFeed);
    window.setInterval(function () {
      feeds.forEach(loadFeed);
    }, 180000);
  }

  const EXPLAIN_SECTIONS = [
    ["setup", "What's going on"],
    ["yes_needs", "What YES needs"],
    ["no_needs", "What NO needs"],
    ["in_the_price", "What's already in the price"],
    ["could_change", "What could change this"],
  ];

  function renderExplanation(body, explanation, fallback) {
    if (!body) return;
    body.replaceChildren();
    if (explanation) {
      let split = null;
      let index = 0;
      EXPLAIN_SECTIONS.forEach(function (pair) {
        const text = explanation[pair[0]];
        if (!text) return;
        const section = document.createElement("section");
        section.className = "dl-explain__section";
        section.style.setProperty("--i", String(index));
        index += 1;
        if (pair[0] === "setup") section.classList.add("dl-explain__section--lead");
        if (pair[0] === "yes_needs") section.classList.add("dl-explain__section--yes");
        if (pair[0] === "no_needs") section.classList.add("dl-explain__section--no");
        if (pair[0] === "could_change") section.classList.add("dl-explain__section--change");
        const heading = document.createElement("h3");
        heading.textContent = pair[1];
        const para = document.createElement("p");
        para.textContent = text;
        section.appendChild(heading);
        section.appendChild(para);
        if (pair[0] === "yes_needs" || pair[0] === "no_needs") {
          if (!split) {
            split = document.createElement("div");
            split.className = "dl-explain__split";
          }
          split.appendChild(section);
          return;
        }
        if (split) {
          body.appendChild(split);
          split = null;
        }
        body.appendChild(section);
      });
      if (split) body.appendChild(split);
      if (body.childNodes.length) return;
    }
    body.textContent = fallback || "No explanation right now.";
  }

  function initExplainSheet() {
    const sheet = document.getElementById("explain-sheet");
    if (!sheet) return;
    const body = document.getElementById("explain-body");
    const questionEl = document.getElementById("explain-question");
    const lensLink = document.getElementById("explain-lens-link");
    const newsWrap = document.getElementById("explain-news");
    const newsList = document.getElementById("explain-news-list");

    function close() {
      sheet.classList.remove("is-open");
      sheet.setAttribute("aria-hidden", "true");
    }
    sheet.querySelectorAll("[data-explain-close]").forEach(function (el) {
      el.addEventListener("click", close);
    });

    document.querySelectorAll("[data-explain-event]").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        const eventId = btn.getAttribute("data-explain-event");
        const question = btn.getAttribute("data-explain-question") || "";
        const asset = (
          btn.getAttribute("data-explain-asset") ||
          (btn.closest("[data-asset]") && btn.closest("[data-asset]").getAttribute("data-asset")) ||
          ""
        ).toUpperCase();
        if (questionEl) questionEl.textContent = question;
        if (body) {
          body.replaceChildren(cloneMarketReader("DreamLens is reading this market…"));
        }
        sheet.setAttribute("aria-busy", "true");
        if (newsWrap) newsWrap.hidden = true;
        if (lensLink && eventId) {
          lensLink.setAttribute("href", "/lens/?event=" + encodeURIComponent(eventId));
        }
        sheet.classList.add("is-open");
        sheet.setAttribute("aria-hidden", "false");
        if (asset && newsList) {
          fetchHeadlines(asset, 4)
            .then(function (headlines) {
              if (!headlines.length) return;
              renderHeadlineList(newsList, headlines);
              if (newsWrap) newsWrap.hidden = false;
            })
            .catch(function () { /* keep the explanation even if news fails */ });
        }
        try {
          const res = await csrfFetch("/api/ai/lens/", {
            method: "POST",
            body: JSON.stringify({
              message: "Explain this market in plain language. What would have to happen for YES to win? Do not suggest a trade.",
              history: [],
              event_id: Number(eventId),
              structured: true,
            }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || "Could not explain this market.");
          if (data.prepare_params || (data.tool_results && data.tool_results.prepare_params)) {
            throw new Error("Lens does not prepare trades.");
          }
          renderExplanation(body, data.explanation, data.reply);
        } catch (err) {
          if (body) {
            body.replaceChildren();
            const p = document.createElement("p");
            p.className = "dl-explain__error";
            p.textContent = err.message || "Could not explain this market.";
            body.appendChild(p);
          }
        } finally {
          sheet.removeAttribute("aria-busy");
        }
      });
    });
  }

  function initPossibleResult() {
    const grid = document.getElementById("possible-grid");
    if (!grid) return;
    const yesPrice = parseFloat(grid.getAttribute("data-yes-price") || "0.5");
    const noPrice = parseFloat(grid.getAttribute("data-no-price") || "0.5");

    function fill(side, price, amount) {
      const parts = payoutParts(amount, price);
      const pay = grid.querySelector("[data-possible-pay='" + side + "']");
      const payout = grid.querySelector("[data-possible-payout='" + side + "']");
      const profit = grid.querySelector("[data-possible-profit='" + side + "']");
      const loss = grid.querySelector("[data-possible-loss='" + side + "']");
      if (pay) pay.textContent = formatUsd(parts.pay);
      if (payout) payout.textContent = formatUsd(parts.payout);
      if (profit) profit.textContent = formatUsd(parts.profit);
      if (loss) loss.textContent = formatUsd(parts.loss);
    }

    function apply(amount) {
      fill("yes", yesPrice, amount);
      fill("no", noPrice, amount);
      document.querySelectorAll(".dl-trade-trigger[data-amount]").forEach(function (btn) {
        btn.setAttribute("data-amount", String(amount));
      });
    }

    document.querySelectorAll("[data-possible-amount]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        document.querySelectorAll("[data-possible-amount]").forEach(function (c) {
          c.classList.remove("is-active");
        });
        chip.classList.add("is-active");
        apply(chip.getAttribute("data-possible-amount") || "5");
      });
    });
    apply(5);
  }

  /* ── Init ── */
  listenForWalletProviders();
  document.addEventListener("DOMContentLoaded", function () {
    listenForWalletProviders();
    // localStorage only — never talk to the wallet extension on navigation.
    restoreWallet();
    const walletBtn = document.getElementById("wallet-connect");
    if (walletBtn) walletBtn.addEventListener("click", connectWallet);

    initViewMode();
    initLensChat();
    initLensTabs();
    initCountdowns();
    initMarketFilters();
    initRadarFilters();
    initIntentFilters();
    initMarketFeed();
    initExplainSheet();
    initPossibleResult();
    initTradeModal();
    initCopyForm();
    initFollowButtons();
    initFollowWalletForm();
    initSmartCopyWizard();
    initSmartCopyAlert();
    initCopyManage();
    initChart();
    initTraderVolumeChart();
    initTraderTableFilter();
    initCopyTextButtons();
    initPortfolioBalances();
    initTelegramLink();
    initDreamAgent();
    initOwnerWithdraw();
    initAgentBalance();
    initClaimPositions();
    initStartConnect();
  });

  function initStartConnect() {
    const btn = document.querySelector("[data-start-connect]");
    if (!btn) return;
    btn.addEventListener("click", function () {
      const walletBtn = document.getElementById("wallet-connect");
      if (walletBtn) walletBtn.click();
    });
  }

  DreamLens.toHexChainId = toHexChainId;
  DreamLens.walletTxParams = walletTxParams;
  DreamLens.getEthereumProvider = getEthereumProvider;

  function toast(msg, kind) {
    if (typeof DreamLens.showToast === "function") {
      DreamLens.showToast(msg, kind);
      return;
    }
    const root = document.getElementById("dl-toasts");
    if (!root) return;
    const el = document.createElement("div");
    el.className = "dl-toast" + (kind ? " dl-toast--" + kind : "");
    el.textContent = msg;
    root.appendChild(el);
    setTimeout(function () { el.remove(); }, 4000);
  }

  function initOwnerWithdraw() {
    const withdrawBtn = document.getElementById("sa-withdraw");
    if (!withdrawBtn) return;

    async function ensureOwnerWallet() {
      let addr = getConnectedAddress();
      if (!addr) {
        await connectWallet();
        addr = getConnectedAddress();
      }
      if (!addr) throw new Error("Connect MetaMask first");
      return addr;
    }

    withdrawBtn.addEventListener("click", async function () {
      withdrawBtn.disabled = true;
      withdrawBtn.setAttribute("aria-busy", "true");
      try {
        const owner = await ensureOwnerWallet();
        const { eth } = await ensureWalletForTrade();
        const amount = document.getElementById("sa-withdraw-amount")?.value || "";
        if (!amount || Number(amount) <= 0) {
          throw new Error("Enter how many trading dollars to withdraw.");
        }
        const prepRes = await csrfFetch(
          "/api/smart-account/withdraw/?amount=" + encodeURIComponent(amount)
        );
        const prep = await prepRes.json().catch(function () { return {}; });
        if (!prepRes.ok) throw new Error(prep.detail || "Could not prepare withdraw");
        const dest = String(prep.destination || "").toLowerCase();
        if (dest && dest !== owner.toLowerCase()) {
          throw new Error("Connect the MetaMask that owns this trading account.");
        }
        const typed = prep.typed_data;
        if (!typed) throw new Error("Could not prepare the withdraw signature.");
        const signTyped = {
          types: typed.types,
          primaryType: typed.primaryType,
          domain: typed.domain,
          message: typed.message,
        };
        toast("Sign the withdraw permission in MetaMask…", "success");
        const signature = await eth.request({
          method: "eth_signTypedData_v4",
          params: [owner, JSON.stringify(signTyped)],
        });
        const assembleRes = await csrfFetch("/api/smart-account/withdraw/", {
          method: "POST",
          body: JSON.stringify({
            amount: amount,
            signature: signature,
            salt: typed.message && typed.message.salt,
            expires_at: prep.expires_at,
          }),
        });
        const assembled = await assembleRes.json().catch(function () { return {}; });
        if (!assembleRes.ok) {
          throw new Error(assembled.detail || "Could not build the withdraw transaction.");
        }
        if (!assembled.unsigned_tx) {
          throw new Error("Withdraw transaction was not prepared.");
        }
        toast("Confirm withdraw in MetaMask…", "success");
        const txHash = await sendWalletTx(eth, assembled.unsigned_tx, owner);
        const confirmRes = await csrfFetch("/api/smart-account/withdraw/", {
          method: "POST",
          body: JSON.stringify({ amount: amount, tx_hash: txHash }),
        });
        const confirmed = await confirmRes.json().catch(function () { return {}; });
        if (!confirmRes.ok) throw new Error(confirmed.detail || "Withdraw failed");
        toast("Trading dollars sent to MetaMask.", "success");
        window.location.reload();
      } catch (err) {
        toast((err && err.message) || "Withdraw failed", "error");
      } finally {
        withdrawBtn.disabled = false;
        withdrawBtn.removeAttribute("aria-busy");
      }
    });
  }

  function initAgentBalance() {
    const el = document.getElementById("agent-available");
    if (!el) return;
    csrfFetch("/api/smart-account/")
      .then(function (res) {
        return res.json().then(function (data) {
          return { ok: res.ok, data: data };
        });
      })
      .then(function (result) {
        if (!result.ok) return;
        const bal = result.data.balance || {};
        if (bal.error || bal.collateral == null || bal.collateral === "") return;
        el.textContent = formatUsd(bal.collateral);
      })
      .catch(function () {});
  }

  function initDreamAgent() {
    const createBtn = document.getElementById("sa-create");
    const depositBtn = document.getElementById("sa-deposit");
    const depositGasBtn = document.getElementById("sa-deposit-gas");
    const grantBtn = document.getElementById("grant-permission");
    const pauseBtn = document.querySelector("[data-agent-pause]");
    const resumeBtn = document.querySelector("[data-agent-resume]");
    const revokeBtn = document.querySelector("[data-agent-revoke]");
    const revokeModal = document.getElementById("agent-revoke-modal");
    const revokeConfirm = document.getElementById("agent-revoke-confirm");

    async function ensureWallet() {
      let addr = getConnectedAddress();
      if (!addr) {
        await connectWallet();
        addr = getConnectedAddress();
      }
      if (!addr) throw new Error("Connect MetaMask first");
      return addr;
    }

    function encodeErc20Transfer(to, amountRaw) {
      const toPad = String(to).replace(/^0x/, "").toLowerCase().padStart(64, "0");
      const amt = BigInt(amountRaw).toString(16).padStart(64, "0");
      return "0xa9059cbb" + toPad + amt;
    }

    async function loadGrant(owner) {
      const qs = owner ? ("?owner=" + encodeURIComponent(owner)) : "";
      const res = await csrfFetch("/api/agent/grant/" + qs);
      const data = await res.json().catch(function () { return {}; });
      if (res.status === 403) {
        throw new Error("Connect your wallet again so DreamLens can start a session.");
      }
      if (!res.ok) throw new Error(data.detail || "Could not load agent config");
      if (data.configured === false) {
        throw new Error(data.config_error || "Trading accounts are not available on this network yet.");
      }
      return data;
    }

    async function waitForTxReceipt(eth, txHash, timeoutMs) {
      const started = Date.now();
      while (Date.now() - started < (timeoutMs || 90000)) {
        const receipt = await eth.request({
          method: "eth_getTransactionReceipt",
          params: [txHash],
        });
        if (receipt) return receipt;
        await new Promise(function (r) { setTimeout(r, 1500); });
      }
      throw new Error("Timed out waiting for the trading account.");
    }

    async function hasOnchainCode(eth, address) {
      if (!address) return false;
      try {
        const code = await eth.request({
          method: "eth_getCode",
          params: [address, "latest"],
        });
        return Boolean(code && code !== "0x" && code !== "0x0" && String(code).length > 4);
      } catch (err) {
        return false;
      }
    }

    function isAlreadyDeployedError(err) {
      const blob = JSON.stringify(err || {}) + " " + ((err && err.message) || "");
      return /741752c2|already.?deployed|CREATE2/i.test(blob);
    }

    if (createBtn) {
      createBtn.addEventListener("click", async function () {
        createBtn.disabled = true;
        try {
          const owner = await ensureWallet();
          const grant = await loadGrant(owner);
          const { eth } = await ensureWalletForTrade();
          let saAddress = grant.smart_account && grant.smart_account.address;
          if (!saAddress && grant.deploy_tx) {
            saAddress = grant.deploy_tx.predicted_address;
            const already =
              grant.deploy_tx.already_deployed || (await hasOnchainCode(eth, saAddress));
            if (!already) {
              toast("Confirm in MetaMask…", "success");
              try {
                const txHash = await sendWalletTx(eth, grant.deploy_tx, owner);
                toast("Creating your trading account…", "success");
                const receipt = await waitForTxReceipt(eth, txHash, 120000);
                const ok = receipt.status === "0x1" || receipt.status === 1 || receipt.status === "1";
                if (!ok) {
                  throw new Error("Trading account could not be created. Add network fees and try again.");
                }
              } catch (err) {
                if (!(isAlreadyDeployedError(err) && (await hasOnchainCode(eth, saAddress)))) {
                  throw err;
                }
                toast("Trading account already exists — saving it…", "success");
              }
            } else {
              toast("Trading account already exists — saving it…", "success");
            }
          }
          if (!saAddress) {
            throw new Error(
              grant.deploy_error ||
                "Could not create a trading account. Connect MetaMask and retry."
            );
          }
          const res = await csrfFetch("/api/smart-account/", {
            method: "POST",
            body: JSON.stringify({
              owner_address: owner,
              address: saAddress,
              factory_address: grant.framework && grant.framework.simple_factory,
              deploy_salt: grant.deploy_tx && grant.deploy_tx.salt,
            }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || "Create failed");
          toast("Trading account created", "success");
          window.location.reload();
        } catch (err) {
          toast(err.message || "Create failed", "error");
        } finally {
          createBtn.disabled = false;
        }
      });
    }

    if (depositBtn) {
      depositBtn.addEventListener("click", async function () {
        depositBtn.disabled = true;
        try {
          const owner = await ensureWallet();
          const { eth } = await ensureWalletForTrade();
          const grant = await loadGrant(owner);
          const sa = grant.smart_account;
          if (!sa || !sa.address) throw new Error("Create your trading account first");
          const amount = document.getElementById("sa-deposit-amount")?.value || "50";
          const decimals = 6;
          const raw = BigInt(Math.round(Number(amount) * 10 ** decimals));
          const token = grant.framework && grant.framework.collateral;
          if (!token) throw new Error("Collateral token unknown");
          const txHash = await sendWalletTx(
            eth,
            { to: token, data: encodeErc20Transfer(sa.address, raw), value: 0 },
            owner
          );
          const res = await csrfFetch("/api/smart-account/deposit/", {
            method: "POST",
            body: JSON.stringify({ amount: amount, tx_hash: txHash }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || "Deposit failed");
          toast("Trading dollars sent.", "success");
          if (document.body.getAttribute("data-onboarding") === "1") {
            window.location.reload();
          }
        } catch (err) {
          toast(err.message || "Deposit failed", "error");
        } finally {
          depositBtn.disabled = false;
        }
      });
    }

    if (depositGasBtn) {
      depositGasBtn.addEventListener("click", async function () {
        depositGasBtn.disabled = true;
        try {
          const owner = await ensureWallet();
          const { eth } = await ensureWalletForTrade();
          const grant = await loadGrant(owner);
          const sa = grant.smart_account;
          if (!sa || !sa.address) throw new Error("Create your trading account first");
          const amount = document.getElementById("sa-gas-amount")?.value || "0.5";
          const wei = parseUnits(amount, 18);
          toast("Confirm network fee in MetaMask…", "success");
          const txHash = await sendWalletTx(
            eth,
            { to: sa.address, data: "0x", value: "0x" + wei.toString(16) },
            owner
          );
          toast("Adding network fee…", "success");
          const receipt = await waitForTxReceipt(eth, txHash, 120000);
          const ok = receipt.status === "0x1" || receipt.status === 1 || receipt.status === "1";
          if (!ok) {
            throw new Error("Network fee transfer did not go through.");
          }
          toast("Trading account funded for network fees", "success");
          if (document.body.getAttribute("data-onboarding") === "1") {
            window.location.reload();
          }
        } catch (err) {
          toast(err.message || "Gas deposit failed", "error");
        } finally {
          depositGasBtn.disabled = false;
        }
      });
    }

    if (grantBtn) {
      grantBtn.addEventListener("click", async function () {
        grantBtn.disabled = true;
        grantBtn.setAttribute("aria-busy", "true");
        try {
          const owner = await ensureWallet();
          await ensureWalletForTrade();
          const grant = await loadGrant(owner);
          if (!grant.smart_account) throw new Error("Create and fund your trading account first");
          if (!grant.typed_data) throw new Error("Could not prepare the permission. Reconnect MetaMask and try again.");

          const typed = grant.typed_data;
          const signTyped = {
            types: typed.types,
            primaryType: typed.primaryType,
            domain: typed.domain,
            message: typed.message,
          };

          const eth = getEthereumProvider() || window.ethereum;
          if (!eth) throw new Error("No wallet found.");
          const signature = await eth.request({
            method: "eth_signTypedData_v4",
            params: [owner, JSON.stringify(signTyped)],
          });

          const msg = typed.message || {};
          const body = {
            max_trade_amount: document.getElementById("grant-max-trade")?.value || "10",
            max_daily_volume: document.getElementById("grant-max-daily")?.value || "50",
            min_copy_score: document.getElementById("grant-min-score")?.value || "75",
            expires_in_days: document.getElementById("grant-expires")?.value || "30",
            activate: true,
            signed_delegation: {
              delegate: msg.delegate || grant.session_address,
              delegator: msg.delegator || (grant.smart_account && grant.smart_account.address),
              authority: msg.authority || "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
              caveats: (msg.caveats || []).map(function (c) {
                return {
                  enforcer: c.enforcer,
                  terms: c.terms,
                  args: c.args || "0x",
                };
              }),
              salt: msg.salt,
              signature: signature,
              typed_data: signTyped,
            },
          };
          const res = await csrfFetch("/api/agent/grant/", {
            method: "POST",
            body: JSON.stringify(body),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || "Could not save permission");
          toast("DreamLens can trade for you now", "success");
          window.location.href =
            document.body.getAttribute("data-onboarding") === "1" ? "/start/" : "/agent/";
        } catch (err) {
          toast(err.message || "Grant failed", "error");
        } finally {
          grantBtn.disabled = false;
          grantBtn.removeAttribute("aria-busy");
        }
      });
    }

    async function patchStatus(status) {
      const res = await csrfFetch("/api/agent/", {
        method: "PATCH",
        body: JSON.stringify({ status: status }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Update failed");
      window.location.reload();
    }

    if (pauseBtn) {
      pauseBtn.addEventListener("click", function () {
        const pauseModal = document.getElementById("agent-pause-modal");
        const confirm = document.getElementById("agent-pause-confirm");
        if (pauseModal && typeof pauseModal.showModal === "function") {
          if (confirm) {
            confirm.onclick = function () {
              pauseModal.close();
              patchStatus("PAUSED").catch(function (e) { toast(e.message, "error"); });
            };
          }
          pauseModal.showModal();
          return;
        }
        if (window.confirm("Pause Dream Agent? Your funds stay. No new autonomous trades.")) {
          patchStatus("PAUSED").catch(function (e) { toast(e.message, "error"); });
        }
      });
    }
    if (resumeBtn) {
      resumeBtn.addEventListener("click", function () {
        patchStatus("RUNNING").catch(function (e) { toast(e.message, "error"); });
      });
    }
    if (revokeBtn && revokeModal) {
      revokeBtn.addEventListener("click", function () {
        if (typeof revokeModal.showModal === "function") revokeModal.showModal();
        else if (confirm("Revoke Dream Agent?")) doRevoke();
      });
    }
    async function doRevoke() {
      const res = await csrfFetch("/api/agent/revoke/", {
        method: "POST",
        body: "{}",
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Revoke failed");
      toast(data.detail || "Agent revoked", "success");
      window.location.reload();
    }
    if (revokeConfirm) {
      revokeConfirm.addEventListener("click", function (e) {
        e.preventDefault();
        doRevoke().catch(function (err) { toast(err.message, "error"); });
      });
    }
  }

  window.DreamLens = DreamLens;
  window.DreamLens.csrfFetch = csrfFetch;
  window.DreamLens.getCsrfToken = getCsrfToken;
})();
