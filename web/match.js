/* One search, explicit identity confirmation; independent per-source score modes. */
let matchingRequest = 0;
let matchingCandidates = [];
let matchingPreview = null;
let queueFilter = 'action';
let doubanGapScope='works',doubanGapReason='';
const doubanGapLabels={missing_identity:'豆瓣 ID 未确认',missing_cache:'上游缓存未生成',unrated_cache:'缓存没有有效评分',ready_to_apply:'已有评分，待封面应用',identity_conflict:'作品身份或类型冲突',season_mapping:'缺少独立季映射',source_unavailable:'缓存不可用或读取异常',unavailable:'暂无可用评分'};
let lastSearchResponse=null;
let keepExistingImdb=false;
const matchLabels = {missing_both_ids:'豆瓣 / IMDb 都缺失',missing_douban_id:'缺豆瓣 ID',missing_imdb_id:'缺 IMDb ID',identity_conflict:'身份冲突',ids_without_score:'ID 已齐，评分未获取'};
const scoreSources = [{key:'douban',name:'豆瓣'},{key:'imdb',name:'IMDb'},{key:'rt',name:'RT'}];
const matchStatusLabels = {id_verified:'ID 已核实',negative_placeholder:'尚未生成豆瓣缓存',no_api_key:'未配置',invalid_response:'响应异常',not_requested:'未查询',ok:'可用',no_result:'未找到',no_api_key:'未配置',invalid_api_key:'凭据无效',rate_limited:'已限流',circuit_open:'暂缓查询',lookup_error:'连接失败',http_error:'服务异常',json_without_rating:'缓存暂无评分',missing_cache_file:'等待缓存',missing_ids:'ID 未齐',identity_mismatch:'身份冲突',media_type_mismatch:'类型不符',verification_required:'需人工核对条目'};
function matchStatusText(status){return matchStatusLabels[status]||status||'待查询'}
function modeFor(item,key){return item.override?.[key]?'manual':'automatic'}
function boundIds(item){return item.effective_ids||{douban:item.override?.douban_id||item.provider_ids?.Douban,imdb:item.override?.imdb_id||item.provider_ids?.Imdb}}
function idPill(label,value){return `<span class="id-pill"><b>${label}</b> ${escapeHtml(value||'尚未匹配')}</span>`}
function matchEndpoint(action){return `/api/items/${encodeURIComponent(state.selected.id)}/${action}`}

let scoreBaseline = '';
let lastTaskRevision = null;
let taskPollBusy = false;
const taskStatusLabels = {queued:'等待处理',running:'处理中',succeeded:'已完成',failed:'失败',superseded:'已合并'};
function scoreDraft(){if(!$('#edit-reason'))return null;return {reason:$('#edit-reason').value,sources:Object.fromEntries(scoreSources.map(s=>[s.key,{mode:$(`#mode-${s.key}`).value,value:$(`#edit-${s.key}`).value}]))}}
function scoreDirty(){return JSON.stringify(scoreDraft())!==scoreBaseline}
function updateScoreDirty(){const button=$('#save-item-btn');if(!button)return;const dirty=scoreDirty();button.disabled=!dirty;$('#score-save-status').textContent=dirty?'评分设置有修改，保存后加入后台队列。':'评分设置无修改。确认作品后无需再保存评分设置。'}
function taskText(job){return job?`${taskStatusLabels[job.status]||job.status}${job.error?' · '+job.error:''}`:'暂无封面任务'}
function cacheText(item){const r=item.cache_status||{};return r.status==='ok'?`缓存已生成 · 豆瓣 ${r.value}`:r.status==='json_without_rating'?`缓存没有有效评分${r.rating_count!==undefined&&r.rating_count!==null?' · 缓存评价人数 '+r.rating_count:''}`:['missing_cache_file','negative_placeholder'].includes(r.status)?(boundIds(item).douban?'未找到对应缓存 · 豆瓣 ID 已保存':'豆瓣 ID 尚未确认'):r.status==='missing_ids'?'先确认作品 ID':matchStatusText(r.status)}
function doubanGapDetail(item){
 const gap=item.douban_gap;if(!gap)return '';const cache=item.cache_status||{};
 return `<section class="detail-gap-explanation"><b>${escapeHtml(gap.label)}</b><p>${escapeHtml(gap.description)}</p>${cache.cache_updated_at?`<small>本地缓存更新时间：${escapeHtml(dateText(cache.cache_updated_at*1000))}；这是缓存记录，不是豆瓣网站实时评分。</small>`:''}${!boundIds(item).imdb?'<small>IMDb ID 尚未确认，无法读取该作品的 IMDb／烂番茄关联评分。</small>':''}</section>`;
}

