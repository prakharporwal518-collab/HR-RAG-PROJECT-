const $ = (s) => document.querySelector(s);

const SUGGESTIONS = [
  "How do I apply for leave?",
  "What should I do if I'm sick?",
  "Can I accept gifts from vendors?",
  "How do I report harassment (POSH)?",
  "Can I use AI tools like ChatGPT at work?",
  "What happens when I resign?",
  "What are the rules for remote work?",
];

const TOPICS = [
  ["🧭", "Purpose & HR Philosophy", "Scope and people principles"],
  ["🤝", "Employment & Recruitment", "Hiring, verification, mobility"],
  ["🚀", "Onboarding & Probation", "First weeks and confirmation"],
  ["⚖️", "Code of Conduct", "Professional standards"],
  ["⏰", "Working Hours & Attendance", "Overtime and flexibility"],
  ["🌴", "Leave & Time-Off", "Annual, sick and statutory leave"],
  ["💰", "Compensation & Payroll", "Salary, benefits, expenses"],
  ["📈", "Performance & Career", "Reviews, goals, growth"],
  ["🏠", "Remote / Hybrid Work", "Working from anywhere safely"],
  ["🔐", "Information Security", "Privacy and technology use"],
  ["💡", "IP & Confidentiality", "Protecting company knowledge"],
  ["🌈", "Equal Opportunity & POSH", "Anti-harassment and inclusion"],
  ["🎁", "Conflict of Interest & Gifts", "Hospitality and disclosure"],
  ["📣", "Social Media", "External communications"],
  ["🛡️", "Grievance & Whistleblowing", "Raising concerns safely"],
  ["❤️", "Health, Safety & Wellbeing", "A safe workplace"],
  ["📋", "Disciplinary Process", "Fair and documented steps"],
  ["👋", "Separation & Exit", "Resignation and handover"],
  ["🤖", "AI & Responsible Tech", "Using AI tools responsibly"],
  ["📜", "Policy Governance", "Updates and acknowledgement"],
];

const history = [];
let busy = false;
let me = null;      // signed-in user, or null
let status = null;  // knowledge-base status for this user

/* ---------- tiny safe markdown renderer ---------- */
const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function inline(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\s][^*]*?)\*/g, "$1<em>$2</em>")
    .replace(/\[(?:p\.|page)\s*(\d+)(?:[–-](\d+))?\]/gi, (_, a, b) =>
      `<button type="button" class="cite" data-page="${a}">p. ${a}${b ? "–" + b : ""}</button>`);
}

function markdown(md) {
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of md.split("\n")) {
    const line = raw.trim();
    let m;
    if (!line) { closeList(); continue; }
    if ((m = line.match(/^[-*•]\s+(.*)/))) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^\d+[.)]\s+(.*)/))) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^>\s?(.*)/))) {
      closeList(); out.push(`<blockquote>${inline(m[1])}</blockquote>`);
    } else {
      closeList(); out.push(`<p>${inline(line.replace(/^#+\s*/, ""))}</p>`);
    }
  }
  closeList();
  return out.join("");
}

/* ---------- chat ---------- */
const log = $("#chatLog");
const input = $("#question");

function addMessage(role, html) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = role === "bot" ? `<div class="avatar">Q</div><div class="bubble">${html}</div>` : `<div class="bubble">${html}</div>`;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el.querySelector(".bubble");
}

function renderSources(bubble, sources) {
  if (!sources.length) return;
  const d = document.createElement("details");
  d.className = "sources";
  d.innerHTML = `<summary>📄 ${sources.length} handbook passage${sources.length > 1 ? "s" : ""} used</summary>` +
    sources.map((s) => `<div class="source-item" data-page="${s.page}"><b>Page ${s.page} · ${esc(s.section)}</b>${esc(s.snippet)}</div>`).join("");
  bubble.appendChild(d);
}

