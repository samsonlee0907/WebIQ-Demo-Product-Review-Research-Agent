/**
 * Product Due-Diligence Portal Content Script Bridge.
 *
 * Bridges messages between the Product Due-Diligence Portal web page
 * (`window.postMessage`) and the extension's MV3 service worker (`background.js`).
 */
(function () {
  const EXT_VERSION = chrome.runtime.getManifest().version;

  function announceReady() {
    window.postMessage(
      {
        source: "pdd-chrome-extension",
        type: "PDD_EXT_READY",
        version: EXT_VERSION,
      },
      window.location.origin
    );
  }

  // Announce readiness immediately and whenever the portal page pings
  announceReady();
  setTimeout(announceReady, 500);
  setTimeout(announceReady, 1500);

  window.addEventListener("message", (event) => {
    if (event.source !== window || !event.data || typeof event.data !== "object") {
      return;
    }
    if (event.data.source !== "pdd-portal-page") {
      return;
    }

    if (event.data.type === "PDD_EXT_PING") {
      announceReady();
      return;
    }

    if (event.data.type === "PDD_BROWSER_COMMAND" && event.data.command) {
      const cmd = event.data.command;
      chrome.runtime.sendMessage(
        {
          type: "PDD_EXEC_BROWSER_COMMAND",
          command: cmd,
        },
        (response) => {
          const runtimeErr = chrome.runtime.lastError;
          if (runtimeErr) {
            window.postMessage(
              {
                source: "pdd-chrome-extension",
                type: "PDD_BROWSER_OBSERVATION",
                observation: {
                  run_id: cmd.run_id,
                  command_id: cmd.command_id,
                  url: cmd.url || "",
                  title: "",
                  status_code: 0,
                  headings: [],
                  body_excerpt: "",
                  candidate_lines: [],
                  screenshot_data_url: null,
                  error: runtimeErr.message || "Extension service worker error",
                },
              },
              window.location.origin
            );
            return;
          }
          if (response && response.observation) {
            window.postMessage(
              {
                source: "pdd-chrome-extension",
                type: "PDD_BROWSER_OBSERVATION",
                observation: response.observation,
              },
              window.location.origin
            );
          }
        }
      );
    }
  });
})();
