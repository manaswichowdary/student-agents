const PHONES = ["mpatel21@asu.edu", "drao7@asu.edu", "skim14@asu.edu"];
const NEVER = {
  OPPORTUNITY_RELAY: "the sender's profile, why it didn't fit them, who else got it",
  VERIFY_REQUEST: "anything about either student: only the listing's own fields",
  VERIFY_RESULT: "the verifier's profile",
  AVAILABILITY_QUERY: "the asker's schedule",
  AVAILABILITY_REPLY: "the schedule, the exact location, why they're busy",
  PLAN_INVITE: "other invitees' answers or locations",
  PLAN_REPLY: "the reason for the answer",
  PLAN_UPDATE: "other riders' pickup spots",
};
let S = null, names = {}, forms = {};
const $ = id => document.getElementById(id);
const esc = t => String(t ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const first = e => names[e] || String(e || "").split("@")[0];
async function api(path, body) {
  const r = await fetch("/demo" + path, { method: body ? "POST" : "GET", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || "Something went wrong");
  return j;
}

function showShared(title, fields, type) {
  $("dTitle").textContent = title;
  const blocked = fields && fields.blocked;
  $("dBody").innerHTML = `<p class="note">${blocked ? "The bus blocked this message. Nothing crossed between the agents." : "Exactly these fields crossed between the two agents:"}</p>
    <pre>${esc(JSON.stringify(fields, null, 2))}</pre>
    ${NEVER[type] ? `<p class="note"><b>Never shared:</b> ${esc(NEVER[type])}.</p>` : ""}`;
  $("drawer").showModal();
}
$("dClose").onclick = () => $("drawer").close();

function phoneShell(email) {
  const el = document.createElement("article");
  el.className = "phone";
  el.innerHTML = `
    <div class="bar"><div><div class="who" data-k="who"></div><div class="sub" data-k="sub"></div></div><span class="chip" data-k="badge"></span></div>
    <div class="sec" data-k="onboard">
      <h3>Sign up with your .edu ID</h3>
      <div class="row"><input data-k="id" aria-label=".edu ID" placeholder="${email}"><button type="button" data-a="code">Send code</button></div>
      <div class="msg" data-k="mail" hidden></div>
      <div class="row" data-k="coderow" hidden><input data-k="code" inputmode="numeric" aria-label="6 digit code" placeholder="6 digit code"><button class="primary" type="button" data-a="verify">Verify</button></div>
      <div class="note warn" data-k="err"></div>
    </div>
    <div class="sec" data-k="app" hidden>
      <div class="sec"><h3>Requests</h3><div data-k="requests"></div></div>
      <div class="sec"><h3>Connect</h3>
        <div class="row"><input data-k="target" aria-label="Their .edu ID" placeholder="their asurite"><button type="button" data-a="connect">Send</button></div>
        <div class="msg" data-k="cmsg" hidden></div><div data-k="outgoing"></div></div>
      <div class="sec"><h3>Connections</h3><div data-k="friends"></div></div>
      <div class="sec"><h3>For you</h3><div data-k="feed"></div></div>
      <div class="sec"><h3>Plans</h3><div data-k="plans"></div></div>
      <div class="sec"><h3>Your agent</h3><div data-k="activity"></div>
        <div class="row"><select data-k="pick" aria-label="Listing to find"></select><button type="button" data-a="find">Find</button></div>
        <button type="button" data-a="squad">Plan a squad for the free Diwali dinner</button></div>
    </div>`;
  const q = k => el.querySelector(`[data-k="${k}"]`);
  el.addEventListener("click", async ev => {
    const b = ev.target.closest("button"); if (!b) return;
    const a = b.dataset.a, d = b.dataset;
    try {
      if (a === "code") { await api("/api/signup", { email: q("id").value || email }); q("err").textContent = "" }
      if (a === "verify") { await api("/api/verify", { email: q("id").value || email, code: q("code").value }); q("err").textContent = "" }
      if (a === "connect") { const r = await api("/api/connect", { user: email, target: q("target").value }); q("cmsg").textContent = r.message; q("cmsg").hidden = false; q("target").value = "" }
      if (a === "accept" || a === "deny") await api("/api/" + a, { user: email, other: d.o });
      if (a === "remove" || a === "block") await api("/api/" + a, { user: email, other: d.o });
      if (a === "find") await api("/api/find", { user: email, listing: q("pick").value });
      if (a === "squad") await api("/api/squad", { user: email });
      if (a === "yes" || a === "no") await api("/api/plan/answer", { user: email, plan_id: d.p, accept: a === "yes" });
      if (a === "confirm") await api("/api/plan/confirm", { user: email, plan_id: d.p });
      if (a === "shared") { const it = JSON.parse(d.j); showShared(it.t, it.f, it.ty) }
    } catch (e) { q("err").textContent = e.message; if (!q("onboard").hidden) {} else alertBox(el, e.message) }
    refresh();
  });
  el.addEventListener("change", async ev => {
    const t = ev.target; if (!t.dataset.perm) return;
    try { await api("/api/perm", { user: email, other: t.dataset.o, perm: t.dataset.perm, value: t.checked }) } catch (e) { alertBox(el, e.message) }
    refresh();
  });
  forms[email] = { el, q };
  return el;
}
function alertBox(el, text) { const n = el.querySelector('[data-k="activity"]'); n.insertAdjacentHTML("afterbegin", `<p class="note warn">${esc(text)}</p>`) }

function sharedBtn(title, fields, type) {
  return `<button class="link" type="button" data-a="shared" data-j='${esc(JSON.stringify({ t: title, f: fields, ty: type }))}'>What was shared</button>`;
}

function renderPhone(p) {
  const { q } = forms[p.email];
  q("onboard").hidden = p.verified; q("app").hidden = !p.verified;
  q("who").textContent = p.verified ? p.name : "New student";
  q("sub").textContent = p.verified ? `${p.major}, ${p.year}` : p.email;
  q("badge").textContent = p.verified ? "verified .edu" : "not verified";
  q("badge").className = "chip " + (p.verified ? "ok" : "warn");
  if (!p.verified) {
    q("mail").hidden = q("coderow").hidden = !p.code_sent;
    q("mail").textContent = p.code_sent ? `Mock email: your code is ${p.mock_code}` : "";
    return;
  }
  q("requests").innerHTML = p.requests.map(r => `<div class="card hl"><b>${esc(r.name)}</b><span class="meta">${esc(r.major)}, ${esc(r.year)} · verified name</span>
      <div class="row"><button class="primary" type="button" data-a="accept" data-o="${esc(r.from)}">Accept</button><button type="button" data-a="deny" data-o="${esc(r.from)}">Deny</button></div></div>`).join("")
    || `<p class="empty">No requests</p>`;
  q("outgoing").innerHTML = p.outgoing.map(o => `<p class="note">${esc(o.to)} · <span class="chip">${esc(o.status)}</span></p>`).join("");
  const label = { receive_relay: "Receive relayed opportunities", share_freebusy: "Share free/busy for Squad", share_distance: "Share rough distance" };
  q("friends").innerHTML = p.friends.map(f => `<div class="card"><div class="row" style="justify-content:space-between"><b>${esc(f.name)}</b><span class="meta">${esc(f.major)}</span></div>
      ${Object.entries(f.perms).map(([k, v]) => `<label class="toggle">${label[k]}<input type="checkbox" data-perm="${k}" data-o="${esc(f.email)}" ${v ? "checked" : ""}></label>`).join("")}
      <div class="row"><button class="danger" type="button" data-a="remove" data-o="${esc(f.email)}">Remove</button><button class="danger" type="button" data-a="block" data-o="${esc(f.email)}">Block</button></div></div>`).join("")
    || `<p class="empty">No connections yet</p>`;
  q("feed").innerHTML = p.feed.map(it => `<div class="card ${it.urgent ? "hl" : ""}"><b>${esc(it.title)}</b>
      <span class="meta">${esc(it.org)}${it.pay ? ` · $${esc(it.pay)}` : ""}</span>
      <span>${esc(it.line)}</span>
      <span class="meta">${it.verified ? '<span class="chip ok">verified by 2 agents</span> ' : ""}<span class="chip">${esc(it.fit_source)}</span> ${esc(it.why)}</span>
      ${it.msg_id ? sharedBtn("Relayed opportunity", it.shared, "OPPORTUNITY_RELAY") : ""}</div>`).join("")
    || `<p class="empty">Nothing yet</p>`;
  q("plans").innerHTML = p.plans.map(pl => {
    const host = pl.host === p.email;
    const st = pl.state;
    const chip = st === "confirmed" ? "ok" : st === "cancelled" || st === "rejected" ? "bad" : "warn";
    return `<div class="card"><div class="row" style="justify-content:space-between"><b>${esc(pl.title)}</b><span class="chip ${chip}">${esc(st)}</span></div>
      <span class="meta">${host ? `You're hosting · ${esc(pl.transport)} · ${pl.accepted.length} accepted` : `${esc(pl.host_name)}'s plan · you're a ${esc(pl.role)}`} · ${esc(pl.starts_label)}</span>
      ${pl.meet ? `<span><b>${esc(pl.meet)}</b></span>` : ""}
      ${!host && st === "invited" ? `<div class="row"><button class="primary" type="button" data-a="yes" data-p="${esc(pl.id)}">Accept</button><button type="button" data-a="no" data-p="${esc(pl.id)}">Decline</button></div>` : ""}
      ${host && st === "proposed" ? `<div class="row"><button class="primary" type="button" data-a="confirm" data-p="${esc(pl.id)}">Confirm plan</button></div>` : ""}
      ${!host && pl.shared ? sharedBtn("Plan message", pl.shared, "PLAN_UPDATE") : ""}</div>`;
  }).join("") || `<p class="empty">No plans</p>`;
  q("activity").innerHTML = p.activity.slice(0, 4).map(a => `<p class="note ${a.kind === "warn" ? "warn" : ""}">${esc(a.text)}</p>`).join("");
  const pick = q("pick");
  if (!pick.options.length) pick.innerHTML = S.listings.map(l => `<option value="${esc(l.key)}">${esc(l.title)} (${esc(l.source)})</option>`).join("");
}

function renderLog() {
  const rows = S.log;
  $("count").textContent = `${rows.filter(r => r.kind === "message").length} messages`;
  const atBottom = $("log").scrollTop + $("log").clientHeight >= $("log").scrollHeight - 30;
  $("log").innerHTML = rows.map(r => {
    if (r.kind === "event") return `<li class="${r.event === "step" ? "step" : "event"}"><span class="t">${r.event === "step" ? "step" : esc(r.event)}</span><span>${esc(r.text)}</span></li>`;
    const ok = r.result === "delivered";
    return `<li class="message ${ok ? "" : "rejected"}" data-n="${r.n}"><span class="t">${ok ? "✔ sent" : "✘ blocked"}</span>
      <span><span class="mtype">${esc(r.type)}</span> ${esc(first(r.sender))} → ${esc(first(r.recipient))}${ok ? "" : ` · <b>${esc(r.reason)}</b>`}</span></li>`;
  }).join("") || `<li class="event"><span class="t">ready</span><span>Press "Run demo scenario", or sign up on a phone.</span></li>`;
  if (atBottom) $("log").scrollTop = $("log").scrollHeight;
}
$("log").addEventListener("click", ev => {
  const li = ev.target.closest("li.message"); if (!li) return;
  const r = S.log.find(x => x.n == li.dataset.n);
  showShared(`${r.type}: ${first(r.sender)} → ${first(r.recipient)}`, r.result === "delivered" ? r.fields : { blocked: r.reason, delivered_fields: "none" }, r.type);
});

function renderTrust() {
  $("trust").innerHTML = S.trust.map(t => `<div class="tr"><div class="head"><span>${esc(t.source)}</span>
    <span><span class="chip ${t.label === "flagged" ? "bad" : t.label === "trusted" ? "ok" : ""}">${esc(t.label)}</span> <span class="num">${t.score.toFixed(2)}</span></span></div>
    <div class="meter ${t.label}"><i style="width:${(t.score * 100).toFixed(1)}%"></i><span class="tick" style="left:30%"></span><span class="tick" style="left:85%"></span></div></div>`).join("");
}

async function refresh() {
  S = await api("/api/state");
  names = S.names;
  $("clock").textContent = "Demo time: " + S.clock;
  $("run").disabled = S.running;
  $("run").textContent = S.running ? "Running…" : "Run demo scenario";
  S.phones.forEach(renderPhone);
  renderLog(); renderTrust();
}
let timer = null;
const soon = () => { clearTimeout(timer); timer = setTimeout(refresh, 120) };

PHONES.forEach(e => $("phones").appendChild(phoneShell(e)));
$("run").onclick = async () => { await api("/api/demo", {}); soon() };
$("reset").onclick = async () => { await api("/api/reset", {}); Object.values(forms).forEach(f => { f.q("pick").innerHTML = ""; f.q("cmsg").hidden = true; f.q("code").value = "" }); refresh() };
$("digest").onclick = async () => { await api("/api/digest", {}); refresh() };

function connect() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/demo/ws");
  ws.onmessage = soon;
  ws.onclose = () => setTimeout(connect, 1500);
}
setInterval(() => { if (S && S.running) refresh() }, 1500);   // backup in case the websocket can't connect
connect();
refresh();
