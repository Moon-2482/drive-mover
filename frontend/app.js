async function jget(p){ const r = await fetch(p); return r.json(); }
function fmt(n){ n = +n || 0; const u = ["B","KB","MB","GB","TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1){ n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(1)) + " " + u[i]; }
function esc(s){ return String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

async function loadStatus(){
  try{
    const s = await jget("/api/status");
    const el = document.getElementById("status");
    if (s.connected){
      el.innerHTML = "✅ Connected: <b>" + esc(s.account.name || "") + "</b> (" +
        esc(s.account.email || "") + ")<br><small>Drive use: " +
        fmt(s.quota.usageInDrive) + " / " + fmt(s.quota.limit) + "</small>";
    } else if (s.needs_connect){
      el.innerHTML = "Drive connect nahi hai.<br><a href='/auth/login'><button style='margin-top:8px'>🔗 Connect Google Drive</button></a>";
    } else {
      el.textContent = "❌ Drive connect nahi: " + (s.error || "?");
    }
  }catch(e){ document.getElementById("status").textContent = "Status load nahi hua."; }
}

function batchHtml(b){
  const tot = b.total_files || 0, done = b.done_files || 0;
  const pct = tot ? Math.round(done / tot * 100) : (b.status === "completed" ? 100 : 0);
  const live = (b.status === "running") ?
    ' <span class="live">● LIVE</span> <small>' +
    new Date().toLocaleTimeString("en-GB") + '</small>' : "";
  let h = '<div class="batch"><h3>' + esc(b.combined_folder_name || b.name || "Batch") +
    ' <small>#' + b.id.slice(0, 8) + '</small></h3>' +
    '<div>Status: <b>' + esc(b.status) + '</b>' + live + ' — Links: ' + b.done_links + '/' +
    b.total_links + ' — Files: ' + done + '/' + tot + ' (' + fmt(b.bytes_done) + ' / ' +
    fmt(b.total_bytes) + ')</div><div class="bar"><i style="width:' + pct + '%"></i></div>';
  if (b.status === "running" || b.status === "paused"){
    h += '<div class="actions"><button data-act="pause" data-id="' + b.id +
      '">Pause</button><button data-act="resume" data-id="' + b.id +
      '">Resume</button><button data-act="cancel" data-id="' + b.id +
      '">Cancel</button></div>';
  }
  if (b.failed_links > 0 && (b.status === "completed" || b.status === "failed")){
    h += '<div class="actions"><button data-act="retry-failed" data-id="' + b.id +
      '">Retry failed</button></div>';
  }
  for (const l of b.links){
    h += '<div class="link"><span class="st ' + l.status + '">' + l.status + '</span>' +
      '<b>' + l.seq + '.</b> ' + esc(l.dest_folder_name || l.source_name || l.kind) +
      ' <small>(' + esc(l.kind) + ')</small><br><small>' + esc(l.url) + '</small>';
    if (l.status === "running" && !l.total_bytes && l.bytes_done > 0){
      h += '<br><small>⬇ Download ho raha hai: <b>' + fmt(l.bytes_done) + '</b> — ' +
        l.done_files + ' files abhi tak</small><div class="bar indet"><i></i></div>';
    } else if (l.total_files){
      const lp = Math.round(l.done_files / l.total_files * 100);
      h += '<br><small>Files: ' + l.done_files + '/' + l.total_files +
        ' — ' + fmt(l.bytes_done) + ' / ' + fmt(l.total_bytes) + '</small>' +
        '<div class="bar"><i style="width:' + lp + '%"></i></div>';
    }
    if (l.current_file) h += '<small>▶ ' + esc(l.current_file) + '</small>';
    if (l.error) h += '<br><small style="color:#ff8a80">⚠ ' + esc(l.error) + '</small>';
    h += '</div>';
  }
  return h + '</div>';
}

let showDone = false;
async function loadBatches(){
  try{
    const d = await jget("/api/batches");
    const all = d.batches || [];
    const active = all.filter(b => b.status === "running" || b.status === "paused");
    const done = all.filter(b => !(b.status === "running" || b.status === "paused"));
    document.getElementById("batches").innerHTML =
      active.map(batchHtml).join("") ||
      "<p class='hint'>Abhi koi migration nahi chal rahi. Upar link paste karke Migrate dabayein.</p>";
    const t = document.getElementById("toggleDone");
    t.style.display = done.length ? "" : "none";
    t.textContent = showDone ? "✓ Moved data chhupayein" : "✓ Moved data dekhein (" + done.length + ")";
    document.getElementById("doneBatches").innerHTML =
      showDone ? done.map(batchHtml).join("") : "";
  }catch(e){}
}

document.getElementById("toggleDone").addEventListener("click", () => { showDone = !showDone; loadBatches(); });

document.getElementById("go").addEventListener("click", async () => {
  const links = document.getElementById("links").value.split("\n").filter(s => s.trim());
  const name = document.getElementById("folderName").value.trim();
  if (!links.length){ alert("Pehle ek link paste karein."); return; }
  const btn = document.getElementById("go");
  btn.disabled = true; btn.textContent = "Shuru ho raha hai...";
  try{
    const r = await fetch("/api/batches", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: name, links: links})});
    const d = await r.json();
    if (!r.ok){ alert(d.error || "Error"); }
    else { document.getElementById("links").value = ""; loadBatches(); }
  }catch(e){ alert("Server se rabta nahi: " + e); }
  btn.disabled = false; btn.textContent = "⬇️ Migrate — One Click";
});

document.getElementById("batches").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  await fetch("/api/batches/" + b.dataset.id + "/" + b.dataset.act, {method: "POST"});
  loadBatches();
});

loadStatus(); loadBatches();
setInterval(loadBatches, 3000);
setInterval(loadStatus, 30000);
