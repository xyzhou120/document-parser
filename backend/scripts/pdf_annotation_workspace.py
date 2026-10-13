"""Build a portable offline original-image annotation workspace, without predictions."""

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path, PureWindowsPath

import pymupdf

FIELDS = {
    "designed_hole_count": "报告设计钻孔数",
    "actual_hole_count": "报告实际施工钻孔数",
    "supplementary_hole_count": "补孔数量",
    "measurement_point_count": "效果测点数量",
    "desorbable_gas_content_measured": "可解吸瓦斯含量实测值",
    "desorbable_gas_content_calculated": "可解吸瓦斯含量计算值",
    "evaluation_unit_range": "评价单元及范围",
}


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def parse_pages(value, page_count):
    pages = set()
    for part in value.split(","):
        numbers = part.strip().split("-")
        if len(numbers) not in {1, 2} or any(not n.isdigit() for n in numbers):
            raise ValueError("Pages must use 1,3-5 syntax")
        first, last = int(numbers[0]), int(numbers[-1])
        if first < 1 or first > last or last > page_count:
            raise ValueError("Requested pages are outside PDF bounds")
        pages.update(range(first, last + 1))
    if not pages or len(pages) > 40:
        raise ValueError("Choose between 1 and 40 pages per workspace")
    return sorted(pages)


def canvas_bbox_to_pdf(box, pixel_width, pixel_height, page_width, page_height):
    values = [*box, pixel_width, pixel_height, page_width, page_height]
    if len(box) != 4 or any(isinstance(x, bool) or not isinstance(x, int | float)
                            or not math.isfinite(x) for x in values):
        raise ValueError("Coordinates and dimensions must be finite numbers")
    if min(pixel_width, pixel_height, page_width, page_height) <= 0:
        raise ValueError("Page geometry must have positive dimensions")
    x0, y0, x1, y1 = box
    if not 0 <= x0 < x1 <= pixel_width or not 0 <= y0 < y1 <= pixel_height:
        raise ValueError("Box must lie within rendered page bounds")
    return [x0 / pixel_width * page_width, y0 / pixel_height * page_height,
            x1 / pixel_width * page_width, y1 / pixel_height * page_height]


def source_from_manifest(manifest_path, sample_id):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    matches = [d for d in manifest["documents"] if str(d["id"]) == str(sample_id)]
    if len(matches) != 1:
        raise ValueError("Sample id must identify exactly one document")
    row = matches[0]
    relative = row.get("copied_file")
    if not relative or Path(relative).is_absolute() or PureWindowsPath(relative).is_absolute():
        raise ValueError("Use a portable manifest with relative copied_file")
    source = (manifest_path.parent / relative.replace("\\", "/")).resolve()
    if not source.is_relative_to(manifest_path.parent) or not source.is_file():
        raise ValueError("Sample must remain within the manifest package")
    if digest(source) != row.get("sha256"):
        raise ValueError("Source SHA256 mismatch")
    return manifest, row, source


