/* Toast 通知 */

/* 剪贴板统一入口：Clipboard API 只在安全上下文（HTTPS / localhost）存在，
   HTTP 局域网访问时回退到隐藏 textarea + execCommand('copy') */
async function copyToClipboard(text){
  if(window.isSecureContext&&navigator.clipboard){
    try{await navigator.clipboard.writeText(text);return true;}catch{}
  }
  const ta=document.createElement('textarea');
  ta.value=text;
  ta.setAttribute('readonly','');
  ta.style.cssText='position:fixed;top:0;left:-9999px;opacity:0';
  document.body.appendChild(ta);
  const sel=getSelection(),prev=sel.rangeCount?sel.getRangeAt(0):null;
  if(/iphone|ipad|ipod/i.test(navigator.userAgent)){
    ta.contentEditable='true';
    const range=document.createRange();
    range.selectNodeContents(ta);
    sel.removeAllRanges();sel.addRange(range);
    ta.setSelectionRange(0,text.length);
  }else{
    ta.select();
    ta.setSelectionRange(0,text.length);
  }
  let ok=false;
  try{ok=document.execCommand('copy');}catch{}
  ta.remove();
  if(prev){sel.removeAllRanges();sel.addRange(prev);}
  return ok;
}

function _toastContainer(){
  let c=document.getElementById('toast-container');
  if(!c){c=document.createElement('div');c.id='toast-container';c.className='toast-container';document.body.appendChild(c);}
  return c;
}
function _esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function showToast(message,type='success'){
  const tone=type==='error'?'error':(type==='info'?'info':'success');
  const icon=tone==='success'
    ?'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>'
    :tone==='error'
      ?'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>'
      :'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>';
  const el=document.createElement('div');
  el.className=`toast toast-${tone}`;
  el.innerHTML=`<div class="toast-icon">${icon}</div><div class="toast-content">${_esc(message)}</div>`;
  _toastContainer().appendChild(el);
  setTimeout(()=>{el.classList.add('out');el.addEventListener('animationend',()=>el.remove(),{once:true});},3000);
}
