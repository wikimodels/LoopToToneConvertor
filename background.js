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
      try {
        chrome.action.setBadgeText({ text: "ok" });
      } catch (e) {
        /* popup-only badge, ignore */
      }
      return true;
    }
    try {
      const body = await res.json();
      if (body && body.token && body.token !== token) {
        chrome.storage.local.remove("pendingToken");
      }
    } catch (e) {
      /* ignore */
    }
    retry(token);
    return false;
  } catch (e) {
    retry(token);
    return false;
  }
};

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
});