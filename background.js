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
    grabAppCheckToken()
      .then(async (res) => {
        if (res.ok && res.token) {
          const delivered = await deliver(res.token);
          sendResponse({ ok: true, delivered, token_len: res.token.length });
        } else {
          sendResponse(res);
        }
      })
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

function grabAppCheckToken() {
  const TIMEOUT = 75000;
  return (async () => {
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
      } catch (e) { /* ignore */ }
    }

    let attached = false;
    try {
      await new Promise((resolve, reject) => {
        chrome.debugger.attach({ tabId: tab.id }, "1.3", () => {
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
          } else {
            attached = true;
            resolve();
          }
        });
      });
    } catch (e) {
      return { ok: false, error: "debugger: " + (e && e.message || e) };
    }

    return await new Promise((resolve) => {
      let done = false;
      const finish = (res) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        if (attached) {
          try {
            chrome.debugger.onEvent.removeListener(onEvent);
            chrome.debugger.detach({ tabId: tab.id });
          } catch (e) { /* ignore */ }
        }
        resolve(res);
      };
      const timer = setTimeout(() => {
        finish({ ok: false, error: "Обмен токеном не замечен за 75 секунд. " +
          "Перезагрузите вкладку chordmini.me и нажмите кнопку ещё раз." });
      }, TIMEOUT);

      const onEvent = (src, method, params) => {
        if (src.tabId !== tab.id) return;
        if (method === "Network.requestWillBeSent") {
          const headers = (params && params.request && params.request.headers) || {};
          for (const key of Object.keys(headers)) {
            if (key.toLowerCase() === "x-firebase-appcheck") {
              const token = headers[key];
              if (typeof token === "string" && token.length > 80) {
                finish({ ok: true, token });
                return;
              }
            }
          }
        }
        if (method === "Network.responseReceived") {
          const url = (params && params.response && params.response.url) || "";
          if (!/exchangeRecaptcha/i.test(url)) return;
          chrome.debugger.sendCommand(
            { tabId: tab.id },
            "Network.getResponseBody",
            { requestId: params.requestId },
            (res) => {
              if (res && res.body) {
                try {
                  const data = JSON.parse(res.body);
                  const token = (data && data.appCheckToken && data.appCheckToken.token) ||
                    (data && data.token);
                  if (typeof token === "string" && token.length > 80) {
                    finish({ ok: true, token });
                  }
                } catch (e) { /* not JSON */ }
              }
            }
          );
        }
      };
      chrome.debugger.onEvent.addListener(onEvent);
      try {
        chrome.tabs.reload(tab.id);
      } catch (e) { /* ignore */ }
    });
  })();
}