const {api,toast}=WakattaStudy;
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const time=s=>`${Math.floor((s||0)/60)}:${String(Math.floor((s||0)%60)).padStart(2,'0')}`;
let data,limit=36;
function href(s,revisit=false){return s.kind==='reading'?`/read/${s.metadata.work_id}?page=${revisit?1:Math.max(1,Math.round(s.position||1))}`:`/watch/${s.id}${revisit?'?from=0':''}`;}
function card(s){
 const ready=s.status==='ready'||s.kind==='reading';
 const detail=s.kind==='reading'?`${s.metadata.pages} pages`:(s.duration?`${Math.round(s.duration/60)} min`:(s.metadata.has_transcript?'Japanese subtitles available':'Video'));
 return `<article class="card"><div class="eyebrow">${esc(s.collection)}</div><p><a class="title" href="${href(s)}">${esc(s.title)}</a></p><p class="small muted">${esc(detail)} ${s.seconds?` · ${Math.round(s.seconds/60)} min revisited / explored`:''}</p>${s.metadata.warning?`<p class="notice small">${esc(s.metadata.warning)}</p>`:''}<div class="row"><a class="button ${ready?'primary':''}" href="${href(s)}">${s.kind==='reading'?'Read':ready?(s.position?'Continue':'Open'):'Prepare to watch'}</a>${s.position?`<span class="small muted">${s.kind==='reading'?'Page '+Math.round(s.position):time(s.position)}</span>`:''}</div></article>`;
}
function captureCard(c,review=false){return `<article class="card"><span class="badge">${esc(c.kind)}</span><p class="capture-text" lang="ja">${esc(c.expression)}</p><p class="small muted">${esc(c.collection)} · ${esc(c.title)} · ${time(c.start)}</p><a class="button ${review?'primary':''}" href="/watch/${c.source_id}?cue=${c.cue_id}&capture=${c.id}">${review?'Listen & revisit':'Return to context'}</a></article>`;}
function renderSources(){
 const q=$('search').value.toLowerCase(),kind=$('kind').value,col=$('collection').value;
 const sources=data.sources.filter(s=>(!kind||s.kind===kind)&&(!col||s.collection===col)&&(!q||(s.title+' '+s.collection).toLowerCase().includes(q))&&(!$('ready').checked||s.status==='ready'||s.kind==='reading'));
 $('libraryStats').textContent=`${sources.length} items · ${sources.filter(s=>s.status==='ready'||s.kind==='reading').length} ready`;
 $('sources').innerHTML=sources.slice(0,limit).map(card).join('')||'<p class="empty">No matching items yet.</p>';
 $('more').hidden=sources.length<=limit;
}
async function load(){
 try{
 data=await api('/api/study/library');
 const cols=[...new Set(data.sources.map(s=>s.collection))].sort();
 $('collection').innerHTML='<option value="">All collections</option>'+cols.map(c=>`<option>${esc(c)}</option>`).join('');
 const continuing=data.sources.filter(s=>s.position&&(s.status==='ready'||s.kind==='reading')&&(!s.duration||s.position<s.duration*.98)).sort((a,b)=>(b.updated_at||'').localeCompare(a.updated_at||'')).slice(0,3);
 const initial=data.sources.filter(s=>s.status==='ready').slice(0,3);
 $('continue').innerHTML=(continuing.length?continuing:initial).map(card).join('')||'<p class="empty">Choose something below to begin. Your place will be saved here.</p>';
 const due=data.captures.filter(c=>c.next_review_at<=data.now).slice(0,6);
 const familiar=data.sources.filter(s=>s.updated_at&&Date.parse(data.now)-Date.parse(s.updated_at)>=86400000).slice(0,3);
 $('revisit').innerHTML=due.map(c=>captureCard(c,true)).join('')+familiar.map(s=>`<article class="card"><h3>${esc(s.collection)} · ${esc(s.title)}</h3><p class="muted small">You spent time with this before. Try a familiar part again.</p><a class="button" href="${href(s,true)}">Revisit from the beginning</a></article>`).join('')||'<p class="empty">Your first captures will appear here tomorrow. You can revisit them any time below.</p>';
 $('captures').innerHTML=data.captures.slice().sort((a,b)=>b.id-a.id).slice(0,18).map(c=>captureCard(c)).join('')||'<p class="empty">Tap a subtitle word or save a line while watching.</p>';
 $('moreCaptures').hidden=data.captures.length<=18;
 $('practice').innerHTML=data.practice.slice(0,6).map(e=>`<article class="card"><span class="badge">${e.action==='sing'?'Sang along':'Shadowed'}</span><p>${esc(e.title)} · ${time(e.position)}</p><a href="/watch/${e.source_id}?cue=${e.cue_id}">Practice this span again</a><p class="small muted">${new Date(e.created_at).toLocaleString()}</p></article>`).join('')||'<p class="empty">Mark a line after shadowing it or singing along. These are your own practice records.</p>';
 renderSources();
 }catch(e){$('error').textContent=e.message;}
}
for(const id of ['search','kind','collection','ready'])$(id).addEventListener('input',()=>{limit=36;renderSources();});
$('more').onclick=()=>{limit+=36;renderSources();};
$('moreCaptures').onclick=()=>{$('captures').innerHTML=data.captures.slice().sort((a,b)=>b.id-a.id).map(c=>captureCard(c)).join('');$('moreCaptures').hidden=true;};
$('youtube').onsubmit=async e=>{
 e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;$('youtubeStatus').textContent='Adding…';
 try{const r=await api('/api/study/youtube',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:new FormData(e.target).get('url')})});location.href=`/watch/${r.source_id}`;}
 catch(err){$('youtubeStatus').textContent=err.message;b.disabled=false;}
};
$('upload').onsubmit=async e=>{
 e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;$('uploadStatus').textContent='Adding…';
 try{const form=new FormData(e.target);if(!form.get('transcript')?.size)form.delete('transcript');const r=await api('/api/study/upload',{method:'POST',body:form});location.href=`/watch/${r.source_id}`;}
 catch(err){$('uploadStatus').textContent=err.message;button.disabled=false;}
};
load();WakattaStudy.flush();
