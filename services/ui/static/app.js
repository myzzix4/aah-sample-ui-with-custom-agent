// AAH Sample UI — 삼성생명 mockup + AI 채팅
const msgs = document.getElementById('messages');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const fab = document.getElementById('chat-fab');
const panel = document.getElementById('chat-panel');

let sessionId = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2));
let firstOpen = true;

function openChat() {
  panel.classList.remove('hidden');
  fab.classList.add('hidden');
  if (firstOpen) {
    addMsg('assistant', '안녕하세요! 삼성생명 AI 상담사입니다.\n\n약관 검색, 보험금 청구 절차, 상품 비교 등 무엇이든 물어보세요.');
    firstOpen = false;
  }
  setTimeout(() => input.focus(), 100);
}
function closeChat() {
  panel.classList.add('hidden');
  fab.classList.remove('hidden');
}
function newSession() {
  sessionId = (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2));
  msgs.innerHTML = '';
  addMsg('assistant', '새 대화를 시작합니다. 무엇이 궁금하신가요?');
}
window.openChat = openChat;
window.closeChat = closeChat;
window.newSession = newSession;

function addMsg(role, text, opts = {}) {
  const el = document.createElement('div');
  el.className = `msg ${role}`;
  if (opts.thinking) {
    el.innerHTML = `<span class="thinking"><span class="spinner"></span>${text}</span>`;
  } else {
    el.textContent = text;
  }
  msgs.appendChild(el);
  msgs.scrollTop = msgs.scrollHeight;
  return el;
}

async function send() {
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  sendBtn.disabled = true;

  addMsg('user', text);
  const assistantEl = addMsg('assistant', '응답 생성 중…', { thinking: true });

  try {
    const resp = await fetch('/api/chat-sse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input: text, session_id: sessionId }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    let fullText = '';
    let started = false;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      while (buf.includes('\n\n')) {
        const idx = buf.indexOf('\n\n');
        const blk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const lines = blk.split('\n');
        const ev = lines.find(l => l.startsWith('event:'))?.slice(6).trim() || 'message';
        const data = lines.filter(l => l.startsWith('data:')).map(l => l.slice(5).trim()).join('\n');
        let parsed = {};
        try { parsed = JSON.parse(data); } catch (e) {}

        if (ev === 'token') {
          if (!started) { assistantEl.textContent = ''; started = true; }
          fullText += parsed.text || '';
          assistantEl.textContent = fullText;
          msgs.scrollTop = msgs.scrollHeight;
        } else if (ev === 'tool_use_start') {
          const chip = document.createElement('div');
          chip.className = 'tool-call';
          chip.textContent = `🔧 ${parsed.name || '도구'} 호출 중…`;
          assistantEl.appendChild(chip);
        } else if (ev === 'tool_result') {
          const chips = assistantEl.querySelectorAll('.tool-call');
          if (chips.length) {
            const last = chips[chips.length - 1];
            last.textContent = `🔧 ${parsed.name} ${parsed.ok ? '✓' : '✕'} (${parsed.count || 0} 결과)`;
          }
        } else if (ev === 'error') {
          assistantEl.innerHTML = `<span style="color:#ef4444">❌ ${parsed.error || 'error'}</span>`;
        }
      }
    }
    if (!started && !fullText) {
      assistantEl.textContent = '(빈 응답)';
    }
  } catch (e) {
    assistantEl.innerHTML = `<span style="color:#ef4444">❌ ${e.message}</span>`;
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
}

window.send = send;
sendBtn.addEventListener('click', send);
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
});
