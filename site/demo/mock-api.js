/*
 * Static demo transport for Agent Sidecar.
 * It replaces fetch completely: no request can leave this page.
 */
(function () {
  "use strict";

  const demoToken = "demo-read-only-token";
  const sessions = [
    {
      agent: "cursor-ide",
      session_id: "demo-landing-42",
      project: "~/Projects/agent-sidecar",
      status: "working",
      title: "Build a local-first landing page"
    },
    {
      agent: "claude",
      session_id: "demo-review-17",
      project: "~/Projects/agent-sidecar",
      status: "waiting",
      title: "Review the HTTP security boundary"
    },
    {
      agent: "codex",
      session_id: "demo-finished-08",
      project: "~/Projects/cli-tools",
      status: "idle",
      title: "Completed packaging verification"
    }
  ];
  const events = [
    {ok: true, op: "subscribe"},
    {
      ts: "2026-08-24T04:18:07Z",
      agent: "cursor-ide",
      session_id: "demo-landing-42",
      kind: "assistant",
      text: "I’ll preserve the local panel boundary and build the demo around it."
    },
    {
      ts: "2026-08-24T04:18:11Z",
      agent: "cursor-ide",
      session_id: "demo-landing-42",
      kind: "tool",
      tool: "ReadFile",
      path: "sidecar/http_server.py"
    },
    {
      ts: "2026-08-24T04:18:16Z",
      agent: "claude",
      session_id: "demo-review-17",
      kind: "assistant",
      text: "The listener remains opt-in, bearer-authenticated, and loopback-only."
    },
    {
      ts: "2026-08-24T04:18:23Z",
      agent: "cursor-ide",
      session_id: "demo-landing-42",
      kind: "tool_result",
      tool: "tests",
      text: "HTTP panel and deterministic site checks passed."
    }
  ];

  function authorized(init) {
    const headers = init && init.headers ? init.headers : {};
    return headers.Authorization === "Bearer " + demoToken;
  }

  function jsonResponse(payload, status) {
    return new Response(JSON.stringify(payload) + "\n", {
      status: status,
      headers: {"Content-Type": "application/json; charset=utf-8"}
    });
  }

  window.fetch = function (input, init) {
    const rawUrl = typeof input === "string" ? input : input.url;
    const path = new URL(rawUrl, window.location.href).pathname;
    if (path !== "/api/v1/status" && path !== "/api/v1/events") {
      return Promise.reject(new TypeError("Static demo blocks all network access"));
    }
    if (!authorized(init)) {
      return Promise.resolve(jsonResponse({ok: false, error: "unauthorized"}, 401));
    }
    if (path === "/api/v1/status") {
      return Promise.resolve(
        jsonResponse({
          ok: true,
          op: "status",
          sessions: sessions,
          scan_errors: [],
          tail_errors: []
        }, 200)
      );
    }
    const body = events.map(function (event) {
      return JSON.stringify(event);
    }).join("\n") + "\n";
    return Promise.resolve(new Response(body, {
      status: 200,
      headers: {"Content-Type": "application/x-ndjson; charset=utf-8"}
    }));
  };

  document.addEventListener("DOMContentLoaded", function () {
    const form = document.getElementById("auth");
    const input = document.getElementById("token");
    const label = document.querySelector('label[for="token"]');
    const message = document.getElementById("message");
    const button = document.getElementById("connect");
    input.type = "text";
    input.value = demoToken;
    input.readOnly = true;
    label.textContent = "Demo token";
    message.textContent = "Synthetic data · read-only · no backend";
    button.textContent = "Replay demo";
    form.addEventListener("submit", function () {
      input.value = demoToken;
    }, true);
    form.requestSubmit();
  });
}());
