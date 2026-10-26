r"""The Review page's notes for the consultant, added by the app (webapp.py) to the page it
serves, after the decisions hook - the review.html file itself is not changed, and opened
on its own (a package, an e-mail) it works as before.

On the page:
  * the element panel gets "Notes for the consultant": a comment, screenshots (a
    close-up round the element, or the view as on screen; the 3D view in 3D), the notes
    already made, the consultant's reply with its sizes offered as "use" buttons (a
    decision only when pressed);
  * the left column lists the notes (and "+ Note on an area" for a place no element
    covers) and, after a revised drawing is analysed, what the revision changed;
  * open notes are numbered pins on the plan.
Notes are kept by the app in the project (building/notes.py), never in the decisions.
"""

REVIEW_HOOK = r"""<style>
.nt{margin-top:14px;padding-top:10px;border-top:2px solid var(--line)}
.nt h4{margin:0 0 6px;font-size:13px}.nt textarea{width:100%;min-height:58px;font:inherit;padding:5px 6px;border:1px solid var(--line);border-radius:5px;resize:vertical}
.nt-n{border:1px solid var(--line);border-radius:7px;padding:7px 8px;margin-bottom:8px;background:#fcfbf8}
.nt-h{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:4px}.nt-h small{color:var(--mute)}
.nt-st{font-size:11px;padding:1px 7px;border-radius:8px;color:#fff;background:#7b5fb4}.nt-st.sent{background:#1f6fb2}
.nt-st.answered{background:#2e8b57}.nt-st.closed{background:#9aa3ab}
.nt-imgs{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}.nt-imgs span{position:relative;display:inline-block}
.nt-imgs img{width:96px;height:64px;object-fit:cover;border:1px solid var(--line);border-radius:4px;display:block;cursor:zoom-in}
.nt-imgs button{position:absolute;top:-6px;right:-6px;border-radius:50%;width:18px;height:18px;padding:0;line-height:15px;font-size:12px}
.nt-btns{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px}
.nt-reply{background:#eef7f1;border:1px solid #cfe6d7;border-radius:6px;padding:6px 8px;margin-top:6px}
.nt-reply div{white-space:pre-wrap;margin:2px 0 4px}.nt-rev{font-size:12px;margin:6px 0 0;padding:4px 6px;border-radius:5px;background:#f1efe9}
.nt-rev.changed{background:#e8f1fa}.nt-rev.not.found,.nt-rev.notfound{background:#fbeceb}
.nt-big{position:fixed;inset:0;background:rgba(22,32,43,.7);display:flex;align-items:center;justify-content:center;z-index:50;cursor:zoom-out}
.nt-big img{max-width:94vw;max-height:92vh;border-radius:6px;box-shadow:0 6px 30px rgba(0,0,0,.4)}
.nt-big .nt-bigbar{position:fixed;top:12px;right:16px;display:flex;gap:6px;cursor:default}
.sn{position:fixed;inset:0;background:rgba(22,32,43,.82);z-index:60;display:flex;flex-direction:column;align-items:center}
.sn-bar{display:flex;gap:5px;align-items:center;flex-wrap:wrap;justify-content:center;background:#fff;border-radius:9px;padding:6px 9px;margin:10px 10px 4px;
 box-shadow:0 4px 16px rgba(0,0,0,.25);font-size:12px;max-width:calc(100% - 20px)}
.sn-bar button{padding:3px 8px}.sn-bar button.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.sn-bar .sep{width:1px;height:20px;background:var(--line);margin:0 3px}
.sn-c{width:20px;height:20px;border-radius:50%;border:2px solid #fff;box-shadow:0 0 0 1px #9aa3ab;cursor:pointer;padding:0}.sn-c.on{box-shadow:0 0 0 2px var(--ink)}
.sn-hint{color:#fff;font-size:12px;margin:2px 0 6px;opacity:.85}
.sn-wrap{flex:1;min-height:0;display:flex;align-items:center;justify-content:center;width:100%;padding:0 12px 12px;box-sizing:border-box}
.sn canvas{max-width:100%;max-height:100%;background:#fff;box-shadow:0 6px 30px rgba(0,0,0,.4);cursor:crosshair;touch-action:none}
.ntpin{cursor:pointer}.nt-busy{opacity:.55;pointer-events:none}
#chgList .item small,#ntList .item small{color:var(--mute)}#ntList,#chgList{max-height:230px;overflow-y:auto}.nt-k{font-size:10px;padding:0 5px;border-radius:6px;color:#fff;margin-right:4px}
</style>
<script>
(function(){
 if(window.parent===window||typeof D==='undefined')return;
 const N={list:[],chg:null,pend:[],pendFor:null,pendBy:{},draft:{},pin:false,flash:null};
 const CSS=getComputedStyle(document.documentElement);
 const LB={open:'open',sent:'sent',answered:'answered',closed:'closed'};
 const KC={changed:'#1f6fb2',added:'#2e8b57',removed:'#c0392b',moved:'#d98a1c'};
 function call(body){return fetch('/api/notes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
  .then(r=>r.json().then(j=>{if(!r.ok)throw new Error(j.error||'Not saved');return j}))}
 function fail(e){alert('The note was not saved: '+(e&&e.message||e)+'\nCheck the app is still open, then try again.')}
 function load(){return fetch('/api/notes').then(r=>r.json()).then(j=>{N.list=j.notes||[];N.chg=j.changes||null;side();draw();
  if(sel)pick(sel)}).catch(()=>{})}
 // ------------------------------------------------------------ what a note records about its element
 function val(e,p){const d=decided(e,p);return d!==undefined?d:(e.p[p]?e.p[p].v:null)}
 function sizeOf(e){if(e.t==='slab'){const t=val(e,'thickness');return t!=null?t+' thk':''}
  const w=val(e,'width'),d=val(e,'depth');return (w==null&&d==null)?'':`${w??'?'} x ${d??'?'}`}
 function issueOf(e){const out=[];for(const p in e.p){const x=e.p[p];if(decided(e,p)!==undefined)continue;
  if(x.s==='missing')out.push(p+': not given in the drawings');
  else if(x.s==='conflict'){const v=[...new Set(x.src.map(s=>(s.k||'').split(': ').slice(1).join(': ').replace(/^(\d+)\.0$/,'$1')).filter(Boolean))];
   out.push(p+': drawings disagree'+(v.length?' ('+v.slice(0,4).join(' / ')+')':''))}
  else if(x.s==='low')out.push(p+': read with low confidence')}return out.join('; ')}
 function centre(e){const p=e.g.p;let x=0,y=0;p.forEach(q=>{x+=q[0];y+=q[1]});return [Math.round(x/p.length),Math.round(y/p.length)]}
 const sheetOf=id=>D.sheets.find(s=>s.id===id);
 function info(e){const s=sheetOf(e.sheet)||cur;return {el:e.id,sheet:e.sheet,plan:s?(s.title||s.file):'',lv:e.lv||(s&&s.lv)||'',
  type:e.t,mark:e.m||'',size:sizeOf(e),issue:issueOf(e),status:status(e),at:centre(e)}}
 // ------------------------------------------------------------ screenshots
 function dpt(p){const q=P(p);return [q[0],-q[1]]}
 function img(src){return new Promise((ok,no)=>{const i=new Image();i.onload=()=>ok(i);i.onerror=()=>no(new Error('the picture could not be made'));i.src=src})}
 function frame(pic,w,h,t1,t2,key){const c=document.createElement('canvas'),hd=58;c.width=w;c.height=h+hd;const g=c.getContext('2d');
  g.fillStyle='#fff';g.fillRect(0,0,c.width,c.height);g.drawImage(pic,0,hd,w,h);if(key)key(g,w,hd,h);
  g.fillStyle='#16202b';g.font='bold 24px Segoe UI, Arial, sans-serif';g.fillText(t1,16,28);
  g.fillStyle='#5f6c77';g.font='16px Segoe UI, Arial, sans-serif';g.fillText(t2,16,50);
  g.strokeStyle='#dcd9d0';g.beginPath();g.moveTo(0,hd-0.5);g.lineTo(w,hd-0.5);g.stroke();return c.toDataURL('image/jpeg',0.88)}
 // the element's own plan in the subtitle (3D: not the plan last shown)
 function titles(e,at,area){const s=(e&&sheetOf(e.sheet))||cur||{};const t2=[s.lv,s.title||s.file].filter(Boolean).join(' - ');
  if(!e)return [(area&&area.title)||'Area of the plan',t2];return [[e.m||'(no mark)',e.t.replace('_',' '),sizeOf(e)].filter(Boolean).join('  ·  '),t2]}
 // 3D: the element framed and named in the picture, as on the plan - its blue glow alone hardly shows
 // in a screenshot. Drawn on a copy of the picture; the 3D view itself is not touched.
 function mark3(c,e){const ms=(typeof meshes!=='undefined'?meshes:[]).filter(m=>m.userData&&m.userData.id===e.id&&m.visible);
  if(!ms.length)return c;const b=new THREE.Box3();ms.forEach(m=>b.expandByObject(m));if(b.isEmpty())return c;
  const W=c.width,H=c.height,xs=[],ys=[];
  for(const x of [b.min.x,b.max.x])for(const y of [b.min.y,b.max.y])for(const z of [b.min.z,b.max.z]){
   const v=new THREE.Vector3(x,y,z).project(cam);if(v.z>1||v.z<-1)return c;xs.push((v.x+1)/2*W);ys.push((1-v.y)/2*H)}
  const k=W/Math.max(c.clientWidth||W,1),pad=12*k;
  const x0=Math.max(Math.min(...xs)-pad,2*k),y0=Math.max(Math.min(...ys)-pad,2*k),x1=Math.min(Math.max(...xs)+pad,W-2*k),y1=Math.min(Math.max(...ys)+pad,H-2*k);
  if(x1-x0<4||y1-y0<4)return c;                    // not in the picture
  const o=document.createElement('canvas');o.width=W;o.height=H;const g=o.getContext('2d');g.drawImage(c,0,0);
  g.strokeStyle='#e0218a';g.lineWidth=3*k;g.setLineDash([10*k,6*k]);g.strokeRect(x0,y0,x1-x0,y1-y0);g.setLineDash([]);
  const lab=[e.m||'(no mark)',sizeOf(e)].filter(Boolean).join('  '),fs=Math.round(15*k);g.font=`bold ${fs}px Arial, sans-serif`;
  const lw=g.measureText(lab).width+fs*0.9,lh=fs*1.5,lx=Math.max(2*k,Math.min(x1+4*k,W-lw-2*k)),ly=Math.max(y0-lh-4*k,2*k);
  g.fillStyle='#e0218a';g.fillRect(lx,ly,lw,lh);g.fillStyle='#fff';g.fillText(lab,lx+fs*0.45,ly+fs*1.08);return o}
 async function shot(e,at,asShown,area){
  if(mode==='3d'&&r3){if(window.c2rShot)window.c2rShot(true);     // tools (box handles, gumball) left out
   try{r3.render(s3,cam);const c=r3.domElement,k=Math.min(1600/c.width,1100/c.height,1),w=Math.round(c.width*k),h=Math.round(c.height*k);
    const t=titles(e,at,area),sub=area&&area.note?[area.note.lv,area.note.plan].filter(Boolean).join(' - '):t[1];
    return frame(e?mark3(c,e):c,w,h,t[0],sub+'  (3D view)')}finally{if(window.c2rShot)window.c2rShot(false)}}
  if(!cur)throw new Error('no plan is shown');
  const keep=vb.slice(),W=1500,H=1000;let pts=null;
  if(e&&e.sheet===cur.id)pts=e.g.p.map(dpt);else if(at)pts=[dpt(at)];
  if(pts&&!asShown){const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]),cx=(Math.min(...xs)+Math.max(...xs))/2,cy=(Math.min(...ys)+Math.max(...ys))/2;
   const w=Math.max(9000,(Math.max(...xs)-Math.min(...xs))*1.7,(Math.max(...ys)-Math.min(...ys))*1.7*W/H);vb=[cx-w/2,cy-w*H/W/2,w,w*H/W]}
  else{const w=Math.max(vb[2],vb[3]*W/H);vb=[vb[0]+vb[2]/2-w/2,vb[1]+vb[3]/2-w*H/W/2,w,w*H/W]}
  const v=vb.slice();draw();
  const c=$('svg').cloneNode(true);c.setAttribute('xmlns','http://www.w3.org/2000/svg');c.setAttribute('width',W);c.setAttribute('height',H);
  c.setAttribute('viewBox',v.join(' '));c.removeAttribute('style');let ring='';
  if(pts){const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]),pad=Math.max(500,v[2]/40);
   const rx=Math.min(...xs)-pad,ry=Math.min(...ys)-pad,rw=Math.max(...xs)-Math.min(...xs)+2*pad,rh=Math.max(...ys)-Math.min(...ys)+2*pad;
   ring=`<rect x="${rx}" y="${ry}" width="${rw}" height="${rh}" fill="none" stroke="#e0218a" stroke-width="${v[2]/280}" stroke-dasharray="${v[2]/90} ${v[2]/160}"/>`;
   // the queried element named beside its frame, in case the drawing's own label is further off
   const lab=e?[e.m||'(no mark)',sizeOf(e)].filter(Boolean).join('  '):'here',fs=v[2]/62,lw=lab.length*fs*0.62+fs*0.9;
   const lx=Math.min(rx+rw+fs*0.3,v[0]+v[2]-lw-fs*0.3),ly=Math.max(ry-fs*1.7,v[1]+fs*0.3);
   ring+=`<rect x="${lx}" y="${ly}" width="${lw}" height="${fs*1.45}" rx="${fs*0.25}" fill="#e0218a"/>
    <text x="${lx+fs*0.45}" y="${ly+fs*1.05}" font-size="${fs}" font-family="Arial,sans-serif" font-weight="700" fill="#fff">${esc(lab)}</text>`}
  c.innerHTML=`<rect x="${v[0]}" y="${v[1]}" width="${v[2]}" height="${v[3]}" fill="#fbfaf7"/>`+c.innerHTML+ring;
  let s=new XMLSerializer().serializeToString(c).replace(/var\(--([\w-]+)\)/g,(m,k)=>CSS.getPropertyValue('--'+k).trim()||'#888')
   .replace(/stroke="#8d969e" stroke-opacity=".55" stroke-width="0.6"/g,'stroke="#5f6870" stroke-opacity=".75" stroke-width="1"');
  vb=keep;draw();
  const u=URL.createObjectURL(new Blob([s],{type:'image/svg+xml;charset=utf-8'}));
  try{const p=await img(u);const t=titles(e,at);return frame(p,W,H,t[0],t[1],keyPlan(v))}finally{URL.revokeObjectURL(u)}}
 // the whole floor in miniature in the screenshot's corner, the close-up marked on it
 function keyPlan(v){const whole=bounds(cur);if(v[2]*v[3]>0.6*whole[2]*whole[3])return null;
  return (g,W,top,H)=>{const kw=300,k=Math.min(kw/whole[2],220/whole[3]),bw=whole[2]*k,bh=whole[3]*k,x0=W-bw-24,y0=top+H-bh-20;
   g.fillStyle='#fff';g.fillRect(x0-8,y0-24,bw+16,bh+32);g.strokeStyle='#9aa3ab';g.lineWidth=1;g.strokeRect(x0-8,y0-24,bw+16,bh+32);
   g.fillStyle='#5f6c77';g.font='13px Segoe UI, Arial, sans-serif';g.fillText('Key plan',x0,y0-8);
   const X=x=>x0+(x-whole[0])*k,Y=y=>y0+(y-whole[1])*k;g.strokeStyle='#6d8f7c';g.lineWidth=1.2;
   for(const el of cur.els){if(el.t==='slab'||el.t==='raft')continue;const p=el.g.p.map(dpt);if(!p.length)continue;g.beginPath();g.moveTo(X(p[0][0]),Y(p[0][1]));
    for(const q of p.slice(1))g.lineTo(X(q[0]),Y(q[1]));if(el.g.k==='poly')g.closePath();g.stroke()}
   g.strokeStyle='#e0218a';g.lineWidth=2.5;const rx=X(v[0]),ry=Y(v[1]),rw=Math.max(v[2]*k,6),rh=Math.max(v[3]*k,6);g.strokeRect(rx,ry,rw,rh)}}
 // o.edit(r): the picture edited, r = {url, how: 'replace' | 'new'}
 function big(src,o){o=o||{};const d=document.createElement('div');d.className='nt-big';
  d.innerHTML=`<img src="${src}"><div class="nt-bigbar">${o.edit?'<button data-b="edit" class="primary" title="Crop it, mark it up - then replace it or keep both">Edit</button>':''}<button data-b="save" title="Save this picture on this PC (Downloads)">Save to PC</button><button data-b="close">Close</button></div>`;
  d.onclick=async ev=>{const b=ev.target.closest('[data-b]');if(b&&b.dataset.b==='save'){ev.stopPropagation();savePic(src,picName());return}
   d.remove();if(b&&b.dataset.b==='edit'){const r=await snip(src,{edit:true,name:picName()}).catch(()=>null);if(r)o.edit(r)}};document.body.appendChild(d)}
 // ------------------------------------------------------------ the snipping tool: crop, mark up, save
 function picName(what){const s=new Date(),p=n=>String(n).padStart(2,'0');
  const parts=[mode==='3d'?'3D':(cur&&cur.lv)||'',what||'',`${s.getFullYear()}-${p(s.getMonth()+1)}-${p(s.getDate())} ${p(s.getHours())}${p(s.getMinutes())}${p(s.getSeconds())}`];
  return parts.filter(Boolean).join(' ').replace(/[\\/:*?"<>|]+/g,'-').slice(0,90)}
 function savePic(src,name){fetch(src).then(r=>r.blob()).then(b=>{const u=URL.createObjectURL(b),a=document.createElement('a');
  a.href=u;a.download=name+(b.type==='image/png'?'.png':'.jpg');document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),4000)})
  .catch(()=>alert('The picture could not be saved.'))}
 // opens the picture in the editor; resolves with the edited picture (Add to note) or null
 function snip(src,o){o=o||{};return img(src).then(im=>new Promise(done=>{
  const S={tool:'crop',col:'#e0218a',ops:[],crop:null,hist:[],drag:null,size:3},W=im.width,H=im.height,lw=Math.max(3,W/400),fs=Math.max(18,Math.round(W/55));
  try{const v=+localStorage.getItem('c2r_snip_size');if(v>=1&&v<=10)S.size=v}catch(x){}
  // Size 1-10 (3 = as before): lines in proportion, text less steeply
  const curW=()=>Math.max(2,lw*S.size/3),curF=()=>Math.round(fs*(S.size+3)/6);
  const COL=['#e0218a','#d62828','#f4b400','#1f6fb2','#16202b'];
  const T={crop:['Crop','Drag over the part to keep'],pen:['Pen','Draw freehand'],arrow:['Arrow','Drag from the tail to the point'],
   box:['Box','Drag a rectangle round something'],text:['Text','Click where the text goes']};
  const d=document.createElement('div');d.className='sn';
  d.innerHTML=`<div class="sn-bar">${Object.entries(T).map(([k,v])=>`<button data-k="${k}" title="${v[1]}">${v[0]}</button>`).join('')}
   <span class="sep"></span>${COL.map(c=>`<button class="sn-c" data-c="${c}" style="background:${c}" title="Colour"></button>`).join('')}
   <span class="sep"></span><label title="Thickness of the pen, arrow and box, and size of the text - for what is drawn next">Size
   <input type="range" data-s min="1" max="10" step="1" style="width:80px;vertical-align:middle"></label><span class="sn-dot" title="Size and colour"></span>
   <span class="sep"></span><button data-a="undo" title="Undo (Ctrl+Z)">Undo</button><button data-a="reset" title="Back to the picture as taken">Reset</button>
   <span class="sep"></span>${navigator.clipboard&&window.ClipboardItem&&window.isSecureContext?'<button data-a="copy" title="Copy to paste into an e-mail, WhatsApp, Word ...">Copy</button>':''}
   <button data-a="save" title="Save this picture on this PC (Downloads)">Save to PC</button>
   ${o.add?'<button data-a="add" class="primary" title="Add this picture to the note (Enter)">Add to note</button>':''}
   ${o.edit?'<button data-a="replace" class="primary" title="Put the edited picture in place of the old one (Enter)">Replace</button><button data-a="asnew" title="Keep the old picture and add the edited one to the note">Save as new</button>':''}
   <button data-a="cancel" title="Close without keeping (Esc)">${o.add||o.edit?'Cancel':'Close'}</button></div>
   <div class="sn-hint"></div><div class="sn-wrap"><canvas></canvas></div>`;
  document.body.appendChild(d);const cv=d.querySelector('canvas'),g=cv.getContext('2d');cv.width=W;cv.height=H;
  const sz=d.querySelector('[data-s]');sz.value=S.size;
  sz.oninput=()=>{S.size=+sz.value;try{localStorage.setItem('c2r_snip_size',S.size)}catch(x){}paint()};
  function drawOp(g,p,dx,dy){const w=p.w||lw,f=p.f||fs;g.strokeStyle=g.fillStyle=p.c;g.lineWidth=w;g.lineCap=g.lineJoin='round';
   if(p.t==='pen'){g.beginPath();p.pts.forEach((q,i)=>i?g.lineTo(q[0]-dx,q[1]-dy):g.moveTo(q[0]-dx,q[1]-dy));g.stroke()}
   else if(p.t==='box'){g.strokeRect(Math.min(p.a[0],p.b[0])-dx,Math.min(p.a[1],p.b[1])-dy,Math.abs(p.b[0]-p.a[0]),Math.abs(p.b[1]-p.a[1]))}
   else if(p.t==='arrow'){const [ax,ay]=[p.a[0]-dx,p.a[1]-dy],[bx,by]=[p.b[0]-dx,p.b[1]-dy],an=Math.atan2(by-ay,bx-ax),hl=Math.max(w*5,12);
    g.beginPath();g.moveTo(ax,ay);g.lineTo(bx,by);g.stroke();g.beginPath();g.moveTo(bx,by);
    g.lineTo(bx-hl*Math.cos(an-0.45),by-hl*Math.sin(an-0.45));g.lineTo(bx-hl*Math.cos(an+0.45),by-hl*Math.sin(an+0.45));g.closePath();g.fill()}
   else if(p.t==='text'){g.font=`bold ${f}px Arial, sans-serif`;g.textBaseline='top';g.strokeStyle='#fff';g.lineWidth=f/5;
    p.s.split('\n').forEach((ln,i)=>{g.strokeText(ln,p.at[0]-dx,p.at[1]-dy+i*f*1.2);g.fillText(ln,p.at[0]-dx,p.at[1]-dy+i*f*1.2)})}}
  function paint(){g.clearRect(0,0,W,H);g.drawImage(im,0,0);S.ops.forEach(p=>drawOp(g,p,0,0));if(S.drag&&S.drag.op)drawOp(g,S.drag.op,0,0);
   const c=S.drag&&S.drag.crop||S.crop;
   if(c){g.fillStyle='rgba(22,32,43,.45)';g.fillRect(0,0,W,c.y);g.fillRect(0,c.y+c.h,W,H-c.y-c.h);g.fillRect(0,c.y,c.x,c.h);g.fillRect(c.x+c.w,c.y,W-c.x-c.w,c.h);
    g.strokeStyle='#fff';g.lineWidth=Math.max(1.5,W/900);g.setLineDash([8,5]);g.strokeRect(c.x,c.y,c.w,c.h);g.setLineDash([])}
   d.querySelectorAll('[data-k]').forEach(b=>b.classList.toggle('on',b.dataset.k===S.tool));
   d.querySelectorAll('[data-c]').forEach(b=>b.classList.toggle('on',b.dataset.c===S.col));
   const dot=d.querySelector('.sn-dot'),r=cv.getBoundingClientRect(),px=Math.min(22,Math.max(3,curW()*(r.width?r.width/W:1)));
   dot.style.cssText=`display:inline-block;width:${px}px;height:${px}px;border-radius:50%;background:${S.col};margin:0 4px`;
   d.querySelector('.sn-hint').textContent=T[S.tool][1]+(S.crop?' · the dimmed part is left out':'')+' · Esc '+(o.add||o.edit?'cancels':'closes')}
  function result(type){const c=S.crop||{x:0,y:0,w:W,h:H},out=document.createElement('canvas');out.width=Math.round(c.w);out.height=Math.round(c.h);
   const q=out.getContext('2d');q.fillStyle='#fff';q.fillRect(0,0,out.width,out.height);q.drawImage(im,-c.x,-c.y);S.ops.forEach(p=>drawOp(q,p,c.x,c.y));
   return type==='png'?out.toDataURL('image/png'):out.toDataURL('image/jpeg',0.9)}
  const at=ev=>{const r=cv.getBoundingClientRect();return [Math.min(Math.max((ev.clientX-r.left)*W/r.width,0),W),Math.min(Math.max((ev.clientY-r.top)*H/r.height,0),H)]};
  cv.addEventListener('pointerdown',ev=>{if(ev.button!==0)return;const p=at(ev);cv.setPointerCapture(ev.pointerId);
   if(S.tool==='text'){const s=prompt('Text to write on the picture:');if(s&&s.trim()){S.ops.push({t:'text',c:S.col,f:curF(),s:s.trim(),at:p});S.hist.push('op')}paint();return}
   S.drag={a:p,op:S.tool==='pen'?{t:'pen',c:S.col,w:curW(),pts:[p]}:S.tool==='crop'?null:{t:S.tool,c:S.col,w:curW(),a:p,b:p}};paint()});
  cv.addEventListener('pointermove',ev=>{const r=S.drag;if(!r)return;const p=at(ev);
   if(S.tool==='crop')r.crop={x:Math.min(r.a[0],p[0]),y:Math.min(r.a[1],p[1]),w:Math.abs(p[0]-r.a[0]),h:Math.abs(p[1]-r.a[1])};
   else if(r.op.t==='pen')r.op.pts.push(p);else r.op.b=p;paint()});
  cv.addEventListener('pointerup',()=>{const r=S.drag;S.drag=null;if(!r){paint();return}
   if(S.tool==='crop'){if(r.crop&&r.crop.w>8&&r.crop.h>8){S.hist.push({crop:S.crop});S.crop=r.crop}}
   else if(r.op.t==='pen'?r.op.pts.length>1:Math.hypot(r.op.b[0]-r.op.a[0],r.op.b[1]-r.op.a[1])>4){S.ops.push(r.op);S.hist.push('op')}paint()});
  function undo(){const h=S.hist.pop();if(h===undefined)return;if(h==='op')S.ops.pop();else S.crop=h.crop;paint()}
  function close(v){removeEventListener('keydown',key,true);d.remove();done(v)}
  function key(ev){ev.stopImmediatePropagation();               // the views' own keys (arrows, Esc) wait while the editor is open
   if(ev.key==='Escape')close(null);else if(ev.key==='Enter'&&o.add)close(result());
   else if(ev.key==='Enter'&&o.edit)close({url:result(),how:'replace'});
   else if((ev.ctrlKey||ev.metaKey)&&ev.key.toLowerCase()==='z'){ev.preventDefault();undo()}}
  addEventListener('keydown',key,true);
  d.querySelector('.sn-bar').addEventListener('click',ev=>{const b=ev.target.closest('button');if(!b)return;
   if(b.dataset.k)S.tool=b.dataset.k;else if(b.dataset.c)S.col=b.dataset.c;
   else if(b.dataset.a==='undo')undo();else if(b.dataset.a==='reset'){S.ops=[];S.crop=null;S.hist=[]}
   else if(b.dataset.a==='save')savePic(result('png'),o.name||picName());
   else if(b.dataset.a==='copy'){fetch(result('png')).then(r=>r.blob()).then(bl=>navigator.clipboard.write([new ClipboardItem({'image/png':bl})]))
    .then(()=>{b.textContent='Copied';setTimeout(()=>b.textContent='Copy',1500)}).catch(()=>alert('Could not copy - use Save to PC.'))}
   else if(b.dataset.a==='add'){close(result());return}else if(b.dataset.a==='cancel'){close(null);return}
   else if(b.dataset.a==='replace'||b.dataset.a==='asnew'){close({url:result(),how:b.dataset.a==='replace'?'replace':'new'});return}paint()});
  paint()}))}
 function shotName(e,area){return picName(e?[e.m||e.t].join(' '):(area&&area.title)||'')}
 // ------------------------------------------------------------ the panel
 function noteHTML(n,e){const used=(n.used||[]).map(u=>`${esc(u.prop)} ${esc(u.value)}`).join(', ');
  const vals=(n.reply&&n.reply.values)||[];
  return `<div class="nt-n" data-id="${esc(n.id)}"><div class="nt-h"><b>Query ${n.no}</b><span class="nt-st ${esc(n.state)}">${LB[n.state]||esc(n.state)}</span>
  <small>${esc(n.by||'')} ${esc(n.date||'')}</small></div>
  <textarea data-edit>${esc(n.text||'')}</textarea><div class="nt-btns" data-editbar style="display:none"><button class="primary" data-act="edit">Save changes</button></div>
  <div class="nt-imgs">${(n.images||[]).map(i=>`<span><img src="/notes/img/${encodeURIComponent(i)}" data-big="/notes/img/${encodeURIComponent(i)}" data-img="${esc(i)}" alt="screenshot" title="Click to see it large, edit it or save it"><button data-drop="${esc(i)}" title="Remove this screenshot">&times;</button></span>`).join('')}</div>
  ${n.revision?`<p class="nt-rev ${esc(n.revision.result.replace(' ',''))}">${esc(n.revision.text)}</p>`:''}
  ${n.reply?`<div class="nt-reply"><small>Consultant's reply &middot; ${esc(n.reply.date||'')}${n.reply.file?' &middot; '+esc(n.reply.file):''}</small><div>${esc(n.reply.text)}</div>
   ${e&&vals.length?vals.map(x=>`<button data-use="${esc(x.prop)}" data-v="${esc(x.value)}">Use ${esc(x.prop)} ${esc(x.value)}</button>`).join(' ')+(vals.length>1?' <button data-useall="1">Use all</button>':''):''}
   ${used?`<div class="small">Used: ${used}</div>`:''}</div>`:''}
  <div class="nt-btns">${n.view&&window.c2rGoView?'<button data-act="goview" title="The 3D view as it was when the note was made">Go to view</button>':''}<button data-act="shot">+ Screenshot</button>${n.state==='closed'?'<button data-act="reopen">Re-open</button>':'<button data-act="close" title="Settled: left out of the next report">Close query</button>'}
  <button data-act="delete">Delete</button></div></div>`}
 function newHTML(area){const k=N.pendKey;return `<div class="nt-new"><textarea id="ntText" placeholder="${area?'What should the consultant check here?':'What should the consultant check? e.g. width 230 on the plan, 300 in the schedule - please confirm.'}">${esc(N.draft[k]||'')}</textarea>
  <div class="nt-imgs" id="ntPend">${N.pend.map((u,i)=>`<span><img src="${u}" data-big="${u}" data-pi="${i}" alt="screenshot" title="Click to see it large, edit it or save it"><button data-unpend="${i}" title="Remove">&times;</button></span>`).join('')}</div>
  ${N.pend.length?`<div class="small" style="color:#a36400">${N.pend.length} screenshot${N.pend.length>1?'s':''} not saved yet - press "Save note" to keep ${N.pend.length>1?'them':'it'} with the note</div>`:''}
  <div class="nt-btns"><button id="ntShot">Take screenshot</button>${mode==='3d'?'':'<label class="small"><input type="checkbox" id="ntView"> as on screen</label>'}
  <button class="primary" id="ntSave" style="margin-left:auto">Save note</button></div></div>`}
 function panel(e,area){const old=$('c2rnotes');if(old)old.remove();
  const key=e?e.id:(area.key||'area:'+(area.id||area.at.join(',')));if(N.pendKey!==key){
   // screenshots not saved yet stay with their element until "Save note" (or removed), as the typed text does
   if(N.pendKey&&N.pend.length)N.pendBy[N.pendKey]=N.pend;N.pend=N.pendBy[key]||[];delete N.pendBy[key];N.pendKey=key}
  const mine=e?N.list.filter(n=>n.el===e.id):(area.id?N.list.filter(n=>n.id===area.id):area.match?N.list.filter(area.match):[]);
  const box=document.createElement('div');box.id='c2rnotes';box.className='nt';
  box.innerHTML=`<h4>Notes for the consultant</h4>${mine.map(n=>noteHTML(n,e)).join('')}${area&&area.id?'':newHTML(!e)}`;
  $('detail').appendChild(box);wire(box,e,area)}
 function wire(box,e,area){
  box.querySelectorAll('[data-big]').forEach(i=>i.onclick=()=>big(i.dataset.big));
  // a screenshot not saved yet: edited in place, or kept beside the edited one
  box.querySelectorAll('[data-pi]').forEach(i=>i.onclick=()=>{const k=+i.dataset.pi;
   big(i.dataset.big,{edit:r=>{if(r.how==='replace')N.pend[k]=r.url;else N.pend.push(r.url);redo(e,area)}})});
  const tx=$('ntText');if(tx)tx.oninput=()=>{N.draft[N.pendKey]=tx.value};
  box.querySelectorAll('[data-unpend]').forEach(b=>b.onclick=()=>{N.pend.splice(+b.dataset.unpend,1);redo(e,area)});
  const sb=$('ntShot');if(sb)sb.onclick=async()=>{box.classList.add('nt-busy');
   let u=null;try{u=await shot(e,area&&area.at,$('ntView')&&$('ntView').checked,area)}catch(x){alert('No screenshot: '+x.message)}
   box.classList.remove('nt-busy');if(!u)return;
   const v=await snip(u,{add:true,name:shotName(e,area)}).catch(()=>u);if(v){N.pend.push(v);redo(e,area)}};
  const sv=$('ntSave');if(sv)sv.onclick=async()=>{const text=($('ntText').value||'').trim();
   if(!text&&!N.pend.length){alert('Type a comment or take a screenshot first.');return}
   const note=e?info(e):(area.note||{sheet:area.sheet,plan:(sheetOf(area.sheet)||{}).title||'',lv:(sheetOf(area.sheet)||{}).lv||'',type:'area',at:area.at});
   // made in 3D: the view kept with it, to go back to (building/arch_ui.py)
   const v=mode==='3d'&&window.c2rView?window.c2rView():null;
   box.classList.add('nt-busy');
   try{const n=await call({action:'add',note:{...note,...(v?{view:v,set:v.set}:{}),text},images:N.pend});N.list.push(n);N.pend=[];delete N.draft[N.pendKey];
    side();draw();if(e)pick(e);else if(area.after)area.after(n);else areaPanel({...n})}catch(x){box.classList.remove('nt-busy');fail(x)}};
  box.querySelectorAll('.nt-n').forEach(d=>{const id=d.dataset.id,n=N.list.find(x=>x.id===id);if(!n)return;
   const ta=d.querySelector('[data-edit]'),bar=d.querySelector('[data-editbar]');ta.oninput=()=>{bar.style.display=ta.value.trim()!==(n.text||'')?'':'none'};
   const act=async(body,keep)=>{d.classList.add('nt-busy');try{const r=await call({id,...body});
     if(body.action==='delete')N.list=N.list.filter(x=>x.id!==id);else Object.assign(n,r);side();draw();
     if(e)pick(e);else if(area&&area.after)area.after(n);else if(body.action==='delete')$('c2rnotes').remove();else areaPanel(n)}catch(x){d.classList.remove('nt-busy');fail(x)}};
   d.querySelectorAll('[data-act]').forEach(b=>b.onclick=async()=>{const a=b.dataset.act;
    if(a==='edit')act({action:'edit',text:ta.value});
    else if(a==='close')act({action:'state',state:'closed'});
    else if(a==='reopen')act({action:'state',state:n.reply?'answered':'open'});
    else if(a==='delete'){if(confirm('Delete query '+n.no+' and its screenshots?'))act({action:'delete'})}
    else if(a==='goview'){if(window.c2rGoView)window.c2rGoView(n.view)}
    else if(a==='shot'){d.classList.add('nt-busy');let u=null;try{u=await shot(e,n.at,false,area)}catch(x){alert('No screenshot: '+x.message)}
     d.classList.remove('nt-busy');if(u)u=await snip(u,{add:true,name:shotName(e,area)}).catch(()=>u);if(u)act({action:'screenshot',image:u})}});
   d.querySelectorAll('[data-drop]').forEach(b=>b.onclick=()=>{if(confirm('Remove this screenshot?'))act({action:'drop_image',image:b.dataset.drop})});
   // a saved screenshot edited: in its place (Replace) or added beside it (Save as new)
   d.querySelectorAll('[data-img]').forEach(i=>i.onclick=()=>big(i.dataset.big,{edit:r=>act(r.how==='replace'?
    {action:'replace_image',image:i.dataset.img,new:r.url}:{action:'screenshot',image:r.url})}));
   const use=list=>{if(!e)return;for(const x of list){decide(e,x.prop,String(x.value));
     call({id,action:'used',prop:x.prop,value:x.value}).then(r=>{Object.assign(n,r);if(sel&&sel.id===e.id)pick(e)}).catch(fail)}};
   d.querySelectorAll('[data-use]').forEach(b=>b.onclick=()=>use([{prop:b.dataset.use,value:b.dataset.v}]));
   d.querySelectorAll('[data-useall]').forEach(b=>b.onclick=()=>use(n.reply.values))})}
 function redo(e,area){if(e)pick(e);else areaPanel(area)}
 function areaPanel(a){sel=null;draw();const s=sheetOf(a.sheet)||cur;
  $('detail').innerHTML=`<h3 style="margin:0 0 4px">${a.id?'Query '+a.no+' - ':''}Area of the plan</h3><div class="small">${esc((s&&s.lv)||'')} ${esc((s&&(s.title||s.file))||'')}</div>
   <p class="small">A note on a place no element covers. The pin shows where.</p>`;panel(null,a)}
 const pick1=pick;pick=function(e){pick1(e);if(e)panel(e,null)};
 // ------------------------------------------------------------ pins and the area note
 // the drawing's own text (marks, sizes, grid names) where the drawing puts it - display only
 const TX={};
 const txl=document.createElement('label');txl.title="the drawing's own text - marks, sizes, grid names - over the plan";
 txl.innerHTML='<input type="checkbox" id="cadtx" checked> CAD text';$('cadon').parentElement.after(txl);
 $('cadtx').onchange=()=>draw();
 function txLoad(s){if(!s||s.id in TX)return;TX[s.id]=null;
  fetch('/api/cadtext?sheet='+encodeURIComponent(s.id)).then(r=>r.json()).then(j=>{TX[s.id]=j;if(cur&&cur.id===s.id)draw()}).catch(()=>{TX[s.id]={t:[]}})}
 const ANC=['start','middle','end'],BASE=['auto','auto','central','hanging'];
 function txSvg(){const j=TX[cur.id];if(!j||!j.t||!j.t.length||!$('cadtx').checked)return '';let h='<g pointer-events="none" font-family="Arial,sans-serif" fill="#2b3640" stroke="#fbfaf7" stroke-linejoin="round" paint-order="stroke">';
  for(const [x,y,ht,rot,ha,va,s] of j.t){const q=dpt([x,y]),fs=ht*1.35;
   h+=`<text x="${q[0]|0}" y="${q[1]|0}" font-size="${fs.toFixed(0)}" stroke-width="${(fs*0.16).toFixed(0)}" text-anchor="${ANC[ha]||'start'}" dominant-baseline="${BASE[va]||'auto'}"${Math.abs(rot-ang)>0.05?` transform="rotate(${(-(rot-ang)).toFixed(1)} ${q[0]|0} ${q[1]|0})"`:''}>${esc(s)}</text>`}
  return h+'</g>'}
 const show2=show;show=function(s,f){show2(s,f);txLoad(s)};
 const draw1=draw;draw=function(){draw1();if(!cur||!vb)return;const svg=$('svg');let h=txSvg();const r=Math.max(vb[2],vb[3])/95;
  N.list.filter(n=>n.sheet===cur.id&&n.at&&n.state!=='closed').forEach(n=>{const q=dpt(n.at);
   h+=`<g class="ntpin" data-note="${esc(n.id)}"><title>Query ${n.no}: ${esc((n.text||'').slice(0,80))}</title><circle cx="${q[0]}" cy="${q[1]}" r="${r}" fill="#7b5fb4" stroke="#fff" stroke-width="${r/5}"/>
    <text x="${q[0]}" y="${q[1]}" font-size="${r*1.05}" font-family="Segoe UI,Arial,sans-serif" font-weight="700" text-anchor="middle" dominant-baseline="central" fill="#fff">${n.no}</text></g>`});
  if(N.flash&&N.flash.sheet===cur.id){const q=dpt(N.flash.at),k=r*1.6;
   h+=`<g pointer-events="none" stroke="#e0218a" stroke-width="${r/3}"><line x1="${q[0]-k}" y1="${q[1]-k}" x2="${q[0]+k}" y2="${q[1]+k}"/><line x1="${q[0]-k}" y1="${q[1]+k}" x2="${q[0]+k}" y2="${q[1]-k}"/><circle cx="${q[0]}" cy="${q[1]}" r="${k*1.3}" fill="none"/></g>`}
  svg.insertAdjacentHTML('beforeend',h);
  svg.querySelectorAll('.ntpin').forEach(g=>g.onclick=ev=>{ev.stopPropagation();openNote(N.list.find(n=>n.id===g.dataset.note))})};
 const svgEl=$('svg');let down=null;
 svgEl.addEventListener('mousedown',ev=>{down=[ev.clientX,ev.clientY]});
 svgEl.addEventListener('click',ev=>{if(!N.pin||!down||Math.hypot(ev.clientX-down[0],ev.clientY-down[1])>4)return;
  const pt=svgEl.createSVGPoint();pt.x=ev.clientX;pt.y=ev.clientY;const q=pt.matrixTransform(svgEl.getScreenCTM().inverse()),Y=-q.y;
  N.pin=false;svgEl.style.cursor='';$('hint').textContent=hint0;
  areaPanel({sheet:cur.id,at:[Math.round(q.x*ca+Y*sa),Math.round(-q.x*sa+Y*ca)]})});
 const hint0=$('hint').textContent;
 function zoomAt(at){const q=dpt(at);vb=[q[0]-6000,q[1]-4200,12000,8400];draw()}
 function openNote(n){if(!n)return;const e=n.el&&byId[n.el];
  if((n.type==='architecture'||n.type==='3D view')&&window.c2rArchNote){window.c2rArchNote(n);return}
  if(e){if(mode==='3d'){if(n.view&&window.c2rGoView)window.c2rGoView(n.view);pick(e);return}show(sheetOf(e.sheet),e);pick(e);return}
  const s=sheetOf(n.sheet);if(mode==='3d')setMode('2d');if(s){show(s);if(n.at)zoomAt(n.at)}areaPanel(n)}
 // ------------------------------------------------------------ the left column
 const aside=document.querySelector('aside');
 const sec=document.createElement('div');
 sec.innerHTML=`<h2>Notes for the consultant <span id="ntCount"></span></h2><div style="padding:0 8px;display:flex;gap:6px">
  <button id="ntArea" title="Then click the place on the plan">+ Note on an area</button><select id="ntF" style="width:auto"><option value="open">not closed</option><option value="all">all</option></select></div>
  <div class="list" id="ntList"></div><div id="chgBox" style="display:none"><h2>Changes in the revision <span id="chgCount"></span></h2>
  <div style="padding:0 8px"><select id="chgF"></select></div><div class="list" id="chgList"></div></div>`;
 aside.insertBefore(sec,aside.firstChild);
 $('ntF').onchange=side;
 $('ntArea').onclick=()=>{if(mode==='3d')setMode('2d');if(!cur){alert('Open a plan first.');return}N.pin=true;svgEl.style.cursor='crosshair';
  $('hint').textContent='Click the place on the plan the note is about (Esc to cancel)'};
 document.addEventListener('keydown',ev=>{if(ev.key==='Escape'&&N.pin){N.pin=false;svgEl.style.cursor='';$('hint').textContent=hint0}});
 function side(){const f=$('ntF').value,ns=N.list.filter(n=>f==='all'||n.state!=='closed');
  $('ntCount').textContent='('+N.list.filter(n=>n.state!=='closed').length+')';
  $('ntList').innerHTML=ns.length?ns.map(n=>`<div class="item" data-note="${esc(n.id)}"><span class="nt-st ${esc(n.state)}">${n.no}</span> <b>${esc(n.mark||(n.type==='area'?'area':'(no mark)'))}</b>
   <small>${esc((n.type||'').replace('_',' '))} &middot; ${esc(n.lv||'')} &middot; ${LB[n.state]||''}${n.reply?' &middot; reply':''}${n.revision?' &middot; '+esc(n.revision.result):''}</small>
   <div class="small">${esc((n.text||'').slice(0,90))}</div></div>`).join(''):'<p class="small" style="padding:0 8px">Click an element, then "Take screenshot" and "Save note" in its panel.</p>';
  document.querySelectorAll('#ntList .item').forEach(d=>d.onclick=()=>openNote(N.list.find(n=>n.id===d.dataset.note)));
  const C=N.chg&&N.chg.changes||[];$('chgBox').style.display=N.chg?'':'none';if(!N.chg)return;
  $('chgCount').textContent='('+C.length+')';const kinds=[...new Set(C.map(c=>c.kind))],cf=$('chgF').value;
  $('chgF').innerHTML='<option value="">all changes ('+C.length+')</option>'+kinds.map(k=>`<option${k===cf?' selected':''}>${k}</option>`).join('');
  $('chgF').onchange=side;const shown=C.filter(c=>!$('chgF').value||c.kind===$('chgF').value);
  $('chgList').innerHTML=shown.length?shown.slice(0,500).map((c,i)=>`<div class="item" data-c="${C.indexOf(c)}"><span class="nt-k" style="background:${KC[c.kind]||'#888'}">${esc(c.kind)}</span><small>${esc(c.lv||'')}</small>
   <div class="small">${esc(c.text)}</div></div>`).join(''):'<p class="small" style="padding:0 8px">The revised drawing shows the same elements as before.</p>';
  document.querySelectorAll('#chgList .item').forEach(d=>d.onclick=()=>{const c=C[+d.dataset.c],e=c.el&&byId[c.el];if(mode==='3d')setMode('2d');
   N.flash={sheet:c.sheet,at:c.at};if(e){show(sheetOf(e.sheet),e);pick(e)}else{const s=sheetOf(c.sheet);if(s){show(s);zoomAt(c.at)}
   $('detail').innerHTML=`<h3 style="margin:0 0 4px">Removed in the revision</h3><p class="small">${esc(c.text)}</p><p class="small">The cross on the plan marks where it was.</p>`;panel(null,{sheet:c.sheet,at:c.at})}})}
 txLoad(cur);
 load();
 // for the 3D view's architect's model (building/arch_ui.py): its notes in the same panel
 window.c2rNotes={panel:(e,a)=>panel(e,a)};
 // the toolbox's Snip (building/arch_ui.py): the view as on screen, the selected element framed, into the editor
 window.c2rSnip=async function(){const in3=mode==='3d';let u=null;
  const area=in3?{title:'3D view',note:{lv:(typeof focusLv!=='undefined'&&focusLv)||'',plan:(D3[cur3]&&D3[cur3].set)||''}}:{title:'Plan view'};
  try{u=await shot(sel||null,null,true,area)}catch(x){alert('No screenshot: '+x.message);return}
  // an element selected: the picture can go to its note (waiting there until "Save note")
  const v=await snip(u,{add:!!sel,name:shotName(sel,area)});
  if(v&&sel){if(N.pendKey===sel.id)N.pend.push(v);else (N.pendBy[sel.id]=N.pendBy[sel.id]||[]).push(v);pick(sel)}};
})();
</script>"""
