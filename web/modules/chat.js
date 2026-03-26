import { escapeHtml, renderMarkdown } from './utils.js';
import {
    getLogTaskGroupId,
    isGroupedTaskEvent,
    normalizeLogTs,
    summarizeChatLiveEvent,
} from './log_events.js';

const CHAT_STORAGE_KEY = 'ouro_chat';
const CHAT_INPUT_HISTORY_KEY = 'ouro_chat_input_history';
const CHAT_SESSION_ID_KEY = 'ouro_chat_session_id';

function getOrCreateChatSessionId() {
    try {
        const existing = sessionStorage.getItem(CHAT_SESSION_ID_KEY);
        if (existing) return existing;
        const created = (globalThis.crypto && typeof crypto.randomUUID === 'function')
            ? crypto.randomUUID()
            : `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        sessionStorage.setItem(CHAT_SESSION_ID_KEY, created);
        return created;
    } catch {
        return `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }
}

function loadInputHistory() {
    try {
        const raw = JSON.parse(sessionStorage.getItem(CHAT_INPUT_HISTORY_KEY) || '[]');
        return Array.isArray(raw) ? raw.filter(Boolean).slice(-50) : [];
    } catch {
        return [];
    }
}

function saveInputHistory(entries) {
    try {
        sessionStorage.setItem(CHAT_INPUT_HISTORY_KEY, JSON.stringify(entries.slice(-50)));
    } catch {}
}

