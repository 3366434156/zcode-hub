#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""monitoring.html 四项改造（自验证版）：每步断言，末尾完整性检查不过自动回滚。"""
import sys
import pathlib

sys.stdout.reconfigure(encoding="utf-8")
p = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "admin" / "monitoring.html"
orig = p.read_text(encoding="utf-8")
t = orig

def rep(old, new, n=1, label=""):
    global t
    cnt = t.count(old)
    assert cnt >= n, f"锚点未命中[{label}]: {old[:60]!r} (发现 {cnt} 处)"
    t = t.replace(old, new, n)
    print(f"  ✓ {label}")

# 1. head 内加样式块（monitoring 页原本无 <style>）
rep("  <link href=\"/static/css/app.css?v={{APP_VERSION}}\" rel=\"stylesheet\">",
    """  <link href="/static/css/app.css?v={{APP_VERSION}}" rel="stylesheet">
  <style>
    .st-queued{background:rgba(120,140,255,.15);color:#8ea2ff;padding:2px 8px;border-radius:10px;font-size:12px}
    .st-dispatched{background:rgba(255,180,60,.15);color:#ffb43c;padding:2px 8px;border-radius:10px;font-size:12px}
    .st-done{background:rgba(60,180,120,.15);color:#3cb478;padding:2px 8px;border-radius:10px;font-size:12px}
    .st-failed{background:rgba(230,80,80,.15);color:#e65050;padding:2px 8px;border-radius:10px;font-size:12px}
    .fb-table{width:100%;border-collapse:collapse;font-size:13px}
    .fb-table th,.fb-table td{padding:8px 10px;text-align:left;border-bottom:1px solid rgba(255,255,255,.06)}
    .fb-table th{color:#8b93a7;font-weight:600}
    .fb-tag{color:#8b93a7;font-size:12px;margin-right:4px}
  </style>""", 1, "head 样式块")

# 2. 副标题保留期口径
rep("SQLite 持久化，保留 30 天",
    "SQLite 持久化 · 明细保留 3 天 · Token/成败统计按日聚合永久保留", 1, "副标题")

# 3. 进行中徽章
rep('<span class="live-dot" id="live-dot">实时监控中</span>',
    '<span class="live-dot" id="live-dot">实时监控中</span>\n'
    '        <span class="page-action-btn" id="inflight-badge" style="display:none">进行中 <b id="inflight-n">0</b></span>',
    1, "进行中徽章")

# 4. 换模型卡可点 + 明细容器（插在 Token 时序卡后）
rep('          <canvas id="series-canvas" height="176"></canvas>',
    '''          <canvas id="series-canvas" height="176"></canvas>
        </div>
      </div>

      <div class="settings-card" id="fb-detail-card" style="display:none">
        <div class="section-title" style="margin-bottom:8px">换模型明细（客户端请求 → 实际服务的模型）</div>
        <div id="fb-detail-body"><div class="hint">加载中…</div></div>''',
    1, "换模型明细容器")

# 5. 换模型 KPI 卡可点
rep('<div class="kpi big">\n      <div class="k"><span class="ic">⇋</span>换模型</div>',
    '<div class="kpi big" style="cursor:pointer" onclick="toggleFbDetail()" title="点击查看换模型明细">\n      <div class="k"><span class="ic">⇋</span>换模型</div>',
    1, "换模型卡可点")

# 6. contentCell 函数（esc 定义独立存在，不覆盖）
rep("function esc(s){",
    '''/* 内容/错误列：错误红显；正文按来源打标（系统注入/工具回传/用户消息），单行截断，悬停看全文 */
function contentCell(e){
  const err=String(e.error||'').replace(/\\s+/g,' ').trim();
  if(e.ok===false&&err)return `<span class="err-line" title="${esc(err)}">✗ ${esc(err.slice(0,110))}</span>`;
  const raw=String(e.preview||'').replace(/\\s+/g,' ').trim();
  if(!raw)return '—';
  let tag='用户消息';
  if(/<system-reminder>|<system_warning>|<permission/i.test(raw))tag='系统注入';
  else if(/tool_use_id|"type"\\s*:\\s*"tool_result"/.test(raw))tag='工具回传';
  else if(/^[{\\[]/.test(raw))tag='JSON';
  return `<span class="fb-tag">【${tag}】</span>${esc(raw.slice(0,72))}${raw.length>72?'…':''}`;
}
function esc(s){''', 1, "contentCell")

