"""MCP Apps UI resource for inline Manim render playback."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


MANIM_RENDER_APP_URI = "ui://manim-mcp/render-player.html"
MANIM_RENDER_APP_MIME_TYPE = "text/html;profile=mcp-app"
MANIM_RENDER_ARTIFACT_URI_TEMPLATE = "manim-render://{job_id}/artifact"
MANIM_RENDER_ARTIFACT_MIME_TYPE = "application/octet-stream"


def manim_render_artifact_uri(job_id: str) -> str:
    """Return the MCP resource URI for a render job's primary artifact."""
    return f"manim-render://{job_id}/artifact"


def manim_render_tool_meta() -> dict[str, Any]:
    """Return tool metadata linking render tools to the MCP Apps UI."""
    return {
        "ui": {"resourceUri": MANIM_RENDER_APP_URI},
        # Legacy key kept for older hosts, matching the ext-apps Python example.
        "ui/resourceUri": MANIM_RENDER_APP_URI,
    }


def manim_render_resource_meta() -> dict[str, Any]:
    """Return UI resource metadata used by MCP Apps hosts."""
    return deepcopy(
        {
            "ui": {
                "csp": {
                    # Render media is served from a per-session loopback server
                    # on an ephemeral port.
                    "resourceDomains": [
                        "http://127.0.0.1:*",
                        "http://localhost:*",
                    ],
                },
                "prefersBorder": True,
            }
        }
    )