def build_workspace(manifest_path, sample_id, pages, out, dpi=144, allow_holdout=False):
    manifest, row, source = source_from_manifest(manifest_path, sample_id)
    if row.get("split") == "holdout" and not allow_holdout:
        raise ValueError("Holdout access requires --allow-holdout and a recorded blind-review assignment")
    if not 72 <= dpi <= 240:
        raise ValueError("DPI must be between 72 and 240")
    out = Path(out).resolve()
    if out.exists():
        raise ValueError("Output must be a new directory; existing annotations are never overwritten")
    with pymupdf.open(source) as pdf:
        if len(pdf) != row.get("pages"):
            raise ValueError("Source page count mismatch")
        requested = parse_pages(pages, len(pdf))
        out.mkdir(parents=True)
        (out / "pages").mkdir()
        geometry = []
        for number in requested:
            page = pdf[number - 1]
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            asset = f"pages/page-{number:04d}.png"
            pix.save(out / asset)
            geometry.append({"page_number": number, "width": page.rect.width,
                             "height": page.rect.height, "rotation": page.rotation,
                             "cropbox": list(page.cropbox), "pixel_width": pix.width,
                             "pixel_height": pix.height, "asset": asset,
                             "image_sha256": digest(out / asset),
                             "coordinate_system": "PDF_POINT",
                             "coordinate_origin": "top_left",
                             "coordinate_space": "rotated_visible_page",
                             "derotation_matrix": list(page.derotation_matrix)})
    document = {k: v for k, v in row.items() if k not in {"source_path", "path", "fields"}}
    document["path"] = row.get("copied_file")
    document["sampling_notes"] = row.get("sampling_notes", "")
    document["page_bounds"] = geometry
    document["fields"] = [{"field_key": key, "status": "DRAFT", "scope": "sampled_pages",
                            "scope_pages": requested, "complete": False, "annotator": "",
                            "adjudicator": "", "verified_at": "", "facts": [],
                            "unreadable_evidence": [], "notes": ""} for key in FIELDS]
    draft = {"schema_version": 1, "checklist_type": manifest.get("checklist_type", "gas_report_agent"),
             "instructions": "Independent original-image draft; never verified by this tool",
             "documents": [document]}
    payload = {"draft": draft, "pages": geometry, "fields": FIELDS,
               "manifest_sha256": digest(Path(manifest_path)), "source_sha256": row["sha256"]}
    for name, value in [("workspace.json", payload), ("starter-draft.json", draft)]:
        (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    embedded = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    (out / "index.html").write_text(HTML.replace("__DATA__", embedded), encoding="utf-8")
    return {"output": str(out), "id": row["id"], "pages_rendered": requested,
            "source_sha256": row["sha256"], "status": "DRAFT", "prediction_imported": False}


HTML = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>原图独立标注工作区</title><style>
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui;background:#eef0f2;color:#182231}
header{padding:16px 24px;background:#142b3a;color:white}h1{margin:0;font-size:22px}
main{display:grid;grid-template-columns:380px minmax(0,1fr);gap:16px;padding:16px}aside{background:white;padding:16px;border-radius:8px}
label{display:block;margin:10px 0 3px}input,select,textarea,button{font:inherit}input,select,textarea{width:100%;padding:7px;border:1px solid #aab4bf;border-radius:4px}
button{padding:7px 10px;margin:5px 3px 0 0;border:1px solid #96a8b5;border-radius:4px;background:white;cursor:pointer}
button.primary{background:#126075;color:white}button:focus-visible{outline:3px solid #edb444}#sheet{position:relative;width:max-content;max-width:100%}canvas{display:block;max-width:100%;height:auto;background:white;touch-action:none;border:1px solid #ccd3d9}#help{max-width:90ch}small{color:#4b5c6b}pre{white-space:pre-wrap;max-height:180px;overflow:auto;background:#f3f6f8;padding:8px}#message{color:#a13125}section{min-width:0} @media(max-width:900px){main{grid-template-columns:1fr}aside{max-width:none}}
</style><header><h1>原图独立标注 · 草稿工作区</h1><div id="identity"></div><div id="help">只输出 DRAFT。请独立查看原图；工具不加载模型预测。框选按当前可见页左上为原点，以 PDF_POINT 保存，保留页面旋转信息。</div></header>
<main><aside><label for="actor">标注人</label><input id="actor" autocomplete="name">
<label for="field">字段</label><select id="field"></select><label for="facts">当前字段事实</label><select id="facts"></select>
<button id="new">新增事实</button><button id="delete">删除事实</button>
<label for="raw">原始值（逐字抄录）</label><input id="raw"><label for="norm">归一值（不填则保留原值）</label><input id="norm">
<label for="unit">单位（没有则留空）</label><input id="unit">
<label for="dimensions">对象维度 JSON</label><textarea id="dimensions" rows="3">{}</textarea><small>workface / evaluation_unit / measurement_point / roadway / measurement_date / scope_level；没有明示不要猜。</small>
<label for="kind">值类型</label><select id="kind"><option>unknown</option><option>design</option><option>actual</option><option>measured</option><option>calculated</option><option>reported</option></select>
<label for="source">来源角色</label><select id="source"><option>unknown</option><option>report_body</option><option>report_table</option><option>test_report</option><option>design</option><option>ledger</option><option>appendix</option></select>
<label for="role">下一框的证据角色</label><select id="role"><option>value</option><option>unit</option><option value="column_header">header（列头）</option><option value="row_header">header（行头）</option><option>scope</option></select>
<label for="quote">下一框的原图文字（可留空）</label><input id="quote">
<button id="savefact" class="primary">保存事实编辑</button><button id="undo">撤销最后证据框</button><pre id="evidence"></pre>
<label for="notes">字段备注/分歧/未读范围</label><textarea id="notes" rows="2"></textarea>
<button id="timer">开始计时</button><small id="time">0 秒</small><p><button id="download" class="primary">下载 DRAFT JSON</button><button id="log">下载计时 JSON</button></p>
<label for="import">继续已有 DRAFT（本地文件）</label><input type="file" id="import" accept="application/json,.json"><p id="message" role="status" aria-live="polite"></p></aside>
<section><label for="page">已渲染页（如需其他页重新生成新工作区）</label><select id="page"></select><p id="geometry"></p><div id="sheet"><canvas id="canvas" aria-label="原图页；鼠标拖动框选，PDF_POINT坐标保存"></canvas></div></section></main>
<script type="application/json" id="data">__DATA__</script><script>
'use strict';const config=JSON.parse(document.getElementById('data').textContent);let draft=structuredClone(config.draft),currentFact=-1,image=new Image(),down=null,elapsed=0,running=false,last=performance.now();
const $=id=>document.getElementById(id),doc=()=>draft.documents[0];let activeKey=Object.keys(config.fields)[0];const field=()=>doc().fields.find(f=>f.field_key===activeKey),page=()=>config.pages.find(p=>String(p.page_number)===$('page').value),fact=()=>field().facts[currentFact];
const note=s=>{$('message').textContent=s;};
function option(select,value,label){const o=document.createElement('option');o.value=value;o.textContent=label;select.append(o);}
for(const [key,label]of Object.entries(config.fields))option($('field'),key,label);for(const p of config.pages)option($('page'),p.page_number,`第 ${p.page_number} 页`);
$('identity').textContent=`样本 ${doc().id} / ${doc().pages} 页 / SHA256 ${doc().sha256}`;
function syncNotes(){field().notes=$('notes').value;field().annotator=$('actor').value.trim();}
function refreshFacts(){const select=$('facts');select.replaceChildren();field().facts.forEach((f,i)=>option(select,i,`${i+1}. ${f.raw_value||'未填值'}`));select.value=currentFact;loadFact();}
function loadFact(){const f=fact();$('raw').value=f?.raw_value??'';$('norm').value=f?.normalized_value??'';$('unit').value=f?.unit??'';$('dimensions').value=JSON.stringify(f?.dimensions??{},null,2);$('kind').value=f?.value_kind??'unknown';$('source').value=f?.source_role??'unknown';$('notes').value=field().notes||'';$('evidence').textContent=JSON.stringify(f?.evidence??[],null,2);draw();}
function commit(){syncNotes();const f=fact();if(!f)return;let dimensions=JSON.parse($('dimensions').value||'{}');if(!dimensions||Array.isArray(dimensions)||typeof dimensions!=='object')throw Error('维度必须是 JSON 对象');f.raw_value=$('raw').value;f.normalized_value=$('norm').value||$('raw').value;f.unit=$('unit').value||null;f.dimensions=dimensions;f.value_kind=$('kind').value;f.source_role=$('source').value;}
function act(fn){try{fn();note('');}catch(error){note(error.message);}}
$('savefact').onclick=()=>act(()=>{commit();refreshFacts();});$('new').onclick=()=>act(()=>{commit();field().facts.push({raw_value:'',normalized_value:'',unit:null,dimensions:{},value_kind:'unknown',source_role:'unknown',evidence:[]});currentFact=field().facts.length-1;refreshFacts();});
$('delete').onclick=()=>{if(currentFact>=0)field().facts.splice(currentFact,1);currentFact=field().facts.length?0:-1;refreshFacts();};$('facts').onchange=()=>act(()=>{const selected=Number($('facts').value);commit();currentFact=selected;loadFact();});$('field').onchange=()=>act(()=>{commit();activeKey=$('field').value;currentFact=field().facts.length?0:-1;refreshFacts();});$('undo').onclick=()=>{fact()?.evidence.pop();$('evidence').textContent=JSON.stringify(fact()?.evidence??[],null,2);draw();};
const canvas=$('canvas'),ctx=canvas.getContext('2d');function draw(preview){if(!image.complete||!image.naturalWidth)return;ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(image,0,0);for(const ev of fact()?.evidence??[]){if(ev.page_number!==page().page_number)continue;const p=page(),b=ev.bbox;ctx.strokeStyle={value:'#e24027',unit:'#247cca',column_header:'#9144bd',row_header:'#9144bd',scope:'#169652'}[ev.role]||'#222';ctx.lineWidth=Math.max(2,canvas.width/500);ctx.strokeRect(b[0]/p.width*canvas.width,b[1]/p.height*canvas.height,(b[2]-b[0])/p.width*canvas.width,(b[3]-b[1])/p.height*canvas.height);}if(preview){ctx.strokeStyle='#111';ctx.lineWidth=2;ctx.strokeRect(preview[0],preview[1],preview[2]-preview[0],preview[3]-preview[1]);}}
function loadPage(){const p=page();image=new Image();image.onload=()=>{canvas.width=p.pixel_width;canvas.height=p.pixel_height;draw();};image.src=p.asset;$('geometry').textContent=`PDF_POINT ${p.width.toFixed(2)} × ${p.height.toFixed(2)}；旋转 ${p.rotation}°；可见页左上原点；PNG SHA256 ${p.image_sha256}`;}
$('page').onchange=loadPage;function xy(e){const r=canvas.getBoundingClientRect();return [Math.max(0,Math.min(canvas.width,(e.clientX-r.left)*canvas.width/r.width)),Math.max(0,Math.min(canvas.height,(e.clientY-r.top)*canvas.height/r.height))];}
canvas.onpointerdown=e=>{if(!fact())return note('请先新增或选择一条事实');down=xy(e);canvas.setPointerCapture(e.pointerId);};canvas.onpointermove=e=>{if(down){const end=xy(e);draw([Math.min(down[0],end[0]),Math.min(down[1],end[1]),Math.max(down[0],end[0]),Math.max(down[1],end[1])]);}};
canvas.onpointerup=e=>{if(!down)return;const end=xy(e),start=down;down=null;if(Math.abs(start[0]-end[0])<2||Math.abs(start[1]-end[1])<2){draw();return;}const p=page(),bbox=[Math.min(start[0],end[0])/canvas.width*p.width,Math.min(start[1],end[1])/canvas.height*p.height,Math.max(start[0],end[0])/canvas.width*p.width,Math.max(start[1],end[1])/canvas.height*p.height];fact().evidence.push({page_number:p.page_number,bbox,coordinate_system:'PDF_POINT',coordinate_space:p.coordinate_space,page_rotation:p.rotation,role:$('role').value,quote:$('quote').value,table_id:null,image_sha256:p.image_sha256});$('evidence').textContent=JSON.stringify(fact().evidence,null,2);draw();};canvas.onpointercancel=()=>{down=null;draw();};
function tick(){const now=performance.now();if(running&&!document.hidden)elapsed+=Math.min((now-last)/1000,2);last=now;$('time').textContent=`${Math.round(elapsed)} 秒（前台人工计时）`;};setInterval(tick,1000);document.addEventListener('visibilitychange',()=>{tick();});$('timer').onclick=()=>{tick();running=!running;$('timer').textContent=running?'暂停计时':'继续计时';};
function download(value,name){const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$('download').onclick=()=>act(()=>{commit();for(const f of doc().fields){f.status='DRAFT';f.complete=false;f.adjudicator='';f.verified_at='';}download(draft,`sample-${doc().id}-DRAFT.json`);note('已下载 DRAFT，尚未独立复核，不能用于准确率结论。');});
$('log').onclick=()=>{tick();download({schema_version:1,manual_work:[{phase:'annotation',document_id:doc().id,annotator:$('actor').value,active_seconds:Math.round(elapsed),facts:doc().fields.reduce((n,f)=>n+f.facts.length,0),scope_pages:config.pages.map(p=>p.page_number),status:'DRAFT'}]},`sample-${doc().id}-timing.json`);};
$('import').onchange=async()=>{try{const value=JSON.parse(await $('import').files[0].text()),d=value.documents?.[0];if(value.documents.length!==1||String(d.id)!==String(doc().id)||d.sha256!==doc().sha256)throw Error('只能导入本样本同 SHA256 草稿');if(!Array.isArray(d.fields)||d.fields.length!==Object.keys(config.fields).length||new Set(d.fields.map(f=>f.field_key)).size!==d.fields.length)throw Error('需要完整且不重复的七字段草稿');for(const f of d.fields){if(!(f.field_key in config.fields)||!Array.isArray(f.facts))throw Error('字段结构不匹配');f.scope='sampled_pages';f.scope_pages=[...new Set([...(f.scope_pages||[]),...config.pages.map(p=>p.page_number)])].sort((a,b)=>a-b);f.status='DRAFT';f.complete=false;f.adjudicator='';f.verified_at='';}d.page_bounds=[...new Map([...(d.page_bounds||[]),...config.pages].map(p=>[p.page_number,p])).values()];draft=value;currentFact=-1;refreshFacts();note('已导入并保持 DRAFT；请核对已有框。');}catch(error){note(error.message);}};refreshFacts();loadPage();
</script></html>'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--pages", required=True, help="Explicit 1,3-5 page selection; maximum 40")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=144)
    parser.add_argument("--allow-holdout", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = build_workspace(args.manifest, args.id, args.pages, args.out, args.dpi, args.allow_holdout)
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
