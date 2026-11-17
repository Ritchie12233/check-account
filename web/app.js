const $ = id => document.getElementById(id);
const labels = {Claimed:'已发现',Available:'未检测到',Unknown:'请求异常',Illegal:'用户名不适用',WAF:'被反爬拦截'};
const states = {running:'查询中',completed:'已完成',failed:'查询失败',cancelled:'已停止',timeout:'已超时',interrupted:'已中断'};
const reviews={pending:'待核实',confirmed:'已人工核实',rejected:'已排除'};
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
function linkNode(url){const a=document.createElement('a');try{const parsed=new URL(url);if(!['http:','https:'].includes(parsed.protocol))throw new Error();a.href=parsed.href;a.textContent=url;a.target='_blank';a.rel='noopener noreferrer';}catch{a.textContent='—';}return a;}
function reviewSelect(key,state){
  const select=document.createElement('select');select.className='review';select.setAttribute('aria-label','归属核实状态');
  for(const [value,label] of Object.entries(reviews)){const o=document.createElement('option');o.value=value;o.textContent=label;select.append(o);}select.value=state||'pending';
  select.onchange=async()=>{const id=current.id;const next=select.value;select.disabled=true;try{const data=await api(`/api/jobs/${id}/review`,{key,state:next});if(current?.id===id){current=data;render();}error();}catch(e){select.value=state||'pending';error(e.message);}finally{select.disabled=false;}};
  return select;
}
function renderRows(){
  const tbody=$('rows');tbody.replaceChildren();
  const candidates=filter==='found'?(current?.accounts||[]):[...(current?.accounts||[]),...(current?.rows||[]).filter(r=>r.status!=='Claimed')];
  const rows=candidates.filter(r=>(filter!=='errors'||!['Claimed','Available'].includes(r.status))&&r.site.toLowerCase().includes($('search').value.toLowerCase()));
  for(const row of rows){
    const tr=document.createElement('tr'), site=document.createElement('td'), status=document.createElement('td'), link=document.createElement('td');site.textContent=row.site;
    const badge=document.createElement('span');badge.className='badge'+(row.status==='Claimed'?' found':!['Claimed','Available'].includes(row.status)?' warn':'');badge.textContent=labels[row.status]||'未能确认';status.append(badge);
    if(row.key)status.append(reviewSelect(row.key,row.review));link.append(linkNode(row.url));
    const source=document.createElement('small');source.className='source';source.textContent='查询来源：'+(row.sources?Array.from(new Set(row.sources.map(s=>s.username))).join('、'):row.username||current.username);link.append(source);
    if(row.evidence){const evidence=document.createElement('small');evidence.className='source';evidence.textContent=row.evidence;link.append(evidence);}
    if(row.sources?.length){const detail=document.createElement('details'), title=document.createElement('summary');title.textContent=`查看 ${row.sources.length} 条检测来源`;detail.append(title);for(const item of row.sources){const p=document.createElement('p');p.textContent=`${item.site} · ${item.username} · ${item.checked_at||'旧记录无检测时间'} `;p.append(linkNode(item.url));detail.append(p);}link.append(detail);}
    tr.append(site,status,link);tbody.append(tr);
  }
  $('empty').hidden=rows.length>0;
  $('empty').querySelector('p').textContent=current?.state==='running'?'正在检测，请稍候；结果会自动更新。':filter==='found'?'此查询暂未发现同名账号，可切换“全部”查看检测状态。':'当前筛选下没有结果。';
}
function renderLinks(){
  const links=current.links||[];$('links').replaceChildren();$('link-count').textContent=links.length;$('links-empty').hidden=links.length>0;
  for(const item of links){const card=document.createElement('article');card.className='link-card';const label=document.createElement('strong');label.textContent=item.site;const from=document.createElement('p');from.append('来源页：',linkNode(item.from_url));const to=document.createElement('p');to.append('目标链接：',linkNode(item.to_url));const evidence=document.createElement('p');evidence.className='hint';evidence.textContent=`${item.evidence} · 查询 ${item.username} · ${item.observed_at}`;card.append(label,reviewSelect('link:'+item.id,item.review),from,to,evidence);$('links').append(card);}
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
  const raw=(current.rows||[]).filter(r=>r.status==='Claimed').length;
  $('dedup-note').textContent=`本次查询：${(current.usernames||[current.username]).join('、')}。发现 ${raw} 条账号检测，按规范化链接合并为 ${current.found} 个账号；合并不代表归属已确认。`;
  if(!document.activeElement?.classList.contains('review')){renderRows();renderLinks();}renderHistory();
}
async function selectJob(id){const data=await api(`/api/jobs/${id}`);current=data;error();render();}
async function poll(){
  if(pollBusy)return;pollBusy=true;
  try{jobs=await api('/api/jobs');if(current){const id=current.id;const updated=await api(`/api/jobs/${id}`);if(current?.id===id)current=updated;render();}else{renderHistory();const active=jobs.find(j=>j.state==='running');if(active)await selectJob(active.id);}$('submit').disabled=jobs.some(j=>j.state==='running');}
  catch(e){error(`无法连接本地服务：${e.message}`);}finally{pollBusy=false;}
}
$('query-form').addEventListener('submit',async event=>{event.preventDefault();error();const selected=chosenSites();if($('scope').value==='custom'&&!selected.length){error('请至少选择一个平台。');return;}const variants=$('variants').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);if(variants.length>5){error('额外用户名最多 5 个，请编辑后再查询。');return;}$('submit').disabled=true;try{const job=await api('/api/jobs',{username:$('username').value.trim(),sites:selected,variants});jobs.unshift(job);await selectJob(job.id);}catch(e){error(e.message);$('submit').disabled=jobs.some(j=>j.state==='running');}});
$('suggest').onclick=async()=>{try{const name=$('username').value.trim();const data=await api('/api/variants',{username:name});$('variants').value=data.usernames.filter(v=>v!==name).join('\n');error();}catch(e){error(e.message);}};
$('cancel').onclick=async()=>{if(!current)return;try{await api(`/api/jobs/${current.id}/cancel`,{});await poll();}catch(e){error(e.message);}};
$('refresh').onclick=()=>poll();$('scope').onchange=scopeChanged;$('site-picker').onchange=scopeChanged;$('search').oninput=renderRows;
for(const button of document.querySelectorAll('[data-filter]'))button.onclick=()=>{filter=button.dataset.filter;for(const b of document.querySelectorAll('[data-filter]')){const selected=b===button;b.classList.toggle('selected',selected);b.setAttribute('aria-pressed',String(selected));}renderRows();};
async function start(){try{const config=await api('/api/bootstrap');token=config.token;sites=config.sites;for(const site of sites){const option=document.createElement('option');option.value=site;option.textContent=site;$('site-picker').append(option);}scopeChanged();await poll();timer=setInterval(poll,2000);}catch(e){error(e.message);$('submit').disabled=true;}}
start();