let itemReturnView=null;
const submittedRows=new Map();let queueSnapshotRetry;
function libraryViewKey(){return JSON.stringify([state.page,state.library.filter,state.library.library_id,state.library.search,queueFilter,doubanGapScope,doubanGapReason,queuePage,state.library.page])}
function captureListView(preferredId){
 const cards=[...document.querySelectorAll('#matching-queue-rows [data-row-id],#library-grid [data-item-id]')];
 const anchor=cards.find(el=>(el.dataset.rowId||el.dataset.itemId)===String(preferredId))||cards.find(el=>el.getBoundingClientRect().bottom>0&&el.getBoundingClientRect().top<innerHeight);
 const index=cards.indexOf(anchor);const candidates=anchor?[anchor,...cards.slice(index+1),...cards.slice(0,index).reverse()]:[];
 return {key:libraryViewKey(),y:window.scrollY,anchorTop:anchor?.getBoundingClientRect().top,candidates:candidates.map(el=>el.dataset.rowId||el.dataset.itemId)};
}
function restoreListView(view){
 if(!view||view.key!==libraryViewKey())return;
 let y=view.y;
 for(const id of view.candidates){const card=[...document.querySelectorAll('[data-row-id],#library-grid [data-item-id]')].find(el=>(el.dataset.rowId||el.dataset.itemId)===id);if(card){y=window.scrollY+card.getBoundingClientRect().top-view.anchorTop;break}}
 window.scrollTo({top:Math.max(0,y),behavior:'instant'});
}
function freezeLibraryView(id){
 if(!$('#item-modal').classList.contains('hidden'))return;
 itemReturnView=captureListView(id);
 document.documentElement.classList.add('item-view-open');
}
function closeItemModal(){
 ++matchingRequest;
 $('#item-modal').classList.add('hidden');$('#match-modal').classList.add('hidden');
 document.documentElement.classList.remove('item-view-open');
 if(document.activeElement?.closest('#item-modal,#match-modal'))document.activeElement.blur();
 const view=itemReturnView;itemReturnView=null;restoreListView(view);requestAnimationFrame(()=>restoreListView(view));
}
function transferToBackground(id,task,confirmed=false){
 if(task?.action==='restore')return;
 const view=itemReturnView||captureListView(id),row=state.matchingQueue?.items?.find(r=>r.id===id);
 if(row){
  const automated=new Set(['poster_missing','poster_score_stale','cache_wait','background_pending','background_failed']);
  if(confirmed||row.handled)automated.add('missing_identity');
  const remaining=row.triage.issues.filter(i=>!automated.has(i.code));
  remaining.push({code:'background_pending',label:'已处理，后台更新中',bucket:'waiting',normal_bucket:'waiting',description:'操作已保存并提交后台；可以继续处理下一部，无需再次匹配。',action:null});
  const priority={action:0,waiting:1,info:2,ignored:3};remaining.sort((a,b)=>priority[a.bucket]-priority[b.bucket]);
  row.task=task;if(confirmed)row.handled=true;row.triage={...row.triage,status:remaining[0].bucket,issues:remaining};submittedRows.set(id,structuredClone(row));renderMatchingQueue(view);
 }
}
async function openItem(id,options={}){
 const draft=options.preserveDraft&&state.selected?.id===id&&scoreDirty()?scoreDraft():null;
 if(options.background&&$('#item-modal').classList.contains('hidden'))return;
 if(!options.background)freezeLibraryView(id);
 const modal=$('#item-modal');modal.classList.remove('hidden');
 const scroll=$('.item-modal-card').scrollTop;
 if(!options.background)$('#item-detail-content').innerHTML='<div class="empty-state">正在读取作品与评分来源…</div>';
 const request=++matchingRequest;
 try{
  const item=await api(`/api/items/${encodeURIComponent(id)}`);
  if(request!==matchingRequest)return;
  state.selected=item;
  const ids=boundIds(item),o=item.override||{};
  const issues=(item.triage?.issues||[]).filter(i=>i.bucket==='action'||i.bucket==='waiting');
  $('#item-detail-content').innerHTML=`<div class="item-detail-grid match-detail compact-detail"><aside><div class="detail-poster">${item.poster_url?`<img src="${escapeHtml(item.poster_url)}" alt="${escapeHtml(item.name)}封面">`:'<div class="poster-placeholder">暂无封面</div>'}</div><p class="detail-caption">当前已应用封面</p><div class="detail-issue">${escapeHtml(cacheText(item))}</div></aside><div class="detail-main"><div class="detail-head"><div class="eyebrow">${item.type==='Movie'?'电影':item.type==='Season'?'季':'剧集'} · ${escapeHtml(item.id)}</div><h2>${escapeHtml(item.name)}</h2><p>${escapeHtml(item.original_title||'')} · ${escapeHtml(item.year||'年份未记录')}${item.emby_type&&item.emby_type!==item.type?' · Worker 已确认类型':''}</p></div><section class="identity-card"><div class="section-title"><div><h3>作品匹配</h3><p>${o.match?'匹配已保存；只有修改评分设置才需要另行保存。':item.type==='Season'?'各季使用独立豆瓣条目，在 Emby 季元数据中核对。':'确认作品或直接输入豆瓣 ID。'}</p></div>${item.type!=='Season'?'<button class="primary-button" id="open-match-btn">匹配作品 / 输入 ID</button>':''}</div><div class="id-pills">${idPill('豆瓣',ids.douban)}${idPill('IMDb',ids.imdb)}</div>${o.match?`<p class="detail-caption">已确认：${escapeHtml(o.match.title)} ${escapeHtml(o.match.year)} · ${o.match.sync_emby?'已写回 Emby':'仅绑定 Worker'}</p>`:''}<div class="detail-progress"><span><b>豆瓣缓存</b> ${escapeHtml(cacheText(item))}</span><span id="detail-task-state"><b>封面任务</b> ${escapeHtml(taskText(item.task))}</span></div></section>${doubanGapDetail(item)}${issues.length?`<details class="detail-triage"><summary>${escapeHtml(issues[0].label)}${issues.length>1?' · 共 '+issues.length+' 项':''}</summary>${issues.map(i=>`<p><b>${escapeHtml(i.label)}</b> · ${escapeHtml(i.description)}</p>`).join('')}</details>`:''}<section class="scores-section"><h3>评分与更新方式</h3><p class="detail-description">自动跟随来源；人工固定分优先。缺少评分时保持为空。</p><div class="score-table-head"><span>来源 / 当前评分</span><span>更新方式</span><span>固定分</span></div>${scoreSources.map(s=>{const manual=modeFor(item,s.key)==='manual';return `<article class="score-editor compact-score"><div class="score-title"><b>${s.name==='RT'?'烂番茄':s.name}</b><strong>${escapeHtml(item.ratings?.[s.name]||'暂无')}</strong></div><select aria-label="${s.name}更新方式" id="mode-${s.key}" data-score-mode="${s.key}"><option value="automatic" ${manual?'':'selected'}>自动更新</option><option value="manual" ${manual?'selected':''}>人工固定</option></select><input aria-label="${s.name}固定分" id="edit-${s.key}" inputmode="decimal" value="${escapeHtml(o[s.key]||'')}" placeholder="${s.key==='rt'?'0–100%':'0–10'}" ${manual?'':'disabled'}></article>`}).join('')}<details class="all-score-provenance"><summary>查看评分来源与更新时间</summary>${scoreSources.map(s=>`<div><b>${s.name==='RT'?'烂番茄':s.name}</b>${ratingProvenance(item,s.name)}</div>`).join('')}</details><label class="detail-id-field">人工校正备注<input id="edit-reason" value="${escapeHtml(o.reason||'')}" placeholder="可选：填写核对说明"></label></section></div></div><footer class="detail-action-bar"><p id="score-save-status" aria-live="polite"></p><div class="detail-actions"><button class="primary-button" id="save-item-btn" disabled>保存评分设置并加入队列</button><button class="secondary-button" id="render-item-btn">重新渲染</button><button class="text-button" id="restore-item-btn">恢复原图</button><button class="secondary-button" data-close-modal>完成并关闭</button></div></footer>`;
  scoreBaseline=JSON.stringify(scoreDraft());
  if(draft){$('#edit-reason').value=draft.reason;for(const source of scoreSources){$(`#mode-${source.key}`).value=draft.sources[source.key].mode;$(`#edit-${source.key}`).value=draft.sources[source.key].value;$(`#edit-${source.key}`).disabled=draft.sources[source.key].mode!=='manual'}}
  updateScoreDirty();if(options.background)$('.item-modal-card').scrollTop=scroll;
 }catch(e){if(!options.background)$('#item-detail-content').innerHTML=`<div class="empty-state">${escapeHtml(e.message)}</div>`}
}

