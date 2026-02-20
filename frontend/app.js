(function () {
  "use strict";

  // --- State ---
  const state = {
    projects: [],
    selectedProject: null,
    ws: null,
    wsRetry: null,
    logStream: null,
    terminalWs: null,
    terminal: null,
    fitAddon: null,
    resizeObserver: null,
  };

  // When running inside Tauri, location.protocol is "tauri:" so we need to hardcode the backend URL
  const isTauri = location.protocol === "tauri:" || location.protocol === "https:" && location.host === "tauri.localhost";
  const BASE = isTauri ? "http://127.0.0.1:18093" : `${location.protocol}//${location.host}`;
  const WS_BASE = isTauri ? "ws://127.0.0.1:18093" : `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}`;

  // --- API client ---
  async function api(path, opts = {}) {
    const res = await fetch(`${BASE}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }

  // --- Events WebSocket ---
  function connectEvents() {
    if (state.ws) {
      state.ws.close();
      state.ws = null;
    }
    clearTimeout(state.wsRetry);

    const ws = new WebSocket(`${WS_BASE}/api/ws/events`);
    state.ws = ws;

    ws.onopen = () => {
      setConnectionStatus(true);
    };

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === "state") {
          state.projects = msg.projects;
          renderSidebar();
          if (state.selectedProject) {
            const current = state.projects.find(
              (p) => p.name === state.selectedProject
            );
            if (current) renderContainers(current);
          }
        }
      } catch (err) {
        console.error("WS parse error:", err);
      }
    };

    ws.onclose = () => {
      setConnectionStatus(false);
      state.wsRetry = setTimeout(connectEvents, 3000);
    };

    ws.onerror = () => ws.close();
  }

  function setConnectionStatus(connected) {
    const dot = document.getElementById("connection-dot");
    dot.className = `dot ${connected ? "dot-running" : "dot-stopped"}`;
    dot.title = connected ? "Connected" : "Disconnected";
  }

  // --- UI: Sidebar ---
  function renderSidebar() {
    const list = document.getElementById("project-list");
    list.innerHTML = state.projects
      .map(
        (p) => `
      <li class="${p.name === state.selectedProject ? "active" : ""}"
          data-project="${p.name}">
        <span class="dot dot-${p.status}"></span>
        <div class="project-info">
          <div class="name">${escHtml(p.name === "_standalone" ? "Standalone" : p.name)}</div>
          <div class="count">${p.containers.length} container${p.containers.length !== 1 ? "s" : ""}</div>
        </div>
      </li>`
      )
      .join("");

    list.querySelectorAll("li").forEach((li) => {
      li.addEventListener("click", () => selectProject(li.dataset.project));
    });
  }

  function selectProject(name) {
    state.selectedProject = name;
    document.getElementById("empty-state").classList.add("hidden");
    document.getElementById("project-view").classList.remove("hidden");
    renderSidebar();
    const project = state.projects.find((p) => p.name === name);
    if (project) {
      renderProjectHeader(project);
      renderContainers(project);
    }
  }

  // --- UI: Project header ---
  function renderProjectHeader(project) {
    document.getElementById("project-name").textContent =
      project.name === "_standalone" ? "Standalone Containers" : project.name;
    const dot = document.getElementById("project-status-dot");
    dot.className = `dot dot-${project.status}`;

    const actions = document.getElementById("project-actions");
    if (project.name === "_standalone") {
      actions.innerHTML = "";
      return;
    }
    actions.innerHTML = `
      <button class="btn btn-success btn-sm" data-compose="up">&#9654; Up</button>
      <button class="btn btn-sm" data-compose="restart">&#8635; Restart</button>
      <button class="btn btn-danger btn-sm" data-compose="down">&#9632; Down</button>
    `;
    actions.querySelectorAll("[data-compose]").forEach((btn) => {
      btn.addEventListener("click", () =>
        handleComposeAction(project.name, btn.dataset.compose, btn)
      );
    });
  }

  async function handleComposeAction(project, action, btn) {
    btn.classList.add("loading");
    try {
      await api(`/api/compose/${encodeURIComponent(project)}/${action}`, {
        method: "POST",
      });
    } catch (err) {
      alert(`Compose ${action} failed: ${err.message}`);
    } finally {
      btn.classList.remove("loading");
    }
  }

  // --- UI: Container list ---
  function renderContainers(project) {
    const list = document.getElementById("container-list");
    if (!project.containers.length) {
      list.innerHTML = '<p style="color:var(--text-muted);padding:20px;">No containers</p>';
      return;
    }

    list.innerHTML = project.containers
      .map((c) => {
        const ports = Object.entries(c.ports)
          .map(([cp, hp]) => `${hp}→${cp}`)
          .join(", ");
        const isRunning = c.status === "running";
        return `
        <div class="container-row" data-id="${c.id}">
          <span class="dot dot-${isRunning ? "running" : "stopped"}"></span>
          <div class="container-info">
            <div class="container-name">${escHtml(c.name)}</div>
            <div class="container-meta">${escHtml(c.image)}${ports ? " | " + escHtml(ports) : ""}</div>
          </div>
          <span class="container-status status-${c.status}">${c.status}</span>
          <div class="container-actions">
            ${isRunning
              ? `<button class="btn btn-sm" data-action="stop">Stop</button>
                 <button class="btn btn-sm" data-action="restart">Restart</button>`
              : `<button class="btn btn-success btn-sm" data-action="start">Start</button>`
            }
            <button class="btn btn-sm" data-action="logs" title="Logs">&#128220;</button>
            ${isRunning ? `<button class="btn btn-sm" data-action="exec" title="Shell">&#9002;</button>` : ""}
            <button class="btn btn-danger btn-sm" data-action="remove" title="Remove">&#128465;</button>
          </div>
        </div>`;
      })
      .join("");

    list.querySelectorAll("[data-action]").forEach((btn) => {
      const row = btn.closest(".container-row");
      const id = row.dataset.id;
      const action = btn.dataset.action;
      btn.addEventListener("click", () => handleContainerAction(id, action, btn));
    });
  }

  async function handleContainerAction(id, action, btn) {
    if (action === "logs") return openLogs(id);
    if (action === "exec") return openTerminal(id);
    if (action === "remove") {
      const ok = await confirmDialog(
        "Are you sure you want to remove this container? This action cannot be undone."
      );
      if (!ok) return;
      btn.classList.add("loading");
      try {
        await api(`/api/container/${id}`, { method: "DELETE" });
      } catch (err) {
        alert(`Remove failed: ${err.message}`);
      } finally {
        btn.classList.remove("loading");
      }
      return;
    }

    btn.classList.add("loading");
    try {
      await api(`/api/container/${id}/${action}`, { method: "POST" });
    } catch (err) {
      alert(`${action} failed: ${err.message}`);
    } finally {
      btn.classList.remove("loading");
    }
  }

  // --- Confirm dialog ---
  function confirmDialog(message) {
    return new Promise((resolve) => {
      const modal = document.getElementById("confirm-modal");
      document.getElementById("confirm-message").textContent = message;
      modal.classList.remove("hidden");

      const cleanup = (result) => {
        modal.classList.add("hidden");
        resolve(result);
      };

      document.getElementById("confirm-ok").onclick = () => cleanup(true);
      document.getElementById("confirm-cancel").onclick = () => cleanup(false);
    });
  }

  // --- ANSI to HTML converter ---
  const ANSI_COLORS = {
    30: "#414868", 31: "#f7768e", 32: "#9ece6a", 33: "#e0af68",
    34: "#7aa2f7", 35: "#bb9af7", 36: "#7dcfff", 37: "#c0caf5",
    39: null, // default
    90: "#565f89", 91: "#f7768e", 92: "#9ece6a", 93: "#e0af68",
    94: "#7aa2f7", 95: "#bb9af7", 96: "#7dcfff", 97: "#c0caf5",
  };

  function ansiToHtml(text) {
    // Escape HTML first
    let html = text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

    // Replace ANSI sequences with spans
    // Match: ESC[ followed by params and final letter
    html = html.replace(/\x1b\[([0-9;]*)m/g, (_, codes) => {
      if (!codes || codes === "0" || codes === "") return "</span>";
      const parts = codes.split(";");
      const styles = [];
      for (const p of parts) {
        const n = parseInt(p, 10);
        if (ANSI_COLORS[n] !== undefined) {
          if (ANSI_COLORS[n]) styles.push(`color:${ANSI_COLORS[n]}`);
        } else if (n >= 40 && n <= 47) {
          const bgIdx = n - 10;
          if (ANSI_COLORS[bgIdx]) styles.push(`background:${ANSI_COLORS[bgIdx]}`);
        } else if (n === 1) {
          styles.push("font-weight:bold");
        } else if (n === 3) {
          styles.push("font-style:italic");
        } else if (n === 4) {
          styles.push("text-decoration:underline");
        }
        // Handle 256-color: ESC[38;5;Nm
        if (n === 38 && parts[1] === "5") {
          const c256 = ansi256ToHex(parseInt(parts[2], 10));
          if (c256) styles.push(`color:${c256}`);
          break;
        }
      }
      return styles.length ? `<span style="${styles.join(";")}">` : "</span>";
    });

    // Remove any other remaining escape sequences
    html = html.replace(/\x1b\[[0-9;]*[A-Za-z]/g, "");

    return html;
  }

  function ansi256ToHex(n) {
    if (n < 0 || n > 255) return null;
    // Standard 16 colors
    const base16 = [
      "#414868","#f7768e","#9ece6a","#e0af68","#7aa2f7","#bb9af7","#7dcfff","#c0caf5",
      "#565f89","#f7768e","#9ece6a","#e0af68","#7aa2f7","#bb9af7","#7dcfff","#c0caf5",
    ];
    if (n < 16) return base16[n];
    // 216-color cube (16-231)
    if (n < 232) {
      const idx = n - 16;
      const r = Math.floor(idx / 36);
      const g = Math.floor((idx % 36) / 6);
      const b = idx % 6;
      const toHex = (v) => (v === 0 ? 0 : 55 + v * 40).toString(16).padStart(2, "0");
      return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
    }
    // Grayscale (232-255)
    const gray = 8 + (n - 232) * 10;
    const h = gray.toString(16).padStart(2, "0");
    return `#${h}${h}${h}`;
  }

  // --- Log viewer ---
  function openLogs(containerId) {
    closeLogs();
    const modal = document.getElementById("log-modal");
    const output = document.getElementById("log-output");
    const container = state.projects
      .flatMap((p) => p.containers)
      .find((c) => c.id === containerId);
    document.getElementById("log-title").textContent = `Logs: ${container?.name || containerId}`;
    output.innerHTML = "";
    modal.classList.remove("hidden");

    const evtSource = new EventSource(`${BASE}/api/container/${containerId}/logs`);
    state.logStream = evtSource;

    evtSource.onmessage = (e) => {
      try {
        const line = JSON.parse(e.data);
        output.innerHTML += ansiToHtml(line);
      } catch {
        output.innerHTML += ansiToHtml(e.data + "\n");
      }
      output.scrollTop = output.scrollHeight;
    };

    evtSource.onerror = () => {
      // EventSource will auto-reconnect; if container stopped, close gracefully
    };
  }

  function closeLogs() {
    if (state.logStream) {
      state.logStream.close();
      state.logStream = null;
    }
    document.getElementById("log-modal").classList.add("hidden");
  }

  // --- Terminal ---
  function openTerminal(containerId) {
    closeTerminal();
    const modal = document.getElementById("term-modal");
    const termContainer = document.getElementById("terminal-container");
    const container = state.projects
      .flatMap((p) => p.containers)
      .find((c) => c.id === containerId);
    document.getElementById("term-title").textContent = `Shell: ${container?.name || containerId}`;
    modal.classList.remove("hidden");

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 14,
      fontFamily: '"JetBrains Mono", "Fira Code", monospace',
      theme: {
        background: "#1a1b26",
        foreground: "#c0caf5",
        cursor: "#c0caf5",
        selectionBackground: "#33467c",
        black: "#414868",
        red: "#f7768e",
        green: "#9ece6a",
        yellow: "#e0af68",
        blue: "#7aa2f7",
        magenta: "#bb9af7",
        cyan: "#7dcfff",
        white: "#c0caf5",
      },
    });

    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(termContainer);
    fitAddon.fit();

    state.terminal = term;
    state.fitAddon = fitAddon;

    const ws = new WebSocket(`${WS_BASE}/api/ws/exec/${containerId}`);
    state.terminalWs = ws;

    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      const dims = fitAddon.proposeDimensions();
      if (dims) {
        ws.send(JSON.stringify({ type: "resize", cols: dims.cols, rows: dims.rows }));
      }
    };

    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(e.data));
      } else {
        term.write(e.data);
      }
    };

    ws.onclose = () => {
      term.write("\r\n\x1b[33m[Session ended]\x1b[0m\r\n");
    };

    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(data);
      }
    });

    term.onResize(({ cols, rows }) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "resize", cols, rows }));
      }
    });

    state.resizeObserver = new ResizeObserver(() => {
      try {
        fitAddon.fit();
      } catch {}
    });
    state.resizeObserver.observe(termContainer);
  }

  function closeTerminal() {
    if (state.resizeObserver) {
      state.resizeObserver.disconnect();
      state.resizeObserver = null;
    }
    if (state.terminalWs) {
      state.terminalWs.close();
      state.terminalWs = null;
    }
    if (state.terminal) {
      state.terminal.dispose();
      state.terminal = null;
    }
    state.fitAddon = null;
    document.getElementById("terminal-container").innerHTML = "";
    document.getElementById("term-modal").classList.add("hidden");
  }

  // --- Helpers ---
  function escHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  // --- Init ---
  document.getElementById("log-close").addEventListener("click", closeLogs);
  document.getElementById("term-close").addEventListener("click", closeTerminal);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!document.getElementById("log-modal").classList.contains("hidden")) closeLogs();
      else if (!document.getElementById("term-modal").classList.contains("hidden")) closeTerminal();
      else if (!document.getElementById("confirm-modal").classList.contains("hidden")) {
        document.getElementById("confirm-cancel").click();
      }
    }
  });

  // Initial fetch then connect WS
  api("/api/projects")
    .then((projects) => {
      state.projects = projects;
      renderSidebar();
    })
    .catch((err) => console.error("Initial fetch failed:", err))
    .finally(() => connectEvents());
})();
