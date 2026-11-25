(() => {
  const $ = (id) => document.getElementById(id);
  const state = { matrix: identity(), step: 0, sessionId: null, archived: false, model: 'phone', busy: false, trail: [], activity: [], toastTimer: null, listening: true, ingestUrl: '', lastSessionRefresh: 0, renderQueued: false, posture: 'PORTRAIT' };
  const canvas = $('model-canvas');
  const ctx = canvas.getContext('2d');

  function identity() { return [[1,0,0],[0,1,0],[0,0,1]]; }
  function multiply(a,b) {
    return a.map((row,r) => row.map((_,c) => row.reduce((sum,value,k) => sum + value*b[k][c], 0)));
  }
  function rotation(axis, deg) {
    const angle=deg*Math.PI/180, c=Math.cos(angle), s=Math.sin(angle);
    if(axis==='X') return [[1,0,0],[0,c,-s],[0,s,c]];
    if(axis==='Y') return [[c,0,s],[0,1,0],[-s,0,c]];
    return [[c,-s,0],[s,c,0],[0,0,1]];
  }
  function compose(angles, order) {
    let q=identity();
    for(const axis of order) q=multiply(q,rotation(axis,angles[axis.toLowerCase()]));
    return q;
  }
  function transposeMultiplyError(q) {
    let sum=0;
    for(let r=0;r<3;r++) for(let c=0;c<3;c++) {
      const d=q.reduce((acc,row,k)=>acc+row[r]*row[c],0)-(r===c?1:0); sum+=d*d;
    }
    return Math.sqrt(sum);
  }
  function det(q) { return q[0][0]*(q[1][1]*q[2][2]-q[1][2]*q[2][1])-q[0][1]*(q[1][0]*q[2][2]-q[1][2]*q[2][0])+q[0][2]*(q[1][0]*q[2][1]-q[1][1]*q[2][0]); }
  function classifyPosture(q, current='PORTRAIT') {
    const threshold = 0.55;
    const ux = q[1][0], uy = q[1][1], uz = q[1][2];
    const ax = Math.abs(ux), ay = Math.abs(uy), az = Math.abs(uz);
    if (ax >= ay && ax >= az && ax >= threshold) return ux < 0 ? 'LANDSCAPE LEFT' : 'LANDSCAPE RIGHT';
    if (ay >= ax && ay >= az && ay >= threshold) return uy > 0 ? 'PORTRAIT' : 'UPSIDE DOWN';
    if (az >= ax && az >= ay && az >= threshold) return uz > 0 ? 'FACE UP' : 'FACE DOWN';
    return current;
  }
  function angles() { return {x:Number($('angle-x').value)||0,y:Number($('angle-y').value)||0,z:Number($('angle-z').value)||0}; }
  function fmt(value,digits=6) { return Number(value).toFixed(digits); }
  function setBusy(value) {
    state.busy=value;
    for(const id of ['apply-button','repair-button','stress-button','demo-button','load-log-button','stop-button','reset-button']) {
      $(id).disabled=value || (state.archived && id!=='reset-button') || (id==='stop-button' && !state.sessionId);
    }
  }
  function toast(message) {
    const el=$('toast'); el.textContent=message; el.classList.add('show'); clearTimeout(state.toastTimer);
    state.toastTimer=setTimeout(()=>el.classList.remove('show'),2600);
  }
  async function requestJSON(url,options={}) {
    const response=await fetch(url,{headers:{'Content-Type':'application/json'},...options});
    const data=await response.json();
    if(!response.ok) throw new Error(data.error || 'The request could not be completed.');
    return data;
  }
  async function startSession(title='Sensor run') {
    if(state.sessionId && !state.archived) return;
    const data=await requestJSON('/api/sessions',{method:'POST',body:JSON.stringify({title})});
    state.sessionId=data.session.id; state.archived=false; state.step=0; state.matrix=identity(); state.trail=[];
    $('session-status').textContent=`SAVING · #${state.sessionId}`;
    $('run-status').textContent='RECORDING';
  }
  async function applyStep(inputAngles, order=$('order-select').value, decimals=null) {
    if(state.busy) return;
    setBusy(true);
    try {
      await startSession();
      const result=await requestJSON(`/api/sessions/${state.sessionId}/steps`,{method:'POST',body:JSON.stringify({...inputAngles,order,quantize_decimals:decimals})});
      state.matrix=result.matrix; state.step=result.step_index; state.archived=false;
      state.activity.push(Math.max(Math.abs(inputAngles.x),Math.abs(inputAngles.y),Math.abs(inputAngles.z)));
      if(state.activity.length>60) state.activity.shift();
      state.trail.push({step:result.step_index,error:result.orthogonality_error});
      updateDiagnostics(result); updateCompare(); renderModel(); renderBars(); updateStepLabel();
      $('composition-label').textContent=`${order} · Q ← Q R${order[0].toLowerCase()} R${order[1].toLowerCase()} R${order[2].toLowerCase()}`;
      $('log-count').textContent=`${state.step} STEP${state.step===1?'':'S'}`;
      await refreshSessions();
      return result;
    } finally { setBusy(false); }
  }
  function updateStepLabel() { $('step-label').textContent=`STEP ${String(state.step).padStart(3,'0')}`; }
  function updateDiagnostics(result={}) {
    const error=result.orthogonality_error ?? transposeMultiplyError(state.matrix);
    const determinant=result.determinant ?? det(state.matrix);
    $('orth-error').textContent=Number(error).toExponential(2);
    $('det-value').textContent=fmt(determinant,6);
    const noisy=error>1e-6 || Math.abs(determinant-1)>1e-6;
    $('drift-state').classList.toggle('warning',noisy);
    $('drift-state').innerHTML=`<i></i> ${noisy?'DRIFT DETECTED':'NOMINAL'}`;
    const grid=$('matrix-grid'); grid.replaceChildren();
    state.matrix.forEach(row=>row.forEach(value=>{const cell=document.createElement('span');cell.className='matrix-cell';cell.textContent=fmt(value,3);grid.appendChild(cell);}));
    state.posture = classifyPosture(state.matrix, state.posture);
    const postureEl = $('posture-label');
    if (postureEl) postureEl.textContent = `POSTURE · ${state.posture}`;
    $('buffer-count').textContent=`${String(state.activity.length).padStart(2,'0')} / 60`;
  }
  function miniMatrix(m) { return m.map(row=>row.map(v=>fmt(v,2)).join('  ')).join('\n'); }
  let compareTimer;
  async function updateCompare() {
    const a=angles();
    const xyz=compose(a,'XYZ'), zyx=compose(a,'ZYX');
    $('matrix-xyz').textContent=miniMatrix(xyz); $('matrix-zyx').textContent=miniMatrix(zyx);
    const diff=Math.sqrt(xyz.flat().reduce((sum,value,i)=>sum+(value-zyx.flat()[i])**2,0));
    $('order-difference').textContent=fmt(diff,5);
    clearTimeout(compareTimer);
    compareTimer=setTimeout(async()=>{
      try { const data=await requestJSON('/api/compare',{method:'POST',body:JSON.stringify(a)}); $('order-difference').textContent=fmt(data.difference,5); }
      catch(_error) { /* local preview is still calculated synchronously */ }
    },160);
  }
  function renderBars() {
    const container=$('activity-bars'); container.replaceChildren();
    const data=state.activity.slice(-60); const max=Math.max(1,...data.map(Math.abs));
    for(let i=0;i<60;i++) {
      const bar=document.createElement('span'); bar.className='activity-bar';
      const value=data[i]||0; bar.style.height=`${Math.max(value?4:2,4+(Math.abs(value)/max)*26)}px`; bar.style.opacity=value?String(.46+.54*Math.abs(value)/max):'.27';
      container.appendChild(bar);
    }
  }
  function matVec(q,v) { return q.map(row=>row.reduce((s,x,i)=>s+x*v[i],0)); }
  function project(p,w,h,scale) {
    const [x,y,z]=p;
    const cx=w/2+x*scale+z*scale*.31;
    const cy=h/2-y*scale+z*scale*.23;
    const depth=5.7-z*.55+x*.15-y*.12;
    const perspective=6.2/depth;
    return [w/2+(cx-w/2)*perspective,h/2+(cy-h/2)*perspective,depth];
  }
  function drawPolygon(points,fill,stroke,lineWidth=1) {
    ctx.beginPath(); points.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1])); ctx.closePath();
    if(fill){ctx.fillStyle=fill;ctx.fill()} if(stroke){ctx.strokeStyle=stroke;ctx.lineWidth=lineWidth;ctx.stroke()}
  }
  function modelGeometry() {
    if(state.model==='cube') {
      const v=[[-1,-1,-1],[1,-1,-1],[1,1,-1],[-1,1,-1],[-1,-1,1],[1,-1,1],[1,1,1],[-1,1,1]];
      return {vertices:v,faces:[[0,3,2,1],[4,5,6,7],[0,1,5,4],[1,2,6,5],[2,3,7,6],[3,0,4,7]],detail:[]};
    }
    const w=.92,h=1.55,d=.12;
    const v=[[-w,-h,-d],[w,-h,-d],[w,h,-d],[-w,h,-d],[-w,-h,d],[w,-h,d],[w,h,d],[-w,h,d]];
    return {vertices:v,faces:[[0,3,2,1],[4,5,6,7],[0,1,5,4],[1,2,6,5],[2,3,7,6],[3,0,4,7]],detail:[[-.79,-1.38,d+.003],[.79,-1.38,d+.003],[.79,1.37,d+.003],[-.79,1.37,d+.003]]};
  }
  function resizeCanvas() {
    const rect=canvas.getBoundingClientRect(), dpr=Math.min(window.devicePixelRatio||1,2);
    if(!rect.width||!rect.height)return;
    const width=Math.floor(rect.width*dpr),height=Math.floor(rect.height*dpr);
    if(canvas.width!==width||canvas.height!==height){canvas.width=width;canvas.height=height;}
    ctx.setTransform(dpr,0,0,dpr,0,0); renderModel();
  }
  function renderModel() {
    const rect=canvas.getBoundingClientRect(),w=rect.width,h=rect.height;if(!w||!h)return;
    const dpr=Math.min(window.devicePixelRatio||1,2); ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
    const scale=Math.min(w,h)*.235; const geometry=modelGeometry();
    const transformed=geometry.vertices.map(v=>matVec(state.matrix,v));
    const points=transformed.map(p=>project(p,w,h,scale));
    ctx.save();ctx.globalAlpha=.11;ctx.fillStyle='#183e34';ctx.beginPath();ctx.ellipse(w/2+2,h*.79,scale*.72,scale*.15,-.05,0,Math.PI*2);ctx.fill();ctx.restore();
    const faceList=geometry.faces.map(face=>({face,depth:face.reduce((sum,i)=>sum+points[i][2],0)/face.length})).sort((a,b)=>b.depth-a.depth);
    faceList.forEach(({face})=>{
      const front=face.every(i=>geometry.vertices[i][2]>0);
      const fill=front?'rgba(255,255,255,.92)':'rgba(194,211,203,.83)';
      drawPolygon(face.map(i=>points[i]),fill,'#49625a',1.05);
    });
    if(geometry.detail.length){const screen=geometry.detail.map(v=>project(matVec(state.matrix,v),w,h,scale));drawPolygon(screen,'rgba(224,244,238,.92)','#8ebdb1',.8);
      const topLeft=project(matVec(state.matrix,[-.22,1.20,.13]),w,h,scale),topRight=project(matVec(state.matrix,[.22,1.20,.13]),w,h,scale);
      ctx.beginPath();ctx.arc(topLeft[0],topLeft[1],2.1,0,Math.PI*2);ctx.arc(topRight[0],topRight[1],2.1,0,Math.PI*2);ctx.fillStyle='#91aaa1';ctx.fill();
      const mark=project(matVec(state.matrix,[0,-1.46,.13]),w,h,scale);ctx.beginPath();ctx.arc(mark[0],mark[1],2.1,0,Math.PI*2);ctx.fillStyle='#d6e2dc';ctx.fill();
    }
    const origin=project([0,0,0],w,h,scale);
    const axes=[{v:[1.55,0,0],c:'#e5786d',label:'X'},{v:[0,1.72,0],c:'#12a995',label:'Y'},{v:[0,0,1.5],c:'#758dbb',label:'Z'}];
    axes.forEach(axis=>{const end=project(matVec(state.matrix,axis.v),w,h,scale);ctx.beginPath();ctx.moveTo(origin[0],origin[1]);ctx.lineTo(end[0],end[1]);ctx.strokeStyle=axis.c+'b5';ctx.lineWidth=1.5;ctx.stroke();ctx.beginPath();ctx.arc(end[0],end[1],2.6,0,Math.PI*2);ctx.fillStyle=axis.c;ctx.fill();});
  }
  async function refreshSessions(loadPlot=true) {
    try {
      const data=await requestJSON('/api/sessions');const list=$('session-list');list.replaceChildren();
      if(!data.sessions.length){list.innerHTML='<div class="empty-sessions">No runs archived yet.<br><span>Apply a rotation to start a session.</span></div>';return;}
      data.sessions.forEach(session=>{
        const item=document.createElement('div');item.className='session-item';
        const info=document.createElement('div');info.className='session-info';
        const title=document.createElement('span');title.className='session-title';title.textContent=session.title||`Run #${session.id}`;
        const meta=document.createElement('span');meta.className='session-meta';meta.textContent=`#${session.id} · ${session.step_count} steps · peak ${Number(session.peak_error).toExponential(1)}`;
        info.append(title,meta);const button=document.createElement('button');button.className='session-open';button.textContent='OPEN ↗';button.addEventListener('click',()=>loadSession(session.id));item.append(info,button);list.appendChild(item);
      });
      if(loadPlot && data.sessions.length) showPlot(data.sessions[0].id,data.sessions[0].title);
    } catch(_error) { $('session-list').innerHTML='<div class="empty-sessions">Archive is offline.<br><span>Check the database connection.</span></div>'; }
  }
  function showPlot(id,title) {
    const image=document.createElement('img');
    image.src=`/api/sessions/${id}/plot.png?t=${Date.now()}`;
    image.alt=`Orthogonality drift plot for session ${id}`;
    $('plot-wrap').replaceChildren(image);
    $('plot-caption').textContent=`RUN #${id}`;
  }
  async function loadSession(id) {
    try {
      const data=await requestJSON(`/api/sessions/${id}`);const last=data.orientations.at(-1);
      if(!last){toast('That archived run has no orientation samples.');return;}
      state.sessionId=id;state.archived=true;state.step=data.session.step_count;state.matrix=last.matrix;state.activity=data.sensor_logs.slice(-60).map(s=>Math.max(Math.abs(s.x),Math.abs(s.y),Math.abs(s.z)));
      state.trail=data.orientations.slice(-60).map(o=>({step:o.step_index,error:o.orthogonality_error}));
      updateDiagnostics(last);renderBars();renderModel();updateStepLabel();$('log-count').textContent=`${state.step} STEP${state.step===1?'':'S'}`;
      $('session-status').textContent=`ARCHIVE · #${id}`;$('run-status').textContent='VIEWING ARCHIVE';
      await showPlot(id,data.session.title);setBusy(false);toast(`Viewing saved session #${id} · read only`);
    } catch(error){toast(error.message)}
  }
  function parseLog() {
    const lines=$('log-input').value.split(/\n/).map(line=>line.trim()).filter(Boolean);
    if(!lines.length) throw new Error('Add at least one x, y, z sensor step.');
    return lines.map((line,index)=>{
      const parts=line.split(/[\s,;]+/).filter(Boolean);
      if(parts.length!==3||parts.some(p=>!Number.isFinite(Number(p))))throw new Error(`Line ${index+1}: enter exactly three numeric values.`);
      const [x,y,z]=parts.map(Number);if([x,y,z].some(v=>Math.abs(v)>3600))throw new Error(`Line ${index+1}: values must be within ±3600°.`);return{x,y,z};
    });
  }
  async function runLog(steps,title='Pasted sensor log') {
    setBusy(true);$('run-status').textContent='REPLAYING LOG';
    try {
      await startSession(title);
      for(let i=0;i<steps.length;i++){
        const a=steps[i];
        const result=await requestJSON(`/api/sessions/${state.sessionId}/steps`,{method:'POST',body:JSON.stringify({...a,order:$('order-select').value})});
        state.matrix=result.matrix;state.step=result.step_index;state.activity.push(Math.max(Math.abs(a.x),Math.abs(a.y),Math.abs(a.z)));if(state.activity.length>60)state.activity.shift();state.trail.push({step:result.step_index,error:result.orthogonality_error});
        updateDiagnostics(result);renderModel();renderBars();updateStepLabel();$('log-count').textContent=`${state.step} STEPS`;
        if(i%4===0) await new Promise(resolve=>setTimeout(resolve,20));
      }
      $('run-status').textContent='RECORDING';$('session-status').textContent=`SAVING · #${state.sessionId}`;await refreshSessions();toast(`${steps.length} sensor steps saved.`);
    } catch(error){toast(error.message);$('run-status').textContent='PAUSED';}
    finally{setBusy(false)}
  }
  async function runStress() {
    if(state.busy)return;setBusy(true);$('run-status').textContent='PRECISION STRESS';
    try {
      await startSession('Finite-precision drift stress');
      const data=await requestJSON(`/api/sessions/${state.sessionId}/stress`,{method:'POST',body:JSON.stringify({count:1500,decimals:6,seed:28,order:$('order-select').value})});
      state.matrix=data.matrix;state.step=data.step_index;state.archived=false;
      state.activity=data.window.map(sample=>Math.max(Math.abs(sample.angles.x),Math.abs(sample.angles.y),Math.abs(sample.angles.z)));
      state.trail=data.window.map(sample=>({step:sample.step_index,error:sample.orthogonality_error}));
      updateDiagnostics(data);renderBars();renderModel();updateStepLabel();$('log-count').textContent=`${state.step} STEPS`;$('run-status').textContent='RECORDING';await refreshSessions();showPlot(state.sessionId,'Finite-precision drift stress');
      toast(`Precision stress complete · error ${Number(data.orthogonality_error).toExponential(2)}. Repair with Gram–Schmidt.`);
    }catch(error){toast(error.message);$('run-status').textContent='ERROR';}
    finally{setBusy(false)}
  }
  async function repair() {
    if(!state.sessionId||state.archived){toast('Apply a step first to start a live session.');return;}
    setBusy(true);
    try {
      const result=await requestJSON(`/api/sessions/${state.sessionId}/repair`,{method:'POST',body:'{}'});
      state.matrix=result.matrix;updateDiagnostics(result);renderModel();await refreshSessions();showPlot(state.sessionId,'Current session');
      toast(`Frame squared up · ${Number(result.before_error).toExponential(2)} → ${Number(result.orthogonality_error).toExponential(2)}`);
    }catch(error){toast(error.message)}finally{setBusy(false)}
  }
  async function finishSession() {
    if(!state.sessionId||state.archived){toast('No live recording to finish.');return;}
    setBusy(true);
    try { await requestJSON(`/api/sessions/${state.sessionId}/finish`,{method:'POST',body:'{}'});const id=state.sessionId;state.archived=true;$('run-status').textContent='ARCHIVED';$('session-status').textContent=`ARCHIVE · #${id}`;setBusy(false);await refreshSessions();showPlot(id,'Saved session');toast(`Session #${id} saved to the archive.`); }
    catch(error){toast(error.message);setBusy(false)}
  }
  async function reset() {
    if(state.busy)return;
    if(state.sessionId&&!state.archived){
      setBusy(true);
      try{await requestJSON(`/api/sessions/${state.sessionId}/finish`,{method:'POST',body:'{}'});}
      catch(error){toast(error.message);setBusy(false);return;}
    }
    state.matrix=identity();state.step=0;state.sessionId=null;state.archived=false;state.activity=[];state.trail=[];state.posture='PORTRAIT';
    try { await requestJSON('/api/stream/control',{method:'POST',body:JSON.stringify({action:'reset'})}); } catch(_err) {}
    updateDiagnostics();renderModel();renderBars();updateStepLabel();$('session-status').textContent='NOT SAVING';$('run-status').textContent='STANDBY';$('log-count').textContent='0 STEPS';$('composition-label').textContent='XYZ · Q ← Q Rₓ Rᵧ Rz';$('imu-source-label').textContent='SIMULATED IMU · 60 HZ';$('live-angles-readout').textContent='Δ —';setBusy(false);toast('Orientation reset. A new session starts with the next step.');
  }
  function setModel(model) {
    state.model=model;document.querySelectorAll('[data-model]').forEach(button=>button.classList.toggle('active',button.dataset.model===model));renderModel();
  }

  ['angle-x','angle-y','angle-z'].forEach(id=>$(id).addEventListener('input',updateCompare));
  $('apply-button').addEventListener('click',()=>applyStep(angles()).catch(error=>toast(error.message)));
  $('repair-button').addEventListener('click',repair);
  $('stress-button').addEventListener('click',runStress);
  $('demo-button').addEventListener('click',()=>runLog([{x:18,y:0,z:12},{x:0,y:26,z:-8},{x:-14,y:7,z:23},{x:9,y:-19,z:5},{x:21,y:13,z:-16},{x:-8,y:11,z:27}], 'Six-step sample log'));
  $('load-log-button').addEventListener('click',()=>{try{runLog(parseLog())}catch(error){toast(error.message)}});
  $('stop-button').addEventListener('click',finishSession);
  $('reset-button').addEventListener('click',reset);
  $('refresh-sessions').addEventListener('click',()=>refreshSessions());
  document.querySelectorAll('[data-model]').forEach(button=>button.addEventListener('click',()=>setModel(button.dataset.model)));
  $('order-select').addEventListener('change',async ()=>{const order=$('order-select').value;$('composition-label').textContent=`${order} · Q ← Q R${order[0].toLowerCase()} R${order[1].toLowerCase()} R${order[2].toLowerCase()}`;try{await requestJSON('/api/stream/control',{method:'POST',body:JSON.stringify({order})});}catch(_err){}});
  $('copy-url-button').addEventListener('click',async ()=>{
    const url=state.ingestUrl || $('ingest-url').textContent;
    try { await navigator.clipboard.writeText(url); toast('SensorLog URL copied to clipboard.'); }
    catch(_err) { toast(`Copy URL: ${url}`); }
  });
  $('stream-toggle-button').addEventListener('click',async ()=>{
    try {
      const next=!state.listening;
      const data=await requestJSON('/api/stream/control',{method:'POST',body:JSON.stringify({listening:next})});
      updateBridgeStatus(data);
      toast(data.listening ? 'Listening for iPhone SensorLog packets.' : 'iPhone stream paused.');
    } catch(error) { toast(error.message); }
  });
  $('help-button').addEventListener('click',()=>$('help-dialog').showModal());
  $('close-help').addEventListener('click',()=>$('help-dialog').close());
  $('close-help-bottom').addEventListener('click',()=>$('help-dialog').close());
  $('help-dialog').addEventListener('click',event=>{if(event.target===$('help-dialog'))$('help-dialog').close()});
  function updateClock(){const d=new Date();$('clock').textContent=`LOCAL · ${d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false})}`;}
  function updateBridgeStatus(data={}) {
    if(typeof data.listening==='boolean') state.listening=data.listening;
    if(data.lan_url) { state.ingestUrl=data.lan_url; $('ingest-url').textContent=data.lan_url; }
    const dot=$('bridge-dot'), label=$('bridge-state'), toggle=$('stream-toggle-button');
    dot.classList.toggle('paused',!state.listening);
    const activeLive=state.listening && data.rate_hz>0 && data.session_id;
    dot.classList.toggle('live',Boolean(activeLive));
    if(!state.listening) label.textContent='STREAM PAUSED';
    else if(activeLive) label.textContent=`IPHONE LIVE · ${Number(data.rate_hz).toFixed(1)} HZ`;
    else label.textContent='IPHONE LISTENING';
    toggle.textContent=state.listening?'Pause stream':'Resume stream';
    if(data.last_angles) {
      const {x,y,z}=data.last_angles;
      $('live-angles-readout').textContent=`Δ ${fmt(x,2)}°, ${fmt(y,2)}°, ${fmt(z,2)}°`;
    }
    if(data.last_source && data.session_id) {
      const hz=data.rate_hz?` · ${Number(data.rate_hz).toFixed(1)} HZ`:'';
      $('imu-source-label').textContent=`${String(data.last_source).toUpperCase()}${hz}`;
    }
  }
  function scheduleLiveRender(diagData) {
    if(state.renderQueued) return;
    state.renderQueued=true;
    requestAnimationFrame(()=>{
      state.renderQueued=false;
      updateDiagnostics(diagData);
      renderModel();
      renderBars();
      updateStepLabel();
      $('log-count').textContent=`${state.step} STEP${state.step===1?'':'S'}`;
    });
  }
  function applyIncomingLiveEvent(data, isStep=true) {
    if(!data) return;
    updateBridgeStatus(data);
    if(!isStep && !data.session_id) return;
    if(isStep && state.archived) state.archived=false;
    if(state.archived) return;
    const prevSession=state.sessionId;
    if(data.session_id) {
      state.sessionId=data.session_id;
      $('session-status').textContent=`SAVING · #${state.sessionId}`;
      $('run-status').textContent=isStep?'LIVE · IPHONE':'RECORDING';
      $('stop-button').disabled=false;
    }
    if(Array.isArray(data.matrix)) state.matrix=data.matrix;
    if(typeof data.step_index==='number') state.step=data.step_index;
    else if(typeof data.step_count==='number') state.step=data.step_count;
    if(Array.isArray(data.window)) {
      state.activity=data.window.map(s=>Math.max(Math.abs(s.angles?.x||0),Math.abs(s.angles?.y||0),Math.abs(s.angles?.z||0)));
      state.trail=data.window.map(s=>({step:s.step_index,error:s.orthogonality_error}));
    } else if(data.angles) {
      state.activity.push(Math.max(Math.abs(data.angles.x),Math.abs(data.angles.y),Math.abs(data.angles.z)));
      if(state.activity.length>60) state.activity.shift();
    }
    if(data.order && $('order-select').value!==data.order) {
      $('order-select').value=data.order;
      $('composition-label').textContent=`${data.order} · Q ← Q R${data.order[0].toLowerCase()} R${data.order[1].toLowerCase()} R${data.order[2].toLowerCase()}`;
    }
    if(data.angles) {
      $('live-angles-readout').textContent=`Δ ${fmt(data.angles.x,2)}°, ${fmt(data.angles.y,2)}°, ${fmt(data.angles.z,2)}°`;
    }
    if(data.source) {
      const hz=data.rate_hz?` · ${Number(data.rate_hz).toFixed(1)} HZ`:'';
      $('imu-source-label').textContent=`${String(data.source).toUpperCase()}${hz}`;
    }
    scheduleLiveRender(data);
    const now=Date.now();
    if(data.session_id && (prevSession!==data.session_id || now-state.lastSessionRefresh>3000)) {
      state.lastSessionRefresh=now;
      refreshSessions(prevSession!==data.session_id);
    }
  }
  async function initLiveStream() {
    try {
      const info=await requestJSON('/api/stream/info');
      updateBridgeStatus(info);
      if(info.order && $('order-select').value!==info.order) {
        $('order-select').value=info.order;
        $('composition-label').textContent=`${info.order} · Q ← Q R${info.order[0].toLowerCase()} R${info.order[1].toLowerCase()} R${info.order[2].toLowerCase()}`;
      }
    } catch(_err) {
      $('ingest-url').textContent=`${window.location.origin}/api/sensorlog`;
    }
    if(!('EventSource' in window)) return;
    const events=new EventSource('/api/stream/events');
    events.addEventListener('snapshot',(evt)=>{ try{ applyIncomingLiveEvent(JSON.parse(evt.data),false); }catch(_e){} });
    events.addEventListener('step',(evt)=>{ try{ applyIncomingLiveEvent(JSON.parse(evt.data),true); }catch(_e){} });
    events.addEventListener('status',(evt)=>{ try{ updateBridgeStatus(JSON.parse(evt.data)); }catch(_e){} });
  }
  $('clock').textContent='LOCAL · —';setInterval(updateClock,1000);updateClock();
  updateDiagnostics();renderBars();updateCompare();refreshSessions();setBusy(false);initLiveStream();
  if('ResizeObserver' in window)new ResizeObserver(resizeCanvas).observe($('model-stage'));else window.addEventListener('resize',resizeCanvas);
  resizeCanvas();
})();
