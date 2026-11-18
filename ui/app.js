// A.L.T.R.O.N Sci-Fi HUD Client Script
(function () {
  'use strict';

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
        headers: { Accept: 'application/json' },
      });

      if (!response.ok) return;

      const data = await response.json();
      renderState(data.state || 'Idle');
      renderMetrics(data.metrics || data);
      renderLogs(data.logs || []);

      if (data.ai_config) {
        updateAiConfigDisplay(data.ai_config);
      }
    } catch (err) {
      // Offline / reconnecting
    }
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
        await fetch('/clear_logs', { method: 'POST' });
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
  const cfgProviderEl = document.getElementById('cfg-provider');
  const cfgModelEl = document.getElementById('cfg-model');
  const cfgApiKeyEl = document.getElementById('cfg-api-key');
  const cfgBaseUrlEl = document.getElementById('cfg-base-url');
  const cfgLanguageEl = document.getElementById('cfg-language');
  const btnSaveSettingsEl = document.getElementById('btn-save-settings');
  const btnTestSettingsEl = document.getElementById('btn-test-settings');
  const testStatusEl = document.getElementById('test-connection-status');
  const displayAiEngineEl = document.getElementById('display-ai-engine');
  const displayAiLangEl = document.getElementById('display-ai-lang');

  const PROVIDER_DEFAULTS = {
    ollama: {
      model: 'llama3.2',
      baseUrl: 'http://localhost:11434',
      keyPlaceholder: 'Not required for local Ollama',
    },
    anthropic: {
      model: 'claude-3-5-sonnet-20241022',
      baseUrl: 'https://api.anthropic.com',
      keyPlaceholder: 'sk-ant-...',
    },
    openai: {
      model: 'gpt-4o-mini',
      baseUrl: 'https://api.openai.com/v1',
      keyPlaceholder: 'sk-...',
    },
    gemini: {
      model: 'gemini-2.5-flash',
      baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai',
      keyPlaceholder: 'AIzaSy...',
    },
    custom: {
      model: 'serve',
      baseUrl: 'http://localhost:20128',
      keyPlaceholder: 'API Key (if required)',
    },
  };

  function updateAiConfigDisplay(cfg) {
    if (!cfg) return;
    if (displayAiEngineEl) {
      const p = (cfg.provider || 'Anthropic').toUpperCase();
      const m = cfg.model || 'Claude';
      displayAiEngineEl.textContent = `${p} (${m})`;
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
      const resp = await fetch('/config?t=' + Date.now());
      if (resp.ok) {
        const cfg = await resp.json();
        if (cfgProviderEl) cfgProviderEl.value = cfg.provider || 'anthropic';
        if (cfgModelEl) cfgModelEl.value = cfg.model || '';
        if (cfgBaseUrlEl) cfgBaseUrlEl.value = cfg.base_url || '';
        if (cfgLanguageEl) cfgLanguageEl.value = cfg.language || 'English';

        if (cfgApiKeyEl) {
          cfgApiKeyEl.value = '';
          if (cfg.has_api_key) {
            cfgApiKeyEl.placeholder = 'Key configured (leave blank to keep)';
          } else {
            const defs = PROVIDER_DEFAULTS[cfg.provider] || PROVIDER_DEFAULTS.anthropic;
            cfgApiKeyEl.placeholder = defs.keyPlaceholder;
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

  // Handle provider dropdown switch with smart defaults
  if (cfgProviderEl) {
    cfgProviderEl.addEventListener('change', function () {
      const p = cfgProviderEl.value;
      const defs = PROVIDER_DEFAULTS[p];
      if (defs) {
        if (cfgModelEl) cfgModelEl.value = defs.model;
        if (cfgBaseUrlEl) cfgBaseUrlEl.value = defs.baseUrl;
        if (cfgApiKeyEl) {
          cfgApiKeyEl.value = '';
          cfgApiKeyEl.placeholder = defs.keyPlaceholder;
        }
      }
    });
  }

  // Handle saving configuration
  if (settingsFormEl) {
    settingsFormEl.addEventListener('submit', async function (e) {
      e.preventDefault();

      const payload = {
        provider: cfgProviderEl ? cfgProviderEl.value : 'anthropic',
        model: cfgModelEl ? cfgModelEl.value.trim() : '',
        base_url: cfgBaseUrlEl ? cfgBaseUrlEl.value.trim() : '',
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
          headers: { 'Content-Type': 'application/json' },
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
        provider: cfgProviderEl ? cfgProviderEl.value : 'anthropic',
        model: cfgModelEl ? cfgModelEl.value.trim() : '',
        base_url: cfgBaseUrlEl ? cfgBaseUrlEl.value.trim() : '',
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
          headers: { 'Content-Type': 'application/json' },
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

  // Initial fetch and 1-second interval polling
  fetchStatus();
  setInterval(fetchStatus, 1000);
})();
