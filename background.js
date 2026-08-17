const LOCAL = "http://127.0.0.1:8002/api/appcheck-token";

let lastToken = null;

const retry = (token) => {
  chrome.storage.local.set({ pendingToken: token });
};

const deliver = async (token) => {
  try {
    const res = await fetch(LOCAL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token,
        delivered_from: "browser-extension",
        delivered_at: new Date().toISOString(),
      }),
    });
    if (res.ok) {
      lastToken = token;
      chrome.storage.local.remove("pendingToken");
      chrome.storage.local.set({
        bridge: {
          seen_at: Date.now(),
          delivered_at: Date.now(),
          ok: true,
          error: null,
        },
      });
      try {
        chrome.action.setBadgeText({ text: "ok" });
      } catch (e) {
        /* popup-only badge, ignore */
      }
      return true;
    }
    let bodyText;
    try {
      bodyText = await res.text();
    } catch (e) {
      bodyText = "";
    }
    try {
      const body = JSON.parse(bodyText);
      if (body && body.token && body.token !== token) {
        chrome.storage.local.remove("pendingToken");
      }
      chrome.storage.local.set({
        bridge: {
          seen_at: Date.now(),
          delivered_at: null,
          ok: false,
          error: "server replied " + res.status + ": " + (body.error || bodyText),
        },
      });
    } catch (e) {
      chrome.storage.local.set({
        bridge: {
          seen_at: Date.now(),
          delivered_at: null,
          ok: false,
          error: "server replied " + res.status + ": " + bodyText,
        },
      });
    }
    retry(token);
    return false;
  } catch (e) {
    chrome.storage.local.set({
      bridge: {
        seen_at: Date.now(),
        delivered_at: null,
        ok: false,
        error: String(e && e.message || e),
      },
    });
    retry(token);
    return false;
  }
};

const HOST = "com.looptotone.server";

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "appcheck-token" && typeof msg.token === "string") {
    deliver(msg.token);
  }
  if (msg && msg.type === "content-script-loaded") {
    chrome.storage.local.get(["pendingToken"], (data) => {
      if (data && data.pendingToken) {
        deliver(data.pendingToken);
      }
    });
  }
  if (msg && msg.type === "flush-pending") {
    chrome.storage.local.get(["pendingToken"], (data) => {
      if (data && data.pendingToken) {
        deliver(data.pendingToken);
      }
    });
  }
  if (msg && msg.type === "get-appcheck-token") {
    refreshChordminiTab()
      .then((res) => sendResponse(res))
      .catch((e) => sendResponse({ ok: false, error: String(e && e.message || e) }));
    return true;
  }
  if (msg && msg.type === "start-server") {
    let port;
    try {
      port = chrome.runtime.connectNative(HOST);
    } catch (e) {
      sendResponse({ ok: false, error: String(e && e.message || e) });
      return;
    }
    let responded = false;
    port.onMessage.addListener((resp) => {
      if (responded) return;
      responded = true;
      sendResponse(resp || { ok: false, error: "host: empty reply" });
      try {
        port.disconnect();
      } catch (e) { /* ignore */ }
    });
    port.onDisconnect.addListener(() => {
      if (responded) return;
      responded = true;
      const err = chrome.runtime.lastError && chrome.runtime.lastError.message;
      sendResponse({
        ok: false,
        error: err || "native host disconnected (install_host.cmd not run?)",
      });
    });
    port.postMessage({});
    return true;
  }
});

setInterval(() => {
  try {
    chrome.storage.local.get(["pendingToken"], (data) => {
      if (data && data.pendingToken) {
        deliver(data.pendingToken);
      }
    });
  } catch (e) {
    /* ignore */
  }
}, 30000);

chrome.runtime.onInstalled.addListener(() => {
  try {
    chrome.action.setBadgeBackgroundColor({ color: "#2e7d32" });
  } catch (e) {
    /* ignore */
  }
  try {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  } catch (e) {
    /* older browsers: fall back to popup */
  }
});

chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    try {
      const hdrs = details.requestHeaders || [];
      for (const h of hdrs) {
        if (h && h.name && h.name.toLowerCase() === "x-firebase-appcheck") {
          if (typeof h.value === "string" && h.value.length > 80) {
            deliver(h.value);
            break;
          }
        }
      }
    } catch (e) {
      /* ignore */
    }
  },
  {
    urls: [
      "https://chordmini.me/*",
      "https://identitytoolkit.googleapis.com/*",
      "https://firestore.googleapis.com/*",
      "https://firebasestorage.googleapis.com/*",
    ],
  },
  ["requestHeaders"]
);

async function refreshChordminiTab() {
  let tabs = [];
  try {
    tabs = await chrome.tabs.query({ url: ["https://chordmini.me/*"] });
  } catch (e) {
    tabs = [];
  }
  const existing = tabs.find((t) => !t.incognito);
  let tab = existing;
  if (!tab) {
    tab = await chrome.tabs.create({ url: "https://chordmini.me/" });
  } else {
    try {
      await chrome.tabs.update(tab.id, { active: true });
      chrome.tabs.reload(tab.id);
    } catch (e) { /* ignore */ }
  }
  return { ok: true, tab_id: tab.id };
}