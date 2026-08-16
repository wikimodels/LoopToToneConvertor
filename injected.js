(() => {
  if (window.__ltBridgeInstalled) {
    return;
  }
  window.__ltBridgeInstalled = true;

  const report = (token) => {
    try {
      window.postMessage({ source: "lt-appcheck-bridge", type: "appcheck-token", token }, "*");
    } catch (e) {
      /* ignore */
    }
  };

  const tryParse = (url, text) => {
    try {
      if (!url || !/exchangeRecaptcha/i.test(url) || !text || text.length < 40) {
        return;
      }
      const data = JSON.parse(text);
      const token =
        (data && (data.appCheckToken || data.attestationToken || {}).token) ||
        (data && data.token);
      if (typeof token === "string" && token.length > 80) {
        report(token);
      }
    } catch (e) {
      /* not JSON or wrong shape */
    }
  };

  const origFetch = window.fetch;
  if (origFetch && typeof origFetch === "function") {
    window.fetch = function (...args) {
      const res = origFetch.apply(this, args);
      try {
        res.then((r) => {
          try {
            r.clone().text().then((t) => tryParse(r.url, t));
          } catch (e) {
            /* ignore */
          }
        });
      } catch (e) {
        /* ignore */
      }
      return res;
    };
  }

  const XHRopen = XMLHttpRequest.prototype.open;
  const XHRsend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__ltUrl = url;
    return XHRopen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener("load", () => {
      try {
        if (
          this.__ltUrl &&
          /exchangeRecaptcha/i.test(String(this.__ltUrl)) &&
          this.responseText
        ) {
          tryParse(this.__ltUrl, this.responseText);
        }
      } catch (e) {
        /* ignore */
      }
    });
    return XHRsend.apply(this, args);
  };

  window.addEventListener("message", (event) => {
    if (event.source !== window) {
      return;
    }
    if (
      event.data &&
      event.data.source === "lt-appcheck-bridge" &&
      event.data.type === "chrome-extension-token"
    ) {
      try {
        const token = event.data.token;
        report(token);
        window.postMessage(
          { source: "lt-appcheck-bridge", type: "bridge-received" },
          "*"
        );
      } catch (e) {
        /* ignore */
      }
    }
  });

  window.postMessage({ source: "lt-appcheck-bridge", type: "bridge-ready" }, "*");
})();