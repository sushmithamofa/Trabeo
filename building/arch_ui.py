r"""Review's 3D view: the architect's model over the structure, and a section box - added
by the app (webapp.py) to the page it serves, after the notes hook. review.html itself is
not changed: opened on its own it works as before.

On the page (3D only):
  * "Architecture": add the architect's model (IFC, Rhino, glTF, OBJ - read by the app,
    building/arch_model.py), show it or not, its opacity, its layers / kinds and floors on
    and off, its units and up axis where the file does not say; line it up with the drawing
    set shown (as it is / by the columns / by two points picked / typed), saved per set.
    A structural column with no architectural column on it can be shown;
  * "Section box": the view cut to a box, as Revit's - each side dragged by its handle,
    fitted to the model, a floor, or the selected element;
  * a click on the architect's model (where no structural element is under the cursor,
    or the model is shown at 60 % or more) opens it on the right with its notes - a query
    for the consultant about the architecture;
  * every note made in 3D keeps its view (camera, section box, what is shown): "Go to view".
The architect's model is display only: never in the decisions, the export, or the files
the app writes for Revit, IFC or Rhino.
"""

ARCH_HOOK = r"""<style>
#arPanel,#sbPanel{position:absolute;top:10px;width:340px;max-height:calc(100% - 20px);overflow:auto;background:#fff;
 border:1px solid var(--line);border-radius:8px;box-shadow:0 4px 18px rgba(0,0,0,.14);padding:10px 12px;z-index:4;display:none;font-size:12px}
#arPanel{right:10px}#sbPanel{left:10px;width:280px}
#arPanel h4,#sbPanel h4{margin:0 0 6px;font-size:13px;display:flex;justify-content:space-between;align-items:center}
.ar-m{border:1px solid var(--line);border-radius:7px;padding:7px 8px;margin:8px 0;background:#fcfbf8}
.ar-row{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:5px 0}
.ar-list{max-height:150px;overflow:auto;border:1px solid var(--line);border-radius:5px;padding:3px 6px;margin:4px 0;background:#fff}
.ar-list label{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ar-mute{color:var(--mute)}.ar-warn{color:#a36400}.ar-bad{color:var(--conflict)}.ar-good{color:var(--read)}
.ar-box{border-left:3px solid #1f6fb2;background:#f2f7fc;padding:6px 8px;border-radius:0 6px 6px 0;margin:6px 0}
.ar-box.bad{border-color:var(--conflict);background:#fbeceb}
.ar-num{width:76px!important}details summary{cursor:pointer;margin:4px 0}#arPanel select,#sbPanel select{width:auto}
#arBanner{position:absolute;left:50%;top:10px;transform:translateX(-50%);background:#16202b;color:#fff;padding:6px 12px;
 border-radius:6px;font-size:12px;z-index:5;display:none;max-width:60%;text-align:center}
#d3bar button.on{background:var(--ink);color:#fff;border-color:var(--ink)}
</style>
<script>
(function(){
 if(typeof D3==='undefined'||!D3.length||typeof THREE==='undefined')return;
 const $=id=>document.getElementById(id);
 const AR={models:[],live:{},prop:{},pick:null,swallow:0,down:null,poll:null,ready:false,markers:null,dots:null,busy:{}};
 const BOX={on:false,b:new THREE.Box3(),planes:[0,1,2,3,4,5].map(()=>new THREE.Plane()),helper:null,handles:[],drag:null,set:-1,lv:0,hs:3,show:true};
 try{const s=JSON.parse(localStorage.getItem('c2r_box_handles')||'null');if(s){if(s.hs>=1&&s.hs<=10)BOX.hs=+s.hs;BOX.show=s.show!==false}}catch(e){}
 // ------------------------------------------------------------ clicks: the box's hidden parts are not picked
 const RI=THREE.Raycaster.prototype.intersectObjects;
 THREE.Raycaster.prototype.intersectObjects=function(o,r,t){
  if(AR.swallow){AR.swallow=0;return []}
  const h=RI.call(this,o,r,t);return BOX.on?h.filter(x=>inBox(x.point)):h};
 function swallow(){AR.swallow=1;setTimeout(()=>{AR.swallow=0},0)}
 function inBox(p){const a=BOX.b.min,b=BOX.b.max,e=0.001;return p.x>=a.x-e&&p.x<=b.x+e&&p.y>=a.y-e&&p.y<=b.y+e&&p.z>=a.z-e&&p.z<=b.z+e}
 // ------------------------------------------------------------ the bar and the panels
 $('d3bar').insertAdjacentHTML('beforeend',' <button id="arBtn" title="The architect\'s 3D model over the structure">Architecture</button>'
  +' <button id="sbBtn" title="Cut the 3D view to a box, as Revit\'s section box">Section box</button>');
 $('canvas').insertAdjacentHTML('beforeend','<div id="arPanel"></div><div id="sbPanel"></div><div id="arBanner"></div>');
 function toggle(p,b){const on=$(p).style.display!=='block';$(p).style.display=on?'block':'none';$(b).classList.toggle('on',on);
  if(on){if(p==='arPanel')list();else sbPanel()}}
 $('arBtn').onclick=()=>toggle('arPanel','arBtn');$('sbBtn').onclick=()=>toggle('sbPanel','sbBtn');
 function banner(t){const b=$('arBanner');b.style.display=t?'block':'none';b.innerHTML=t||''}
 // ------------------------------------------------------------ the review page's own 3D steps, extended
 const init0=init3;init3=function(){const ok=init0();if(ok&&!AR.ready)setup();return ok};
 const build0=build3;build3=function(i){build0(i);afterBuild()};
 const mode0=setMode;setMode=function(m){mode0(m);if(mode!=='3d'){['arPanel','sbPanel'].forEach(x=>$(x).style.display='none');
  if(!AR.move)banner('');['arBtn','sbBtn'].forEach(x=>$(x).classList.remove('on'));if(AR.pick){AR.pick=null;clearDots()}AR.mdrag=null;if(GB.g)GB.g.visible=false}
  else if(AR.move)gbPlace();
  if(typeof tbSync==='function')tbSync()};
 function setup(){AR.ready=true;r3.localClippingEnabled=true;AR.root=new THREE.Group();s3.add(AR.root);
  BOX.helper=new THREE.Box3Helper(BOX.b,0x1f6fb2);BOX.helper.visible=false;s3.add(BOX.helper);
  const hm=new THREE.MeshBasicMaterial({color:0x1f6fb2,depthTest:false,transparent:true,opacity:.6});
  for(let k=0;k<6;k++){const h=new THREE.Mesh(new THREE.SphereGeometry(1,16,12),hm);h.userData={handle:true,k};h.visible=false;h.renderOrder=10;s3.add(h);BOX.handles.push(h)}
  // the handles keep their size on screen (the slider in the Section box panel), not the box's
  (function hsize(){requestAnimationFrame(hsize);const r=dist*0.0025*BOX.hs;BOX.handles.forEach(h=>h.scale.setScalar(r))})();
  const gl=$('gl');
  gl.addEventListener('pointerdown',down,true);gl.addEventListener('pointerup',up,true);
  addEventListener('pointermove',ev=>{if(BOX.drag)dragTo(ev)});
  addEventListener('pointerup',ev=>{if(BOX.drag){BOX.drag=null;AR.down=null;ev.stopPropagation()}},true);
  document.addEventListener('keydown',ev=>{if(ev.key==='Escape'&&AR.pick){AR.pick=null;clearDots();banner('');list()}});
  load()}
 function afterBuild(){if(!AR.ready)return;AR.root.rotation.y=grp.rotation.y;
  for(const m of AR.models)place(m);clip();gbPlace();if(AR.markers){s3.remove(AR.markers);AR.markers=null}
  if(BOX.on&&BOX.set!==cur3)fitWhole();if($('arPanel').style.display==='block')list()}
 // ------------------------------------------------------------ the structure in this set
 function setKey(){return D3[cur3]?D3[cur3].set:''}
 function sCols(){return (D3[cur3].items||[]).filter(it=>it.t==='column').map(it=>[it.x,it.y])}
 function assumedLv(){const n=(D3[cur3].notes||[]).find(x=>/^levels .* are shown 3 m per storey/.test(x));
  return n?n.replace(/^levels (.*) are shown 3 m per storey.*$/,'$1').split(', '):[]}
 function sLevels(){const a=assumedLv();return D3[cur3].levels.map(l=>({key:l.key,z:l.z,assumed:a.includes(l.key)}))}
 function sBounds(S){S=S||D3[cur3];let x0=1e18,y0=1e18,x1=-1e18,y1=-1e18;for(const it of S.items){
   const ps=it.p?it.p:it.a?[it.a,it.b]:[[it.x,it.y]];for(const p of ps){if(!p)continue;x0=Math.min(x0,p[0]);x1=Math.max(x1,p[0]);y0=Math.min(y0,p[1]);y1=Math.max(y1,p[1])}}
  return {x:(x0+x1)/2,y:(y0+y1)/2,z:Math.min(...S.levels.map(l=>l.z))}}
 // ------------------------------------------------------------ the models
 function call(url,body){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
  .then(r=>r.json().then(j=>{if(!r.ok)throw new Error(j.error||'Not done');return j}))}
 function load(){return fetch('/api/arch').then(r=>r.json()).then(j=>{AR.models=j.models||[];
   if(AR.ready)for(const m of AR.models)if(m.state==='ready'&&m.show)ensure(m);
   for(const id in AR.live)if(!AR.models.find(m=>m.id===id))drop(id);
   const busy=AR.models.some(m=>m.state==='waiting'||m.state==='reading');
   clearTimeout(AR.poll);if(busy)AR.poll=setTimeout(load,2000);
   if($('arPanel').style.display==='block')list();planSoon()}).catch(()=>{})}
 // a model's file, fetched once for the 3D view and the plan
 AR.raw={};
 function getRaw(m){if(!AR.raw[m.id])AR.raw[m.id]=Promise.all([fetch('/arch/'+m.id+'/meta.json').then(r=>{if(!r.ok)throw 0;return r.json()}),
   fetch('/arch/'+m.id+'/geometry.bin').then(r=>{if(!r.ok)throw 0;return r.arrayBuffer()})]).then(([meta,buf])=>({meta,buf}))
   .catch(e=>{delete AR.raw[m.id];throw e});return AR.raw[m.id]}
 function drop(id){const L=AR.live[id];if(L&&L.outer){AR.root.remove(L.outer);L.outer.traverse(o=>{if(o.geometry)o.geometry.dispose()});L.mats.forEach(m=>m.dispose())}
  delete AR.live[id];delete AR.raw[id];PLAN.cache={}}
 const TINT=[[/glass|glaz|window|curtain/i,0x9ec9e6],[/door/i,0xc9a77c],[/stair|ramp/i,0xc9b3c6],[/roof/i,0xb7a99a],
  [/slab|floor|flr/i,0xb9c3ad],[/site|tree|plant|green|landscape/i,0xa9c49c],[/furn/i,0xd6c7a1]];
 function tint(g){for(const [rx,c] of TINT)if(rx.test(g))return c;return 0xbfc7cf}
 function ensure(m){if(AR.live[m.id]||!AR.ready)return;AR.live[m.id]={loading:true};
  getRaw(m).then(({meta,buf})=>{
   if(!AR.live[m.id])return;
   const outer=new THREE.Group(),inner=new THREE.Group();inner.matrixAutoUpdate=false;outer.add(inner);
   const pb=meta.nverts*12,mats=meta.groups.map(g=>new THREE.MeshLambertMaterial({color:tint(g),transparent:true,opacity:m.opacity,
    depthWrite:false,side:THREE.DoubleSide,flatShading:true}));
   const chunks=meta.chunks.map(c=>{const g=new THREE.BufferGeometry();
    g.setAttribute('position',new THREE.BufferAttribute(new Float32Array(buf,c.v0*12,c.vn*3),3));
    g.setIndex(new THREE.BufferAttribute(new Uint32Array(buf,pb+c.i0*4,c.in),1));g.computeBoundingSphere();
    const me=new THREE.Mesh(g,mats[c.g]);me.userData={arch:m.id,c};me.renderOrder=2;inner.add(me);return me});
   AR.root.add(outer);AR.live[m.id]={outer,inner,mats,chunks,meta};place(m);clip();
   if($('arPanel').style.display==='block')list()}).catch(()=>{AR.live[m.id]={failed:true};list()})}
 function hiddenOf(m,L){return m.hidden==null?(L&&L.meta?L.meta.groups_off||[]:[]):m.hidden}
 function alignOf(m){return AR.prop[m.id+'|'+setKey()]||(m.align||{})[setKey()]||null}
 // the line-up in use: proposed / saved, or - not lined up yet - shown over the structure's middle
 function curAl(m,L){const al=alignOf(m);if(al)return {al,rough:false};
  const s=sBounds(),c=roughMid(m,L);return {al:{rot:0,dx:s.x-c[0],dy:s.y-c[1],dz:s.z-c[2]},rough:true}}
 function place(m){const L=AR.live[m.id];if(!L||!L.inner||!D3[cur3])return;
  const {al,rough}=curAl(m,L);
  L.rough=rough;L.inner.matrix.copy(matFor(L.meta,m,al));L.inner.matrixWorldNeedsUpdate=true;
  const hid=hiddenOf(m,L);L.outer.visible=!!m.show;
  for(const c of L.chunks){const cc=c.userData.c,fl=cc.s>=0?L.meta.storeys[cc.s].name:null;
   c.visible=!hid.includes(L.meta.groups[cc.g])&&!(fl!==null&&hid.includes('floor:'+fl))}
  L.mats.forEach(x=>{x.opacity=m.opacity;x.depthWrite=m.opacity>=0.99;x.transparent=m.opacity<0.99;x.needsUpdate=true});
  const rb=AR.models.filter(x=>x.show&&AR.live[x.id]&&AR.live[x.id].rough);
  if(!AR.pick&&!AR.move)banner(rb.length?`${esc(rb.map(x=>x.name).join(', '))}: not lined up with ${esc(setKey())} yet - shown roughly over the structure. Line it up in the Architecture panel.`:'')}
 // the middle of what is shown (mm, Z up) - its layers ticked, and stray objects far off left
 // out (a site or terrain kilometres across, a stray point): the middle 80 % of the objects
 function roughMid(m,L){const hid=hiddenOf(m,L),G=L.meta.groups,F=L.meta.storeys||[],key=JSON.stringify([hid,m.unit_mm,m.up]);
  if(L.midKey===key)return L.mid;const xs=[],ys=[],zs=[];
  for(const e of L.meta.elements){if(hid.includes(G[e.g])||(e.s>=0&&F[e.s]&&hid.includes('floor:'+F[e.s].name)))continue;
   const lo=zup(e.b.slice(0,3),m,L),hi=zup(e.b.slice(3),m,L);xs.push((lo[0]+hi[0])/2);ys.push((lo[1]+hi[1])/2);zs.push(Math.min(lo[2],hi[2]))}
  let mid;if(!xs.length){const b=L.meta.bbox,lo=zup(b.slice(0,3),m,L),hi=zup(b.slice(3),m,L);mid=[(lo[0]+hi[0])/2,(lo[1]+hi[1])/2,Math.min(lo[2],hi[2])]}
  else{const q=(a,p)=>a[Math.floor(p*(a.length-1))];[xs,ys,zs].forEach(a=>a.sort((x,y)=>x-y));
   mid=[(q(xs,.1)+q(xs,.9))/2,(q(ys,.1)+q(ys,.9))/2,q(zs,.05)]}
  L.midKey=key;L.mid=mid;return mid}
 // file units and axes -> mm, Z up (as building/arch_align.py)
 function zup(p,m,L){const u=m.unit_mm||L.meta.unit_mm||1,x=p[0]*u,y=p[1]*u,z=p[2]*u;return (m.up||L.meta.up)==='y'?[x,-z,y]:[x,y,z]}
 function matFor(meta,m,al){const o=meta.origin,u=m.unit_mm||meta.unit_mm||1,c=Math.cos(al.rot*Math.PI/180),s=Math.sin(al.rot*Math.PI/180);
  const T0=new THREE.Matrix4().makeTranslation(o[0],o[1],o[2]),S=new THREE.Matrix4().makeScale(u,u,u);
  const U=(m.up||meta.up)==='y'?new THREE.Matrix4().set(1,0,0,0, 0,0,-1,0, 0,1,0,0, 0,0,0,1):new THREE.Matrix4();
  const R=new THREE.Matrix4().set(c,-s,0,al.dx, s,c,0,al.dy, 0,0,1,al.dz||0, 0,0,0,1);
  const K=new THREE.Matrix4().set(.001,0,0,0, 0,0,.001,0, 0,-.001,0,0, 0,0,0,1);
  return new THREE.Matrix4().multiplyMatrices(K,R).multiply(U).multiply(S).multiply(T0)}
 function save(m,body){return call('/api/arch/settings',{id:m.id,...body}).then(n=>{Object.assign(m,n);return m}).catch(e=>{alert('Not saved: '+e.message)})}
 // ------------------------------------------------------------ the Architecture panel
 const UNITS=[[1,'millimetres'],[10,'centimetres'],[1000,'metres'],[25.4,'inches'],[304.8,'feet']];
 function list(){const p=$('arPanel');if(p.style.display!=='block')return;const k=setKey();
  let h=`<h4>Architect's model <button id="arClose" title="Close">&times;</button></h4>
  <div class="ar-row"><button class="primary" id="arAdd">+ Add a model</button><span class="ar-mute">IFC, Rhino (.3dm), glTF (.glb), OBJ</span>
  <input type="file" id="arFile" accept=".ifc,.3dm,.glb,.gltf,.obj" style="display:none"></div>
  <div class="ar-mute">For checking only: never built, never exported.</div>`;
  if(!AR.models.length)h+=`<p class="ar-mute">No architectural model yet. Revit: export IFC (File &gt; Export &gt; IFC). Rhino: save the .3dm with the view shaded once.</p>`;
  for(const m of AR.models)h+=modelHTML(m,k);
  p.innerHTML=h;$('arClose').onclick=()=>toggle('arPanel','arBtn');$('arAdd').onclick=()=>$('arFile').click();$('arFile').onchange=upload;
  for(const m of AR.models)wire(m,k)}
 function modelHTML(m,k){const L=AR.live[m.id],st=m.state;
  let h=`<div class="ar-m" data-m="${m.id}"><div class="ar-row"><label><input type="checkbox" data-show ${m.show?'checked':''} ${st!=='ready'?'disabled':''}> <b>${esc(m.name)}</b></label>
   <span class="ar-mute">${esc(m.fmt)}</span></div>`;
  if(st!=='ready'){h+=`<div class="${st==='failed'?'ar-bad':'ar-mute'}">${st==='failed'?'Could not be read: ':''}${esc(m.message||'Waiting ...')}</div>
   <div class="ar-row">${st==='failed'?'<button data-again>Read again</button>':''}<button data-remove>Remove</button></div></div>`;return h}
  h+=`<div class="ar-mute">${esc(m.message||'')}${(m.notes||[]).map(n=>'<br>'+esc(n)).join('')}</div>`;
  if(L&&L.failed)h+=`<div class="ar-bad">The model's file could not be loaded into the view. Read it again.</div>`;
  h+=`<div class="ar-row">Opacity <input type="range" data-op min="5" max="100" value="${Math.round(m.opacity*100)}" style="flex:1"> <span data-opv>${Math.round(m.opacity*100)}%</span></div>`;
  if(!m.unit_sure)h+=`<div class="ar-row ar-warn">The file does not say its units: 1 unit = <select data-unit>${UNITS.map(([v,n])=>`<option value="${v}"${Math.abs(v-(m.unit_mm||1))<1e-9?' selected':''}>${n}</option>`).join('')}</select> - suggested by its size; check it.</div>`;
  if(!m.up_sure)h+=`<div class="ar-row ar-warn">Up is <select data-up><option value="y"${m.up==='y'?' selected':''}>Y (most OBJ files)</option><option value="z"${m.up==='z'?' selected':''}>Z</option></select> - if the model lies on its side, change it.</div>`;
  if(L&&L.meta){const hid=hiddenOf(m,L),G=L.meta.groups,F=L.meta.storeys||[];
   h+=`<details><summary>${m.fmt==='Rhino'?'Layers':'Kinds'} (${G.length})</summary><div class="ar-row"><button data-all="1">All</button><button data-all="0">None</button></div>
    <div class="ar-list">${G.map(g=>`<label title="${esc(g)}"><input type="checkbox" data-g="${esc(g)}" ${hid.includes(g)?'':'checked'}> ${esc(g)}</label>`).join('')}</div></details>`;
   if(F.length)h+=`<details><summary>Floors (${F.length})</summary><div class="ar-list">${F.map(f=>`<label><input type="checkbox" data-g="${esc('floor:'+f.name)}" ${hid.includes('floor:'+f.name)?'':'checked'}> ${esc(f.name)}${f.elev!=null?` <span class="ar-mute">${(f.elev*(m.unit_mm||1)/1000).toFixed(3)} m</span>`:''}</label>`).join('')}</div></details>`}
  else if(m.show)h+=`<div class="ar-mute">Loading into the view ...</div>`;
  // the line-up with the drawing set shown
  const al=(m.align||{})[k],pr=AR.prop[m.id+'|'+k];
  h+=`<div style="margin-top:6px"><b>Lined up with ${esc(k)}</b></div>`;
  if(al&&!pr)h+=`<div class="ar-good">${al.pinned?'<b>Pinned</b> &middot; ':''}${esc(al.how||'saved')} &middot; turned ${(+al.rot).toFixed(2)}&deg;, moved ${(al.dx/1000).toFixed(3)} m, ${(al.dy/1000).toFixed(3)} m, height ${al.dz>=0?'+':''}${Math.round(al.dz)} mm<br><span class="ar-mute">${esc(al.date||'')} ${esc(al.by||'')}</span></div>`;
  else if(!pr)h+=`<div class="ar-warn">Not lined up yet.</div>`;
  if(pr)h+=propHTML(m,pr);
  if(!pr&&al&&al.pinned)h+=`<div class="ar-row"><button data-unpin title="Free the model to line it up again">Unpin</button><span class="ar-mute">Pinned: it cannot be moved by accident.</span></div>`;
  else if(!pr)h+=`<div class="ar-row"><button data-move title="Drag the model with the mouse, then pin it">Move by hand</button><button data-auto ${AR.busy[m.id]?'disabled':''}>${AR.busy[m.id]?'Working ...':'Line up automatically'}</button><button data-two>Two points</button><button data-adjust>Type it</button></div>`;
  h+=`<div class="ar-row" style="margin-top:8px"><button data-again>Read again</button><button data-remove>Remove</button></div></div>`;return h}
 function propHTML(m,pr){const bad=pr.ok===false;
  let h=`<div class="ar-box${bad?' bad':''}">${esc(pr.text||'')}`;
  if(pr.heights)h+=`<div class="ar-mute" style="margin-top:4px">${esc(pr.heights.text)}</div>`;
  if(pr.alt)h+=`<div style="margin-top:4px"><button data-alt>Try the other fit</button></div>`;
  if(pr.n_off)h+=`<div style="margin-top:4px">${pr.n_off} structural column${pr.n_off==1?' has':'s have'} no column of the architect's on ${pr.n_off==1?'it':'them'}. <button data-off>Show them</button></div>`;
  h+=`</div>`;
  if(bad&&!pr.edit)return h+`<div class="ar-row"><button data-two>Two points</button><button data-adjust>Type it</button><button data-discard>Close</button></div>`;
  h+=`<div class="ar-row">Turn <input class="ar-num" type="text" data-f="rot" value="${(+pr.rot||0).toFixed(3)}">&deg;</div>
   <div class="ar-row">Move x <input class="ar-num" type="text" data-f="dx" value="${Math.round(pr.dx||0)}"> y <input class="ar-num" type="text" data-f="dy" value="${Math.round(pr.dy||0)}"> mm</div>
   <div class="ar-row">Height <input class="ar-num" type="text" data-f="dz" value="${Math.round(pr.dz||0)}"> mm <span class="ar-mute">(+ moves the model up)</span></div>
   <div class="ar-row">${AR.move&&AR.move.id===m.id?'<button data-stop>Stop moving</button>':'<button data-move>Move by hand</button>'}<button data-fine title="Snap exactly onto the structure's columns from where it is now">Fine-tune by the columns</button></div>
   <div class="ar-row"><button class="primary" data-pin title="Save this position and lock it">Pin here</button><button data-keep>Save line-up</button><button data-discard>Discard</button></div>`;return h}
 function wire(m,k){const d=document.querySelector(`#arPanel [data-m="${m.id}"]`);if(!d)return;const q=s=>d.querySelector(s),key=m.id+'|'+k;
  const sh=q('[data-show]');if(sh)sh.onchange=()=>{m.show=sh.checked;if(m.show)ensure(m);place(m);save(m,{show:m.show})};
  const op=q('[data-op]');if(op){op.oninput=()=>{m.opacity=op.value/100;q('[data-opv]').textContent=op.value+'%';place(m)};op.onchange=()=>save(m,{opacity:m.opacity})}
  const un=q('[data-unit]');if(un)un.onchange=()=>{m.unit_mm=+un.value;delete AR.prop[key];place(m);save(m,{unit_mm:m.unit_mm}).then(list)};
  const upx=q('[data-up]');if(upx)upx.onchange=()=>{m.up=upx.value;delete AR.prop[key];place(m);save(m,{up:m.up}).then(list)};
  d.querySelectorAll('[data-g]').forEach(c=>c.onchange=()=>{const L=AR.live[m.id];let hid=hiddenOf(m,L).slice();const g=c.dataset.g;
   hid=c.checked?hid.filter(x=>x!==g):[...hid,g];m.hidden=hid;place(m);save(m,{hidden:hid})});
  d.querySelectorAll('[data-all]').forEach(b=>b.onclick=()=>{const L=AR.live[m.id];const fl=hiddenOf(m,L).filter(x=>x.startsWith('floor:'));
   m.hidden=b.dataset.all==='1'?fl:[...L.meta.groups,...fl];place(m);save(m,{hidden:m.hidden});list()});
  const rm=q('[data-remove]');if(rm)rm.onclick=()=>{if(!confirm('Remove '+m.name+' from the project? (Your drawings and the structure are not touched.)'))return;
   call('/api/arch/remove',{id:m.id}).then(()=>{drop(m.id);load()}).catch(e=>alert(e.message))};
  d.querySelectorAll('[data-again]').forEach(b=>b.onclick=()=>call('/api/arch/again',{id:m.id}).then(()=>{drop(m.id);load()}).catch(e=>alert(e.message)));
  const au=q('[data-auto]');if(au)au.onclick=()=>auto(m);
  d.querySelectorAll('[data-two]').forEach(b=>b.onclick=()=>startPick(m));
  d.querySelectorAll('[data-adjust]').forEach(b=>b.onclick=()=>{const cur=alignOf(m)||{rot:0,dx:0,dy:0,dz:0};
   AR.prop[key]={...(AR.prop[key]||{}),rot:cur.rot,dx:cur.dx,dy:cur.dy,dz:cur.dz,ok:true,edit:true,how:'typed',
    text:(AR.prop[key]&&AR.prop[key].text)||'Type the turn and the shift (the model as shown moves with them).'};place(m);list()});
  d.querySelectorAll('[data-f]').forEach(i=>i.oninput=()=>{const v=parseFloat(i.value);if(!isFinite(v))return;const p=AR.prop[key];if(!p)return;
   p[i.dataset.f]=v;if(p.how&&!/typed|adjusted/.test(p.how))p.how+=', adjusted';place(m)});
  d.querySelectorAll('[data-discard]').forEach(b=>b.onclick=()=>{delete AR.prop[key];moveStop();if(AR.markers){s3.remove(AR.markers);AR.markers=null}place(m);list()});
  const keep=pin=>{const p=AR.prop[key];moveStop();
   save(m,{set:k,align:{rot:p.rot,dx:p.dx,dy:p.dy,dz:p.dz||0,how:p.how||'typed',pinned:pin}}).then(()=>{delete AR.prop[key];place(m);list();planSoon()})};
  const kp=q('[data-keep]');if(kp)kp.onclick=()=>keep(false);
  const pn=q('[data-pin]');if(pn)pn.onclick=()=>keep(true);
  const up_=q('[data-unpin]');if(up_)up_.onclick=()=>{const a=m.align[k];save(m,{set:k,align:{...a,pinned:false}}).then(list)};
  d.querySelectorAll('[data-move]').forEach(b=>b.onclick=()=>moveStart(m));
  const st=q('[data-stop]');if(st)st.onclick=()=>{moveStop();list()};
  const fi=q('[data-fine]');if(fi)fi.onclick=()=>{const p=AR.prop[key];fi.disabled=true;fi.textContent='Working ...';
   call('/api/arch/refine',{id:m.id,cols:sCols(),al:{rot:p.rot,dx:p.dx,dy:p.dy}}).then(r=>{
    if(r.ok){Object.assign(p,{rot:r.rot,dx:r.dx,dy:r.dy,text:r.text,how:'moved by hand, fine-tuned by the columns',n_off:r.n_off,off:r.off});place(m)}
    else alert(r.text)}).catch(e=>alert(e.message)).finally(()=>list())};
  const of=q('[data-off]');if(of)of.onclick=()=>showOff(AR.prop[key].off||[]);
  const alt=q('[data-alt]');if(alt)alt.onclick=()=>{const p=AR.prop[key],{alt:a,...rest}=p;
   AR.prop[key]={...rest,...a,alt:{...rest,text:'The first fit: '+rest.text}};if(AR.markers){s3.remove(AR.markers);AR.markers=null}place(m);list()}}
 function upload(ev){const f=ev.target.files[0];ev.target.value='';if(!f)return;
  banner(`Sending ${esc(f.name)} (${(f.size/1048576).toFixed(1)} MB) to the app ...`);
  fetch('/api/arch/upload?name='+encodeURIComponent(f.name),{method:'POST',body:f}).then(r=>r.json().then(j=>{if(!r.ok)throw new Error(j.error||'Not added');return j}))
   .then(()=>{banner('');load()}).catch(e=>{banner('');alert(e.message)})}
 function auto(m){const k=setKey();AR.busy[m.id]=true;list();
  call('/api/arch/align',{id:m.id,set:k,cols:sCols(),levels:sLevels()}).then(p=>{
   const key=m.id+'|'+k;p.how=p.how||'';AR.prop[key]={...p,dz:p.dz||0};place(m)}).catch(e=>alert(e.message))
   .finally(()=>{AR.busy[m.id]=false;list()})}
 function showOff(pts){if(AR.markers)s3.remove(AR.markers);const g=new THREE.Group(),lv=D3[cur3].levels.map(l=>l.z/1000);
  const z0=Math.min(...lv)-0.5,z1=Math.max(...lv)+0.5,mt=new THREE.MeshBasicMaterial({color:0xc0392b,transparent:true,opacity:.55,depthTest:false});
  for(const [x,y] of pts){const c=new THREE.Mesh(new THREE.CylinderGeometry(0.35,0.35,z1-z0,16,1,true),mt);c.position.set(x/1000,(z0+z1)/2,-y/1000);c.renderOrder=6;g.add(c)}
  g.rotation.y=grp.rotation.y;s3.add(g);AR.markers=g}
 // ------------------------------------------------------------ picking: two points, a click on the architecture
 function rayAt(ev){const r=$('gl').getBoundingClientRect(),ray=new THREE.Raycaster();
  ray.setFromCamera(new THREE.Vector2((ev.clientX-r.left)/r.width*2-1,-(ev.clientY-r.top)/r.height*2+1),cam);return ray}
 function structHit(ray){const h=RI.call(ray,meshes.filter(m=>m.visible&&m.material.opacity>0.2),false).filter(x=>!BOX.on||inBox(x.point));return h[0]||null}
 function archHit(ray,only){const objs=[];for(const id in AR.live){const L=AR.live[id];if(!L.outer||!L.outer.visible||(only&&id!==only))continue;
   L.chunks.forEach(c=>{if(c.visible)objs.push(c)})}
  const h=RI.call(ray,objs,false).filter(x=>!BOX.on||inBox(x.point));return h[0]||null}
 function elOf(h){const c=h.object.userData.c,L=AR.live[h.object.userData.arch];let lo=0,hi=c.els.length-1;const f=h.faceIndex;
  while(lo<hi){const mid=(lo+hi+1)>>1;if(c.els[mid][0]<=f)lo=mid;else hi=mid-1}return L.meta.elements[c.els[lo][2]]}
 function toSet(p){const l=grp.worldToLocal(p.clone());return [Math.round(l.x*1000),Math.round(-l.z*1000)]}
 function down(ev){AR.down=[ev.clientX,ev.clientY];
  // a measurement's end, dragged over the model
  if(ev.button===0&&typeof MS!=='undefined'&&MS.d3.length){const dots=MS.d3.flatMap(g=>g.userData.dots);
   const h=RI.call(rayAt(ev),dots,false)[0];if(h){MS.drag3={g:h.object.userData.ms3,end:h.object.userData.end};ev.stopPropagation();ev.preventDefault();return}}
  if(BOX.on&&ev.button===0){const h=RI.call(rayAt(ev),BOX.handles.filter(x=>x.visible),false)[0];
   if(h){BOX.drag={k:h.object.userData.k,start:BOX.b.clone()};ev.stopPropagation();ev.preventDefault();return}}
  // moving the architect's model by hand: left-drag moves it, Alt / Ctrl + drag turns it about the
  // point grabbed (right-drag and Shift + drag still pan, the wheel zooms)
  if(AR.move&&ev.button===0&&GB.g&&GB.g.visible){const h=RI.call(rayAt(ev),GB.picks,false)[0],p=AR.prop[AR.move.key];
   if(h&&p){const k=h.object.userData.gb,y=GB.g.getWorldPosition(new THREE.Vector3()).y;
    GB.drag={k,y,al0:{rot:p.rot,dx:p.dx,dy:p.dy,dz:p.dz||0},pv:GB.pv.slice(),p0:planePt(ev,y),t0:k.length===1&&k!=='r'?axisT(ev,k):0};
    ev.stopPropagation();ev.preventDefault();return}}
  if(AR.move&&ev.button===0&&!ev.shiftKey){const p=AR.prop[AR.move.key],p0=planePt(ev);if(!p||!p0)return;
   AR.mdrag={x:ev.clientX,p0,al0:{rot:p.rot,dx:p.dx,dy:p.dy},turn:ev.altKey||ev.ctrlKey};ev.stopPropagation();ev.preventDefault()}}
 // where the pointer is on the level plane through the view's centre, in the drawing set's mm
 function planePt(ev,y){const w=new THREE.Vector3();if(!rayAt(ev).ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0,1,0),-(y===undefined?tgt.y:y)),w))return null;
  const l=grp.worldToLocal(w);return [l.x*1000,-l.z*1000]}
 // ------------------------------------------------------------ the gumball (as Rhino's), while moving by hand
 // red / green arrows: along the drawing's x / y; blue: up and down; yellow ring: turn (Shift: 5 deg steps);
 // white square: anywhere in plan. Kept the same size on screen.
 const GB={g:null,picks:[],drag:null,pv:[0,0]};
 const GBDIR={x:new THREE.Vector3(1,0,0),y:new THREE.Vector3(0,0,-1),z:new THREE.Vector3(0,1,0)};
 function gbMake(){const g=new THREE.Group();
  const mk=(geo,col,op)=>{const m=new THREE.Mesh(geo,new THREE.MeshBasicMaterial({color:col,depthTest:false,depthWrite:false,transparent:true,opacity:op===undefined?1:op}));m.renderOrder=20;return m};
  for(const [k,col] of [['x',0xd03030],['y',0x2e9d4a],['z',0x2f6fd0]]){const a=new THREE.Group();
   const shaft=mk(new THREE.CylinderGeometry(0.012,0.012,0.8,8),col);shaft.position.y=0.4;
   const head=mk(new THREE.ConeGeometry(0.05,0.2,16),col);head.position.y=0.9;
   // its grab zone starts clear of the white square in the middle
   const pick=mk(new THREE.CylinderGeometry(0.07,0.07,0.85,8),col,0.001);pick.position.y=0.625;pick.userData={gb:k};
   a.add(shaft,head,pick);a.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),GBDIR[k]);g.add(a);GB.picks.push(pick)}
  const ring=mk(new THREE.TorusGeometry(0.6,0.012,8,64),0xe0a020);ring.rotation.x=Math.PI/2;
  const rpick=mk(new THREE.TorusGeometry(0.6,0.06,8,48),0xe0a020,0.001);rpick.rotation.x=Math.PI/2;rpick.userData={gb:'r'};
  const sq=mk(new THREE.BoxGeometry(0.14,0.014,0.14),0xffffff,0.95);sq.userData={gb:'xy'};
  g.add(ring,rpick,sq);GB.picks.push(rpick,sq);g.visible=false;AR.root.add(g);GB.g=g;
  (function size(){requestAnimationFrame(size);if(GB.g.visible)GB.g.scale.setScalar(Math.max(0.3,dist*0.13))})()}
 function gbPlace(){if(!AR.ready)return;if(!GB.g)gbMake();const mv=AR.move;if(!mv){GB.g.visible=false;return}
  const m=AR.models.find(x=>x.id===mv.id),L=m&&AR.live[m.id],p=AR.prop[mv.key];if(!L||!L.inner||!p){GB.g.visible=false;return}
  const c0=roughMid(m,L),c=Math.cos(p.rot*Math.PI/180),s=Math.sin(p.rot*Math.PI/180);
  const x=c*c0[0]-s*c0[1]+p.dx,y=s*c0[0]+c*c0[1]+p.dy,z=c0[2]+(p.dz||0);GB.pv=[x,y];
  GB.g.position.set(x/1000,z/1000,-y/1000);GB.g.visible=true}
 // how far along an arrow the pointer is (m, from the gumball's centre)
 function axisT(ev,k){const ray=rayAt(ev).ray,o=GB.g.getWorldPosition(new THREE.Vector3()),u=GBDIR[k].clone().transformDirection(AR.root.matrixWorld);
  const w0=o.clone().sub(ray.origin),b=u.dot(ray.direction),d=u.dot(w0),e=ray.direction.dot(w0),den=1-b*b;
  return Math.abs(den)<1e-6?0:(b*e-d)/den}
 function gbDrag(ev){const d=GB.drag,a=d.al0;
  if(d.k==='x'||d.k==='y'||d.k==='z'){const t=(axisT(ev,d.k)-d.t0)*1000;
   if(d.k==='x')moveTo({rot:a.rot,dx:a.dx+t,dy:a.dy});else if(d.k==='y')moveTo({rot:a.rot,dx:a.dx,dy:a.dy+t});
   else{const p=AR.prop[AR.move.key];p.dz=a.dz+t;moveTo({rot:a.rot,dx:a.dx,dy:a.dy})}return}
  const p1=planePt(ev,d.y);if(!p1||!d.p0)return;
  if(d.k==='xy'){moveTo({rot:a.rot,dx:a.dx+p1[0]-d.p0[0],dy:a.dy+p1[1]-d.p0[1]});return}
  let deg=(Math.atan2(p1[1]-d.pv[1],p1[0]-d.pv[0])-Math.atan2(d.p0[1]-d.pv[1],d.p0[0]-d.pv[0]))*180/Math.PI;
  deg=((deg+540)%360)-180;if(ev.shiftKey)deg=Math.round(deg/5)*5;moveTo(turnAbout(a,deg,d.pv))}
 addEventListener('pointermove',ev=>{if(GB.drag)gbDrag(ev)});
 addEventListener('pointerup',ev=>{if(GB.drag){GB.drag=null;AR.down=null;ev.stopPropagation();list()}},true);
 function turnAbout(al,deg,pv){const c=Math.cos(deg*Math.PI/180),s=Math.sin(deg*Math.PI/180),x=al.dx-pv[0],y=al.dy-pv[1];
  return {rot:al.rot+deg,dx:c*x-s*y+pv[0],dy:s*x+c*y+pv[1]}}
 function moveTo(al){const mv=AR.move;if(!mv)return;const p=AR.prop[mv.key];if(!p)return;Object.assign(p,al);
  const m=AR.models.find(x=>x.id===mv.id);if(m)place(m);gbPlace();if(mode!=='3d')draw();
  document.querySelectorAll(`#arPanel [data-m="${mv.id}"] [data-f]`).forEach(i=>{const f=i.dataset.f;i.value=f==='rot'?(+p.rot).toFixed(3):Math.round(p[f]||0)})}
 addEventListener('pointermove',ev=>{const d=AR.mdrag;if(!d)return;
  if(d.turn)moveTo(turnAbout(d.al0,(ev.clientX-d.x)*0.2,d.p0));
  else{const p1=planePt(ev);if(p1)moveTo({rot:d.al0.rot,dx:d.al0.dx+p1[0]-d.p0[0],dy:d.al0.dy+p1[1]-d.p0[1]})}});
 addEventListener('pointerup',ev=>{if(AR.mdrag){AR.mdrag=null;AR.down=null;ev.stopPropagation()}},true);
 function moveStart(m){const L=AR.live[m.id];if(!L||!L.inner){if(!m.show){m.show=true;ensure(m);save(m,{show:true})}alert('The model is still loading - try again in a moment.');return}
  const k=setKey(),key=m.id+'|'+k,{al}=curAl(m,L);
  AR.prop[key]={...(AR.prop[key]||{}),rot:al.rot||0,dx:al.dx,dy:al.dy,dz:al.dz||0,ok:true,edit:true,how:'moved by hand',
   text:'Moved by hand. Drag to move, Alt + drag to turn; then Pin here (or Fine-tune by the columns first).'};
  AR.move={id:m.id,key};place(m);gbPlace();list();
  banner(`Moving ${esc(m.name)}: gumball arrows red / green = along x / y, blue = up / down, yellow ring = turn (Shift: 5&deg;), white square = in plan &middot; or drag anywhere &middot; arrow keys 100 mm &middot; Esc stops`)}
 function moveStop(){if(!AR.move)return;AR.move=null;AR.mdrag=null;GB.drag=null;banner('');for(const m of AR.models)place(m);gbPlace();
  if(mode!=='3d')draw();if(typeof tbSync==='function')tbSync()}
 document.addEventListener('keydown',ev=>{const mv=AR.move;if(!mv)return;const t=ev.target&&ev.target.tagName;
  if(t==='INPUT'||t==='TEXTAREA'||t==='SELECT')return;const p=AR.prop[mv.key];if(!p)return;
  if(ev.key==='Escape'){moveStop();list();return}
  const big=ev.shiftKey,step=big?1000:100;let al=null;
  const S0=mode==='3d'?D3[cur3]:D3.find(x=>cur&&x.set===cur.set);if(!S0)return;
  if(mode!=='3d'&&ev.key.startsWith('Arrow')){// the plan: along the screen
   const v=ev.key==='ArrowUp'?[sa,ca]:ev.key==='ArrowDown'?[-sa,-ca]:ev.key==='ArrowRight'?[ca,-sa]:[-ca,sa];
   al={rot:p.rot,dx:p.dx+v[0]*step,dy:p.dy+v[1]*step}}
  else if(ev.key==='['||ev.key===']'){const s=sBounds(S0);al=turnAbout(p,(ev.key===']'?-1:1)*(big?5:0.5),[s.x,s.y])}
  else if(ev.key.startsWith('Arrow')){// along the screen: up = away from the viewer
   const f=new THREE.Vector3();cam.getWorldDirection(f);f.y=0;if(f.lengthSq()<1e-9)f.set(0,0,-1);f.normalize();
   const r=new THREE.Vector3().crossVectors(f,new THREE.Vector3(0,1,0)),q=grp.quaternion.clone().invert();f.applyQuaternion(q);r.applyQuaternion(q);
   const v=ev.key==='ArrowUp'?f:ev.key==='ArrowDown'?f.clone().negate():ev.key==='ArrowRight'?r:r.clone().negate();
   al={rot:p.rot,dx:p.dx+v.x*step,dy:p.dy-v.z*step}}
  else if(ev.key==='PageUp'||ev.key==='PageDown'){p.dz=(p.dz||0)+(ev.key==='PageUp'?1:-1)*(big?500:50);al={rot:p.rot,dx:p.dx,dy:p.dy}}
  if(al){ev.preventDefault();moveTo(al)}});
 function up(ev){if(!AR.down||BOX.drag)return;const far=Math.hypot(ev.clientX-AR.down[0],ev.clientY-AR.down[1]);AR.down=null;
  if(far>=4||ev.button!==0)return;const ray=rayAt(ev);
  if(typeof TOOL!=='undefined'&&TOOL.cur==='measure'){swallow();m3Click(ray);return}
  if(AR.pick){swallow();pickPoint(ray);return}
  if(AR.move)return;
  if(!Object.values(AR.live).some(L=>L.outer&&L.outer.visible))return;
  const a=archHit(ray);if(!a)return;const s=structHit(ray);const m=AR.models.find(x=>x.id===a.object.userData.arch);
  if(s&&!(m&&m.opacity>=0.6&&a.distance<s.distance-0.05))return;      // the structure first
  swallow();archPanel(a)}
 function startPick(m){if(!m.show){m.show=true;ensure(m);save(m,{show:true})}
  AR.pick={id:m.id,step:0,s:[],a:[]};clearDots();pickBanner();list()}
 function pickBanner(){const p=AR.pick,t=['Click the FIRST point on the STRUCTURE (a column: its centre is taken)',
  "Click the same point on the ARCHITECT'S model",'Click a SECOND point on the STRUCTURE, far from the first',
  "Click the same second point on the ARCHITECT'S model"][p.step];banner(`Two points ${p.step+1}/4: ${t} &middot; Esc to stop`)}
 function dot(p,col){if(!AR.dots){AR.dots=new THREE.Group();s3.add(AR.dots)}
  const d=new THREE.Mesh(new THREE.SphereGeometry(0.25,16,12),new THREE.MeshBasicMaterial({color:col,depthTest:false,depthWrite:false,transparent:true}));d.position.copy(p);d.renderOrder=31;AR.dots.add(d)}
 function clearDots(){if(AR.dots){s3.remove(AR.dots);AR.dots=null}}
 function pickPoint(ray){const p=AR.pick,m=AR.models.find(x=>x.id===p.id),L=AR.live[p.id];if(!m||!L||!L.inner){AR.pick=null;banner('');return}
  if(p.step%2===0){const h=structHit(ray);if(!h){banner('Click on a structural element - '+$('arBanner').innerHTML.replace(/^Click on a structural element - /,''));return}
   const it=h.object.userData;p.s.push(it.t==='column'?[it.x,it.y]:toSet(h.point));dot(h.point,0xc0392b)}
  else{const h=archHit(ray,p.id);if(!h){return}
   const el=elOf(h),lo=zup(el.b.slice(0,3),m,L),hi=zup(el.b.slice(3),m,L),w=Math.abs(hi[0]-lo[0]),d=Math.abs(hi[1]-lo[1]);
   if(Math.max(w,d)<=1500&&Math.min(w,d)>=100)p.a.push([(lo[0]+hi[0])/2,(lo[1]+hi[1])/2]);
   else{const l=L.inner.worldToLocal(h.point.clone()),o=L.meta.origin;p.a.push(zup([l.x+o[0],l.y+o[1],l.z+o[2]],m,L).slice(0,2))}
   dot(h.point,0x1f6fb2)}
  p.step++;if(p.step<4){pickBanner();return}
  const k=setKey(),key=m.id+'|'+k,cur=alignOf(m);AR.pick=null;banner('Working ...');
  call('/api/arch/two_points',{s1:p.s[0],s2:p.s[1],a1:p.a[0],a2:p.a[1]}).then(r=>{
   AR.prop[key]={...r,ok:true,dz:(cur&&!L.rough)?cur.dz:((AR.prop[key]||{}).dz||0)};banner('');clearDots();place(m);
   if($('arPanel').style.display!=='block')toggle('arPanel','arBtn');else list()}).catch(e=>{banner('');clearDots();alert(e.message)})}
 // ------------------------------------------------------------ the architect's element on the right, with its notes
 function archPanel(h){const id=h.object.userData.arch,L=AR.live[id],m=AR.models.find(x=>x.id===id),el=elOf(h);if(!m||!el)return;
  const at=toSet(h.point),fl=el.s>=0&&L.meta.storeys[el.s]?L.meta.storeys[el.s].name:'',kind=L.meta.groups[el.g]||'';
  showArch({model:m.id,el:el.id||el.n,kind,name:el.n,cls:el.c,fl,at,file:m.name},null)}
 function showArch(a,noteId){sel=null;paint3();
  $('detail').innerHTML=`<h3 style="margin:0 0 4px">${esc(a.name||'(no name)')} <span class="badge" style="background:#7d8a96">architecture</span></h3>
   <div class="small">${esc(a.kind||'')}${a.fl?' &middot; '+esc(a.fl):''} &middot; ${esc(a.file||'')}</div>${/^Ifc/.test(a.cls||'')?`<div class="small">${esc(a.cls)}</div>`:''}
   <p class="small">From the architect's model: shown for checking, never built. A note here goes to the consultant with the others.</p>`;
  if(!window.c2rNotes)return;
  window.c2rNotes.panel(null,{key:'arch:'+a.model+':'+a.el,at:a.at,sheet:'',id:noteId||undefined,
   match:n=>n.arch&&n.arch.model===a.model&&n.arch.el===a.el,title:[a.name||a.kind,a.kind].filter(Boolean).join('  ·  '),
   note:{type:'architecture',mark:a.name||a.kind,lv:a.fl,plan:a.file,at:a.at,set:setKey(),arch:{model:a.model,el:a.el,kind:a.kind,name:a.name,fl:a.fl,file:a.file}},
   after:()=>showArch(a,null)})}
 window.c2rArchNote=function(n){if(n.view)goView(n.view);
  if(n.type==='3D view'){if(typeof viewNote==='function')viewNote(n.id);return}
  const a=n.arch||{};showArch({...a,at:n.at},n.id)};
 // ------------------------------------------------------------ the view a note keeps
 function view(){return {set:setKey(),cam:{x:tgt.x,y:tgt.y,z:tgt.z,d:dist,t:theta,p:phi},types:{...shown3},lv:focusLv||null,
   box:BOX.on?{a:BOX.b.min.toArray().map(v=>+v.toFixed(3)),b:BOX.b.max.toArray().map(v=>+v.toFixed(3))}:null,
   arch:AR.models.map(m=>({id:m.id,on:!!m.show,o:m.opacity}))}}
 function goView(v){if(!v)return;if(mode!=='3d')setMode('3d');if(mode!=='3d')return;
  const i=D3.findIndex(s=>s.set===v.set);if(i>=0&&i!==cur3)build3(i);
  if(v.types)for(const t in v.types){shown3[t]=!!v.types[t];const c=document.querySelector(`#d3types input[data-t="${t}"]`);if(c)c.checked=shown3[t]}
  focusLv=v.lv||null;
  for(const x of v.arch||[]){const m=AR.models.find(y=>y.id===x.id);if(m){m.show=x.on;m.opacity=x.o;if(m.show)ensure(m);place(m)}}
  if(v.box){BOX.b.min.fromArray(v.box.a);BOX.b.max.fromArray(v.box.b);boxOn(true,true)}else boxOn(false,true);
  if(v.cam){tgt.x=v.cam.x;tgt.y=v.cam.y;tgt.z=v.cam.z;dist=v.cam.d;theta=v.cam.t;phi=v.cam.p;place3()}paint3();
  if($('arPanel').style.display==='block')list()}
 window.c2rView=view;window.c2rGoView=goView;
 // ------------------------------------------------------------ the section box
 function clip(){const pl=BOX.on?BOX.planes:null;
  for(const m of meshes){if(m.material.clippingPlanes!==pl){m.material.clippingPlanes=pl;m.material.needsUpdate=true}}
  for(const id in AR.live){const L=AR.live[id];if(L.mats)L.mats.forEach(x=>{if(x.clippingPlanes!==pl){x.clippingPlanes=pl;x.needsUpdate=true}})}
  caps()}
 // the cut faces filled, as Revit shows a section: each solid's inside faces drawn flat in a darker
 // shade - seen only through the cut (a closed solid hides them everywhere else)
 const CAPS=new Map();
 function caps(){for(const [m,c] of CAPS)if(!meshes.includes(m)||!BOX.on){if(c.parent)c.parent.remove(c);c.material.dispose();CAPS.delete(m)}
  if(BOX.on)for(const m of meshes){if(CAPS.has(m)||m.material.side===THREE.DoubleSide)continue;
   const c=new THREE.Mesh(m.geometry,new THREE.MeshBasicMaterial({side:THREE.BackSide,clippingPlanes:BOX.planes,transparent:true}));
   c.userData=m.userData;m.add(c);CAPS.set(m,c)}
  capPaint()}
 function capPaint(){for(const [m,c] of CAPS){const on=sel&&sel.id===m.userData.id;
  c.visible=m.visible&&m.material.opacity>=0.99;c.material.color.copy(m.material.color).multiplyScalar(on?0.8:0.6);
  if(on)c.material.color.lerp(new THREE.Color(0x1f6fb2),0.5)}}
 const paint0=paint3;paint3=function(){paint0();if(CAPS.size)capPaint()};
 function boxSync(){const a=BOX.b.min,b=BOX.b.max,P=BOX.planes;
  P[0].set(new THREE.Vector3(1,0,0),-a.x);P[1].set(new THREE.Vector3(-1,0,0),b.x);P[2].set(new THREE.Vector3(0,1,0),-a.y);
  P[3].set(new THREE.Vector3(0,-1,0),b.y);P[4].set(new THREE.Vector3(0,0,1),-a.z);P[5].set(new THREE.Vector3(0,0,-1),b.z);
  const c=BOX.b.getCenter(new THREE.Vector3());
  const at=[[a.x,c.y,c.z],[b.x,c.y,c.z],[c.x,a.y,c.z],[c.x,b.y,c.z],[c.x,c.y,a.z],[c.x,c.y,b.z]];
  BOX.handles.forEach((h,k)=>{h.position.set(...at[k]);h.visible=BOX.on&&BOX.show});BOX.helper.visible=BOX.on}
 // a note's screenshot: the box's handles and the gumball left out (they are tools, not the building)
 window.c2rShot=function(on){if(on){AR.shotWas={h:BOX.handles.map(h=>h.visible),g:GB.g?GB.g.visible:false};
   BOX.handles.forEach(h=>h.visible=false);if(GB.g)GB.g.visible=false}
  else if(AR.shotWas){BOX.handles.forEach((h,k)=>h.visible=AR.shotWas.h[k]);if(GB.g)GB.g.visible=AR.shotWas.g;AR.shotWas=null}};
 function boxOn(on,quiet){if(!AR.ready)return;if(on&&BOX.b.isEmpty())fitWhole(true);BOX.on=on;BOX.set=cur3;boxSync();clip();if(!quiet)sbPanel();else sbPanel()}
 function fitBox(b,pad){BOX.b.copy(b);BOX.b.expandByScalar(pad||0);BOX.set=cur3;boxSync()}
 function fitWhole(silent){grp.updateMatrixWorld(true);const b=new THREE.Box3().setFromObject(grp);if(!b.isEmpty())fitBox(b,0.3);if(!silent)sbPanel()}
 function fitFloor(i){const L=D3[cur3].levels;if(!L[i])return;grp.updateMatrixWorld(true);const b=new THREE.Box3().setFromObject(grp);
  const top=L[i].z/1000,bot=i>0?L[i-1].z/1000:top-3;b.min.y=bot+0.05;b.max.y=top+0.3;fitBox(b,0);b.min.x-=0.3;b.min.z-=0.3;b.max.x+=0.3;b.max.z+=0.3;BOX.b.copy(b);boxSync()}
 function fitSel(){if(!sel){alert('Click an element first, then fit the box round it.');return}
  const b=new THREE.Box3();meshes.filter(m=>m.userData.id===sel.id).forEach(m=>b.expandByObject(m));if(b.isEmpty()){alert('The selected element is not in this 3D view.');return}fitBox(b,1.5)}
 function dragTo(ev){const d=BOX.drag,ray=rayAt(ev).ray,ax=['x','x','y','y','z','z'][d.k],u=new THREE.Vector3();u[ax]=1;
  const P=BOX.handles[d.k].position.clone(),w0=P.clone().sub(ray.origin),b=u.dot(ray.direction),dd=u.dot(w0),e=ray.direction.dot(w0),den=1-b*b;
  if(Math.abs(den)<1e-6)return;const t=(b*e-dd)/den,v=P[ax]+t,gap=0.2;
  if(d.k%2===0)BOX.b.min[ax]=Math.min(v,BOX.b.max[ax]-gap);else BOX.b.max[ax]=Math.max(v,BOX.b.min[ax]+gap);boxSync()}
 function sbPanel(){const p=$('sbPanel');if(p.style.display!=='block')return;const L=D3[cur3]?D3[cur3].levels:[];
  p.innerHTML=`<h4>Section box <button id="sbClose" title="Close">&times;</button></h4>
  <label><input type="checkbox" id="sbOn" ${BOX.on?'checked':''}> Cut the 3D view to the box</label>
  <div class="ar-row">Fit to <button id="sbAll">Whole model</button><button id="sbSel">Selected element</button></div>
  <div class="ar-row">Floor <select id="sbLv" style="width:auto">${L.map((l,i)=>`<option value="${i}"${i===BOX.lv?' selected':''}>${esc(l.key)} (${(l.z/1000).toFixed(2)} m)</option>`).join('')}</select><button id="sbFl">Fit</button></div>
  <div class="ar-row">Handles <input type="range" id="sbHs" min="1" max="10" value="${BOX.hs}" style="flex:1"> <label><input type="checkbox" id="sbHv" ${BOX.show?'checked':''}> show</label></div>
  <p class="ar-mute">Drag the blue handles to move each side of the box. Hidden parts cannot be clicked. A note's screenshot shows the view as cut, without the handles.</p>`;
  $('sbClose').onclick=()=>toggle('sbPanel','sbBtn');$('sbOn').onchange=ev=>boxOn(ev.target.checked);
  const keepH=()=>{try{localStorage.setItem('c2r_box_handles',JSON.stringify({hs:BOX.hs,show:BOX.show}))}catch(e){}};
  $('sbHs').oninput=ev=>{BOX.hs=+ev.target.value;keepH()};$('sbHv').onchange=ev=>{BOX.show=ev.target.checked;boxSync();keepH()};
  $('sbAll').onclick=()=>{fitWhole();boxOn(true)};$('sbSel').onclick=()=>{fitSel();if(!BOX.b.isEmpty())boxOn(true)};
  $('sbFl').onclick=()=>{BOX.lv=+$('sbLv').value;fitFloor(BOX.lv);boxOn(true)}}
 // ------------------------------------------------------------ the plan: the architect's model cut through
 // As an architect's floor plan: the models lined up with this drawing set, cut level with the plan's
 // level + the cut height (1.2 m, as Revit), drawn as lines under the structure. Display only.
 const PLAN={on:true,cut:1200,cache:{},busy:false,raw:{},lvFor:{}};
 try{const s=JSON.parse(localStorage.getItem('c2r_arch_plan')||'null');if(s){PLAN.on=!!s.on;if(isFinite(s.cut))PLAN.cut=+s.cut}}catch(e){}
 (function(){const l=document.createElement('label');l.id='arPlanL';l.style.display='none';l.title="The architect's model (lined up in 3D) cut through above this level, as a floor plan";
  l.innerHTML=`<input type="checkbox" id="arPlan"${PLAN.on?' checked':''}> Architecture <span id="arPlanCut" class="small">cut <input type="text" id="arCut" value="${PLAN.cut}" style="width:46px"> mm up</span> <span class="small" style="display:none" title="This plan's level is not known: the level to cut the architect's model at">at <select id="arPlanLv" style="width:auto;padding:0 2px"></select></span> <span id="arPlanMsg" class="small" style="color:#a36400"></span>`;
  const after=($('cadtx')||$('cadon')).parentElement;after.after(l);
  const keep=()=>{try{localStorage.setItem('c2r_arch_plan',JSON.stringify({on:PLAN.on,cut:PLAN.cut}))}catch(e){}};
  $('arPlan').onchange=ev=>{PLAN.on=ev.target.checked;keep();draw()};
  $('arCut').onchange=ev=>{const v=parseFloat(ev.target.value);if(isFinite(v)){PLAN.cut=v;keep();draw()}else ev.target.value=PLAN.cut};
  $('arPlanLv').onchange=ev=>{if(cur){PLAN.lvFor[cur.id]=ev.target.value;draw()}}})();
 function planSoon(){const l=$('arPlanL');if(l)l.style.display=AR.models.length?'':'none';if(mode!=='3d'&&PLAN.on)draw();tbSync()}
 // a plan's sheet -> its drawing set's frame (the 3D view's): the shift its columns were moved by
 function sheetShift(s){if(s._arSh!==undefined)return s._arSh;const S=D3.find(x=>x.set===s.set);let out=null;
  if(S){const dx=[],dy=[];for(const it of S.items){if(it.t!=='column')continue;const e=byId[it.id];if(!e||e.sheet!==s.id||!e.g.p.length)continue;
    let x=0,y=0;e.g.p.forEach(q=>{x+=q[0];y+=q[1]});x/=e.g.p.length;y/=e.g.p.length;dx.push(it.x-x);dy.push(it.y-y)}
   if(dx.length){dx.sort((a,b)=>a-b);dy.sort((a,b)=>a-b);out=[dx[dx.length>>1],dy[dy.length>>1]]}
   // a plan not in the 3D view (no level): placed by its own columns on the 3D view's - the shift
   // most column pairs agree on (100 mm steps), only when clearly the best
   if(!out){const pc=s.els.filter(e=>e.t==='column'&&e.g.p.length).map(e=>{let x=0,y=0;e.g.p.forEach(q=>{x+=q[0];y+=q[1]});return [x/e.g.p.length,y/e.g.p.length]});
    const sc=S.items.filter(it=>it.t==='column').map(it=>[it.x,it.y]),votes=new Map();
    if(pc.length>=3&&sc.length>=3&&pc.length*sc.length<=200000){for(const p of pc)for(const q of sc){const k=Math.round((q[0]-p[0])/100)+','+Math.round((q[1]-p[1])/100);votes.set(k,(votes.get(k)||0)+1)}
     // a shift near a 100 mm step splits its votes between neighbours: counted together; the
     // runner-up is the best shift well away from it
     const K=[...votes.keys()].map(k=>k.split(',').map(Number)),near=(a,b,r)=>Math.abs(a[0]-b[0])<=r&&Math.abs(a[1]-b[1])<=r;
     const sum=k=>K.reduce((s,q)=>s+(near(q,k,1)?votes.get(q[0]+','+q[1]):0),0);
     const top=[...votes.entries()].sort((a,b)=>b[1]-a[1]).slice(0,40).map(([k])=>{const q=k.split(',').map(Number);return [k,sum(q),q]}).sort((a,b)=>b[1]-a[1]);
     const rival=top.find(t=>!near(t[2],top[0][2],2));
     if(top.length&&top[0][1]>=3&&(!rival||top[0][1]>=1.5*rival[1])){const [kx,ky]=top[0][2];
      const dx2=[],dy2=[];for(const p of pc)for(const q of sc){const a=q[0]-p[0],b=q[1]-p[1];if(Math.abs(a-kx*100)<=150&&Math.abs(b-ky*100)<=150){dx2.push(a);dy2.push(b)}}
      dx2.sort((a,b)=>a-b);dy2.sort((a,b)=>a-b);out=[dx2[dx2.length>>1],dy2[dy2.length>>1]]}}}}
  s._arSh=out;return out}
 function planLines(s,m,meta,buf,al,z0,sh){const hid=hiddenOf(m,{meta}),G=meta.groups,F=meta.storeys||[],o=meta.origin,u=m.unit_mm||meta.unit_mm||1,yUp=(m.up||meta.up)==='y';
  const c=Math.cos(al.rot*Math.PI/180),sn=Math.sin(al.rot*Math.PI/180),zc=z0+0.37,pb=meta.nverts*12,out=[];
  for(const ch of meta.chunks){if(hid.includes(G[ch.g])||(ch.s>=0&&F[ch.s]&&hid.includes('floor:'+F[ch.s].name)))continue;
   const P3=new Float32Array(buf,ch.v0*12,ch.vn*3),I=new Uint32Array(buf,pb+ch.i0*4,ch.in),n=ch.vn,X=new Float64Array(n),Y=new Float64Array(n),Z=new Float64Array(n);
   let lo=1e18,hi=-1e18;
   for(let i=0;i<n;i++){const fx=(P3[3*i]+o[0])*u,fy=(P3[3*i+1]+o[1])*u,fz=(P3[3*i+2]+o[2])*u,ax=fx,ay=yUp?-fz:fy,az=yUp?fy:fz;
    X[i]=c*ax-sn*ay+al.dx-sh[0];Y[i]=sn*ax+c*ay+al.dy-sh[1];Z[i]=az+(al.dz||0);if(Z[i]<lo)lo=Z[i];if(Z[i]>hi)hi=Z[i]}
   if(lo>zc||hi<zc)continue;
   for(let t=0;t<I.length;t+=3){const a=I[t],b=I[t+1],d=I[t+2],za=Z[a]-zc,zb=Z[b]-zc,zd=Z[d]-zc;
    if((za>0&&zb>0&&zd>0)||(za<0&&zb<0&&zd<0))continue;const pts=[];
    for(const [p,q,zp,zq] of [[a,b,za,zb],[b,d,zb,zd],[d,a,zd,za]])if((zp<0)!==(zq<0)){const k=zp/(zp-zq);pts.push([X[p]+(X[q]-X[p])*k,Y[p]+(Y[q]-Y[p])*k])}
    if(pts.length===2)out.push(pts)}}
  return out}
 function planSvg(){const msg=$('arPlanMsg'),cut=$('arPlanCut');if(cut)cut.style.display=PLAN.on?'':'none';if(msg)msg.textContent='';
  $('arPlanL').style.display=AR.models.length?'':'none';      // the tick only once a model was added
  $('arPlanLv').parentElement.style.display='none';             // shown below for a plan with no level
  if(!PLAN.on||!cur||!AR.models.length)return '';
  const ready=AR.models.filter(m=>m.state==='ready'&&m.show);if(!ready.length){msg.textContent='(no model shown)';return ''}
  const S=D3.find(x=>x.set===cur.set);if(!S){msg.textContent='(this plan is not in the 3D view)';return ''}
  // a plan with no level: cut at the level chosen beside the tick (the lowest unless chosen)
  let lv=S.levels.find(l=>l.key===cur.lv);const lvSel=$('arPlanLv');
  if(!lv){lv=S.levels.find(l=>l.key===PLAN.lvFor[cur.id])||S.levels[0];
   lvSel.innerHTML=S.levels.map(l=>`<option value="${esc(l.key)}"${l===lv?' selected':''}>${esc(l.key)}</option>`).join('');lvSel.parentElement.style.display=''}
  else lvSel.parentElement.style.display='none';
  const sh=sheetShift(cur);
  if(!lv){msg.textContent='(the drawings give no level)';return ''}if(!sh){msg.textContent='(this plan does not line up with the 3D view)';return ''}
  let html='',waiting=false,rough=false;
  for(const m of ready){let al=AR.prop[m.id+'|'+cur.set]||(m.align||{})[cur.set],d='';     // being moved: where it is now
   if(!al){// not lined up yet: shown roughly over the structure, as in 3D - said beside the tick
    const R=PLAN.raw[m.id];if(!R){waiting=true;getRaw(m).then(x=>{PLAN.raw[m.id]={meta:x.meta};if(mode!=='3d')draw()}).catch(()=>{});continue}
    const s=sBounds(S),c=roughMid(m,R);al={rot:0,dx:s.x-c[0],dy:s.y-c[1],dz:s.z-c[2]};rough=true}
   const key=[m.id,cur.id,lv.key,PLAN.cut,JSON.stringify(m.hidden),m.unit_mm,m.up,JSON.stringify(al)].join('|');
   if(PLAN.cache[key]===undefined){waiting=true;if(!PLAN.busy){PLAN.busy=true;
     getRaw(m).then(({meta,buf})=>{PLAN.cache[key]=planLines(cur,m,meta,buf,al,lv.z+PLAN.cut,sh)}).catch(()=>{PLAN.cache[key]=[]})
      .finally(()=>{PLAN.busy=false;if(mode!=='3d')draw()})}continue}
   for(const [a,b] of PLAN.cache[key]){const p=P(a),q=P(b);d+=`M${p[0]|0} ${-p[1]|0}L${q[0]|0} ${-q[1]|0}`}
   if(d)html+=`<path data-am="${m.id}" d="${d}" fill="none" stroke="#3d5a80" stroke-opacity=".85" stroke-width="1.3" vector-effect="non-scaling-stroke" pointer-events="none"/>`}
  if(msg)msg.textContent=waiting?'(cutting the model ...)':rough?'(not lined up yet - shown roughly; line it up: Move in the toolbox below, then Pin)':
   (html?'':'(nothing of the model at this height - try another cut height)');
  return html}
 const drawP=draw;draw=function(){drawP();if(!cur||!vb)return;const h=planSvg();if(h)$('svg').insertAdjacentHTML('afterbegin',h);
  const ms=msSvg();if(ms)$('svg').insertAdjacentHTML('beforeend',ms)};
 // ------------------------------------------------------------ the toolbox (bottom of the view, Plan and 3D)
 // Select, Measure, Zoom all; 3D: Top / Front / Side / 3D views, Section box, Note on this view;
 // the architect's model: shown, opacity (3D), Move (Plan and 3D) and Pin. Display only.
 const TOOL={cur:'select'};
 (function(){const st=document.createElement('style');st.textContent=`
#tbar{position:absolute;left:50%;bottom:10px;transform:translateX(-50%);display:flex;gap:4px;align-items:center;flex-wrap:wrap;justify-content:center;
 background:#fff;border:1px solid var(--line);border-radius:9px;box-shadow:0 4px 16px rgba(0,0,0,.14);padding:5px 8px;z-index:6;font-size:12px;max-width:calc(100% - 20px)}
#tbar button{padding:2px 7px}#tbar button.on{background:var(--ink);color:#fff;border-color:var(--ink)}#tbar button:disabled{opacity:.4;cursor:default}
#tbar .sep{width:1px;height:20px;background:var(--line);margin:0 3px}#tbar select{width:auto;padding:2px}#tbar input[type=range]{width:70px}
#d3note{bottom:62px!important}`;document.head.appendChild(st);
  $('canvas').insertAdjacentHTML('beforeend',`<div id="tbar">
  <button data-t="select" class="on" title="Click an element to see where its values come from">Select</button>
  <button data-t="measure" title="Click two points to measure between them (Esc to stop)">Measure</button>
  <button data-t="clear" title="Remove the measurements" style="display:none">Clear</button>
  <span class="sep"></span><button data-t="fit" title="Show the whole model">Zoom all</button>
  <button data-t="snip" title="A picture of this view (the selected element framed): crop it, mark it up, save it on this PC">Snip</button>
  <span class="t3"><button data-t="v-top" title="Look down from above">Top</button><button data-t="v-front" title="Look from the front (the drawing's bottom edge)">Front</button><button data-t="v-back" title="Look from the back (the drawing's top edge)">Back</button><button data-t="v-left" title="Look from the left (the drawing's left edge)">Left</button><button data-t="v-right" title="Look from the right (the drawing's right edge)">Right</button><button data-t="v-iso" title="The 3D view">3D</button></span>
  <span class="sep t3"></span><span class="t3"><button data-t="box" title="Cut the 3D view to a box">Section box</button><button data-t="boxmenu" title="Fit the box, its handles">&#9662;</button></span>
  <span class="sep" id="tbArchSep"></span><span id="tbArch"><label title="Show the architect's model"><input type="checkbox" id="tbOn"> Architecture</label>
  <select id="tbModel"></select><input type="range" id="tbOp" min="5" max="100" title="Opacity of the architect's model (3D)">
  <button data-t="move" title="Move the architect's model by hand (Plan: drag its lines; 3D: the gumball)">Move</button><button data-t="pin" title="Save where it is and lock it">Pin</button>
  <button data-t="archmenu" title="The architect's model: add, line up, layers">&#9662;</button></span>
  <span class="sep t3"></span><span class="t3"><button data-t="viewnote" title="A note for the consultant on what this view shows">Note on this view</button></span></div>`);
  $('tbar').addEventListener('click',ev=>{const b=ev.target.closest('button');if(!b||b.disabled)return;tbDo(b.dataset.t)});
  $('tbOn').onchange=ev=>{const m=tbModel();if(!m)return;m.show=ev.target.checked;if(m.show&&AR.ready)ensure(m);place(m);save(m,{show:m.show});if(mode!=='3d')draw();list()};
  $('tbOp').oninput=ev=>{const m=tbModel();if(!m)return;m.opacity=ev.target.value/100;place(m)};
  $('tbOp').onchange=ev=>{const m=tbModel();if(m){save(m,{opacity:m.opacity});list()}};
  $('tbModel').onchange=()=>tbSync();
  // kept where it can be seen: the view can run below the window (a tall header, a small screen)
  const keepIn=()=>{const r=$('canvas').getBoundingClientRect(),over=Math.max(0,r.bottom-innerHeight);$('tbar').style.bottom=(10+over)+'px'};
  addEventListener('resize',keepIn);addEventListener('scroll',keepIn,true);setInterval(keepIn,1000);keepIn()})();
 function tbModel(){const v=$('tbModel').value,ok=AR.models.filter(m=>m.state==='ready');return ok.find(m=>m.id===v)||ok.find(m=>m.show)||ok[0]||null}
 function tbSync(){const in3=mode==='3d',bar=$('tbar');if(!bar)return;
  if(AR.move)TOOL.cur='move';else if(TOOL.cur==='move')TOOL.cur='select';
  bar.querySelectorAll('.t3 button').forEach(b=>{b.disabled=!in3;b.title=b.title.replace(/ \(3D view only\)$/,'')+(in3?'':' (3D view only)')});
  bar.querySelectorAll('[data-t]').forEach(b=>b.classList.toggle('on',b.dataset.t===TOOL.cur||(b.dataset.t==='box'&&BOX.on)));
  bar.querySelector('[data-t=clear]').style.display=(MS.list.length||MS.d3.length)?'':'none';
  const ok=AR.models.filter(m=>m.state==='ready');$('tbArch').style.display=$('tbArchSep').style.display=ok.length?'':'none';
  const sel_=$('tbModel'),cur_=sel_.value;sel_.innerHTML=ok.map(m=>`<option value="${m.id}">${esc(m.name.length>24?m.name.slice(0,22)+'...':m.name)}</option>`).join('');
  const first=ok.find(m=>m.show)||ok[0];      // the model shown, not one switched off
  sel_.value=ok.some(m=>m.id===cur_)?cur_:(first?first.id:'');sel_.style.display=ok.length>1?'':'none';
  const m=tbModel();if(m){$('tbOn').checked=!!m.show;$('tbOp').value=Math.round(m.opacity*100);$('tbOp').style.display=in3?'':'none';
   bar.querySelector('[data-t=pin]').disabled=!AR.prop[m.id+'|'+(in3?setKey():(cur&&cur.set))]}}
 function tbDo(t){const in3=mode==='3d';
  if(t==='select'||t==='measure'){if(AR.move)moveStop();TOOL.cur=t;MS.pending=null;if(t==='measure')banner(in3?'Measure: click two points on the model &middot; Esc to stop':'Measure: click two points on the plan (it snaps to column centres and line ends) &middot; Esc to stop');else if(!AR.move)banner('');draw3or2()}
  else if(t==='clear'){msClear()}
  else if(t==='fit'){if(in3)fit3();else if(cur){vb=bounds(cur);draw()}}
  else if(t.startsWith('v-')){// the camera on that side of the model, level, looking at it (as the plan is shown)
   const V={'v-top':[Math.PI/2,0.05],'v-front':[Math.PI/2,Math.PI/2],'v-back':[-Math.PI/2,Math.PI/2],'v-left':[Math.PI,Math.PI/2],
    'v-right':[0,Math.PI/2],'v-iso':[1.1,0.9]}[t];theta=V[0];phi=V[1];fit3()}
  else if(t==='box'){if(!AR.ready)return;boxOn(!BOX.on)}
  else if(t==='boxmenu'){toggle('sbPanel','sbBtn')}
  else if(t==='archmenu'){if(!in3){setMode('3d')}if($('arPanel').style.display!=='block')toggle('arPanel','arBtn')}
  else if(t==='move'){const m=tbModel();if(!m)return;if(AR.move){moveStop();list()}else{TOOL.cur='move';MS.pending=null;if(in3)moveStart(m);else moveStartPlan(m)}}
  else if(t==='pin'){const m=tbModel();if(m)pinModel(m)}
  else if(t==='viewnote'){viewNote()}
  else if(t==='snip'){if(window.c2rSnip)window.c2rSnip()}
  tbSync()}
 function draw3or2(){if(mode!=='3d')draw()}
 function fit3(){if(!grp)return;grp.updateMatrixWorld(true);const bb=new THREE.Box3().setFromObject(grp);if(bb.isEmpty())return;
  const c=bb.getCenter(new THREE.Vector3());tgt.x=c.x;tgt.y=c.y;tgt.z=c.z;dist=(bb.getSize(new THREE.Vector3()).length()||10)*1.1;place3()}
 function pinModel(m){const k=mode==='3d'?setKey():cur.set,key=m.id+'|'+k,p=AR.prop[key];if(!p)return;moveStop();
  save(m,{set:k,align:{rot:p.rot,dx:p.dx,dy:p.dy,dz:p.dz||0,how:p.how||'moved by hand',pinned:true}}).then(()=>{delete AR.prop[key];place(m);list();tbSync();if(mode!=='3d')draw();
   banner('');setTimeout(()=>banner(''),10)})}
 // moving the architect's model in the plan: drag its lines (they follow at once; cut again on letting go)
 function moveStartPlan(m){if(!cur)return;const S=D3.find(x=>x.set===cur.set),key=m.id+'|'+cur.set;if(!S)return;
  if(!m.show){m.show=true;save(m,{show:true})}
  let al=AR.prop[key]||(m.align||{})[cur.set];
  if(!al){const R=PLAN.raw[m.id];if(!R){getRaw(m).then(x=>{PLAN.raw[m.id]={meta:x.meta};moveStartPlan(m)}).catch(()=>{});return}
   const s=sBounds(S),c=roughMid(m,R);al={rot:0,dx:s.x-c[0],dy:s.y-c[1],dz:s.z-c[2]}}
  AR.prop[key]={...(AR.prop[key]||{}),rot:al.rot||0,dx:al.dx,dy:al.dy,dz:al.dz||0,ok:true,edit:true,how:'moved by hand',
   text:'Moved by hand. Then Pin.'};AR.move={id:m.id,key};TOOL.cur='move';
  banner(`Moving ${esc(m.name)} in the plan: drag to move &middot; arrow keys 100 mm (Shift: 1 m) &middot; [ ] turn 0.5&deg; &middot; right-drag pans &middot; then Pin &middot; Esc stops`);draw();tbSync()}
 function planPt(e){const s=$('svg'),pt=s.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;const q=pt.matrixTransform(s.getScreenCTM().inverse()),Y=-q.y;
  return [q.x*ca+Y*sa,-q.x*sa+Y*ca]}
 const MV2={d:null};
 $('svg').addEventListener('mousedown',e=>{MS.down=[e.clientX,e.clientY];
  if(mode==='3d'||!AR.move||e.button!==0)return;const p=AR.prop[AR.move.key];if(!p)return;
  MV2.d={p0:planPt(e),al0:{rot:p.rot,dx:p.dx,dy:p.dy},path:$('svg').querySelector(`path[data-am="${AR.move.id}"]`),k:[0,0]};
  e.stopImmediatePropagation();e.preventDefault()},true);
 addEventListener('mousemove',e=>{const d=MV2.d;if(!d)return;const p1=planPt(e);d.k=[p1[0]-d.p0[0],p1[1]-d.p0[1]];
  const D=P(d.k);if(d.path)d.path.setAttribute('transform',`translate(${D[0]} ${-D[1]})`)});
 addEventListener('mouseup',()=>{const d=MV2.d;if(!d)return;MV2.d=null;if(Math.hypot(...d.k)<1)return;
  moveTo({rot:d.al0.rot,dx:d.al0.dx+d.k[0],dy:d.al0.dy+d.k[1]})});
 // ------------------------------------------------------------ measuring
 const MS={list:[],d3:[],pending:null,down:null};
 $('svg').addEventListener('click',e=>{if(MS.dragged){e.stopImmediatePropagation();MS.dragged=false;return}
  if(TOOL.cur!=='measure'||mode==='3d')return;e.stopImmediatePropagation();e.preventDefault();
  if(MS.down&&Math.hypot(e.clientX-MS.down[0],e.clientY-MS.down[1])>4)return;const p=snapPlan(e);
  if(!MS.pending){MS.pending={sheet:cur.id,a:p}}else{MS.list.push({sheet:cur.id,a:MS.pending.a,b:p});MS.pending=null}draw();tbSync()},true);
 function snapPlan(e){const p=planPt(e),s=$('svg'),ctm=s.getScreenCTM(),pt=s.createSVGPoint();let best=p,bd=12;
  const scr=q=>{const d=P(q);pt.x=d[0];pt.y=-d[1];const r=pt.matrixTransform(ctm);return Math.hypot(r.x-e.clientX,r.y-e.clientY)};
  for(const el of cur.els){const g=el.g.p;if(!g||!g.length)continue;const cand=g.map(q=>[q,0]);
   // a column's centre wins over the beam ends that meet at it
   if(el.g.k==='poly'){let x=0,y=0;g.forEach(q=>{x+=q[0];y+=q[1]});cand.push([[x/g.length,y/g.length],el.t==='column'?8:0])}
   for(const [q,bonus] of cand){const d=scr(q)-bonus;if(d<bd){bd=d;best=[q[0],q[1]]}}}
  return best}
 // the same size on screen at any zoom (redrawn after the wheel); each end a handle to drag
 function msSvg(){if(!cur)return '';const pend=MS.pending&&MS.pending.sheet===cur.id?MS.pending:null;
  if(!MS.list.some(x=>x.sheet===cur.id)&&!pend)return '';
  const px=vb[2]/Math.max($('svg').clientWidth||1,1),fs=13*px;let h='<g id="msG" font-family="Arial,sans-serif">';
  MS.list.forEach((m,i)=>{if(m.sheet!==cur.id)return;const a=P(m.a),b=P(m.b),L=Math.hypot(m.b[0]-m.a[0],m.b[1]-m.a[1]),mx=(a[0]+b[0])/2,my=-(a[1]+b[1])/2;
   h+=`<line pointer-events="none" x1="${a[0]}" y1="${-a[1]}" x2="${b[0]}" y2="${-b[1]}" stroke="#e0218a" stroke-width="2" vector-effect="non-scaling-stroke"/>`+
    [a,b].map((q,k)=>`<circle data-ms="${i}" data-end="${k?'b':'a'}" cx="${q[0]}" cy="${-q[1]}" r="${5*px}" fill="#e0218a" stroke="#fff" stroke-width="${1.5*px}" style="cursor:move"><title>Drag to move this end</title></circle>`).join('')+
    `<text pointer-events="none" x="${mx}" y="${my-fs*0.5}" font-size="${fs}" text-anchor="middle" fill="#e0218a" stroke="#fff" stroke-width="${fs/4}" paint-order="stroke" font-weight="700">${(L/1000).toFixed(3)} m</text>`});
  if(pend){const a=P(pend.a);h+=`<circle pointer-events="none" cx="${a[0]}" cy="${-a[1]}" r="${6*px}" fill="none" stroke="#e0218a" stroke-width="2" vector-effect="non-scaling-stroke"/>`}
  return h+'</g>'}
 function msRedraw(){const g=$('msG');if(g)g.remove();const h=msSvg();if(h)$('svg').insertAdjacentHTML('beforeend',h)}
 $('svg').addEventListener('wheel',()=>{if(MS.list.length||MS.pending)requestAnimationFrame(msRedraw)});
 // dragging a measurement's end in the plan (snapping as when it was put)
 const MSD={d:null};
 $('svg').addEventListener('mousedown',e=>{const t=e.target;if(mode==='3d'||e.button!==0||!t.dataset||t.dataset.ms===undefined)return;
  MSD.d={i:+t.dataset.ms,end:t.dataset.end};MS.dragged=true;e.stopImmediatePropagation();e.preventDefault()},true);
 addEventListener('mousemove',e=>{if(!MSD.d)return;const m=MS.list[MSD.d.i];if(!m)return;m[MSD.d.end]=snapPlan(e);msRedraw()});
 addEventListener('mouseup',()=>{if(MSD.d){MSD.d=null;setTimeout(()=>{MS.dragged=false},0)}});
 function m3Click(ray){const h=structHit(ray)||archHit(ray);if(!h)return;const p=h.point.clone();
  if(!MS.pending3){MS.pending3={p,dot:m3Dot(p)};return}
  const a=MS.pending3.p,g=new THREE.Group(),d0=MS.pending3.dot,d1=m3Dot(p);g.add(d0,d1);MS.pending3=null;
  // drawn on top of everything: in the see-through pass (the structure is drawn see-through, after
  // anything solid), last, never hidden by depth; a bar the same thickness on screen at any zoom
  const ln=new THREE.Mesh(new THREE.CylinderGeometry(1,1,1,8),new THREE.MeshBasicMaterial({color:0xe0218a,depthTest:false,depthWrite:false,transparent:true}));
  ln.renderOrder=30;ln.onBeforeRender=()=>{const u=g.userData,r=Math.max(cam.position.distanceTo(ln.position),0.5)*0.0018;ln.scale.set(r,u.a.distanceTo(u.b),r)};g.add(ln);
  d0.userData={ms3:g,end:'a'};d1.userData={ms3:g,end:'b'};g.userData={a:a.clone(),b:p.clone(),dots:[d0,d1],line:ln,sprite:null};
  s3.add(g);MS.d3.push(g);m3Update(g);tbSync()}
 // a 3D measurement drawn again from its two ends (after one was dragged)
 function m3Update(g){const u=g.userData,a=u.a,b=u.b;u.dots[0].position.copy(a);u.dots[1].position.copy(b);
  u.line.position.copy(a).add(b).multiplyScalar(0.5);
  const dir=b.clone().sub(a);if(dir.lengthSq()>1e-12)u.line.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),dir.normalize());
  const sa_=toSetF(a),sb_=toSetF(b),dx=sb_[0]-sa_[0],dy=sb_[1]-sa_[1],dz=(b.y-a.y)*1000,L=Math.hypot(dx,dy,dz);
  if(u.sprite){g.remove(u.sprite);u.sprite.material.map.dispose();u.sprite.material.dispose()}
  u.sprite=m3Label(`${(L/1000).toFixed(3)} m`,`across ${(Math.hypot(dx,dy)/1000).toFixed(3)} m  up ${(dz/1000).toFixed(3)} m`);
  u.sprite.position.copy(a).add(b).multiplyScalar(0.5);g.add(u.sprite)}
 addEventListener('pointermove',ev=>{const d=MS.drag3;if(!d)return;const ray=rayAt(ev),h=structHit(ray)||archHit(ray);if(!h)return;
  d.g.userData[d.end]=h.point.clone();m3Update(d.g)});
 addEventListener('pointerup',ev=>{if(MS.drag3){MS.drag3=null;AR.down=null;ev.stopPropagation()}},true);
 function toSetF(p){const l=grp.worldToLocal(p.clone());return [l.x*1000,-l.z*1000]}
 function m3Dot(p){const d=new THREE.Mesh(new THREE.SphereGeometry(1,12,8),new THREE.MeshBasicMaterial({color:0xe0218a,depthTest:false,depthWrite:false,transparent:true}));
  d.position.copy(p);d.renderOrder=31;d.onBeforeRender=()=>d.scale.setScalar(Math.max(cam.position.distanceTo(d.getWorldPosition(new THREE.Vector3())),0.5)*0.007);s3.add(d);return d}
 function m3Label(t1,t2){const c=document.createElement('canvas');c.width=360;c.height=96;const g=c.getContext('2d');
  g.fillStyle='rgba(255,255,255,.92)';g.fillRect(0,0,360,96);g.strokeStyle='#e0218a';g.lineWidth=3;g.strokeRect(1.5,1.5,357,93);
  g.fillStyle='#e0218a';g.font='bold 40px Arial';g.textAlign='center';g.fillText(t1,180,46);g.fillStyle='#5f6c77';g.font='22px Arial';g.fillText(t2,180,80);
  const s=new THREE.Sprite(new THREE.SpriteMaterial({map:new THREE.CanvasTexture(c),depthTest:false,depthWrite:false,sizeAttenuation:false}));s.scale.set(0.16,0.043,1);s.renderOrder=32;return s}
 function msClear(){MS.list=MS.list.filter(x=>!cur||x.sheet!==cur.id);if(mode==='3d'||!MS.list.length){MS.d3.forEach(g=>s3.remove(g));MS.d3=[];if(MS.pending3){s3.remove(MS.pending3.dot);MS.pending3=null}}
  MS.pending=null;draw3or2();tbSync()}
 document.addEventListener('keydown',ev=>{if(ev.key==='Escape'&&TOOL.cur==='measure'){TOOL.cur='select';MS.pending=null;if(MS.pending3){s3.remove(MS.pending3.dot);MS.pending3=null}banner('');draw3or2();tbSync()}});
 // ------------------------------------------------------------ a note on the view as it is (no element needed)
 function viewNote(noteId){if(mode!=='3d')setMode('3d');if(!window.c2rNotes)return;sel=null;paint3();
  const at=toSet(new THREE.Vector3(tgt.x,tgt.y,tgt.z));
  $('detail').innerHTML=`<h3 style="margin:0 0 4px">3D view <span class="badge" style="background:#7d8a96">view</span></h3>
   <div class="small">${esc(setKey())}${focusLv?' &middot; '+esc(focusLv):''}</div>
   <p class="small">A note on what this view shows: its screenshot and the view itself are kept ("Go to view").</p>`;
  window.c2rNotes.panel(null,{key:noteId?'view:'+noteId:'view:new',id:noteId||undefined,at,sheet:'',title:'3D view',
   note:{type:'3D view',mark:'',lv:focusLv||'',plan:'3D view',at,set:setKey()},after:n=>viewNote(n.id)})}
 load();
 if(mode==='3d'&&typeof r3!=='undefined'&&r3)setup();
})();
</script>"""
