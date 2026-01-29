function setPoolVisible(visible){
  const drawer = document.getElementById("poolDrawer");
  const mini = document.getElementById("poolMini");
  if(!drawer || !mini) return;

  if(visible){
    drawer.style.visibility = "visible";
    drawer.style.width = "360px";
    drawer.style.opacity = "1";
    mini.style.visibility = "hidden";
    mini.style.width = "0";
    mini.style.opacity = "0";
    localStorage.setItem("poolVisible", "1");
  }else{
    drawer.style.visibility = "hidden";
    drawer.style.width = "0";
    drawer.style.opacity = "0";
    mini.style.visibility = "visible";
    mini.style.width = "78px";
    mini.style.opacity = "1";
    localStorage.setItem("poolVisible", "0");
  }
}

function togglePool(){
  const drawer = document.getElementById("poolDrawer");
  if(!drawer) return;
  const visible = drawer.style.visibility !== "hidden" && drawer.style.width !== "0";
  setPoolVisible(!visible);
}

async function unlockGamemaster(){
  const pw = prompt("Enter Game Master password:");
  if(pw === null) return;

  const form = new FormData();
  form.append("password", pw);

  const res = await fetch("/gamemaster/unlock", { method: "POST", body: form });
  const data = await res.json().catch(()=>({error:"Unlock failed"}));

  if(!res.ok){
    alert(data.error || "Unlock failed");
    return;
  }
  window.location.reload();
}

async function postMove(instanceId, status){
  const form = new FormData();
  form.append("status", status);

  const res = await fetch(`/instances/${instanceId}/move`, { method: "POST", body: form });
  const data = await res.json().catch(()=>({error:"Move failed"}));

  if(!res.ok){
    throw new Error(data.error || "Move failed");
  }
  return data;
}

async function saveDetails(instanceId){
  const ta = document.querySelector(`[data-details-for="${instanceId}"]`);
  if(!ta) return;

  const form = new FormData();
  form.append("details", ta.value);

  const res = await fetch(`/instances/${instanceId}/details`, { method: "POST", body: form });
  const data = await res.json().catch(()=>({error:"Save failed"}));

  if(!res.ok){
    alert(data.error || "Save failed");
  }
}

async function instantiateTemplate(templateId, actingKidId, targetStatus){
  const form = new FormData();
  form.append("acting_kid_id", actingKidId);
  form.append("target_status", targetStatus);

  const res = await fetch(`/templates/${templateId}/instantiate`, { method: "POST", body: form });
  const data = await res.json().catch(()=>({error:"Instantiate failed"}));

  if(!res.ok){
    throw new Error(data.error || "Instantiate failed");
  }
  return data;
}

async function persistOrder(status, containerEl){
  const ids = Array.from(containerEl.querySelectorAll("[data-instance-id]"))
    .map(el => el.getAttribute("data-instance-id"))
    .filter(Boolean);

  const form = new FormData();
  form.append("status", status);
  form.append("ordered_ids", ids.join(","));
  form.append("filter_kid", containerEl.getAttribute("data-filter-kid") || "");

  const res = await fetch(`/instances/reorder`, { method:"POST", body: form });
  if(!res.ok){
    console.warn("order persist failed");
  }
}

function initPoolSortable(){
  const pool = document.getElementById("poolList");
  if(!pool) return;

  new Sortable(pool, {
    group: { name: "kanban", pull: "clone", put: false },
    sort: false,
    animation: 120,
  });
}

function initColumnSortable(columnId){
  const el = document.getElementById(columnId);
  if(!el) return;

  // Prevent drops into the Done (Claim Reward) column
  const isDoneColumn = columnId === "colDone";

  new Sortable(el, {
    group: { name: "kanban", pull: true, put: !isDoneColumn },
    animation: 120,

    onAdd: async function (evt) {
      const status = evt.to.getAttribute("data-status");

      const templateId = evt.item.getAttribute("data-template-id");
      if(templateId){
        const pool = document.getElementById("poolList");
        const actingKidId = pool?.getAttribute("data-acting-kid");
        if(!actingKidId || actingKidId === "None"){
          alert("No active player selected.");
          evt.item.remove();
          return;
        }
        try{
          await instantiateTemplate(templateId, actingKidId, status);
          window.location.reload();
        }catch(e){
          alert(e.message || "Could not create task");
          evt.item.remove();
        }
        return;
      }

      const instanceId = evt.item.getAttribute("data-instance-id");
      if(instanceId && status){
        try{
          await postMove(instanceId, status);
          await persistOrder(status, evt.to);
          window.location.reload();
        }catch(e){
          alert(e.message || "Move failed");
          window.location.reload();
        }
      }
    },

    onUpdate: async function(evt){
      const status = evt.to.getAttribute("data-status");
      await persistOrder(status, evt.to);
    }
  });
}