async function saveItem(){
 if(!state.selected)return;
 const body={score_modes:{},reason:$('#edit-reason').value.trim(),render:true};
 for(const source of scoreSources){
  const mode=$(`#mode-${source.key}`).value;body.score_modes[source.key]=mode;
  if(mode==='manual'){
   const text=$(`#edit-${source.key}`).value.trim(), number=Number(text.replace('%',''));
   if(!text||!Number.isFinite(number)||number<0||number>(source.key==='rt'?100:10)||(source.key!=='rt'&&number===0)){toast('请填写有效固定评分：豆瓣/IMDb 大于 0 且不超过 10；烂番茄 0–100%',true);return}
   body[source.key]=text;
  }
 }
 const button=$('#save-item-btn');button.disabled=true;
 try{
  const result=await api(`/api/items/${encodeURIComponent(state.selected.id)}/override`,{method:'POST',body:JSON.stringify(body)});
  toast('评分设置已保存，封面已加入后台队列，可继续处理下一部');
  transferToBackground(state.selected.id,result.task);closeItemModal();refreshMatchViews();
 }catch(e){toast(e.message,true);button.disabled=false}
}

function showMatching(){
 if(!state.selected)return;
 ++matchingRequest;matchingCandidates=[];matchingPreview=null;keepExistingImdb=false;
 const item=state.selected;
 $('.match-modal-card').classList.remove('is-review');$('#match-modal').classList.remove('hidden');$('#match-candidates').classList.remove('hidden');$('#match-candidates').innerHTML='';$('#match-review').classList.add('hidden');
 $('#match-douban-id').value='';$('#match-search-query').value=item.name;$('#match-search-year').value=item.year||'';$('#match-search-kind').value=item.type==='Movie'?'Movie':'Series';
 $('#match-subject').textContent=`为「${item.name}」${item.year||''} 匹配作品`;
 $('#match-source-status').innerHTML='';$('#match-search-status').textContent='按片名搜索，或直接输入豆瓣 ID / 条目链接。年份用于排序，来源可能遗漏作品。';
 $('#match-search-query').focus();
}
function closeMatching(){$('.match-modal-card').classList.remove('is-review');++matchingRequest;matchingPreview=null;$('#match-modal').classList.add('hidden');$('#match-candidates').classList.remove('hidden')}
async function searchMatching(event){
 event?.preventDefault();const request=++matchingRequest;matchingPreview=null;
 $('#match-review').classList.add('hidden');$('#match-candidates').classList.remove('hidden');$('#match-candidates').innerHTML='';
 $('#match-search-status').textContent='正在查询缓存、豆瓣网页与关联作品…';$('#match-search-submit').disabled=true;
 try{
  const body={query:$('#match-search-query').value.trim(),year:$('#match-search-year').value.trim(),kind:$('#match-search-kind').value,douban_id:$('#match-douban-id').value.trim()};
  const data=await api(matchEndpoint('match-search'),{method:'POST',body:JSON.stringify(body)});
  if(request!==matchingRequest)return;
  lastSearchResponse=data;matchingCandidates=data.items||[];$('#match-source-status').innerHTML=[['豆瓣',data.providers?.douban_online],['网页搜索',data.providers?.douban_search],['TMDB',data.providers?.tmdb],['IMDb',data.providers?.omdb]].map(([name,status])=>`<span class="source-chip ${['ok','id_verified'].includes(status)?'good':''}">${name} · ${escapeHtml(matchStatusText(status))}</span>`).join('');
  $('#match-search-status').textContent=`找到 ${matchingCandidates.length} 个候选。请核对版本和季别；搜索范围受来源限制，缺少作品可直接输入豆瓣 ID。`;
  $('#match-candidates').innerHTML=matchingCandidates.length?matchingCandidates.map((r,index)=>`<button type="button" class="match-candidate ${r.blocked?'blocked':''}" data-match-candidate="${index}" ${r.blocked||r.bindable===false?'disabled':''}><div class="candidate-poster">${r.poster?`<img src="${escapeHtml(r.poster)}" alt="${escapeHtml(r.title)}" loading="lazy" referrerpolicy="no-referrer">`:'<span>暂无海报</span>'}</div><div><strong>${escapeHtml(r.title)} <span>${escapeHtml(r.year||'年份未知')}</span></strong><small>${r.kind==='Movie'?'电影':'剧集'} · ${escapeHtml(r.source_label)}</small><div class="id-pills">${r.douban_id?idPill('豆瓣',r.douban_id):''}${r.imdb_id?idPill('IMDb',r.imdb_id):''}${!r.douban_id&&!r.imdb_id?idPill('TMDB',r.tmdb_id):''}</div>${r.douban_score?`<p>豆瓣缓存评分 ${escapeHtml(r.douban_score)}</p>`:''}${r.overview?`<p class="candidate-overview">${escapeHtml(r.overview)}</p>`:''}<small class="candidate-warning">${r.blocked?'版本明显不符':r.bindable===false?'已找到作品，暂未取得豆瓣 / IMDb 关联，不能绑定':r.requires_manual_verification?'条目详情未读取，需人工核对':r.warnings?.includes('imdb_version_difference')?'与当前 IMDb 不是同一版本，请核对':r.warnings?.includes('type_correction')?'确认后纠正 Worker 的匹配类型':r.type_unverified?'类型未核实，请在确认页核对':r.warnings?.length?'标题或年份有差异，请核对原名、季别和演员':'查看并确认此候选'}</small></div><span class="candidate-arrow">→</span></button>`).join(''):`<div class="match-empty"><strong>${Object.values(data.providers||{}).some(v=>!['ok','no_result'].includes(v))?'查询来源暂不可用':'没有找到对应版本'}</strong><p>可改用原名或去掉年份后重试。问题与影片是否有评分分开记录。</p><button type="button" class="secondary-button" id="match-retry">重试搜索</button></div>`;
 }catch(e){if(request===matchingRequest)$('#match-search-status').textContent=e.message}
 finally{if(request===matchingRequest)$('#match-search-submit').disabled=false}
}
async function previewMatching(index,keep=false){
 const candidate=matchingCandidates[index];if(!candidate||candidate.blocked||candidate.bindable===false)return;state.previewCandidateIndex=index;keepExistingImdb=keep;
 const request=++matchingRequest;
 $('#match-review').classList.remove('hidden');$('#match-review').innerHTML='<div class="empty-state">正在验证候选与可用评分…</div>';
 try{
  const preview=await api(matchEndpoint('match-preview'),{method:'POST',body:JSON.stringify({token:candidate.token,keep_existing_imdb:keep})});
  if(request!==matchingRequest)return;
  matchingPreview={...preview,token:candidate.token};$('.match-modal-card').classList.add('is-review');$('#match-candidates').classList.add('hidden');
  const current=boundIds(state.selected), next=preview.effective_ids;
  $('#match-review').innerHTML=`<div class="section-title"><div><h3>确认这部作品</h3><p>${escapeHtml(candidate.title)} · ${escapeHtml(candidate.year)} · ${candidate.kind==='Movie'?'电影':'剧集'}</p></div><button type="button" class="text-button" id="match-back">返回候选</button></div><div class="match-comparison"><div><small>当前匹配</small>${idPill('豆瓣',current.douban)}${idPill('IMDb',current.imdb)}</div><span>→</span><div><small>确认后</small>${idPill('豆瓣',next.douban)}${idPill('IMDb',next.imdb)}</div></div>${preview.changes.some(x=>x.before&&!x.after)?'<p class="match-warning">候选没有完整关联：将清除无法确认属于新作品的旧 ID，避免继续混用其他作品评分。</p>':''}${candidate.existing_imdb_id?`<label class="sync-choice"><input id="keep-current-imdb" type="checkbox" ${keep?'checked':''}><span><b>同时保留当前 IMDb：${escapeHtml(candidate.existing_imdb_title)} (${escapeHtml(candidate.existing_imdb_id)})</b><small>请核对它与所选豆瓣作品为同一版本；此关联需你明确确认。</small></span></label>`:''}<div class="preview-score-grid">${scoreSources.map(s=>`<div><span>${s.name==='RT'?'烂番茄':s.name}</span><b>${escapeHtml(preview.automatic_ratings?.[s.name]||'待获取')}</b><small>${s.key==='douban'?preview.douban_status==='ok'?'本地评分':matchStatusText(preview.douban_status):matchStatusText(preview.omdb_status)}</small></div>`).join('')}</div>${preview.cache_age_days>90?`<p class="match-warning">豆瓣缓存已 ${escapeHtml(preview.cache_age_days)} 天，属于最后已知分数，不是平台实时评分。</p>`:''}${preview.waiting_for_cache?`<p class="match-warning">${preview.douban_status==='json_without_rating'?'缓存已生成，但尚无可用评分。':'绑定后需要等待神医助手生成可用缓存。'}确认绑定会加入封面队列，不代表已经取得评分。</p>`:''}${preview.requires_manual_verification?`<label class="sync-choice manual-check"><input id="match-manual-verified" type="checkbox"><span><b>我已在豆瓣核对条目 ID、作品版本和年份</b><small>条目详情未能自动读取。上方标题和年份仅为当前资源的核对参考；不会猜配 IMDb 或评分。<a href="https://movie.douban.com/subject/${encodeURIComponent(next.douban)}/" target="_blank" rel="noopener noreferrer">打开豆瓣条目核对</a></small></span></label>`:''}${preview.type_changed||preview.type_unverified?`<label class="sync-choice manual-check"><input id="match-type-confirmed" type="checkbox"><span><b>确认按${candidate.kind==='Movie'?'电影':'剧集'}匹配此资源</b><small>Worker 的搜索、缓存校验和评分读取使用此类型；Emby 媒体库类型请在 Emby 中维护。</small></span></label>`:''}<div class="match-score-modes">${scoreSources.filter(s=>preview.manual_scores?.[s.key]).map(s=>`<label>${s.name==='RT'?'烂番茄':s.name} 已有固定分 ${escapeHtml(preview.manual_scores[s.key])}<select id="match-keep-${s.key}"><option value="keep">保留人工固定分</option><option value="automatic">清除固定分，切回自动</option></select></label>`).join('')}</div><label class="sync-choice"><input type="checkbox" id="match-sync-emby" checked><span><b>同时把确认后的 ID 写回 Emby</b><small>默认同步到 Emby。神医助手启用豆瓣辅助刮削且未限流时，才能构建评分缓存；封面随后更新。</small></span></label><div class="match-confirm-actions"><p id="match-confirm-status" aria-live="polite">确认只保存已核实的关联，人工固定分默认保留。</p><button type="button" class="primary-button" id="match-confirm-btn">确认绑定并更新封面</button></div>`;
  $('.match-modal-card').scrollTop=0;
 }catch(e){if(request===matchingRequest)$('#match-review').innerHTML=`<div class="empty-state">${escapeHtml(e.message)}</div>`}
}
async function confirmMatching(){
 if(!matchingPreview)return;
 const preview=matchingPreview, itemId=state.selected.id;
 const body={token:preview.token,confirmed:true,sync_emby:$('#match-sync-emby').checked,score_modes:{},manual_verified:$('#match-manual-verified')?.checked===true,type_confirmed:$('#match-type-confirmed')?.checked===true};
 if(preview.requires_manual_verification&&!body.manual_verified){toast('请先核对豆瓣条目并勾选人工确认',true);return}
 if((preview.type_changed||preview.type_unverified)&&!body.type_confirmed){toast('请勾选确认电影 / 剧集类型',true);return}
 for(const source of scoreSources){const el=$(`#match-keep-${source.key}`);if(el)body.score_modes[source.key]=el.value}
 const button=$('#match-confirm-btn');button.disabled=true;$('#match-confirm-status').textContent=body.sync_emby?'正在保存并回读 Emby 外部 ID…':'正在保存 Worker 绑定…';
 try{
  const result=await api(matchEndpoint('match-confirm'),{method:'POST',body:JSON.stringify(body)});
  transferToBackground(itemId,result.task,true);closeMatching();await openItem(itemId,{preserveDraft:true});toast(result.message);refreshMatchViews();
 }catch(e){$('#match-confirm-status').textContent=e.message;button.disabled=false}
}
function refreshMatchViews(){libraryPages.clear();if(state.page==='library'){if(state.library.filter!=='all')loadMatchingQueue(true);else loadLibrary();}if(state.page==='overview')loadOverview();if(state.page==='sources')loadAccuracy();pollTaskUpdates()}
function pageButtons(current,pages,scope){let numbers=new Set([1,pages,current-1,current,current+1]);let last=0;return [...numbers].filter(n=>n>=1&&n<=pages).sort((a,b)=>a-b).map(n=>{const gap=last&&n-last>1?'<span class="page-gap">…</span>':'';last=n;return gap+`<button type="button" data-page-number="${n}" data-page-scope="${scope}" class="${n===current?'active':''}" ${n===current?'aria-current="page"':''}>${n}</button>`}).join('')}
function renderLibraryPager(){const pages=Math.max(1,Math.ceil(state.library.total/24));$('#library-page-numbers').innerHTML=pageButtons(state.library.page,pages,'library');$('#library-page-jump').max=pages;if(document.activeElement!==$('#library-page-jump'))$('#library-page-jump').value=state.library.page}
function renderTasks(data){
 const host=$('#task-queue-panel');if(!host)return;const view=document.documentElement.classList.contains('item-view-open')?itemReturnView:captureListView();const c=data.counts||{},rows=[...(data.active||[]),...(data.recent||[]).slice(0,6)];
 host.classList.toggle('hidden',!rows.length);
 host.innerHTML=`<details ${(c.queued||c.running)?'open':''}><summary><span><b>后台封面任务</b> <em>${c.running||0} 项处理中 · ${c.queued||0} 项等待</em></span><span>查看进度</span></summary><div class="task-rows">${rows.slice(0,12).map(j=>`<div class="task-row"><span>${escapeHtml(j.name)}</span><small class="task-status ${j.status}">${escapeHtml(taskText(j))}</small>${j.status==='failed'?`<button class="secondary-button" data-retry-task="${escapeHtml(j.id)}">重试</button>`:''}</div>`).join('')}${rows.length>12?`<p>还有 ${rows.length-12} 项后台任务。</p>`:''}</div></details>`;
 const current=state.selected?.id,task=current?[...(data.active||[]),...(data.recent||[])].filter(j=>j.item_id===current).sort((a,b)=>b.updated_at-a.updated_at)[0]:null;
 if(task&&$('#detail-task-state'))$('#detail-task-state').innerHTML='<b>封面任务</b> '+escapeHtml(taskText(task));
 restoreListView(view);
}
async function pollTaskUpdates(){
 if(taskPollBusy||$('#app').classList.contains('hidden'))return;taskPollBusy=true;
 try{
  const data=await api('/api/tasks');renderTasks(data);const changed=lastTaskRevision!==null&&lastTaskRevision!==data.revision;lastTaskRevision=data.revision;
  const refreshDue=state.page==='library'&&state.library.filter!=='all'&&Date.now()-queueLoadedAt>60000;
  if(changed||refreshDue){
   if(state.page==='library'){if(state.library.filter!=='all')await loadMatchingQueue(true);else if(changed)await loadLibrary()}
   if(changed&&state.selected&&!$('#item-modal').classList.contains('hidden')&&$('#match-modal').classList.contains('hidden'))await openItem(state.selected.id,{background:true,preserveDraft:true});
  }
 }catch{}finally{taskPollBusy=false}
}
let queuePage=1, queuePending=null, queueLoadedAt=0, queueRefreshNeeded=false;
const queuePageSize=20;
async function loadMatchingQueue(force=false,background=false){
 const host=$('#matching-queue-rows');if(!host||state.page!=='library'||state.library.filter==='all')return;
 if(state.matchingQueue&&!force&&!background){renderMatchingQueue();return}
 if(queuePending){if(force)queueRefreshNeeded=true;return queuePending}
 const button=$('#refresh-matching-queue');button.disabled=true;
 if(!state.matchingQueue){host.innerHTML='<div class="workbench-empty"><strong>正在整理处理清单…</strong><p>首次读取会核对本地评分缓存，分类切换无需重新检查。</p></div>';$('#matching-queue-summary').textContent='正在读取影视库状态'}
 queuePending=(async()=>{try{
  const data=await api('/api/matching-queue'+(force?'?refresh=1':''));renderSnapshotNotice('library',data.snapshot);if(data.snapshot?.refreshing||data.snapshot?.error){clearTimeout(queueSnapshotRetry);queueSnapshotRetry=setTimeout(()=>loadMatchingQueue(false,true),data.snapshot?.error?15000:1500)}if(data.snapshot?.warming){$('#matching-queue-summary').textContent='后台正在准备清单，页面可继续使用';return}if(data.snapshot?.refreshing){data.items=(data.items||[]).map(row=>submittedRows.get(row.id)||row)}else submittedRows.clear();state.matchingQueue=data;queueLoadedAt=Date.now();
  if(state.page==='library'&&state.library.filter!=='all')renderMatchingQueue();
 }catch(e){$('#matching-queue-summary').textContent='读取失败：'+e.message;toast(e.message,true)}finally{queuePending=null;button.disabled=false;if(queueRefreshNeeded){queueRefreshNeeded=false;loadMatchingQueue(true)}}})();
 return queuePending;
}
function renderMatchingQueue(savedView){
 const view=savedView||(document.documentElement.classList.contains('item-view-open')?itemReturnView:captureListView());
 const data=state.matchingQueue;if(!data)return;
 const needle=state.library.search.toLowerCase(),lib=String(state.library.library_id||'');
 const scope=(data.items||[]).filter(r=>(!needle||`${r.name||''} ${r.original_title||''}`.toLowerCase().includes(needle))&&(!lib||String(r.library_id)===lib));
 const counts={action:0,waiting:0,info:0,ignored:0};scope.forEach(r=>{if(r.triage.status in counts)counts[r.triage.status]++});
 const missingWorks=scope.filter(r=>r.douban_gap&&!r.douban_gap.ignored&&r.douban_gap.scope==='works');
 const missingSeasons=scope.filter(r=>r.douban_gap&&!r.douban_gap.ignored&&r.douban_gap.scope==='seasons');
 const missing=scope.filter(r=>r.douban_gap&&!r.douban_gap.ignored&&r.douban_gap.scope===doubanGapScope);
 $('#matching-queue-summary').textContent=`${counts.action} 项需要处理 · ${counts.waiting} 项等待自动更新 · ${missingWorks.length} 部未忽略作品缺豆瓣分`;
 $('#matching-queue-filters').innerHTML=Object.entries({action:'需要操作',waiting:'等待自动处理',info:'仅供参考',ignored:'已忽略',douban_missing:'缺豆瓣评分'}).map(([key,label])=>`<button class="workbench-filter ${queueFilter===key?'active':''}" role="tab" aria-selected="${queueFilter===key}" data-queue-filter="${key}">${label}<b>${key==='douban_missing'?missingWorks.length:counts[key]}</b></button>`).join('');
 const descriptions={action:'这里只列仍需要你操作的作品；提交成功后移到后台处理。',waiting:'已提交的作品在后台处理，或等待缓存；无需重复匹配。',info:'已有评分、缓存无有效评分或缓存较旧，按需维护；不代表豆瓣网站没有评分。',ignored:'已知缺失和稍后提醒的记录；可以恢复提醒。',douban_missing:'评分覆盖视图：汇总缺豆瓣分的所有未忽略资源，与处理分类独立；不会把已处理条目重新变成待操作。'};
 $('#queue-help').textContent=descriptions[queueFilter];
 $('#douban-gap-tools').classList.toggle('hidden',queueFilter!=='douban_missing');
 if(queueFilter==='douban_missing'){
  const reasons={};missing.forEach(r=>reasons[r.douban_gap.reason]=(reasons[r.douban_gap.reason]||0)+1);
  if(doubanGapReason&&!(doubanGapReason in reasons))reasons[doubanGapReason]=0;
  $('#douban-gap-scope').value=doubanGapScope;
  $('#douban-gap-reason').innerHTML='<option value="">全部原因</option>'+Object.entries(reasons).map(([key,n])=>`<option value="${key}">${escapeHtml(doubanGapLabels[key]||key)} · ${n}</option>`).join('');$('#douban-gap-reason').value=doubanGapReason;
  $('#douban-gap-breakdown').textContent=`未忽略作品 ${missingWorks.length} 部 · 独立季 ${missingSeasons.length} 个。`+Object.entries(reasons).map(([key,n])=>`${doubanGapLabels[key]||key} ${n}`).join('；');
 }
 const rows=queueFilter==='douban_missing'?missing.filter(r=>!doubanGapReason||r.douban_gap.reason===doubanGapReason):scope.filter(r=>r.triage.status===queueFilter),pages=Math.max(1,Math.ceil(rows.length/queuePageSize));queuePage=Math.max(1,Math.min(queuePage,pages));
 const slice=rows.slice((queuePage-1)*queuePageSize,queuePage*queuePageSize);
 $('#matching-queue-rows').innerHTML=slice.length?slice.map(r=>{const problems=queueFilter==='douban_missing'?[{code:'douban_gap',label:r.douban_gap.label,description:r.douban_gap.description,bucket:'coverage',action:r.douban_gap.action}]:r.triage.issues.filter(i=>i.bucket===queueFilter);return `<article class="workbench-row" data-row-id="${escapeHtml(r.id)}"><button class="workbench-poster" data-queue-detail="${escapeHtml(r.id)}" aria-label="查看 ${escapeHtml(r.name)}">${r.poster_url?`<img src="${escapeHtml(r.poster_url)}" alt="" loading="lazy">`:'<span>暂无封面</span>'}</button><div class="workbench-copy"><button class="text-button workbench-title" data-queue-detail="${escapeHtml(r.id)}">${escapeHtml(r.name)}</button><small>${escapeHtml(r.year||'年份未知')} · ${escapeHtml(r.library_name||'')} ${r.type==='Season'?'· 季':''}</small>${r.type==='Season'?'<span class="workbench-kind">季海报</span>':''}${queueFilter==='douban_missing'?`<span class="workbench-kind coverage-state">${escapeHtml({action:'仍需操作',waiting:'后台／缓存等待',info:'仅供参考',healthy:'暂无其他问题'}[r.triage.status]||'评分待补充')}${r.handled?' · 匹配已保存':''}</span>`:''}${queueFilter==='waiting'?`<div class="wait-context">${r.task&&['running','queued'].includes(r.task.status)?'封面任务 · '+escapeHtml(taskStatusLabels[r.task.status]):r.cache_status==='missing_cache_file'?'等待上游缓存 · 非任务排队':'等待自动同步'}${r.binding_at?' · 提交于 '+escapeHtml(dateText(r.binding_at)):''}</div>`:''}${problems.map(i=>`<div class="problem-line"><b>${escapeHtml(i.label)}</b><p>${escapeHtml(i.description)}</p><div class="problem-actions">${i.action==='match'?`<button class="secondary-button" data-queue-match="${escapeHtml(r.id)}">匹配作品 →</button>`:i.action==='refresh'&&i.code==='cache_wait'?`<button class="secondary-button" data-queue-detail="${escapeHtml(r.id)}">查看缓存状态</button>`:i.action==='refresh'&&i.code==='cache_blocked'?`<button class="secondary-button" data-queue-detail="${escapeHtml(r.id)}">查看处理建议</button>`:i.action==='refresh'?`<button class="secondary-button" data-issue-action="refresh" data-issue-id="${escapeHtml(r.id)}">更新评分封面</button>`:i.action==='details'?`<button class="secondary-button" data-queue-detail="${escapeHtml(r.id)}">查看详情与来源</button>`:i.action==='sources'?'<button class="secondary-button" data-fix-sources>检查评分源</button>':''}${queueFilter==='action'||queueFilter==='ignored'?`<button class="text-button" data-issue-action="${queueFilter==='ignored'?'restore':'snooze'}" data-issue-id="${escapeHtml(r.id)}" data-issue-code="${escapeHtml(i.code)}">${queueFilter==='ignored'?'恢复提醒':'稍后提醒'}</button>${queueFilter!=='ignored'?`<button class="text-button muted-action" data-issue-action="dismiss" data-issue-id="${escapeHtml(r.id)}" data-issue-code="${escapeHtml(i.code)}">忽略此项</button>`:''}`:''}</div></div>`).join('')}</div></article>`}).join(''):`<div class="workbench-empty"><strong>${queueFilter==='action'?'没有需要立即处理的问题':'此分类暂无资源'}</strong><p>可调整影视库或搜索条件查看其他资源。</p></div>`;
 $('#queue-page-label').textContent=rows.length?`第 ${queuePage} / ${pages} 页 · 共 ${rows.length} 项（${(queuePage-1)*queuePageSize+1}–${Math.min(queuePage*queuePageSize,rows.length)}）`:'0 项';
 $('#queue-page-numbers').innerHTML=pageButtons(queuePage,pages,'queue');$('#queue-page-jump').max=pages;if(document.activeElement!==$('#queue-page-jump'))$('#queue-page-jump').value=queuePage;
 $('#queue-prev').disabled=queuePage===1;$('#queue-next').disabled=queuePage===pages;
 $('#queue-updated-at').textContent=`上次检查 ${dateText(data.generated_at)}`;
 restoreListView(view);requestAnimationFrame(()=>restoreListView(view));
}
async function handleIssueAction(button){
 button.disabled=true;
 try{const r=await api(`/api/items/${encodeURIComponent(button.dataset.issueId)}/issue-action`,{method:'POST',body:JSON.stringify({action:button.dataset.issueAction,code:button.dataset.issueCode})});if(r.queued)transferToBackground(button.dataset.issueId,r.task);toast(r.queued?'已处理，已移到后台队列；可继续下一部':r.message);loadMatchingQueue(true);if(state.page==='overview')loadOverview()}catch(e){toast(e.message,true)}finally{button.disabled=false}
}
document.addEventListener('change',e=>{const mode=e.target.dataset.scoreMode;if(mode){const input=$(`#edit-${mode}`);input.disabled=e.target.value!=='manual';if(!input.disabled&&!input.value){const source=scoreSources.find(s=>s.key===mode);input.value=state.selected?.ratings?.[source.name]||''}$('#score-save-status').textContent='设置有变化，保存后生效。'}});
document.addEventListener('click',async e=>{
 if(e.target.closest('#open-match-btn'))showMatching();
 const candidate=e.target.closest('[data-match-candidate]');if(candidate)previewMatching(Number(candidate.dataset.matchCandidate));
 if(e.target.closest('[data-close-match]'))closeMatching();
 if(e.target.closest('#match-back')){$('.match-modal-card').classList.remove('is-review');$('#match-review').classList.add('hidden');$('#match-candidates').classList.remove('hidden');matchingPreview=null}
 if(e.target.closest('#match-confirm-btn'))confirmMatching();
 if(e.target.closest('#match-retry'))searchMatching();
 if(e.target.closest('#refresh-matching-queue'))loadMatchingQueue(true);
 if(e.target.closest('#queue-prev')){queuePage--;renderMatchingQueue()}
 if(e.target.closest('#queue-next')){queuePage++;renderMatchingQueue()}
 const detail=e.target.closest('[data-queue-detail]');if(detail)openItem(detail.dataset.queueDetail);
 const fix=e.target.closest('[data-issue-action]');if(fix)handleIssueAction(fix);
 if(e.target.closest('[data-fix-sources]'))navigate('sources');
 const filter=e.target.closest('[data-queue-filter]');if(filter){queueFilter=filter.dataset.queueFilter;queuePage=1;renderMatchingQueue()}
 const entry=e.target.closest('[data-queue-match]');if(entry){await openItem(entry.dataset.queueMatch);if(state.selected?.matching_flags?.some(f=>f!=='ids_without_score'))showMatching()}
});
$('#match-search-form').addEventListener('submit',searchMatching);