function renderFeedback(bubble, { id, answered }) {
  const el = document.createElement("div");
  if (!answered) {
    el.innerHTML = `<span class="not-found-tag">Shared with HR as a policy gap</span>`;
    bubble.appendChild(el);
    return;
  }
  el.className = "feedback";
  el.innerHTML = `Was this helpful? <button type="button" data-v="1" aria-label="Helpful">👍</button><button type="button" data-v="0" aria-label="Not helpful">👎</button>`;
  el.addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    el.querySelectorAll("button").forEach((x) => { x.disabled = true; x.classList.toggle("chosen", x === b); });
    await fetch("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id, helpful: b.dataset.v === "1" }) });
    el.insertAdjacentHTML("beforeend", `<span>${b.dataset.v === "1" ? "Thanks!" : "Thanks, HR will review this."}</span>`);
  });
  bubble.appendChild(el);
}

async function ask(question) {
  question = question.trim();
  if (!question || busy) return;
  if (!me) { showGate(true); return; }
  busy = true;
  $("#sendBtn").disabled = true;
  addMessage("user", esc(question));
  input.value = "";
  autosize();

  const bubble = addMessage("bot", `<div class="typing"><i></i><i></i><i></i></div>`);
  const answerEl = document.createElement("div");
  let answer = "";
  let sources = [];
  let meta = null;

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history: history.slice(-6) }),
    });
    if (res.status === 401) { bubble.closest(".msg").remove(); me = null; renderAuth(); return; }
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const event = block.match(/^event: (.*)$/m)?.[1];
        const data = JSON.parse(block.match(/^data: (.*)$/m)?.[1] ?? "null");
        if (event === "sources") sources = data;
        if (event === "done") meta = data;
        if (event === "token") {
          if (!answer) { bubble.innerHTML = ""; bubble.appendChild(answerEl); }
          answer += data;
          answerEl.innerHTML = markdown(answer);
          log.scrollTop = log.scrollHeight;
        }
      }
    }
    renderSources(bubble, sources);
    if (meta) renderFeedback(bubble, meta);
    history.push({ role: "user", content: question }, { role: "assistant", content: answer });
  } catch (err) {
    bubble.innerHTML = `<p>⚠️ Sorry, something went wrong: ${esc(String(err.message || err))}</p>`;
  } finally {
    busy = false;
    $("#sendBtn").disabled = false;
    log.scrollTop = log.scrollHeight;
  }
}

// Clicking a [p. N] citation opens the sources list and highlights that page's passage.
log.addEventListener("click", (e) => {
  const cite = e.target.closest("button.cite");
  if (!cite) return;
  const bubble = cite.closest(".bubble");
  const details = bubble.querySelector(".sources");
  if (!details) return;
  details.open = true;
  const item = details.querySelector(`.source-item[data-page="${cite.dataset.page}"]`);
  if (item) {
    item.scrollIntoView({ behavior: "smooth", block: "nearest" });
    item.classList.add("flash");
    setTimeout(() => item.classList.remove("flash"), 1600);
  }
});

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 140) + "px";
}
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input.value); }
});
$("#chatForm").addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });

function askFromAnywhere(q) {
  $("#ask").scrollIntoView({ behavior: "smooth" });
  setTimeout(() => ask(q), 450);
}

/* ---------- suggestions + topics ---------- */
$("#suggestions").innerHTML = SUGGESTIONS.map((q) => `<button type="button">${esc(q)}</button>`).join("");
$("#suggestions").addEventListener("click", (e) => { if (e.target.tagName === "BUTTON") ask(e.target.textContent); });

$("#topicGrid").innerHTML = TOPICS.map(([ico, title, desc], i) =>
  `<button type="button" class="topic reveal" style="transition-delay:${(i % 4) * 0.07}s" data-title="${esc(title)}">
     <span class="ico">${ico}</span><span class="num">${String(i + 1).padStart(2, "0")}</span>
     <h3>${esc(title)}</h3><p>${esc(desc)}</p></button>`).join("");
$("#topicGrid").addEventListener("click", (e) => {
  const t = e.target.closest(".topic");
  if (t) askFromAnywhere(`Give me a short summary of the ${t.dataset.title} policy.`);
});

/* ---------- auth + status ---------- */
const api = (url, opts) => fetch(url, opts).then(async (r) => {
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
});

function showGate(show) { $("#signinGate").hidden = !show; }

