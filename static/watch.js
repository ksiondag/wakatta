const {api,record,toast,uuid}=WakattaStudy;
const $=id=>document.getElementById(id);
const video=$('media'),sourceId=Number(location.pathname.split('/').pop());
const params=new URLSearchParams(location.search);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stamp=s=>`${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`;
let source,cues=[],captures=[],active=-2,word=null,wordCue=null,replayEnd=null,replayStart=null;
let loaded=false,subtitleShift=0,currentCapture=null,playbackAction='watch',track;
let lastSample=null,span=null,lastFlush=performance.now();
const cueMap=new Map();

function tokensHTML(cue){
 const chars=Array.from(cue.text);let pos=0,out='';
 for(const [i,t] of cue.tokens.entries()){
  out+=esc(chars.slice(pos,t.start).join(''));
  const reading=/[\p{Script=Han}々〆]/u.test(t.surface)&&t.reading&&t.reading!==t.surface
    ?t.reading:'';
  out+=`<button class="token" data-token="${i}" data-cue="${cue.id}"${reading?` data-reading="${esc(reading)}" aria-label="${esc(t.surface)} (${esc(reading)})"`:''} title="Look up ${esc(t.surface)}">${esc(t.surface)}</button>`;
  pos=t.end;
 }
 return out+esc(chars.slice(pos).join(''));
}
function transcript(){
 const q=$('transcriptSearch').value;
 $('transcript').innerHTML=cues.filter(c=>!q||c.text.includes(q)).map(c=>`<div class="cue${c.ordinal===active?' active':''}" id="cue-${c.id}" data-cue="${c.id}"><button class="time" data-seek="${c.id}" aria-label="Play from ${stamp(c.start)}">${stamp(c.start)}</button><button class="cue-save" data-save="${c.id}" aria-label="Save line at ${stamp(c.start)}">Save</button><span class="cue-text">${tokensHTML(c)||'<span class="muted">♪ / annotation</span>'}</span></div>`).join('')||'<p class="empty">No matching timed lines. Add a timed transcript to make this source interactive.</p>';
}
function showCue(index){
 if(index===active)return;
 const previous=cues[active];active=index;
 if(previous)$(`cue-${previous.id}`)?.classList.remove('active');
 const cue=cues[active];
 $('currentLine').dataset.cue=cue?.id||'';
 $('currentLine').innerHTML=cue?tokensHTML(cue)||'<span class="muted">♪</span>':'<span class="muted">Listen…</span>';
 $('rawLine').textContent=cue?.raw_text||'';
 for(const id of ['savePhrase','saveGrammar','replay','shadow','sing'])$(id).disabled=!cue;
 if(cue){
  const row=$(`cue-${cue.id}`);row?.classList.add('active');
  if(row&&!video.paused&&!document.getSelection()?.toString()){
   const panel=$('transcript');const top=row.offsetTop-panel.offsetTop;
   if(top<panel.scrollTop||top+row.offsetHeight>panel.scrollTop+panel.clientHeight)panel.scrollTop=top-panel.clientHeight/3;
  }
 }
}
function currentIndex(){
 const t=video.currentTime-subtitleShift;
 // Subtitles can overlap. Prefer the most recently started visible cue.
 let lo=0,hi=cues.length-1,index=-1;
 while(lo<=hi){const mid=(lo+hi)>>1;if(cues[mid].start<=t){index=mid;lo=mid+1;}else hi=mid-1;}
 // Keep the last spoken line available during a pause, so it can be captured
 // or marked as shadowed after the audio finishes.
 return index;
}
function selection(){
 const s=document.getSelection();if(!s||s.isCollapsed)return null;
 const parent=n=>(n?.nodeType===1?n:n?.parentElement)?.closest('[data-cue]');
 const a=cueMap.get(Number(parent(s.anchorNode)?.dataset.cue));
 const b=cueMap.get(Number(parent(s.focusNode)?.dataset.cue));
 if(!a||!b)return null;
 const first=a.ordinal<=b.ordinal?a:b,last=a.ordinal<=b.ordinal?b:a;
 return {cue_id:first.id,end_cue_id:last.id,expression:a.id===b.id?s.toString().trim():''};
}
function captureSpan(index=active){
 const cue=cues[index];if(!cue)return null;
 const end=cues[Math.min(cues.length-1,index+Number($('spanLength').value))];
 return {cue_id:cue.id,end_cue_id:end.id};
}
async function save(kind,chosen){
 const range=chosen||selection()||captureSpan();if(!range)return;
 try{
  const c=await api('/api/study/captures',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,...range})});
  if(!captures.some(x=>x.id===c.id))captures.push(c);
  renderCaptures();toast('Saved with its context. Keep watching.');
 }catch(e){toast('Capture not saved: '+e.message);}
}
function renderCaptures(){
 $('captures').innerHTML=captures.filter(c=>!c.archived).slice().reverse().map(c=>`<article class="card"><div class="row"><span class="badge">${esc(c.kind)}</span><span class="small muted">${stamp(c.start)}</span><span class="spacer"></span><button data-archive="${c.id}" class="small">Remove</button></div><p class="capture-text" lang="ja">${esc(c.expression)}</p><button data-capture-play="${c.id}">Replay in context</button></article>`).join('')||'<p class="muted small">Nothing captured yet. Watching still counts.</p>';
}
async function lookup(cue,token){
 video.pause();word=token;wordCue=cue;
 $('wordTitle').textContent=token.surface;$('wordReading').textContent=token.reading;
 $('definitions').textContent='Looking up…';$('dictionary').showModal();
 try{
  const r=await api('/api/dict/lookup?'+new URLSearchParams({surface:token.surface,lemma:token.lemma,reading:token.lemma_reading}));
  if(word!==token)return;
  $('definitions').innerHTML=(r.candidates||[]).map(e=>`<div class="entry"><strong lang="ja">${esc(e.kanji_forms.join(' / '))} ${esc(e.kana_forms.join(' / '))}</strong><ul>${e.senses.map(s=>`<li>${esc(s.glosses.join('; '))}</li>`).join('')}</ul></div>`).join('')||'<p>No dictionary match. You can still capture the word and its context.</p>';
 }catch(e){$('definitions').textContent=e.message;}
}
function resetSampling(){lastSample={time:video.currentTime,wall:performance.now()};}
function flushSpan(){
 if(span&&span.seconds>=0.1){record(span);span=null;}
 lastFlush=performance.now();
}
function sample(){
 const wall=performance.now(),time=video.currentTime;
 if(lastSample&&!video.paused&&!video.seeking){
  const dt=(wall-lastSample.wall)/1000,delta=time-lastSample.time;
  // Do not count buffering, seeks, or a sleeping tab as watched time.
  if(dt>0&&dt<=3&&delta>0&&delta<=dt*4+.5){
   const seconds=Math.min(dt,delta/video.playbackRate);
   const action=replayEnd!==null?'replay':playbackAction;
   if(span&&(span.action!==action||Math.abs(span.end_position-lastSample.time)>.5))flushSpan();
   if(!span)span={source_id:sourceId,action,position:lastSample.time,end_position:time,seconds:0,cue_id:cues[active]?.id||null};
   span.end_position=time;span.seconds+=seconds;
  }else flushSpan();
 }
 lastSample={time,wall};
 if(wall-lastFlush>=10000)flushSpan();
}
async function playSpan(start,end){
 flushSpan();replayStart=Math.max(0,start+subtitleShift-.2);replayEnd=end+subtitleShift+.25;
 video.currentTime=replayStart;resetSampling();
 try{await video.play();}catch(e){toast('Press play to begin.');}
}
function replay(index=active){
 const c=cues[index];if(!c)return;
 const end=cues[Math.min(cues.length-1,index+Number($('spanLength').value))];
 playSpan(c.start,end.end);
}
function update(){
 if(!loaded)return;
 sample();
 if(replayEnd!==null&&video.currentTime>=replayEnd){
  flushSpan();
  if($('loop').checked){video.currentTime=replayStart;resetSampling();}
  else{video.pause();replayEnd=null;replayStart=null;}
 }
 showCue(currentIndex());
}
function rebuildTrack(){
 if(track)video.removeChild(track);
 const vtt='WEBVTT\n\n'+cues.map(c=>{
  const format=s=>{s=Math.max(0,s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${(s%60).toFixed(3).padStart(6,'0')}`;};
  return `${format(c.start+subtitleShift)} --> ${format(c.end+subtitleShift)}\n${c.text.replace(/-->/g,'→')}\n`;
 }).join('\n');
 track=document.createElement('track');track.kind='subtitles';track.label='日本語';track.srclang='ja';track.src=URL.createObjectURL(new Blob([vtt],{type:'text/vtt'}));
 video.append(track);track.track.mode='hidden';
}
function setup(data){
 source=data.source;cues=data.cues;captures=data.captures;
 for(const c of cues)cueMap.set(c.id,c);
 loaded=true;$('preparing').hidden=true;$('experience').hidden=false;
 try{subtitleShift=Number(localStorage.getItem('wakatta-subtitle-shift:'+sourceId))||0;}catch{}
 $('offset').value=subtitleShift;
 video.src=`/api/study/sources/${sourceId}/media`;
 $('audioTitle').textContent=source.collection+' · '+source.title;
 $('audioOnly').checked=source.kind!=='video';setAudioMode();
 $('sing').hidden=source.kind!=='song';
 if(source.kind==='song')$('savePhrase').textContent='Save lyric';
 $('subtitleOrigin').textContent=source.metadata.subtitle_origin||'User supplied transcript';
  transcript();renderCaptures();rebuildTrack();
 if(data.next_source_id){$('nextEpisode').href=`/watch/${data.next_source_id}`;$('nextEpisode').hidden=false;}
 $('chapters').innerHTML=(source.metadata.chapters||[]).map(c=>`<button class="small" data-chapter="${Number(c.start_time)}">${stamp(c.start_time)} ${esc(c.title)}</button>`).join('');
 video.addEventListener('loadedmetadata',()=>{
  const cue=cueMap.get(Number(params.get('cue')));
  const requested=Number(params.get('from'));
  const resume=cue?cue.start+subtitleShift:params.has('from')&&Number.isFinite(requested)?requested:data.progress?.position||0;
  video.currentTime=Math.min(video.duration-.1,Math.max(0,resume));
  resetSampling();showCue(currentIndex());
 },{once:true});
 currentCapture=captures.find(c=>c.id===Number(params.get('capture')));
 if(currentCapture){$('revisitPanel').hidden=false;$('revisitExpression').textContent=currentCapture.expression;}
 $('savePhrase').disabled=!cues.length;$('saveGrammar').disabled=!cues.length;
}
async function load(){
 try{
  const data=await api(`/api/study/sources/${sourceId}`);source=data.source;
  $('title').textContent=source.title;$('collection').textContent=source.collection;
  document.title=`${source.collection} · ${source.title} — Wakatta`;
  if(source.status==='ready'){setup(data);return;}
  $('preparing').hidden=false;
  const busy=['queued','copying','converting','indexing'].includes(source.status);
  $('preparing').innerHTML=`<h2>${busy?'Getting your episode ready…':'Make this episode ready to watch'}</h2><p class="muted">${busy?'You can leave this page and come back.':'A Japanese-audio viewing copy will be prepared from your media library.'}</p>${source.metadata.warning?`<p class="notice">${esc(source.metadata.warning)}</p>`:''}${source.error?`<p class="error">${esc(source.error)}</p>`:''}${!busy&&!source.metadata.warning?'<button id="prepare" class="primary">Prepare episode</button>':''}`;
  if(busy)setTimeout(load,3000);
  if($('prepare'))$('prepare').onclick=async()=>{try{await api(`/api/study/sources/${sourceId}/prepare`,{method:'POST'});load();}catch(e){$('error').textContent=e.message;}};
 }catch(e){$('error').textContent=e.message;}
}
function setAudioMode(){flushSpan();const audio=$('audioOnly').checked;playbackAction=audio?'listen':'watch';video.classList.toggle('audio-mode',audio);$('audioCover').hidden=!audio;}
document.addEventListener('click',e=>{
 const chapter=e.target.closest('[data-chapter]');if(chapter){flushSpan();replayEnd=null;video.currentTime=Number(chapter.dataset.chapter);video.play().catch(()=>toast('Press play to begin.'));return;}
 const t=e.target.closest('[data-token]');if(t){const cue=cueMap.get(Number(t.dataset.cue));lookup(cue,cue.tokens[Number(t.dataset.token)]);return;}
 const seek=e.target.closest('[data-seek]');if(seek){const c=cueMap.get(Number(seek.dataset.seek));replay(c.ordinal);}
 const saveButton=e.target.closest('[data-save]');if(saveButton){save(source.kind==='song'?'lyric':'phrase',{cue_id:Number(saveButton.dataset.save)});}
 const cap=e.target.closest('[data-capture-play]');if(cap){const c=captures.find(c=>c.id===Number(cap.dataset.capturePlay));playSpan(c.start,c.end);}
 const archive=e.target.closest('[data-archive]');if(archive){const id=Number(archive.dataset.archive);api(`/api/study/captures/${id}`,{method:'DELETE'}).then(()=>{captures=captures.filter(c=>c.id!==id);renderCaptures();toast('Removed from inbox');}).catch(e=>toast(e.message));}
});
document.addEventListener('mouseup',()=>{if(selection())video.pause();});
$('savePhrase').onclick=()=>save(source.kind==='song'?'lyric':'phrase');
$('saveGrammar').onclick=()=>save('grammar');
$('saveWord').onclick=()=>save('word',{cue_id:wordCue.id,expression:word.surface,lemma:word.lemma,reading:word.lemma_reading});
$('closeDictionary').onclick=()=>$('dictionary').close();
$('hearWord').onclick=()=>{$('dictionary').close();replay(wordCue.ordinal);};
$('replay').onclick=()=>replay();
$('previous').onclick=()=>replay(Math.max(0,(active<0?currentIndex():active)-1));
$('next').onclick=()=>replay(Math.min(cues.length-1,active+1));
$('reveal').onclick=()=>{const hidden=$('experience').classList.toggle('hide-subtitles');$('reveal').textContent=hidden?'Reveal Japanese':'Hide Japanese';$('reveal').setAttribute('aria-pressed',String(hidden));};
$('speed').onchange=()=>{flushSpan();video.playbackRate=Number($('speed').value);resetSampling();};
$('audioOnly').onchange=setAudioMode;
$('offset').onchange=()=>{subtitleShift=Math.max(-120,Math.min(120,Number($('offset').value)||0));try{localStorage.setItem('wakatta-subtitle-shift:'+sourceId,subtitleShift);}catch{}rebuildTrack();showCue(currentIndex());};
$('transcriptSearch').oninput=transcript;
for(const action of ['shadow','sing'])$(action).onclick=()=>{
 const range=captureSpan();if(!range)return;
 const c=cueMap.get(range.cue_id),end=cueMap.get(range.end_cue_id);
 record({source_id:sourceId,action,position:c.start,end_position:end.end,seconds:0,cue_id:c.id});
 $('practiceHistory').textContent=`${action==='shadow'?'Shadowed':'Sang'} ${stamp(c.start)}–${stamp(end.end)}. Saved to your practice history.`;
 toast('Practice marked.');
};
const revisitEventId=uuid();
$('completeRevisit').onclick=async()=>{
 const b=$('completeRevisit');b.disabled=true;
 try{const r=await api(`/api/study/captures/${currentCapture.id}/revisit`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:revisitEventId})});b.textContent='Revisited';toast('Next invitation: '+new Date(r.next_review_at).toLocaleDateString());}
 catch(e){b.disabled=false;toast(e.message);}
};
video.addEventListener('play',resetSampling);
video.addEventListener('pause',flushSpan);
video.addEventListener('seeking',()=>{flushSpan();resetSampling();});
video.addEventListener('seeked',resetSampling);
video.addEventListener('ended',flushSpan);
video.addEventListener('error',()=>{$('error').textContent='This media could not play. Try reloading or a supported MP4/audio file.';});
video.addEventListener('timeupdate',update);setInterval(update,500);
addEventListener('pagehide',flushSpan);
document.addEventListener('visibilitychange',()=>{flushSpan();resetSampling();});
document.addEventListener('fullscreenchange',()=>{if(track)track.track.mode=document.fullscreenElement===video?'showing':'hidden';});
document.addEventListener('keydown',e=>{
 if(e.defaultPrevented||e.metaKey||e.ctrlKey||e.altKey||e.isComposing||e.target.isContentEditable)return;
 if(/INPUT|TEXTAREA|SELECT|BUTTON/.test(e.target.tagName)||$('dictionary').open||!loaded)return;
 if(e.code==='Space'){e.preventDefault();video.paused?video.play().catch(()=>{}):video.pause();}
 if(e.key.toLowerCase()==='r'){e.preventDefault();replay();}
 if(e.key.toLowerCase()==='c'){e.preventDefault();save(source.kind==='song'?'lyric':'phrase');}
});
load();WakattaStudy.flush();
