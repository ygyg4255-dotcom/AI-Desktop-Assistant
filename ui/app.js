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
      document.body.className = 'state-idle';
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
  const btnSaveSettingsEl = document.getElementById('btn-save-settings');
  const btnTestSettingsEl = document.getElementById('btn-test-settings');
  const testStatusEl = document.getElementById('test-connection-status');
  const displayAiEngineEl = document.getElementById('display-ai-engine');
  const displayAiLangEl = document.getElementById('display-ai-lang');

  function updateAiConfigDisplay(cfg) {
    if (!cfg) return;
    if (displayAiEngineEl) {
      displayAiEngineEl.textContent = `CLAUDE (${cfg.model || 'claude-sonnet-5-5'})`;
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
        if (cfgModelEl) cfgModelEl.value = cfg.model || '';
        if (cfgLanguageEl) cfgLanguageEl.value = cfg.language || 'English';

        if (cfgApiKeyEl) {
          cfgApiKeyEl.value = '';
          if (cfg.has_api_key) {
            cfgApiKeyEl.placeholder = 'Key configured (leave blank to keep)';
          } else {
            cfgApiKeyEl.placeholder = 'sk-ant-...';
          }
        }
      }
    } catch (err) {
      console.error('Failed to load settings:', err);
    }
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

      const payload = {
        model: cfgModelEl ? cfgModelEl.value.trim() : '',
        api_key: cfgApiKeyEl ? cfgApiKeyEl.value.trim() : '',
        language: cfgLanguageEl ? cfgLanguageEl.value : 'English',
      };

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
      const payload = {
        model: cfgModelEl ? cfgModelEl.value.trim() : '',
        api_key: cfgApiKeyEl ? cfgApiKeyEl.value.trim() : '',
      };

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
    const signature = pending.map((p) => `${p.id}:${p.expires_in}`).join('|');
    if (signature === lastApprovalsSignature) return;
    lastApprovalsSignature = signature;

    approvalsCardEl.hidden = pending.length === 0;
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

      const args = document.createElement('pre');
      args.className = 'approval-args';
      args.textContent = req.args_preview;

      const footer = document.createElement('div');
      footer.className = 'approval-actions';
      const timer = document.createElement('span');
      timer.className = 'approval-timer';
      timer.textContent = `expires in ${req.expires_in}s`;

      const approveBtn = document.createElement('button');
      approveBtn.type = 'button';
      approveBtn.className = 'header-btn approve-btn';
      approveBtn.textContent = 'Approve';
      const denyBtn = document.createElement('button');
      denyBtn.type = 'button';
      denyBtn.className = 'header-btn deny-btn';
      denyBtn.textContent = 'Deny';
      if (approvalsInFlight.has(req.id)) {
        approveBtn.disabled = true;
        denyBtn.disabled = true;
      }
      approveBtn.addEventListener('click', () => submitApproval(req, 'approve'));
      denyBtn.addEventListener('click', () => submitApproval(req, 'deny'));

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

  // Initial fetch and 1-second interval polling
  fetchStatus();
  fetchApprovals();
  setInterval(fetchStatus, 1000);
  setInterval(fetchApprovals, 1000);
})();