function renderAuth(authInfo) {
  const nav = $("#navUser");
  if (!me) {
    nav.innerHTML = `<a href="#ask" class="btn btn-primary btn-sm" id="navSignIn">Sign in</a>`;
    showGate(true);
    $("#kbStatus").textContent = "Sign in to see the policies available to you.";
    return;
  }
  const initials = me.name.split(" ").map((w) => w[0]).slice(0, 2).join("").toUpperCase();
  nav.innerHTML =
    (me.is_admin ? `<button class="btn btn-ghost btn-sm" id="openInsights" type="button">Insights</button>
                    <button class="btn btn-ghost btn-sm" id="openAdmin" type="button">Upload policy</button>` : "") +
    `<span class="user-chip"><span class="initials">${esc(initials)}</span>${esc(me.name.split(" ")[0])}
       <span class="role">${me.is_admin ? "HR admin" : me.groups.includes("managers") ? "Manager" : "Employee"}</span></span>
     <button class="link-btn" id="signOut" type="button">Sign out</button>`;
  showGate(false);
  loadStatus();
}

async function initAuth() {
  const info = await api("/api/me").catch(() => null);
  if (!info) { $("#kbStatus").textContent = "Could not reach the server."; return; }
  me = info.user;
  if (info.auth.mode === "oidc") {
    $("#ssoBtn").hidden = false;
    $("#ssoName").textContent = info.auth.provider;
  } else {
    $("#devLogin").hidden = false;
    $("#demoUsers").innerHTML = info.demo_users.map((u) =>
      `<button type="button" data-email="${esc(u.email)}"><span class="avatar">${esc(u.name[0])}</span>
         <span><b>${esc(u.name)}</b><small>${esc(u.title || u.email)}</small></span></button>`).join("");
  }
  renderAuth();
}

async function devSignIn(email) {
  try {
    me = (await api("/auth/dev-login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email }) })).user;
    $("#loginMsg").textContent = "";
    renderAuth();
  } catch (err) { $("#loginMsg").textContent = err.message; }
}
$("#demoUsers").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) devSignIn(b.dataset.email); });
$("#devForm").addEventListener("submit", (e) => { e.preventDefault(); devSignIn($("#devEmail").value); });

$("#navUser").addEventListener("click", async (e) => {
  const id = e.target.closest("button, a")?.id;
  if (id === "signOut") {
    await fetch("/auth/logout", { method: "POST" });
    me = null; history.length = 0;
    log.querySelectorAll(".msg:not(.welcome)").forEach((m) => m.remove());
    renderAuth();
  }
  if (id === "openAdmin") openUpload();
  if (id === "openInsights") openInsights();
});

async function loadStatus() {
  try {
    status = await api("/api/status");
  } catch { $("#kbStatus").textContent = "Could not load the knowledge base."; return; }
  const docs = status.documents;
  const pages = docs.reduce((n, d) => n + d.pages, 0);
  $("#kbStatus").innerHTML =
    `<div><span class="dot"></span>${docs.length} document${docs.length !== 1 ? "s" : ""} · ${pages} pages available to you</div>` +
    `<div style="margin-top:6px"><span class="dot"></span>${status.retrieval === "hybrid" ? "Hybrid search (keywords + meaning)" : "Keyword search"}</div>` +
    `<div style="margin-top:6px"><span class="dot ${status.llm ? "" : "warn"}"></span>${status.llm ? `AI answers by ${status.llm_provider === "gemini" ? "Gemini" : "Claude"}` : "Passage mode (no API key)"}</div>`;
}

