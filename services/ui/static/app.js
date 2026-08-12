// AAH Sample UI — 삼성생명 mockup + AI 채팅
const msgs = document.getElementById('messages');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const fab = document.getElementById('chat-fab');
const panel = document.getElementById('chat-panel');

// AgentCore 는 runtimeSessionId 를 33자 이상으로 요구한다. crypto.randomUUID 는
// 보안 컨텍스트(HTTPS·localhost)에서만 있어서, http 로 열면 undefined 로 떨어진다.
// 예전 폴백(Math.random().toString(36).slice(2))은 11자 남짓이라 호출이 거부됐고,
// 화면엔 그 이유가 안 보인 채 "(빈 응답)" 만 남았다.
function newSessionId() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  let s = '';
  while (s.length < 36) s += Math.random().toString(36).slice(2);
  return s.slice(0, 36);
}

let sessionId = newSessionId();
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
  sessionId = newSessionId();
  msgs.innerHTML = '';
  addMsg('assistant', '새 대화를 시작합니다. 무엇이 궁금하신가요?');
}
window.openChat = openChat;
window.closeChat = closeChat;
window.newSession = newSession;

// ── Markdown 렌더링 ────────────────────────────────────────────────
// 모델 답변은 **굵게**·목록·표 같은 마크다운을 쓴다. 그대로 두면 별표가 그대로
// 보이므로 최소한만 해석한다. 라이브러리를 쓰지 않는 이유는 컨테이너가 정적
// 파일만 서빙하기 때문이다(외부 CDN 은 폐쇄망에서 못 받는다).
//
// XSS — 먼저 전부 이스케이프하고 그 뒤에 서식을 입힌다. 순서가 바뀌면 모델이
// 뱉은 <script> 가 그대로 실행된다.
function mdEscape(s) {
  return s.replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function mdInline(s) {
  return s
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
             '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
}

function mdToHtml(src) {
  // 코드블록·인라인코드는 안쪽에 서식을 입히면 안 되므로 먼저 빼둔다
  const holes = [];
  const stash = (html) => { holes.push(html); return `@@CTPHOLE${holes.length - 1}@@`; };

  let s = String(src || '').replace(/```[\w-]*\n?([\s\S]*?)```/g,
    (m, code) => stash(`<pre class="md-pre"><code>${mdEscape(code.replace(/\n$/, ''))}</code></pre>`));
  s = mdEscape(s);
  s = s.replace(/`([^`\n]+)`/g, (m, c) => stash(`<code class="md-code">${c}</code>`));

  const out = [];
  let list = null;                       // 'ul' | 'ol' | null
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (const raw of s.split('\n')) {
    const line = raw.replace(/\s+$/, '');
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    const tr = line.match(/^\s*\|(.+)\|\s*$/);
    const bq = line.match(/^\s*&gt;\s?(.*)$/);

    if (h) { closeList(); out.push(`<h${h[1].length + 2} class="md-h">${mdInline(h[2])}</h${h[1].length + 2}>`); continue; }
    if (ul) { if (list !== 'ul') { closeList(); out.push('<ul class="md-list">'); list = 'ul'; }
              out.push(`<li>${mdInline(ul[1])}</li>`); continue; }
    if (ol) { if (list !== 'ol') { closeList(); out.push('<ol class="md-list">'); list = 'ol'; }
              out.push(`<li>${mdInline(ol[1])}</li>`); continue; }
    if (bq) { closeList(); out.push(`<blockquote class="md-quote">${mdInline(bq[1])}</blockquote>`); continue; }
    if (tr) { // 표 구분선(|---|---|)은 버리고 나머지 행만 살린다
      closeList();
      const cells = tr[1].split('|').map((c) => c.trim());
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;
      out.push(`<div class="md-row">${cells.map((c) => `<span>${mdInline(c)}</span>`).join('')}</div>`);
      continue;
    }
    closeList();
    // 코드블록 자리표시자만 있는 줄은 <p> 로 감싸지 않는다 — <p><pre> 는
    // 유효하지 않아 브라우저가 태그를 끊어버린다.
    if (/^@@CTPHOLE\d+@@$/.test(line)) { out.push(line); continue; }
    out.push(line ? `<p class="md-p">${mdInline(line)}</p>` : '');
  }
  closeList();
  return out.join('').replace(/@@CTPHOLE(\d+)@@/g, (m, i) => holes[Number(i)]);
}

// ── 실행 과정(step) 표시 ────────────────────────────────────────────
// Control Plane 의 AgentSteps 와 같은 규칙이다. 워크플로우·팀은 노드마다
// step_begin(running) → step(ok) 이 흘러오고 title·agent_name·model·detail 이
// 들어있어 무슨 일이 벌어지는지 그대로 보인다. 실행 중엔 펼쳐 두고, 끝나면
// 접어서 답변을 가리지 않는다.
//
// trigger·end 는 배선이라 사용자에게 의미가 없어 뺀다.
const STEP_HIDE = ['trigger', 'end'];

function stepLabel(s) {
  return s.agent_name || s.title || s.node_id || '';
}

function mergeStep(list, d) {
  const n = Number(d.step || 0);
  const prev = list.find((x) => x.step === n) || {};
  const st = {
    step: n,
    node_id: d.node_id || prev.node_id || '',
    node_type: d.node_type || prev.node_type || '',
    status: d.status || prev.status || '',
    title: d.title || d.label || prev.title || '',
    agent_name: d.agent_name || prev.agent_name || '',
    model: d.model || prev.model || '',
    detail: d.detail || prev.detail || '',
  };
  return list.filter((x) => x.step !== n).concat([st]).sort((a, b) => a.step - b.step);
}

function renderSteps(box, list, running) {
  const shown = list.filter((s) => !STEP_HIDE.includes(s.node_type));
  if (!shown.length) { box.innerHTML = ''; box.classList.add('hidden'); return; }
  box.classList.remove('hidden');

  const cur = [...shown].reverse().find((s) => s.status === 'running');
  const open = running || box.dataset.open === '1';
  const head = running
    ? `<span class="steps-spin"></span>${cur ? mdEscape(stepLabel(cur)) + ' 실행 중…' : '실행 중…'}`
    : `<span class="steps-caret">${open ? '▾' : '▸'}</span>과정 ${shown.length}단계 보기`;

  const rows = !open ? '' : shown.map((s) => {
    const dot = s.status === 'running' ? 'run' : s.status === 'ok' ? 'ok'
              : s.status === 'blocked' ? 'bad' : 'warn';
    const model = s.model ? ` · ${mdEscape(s.model.split('.').pop())}` : '';
    const detail = s.detail ? `<div class="step-detail">${mdEscape(s.detail)}</div>` : '';
    return `<div class="step-row"><span class="step-dot ${dot}"></span>`
         + `<div class="step-body"><div class="step-title">${mdEscape(stepLabel(s))}`
         + `<span class="step-model">${model}</span></div>${detail}</div></div>`;
  }).join('');

  box.innerHTML = `<button type="button" class="steps-head">${head}</button>`
                + (open ? `<div class="steps-list">${rows}</div>` : '');
  box.querySelector('.steps-head').onclick = () => {
    box.dataset.open = box.dataset.open === '1' ? '0' : '1';
    renderSteps(box, list, running);
  };
}

function addMsg(role, text, opts = {}) {
  const el = document.createElement('div');
  el.className = `msg ${role}`;
  if (opts.thinking) {
    el.innerHTML = `<span class="thinking"><span class="spinner"></span>${text}</span>`;
  } else if (role === 'assistant') {
    el.innerHTML = mdToHtml(text);       // 모델 답변만 마크다운 해석
  } else {
    el.textContent = text;               // 사용자 입력은 그대로
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
  const stepsBox = document.createElement('div');
  stepsBox.className = 'steps hidden';
  msgs.appendChild(stepsBox);
  let steps = [];
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
    let errored = false;

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

        if (ev === 'step' || ev === 'step_begin' || ev === 'trace') {
          steps = mergeStep(steps, parsed);
          renderSteps(stepsBox, steps, true);
          msgs.scrollTop = msgs.scrollHeight;
        } else if (ev === 'token') {
          if (!started) { assistantEl.textContent = ''; started = true; }
          fullText += parsed.text || '';
          assistantEl.innerHTML = mdToHtml(fullText);
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
          errored = true;
          assistantEl.innerHTML = `<span style="color:#ef4444">❌ ${parsed.error || 'error'}</span>`;
        }
      }
    }
    renderSteps(stepsBox, steps, false);
    // 에러를 이미 보여줬으면 덮지 않는다 — 원인이 가려지면 디버깅이 불가능하다
    if (!started && !fullText && !errored) {
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
