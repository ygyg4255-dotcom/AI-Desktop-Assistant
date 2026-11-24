// A.L.T.R.O.N Sci-Fi HUD Client Script
(function () {
  'use strict';

  // Per-launch HUD auth token, injected by ui_server.py into index.html.
  const tokenMeta = document.querySelector('meta[name="hud-token"]');
  const HUD_TOKEN = tokenMeta ? tokenMeta.getAttribute('content') : '';

  // DOM Elements
  const clockEl = document.getElementById('hud-clock');
  const dateEl = document.getElementById('hud-date');
  const greetingTitleEl = document.getElementById('greeting-title');
  const batteryValEl = document.getElementById('battery-value');

  // Stats DOM Elements
  const cpuValEl = document.getElementById('cpu-value');
  const cpuBarEl = document.getElementById('cpu-bar');
  const tempValEl = document.getElementById('temp-value');
  const tempBarEl = document.getElementById('temp-bar');
  const ramValEl = document.getElementById('ram-value');
  const ramBarEl = document.getElementById('ram-bar');
  const diskValEl = document.getElementById('disk-value');
  const diskBarEl = document.getElementById('disk-bar');

  // Core & Command DOM Elements
  const coreStatusTextEl = document.getElementById('core-status-text');
  const commandFormEl = document.getElementById('command-form');
  const commandInputEl = document.getElementById('command-input');
  const btnClearInputEl = document.getElementById('btn-clear-input');

  // Conversation DOM Elements
  const chatMessagesEl = document.getElementById('chat-messages');
  const btnClearChatEl = document.getElementById('btn-clear-chat');
  const btnExportChatEl = document.getElementById('btn-export-chat');

  let currentLogs = [];
  let lastLogsSignature = '';

  // 1. Clock, Date, and Greeting Handlers
  function updateClockAndGreeting() {
    const now = new Date();

    // Time: 12-hour format with seconds (e.g. 11:54:25 PM)
    if (clockEl) {
      clockEl.textContent = now.toLocaleTimeString('en-US', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: true,
      });
    }

    // Date: e.g. November 17, 2026
    if (dateEl) {
      dateEl.textContent = now.toLocaleDateString('en-US', {
        month: 'long',
        day: 'numeric',
        year: 'numeric',
      });
    }

    // Dynamic Time-of-Day Greeting
    if (greetingTitleEl) {
      const hour = now.getHours();
      let greeting = 'Good Evening Sir';
      if (hour >= 5 && hour < 12) {
        greeting = 'Good Morning Sir';
      } else if (hour >= 12 && hour < 17) {
        greeting = 'Good Afternoon Sir';
      }
      if (greetingTitleEl.textContent !== greeting) {
        greetingTitleEl.textContent = greeting;
      }
    }
  }

  setInterval(updateClockAndGreeting, 1000);
  updateClockAndGreeting();

  // Helper: Format Time for Chat Bubbles (e.g. 11:54 PM)
  function formatBubbleTime() {
    return new Date().toLocaleTimeString('en-US', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: true,
    });
  }

  // Silent toast for scheduler events; each toast id is shown once (first poll only records it).
  let lastToastId = null;
  function showToast(toast) {
    if (!toast) return;
    if (lastToastId === null) { lastToastId = toast.id; return; }
    if (toast.id === lastToastId) return;
    lastToastId = toast.id;
    const el = document.createElement('div');
    el.className = 'altron-toast';
    el.textContent = toast.text;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 8000);
  }

  // 2. Fetch Status and Live Hardware Metrics
  async function fetchStatus() {
    try {
      const response = await fetch('/status?t=' + Date.now(), {
        cache: 'no-store',
        headers: { Accept: 'application/json', 'X-HUD-Token': HUD_TOKEN },
      });

      if (!response.ok) return;

      const data = await response.json();
      failures = 0;
      if ((data.state || '').trim().toLowerCase() === 'offline') {
        goOffline();
        return;
      }
      renderState(data.state || 'Idle');
      renderMetrics(data.metrics || data);
      renderLogs(data.logs || []);
      showToast(data.toast);

      if (data.ai_config) {
        updateAiConfigDisplay(data.ai_config);
      }
    } catch (err) {
      // Server unreachable: after a few misses assume the backend is gone.
      if (++failures >= 3) goOffline();
    }
  }

  let failures = 0;
  let offlineShown = false;
  function goOffline() {
    if (!offlineShown) {
      offlineShown = true;
      document.body.className = 'state-offline';
      if (window.AltronCore) window.AltronCore.setState('offline');
      document.title = 'A.L.T.R.O.N - Offline';
      if (coreStatusTextEl) coreStatusTextEl.textContent = 'Offline';
    }
    try { window.close(); } catch (e) { /* tab not script-closable; stays on Offline */ }
  }

  // 3. Render State Accents & Subtitle
  function renderState(rawState) {
    const state = rawState.trim();
    const stateLower = state.toLowerCase();
    let normalized = 'idle';
    let subtitle = 'System Standby (Say "Altron")';

    if (stateLower.includes('listen')) {
      normalized = 'listening';
      subtitle = 'Listening for commands...';
    } else if (stateLower.includes('think')) {
      normalized = 'thinking';
      subtitle = 'Analyzing & executing request...';
    } else if (stateLower.includes('speak')) {
      normalized = 'speaking';
      subtitle = 'Speaking response...';
    }

    document.body.className = `state-${normalized}`;
    if (window.AltronCore) window.AltronCore.setState(normalized);
    if (coreStatusTextEl && coreStatusTextEl.textContent !== subtitle) {
      coreStatusTextEl.textContent = subtitle;
    }
  }

  // 4. Render Live Hardware Stats
  function renderMetrics(m) {
    if (!m) return;

    // CPU Usage
    if (m.cpu_percent !== undefined) {
      const cpu = Math.min(100, Math.max(0, parseFloat(m.cpu_percent) || 0));
      if (cpuValEl) cpuValEl.textContent = `${cpu.toFixed(1)} %`;
      if (cpuBarEl) cpuBarEl.style.width = `${cpu}%`;
    }

    // CPU Temp / Frequency Fallback
    if (m.cpu_temp !== undefined) {
      const temp = parseFloat(m.cpu_temp) || 0;
      if (tempValEl) tempValEl.textContent = `${temp.toFixed(1)} °C`;
      if (tempBarEl) tempBarEl.style.width = `${Math.min(100, (temp / 100) * 100)}%`;
    } else if (m.cpu_freq_mhz) {
      if (tempValEl) tempValEl.textContent = `${m.cpu_freq_mhz} MHz`;
      if (tempBarEl) tempBarEl.style.width = '50%';
    }

    // RAM Usage
    if (m.ram_used_gb !== undefined && m.ram_total_gb !== undefined) {
      const ramPct = Math.min(100, Math.max(0, parseFloat(m.ram_percent) || 0));
      if (ramValEl) ramValEl.textContent = `${m.ram_used_gb} / ${m.ram_total_gb} GB`;
      if (ramBarEl) ramBarEl.style.width = `${ramPct}%`;
    }

    // Disk Usage
    if (m.disk_used_gb !== undefined && m.disk_total_gb !== undefined) {
      const diskPct = Math.min(100, Math.max(0, parseFloat(m.disk_percent) || 0));
      if (diskValEl) diskValEl.textContent = `${m.disk_used_gb} / ${m.disk_total_gb} GB`;
      if (diskBarEl) diskBarEl.style.width = `${diskPct}%`;
    }

    // Battery
    if (m.battery_percent !== undefined && batteryValEl) {
      const plug = m.battery_plugged ? '⚡' : '🔋';
      batteryValEl.textContent = `${plug} ${m.battery_percent}%`;
    }
  }

  // 5. Render Deduplicated Conversation Messages Feed
  function renderLogs(logs) {
    if (!chatMessagesEl) return;
    currentLogs = logs;

    const signature = logs.join('|||');
    if (signature === lastLogsSignature) return;
    lastLogsSignature = signature;

    if (!logs || logs.length === 0) {
      chatMessagesEl.innerHTML = `
        <div class="msg-bubble system">
          <span class="bubble-sender">[SYSTEM]</span>
          <span class="bubble-text">Altron is online. Listening for voice and commands.</span>
        </div>`;
      return;
    }

    chatMessagesEl.innerHTML = '';
    const nowTime = formatBubbleTime();

    logs.forEach((entry) => {
      const trimmed = entry.trim();
      if (!trimmed) return;

      const bubble = document.createElement('div');
      bubble.className = 'msg-bubble';

      const timeSpan = document.createElement('span');
      timeSpan.className = 'bubble-time';
      timeSpan.textContent = nowTime;

      const lower = trimmed.toLowerCase();
      if (
        lower.startsWith('user:') ||
        lower.startsWith('voice:') ||
        lower.startsWith('user said:') ||
        lower.startsWith('telegram:')
      ) {
        bubble.classList.add('user');
        const colonIdx = trimmed.indexOf(':');
        const sender = trimmed.slice(0, colonIdx);
        const text = trimmed.slice(colonIdx + 1).trim();

        bubble.innerHTML = `<span class="bubble-sender">${sender}:</span> <span class="bubble-text">${escapeHtml(text)}</span>`;
      } else if (lower.startsWith('altron:')) {
        bubble.classList.add('altron');
        const text = trimmed.slice(7).trim();
        bubble.innerHTML = `<span class="bubble-sender">Altron:</span> <span class="bubble-text">${escapeHtml(text)}</span>`;
      } else {
        bubble.classList.add('system');
        bubble.innerHTML = `<span class="bubble-text">${escapeHtml(trimmed)}</span>`;
      }

      bubble.appendChild(timeSpan);
      chatMessagesEl.appendChild(bubble);
    });

    chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
  }

  function escapeHtml(str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // 6. Interactive Command Submission via Text Input
  if (commandFormEl && commandInputEl) {
    commandFormEl.addEventListener('submit', async function (e) {
      e.preventDefault();
      const cmd = commandInputEl.value.trim();
      if (!cmd) return;

      commandInputEl.value = '';

      // Optimistically append user message to chat feed
      if (chatMessagesEl) {
        const bubble = document.createElement('div');
        bubble.className = 'msg-bubble user';
        bubble.innerHTML = `
          <span class="bubble-sender">User:</span>
          <span class="bubble-text">${escapeHtml(cmd)}</span>
          <span class="bubble-time">${formatBubbleTime()}</span>
        `;
        chatMessagesEl.appendChild(bubble);
        chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
      }

      // Immediately switch state display to Thinking
      renderState('Thinking');

      try {
        await fetch('/command', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-HUD-Token': HUD_TOKEN,
          },
          body: JSON.stringify({ command: cmd }),
        });
      } catch (err) {
        console.error('Command submission error:', err);
      }
    });
  }

  // Clear Input Button
  if (btnClearInputEl && commandInputEl) {
    btnClearInputEl.addEventListener('click', function () {
      commandInputEl.value = '';
      commandInputEl.focus();
    });
  }

  // 7. Clear Conversation Logs
  if (btnClearChatEl) {
    btnClearChatEl.addEventListener('click', async function () {
      try {
        await fetch('/clear_logs', { method: 'POST', headers: { 'X-HUD-Token': HUD_TOKEN } });
        if (chatMessagesEl) {
          chatMessagesEl.innerHTML = `
            <div class="msg-bubble system">
              <span class="bubble-sender">[SYSTEM]</span>
              <span class="bubble-text">Conversation history cleared.</span>
            </div>`;
        }
        lastLogsSignature = '';
      } catch (err) {
        console.error('Clear logs error:', err);
      }
    });
  }

  // 8. Export Chat as .txt File
  if (btnExportChatEl) {
    btnExportChatEl.addEventListener('click', function () {
      const logsToExport = currentLogs.length > 0 ? currentLogs : ['[SYSTEM] No conversation logs recorded.'];
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
      const content = `A.L.T.R.O.N // Conversation Log Export\nExported: ${new Date().toLocaleString()}\n` +
        `============================================================\n\n` +
        logsToExport.join('\n\n') +
        `\n\n============================================================\n`;

      const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `altron_chat_${timestamp}.txt`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    });
  }

  // 9. AI Configuration Modal & Hot-Reloading
  const settingsModalEl = document.getElementById('settings-modal');
  const btnSettingsEl = document.getElementById('btn-settings');
  const cardAiConfigEl = document.getElementById('card-ai-config');
  const btnCloseModalEl = document.getElementById('btn-close-modal');
  const btnCancelSettingsEl = document.getElementById('btn-cancel-settings');
  const settingsFormEl = document.getElementById('settings-form');
  const cfgModelEl = document.getElementById('cfg-model');
  const cfgApiKeyEl = document.getElementById('cfg-api-key');
  const cfgLanguageEl = document.getElementById('cfg-language');
  const cfgProviderEl = document.getElementById('cfg-provider');
  const cfgBaseUrlEl = document.getElementById('cfg-base-url');
  const cfgKeyGroupEl = document.getElementById('cfg-key-group');
  const cfgUrlGroupEl = document.getElementById('cfg-url-group');
  const cfgKeyLabelEl = document.getElementById('cfg-key-label');

  // Per-provider form hints. Saved values per provider come from /config -> cfg.providers.
  const PROVIDERS = {
    anthropic: { label: 'Anthropic', model: 'claude-sonnet-4-5', key: 'sk-ant-...', url: null },
    openai: { label: 'OpenAI', model: 'gpt-4o-mini', key: 'sk-...', url: null },
    gemini: { label: 'Gemini', model: 'gemini-2.5-flash', key: 'AIza...', url: null },
    ollama: { label: 'Ollama', model: 'qwen2.5-coder:3b', key: null, url: 'http://127.0.0.1:11434' },
    custom: { label: 'Custom', model: 'model-name', key: 'API key', url: 'https://api.groq.com/openai/v1 or https://openrouter.ai/api/v1' },
  };
  let cfgProviders = {};  // last /config summary: switching the dropdown shows each provider's saved values

  function renderProviderFields() {
    const meta = PROVIDERS[cfgProviderEl.value];
    const saved = cfgProviders[cfgProviderEl.value] || {};
    cfgModelEl.value = saved.model || '';
    cfgModelEl.placeholder = 'e.g. ' + meta.model;
    cfgKeyGroupEl.hidden = !meta.key;
    cfgKeyLabelEl.textContent = meta.label + ' API Key';
    cfgApiKeyEl.value = '';
    cfgApiKeyEl.placeholder = saved.has_api_key ? 'Key configured (leave blank to keep)' : (meta.key || '');
    cfgUrlGroupEl.hidden = !meta.url;
    cfgBaseUrlEl.value = saved.base_url || '';
    cfgBaseUrlEl.placeholder = meta.url || '';
  }
  if (cfgProviderEl) cfgProviderEl.addEventListener('change', renderProviderFields);

  function providerPayload() {
    const p = cfgProviderEl.value;
    return {
      provider: p,
      model: cfgModelEl.value.trim(),
      api_key: cfgApiKeyEl.value.trim(),
      base_url: PROVIDERS[p].url ? cfgBaseUrlEl.value.trim() : '',
    };
  }
  const btnSaveSettingsEl = document.getElementById('btn-save-settings');
  const btnTestSettingsEl = document.getElementById('btn-test-settings');
  const testStatusEl = document.getElementById('test-connection-status');
  const displayAiEngineEl = document.getElementById('display-ai-engine');
  const displayAiLangEl = document.getElementById('display-ai-lang');

  function updateAiConfigDisplay(cfg) {
    if (!cfg) return;
    if (displayAiEngineEl) {
      const label = (PROVIDERS[cfg.provider] || PROVIDERS.anthropic).label.toUpperCase();
      displayAiEngineEl.textContent = `${label} (${cfg.model || '-'})`;
    }
    if (displayAiLangEl && cfg.language) {
      displayAiLangEl.textContent = cfg.language;
    }
  }

  async function openSettingsModal() {
    if (!settingsModalEl) return;
    settingsModalEl.style.display = 'flex';
    if (testStatusEl) {
      testStatusEl.style.display = 'none';
      testStatusEl.textContent = '';
    }

    try {
      const resp = await fetch('/config?t=' + Date.now(), { headers: { 'X-HUD-Token': HUD_TOKEN } });
      if (resp.ok) {
        const cfg = await resp.json();
        cfgProviders = cfg.providers || {};
        cfgProviderEl.value = PROVIDERS[cfg.provider] ? cfg.provider : 'anthropic';
        renderProviderFields();
        if (cfgLanguageEl) cfgLanguageEl.value = cfg.language || 'English';
        renderPinState(!!cfg.pin_enabled);
        pinFeedback('', true);
      }
    } catch (err) {
      console.error('Failed to load settings:', err);
    }
  }

  // Approval PIN management (/config/pin). Raw PINs live only in the inputs and the request body.
  const pinEl = (id) => document.getElementById(id);
  const PIN_MSG = {
    saved: 'PIN saved',
    changed: 'PIN changed',
    disabled: 'PIN disabled',
    mismatch: 'PINs do not match',
    format: 'PIN must be 4-12 characters',
    wrong: 'Wrong current PIN',
    locked: 'Too many attempts, wait a minute',
    failed: 'Could not save',
  };

  function renderPinState(enabled) {
    const set = pinEl('pin-set-block'), active = pinEl('pin-active-block'), badge = pinEl('pin-badge');
    if (!set || !active || !badge) return;
    set.hidden = enabled;
    active.hidden = !enabled;
    badge.hidden = !enabled;
    ['pin-new-set', 'pin-confirm-set', 'pin-current', 'pin-new'].forEach((id) => { pinEl(id).value = ''; });
  }

  function pinFeedback(text, ok) {
    const el = pinEl('pin-feedback');
    if (!el) return;
    el.textContent = text;
    el.className = 'pin-feedback ' + (ok ? 'ok' : 'err');
  }

  async function pinRequest(action, currentPin, newPin, okMsg) {
    try {
      const resp = await fetch('/config/pin', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-HUD-Token': HUD_TOKEN },
        body: JSON.stringify({ action: action, current_pin: currentPin, new_pin: newPin }),
      });
      if (resp.ok) {
        const data = await resp.json();
        renderPinState(!!data.pin_enabled);
        pinFeedback(okMsg, true);
      } else {
        pinFeedback(resp.status === 403 ? PIN_MSG.wrong : resp.status === 429 ? PIN_MSG.locked
          : resp.status === 400 ? PIN_MSG.format : PIN_MSG.failed, false);
      }
    } catch (err) {
      pinFeedback(PIN_MSG.failed, false);
    }
  }

  if (pinEl('btn-pin-save')) {
    pinEl('btn-pin-save').addEventListener('click', () => {
      const pin = pinEl('pin-new-set').value;
      if (pin !== pinEl('pin-confirm-set').value) return pinFeedback(PIN_MSG.mismatch, false);
      pinRequest('set', '', pin, PIN_MSG.saved);
    });
    pinEl('btn-pin-change').addEventListener('click', () =>
      pinRequest('change', pinEl('pin-current').value, pinEl('pin-new').value, PIN_MSG.changed));
    pinEl('btn-pin-disable').addEventListener('click', () =>
      pinRequest('disable', pinEl('pin-current').value, '', PIN_MSG.disabled));
  }

  function closeSettingsModal() {
    if (settingsModalEl) {
      settingsModalEl.style.display = 'none';
    }
  }

  if (btnSettingsEl) {
    btnSettingsEl.addEventListener('click', openSettingsModal);
  }
  if (cardAiConfigEl) {
    cardAiConfigEl.addEventListener('click', openSettingsModal);
  }
  if (btnCloseModalEl) {
    btnCloseModalEl.addEventListener('click', closeSettingsModal);
  }
  if (btnCancelSettingsEl) {
    btnCancelSettingsEl.addEventListener('click', closeSettingsModal);
  }

  // Handle saving configuration
  if (settingsFormEl) {
    settingsFormEl.addEventListener('submit', async function (e) {
      e.preventDefault();

      const payload = { ...providerPayload(), language: cfgLanguageEl ? cfgLanguageEl.value : 'English' };

      const origBtnText = btnSaveSettingsEl ? btnSaveSettingsEl.textContent : 'SAVE & APPLY';
      if (btnSaveSettingsEl) {
        btnSaveSettingsEl.textContent = 'SAVING...';
        btnSaveSettingsEl.disabled = true;
      }

      try {
        const resp = await fetch('/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-HUD-Token': HUD_TOKEN },
          body: JSON.stringify(payload),
        });

        if (resp.ok) {
          const updated = await resp.json();
          updateAiConfigDisplay(updated);
          if (btnSaveSettingsEl) btnSaveSettingsEl.textContent = 'SAVED ✓';
          setTimeout(() => {
            closeSettingsModal();
            if (btnSaveSettingsEl) {
              btnSaveSettingsEl.textContent = origBtnText;
              btnSaveSettingsEl.disabled = false;
            }
          }, 500);
        } else {
          throw new Error('Save failed');
        }
      } catch (err) {
        console.error('Save settings error:', err);
        if (btnSaveSettingsEl) {
          btnSaveSettingsEl.textContent = 'ERROR ✗';
          setTimeout(() => {
            btnSaveSettingsEl.textContent = origBtnText;
            btnSaveSettingsEl.disabled = false;
          }, 1500);
        }
      }
    });
  }

  // Handle Test Connection Button
  if (btnTestSettingsEl) {
    btnTestSettingsEl.addEventListener('click', async function () {
      const payload = providerPayload();

      if (testStatusEl) {
        testStatusEl.className = 'test-status loading';
        testStatusEl.textContent = 'Testing connection...';
        testStatusEl.style.display = 'flex';
      }

      btnTestSettingsEl.disabled = true;
      btnTestSettingsEl.textContent = 'TESTING...';

      try {
        const resp = await fetch('/test_config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-HUD-Token': HUD_TOKEN },
          body: JSON.stringify(payload),
        });

        const data = await resp.json();
        if (testStatusEl) {
          if (data.ok) {
            testStatusEl.className = 'test-status success';
            testStatusEl.textContent = `✓ ${data.message || 'Connected successfully!'}`;
          } else {
            testStatusEl.className = 'test-status error';
            testStatusEl.textContent = `✗ Error: ${data.message || 'Connection failed'}`;
          }
        }
      } catch (err) {
        if (testStatusEl) {
          testStatusEl.className = 'test-status error';
          testStatusEl.textContent = `✗ Error: ${err.message || 'Network request failed'}`;
        }
      } finally {
        btnTestSettingsEl.disabled = false;
        btnTestSettingsEl.textContent = 'TEST CONNECTION';
      }
    });
  }

  // 10. Pending Tool Approvals Panel
  // Request data comes from tool args (model/user controlled): render with textContent only.
  const approvalsCardEl = document.getElementById('approvals-card');
  const approvalsListEl = document.getElementById('approvals-list');
  let lastApprovalsSignature = '';
  const approvalsInFlight = new Set();

  function renderApprovals(pending) {
    if (!approvalsCardEl || !approvalsListEl) return;
    const signature = pending.map((p) => `${p.id}:${p.expires_in}:${p.pin_required}:${p.attempts_left}`).join('|');
    if (signature === lastApprovalsSignature) return;
    lastApprovalsSignature = signature;

    approvalsCardEl.hidden = pending.length === 0;
    // The list is rebuilt every second: keep a half-typed PIN and its focus.
    const savedPins = {};
    approvalsListEl.querySelectorAll('input[data-req]').forEach((el) => {
      savedPins[el.dataset.req] = { value: el.value, focused: document.activeElement === el };
    });
    approvalsListEl.replaceChildren();

    pending.forEach((req) => {
      const item = document.createElement('div');
      item.className = 'approval-item';

      const title = document.createElement('div');
      title.className = 'approval-tool';
      title.textContent = `${req.tool} — via ${req.channel}`;

      const summary = document.createElement('div');
      summary.className = 'approval-summary';
      summary.textContent = req.summary;

      const args = document.createElement('details');
      args.className = 'approval-details';
      const argsSummary = document.createElement('summary');
      argsSummary.textContent = 'Show technical details';
      const argsPre = document.createElement('pre');
      argsPre.className = 'approval-args';
      argsPre.textContent = req.args_preview;
      args.append(argsSummary, argsPre);

      const footer = document.createElement('div');
      footer.className = 'approval-actions';
      const timer = document.createElement('span');
      timer.className = 'approval-timer';
      timer.textContent = `expires in ${req.expires_in}s`;

      const busy = approvalsInFlight.has(req.id);
      const denyBtn = document.createElement('button');
      denyBtn.type = 'button';
      denyBtn.className = 'header-btn deny-btn';
      denyBtn.disabled = busy;
      denyBtn.addEventListener('click', () => submitApproval(req, 'deny'));

      if (req.pin_required) {
        // PIN mode: a correct PIN executes immediately; there is no Approve button.
        denyBtn.textContent = 'Cancel';
        const pinInput = document.createElement('input');
        pinInput.type = 'password';
        pinInput.className = 'form-control pin-input';
        pinInput.placeholder = 'PIN · الرمز';
        pinInput.maxLength = 12;
        pinInput.autocomplete = 'off';
        pinInput.dataset.req = req.id;
        pinInput.disabled = busy;
        const saved = savedPins[req.id];
        if (saved) pinInput.value = saved.value;
        pinInput.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') { e.preventDefault(); submitPin(req, pinInput); }
        });
        footer.append(timer, pinInput, denyBtn);
        item.append(title, summary, args);
        if (req.attempts_left < 3) {
          const err = document.createElement('div');
          err.className = 'pin-error';
          err.textContent = `Wrong PIN — ${req.attempts_left} attempt(s) left · رمز خاطئ، المحاولات المتبقية: ${req.attempts_left}`;
          item.append(err);
        }
        item.append(footer);
        approvalsListEl.appendChild(item);
        // Focus a new card's input (or restore the focus it had before the rebuild).
        if (!busy && (!saved || saved.focused)) pinInput.focus();
        return;
      }

      const approveBtn = document.createElement('button');
      approveBtn.type = 'button';
      approveBtn.className = 'header-btn approve-btn';
      approveBtn.textContent = 'Approve';
      approveBtn.disabled = busy;
      denyBtn.textContent = 'Deny';
      approveBtn.addEventListener('click', () => submitApproval(req, 'approve'));

      footer.append(timer, approveBtn, denyBtn);
      item.append(title, summary, args, footer);
      approvalsListEl.appendChild(item);
    });
  }

  async function fetchApprovals() {
    try {
      const resp = await fetch('/approvals?t=' + Date.now(), {
        cache: 'no-store',
        headers: { Accept: 'application/json', 'X-HUD-Token': HUD_TOKEN },
      });
      if (!resp.ok) return;
      const data = await resp.json();
      renderApprovals(Array.isArray(data.pending) ? data.pending : []);
    } catch (err) {
      // Offline / reconnecting
    }
  }

  async function submitPin(req, input) {
    const pin = input.value;
    if (!pin || approvalsInFlight.has(req.id)) return;
    input.value = '';  // never keep the PIN in the DOM after submitting
    approvalsInFlight.add(req.id);
    lastApprovalsSignature = '';
    try {
      await fetch('/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-HUD-Token': HUD_TOKEN },
        body: JSON.stringify({ id: req.id, pin: pin }),
      });
    } catch (err) {
      console.error('PIN submission error');
    } finally {
      approvalsInFlight.delete(req.id);
      lastApprovalsSignature = '';
      fetchApprovals();
    }
  }

  async function submitApproval(req, decision) {
    if (approvalsInFlight.has(req.id)) return;
    approvalsInFlight.add(req.id);
    lastApprovalsSignature = '';
    try {
      // args_hash binds the decision to exactly the args shown above.
      await fetch('/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-HUD-Token': HUD_TOKEN },
        body: JSON.stringify({ id: req.id, decision: decision, args_hash: req.args_hash }),
      });
    } catch (err) {
      console.error('Approval submission error:', err);
    } finally {
      approvalsInFlight.delete(req.id);
      lastApprovalsSignature = '';
      fetchApprovals();
    }
  }

  // 10. Memories modal (view / add / delete); text is always set via textContent.
  const memoriesModalEl = document.getElementById('memories-modal');
  const memoriesListEl = document.getElementById('memories-list');
  async function memApi(path, method, body) {
    const headers = { 'X-HUD-Token': HUD_TOKEN };
    if (body) headers['Content-Type'] = 'application/json';
    const resp = await fetch(path, { method: method, headers: headers, body: body ? JSON.stringify(body) : undefined });
    return resp.json();
  }
  async function loadMemories() {
    if (!memoriesListEl) return;
    try {
      const data = await memApi('/memories?t=' + Date.now(), 'GET');
      memoriesListEl.textContent = '';
      (data.memories || []).forEach(function (m) {
        const row = document.createElement('div');
        row.className = 'memory-row';
        row.textContent = '[' + m.kind + '] ' + m.text + ' ';
        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'header-btn';
        del.textContent = '✕';
        del.addEventListener('click', async function () {
          await memApi('/memories/' + m.id, 'DELETE');
          loadMemories();
        });
        row.appendChild(del);
        memoriesListEl.appendChild(row);
      });
      if (!memoriesListEl.children.length) memoriesListEl.textContent = 'No saved memories.';
    } catch (err) {
      console.error('Load memories error:', err);
    }
  }
  const btnMemoriesEl = document.getElementById('btn-memories');
  if (btnMemoriesEl && memoriesModalEl) {
    btnMemoriesEl.addEventListener('click', function () { memoriesModalEl.style.display = 'flex'; loadMemories(); });
    document.getElementById('btn-close-memories').addEventListener('click', function () { memoriesModalEl.style.display = 'none'; });
    document.getElementById('btn-memory-add').addEventListener('click', async function () {
      const textEl = document.getElementById('memory-text');
      const text = textEl.value.trim();
      if (!text) return;
      await memApi('/memories', 'POST', { kind: document.getElementById('memory-kind').value, text: text });
      textEl.value = '';
      loadMemories();
    });
  }

  // Initial fetch and 1-second interval polling
  fetchStatus();
  fetchApprovals();
  setInterval(fetchStatus, 1000);
  setInterval(fetchApprovals, 1000);
})();