/* ---------- admin upload ---------- */
const modal = $("#adminModal");
const fileInput = $("#pdfFile");
const drop = $("#dropZone");
const msg = $("#uploadMsg");
function openUpload() {
  msg.textContent = "";
  $("#groupChoices").innerHTML = (status?.groups || ["everyone"]).map((g) =>
    `<label><input type="checkbox" name="groups" value="${esc(g)}" ${g === "everyone" ? "checked" : ""}/> ${esc(g)}</label>`).join("");
  modal.showModal();
}
fileInput.addEventListener("change", () => { $("#dropText").innerHTML = fileInput.files[0] ? `📄 <b>${esc(fileInput.files[0].name)}</b>` : "<b>Choose a PDF</b> or drag it here"; });
["dragover", "dragenter"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
drop.addEventListener("drop", (e) => { e.preventDefault(); fileInput.files = e.dataTransfer.files; fileInput.dispatchEvent(new Event("change")); });

$("#uploadForm").addEventListener("submit", async (e) => {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  const btn = $("#uploadBtn");
  btn.disabled = true;
  msg.className = "upload-msg";
  msg.textContent = "Uploading and indexing…";
  const fd = new FormData();
  fd.append("file", fileInput.files[0]);
  fd.append("groups", [...document.querySelectorAll('#groupChoices input:checked')].map((i) => i.value).join(","));
  try {
    const data = await api("/api/upload", { method: "POST", body: fd });
    msg.className = "upload-msg ok";
    msg.textContent = `✓ ${data.name} indexed. Visible to: ${data.groups.join(", ")}.`;
    loadStatus();
  } catch (err) {
    msg.className = "upload-msg err";
    msg.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

/* ---------- HR insights ---------- */
const fmtDate = (iso) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

async function openInsights() {
  $("#insightsModal").showModal();
  $("#gapRows").innerHTML = `<tr><td colspan="4" class="empty-row">Loading…</td></tr>`;
  try {
    const [d] = await Promise.all([api("/api/insights"), loadStatus()]);
    const rate = d.answer_rate == null ? "–" : Math.round(d.answer_rate * 100) + "%";
    $("#insightTiles").innerHTML = [[d.total, "questions asked"], [rate, "answered from policy"], [d.unanswered, "not in handbook"], [d.unhelpful, "marked not helpful"]]
      .map(([v, l]) => `<div class="tile"><b>${v}</b><span>${l}</span></div>`).join("");
    $("#gapRows").innerHTML = d.gaps.length ? d.gaps.map((g) => `<tr><td>${esc(g.question)}</td><td class="num">${g.count}</td>
        <td><span class="pill ${g.reason === "not_in_handbook" ? "warn" : "bad"}">${g.reason === "not_in_handbook" ? "Not in handbook" : "Not helpful"}</span></td>
        <td>${fmtDate(g.last_asked)}</td></tr>`).join("")
      : `<tr><td colspan="4" class="empty-row">No gaps yet. Every question so far was answered. 🎉</td></tr>`;
    $("#docRows").innerHTML = status.documents.map((doc) => `<tr><td>${esc(doc.name)}</td><td>${doc.pages}</td>
        <td>${doc.groups.map((g) => `<span class="pill">${esc(g)}</span>`).join("")}</td></tr>`).join("");
  } catch (err) {
    $("#gapRows").innerHTML = `<tr><td colspan="4" class="empty-row">${esc(err.message)}</td></tr>`;
  }
}

initAuth();

/* ---------- scroll craft: reveal, progress, parallax, counters, nav ---------- */
const io = new IntersectionObserver((entries) => {
  entries.forEach((en) => {
    if (!en.isIntersecting) return;
    en.target.classList.add("in");
    en.target.querySelectorAll("[data-count]").forEach(countUp);
    io.unobserve(en.target);
  });
}, { threshold: 0.15 });
document.querySelectorAll(".reveal").forEach((el) => io.observe(el));

function countUp(el) {
  const target = +el.dataset.count;
  const start = performance.now();
  const tick = (now) => {
    const p = Math.min((now - start) / 1200, 1);
    el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

const nav = $("#nav");
const bar = $("#scrollProgress");
const parallax = document.querySelectorAll("[data-parallax]");
const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
function onScroll() {
  const y = scrollY;
  const max = document.documentElement.scrollHeight - innerHeight;
  bar.style.width = `${max > 0 ? (y / max) * 100 : 0}%`;
  nav.classList.toggle("scrolled", y > 10);
  if (!reduce) parallax.forEach((el) => { el.style.transform = `translateY(${y * el.dataset.parallax}px)`; });
}
addEventListener("scroll", onScroll, { passive: true });
onScroll();

$("#navToggle").addEventListener("click", () => $("#navLinks").classList.toggle("open"));
$("#navLinks").addEventListener("click", (e) => { if (e.target.tagName === "A") $("#navLinks").classList.remove("open"); });