def manim_render_app_html() -> str:
    """Return the self-contained HTML view for MCP Apps-capable hosts."""
    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Manim Render Player</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #0f1115;
      --panel: #171a21;
      --text: #f6f7fb;
      --muted: #aab4c0;
      --line: rgba(255,255,255,0.12);
      --accent: #7cc7ff;
      --error: #ffb4a8;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; min-width: 280px; background: transparent; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      padding: 12px;
    }
    main {
      background: var(--bg);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 {
      margin: 0;
      font-size: 15px;
      line-height: 1.25;
      font-weight: 650;
      overflow-wrap: anywhere;
    }
    .job {
      color: var(--muted);
      font-size: 11px;
      line-height: 1.2;
      white-space: nowrap;
    }
    .stage {
      padding: 12px;
    }
    video, img {
      display: none;
      width: 100%;
      max-height: 70vh;
      background: #000;
      border-radius: 6px;
    }
    .empty {
      min-height: 180px;
      display: grid;
      place-items: center;
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 6px;
      padding: 18px;
      text-align: center;
      font-size: 13px;
      line-height: 1.45;
    }
    .empty.error {
      color: var(--error);
      border-color: rgba(255,180,168,0.42);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 0 12px 12px;
    }
    a {
      color: var(--accent);
      text-decoration: none;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px 9px;
      font-size: 12px;
      line-height: 1.2;
      max-width: 100%;
      overflow-wrap: anywhere;
    }
    a:hover { border-color: rgba(124,199,255,0.7); }
    .meta {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      padding: 0 12px 12px;
      overflow-wrap: anywhere;
    }
    [hidden] { display: none !important; }
    @media (prefers-color-scheme: light) {
      :root {
        --bg: #f7f8fb;
        --panel: #ffffff;
        --text: #111827;
        --muted: #5d6978;
        --line: rgba(17,24,39,0.14);
        --accent: #006fba;
        --error: #9f2d20;
      }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1 id="title">Manim render</h1>
      <div class="job" id="job"></div>
    </header>
    <section class="stage">
      <video id="video" controls playsinline preload="metadata"></video>
      <img id="image" alt="Manim render">
      <div id="empty" class="empty">Waiting for a render result.</div>
    </section>
    <nav class="actions" id="actions" hidden></nav>
    <div class="meta" id="meta" hidden></div>
  </main>
  <script>
    const pending = new Map();
    let nextId = 1;
    let initialized = false;
    let hostCanOpenLinks = false;
    let currentObjectUrl = null;

    const titleEl = document.getElementById("title");
    const jobEl = document.getElementById("job");
    const videoEl = document.getElementById("video");
    const imageEl = document.getElementById("image");
    const emptyEl = document.getElementById("empty");
    const actionsEl = document.getElementById("actions");
    const metaEl = document.getElementById("meta");

    function send(message) {
      window.parent.postMessage({ jsonrpc: "2.0", ...message }, "*");
    }

    function request(method, params) {
      const id = nextId++;
      send({ id, method, params });
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
      });
    }

    function notify(method, params = {}) {
      send({ method, params });
    }

    function setText(el, value) {
      el.textContent = value || "";
    }

    function showEmpty(message, isError = false) {
      videoEl.style.display = "none";
      imageEl.style.display = "none";
      videoEl.removeAttribute("src");
      imageEl.removeAttribute("src");
      emptyEl.hidden = false;
      emptyEl.classList.toggle("error", isError);
      setText(emptyEl, message);
    }

    function showLoading(message) {
      showEmpty(message, false);
      emptyEl.classList.remove("error");
    }

    function revokeObjectUrl() {
      if (!currentObjectUrl) return;
      URL.revokeObjectURL(currentObjectUrl);
      currentObjectUrl = null;
    }

    function setActions(links) {
      actionsEl.replaceChildren();
      for (const link of links) {
        if (!link.href) continue;
        const anchor = document.createElement("a");
        anchor.href = link.href;
        anchor.target = "_blank";
        anchor.rel = "noreferrer";
        anchor.textContent = link.label;
        anchor.addEventListener("click", (event) => {
          if (!initialized || !hostCanOpenLinks) return;
          event.preventDefault();
          request("ui/open-link", { url: link.href }).catch((error) => {
            console.error("Open link failed:", error);
          });
        });
        actionsEl.appendChild(anchor);
      }
      actionsEl.hidden = actionsEl.childElementCount === 0;
    }

    function firstTextContent(result) {
      return (result.content || [])
        .filter((item) => item && item.type === "text" && typeof item.text === "string")
        .map((item) => item.text)
        .join("\n\n");
    }

    function blobFromBase64(base64, mimeType) {
      const binary = atob(base64);
      const chunks = [];
      for (let offset = 0; offset < binary.length; offset += 65536) {
        const slice = binary.slice(offset, offset + 65536);
        const bytes = new Uint8Array(slice.length);
        for (let i = 0; i < slice.length; i += 1) {
          bytes[i] = slice.charCodeAt(i);
        }
        chunks.push(bytes);
      }
      return new Blob(chunks, { type: mimeType });
    }

    async function readArtifactObjectUrl(uri, mimeType) {
      const result = await request("resources/read", { uri });
      const content = result && result.contents && result.contents[0];
      if (!content || typeof content.blob !== "string") {
        throw new Error("Artifact resource did not return blob data.");
      }
      const blob = blobFromBase64(content.blob, mimeType || content.mimeType || "application/octet-stream");
      return URL.createObjectURL(blob);
    }

    function showVideo(src) {
      emptyEl.hidden = true;
      imageEl.style.display = "none";
      imageEl.removeAttribute("src");
      videoEl.src = src;
      videoEl.style.display = "block";
      videoEl.load();
      reportSize();
    }

    function showImage(src) {
      emptyEl.hidden = true;
      videoEl.style.display = "none";
      videoEl.removeAttribute("src");
      imageEl.src = src;
      imageEl.style.display = "block";
      reportSize();
    }

    async function renderToolResult(result) {
      const data = result.structuredContent || {};
      const access = data.access || {};
      const sceneName = data.scene_name || data.sceneName || "Manim render";
      const jobId = data.job_id || data.jobId || "";
      const mimeType = access.video_mime_type || "";
      const mediaUrl = access.video_stream_url || access.video_file_uri || "";
      const resourceUri = access.video_resource_uri || "";
      const text = firstTextContent(result);
      const isError = Boolean(result.isError || data.error);

      setText(titleEl, sceneName);
      setText(jobEl, jobId);

      setActions([
        { label: "File URI", href: access.video_file_uri },
      ]);

      if (data.error) {
        metaEl.hidden = false;
        setText(metaEl, data.error);
      } else if (access.video_path) {
        metaEl.hidden = false;
        setText(metaEl, access.video_path);
      } else {
        metaEl.hidden = true;
        setText(metaEl, "");
      }

      if (!mediaUrl) {
        showEmpty(data.error || text || "No render artifact was returned.", isError);
        reportSize();
        return;
      }

      revokeObjectUrl();
      if (mimeType.startsWith("image/") || /\.(png|jpg|jpeg|gif|webp|svg)$/i.test(mediaUrl)) {
        if (resourceUri) {
          showLoading("Loading render into the embedded player.");
          try {
            currentObjectUrl = await readArtifactObjectUrl(resourceUri, mimeType);
            showImage(currentObjectUrl);
            return;
          } catch (error) {
            console.error("Artifact resource load failed, falling back to image URL:", error);
          }
        }
        showImage(mediaUrl);
        return;
      }

      if (resourceUri) {
        showLoading("Loading render into the embedded player.");
        try {
          currentObjectUrl = await readArtifactObjectUrl(resourceUri, mimeType);
          showVideo(currentObjectUrl);
          return;
        } catch (error) {
          console.error("Artifact resource load failed, falling back to stream URL:", error);
        }
      }

      showVideo(mediaUrl);
    }

    function applyHostContext(ctx) {
      const insets = ctx && ctx.safeAreaInsets;
      if (!insets) return;
      document.body.style.paddingTop = `${12 + (insets.top || 0)}px`;
      document.body.style.paddingRight = `${12 + (insets.right || 0)}px`;
      document.body.style.paddingBottom = `${12 + (insets.bottom || 0)}px`;
      document.body.style.paddingLeft = `${12 + (insets.left || 0)}px`;
    }

    function reportSize() {
      if (!initialized) return;
      requestAnimationFrame(() => {
        const width = Math.ceil(document.documentElement.scrollWidth);
        const height = Math.ceil(document.documentElement.scrollHeight);
        notify("ui/notifications/size-changed", { width, height });
      });
    }

    window.addEventListener("message", (event) => {
      const message = event.data;
      if (!message || message.jsonrpc !== "2.0") return;

      if (message.id && pending.has(message.id)) {
        const waiter = pending.get(message.id);
        pending.delete(message.id);
        if (message.error) waiter.reject(message.error);
        else waiter.resolve(message.result);
        return;
      }

      if (message.method === "ui/notifications/tool-result") {
        renderToolResult(message.params || {}).catch((error) => {
          console.error("Render result handling failed:", error);
          showEmpty("Unable to load this render in the embedded player.", true);
        });
      } else if (message.method === "ui/notifications/tool-cancelled") {
        showEmpty((message.params && message.params.reason) || "Render was cancelled.", true);
        reportSize();
      } else if (message.method === "ui/notifications/host-context-changed") {
        applyHostContext(message.params || {});
        reportSize();
      }
    });

    videoEl.addEventListener("loadedmetadata", reportSize);
    videoEl.addEventListener("error", () => {
      showEmpty("This render could not be loaded in the embedded player. The file path is shown below.", true);
      reportSize();
    });
    imageEl.addEventListener("load", reportSize);
    imageEl.addEventListener("error", () => {
      showEmpty("This image could not be loaded in the embedded player. The file path is shown below.", true);
      reportSize();
    });

    async function connect() {
      try {
        const result = await request("ui/initialize", {
          protocolVersion: "2026-01-26",
          appInfo: { name: "Manim Render Player", version: "1.0.0" },
          appCapabilities: {},
        });
        hostCanOpenLinks = Boolean(result && result.hostCapabilities && result.hostCapabilities.openLinks);
        applyHostContext(result && result.hostContext);
        notify("ui/notifications/initialized");
        initialized = true;
        if ("ResizeObserver" in window) {
          new ResizeObserver(reportSize).observe(document.body);
        }
        reportSize();
      } catch (error) {
        console.error("MCP Apps initialization failed:", error);
        showEmpty("Unable to initialize the inline player.", true);
      }
    }

    connect();
  </script>
</body>
</html>
"""


def manim_render_app_resource() -> str:
    """Inline render player for MCP Apps-capable hosts."""
    return manim_render_app_html()


__all__ = [
    "MANIM_RENDER_APP_MIME_TYPE",
    "MANIM_RENDER_APP_URI",
    "MANIM_RENDER_ARTIFACT_MIME_TYPE",
    "MANIM_RENDER_ARTIFACT_URI_TEMPLATE",
    "manim_render_artifact_uri",
    "manim_render_app_html",
    "manim_render_app_resource",
    "manim_render_resource_meta",
    "manim_render_tool_meta",
]