export function initChat({ ws, state, updateUnreadBadge }) {
    const container = document.getElementById('content');
    const chatSessionId = getOrCreateChatSessionId();

    const page = document.createElement('div');
    page.id = 'page-chat';
    page.className = 'page active';
    page.innerHTML = `
        <div class="page-header chat-page-header">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            <h2>Chat</h2>
            <div class="spacer"></div>
            <div class="chat-header-actions" id="chat-header-actions">
                <button class="chat-header-btn" type="button" data-chat-command="evolve" title="Toggle evolution mode">Evolve</button>
                <button class="chat-header-btn" type="button" data-chat-command="bg" title="Toggle background consciousness">Consciousness</button>
                <button class="chat-header-btn" type="button" data-chat-command="review" title="Run review now">Review</button>
                <button class="chat-header-btn" type="button" data-chat-command="restart" title="Restart agent">Restart</button>
                <button class="chat-header-btn danger" type="button" data-chat-command="panic" title="Stop all workers">Panic</button>
            </div>
            <span id="chat-status" class="status-badge offline">Connecting...</span>
        </div>
        <div id="chat-messages"></div>
        <div id="chat-input-area">
            <textarea id="chat-input" placeholder="Message Ouroboros..." rows="1"></textarea>
            <button class="icon-btn" id="chat-send">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
            </button>
        </div>
    `;
    container.appendChild(page);

    const messagesDiv = document.getElementById('chat-messages');
    const input = document.getElementById('chat-input');
    const sendBtn = document.getElementById('chat-send');
    const statusBadge = document.getElementById('chat-status');
    const headerActions = document.getElementById('chat-header-actions');

    const persistedHistory = [];
    const seenMessageKeys = new Set();
    const messageKeyOrder = [];
    const pendingUserBubbles = new Map();
    const inputHistory = loadInputHistory();
    let inputHistoryIndex = inputHistory.length;
    let inputDraft = '';
    let historyLoaded = false;
    let historySyncPromise = null;
    let welcomeShown = false;
    const liveCard = document.createElement('details');
    liveCard.id = 'chat-live-card';
    liveCard.className = 'chat-live-card';
    liveCard.hidden = true;
    liveCard.innerHTML = `
        <summary>
            <div class="chat-live-summary">
                <div class="chat-live-summary-main">
                    <span class="chat-live-phase working" data-live-phase>Working</span>
                    <span class="chat-live-title" data-live-title>Waiting for work</span>
                </div>
                <div class="chat-live-summary-side">
                    <span class="chat-live-count" data-live-count hidden>2 notes</span>
                    <span class="chat-live-toggle" data-live-toggle>Show details</span>
                    <svg class="chat-live-chevron" width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                        <path d="M5 7.5 10 12.5 15 7.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"></path>
                    </svg>
                </div>
            </div>
            <div class="chat-live-meta" data-live-meta></div>
        </summary>
        <div class="chat-live-timeline" data-live-timeline></div>
    `;
    const liveCardPhase = liveCard.querySelector('[data-live-phase]');
    const liveCardTitle = liveCard.querySelector('[data-live-title]');
    const liveCardCount = liveCard.querySelector('[data-live-count]');
    const liveCardMeta = liveCard.querySelector('[data-live-meta]');
    const liveCardToggle = liveCard.querySelector('[data-live-toggle]');
    const liveCardTimeline = liveCard.querySelector('[data-live-timeline]');
    const liveCardState = {
        groupId: '',
        updates: 0,
        finished: false,
        items: [],
        lastHumanHeadline: '',
    };
    liveCard.addEventListener('toggle', syncLiveCardToggle);

    function buildMessageKey(role, text, timestamp, opts = {}) {
        if (opts.clientMessageId) return `client|${opts.clientMessageId}`;
        if (!timestamp) return '';
        return [
            role,
            opts.isProgress ? '1' : '0',
            opts.systemType || '',
            opts.source || '',
            opts.senderLabel || '',
            opts.senderSessionId || '',
            timestamp,
            text,
        ].join('|');
    }

    function rememberMessageKey(key) {
        if (!key || seenMessageKeys.has(key)) return;
        seenMessageKeys.add(key);
        messageKeyOrder.push(key);
        if (messageKeyOrder.length > 2000) {
            const oldest = messageKeyOrder.shift();
            if (oldest) seenMessageKeys.delete(oldest);
        }
    }

    function formatMsgTime(isoStr) {
        if (!isoStr) return null;
        try {
            const d = new Date(isoStr);
            if (isNaN(d)) return null;
            const now = new Date();
            const pad = n => String(n).padStart(2, '0');
            const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
            const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
            const todayStr = now.toDateString();
            const yesterday = new Date(now);
            yesterday.setDate(now.getDate() - 1);
            let short;
            if (d.toDateString() === todayStr) short = hhmm;
            else if (d.toDateString() === yesterday.toDateString()) short = `Yesterday, ${hhmm}`;
            else short = `${months[d.getMonth()]} ${d.getDate()}, ${hhmm}`;
            const full = `${months[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()} at ${hhmm}`;
            return { short, full };
        } catch {
            return null;
        }
    }

    function getSenderLabel(role, isProgress = false, systemType = '', opts = {}) {
        if (role === 'user') {
            if (opts.source === 'telegram') return opts.senderLabel || 'Telegram';
            if (opts.senderSessionId && opts.senderSessionId !== chatSessionId) {
                return `WebUI (${opts.senderSessionId.slice(0, 8)})`;
            }
            return opts.senderLabel || 'You';
        }
        if (role === 'system') {
            return systemType === 'task_summary' ? '📋 Task Summary' : '📋 System';
        }
        if (isProgress) return '💬 Thought';
        return 'Ouroboros';
    }

    function setStatus(kind, text) {
        if (!statusBadge) return;
        statusBadge.className = `status-badge ${kind}`;
        statusBadge.textContent = text;
    }

    function syncHeaderControlState(data) {
        headerActions?.querySelectorAll('[data-chat-command]').forEach((button) => {
            const cmd = button.dataset.chatCommand;
            if (cmd === 'evolve') {
                button.classList.toggle('on', !!data?.evolution_enabled);
            } else if (cmd === 'bg') {
                button.classList.toggle('on', !!data?.bg_consciousness_enabled);
            }
        });
    }

    async function refreshHeaderControlState(force = false) {
        if (!force && state.activePage !== 'chat') return;
        try {
            const resp = await fetch('/api/state', { cache: 'no-store' });
            if (!resp.ok) return;
            syncHeaderControlState(await resp.json());
        } catch {}
    }

    function persistVisibleHistory() {
        try {
            sessionStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(persistedHistory.slice(-200)));
        } catch {}
    }

    function insertMessageNode(node) {
        const typing = document.getElementById('typing-indicator');
        if (typing && typing.parentNode === messagesDiv) messagesDiv.insertBefore(node, typing);
        else messagesDiv.appendChild(node);
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }

    function resetLiveCard(groupId) {
        liveCardState.groupId = groupId || '';
        liveCardState.updates = 0;
        liveCardState.finished = false;
        liveCardState.items = [];
        liveCardState.lastHumanHeadline = '';
        liveCardTitle.textContent = 'Working...';
        liveCardPhase.dataset.phase = 'working';
        liveCardPhase.textContent = 'Working';
        liveCardPhase.className = 'chat-live-phase working';
        liveCardCount.hidden = true;
        liveCardCount.textContent = '0 notes';
        liveCardMeta.innerHTML = '';
        liveCardTimeline.innerHTML = '';
        liveCard.open = false;
        liveCard.dataset.finished = '0';
        syncLiveCardToggle();
    }

    function ensureLiveCardVisible() {
        if (liveCard.hidden) {
            liveCard.hidden = false;
            insertMessageNode(liveCard);
            return;
        }
        insertMessageNode(liveCard);
    }

    function formatLiveCardPhaseLabel(phase) {
        if (phase === 'thinking') return 'Thinking';
        if (phase === 'working') return 'Working';
        if (phase === 'done') return 'Done';
        if (phase === 'error' || phase === 'timeout') return 'Issue';
        if (!phase) return 'Working';
        return phase.charAt(0).toUpperCase() + phase.slice(1);
    }

    function syncLiveCardToggle() {
        if (!liveCardToggle) return;
        liveCardToggle.textContent = liveCard.open ? 'Hide details' : 'Show details';
    }

    function renderLiveCardTimeline() {
        liveCardTimeline.innerHTML = liveCardState.items.map((item) => `
            <div class="chat-live-line ${item.phase || 'working'}">
                <div class="chat-live-line-head">
                    <span class="chat-live-line-title">${escapeHtml(item.headline)}</span>
                    <span class="chat-live-line-repeat" ${item.count > 1 ? '' : 'hidden'}>${item.count > 1 ? `${item.count}x` : ''}</span>
                    ${item.ts ? `<span class="chat-live-line-time">${escapeHtml(item.ts)}</span>` : ''}
                </div>
                ${item.body ? `<div class="chat-live-line-body">${escapeHtml(item.body)}</div>` : ''}
            </div>
        `).join('');
    }

    function applyLiveCardState(summary, groupId, ts, dedupeKey = '') {
        const nextGroupId = groupId || liveCardState.groupId || 'active';
        if (liveCardState.groupId && liveCardState.groupId !== nextGroupId) {
            resetLiveCard(nextGroupId);
        } else if (!liveCardState.groupId) {
            resetLiveCard(nextGroupId);
        }

        ensureLiveCardVisible();
        liveCardState.updates += 1;
        liveCardState.finished = ['done', 'error', 'timeout'].includes(summary.phase || '');
        liveCard.dataset.finished = liveCardState.finished ? '1' : '0';
        const headline = summary.headline || 'Working...';
        if (summary.human && headline) {
            liveCardState.lastHumanHeadline = headline;
        }

        const shouldPromote =
            Boolean(summary.promote)
            || !liveCardState.lastHumanHeadline
            || liveCardState.finished;
        const activeHeadline = shouldPromote
            ? headline
            : (liveCardState.lastHumanHeadline || headline);
        const activePhase = liveCardState.finished
            ? (summary.phase || 'done')
            : (shouldPromote ? (summary.phase || 'working') : (liveCardPhase.dataset.phase || 'working'));

        liveCardPhase.dataset.phase = activePhase;
        liveCardPhase.textContent = formatLiveCardPhaseLabel(activePhase);
        liveCardPhase.className = `chat-live-phase ${activePhase}`;
        liveCardTitle.textContent = activeHeadline;

        const syntheticKey = summary.dedupeKey || dedupeKey || `${summary.phase || 'working'}|${headline}|${summary.body || ''}`;
        const shouldRenderLine = summary.visible !== false && Boolean(headline || summary.body);
        if (shouldRenderLine) {
            const last = liveCardState.items[liveCardState.items.length - 1];
            if (last && last.dedupeKey === syntheticKey) {
                last.count += 1;
                last.ts = ts || last.ts;
            } else {
                liveCardState.items.push({
                    phase: summary.phase || 'working',
                    headline: headline || 'Update',
                    body: summary.body || '',
                    ts: ts || '',
                    count: 1,
                    dedupeKey: syntheticKey,
                });
                if (liveCardState.items.length > 20) liveCardState.items.shift();
            }
        }
        liveCardCount.hidden = liveCardState.items.length < 2;
        liveCardCount.textContent = `${liveCardState.items.length} notes`;
        liveCardMeta.innerHTML = [
            nextGroupId === 'bg-consciousness' ? 'Background thinking' : '',
            ts ? `Latest ${ts}` : '',
        ].filter(Boolean).map((item) => `<span class="chat-live-meta-text">${escapeHtml(item)}</span>`).join('');
        renderLiveCardTimeline();
        insertMessageNode(liveCard);
        hideTypingIndicatorOnly();
        if (liveCardState.finished) {
            setStatus(summary.phase === 'error' || summary.phase === 'timeout' ? 'error' : 'online', summary.phase === 'error' || summary.phase === 'timeout' ? 'Attention' : 'Online');
        } else {
            setStatus('thinking', 'Working...');
        }
    }

    function updateLiveCardFromProgressMessage(msg) {
        const summary = summarizeChatLiveEvent({
            type: 'send_message',
            is_progress: true,
            content: msg?.content || '',
            text: msg?.content || '',
            task_id: liveCardState.groupId || 'chat',
        });
        if (!summary) return;
        applyLiveCardState(
            summary,
            liveCardState.groupId || 'chat',
            normalizeLogTs(msg.ts || new Date().toISOString()),
            summary.dedupeKey || '',
        );
    }

    function updateLiveCardFromLogEvent(evt) {
        if (!evt || !isGroupedTaskEvent(evt)) return;
        const summary = summarizeChatLiveEvent(evt);
        if (!summary) return;
        applyLiveCardState(
            summary,
            getLogTaskGroupId(evt) || liveCardState.groupId || 'active',
            normalizeLogTs(evt.ts || evt.timestamp),
            summary.dedupeKey || '',
        );
    }

    function addMessage(text, role, markdown = false, timestamp = null, isProgress = false, opts = {}) {
        const pending = !!opts.pending;
        const ephemeral = !!opts.ephemeral;
        const clientMessageId = opts.clientMessageId || '';
        const senderLabel = opts.senderLabel || '';
        const senderSessionId = opts.senderSessionId || '';
        const source = opts.source || '';
        const systemType = opts.systemType || '';
        const ts = timestamp || new Date().toISOString();
        const messageKey = buildMessageKey(role, text, ts, {
            clientMessageId,
            systemType,
            isProgress,
            source,
            senderLabel,
            senderSessionId,
        });
        if (messageKey && seenMessageKeys.has(messageKey)) return null;

        if (!isProgress && !ephemeral) {
            persistedHistory.push({
                text,
                role,
                ts,
                markdown: !!markdown,
                systemType,
                source,
                senderLabel,
                senderSessionId,
                clientMessageId,
            });
            persistVisibleHistory();
        }

        const bubble = document.createElement('div');
        bubble.className = `chat-bubble ${role}` + (isProgress ? ' progress' : '');
        if (pending) bubble.classList.add('pending');
        if (ephemeral) bubble.dataset.ephemeral = '1';
        if (clientMessageId) bubble.dataset.clientMessageId = clientMessageId;
        if (systemType) bubble.dataset.systemType = systemType;
        if (senderSessionId) bubble.dataset.senderSessionId = senderSessionId;

        const sender = getSenderLabel(role, isProgress, systemType, { source, senderLabel, senderSessionId });
        const rendered = role === 'user' ? escapeHtml(text) : renderMarkdown(text);
        const timeFmt = formatMsgTime(ts);
        const timeHtml = timeFmt ? `<div class="msg-time" title="${timeFmt.full}">${timeFmt.short}</div>` : '';
        const pendingHtml = pending ? `<div class="msg-pending">Queued until reconnect</div>` : '';
        bubble.innerHTML = `
            <div class="sender">${escapeHtml(sender)}</div>
            <div class="message">${rendered}</div>
            ${pendingHtml}
            ${timeHtml}
        `;
        insertMessageNode(bubble);
        rememberMessageKey(messageKey);
        if (pending && clientMessageId) pendingUserBubbles.set(clientMessageId, bubble);
        return bubble;
    }

    function markPendingDelivered(clientMessageId) {
        const bubble = pendingUserBubbles.get(clientMessageId || '');
        if (!bubble) return;
        bubble.classList.remove('pending');
        bubble.querySelector('.msg-pending')?.remove();
        pendingUserBubbles.delete(clientMessageId);
    }

    function ensureWelcomeMessage() {
        if (welcomeShown) return;
        const hasRealBubbles = Array.from(messagesDiv.querySelectorAll('.chat-bubble')).some(
            bubble => !bubble.classList.contains('typing-bubble')
        );
        if (hasRealBubbles) return;
        welcomeShown = true;
        addMessage('Ouroboros has awakened', 'assistant', false, null, false, { ephemeral: true });
    }

    async function syncHistory({ includeUser = false } = {}) {
        if (historySyncPromise) return historySyncPromise;
        historySyncPromise = (async () => {
            try {
                const resp = await fetch('/api/chat/history?limit=1000', { cache: 'no-store' });
                if (!resp.ok) return false;
                const data = await resp.json();
                const messages = Array.isArray(data.messages) ? data.messages : [];
                for (const msg of messages) {
                    if (!includeUser && msg.role === 'user') continue;
                    if (msg.is_progress) continue;
                    addMessage(msg.text, msg.role, !!msg.markdown, msg.ts || null, false, {
                        systemType: msg.system_type || '',
                        source: msg.source || '',
                        senderLabel: msg.sender_label || '',
                        senderSessionId: msg.sender_session_id || '',
                        clientMessageId: msg.client_message_id || '',
                    });
                }
                historyLoaded = true;
                return messages.length > 0;
            } catch (err) {
                console.error('Failed to load chat history:', err);
                return false;
            } finally {
                historySyncPromise = null;
            }
        })();
        return historySyncPromise;
    }

    (async () => {
        if (await syncHistory({ includeUser: true })) return;
        try {
            const saved = JSON.parse(sessionStorage.getItem(CHAT_STORAGE_KEY) || '[]');
            for (const msg of saved) {
                addMessage(msg.text, msg.role, !!msg.markdown, msg.ts || null, false, {
                    systemType: msg.systemType || '',
                    source: msg.source || '',
                    senderLabel: msg.senderLabel || '',
                    senderSessionId: msg.senderSessionId || '',
                    clientMessageId: msg.clientMessageId || '',
                });
            }
        } catch {}
        historyLoaded = true;
        ensureWelcomeMessage();
    })();

    function rememberInput(text) {
        if (!text) return;
        if (inputHistory[inputHistory.length - 1] !== text) inputHistory.push(text);
        saveInputHistory(inputHistory);
        inputHistoryIndex = inputHistory.length;
        inputDraft = '';
    }

    function restoreInputHistory(step) {
        if (!inputHistory.length) return;
        if (step < 0) {
            if (input.selectionStart !== 0 || input.selectionEnd !== 0) return;
            if (inputHistoryIndex === inputHistory.length) inputDraft = input.value;
            inputHistoryIndex = Math.max(0, inputHistoryIndex - 1);
            input.value = inputHistory[inputHistoryIndex] || '';
        } else {
            if (input.selectionStart !== input.value.length || input.selectionEnd !== input.value.length) return;
            inputHistoryIndex = Math.min(inputHistory.length, inputHistoryIndex + 1);
            input.value = inputHistoryIndex === inputHistory.length ? inputDraft : (inputHistory[inputHistoryIndex] || '');
        }
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
        const cursor = input.value.length;
        input.setSelectionRange(cursor, cursor);
    }

    function sendMessage() {
        const text = input.value.trim();
        if (!text) return;
        rememberInput(text);
        input.value = '';
        input.style.height = 'auto';
        const result = ws.send({
            type: 'chat',
            content: text,
            sender_session_id: chatSessionId,
        });
        addMessage(text, 'user', false, null, false, {
            pending: result?.status === 'queued',
            source: 'web',
            senderSessionId: chatSessionId,
            clientMessageId: result?.clientMessageId || '',
        });
    }

    sendBtn.addEventListener('click', sendMessage);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
            return;
        }
        if (e.key === 'ArrowUp' && !e.shiftKey) {
            restoreInputHistory(-1);
        } else if (e.key === 'ArrowDown' && !e.shiftKey) {
            restoreInputHistory(1);
        }
    });
    input.addEventListener('input', () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
        if (inputHistoryIndex === inputHistory.length) inputDraft = input.value;
    });

    headerActions?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-chat-command]');
        if (!button) return;
        const command = button.dataset.chatCommand;
        if (command === 'evolve') {
            const next = !button.classList.contains('on');
            button.classList.toggle('on', next);
            ws.send({ type: 'command', cmd: `/evolve ${next ? 'start' : 'stop'}` });
            return;
        }
        if (command === 'bg') {
            const next = !button.classList.contains('on');
            button.classList.toggle('on', next);
            ws.send({ type: 'command', cmd: `/bg ${next ? 'start' : 'stop'}` });
            return;
        }
        if (command === 'review') {
            ws.send({ type: 'command', cmd: '/review' });
            return;
        }
        if (command === 'restart') {
            ws.send({ type: 'command', cmd: '/restart' });
            return;
        }
        if (command === 'panic' && confirm('Kill all workers immediately?')) {
            ws.send({ type: 'command', cmd: '/panic' });
        }
    });

    refreshHeaderControlState(true);
    setInterval(refreshHeaderControlState, 3000);

    const typingEl = document.createElement('div');
    typingEl.id = 'typing-indicator';
    typingEl.className = 'chat-bubble assistant typing-bubble';
    typingEl.style.display = 'none';
    typingEl.innerHTML = `<div class="typing-dots"><span></span><span></span><span></span></div>`;
    messagesDiv.appendChild(typingEl);

    function showTyping() {
        typingEl.style.display = '';
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
        setStatus('thinking', 'Thinking...');
    }

    function hideTypingIndicatorOnly() {
        typingEl.style.display = 'none';
    }

    function hideTyping() {
        hideTypingIndicatorOnly();
        if (statusBadge && ['Thinking...', 'Working...'].includes(statusBadge.textContent)) {
            setStatus('online', 'Online');
        }
    }

    function incrementUnreadIfNeeded() {
        if (state.activePage === 'chat') return;
        state.unreadCount++;
        updateUnreadBadge();
    }

    ws.on('typing', () => {
        showTyping();
    });

    ws.on('chat', (msg) => {
        if (msg.role === 'user') {
            const clientMessageId = msg.client_message_id || '';
            const senderSessionId = msg.sender_session_id || '';
            if (senderSessionId === chatSessionId && clientMessageId) {
                markPendingDelivered(clientMessageId);
                return;
            }
            addMessage(msg.content, 'user', false, msg.ts || null, false, {
                source: msg.source || '',
                senderLabel: msg.sender_label || '',
                senderSessionId,
                clientMessageId,
            });
            incrementUnreadIfNeeded();
            return;
        }

        if (msg.role === 'assistant' || msg.role === 'system') {
            hideTyping();
            if (msg.is_progress) {
                updateLiveCardFromProgressMessage(msg);
                return;
            }
            if (liveCardState.groupId && !liveCardState.finished) {
                liveCardState.finished = true;
                liveCard.dataset.finished = '1';
                if (['working', 'thinking'].includes(liveCardPhase.dataset.phase || '')) {
                    liveCardPhase.dataset.phase = 'done';
                    liveCardPhase.textContent = 'Done';
                    liveCardPhase.className = 'chat-live-phase done';
                }
            }
            addMessage(msg.content, msg.role, msg.markdown, msg.ts || null, false, {
                systemType: msg.system_type || '',
                source: msg.source || '',
            });
            incrementUnreadIfNeeded();
        }
    });

    ws.on('log', (msg) => {
        if (!msg?.data) return;
        updateLiveCardFromLogEvent(msg.data);
    });

    ws.on('outbound_sent', (evt) => {
        markPendingDelivered(evt?.clientMessageId || '');
    });

    ws.on('photo', (msg) => {
        hideTyping();
        const role = msg.role === 'user' ? 'user' : 'assistant';
        const sender = role === 'user'
            ? getSenderLabel('user', false, '', {
                source: msg.source || '',
                senderLabel: msg.sender_label || '',
                senderSessionId: msg.sender_session_id || '',
            })
            : 'Ouroboros';
        const bubble = document.createElement('div');
        bubble.className = `chat-bubble ${role}`;
        const timeFmt = formatMsgTime(msg.ts || new Date().toISOString());
        const timeHtml = timeFmt ? `<div class="msg-time" title="${timeFmt.full}">${timeFmt.short}</div>` : '';
        const captionHtml = msg.caption ? `<div class="message">${escapeHtml(msg.caption)}</div>` : '';
        bubble.innerHTML = `
            <div class="sender">${escapeHtml(sender)}</div>
            ${captionHtml}
            <div class="message"><img src="data:${msg.mime || 'image/png'};base64,${msg.image_base64}" style="max-width:100%;border-radius:8px;cursor:pointer" onclick="window.open(this.src,'_blank')" /></div>
            ${timeHtml}
        `;
        insertMessageNode(bubble);
        incrementUnreadIfNeeded();
    });

    ws.on('open', () => {
        setStatus('online', 'Online');
        refreshHeaderControlState(true);
        syncHistory({ includeUser: !historyLoaded })
            .then((hasMessages) => {
                if (!hasMessages) ensureWelcomeMessage();
            })
            .catch(() => {});
    });

    ws.on('close', () => {
        hideTyping();
        setStatus('offline', 'Reconnecting...');
    });
}
