#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用提交版的干净尾部结构重建 monitoring.html 的时序卡之后区域。
新增件：fb-detail-card（换模型明细）、标签自适应。带深度自检，失败自动回滚。"""
import subprocess
import sys
import pathlib

sys.stdout.reconfigure(encoding="utf-8")
ROOT = pathlib.Path(__file__).resolve().parents[1]
p = ROOT / "frontend" / "admin" / "monitoring.html"
t = p.read_text(encoding="utf-8")

committed = subprocess.run(
    ["git", "show", "HEAD:frontend/admin/monitoring.html"],
    capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
).stdout
assert committed.count("<section") == committed.count("</section>"), "提交版本身失衡？"

# ── 提交版尾部模板：从 chartbox 到 最近请求 section 闭合 ──
c0 = committed.index('<div class="chartbox">')
h2 = committed.index("<h2>最近请求</h2>")
c_end = committed.index("</section>", h2) + len("</section>")
tail_tpl = committed[c0:c_end]

# ── 当前文件：canvas 行之前截断，重建尾部 ──
cur0 = t.index('<div class="chartbox">')
h2c = t.index("<h2>最近请求</h2>")
c_end_cur = t.index("</section>", h2c) + len("</section>")

# 提交版模板的 canvas 行替换为当前 canvas 行（一致，无需替换——两版相同）
# fb-detail-card 插在 series section 闭合之后
fb_card = '''
      <div class="settings-card" id="fb-detail-card" style="display:none">
        <div class="section-title" style="margin-bottom:8px">换模型明细（客户端请求 → 实际服务的模型）</div>
        <div id="fb-detail-body"><div class="hint">加载中…</div>
      </div>

    </section>

'''  # 注意：保持与提交版缩进/闭合习惯一致（fb-card 由尾部自带的闭合收口）

new_tail = tail_tpl.replace(
    '<canvas id="series-canvas" height="176"></canvas>',
    '<canvas id="series-canvas" height="176"></canvas>\n        </div>\n      </div>\n\n'
    + fb_card.strip()
    + "\n    </section>\n\n    <section class=\"panel\">\n      <div class=\"phead\">\n        <h2>最近请求</h2>\n        <span class=\"hint\">实时活动流</span>\n        <div class=\"spacer\"></div>\n        <span class=\"hm-meta\" id=\"recent-meta\">—</span>\n      </div>\n      <div class=\"pbody\">\n        <div class=\"recent-list\" id=\"recent-list\"><div class=\"empty\">加载中…</div></div>\n      </div>",
    1,
)
new_tail = new_tail[: new_tail.rindex("</section>") + len("</section>")]

t_new = t[:cur0] + new_tail + t[c_end_cur:]

# ── 自检 ──
import re
so, sc = len(re.findall(r"<section\b", t_new)), t_new.count("</section>")
do, dc = len(re.findall(r"<div\b", t_new)), t_new.count("</div>")
checks = {
    "section 配平": so == sc,
    "div 配平": do == dc,
    "fb-detail-card": t_new.count("fb-detail-card") == 2,
    "series-canvas": t_new.count("series-canvas") == 1,
    "最近请求": t_new.count("<h2>最近请求</h2>") == 1,
}
for k, v in checks.items():
    print(("✓" if v else "✗"), k)
if not all(checks.values()):
    print("自检失败，文件保持原样")
    sys.exit(1)
p.write_text(t_new, encoding="utf-8")
print("尾部重建完成：section", f"{so}开{sc}闭", "div", f"{do}开{dc}闭")
