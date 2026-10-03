// Small progressive enhancements: status polling, chapter seeking, confirmations.
(function () {
  "use strict";

  document.addEventListener("submit", (event) => {
    const form = event.target;
    const button = event.submitter;
    const message = (button && button.dataset.confirm) || form.dataset.confirm;
    if (message && !window.confirm(message)) event.preventDefault();
  });

  document.addEventListener("click", async (event) => {
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const input = document.querySelector(copy.dataset.copy);
      input.select();
      try { await navigator.clipboard.writeText(input.value); } catch (e) { document.execCommand("copy"); }
      copy.textContent = "Kopiert ✓";
      setTimeout(() => { copy.textContent = "Kopieren"; }, 2000);
      return;
    }
    const target = event.target.closest("[data-seek]");
    const player = document.getElementById("player");
    if (target && player) {
      player.currentTime = parseFloat(target.dataset.seek);
      player.play();
    }
  });

  async function fetchJson(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(response.status);
    return response.json();
  }

  // Elapsed time of a running job.
  const elapsed = document.querySelector("[data-elapsed]");
  if (elapsed) {
    const start = new Date(elapsed.dataset.elapsed).getTime();
    const tick = () => {
      const seconds = Math.max(0, Math.floor((Date.now() - start) / 1000));
      elapsed.textContent = `läuft seit ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")} min`;
    };
    tick();
    setInterval(tick, 1000);
  }

  // Episode page: poll while the job is active, reload when it finishes.
  const episode = document.querySelector("[data-episode-status]");
  if (episode && ["queued", "running"].includes(episode.dataset.status)) {
    let lastStage = null;
    const tick = async () => {
      try {
        const job = await fetchJson(episode.dataset.episodeStatus);
        if (!job.active) return window.location.reload();
        if (lastStage !== null && job.stage !== lastStage) return window.location.reload();
        lastStage = job.stage;
        const message = episode.querySelector('[data-field="message"]');
        if (message && job.message) message.textContent = job.message;
        const badge = episode.querySelector('[data-field="status"]');
        if (badge) { badge.textContent = job.status_label; badge.className = "badge " + job.status; }
      } catch (e) { /* keep polling */ }
      setTimeout(tick, 3000);
    };
    setTimeout(tick, 3000);
  }

  // Settings page: check keys and services in the background.
  const checks = document.querySelector("[data-checks]");
  if (checks) {
    const states = ["ok", "warn", "error", "off", "pending"];
    const show = (name, result) => {
      document.querySelectorAll(`[data-check-state="${name}"], [data-check="${name}"], [data-check="${name}"] .check-dot`)
        .forEach((el) => { states.forEach((s) => el.classList.remove(s)); el.classList.add(result.state); });
      document.querySelectorAll(`[data-check="${name}"] [data-check-message]`)
        .forEach((el) => { el.textContent = result.message; });
      const row = document.querySelector(`[data-check-row="${name}"]`);
      if (row) row.title = result.message;
    };
    const load = async (force) => {
      if (force) {
        ["claude", "gemini", "edge", "telegram"].forEach((n) => show(n, { state: "pending", message: "Wird geprüft …" }));
      }
      try {
        const data = await fetchJson(checks.dataset.checks + (force ? "?force=1" : ""));
        Object.entries(data.checks).forEach(([name, result]) => show(name, result));
      } catch (e) { /* page still works without checks */ }
    };
    load(false);
    const button = checks.querySelector("[data-recheck]");
    if (button) button.addEventListener("click", () => load(true));
  }

  // Episode list: update status badges while anything is active.
  const list = document.querySelector("[data-poll]");
  if (list && list.querySelector(".badge.queued, .badge.running")) {
    const tick = async () => {
      try {
        const data = await fetchJson(list.dataset.poll);
        let active = false;
        for (const job of data.jobs) {
          const row = list.querySelector(`[data-job-id="${CSS.escape(job.id)}"]`);
          if (!row) continue;
          const badge = row.querySelector('[data-field="status"]');
          badge.textContent = job.status_label + (job.status === "running" && job.stage_label ? " · " + job.stage_label : "");
          badge.className = "badge " + job.status;
          row.querySelector(".job-title").textContent = job.title;
          active = active || job.active;
        }
        if (!active) return;
      } catch (e) { /* keep polling */ }
      setTimeout(tick, 5000);
    };
    setTimeout(tick, 5000);
  }
})();