document.addEventListener('change',e=>{if(e.target.id==='keep-current-imdb')previewMatching(state.previewCandidateIndex,e.target.checked)});


document.addEventListener('error',e=>{if(e.target.tagName==='IMG'&&e.target.closest('.candidate-poster')){e.target.closest('.candidate-poster').innerHTML='<span>暂无海报</span>'}},true);

document.addEventListener('input',e=>{if(e.target.closest('#item-detail-content')&&e.target.matches('input,select'))updateScoreDirty()});
document.addEventListener('change',e=>{if(e.target.closest('#item-detail-content')&&e.target.matches('input,select')){if(e.target.dataset.scoreMode)$(`#edit-${e.target.dataset.scoreMode}`).disabled=e.target.value!=='manual';updateScoreDirty()}});
document.addEventListener('click',async e=>{
 if(e.target.closest('[data-close-modal]')){closeItemModal();return}
 const page=e.target.closest('[data-page-number]');if(page){if(page.dataset.pageScope==='queue'){queuePage=Number(page.dataset.pageNumber);renderMatchingQueue()}else{state.library.page=Number(page.dataset.pageNumber);loadLibrary()}}
 const jump=e.target.closest('[data-page-jump]');if(jump){const input=$(`#${jump.dataset.pageJump}-page-jump`),number=Math.max(1,Math.min(Number(input.max),Number(input.value)||1));if(jump.dataset.pageJump==='queue'){queuePage=number;renderMatchingQueue()}else{state.library.page=number;loadLibrary()}}
 const retry=e.target.closest('[data-retry-task]');if(retry){retry.disabled=true;try{await api(`/api/tasks/${encodeURIComponent(retry.dataset.retryTask)}/retry`,{method:'POST',body:'{}'});toast('已重新加入后台队列');pollTaskUpdates()}catch(error){toast(error.message,true);retry.disabled=false}}
});