/* --------------------------
   Confetti (simple fullscreen)
--------------------------- */
function launchConfetti(spawnMs = 5000){
  if(document.getElementById("confetti-canvas")) return;

  const canvas = document.createElement("canvas");
  canvas.id = "confetti-canvas";
  canvas.style.position = "fixed";
  canvas.style.top = "0";
  canvas.style.left = "0";
  canvas.style.width = "100vw";
  canvas.style.height = "100vh";
  canvas.style.pointerEvents = "none";
  canvas.style.zIndex = "999999";
  document.body.appendChild(canvas);

  const ctx = canvas.getContext("2d");

  const resize = () => {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.floor(window.innerWidth));
    const h = Math.max(1, Math.floor(window.innerHeight));
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };
  resize();
  window.addEventListener("resize", resize);

  const colors = ["#22c55e","#60a5fa","#f59e0b","#ef4444","#a78bfa","#14b8a6"];
  const rand = (a,b) => a + Math.random() * (b-a);

  const pieces = Array.from({length: 280}).map(() => ({
    x: rand(0, window.innerWidth),
    y: rand(-window.innerHeight * 0.3, 0),
    vx: rand(-4.5, 4.5),
    vy: rand(2.5, 9.5),
    size: rand(4, 10),
    rot: rand(0, Math.PI * 2),
    vr: rand(-0.25, 0.25),
    color: colors[Math.floor(Math.random() * colors.length)],
    shape: Math.random() < 0.55 ? "rect" : "circle",
    active: true,
  }));

  const start = performance.now();

  // Let confetti fall 125% lower than the viewport before considering "done"
  const bottomLimit = () => window.innerHeight * 2.25 + 60;

  function allOffscreen(){
    const limit = bottomLimit();
    for(const p of pieces){
      if(p.active && p.y < limit) return false;
    }
    return true;
  }

  function tick(now){
    const elapsed = now - start;

    ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);

    for(const p of pieces){
      if(!p.active) continue;

      p.x += p.vx;
      p.y += p.vy;
      p.vy += 0.22;   // gravity
      p.vx *= 0.85;  // drag
      p.rot += p.vr;

      if(p.y > bottomLimit()){
        p.active = false;
        continue;
      }

      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rot);
      ctx.fillStyle = p.color;

      if(p.shape === "rect"){
        ctx.fillRect(-p.size/2, -p.size/2, p.size, p.size * 0.65);
      }else{
        ctx.beginPath();
        ctx.arc(0, 0, p.size/2, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    }

    // Keep animating after spawnMs; remove only when everything fell below 125%
    if(elapsed >= spawnMs && allOffscreen()){
      window.removeEventListener("resize", resize);
      canvas.remove();
      return;
    }

    requestAnimationFrame(tick);
  }

  requestAnimationFrame(tick);
}

function handleCollectSubmit(e){
  e.preventDefault();
  const form = e.target;

  const btn = form.querySelector("button[type='submit']");
  if(btn) btn.disabled = true;

  launchConfetti(5000);

  setTimeout(() => form.submit(), 1500);
  return false;
}

/* --------------------------
   End of Confetti
--------------------------- */

window.addEventListener("DOMContentLoaded", () => {
  const vis = localStorage.getItem("poolVisible");
  if(vis === "0") setPoolVisible(false);
  else setPoolVisible(true);

  initPoolSortable();
  initColumnSortable("colDoing");
  initColumnSortable("colReview");
  initColumnSortable("colDone");
});

window.saveDetails = saveDetails;
window.togglePool = togglePool;
window.unlockGamemaster = unlockGamemaster;
window.handleCollectSubmit = handleCollectSubmit;
