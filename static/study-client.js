// Durable, idempotent activity transport shared by the reader and player.
// One storage key per event avoids two tabs overwriting one another's queues.
window.WakattaStudy = (() => {
  const prefix = 'wakatta-activity-v1:';
  let flushing = false;
  const memory = new Map();
  const uuid = () => globalThis.crypto?.randomUUID?.() ||
    '10000000-1000-4000-8000-100000000000'.replace(/[018]/g,c =>
      (Number(c)^crypto.getRandomValues(new Uint8Array(1))[0]&15>>Number(c)/4).toString(16));
  async function api(url, options={}) {
    const r = await fetch(url,options);
    const data = await r.json().catch(()=>({}));
    if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${r.status})`);
    return data;
  }
  function pending() {
    const events = new Map(memory);
    try {
      for (let i=0;i<localStorage.length;i++) {
        const key=localStorage.key(i);
        if (key?.startsWith(prefix)) {
          try {const e=JSON.parse(localStorage.getItem(key)); events.set(e.event_id,e);} catch {}
        }
      }
    } catch {}
    return [...events.values()];
  }
  function status(text) {document.querySelectorAll('[data-sync-status]').forEach(n=>n.textContent=text);}
  function record(event) {
    const e={...event,event_id:uuid(),occurred_at:new Date().toISOString()};
    try {localStorage.setItem(prefix+e.event_id,JSON.stringify(e));}
    catch {memory.set(e.event_id,e);status('Progress kept in this tab; browser storage is unavailable');}
    flush();
  }
  async function flush() {
    if(flushing) return;
    const events=pending().slice(0,100);
    if(!events.length) return;
    flushing=true;
    try {
      await api('/api/study/events',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({events}),keepalive:true});
      for(const e of events) {memory.delete(e.event_id);try{localStorage.removeItem(prefix+e.event_id);}catch{}}
      status('Progress saved');
    } catch {status('Progress waiting to sync');}
    finally {flushing=false;}
  }
  let toastTimer;
  function toast(message) {
    let n=document.getElementById('toast');
    if(!n){n=document.createElement('div');n.id='toast';n.setAttribute('role','status');document.body.append(n);}
    n.textContent=message;clearTimeout(toastTimer);toastTimer=setTimeout(()=>n.textContent='',4000);
  }
  function trackReading(sourceId,getPage) {
    let last=performance.now(),lastActive=last;
    for(const event of ['pointerdown','keydown','scroll']) document.addEventListener(event,()=>lastActive=performance.now(),{passive:true,capture:true});
    function tick(){
      const t=performance.now(),seconds=Math.min(15,(t-last)/1000);last=t;
      const page=getPage();
      if(document.hidden||t-lastActive>90000||!page||seconds<1)return;
      record({source_id:sourceId,action:'read',position:page,end_position:page,seconds});
    }
    setInterval(tick,15000);addEventListener('pagehide',tick);
  }
  addEventListener('online',flush);addEventListener('pagehide',flush);
  setInterval(flush,15000);
  return {api,record,flush,toast,uuid,trackReading};
})();