document.addEventListener('keydown',e=>{if(e.key==='Escape'){if(!$('#match-modal').classList.contains('hidden'))closeMatching();else if(!$('#item-modal').classList.contains('hidden'))closeItemModal()}});

function renderSnapshotNotice(page,meta){
 const host=document.querySelector(`[data-page-view="${page}"]`);if(!host)return;let notice=host.querySelector('.snapshot-notice');
 if(!meta||(!meta.refreshing&&!meta.error)){notice?.remove();return}
 if(!notice){notice=document.createElement('div');notice.className='snapshot-notice';notice.setAttribute('role','status');host.querySelector('.page-intro,.hero-panel')?.insertAdjacentElement('afterend',notice);if(!notice.parentNode)host.prepend(notice)}
 notice.classList.toggle('error',!!meta.error);notice.textContent=meta.error?'后台更新暂未完成，继续显示最近快照；稍后自动重试。':meta.warming?'后台正在首次整理数据，你可以继续浏览和操作。':'正在后台更新统计，当前显示最近快照。';
}

$('#douban-gap-scope').addEventListener('change',e=>{doubanGapScope=e.target.value;doubanGapReason='';queuePage=1;renderMatchingQueue()});
$('#douban-gap-reason').addEventListener('change',e=>{doubanGapReason=e.target.value;queuePage=1;renderMatchingQueue()});
document.addEventListener('click',e=>{if(!e.target.closest('[data-open-douban-gaps]'))return;state.library.filter='issues';state.library.search='';state.library.library_id='';$('#library-search').value='';$('#library-select').value='';queueFilter='douban_missing';doubanGapScope='works';doubanGapReason='';queuePage=1;$$('[data-library-filter]').forEach(el=>el.classList.toggle('active',el.dataset.libraryFilter==='issues'));navigate('library')});
