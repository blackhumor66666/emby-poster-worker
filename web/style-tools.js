/* Explicit, draft-only precision controls. No keyboard capture outside this editor. */
let styleHistory=[],styleHistoryIndex=-1,styleBaseline='',styleHistoryTimer;
function styleSnapshot(){return {render:structuredClone(state.styleDraft),strategy:$('.preset.active')?.dataset.preset||'compact'}}
function refreshStyleHistory(){
 $('#style-undo').disabled=styleHistoryIndex<=0;$('#style-redo').disabled=styleHistoryIndex>=styleHistory.length-1;
 const dirty=state.styleDraft&&JSON.stringify(styleSnapshot())!==styleBaseline;
 $('#style-draft-status').textContent=dirty?'草稿未保存':'已保存状态';$('#style-draft-status').classList.toggle('dirty',!!dirty);
}
function resetStyleHistory(){if(!state.styleDraft)return;syncSimpleStyle();styleHistory=[styleSnapshot()];styleHistoryIndex=0;styleBaseline=JSON.stringify(styleHistory[0]);refreshStyleHistory()}
function recordStyleHistory(){
 if(!state.styleDraft)return;const current=styleSnapshot();
 if(styleHistoryIndex>=0&&JSON.stringify(styleHistory[styleHistoryIndex])===JSON.stringify(current))return;
 styleHistory=styleHistory.slice(0,styleHistoryIndex+1);styleHistory.push(current);if(styleHistory.length>60)styleHistory.shift();styleHistoryIndex=styleHistory.length-1;refreshStyleHistory();
}
function syncStyleControls(){
 for(const id of ['badge-position','badge-scale','badge-offset-y','rating-position','rating-scale','rating-offset-y'])$(`#${id}`).value=state.styleDraft[id.replaceAll('-','_')];
 updateStylePreview();syncSimpleStyle();refreshStyleHistory();
}
function undoStyle(delta){
 clearTimeout(styleHistoryTimer);recordStyleHistory();const next=styleHistoryIndex+delta;if(next<0||next>=styleHistory.length)return;
 styleHistoryIndex=next;const entry=styleHistory[next];state.styleDraft=structuredClone(entry.render);$$('.preset').forEach(el=>el.classList.toggle('active',el.dataset.preset===entry.strategy));syncStyleControls();
}
function nudgeStyle(action,multiplier=1){
 if(!state.styleDraft)return;const target=$('#style-target').value;const key=$(`#${target}-layout-count`).value;const layout=state.styleDraft[target==='rating'?'rating_layouts':'badge_layouts'][key];
 const step=Number($('#style-step').value)*multiplier;
 if(action==='left'||action==='right')layout.offset_x=Math.max(-120,Math.min(120,layout.offset_x+(action==='left'?-step:step)));
 if(action==='up'||action==='down')layout.offset_y=Math.max(-80,Math.min(target==='badge'?120:80,layout.offset_y+(action==='up'?-step:step)));
 if(action==='larger'||action==='smaller')layout.scale=Math.max(.75,Math.min(1.5,Math.round((layout.scale+(action==='larger'?.05:-.05))*100)/100));
 if(action==='center')layout.offset_x=0;
 if(action==='reset')Object.assign(layout,{offset_x:0,offset_y:target==='badge'?12:0,scale:1});
 updateStylePreview();syncSimpleStyle();recordStyleHistory();
}
$('#style-undo').addEventListener('click',()=>undoStyle(-1));$('#style-redo').addEventListener('click',()=>undoStyle(1));
document.addEventListener('click',event=>{
 const nudge=event.target.closest('[data-style-nudge]');if(nudge)nudgeStyle(nudge.dataset.styleNudge);
 if(event.target.closest('.preset,#reset-style-btn'))queueMicrotask(recordStyleHistory);
});
document.addEventListener('input',event=>{
 if(!event.target.closest('.style-controls'))return;clearTimeout(styleHistoryTimer);styleHistoryTimer=setTimeout(recordStyleHistory,200);refreshStyleHistory();
});
document.addEventListener('keydown',event=>{
 if(state.page!=='style'||!state.styleDraft||!$('#item-modal').classList.contains('hidden')||!$('#password-modal').classList.contains('hidden'))return;
 const editing=event.target.matches('input,textarea,select')||event.target.isContentEditable;
 if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s'){event.preventDefault();saveStyle();return}
 if(editing)return;
 if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='z'){event.preventDefault();undoStyle(event.shiftKey?1:-1);return}
 if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='y'){event.preventDefault();undoStyle(1);return}
 if(event.target.closest('.style-quickbar')){
  const direction={ArrowLeft:'left',ArrowRight:'right',ArrowUp:'up',ArrowDown:'down'}[event.key];
  if(direction){event.preventDefault();nudgeStyle(direction,event.shiftKey?5:1)}
 }
});

function simpleStyleLayouts(){const target=$('#style-target').value;const key=$(`#${target}-layout-count`).value;const layouts=state.styleDraft[target==='rating'?'rating_layouts':'badge_layouts'];return {target,key,layouts,current:layouts[key]}}
function syncSimpleStyle(){
 if(!state.styleDraft)return;const {target,key,layouts,current}=simpleStyleLayouts();
 const position=$('#simple-position');position.innerHTML=target==='rating'?'<option value="bottom">底部</option><option value="top">顶部</option>':'<option value="top-left">顶部靠左</option><option value="top-center">顶部居中</option><option value="top-right">顶部靠右</option>';
 position.value=state.styleDraft[`${target}_position`]||(target==='rating'?'bottom':'top-center');
 $('#simple-size').value=current.scale;$('#simple-size-value').textContent=Math.round(current.scale*100)+'%';
 $('#simple-y').max=target==='badge'?120:80;$('#simple-y').value=current.offset_y;$('#simple-y-value').textContent=current.offset_y+' px';
 const different=new Set(Object.values(layouts).map(l=>JSON.stringify(l))).size>1;
 $('#simple-context').textContent=(target==='rating'?'评分栏':'质量徽章')+' · 当前预览 '+key+(target==='rating'?' 项评分':' 个徽章')+(different?'；不同数量已有独立布局。':'')+($('#simple-apply-all').checked?' 调整大小/上下位置时同步所有数量。':' 只调整当前预览数量。');
}
function applySimpleStyle(field,value){
 if(!state.styleDraft)return;const {layouts,current}=simpleStyleLayouts();
 const targets=$('#simple-apply-all').checked?Object.values(layouts):[current];for(const layout of targets)layout[field]=value;
 updateStylePreview();syncSimpleStyle();clearTimeout(styleHistoryTimer);styleHistoryTimer=setTimeout(recordStyleHistory,150);refreshStyleHistory();
}
$('#style-target').addEventListener('change',syncSimpleStyle);$('#simple-apply-all').addEventListener('change',syncSimpleStyle);
$('#simple-size').addEventListener('input',e=>applySimpleStyle('scale',Number(e.target.value)));
$('#simple-y').addEventListener('input',e=>applySimpleStyle('offset_y',Number(e.target.value)));
$('#simple-position').addEventListener('change',e=>{const target=$('#style-target').value;state.styleDraft[`${target}_position`]=e.target.value;$(`#${target}-position`).value=e.target.value;updateStylePreview();recordStyleHistory()});
for(const id of ['rating-layout-count','badge-layout-count'])$('#'+id).addEventListener('change',syncSimpleStyle);
document.addEventListener('input',e=>{if(e.target.closest('.layout-editor'))syncSimpleStyle()});
