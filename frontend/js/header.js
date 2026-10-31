/* 后台公共头部渲染 */
const REPO_URL = 'https://github.com/3366434156/zcode-hub';
async function renderAdminHeader(){
  const mount=document.getElementById('admin-header');
  if(!mount)return;
  let version='';
  try{const r=await fetch('/meta');if(r.ok)version='v'+(await r.json()).version;}catch{}
  const active=mount.dataset.active||location.pathname;
  const nav=[
    ['/admin/accounts','账号池'],
    ['/admin/monitoring','请求监控'],
    ['/admin/logs','网关日志'],
    ['/admin/settings','设置'],
  ].map(([href,label])=>
    `<a href="${href}" class="admin-nav-link${href===active?' active':''}">${label}</a>`
  ).join('');
  mount.innerHTML=`
    <header class="admin-header">
      <div class="admin-header-inner">
        <div class="admin-brand-wrap">
          <span class="admin-brand">Z<span class="tick">·</span>HUB</span>
        </div>
        <nav class="admin-nav">${nav}</nav>
        <div class="admin-header-right">
          ${version?`<span class="admin-header-version">${version}</span>`:''}
          <a href="${REPO_URL}" target="_blank" rel="noopener noreferrer"
             class="admin-header-icon-btn" title="GitHub 仓库 · ${REPO_URL}" aria-label="打开 GitHub 仓库">
            <svg class="gh-mark" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.012 8.012 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/></svg>
          </a>
          <button onclick="adminLogout()" class="admin-header-icon-btn" title="退出登录" aria-label="退出登录">
            <svg viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>
          </button>
        </div>
      </div>
    </header>`;
}
