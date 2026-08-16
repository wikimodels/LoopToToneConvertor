(() => {
  const KEY = "lt-appcheck-bridge";

  const inject = () => {
    try {
      const existing = document.getElementById(KEY);
      if (existing) {
        return;
      }
      const script = document.createElement("script");
      script.id = KEY;
      script.src = chrome.runtime.getURL("injected.js");
      (document.head || document.documentElement).appendChild(script);
      script.remove();
    } catch (e) {
      /* ignore */
    }
  };

  inject();

  const reportToken = (token) => {
    try {
      chrome.runtime.sendMessage({ type: "appcheck-token", token });
    } catch (e) {
      /* ignore */
    }
  };

  window.addEventListener("message", (event) => {
    if (event.source !== window) {
      return;
    }
    const data = event.data;
    if (!data || data.source !== KEY) {
      return;
    }
    if (data.type === "appcheck-token" && typeof data.token === "string") {
      reportToken(data.token);
    }
  });

  document.addEventListener("DOMContentLoaded", inject);

  try {
    chrome.runtime.sendMessage({ type: "content-script-loaded" });
  } catch (e) {
    /* ignore */
  }
})();