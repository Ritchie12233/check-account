const $ = id => document.getElementById(id);
const labels = {Claimed:'已发现',Available:'未检测到',Unknown:'请求异常',Illegal:'用户名不适用',WAF:'被反爬拦截'};
const states = {running:'查询中',completed:'已完成',failed:'查询失败',cancelled:'已停止',timeout:'已超时',interrupted:'已中断'};
let token='', sites=[], jobs=[], current=null, filter='found', timer, pollBusy=false;
function error(message='') {$('error').textContent=message; $('error').hidden=!message;}
async function api(path, body) {
  const response=await fetch(path, body===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify(body)});
  const data=await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败，请稍后重试。');
  return data;
}
const common=['GitHub','Reddit','Instagram','YouTube','Pinterest','Twitch','TikTok','Steam','GitLab','Medium'];
function chosenSites(){const scope=$('scope').value;return scope==='all'?[]:scope==='github'?['GitHub']:scope==='common'?common.filter(s=>sites.includes(s)):Array.from($('site-picker').selectedOptions,o=>o.value);}
function scopeChanged(){ $('custom-sites').hidden=$('scope').value!=='custom'; const selected=chosenSites(); $('scope-info').textContent=`${$('scope').value==='all'?sites.length:selected.length} 个平台`; }
function renderHistory(){
  $('history').replaceChildren(); $('history-empty').hidden=jobs.length>0;
  for(const job of jobs){const b=document.createElement('button');b.className='history-item'+(current?.id===job.id?' active':'');const title=document.createElement('strong');title.textContent=job.username;const sub=document.createElement('small');sub.textContent=`${states[job.state]||job.state} · ${job.found} 个账号 · ${new Date(job.created*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})}`;b.append(title,sub);b.onclick=()=>selectJob(job.id).catch(e=>error(e.message));$('history').append(b);}
}
function renderRows(){
  const tbody=$('rows');tbody.replaceChildren();
  const rows=(current?.rows||[]).filter(r=>(filter==='all'||filter==='found'&&r.status==='Claimed'||filter==='errors'&&!['Claimed','Available'].includes(r.status))&&r.site.toLowerCase().includes($('search').value.toLowerCase()));
  for(const row of rows){const tr=document.createElement('tr');const site=document.createElement('td');site.textContent=row.site;const status=document.createElement('td');const badge=document.createElement('span');badge.className='badge'+(row.status==='Claimed'?' found':!['Claimed','Available'].includes(row.status)?' warn':'');badge.textContent=labels[row.status]||'未能确认';status.append(badge);const link=document.createElement('td');try{const url=new URL(row.url);if(['http:','https:'].includes(url.protocol)){const a=document.createElement('a');a.href=url.href;a.textContent=row.url;a.target='_blank';a.rel='noopener noreferrer';link.append(a);}else{link.textContent='—';}}catch{link.textContent='—';}tr.append(site,status,link);tbody.append(tr);}
  $('empty').hidden=rows.length>0;
  $('empty').querySelector('p').textContent=current?.state==='running'?'正在检测，请稍候；结果会自动更新。':filter==='found'?'此查询暂未发现同名账号，可切换“全部”查看检测状态。':'当前筛选下没有结果。';
}
function render(){
  if(!current)return;
  $('welcome').hidden=true;$('results').hidden=false;$('result-heading').textContent=current.username;
  $('progress-text').textContent=`${current.checked} / ${current.total}`;$('found-text').textContent=current.found;$('state-text').textContent=states[current.state]||current.state;
  $('progress').max=current.total||1;$('progress').value=current.checked;
  $('job-message').textContent=current.message||(current.state==='running'?'正在检测公开账号页面，结果将逐步显示。':'检测结束。账号归属仍需人工核实。');
  $('cancel').hidden=current.state!=='running';
  $('submit').disabled=jobs.some(j=>j.state==='running');
  for(const fmt of ['csv','txt','json']){$(fmt).href=`/api/jobs/${current.id}/export.${fmt}`;$(fmt).setAttribute('download','');}
  renderRows();renderHistory();
}
async function selectJob(id){const data=await api(`/api/jobs/${id}`);current=data;error();render();}
async function poll(){
  if(pollBusy)return;pollBusy=true;
  try{jobs=await api('/api/jobs');if(current){const id=current.id;const updated=await api(`/api/jobs/${id}`);if(current?.id===id)current=updated;render();}else{renderHistory();const active=jobs.find(j=>j.state==='running');if(active)await selectJob(active.id);}$('submit').disabled=jobs.some(j=>j.state==='running');}
  catch(e){error(`无法连接本地服务：${e.message}`);}finally{pollBusy=false;}
}
$('query-form').addEventListener('submit',async event=>{event.preventDefault();error();const selected=chosenSites();if($('scope').value==='custom'&&!selected.length){error('请至少选择一个平台。');return;}$('submit').disabled=true;try{const job=await api('/api/jobs',{username:$('username').value.trim(),sites:selected});jobs.unshift(job);await selectJob(job.id);}catch(e){error(e.message);$('submit').disabled=jobs.some(j=>j.state==='running');}});
$('cancel').onclick=async()=>{if(!current)return;try{await api(`/api/jobs/${current.id}/cancel`,{});await poll();}catch(e){error(e.message);}};
$('refresh').onclick=()=>poll();$('scope').onchange=scopeChanged;$('site-picker').onchange=scopeChanged;$('search').oninput=renderRows;
for(const button of document.querySelectorAll('[data-filter]'))button.onclick=()=>{filter=button.dataset.filter;for(const b of document.querySelectorAll('[data-filter]')){const selected=b===button;b.classList.toggle('selected',selected);b.setAttribute('aria-pressed',String(selected));}renderRows();};
async function start(){try{const config=await api('/api/bootstrap');token=config.token;sites=config.sites;for(const site of sites){const option=document.createElement('option');option.value=site;option.textContent=site;$('site-picker').append(option);}scopeChanged();await poll();timer=setInterval(poll,2000);}catch(e){error(e.message);$('submit').disabled=true;}}
start();
