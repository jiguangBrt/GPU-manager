'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = (v, digits = 0) => v == null ? '—' : Number(v).toFixed(digits);
const gib = v => number(v == null ? null : v / 1024, 1);
const labels = {zero:'当前 0%', running:'计算中', unknown:'指标未知', offline:'数据过期'};
let data = null, selectedServer = null, timer = null, selectedGPU = null, draftServers = [], toastTimer;

function toast(message) {
  $('#toast').textContent = message;
  $('#toast').classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('#toast').classList.remove('show'), 3500);
}
async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  if (!response.ok) {
    let error; try {error = await response.json();} catch {error = {};}
    throw new Error(typeof error.detail === 'string' ? error.detail : '配置格式有误，请检查连接目标、端口及刷新间隔。');
  }
  return response.json();
}
function visibleServers() {return data.servers.filter(s => !selectedServer || s.id === selectedServer);}
function cards() {return visibleServers().flatMap(server => server.gpus.map(gpu => ({...gpu, server})));}
function render() {
  if (selectedServer && !data.servers.some(s=>s.id===selectedServer)) selectedServer = null;
  $('#nav-count').textContent = data.servers.reduce((n,s)=>n+s.gpus.length,0);
  $('#server-list').innerHTML = data.servers.map(s=>`<button class="server-button ${s.id===selectedServer?'selected':''}" data-server="${escapeHTML(s.id)}"><span class="server-dot ${data.demo?'demo':s.stale?'offline':''}"></span><span>${escapeHTML(s.name)}<small>${data.demo?'示例快照':s.error?'连接失败':s.stale?'等待连接':`${s.gpus.length} 张 GPU · 已连接`}</small></span></button>`).join('');
  $$('#server-list [data-server]').forEach(b=>b.addEventListener('click',()=>{selectedServer=b.dataset.server;render();}));
  const gpus = cards(), online = gpus.filter(g=>g.status!=='offline');
  const free = online.map(g=>g.memory_free_mib).filter(v=>v!=null);
  const util = online.map(g=>g.utilization).filter(v=>v!=null);
  const countZero = online.filter(g=>g.utilization===0).length;
  $('#stats').innerHTML = [
    ['GPU 总数',number(gpus.length), '张',`${visibleServers().length} 台服务器 · ${data.demo?'静态快照':'已配置'}`,'▦',''],
    ['当前利用率为 0',online.length?number(countZero):'—','张','仅表示当前采样，不代表持续空闲','◉','featured'],
    ['剩余显存合计',free.length?gib(free.reduce((a,b)=>a+b,0)):'—','GiB', '各卡独立显存的合计','▤',''],
    ['平均 GPU 利用率',util.length?number(util.reduce((a,b)=>a+b,0)/util.length):'—','%', '按有数据的显卡计算','⌁','']
  ].map(([title,n,unit,note,icon,cls])=>`<div class="stat-card ${cls}"><div class="stat-title">${title}</div><span class="stat-icon">${icon}</span><div class="stat-number">${n}<small>${unit}</small></div><div class="stat-note">${note}</div></div>`).join('');
  const notice = $('#notice');
  if (data.demo) {
    notice.className='notice';notice.innerHTML='<span class="notice-icon">i</span><div><strong>正在查看八卡示例快照</strong><span>虚构示例 · 非实时数据。添加 SSH 服务器后即可自动采样。</span></div>';
  } else {
    const bad = visibleServers().filter(s=>s.stale), error = bad.find(s=>s.error);
    notice.className = `notice ${bad.length?'':'live'}`;
    notice.innerHTML=`<span class="notice-icon">${bad.length?'!':'✓'}</span><div><strong>${bad.length?'等待连接或采集异常':'正在自动监控'}</strong><span>${bad.length?escapeHTML(error?.error||'正在通过 SSH 连接服务器，请稍候。'):`每 ${data.settings.interval_seconds} 秒自动更新当前显卡状态。`}</span></div>`;
  }
  $('#gpu-count').textContent = `${gpus.length} 张`;
  $('#server-meta').textContent = visibleServers().map(s=>`${s.name}${s.driver?` · Driver ${s.driver}`:''}${s.cuda?` · CUDA ${s.cuda}`:''}`).join(' / ');
  const latest = Math.max(...visibleServers().map(s=>s.updated_at||0));
  $('#updated').textContent = data.demo?'示例快照 · 非实时数据':latest?`最后采集 ${new Date(latest*1000).toLocaleTimeString('zh-CN',{hour12:false})}`:'等待首次采样';
  $('#poll-label').textContent = data.demo?'静态预览 · 尚未连接服务器':`每 ${data.settings.interval_seconds} 秒采样 · 本机 SSH`;
  $('#gpu-grid').innerHTML = gpus.length ? gpus.map(g=>{
    const percent = g.memory_total_mib && g.memory_used_mib!=null ? Math.min(100,g.memory_used_mib/g.memory_total_mib*100) : 0;
    return `<article class="gpu-card ${g.status}" tabindex="0" role="button" aria-label="查看 GPU ${g.index} 详情" data-gpu="${escapeHTML(g.uuid)}" data-sid="${escapeHTML(g.server.id)}">
      <div class="card-top"><span class="gpu-number"><span class="chip-icon">▦</span> GPU ${g.index}</span><span class="badge ${g.status}"><i></i>${labels[g.status]}</span></div>
      <div class="gpu-model" title="${escapeHTML(g.name)}">${escapeHTML(g.name)}</div>${data.servers.length>1?`<div class="gpu-server">${escapeHTML(g.server.name)}</div>`:''}
      <div class="metric-heading"><span>显存占用</span><b>${gib(g.memory_used_mib)} / ${gib(g.memory_total_mib)} GiB</b></div><div class="bar"><span style="width:${percent}%"></span></div>
      <div class="metric-heading"><span>GPU 利用率</span><b>${number(g.utilization)}${g.utilization==null?'':'%'}</b></div><div class="bar util ${g.utilization>=90?'high':''}"><span style="width:${Math.min(100,Math.max(0,g.utilization||0))}%"></span></div>
      <div class="free-memory"><strong>${gib(g.memory_free_mib)}</strong><span>GiB</span><small>剩余显存</small></div>
      <div class="card-footer"><span class="${g.temperature>=75?'hot':''}">${number(g.temperature)} °C</span><span>${number(g.power_w)} / ${number(g.power_limit_w)} W</span><span>${g.processes.length} 进程 <span class="detail-arrow">↗</span></span></div></article>`;
  }).join('') : '<div class="empty"><strong>等待显卡数据</strong><br>连接成功后，这里会自动平铺所有显卡。<br>若长时间没有数据，请检查上方连接提示。</div>';
  $$('#gpu-grid [data-gpu]').forEach(el=>{
    const open=()=>{selectedGPU={sid:el.dataset.sid,uuid:el.dataset.gpu,processes:null,updatedAt:null,error:null,fetching:false};renderDetail();$('#detail-dialog').showModal();if(!data.demo)loadProcesses();};
    el.addEventListener('click',open);el.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();open();}});
  });
  // Keep the open process table stable for reading and copying. Refresh on demand.
}
function renderDetail() {
  const server = data.servers.find(s=>s.id===selectedGPU?.sid), g = server?.gpus.find(g=>g.uuid===selectedGPU?.uuid);
  if (!g) {$('#detail-dialog').close();return;}
  $('#detail-content').innerHTML=`<div class="dialog-header"><div class="eyebrow">GPU DETAILS ${data.demo?'· STATIC SNAPSHOT':''}</div><button class="close-button" id="close-detail" aria-label="关闭详情">×</button><h2>GPU ${g.index} <span>${labels[g.status]}</span></h2><p>${escapeHTML(g.name)} · ${escapeHTML(server.name)}</p></div>
    <div class="dialog-body">${server.error?`<div class="detail-error">${escapeHTML(server.error)}</div>`:''}
    <div class="detail-metrics"><div>剩余显存<strong>${gib(g.memory_free_mib)} <small>GiB</small></strong></div><div>GPU 利用率<strong>${number(g.utilization)} <small>%</small></strong></div><div>温度<strong>${number(g.temperature)} <small>°C</small></strong></div></div>
    <div class="form-section-heading"><h3>谁在使用 · ${selectedGPU.processes?.length ?? g.processes.length} 个进程</h3>${data.demo?'':`<button class="quiet-link" id="refresh-processes" ${selectedGPU.fetching?'disabled':''}>${selectedGPU.fetching?'正在查询…':'↻ 刷新进程'}</button>`}</div>
    <p class="field-hint">${data.demo?'静态快照没有用户名；连接服务器后可实时查询。':selectedGPU.fetching?'正在向服务器查询这张卡的当前进程和所属用户…':selectedGPU.updatedAt?`进程采集时间 ${new Date(selectedGPU.updatedAt*1000).toLocaleTimeString('zh-CN',{hour12:false})} · 点击刷新获取最新进程。`:'尚未取得实时进程信息。'}</p>
    ${selectedGPU.error?`<div class="detail-error">${escapeHTML(selectedGPU.error)}</div>`:''}
    ${selectedGPU.processes?`<div class="owner-summary">${[...new Set(selectedGPU.processes.map(p=>p.user||'未知'))].map(user=>`<span>${escapeHTML(user)} <small>${selectedGPU.processes.filter(p=>(p.user||'未知')===user).length} 个进程</small></span>`).join('')}</div>`:''}
    <div class="process-scroll">${(selectedGPU.processes??g.processes).length?`<table class="process-table"><thead><tr><th>用户</th><th>PID / 类型</th><th>进程 / 启动命令</th><th>显存 MiB</th></tr></thead><tbody>${(selectedGPU.processes??g.processes).map(p=>`<tr><td class="process-user">${escapeHTML(p.user||'未知')}</td><td>${escapeHTML(p.pid)}<small>${escapeHTML(p.type)}</small></td><td><span class="process-name">${escapeHTML(p.name)}</span>${p.command?`<details class="process-command"><summary>完整启动命令</summary><code>${escapeHTML(p.command)}</code></details>`:''}</td><td>${number(p.memory_mib)}</td></tr>`).join('')}</tbody></table>`:'<p class="field-hint">当前未列出进程；部分权限或驱动模式可能无法返回完整信息。</p>'}</div>
    <p class="field-hint">用户名和命令由系统进程信息匹配 PID；权限不足或进程已退出时显示未知。C = 计算，G = 图形。</p>
    </div>`;
  $('#close-detail').addEventListener('click',()=>$('#detail-dialog').close());
  $('#refresh-processes')?.addEventListener('click',loadProcesses);
}
async function loadProcesses() {
  const selection=selectedGPU;
  if(!selection||selection.fetching||data.demo)return;
  selection.fetching=true;selection.error=null;renderDetail();
  try {
    const result=await api(`/api/servers/${encodeURIComponent(selection.sid)}/gpus/${encodeURIComponent(selection.uuid)}/processes`);
    if(selectedGPU===selection){selection.processes=result.processes;selection.updatedAt=result.updated_at;}
  } catch(error) {if(selectedGPU===selection)selection.error=error.message;}
  finally {if(selectedGPU===selection){selection.fetching=false;renderDetail();}}
}
async function load() {
  try {data=await api('/api/state');render();}
  catch(e){$('#notice').className='notice';$('#notice').innerHTML='<span class="notice-icon">!</span><div><strong>本机监控服务连接中断</strong><span>当前页面数据可能已经过期。请确认程序仍在运行，连接恢复后会自动更新。</span></div>'; if(data){data.servers.forEach(s=>{s.stale=true;s.error='本机服务连接中断';s.gpus.forEach(g=>g.status='offline');});const notice=$('#notice').innerHTML;render();$('#notice').className='notice';$('#notice').innerHTML=notice;}}
  clearTimeout(timer);timer=setTimeout(load,2000);
}
function serverEditor() {
  $('#server-edit-list').innerHTML = draftServers.length?draftServers.map((s,i)=>`<div class="server-edit" data-editor="${i}"><div class="form-grid"><label>显示名称<input data-field="name" value="${escapeHTML(s.name)}" maxlength="80" placeholder="公司计算服务器" required></label><label>SSH 别名 / 连接目标<input data-field="target" value="${escapeHTML(s.target)}" maxlength="253" placeholder="gpu-server 或 user@host" required></label><label>SSH 端口（可选）<input data-field="port" type="number" min="1" max="65535" value="${s.port||''}" placeholder="沿用 SSH config / 22"></label><label>本机密钥路径（可选）<input data-field="identity_file" value="${escapeHTML(s.identity_file||'')}" placeholder="沿用 SSH config / Agent"></label></div><button type="button" class="remove-server" data-remove="${i}">移除此连接</button></div>`).join(''):'<p class="field-hint">尚未配置服务器，首页将展示虚构的八卡示例快照。</p>';
  $$('[data-editor] input').forEach(input=>input.addEventListener('input',()=>{const i=+input.closest('[data-editor]').dataset.editor;draftServers[i][input.dataset.field]=input.dataset.field==='port'?(input.value?Number(input.value):null):input.value;}));
  $$('[data-remove]').forEach(b=>b.addEventListener('click',()=>{draftServers.splice(+b.dataset.remove,1);serverEditor();}));
}
function newServer() {draftServers.push({id:crypto.randomUUID().replaceAll('-',''),name:'公司计算服务器',target:'',port:null,identity_file:''});serverEditor();}
function openSettings(add = false) {
  if(!data){toast('正在连接本机服务，请稍候');return;}
  draftServers=structuredClone(data.settings.servers);$('#interval').value=data.settings.interval_seconds;$('#settings-error').textContent='';
  if(add)newServer();else serverEditor();
  $('#settings-dialog').showModal();
}
$('#settings-open').addEventListener('click',()=>openSettings());$('#settings-side').addEventListener('click',()=>openSettings());
$('#add-server').addEventListener('click',()=>openSettings(true));$('#add-server-side').addEventListener('click',()=>openSettings(true));$('#new-server').addEventListener('click',newServer);
$$('[data-close]').forEach(b=>b.addEventListener('click',()=>document.getElementById(b.dataset.close).close()));
$('#overview-nav').addEventListener('click',()=>{selectedServer=null;if(data)render();});
$('#settings-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=$('#save-settings');button.disabled=true;$('#settings-error').textContent='';
  try {await api('/api/settings',{method:'PUT',body:JSON.stringify({interval_seconds:Number($('#interval').value),servers:draftServers})});$('#settings-dialog').close();toast('设置已保存');await load();}
  catch(error){$('#settings-error').textContent=error.message;}finally{button.disabled=false;}
});
$('#refresh').addEventListener('click',async()=>{
  if(data?.demo){await load();toast('已重新读取静态快照；添加服务器后可实时刷新');return;}
  const b=$('#refresh');b.disabled=true;
  try{await api('/api/refresh',{method:'POST',body:'{}'});toast('已请求采样，完成后自动更新');await load();}catch{toast('刷新失败，请确认本机服务仍在运行');}finally{b.disabled=false;}
});
load();