# 7. 两处内容列调用 → contentCell
cell_old = """<span class="mon-preview" title="${esc(e.error||e.preview||'')}">${e.ok===false&&e.error?`<span class="err-line">${esc(e.error)}</span>`:esc(e.preview||'—')}</span>"""
assert t.count(cell_old) == 2, f"内容列 {t.count(cell_old)} 处"
t = t.replace(cell_old,
    """<span class="mon-preview" title="${esc(e.error||e.preview||'')}">${contentCell(e)}</span>""", 2)
print("  ✓ 内容列 ×2")

# 8. 时序图标签：granularity 自适应
rep("const only = new Date(rows[0].hour*3600000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});",
    "const only = fmtBucket(rows[0]);", 1, "单桶标签")
rep("const hourLabel = i => new Date(rows[i].hour*3600000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});",
    "const hourLabel = i => fmtBucket(rows[i]);", 1, "多桶标签")
rep("function renderSeries(){",
    '''function fmtBucket(r){
  const d=new Date(r.bucket*1000);
  return r.granularity==='day'
    ? d.toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'})
    : d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});
}

function renderSeries(){''', 1, "fmtBucket")

# 9. fb-detail 渲染/开关函数（挂在 fmtNum 前）
rep("function fmtNum(n){",
    '''function toggleFbDetail(){
  const c=document.getElementById('fb-detail-card');
  const show=c.style.display==='none';
  c.style.display=show?'block':'none';
  if(show)renderFbDetail();
}
function renderFbDetail(){
  const box=document.getElementById('fb-detail-body');
  const rows=(state.usage&&state.usage.fallbacks)||[];
  if(!rows.length){box.innerHTML='<div class="hint">这个区间没有换模型记录——只有「请求的模型」被上游判额度用尽/不在套餐时才会自动切换。</div>';return;}
  box.innerHTML=`<table class="fb-table"><thead><tr><th>时间</th><th>账号</th><th>请求 → 实际</th><th>结果</th></tr></thead><tbody>
    ${rows.map(r=>`<tr>
      <td class="hint">${new Date(r.ts*1000).toLocaleString('zh-CN')}</td>
      <td>${esc(r.account||'—')}</td>
      <td><code>${esc(r.requested_model)}</code> → <b>${esc(r.model)}</b></td>
      <td>${outcomeBadge(r.outcome,r.http_status)}</td>
    </tr>`).join('')}</tbody></table>`;
}

function fmtNum(n){''', 1, "fb 渲染函数")

# 10. 加载链：渲染完 KPI 后刷新 fb 明细 + 进行中徽章
rep("    renderKpis(); renderChannels(); renderSeries(); renderHeatmap(); renderTable(); renderRecent();",
    "    renderKpis(); renderChannels(); renderSeries(); renderHeatmap(); renderTable(); renderRecent();\n"
    "    renderFbDetail();\n"
    "    {const nIn=(state.live?0:0, (typeof _lastInflight!=='undefined')?_lastInflight:0);}", 1, "加载链挂 fb")

# 10b. 进行中徽章赋值（state.live 赋值点）
rep("    state.live = (live.entries || []);",
    "    state.live = (live.entries || []);\n"
    "    {const nIn=live.inflight||0;\n"
    "     const b=document.getElementById('inflight-badge');\n"
    "     if(b){b.style.display=nIn>0?'inline-block':'none';\n"
    "           document.getElementById('inflight-n').textContent=nIn;}}", 1, "徽章赋值")

# ── 完整性检查 ──
ok = True
for pat, minimum in (("</style>", 1), ("contentCell", 3), ("fmtBucket", 3),
                     ("toggleFbDetail", 2), ("renderFbDetail", 2),
                     ("inflight-n", 2), ("renderSeries", 3)):
    c = t.count(pat)
    print(f"  {'✓' if c >= minimum else '✗'} {pat} ×{c} (需 ≥{minimum})")
    ok = ok and c >= minimum
if not ok:
    p.write_text(orig, encoding="utf-8")
    print("完整性不过 → 已回滚为原文件")
    sys.exit(1)
p.write_text(t, encoding="utf-8")
print("monitoring.html 四项改造完成")
