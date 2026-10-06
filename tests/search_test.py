"""原文检索辅助遮盖的端到端测试（不进镜像，仅开发环境用）。"""
import io
import sys

import fitz
from fastapi.testclient import TestClient

sys.path.insert(0, "/workspace")
from app.main import app, MAX_SEARCH_HITS  # noqa: E402

client = TestClient(app)
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail else ""))


def make_pdf(path, *, pages=3, rotations=(0, 90, 270), cropbox=None):
    doc = fitz.open()
    for i in range(pages):
        w, h = 595, 842
        page = doc.new_page(width=w, height=h)
        page.draw_rect(fitz.Rect(0, 0, w, h), color=(0.9, 0.9, 0.9),
                       fill=(0.9, 0.9, 0.9), width=0)
        for j in range(6):
            page.insert_text((72, 72 + j * 40),
                             f"SECRET page {i + 1} line {j} TOPSECRET",
                             fontsize=24, color=(0.1, 0.1, 0.5))
        page.set_rotation(rotations[i % len(rotations)])
        if cropbox is not None and i == 0:
            page.set_cropbox(fitz.Rect(*cropbox))
    doc.save(path)
    doc.close()


def px(pix, x_pt, y_pt, zoom=2):
    x, y = int(x_pt * zoom), int(y_pt * zoom)
    off = y * pix.stride + x * pix.n
    return tuple(pix.samples[off:off + 3])


def upload(path="/tmp/search.pdf", **kw):
    make_pdf(path, **kw)
    with open(path, "rb") as f:
        r = client.post("/api/upload",
                        files={"file": (path.rsplit("/", 1)[-1], f, "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def search(sid, query):
    return client.post(f"/api/{sid}/search", json={"query": query})


def apply(sid, search_id, hit_ids):
    return client.post(f"/api/{sid}/search/apply",
                       json={"searchId": search_id, "hitIds": hit_ids})


def versions(sid):
    return client.get(f"/api/{sid}/state").json()["versions"]


def baked_version(sid, page):
    r = client.get(f"/api/{sid}/page/{page}.png?zoom=1&redacted=true")
    assert r.status_code == 200
    return int(r.headers["X-Box-Version"])


print("== 1. 检索基本行为：非空校验、字面匹配、排序、不改变状态 ==")
up = upload()
sid = up["session"]

r = search(sid, "")
check("空短语 422", r.status_code == 422, str(r.status_code))
r = search(sid, "   ")
check("纯空白短语 400", r.status_code == 400 and "空" in r.json()["detail"],
      str(r.status_code))
r = search(sid, "x" * 201)
check("超长短语 422", r.status_code == 422, str(r.status_code))

before_state = client.get(f"/api/{sid}/state").json()
before_pdf = client.get(f"/api/{sid}/download").content

r = search(sid, "TOPSECRET")
check("检索 200", r.status_code == 200, str(r.status_code))
res = r.json()
check("3 页 × 6 行 = 18 处命中", res["hitCount"] == 18, str(res["hitCount"]))
check("未截断", res["truncated"] is False)
hits = res["hits"]
check("命中 id 唯一", len({h["id"] for h in hits}) == len(hits))
check("按页码排序", [h["page"] for h in hits] == sorted(h["page"] for h in hits))
per_page = {}
for h in hits:
    per_page.setdefault(h["page"], []).append((h["rect"]["y0"], h["rect"]["x0"]))
check("页内自上而下排序",
      all(v == sorted(v) for v in per_page.values()))
check("每页 6 处", all(len(v) == 6 for v in per_page.values()))
check("命中在页面范围内",
      all(0 <= h["rect"]["y0"] <= up["pages"][h["page"]]["height"] and
          0 <= h["rect"]["x0"] <= up["pages"][h["page"]]["width"] for h in hits))
check("返回各页版本快照",
      res["pageVersions"] == {"0": 0, "1": 0, "2": 0}, str(res["pageVersions"]))

r = search(sid, "topsecret")   # 大小写不敏感
check("小写同样命中 18 处", r.json()["hitCount"] == 18)
r = search(sid, "l.ne")
check("字面匹配（非正则）", r.json()["hitCount"] == 0)
r = search(sid, "page 2 line 3")
r2 = r.json()
check("短语命中唯一且在第 2 页",
      r2["hitCount"] == 1 and r2["hits"][0]["page"] == 1, str(r2["hits"]))
r = search(sid, "zzz-no-such-text")
check("无命中 hitCount=0", r.json()["hitCount"] == 0 and r.json()["hits"] == [])

after_state = client.get(f"/api/{sid}/state").json()
check("检索不推进任何版本", after_state["versions"] == before_state["versions"])
check("检索不改变复核状态", after_state["reviews"] == before_state["reviews"])
check("检索不影响交付包状态", after_state["delivery"] == before_state["delivery"])
# 下载字节含随机 /ID，逐页比较渲染像素即可证明副本内容未被检索改变
after_pdf = client.get(f"/api/{sid}/download").content
d_before = fitz.open(stream=before_pdf, filetype="pdf")
d_after = fitz.open(stream=after_pdf, filetype="pdf")
same_pixels = all(
    d_before[i].get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False).samples
    == d_after[i].get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False).samples
    for i in range(d_before.page_count))
check("检索后下载副本内容不变（逐页像素一致）", same_pixels)
d_before.close(); d_after.close()

print("== 2. 旋转页（90°）命中位置与预览像素对齐 ==")
r = search(sid, "page 2 line 3")
res = r.json()
hit = res["hits"][0]
check("命中在第 2 页（旋转 90°）", hit["page"] == 1)
r = apply(sid, res["searchId"], [hit["id"]])
check("提交 200，添加 1 框", r.status_code == 200 and r.json()["added"] == 1,
      str(r.status_code))
check("addedPages 仅第 2 页", r.json()["addedPages"] == [1])
png = client.get(f"/api/{sid}/page/1.png?zoom=2&redacted=true").content
pix = fitz.Pixmap(io.BytesIO(png))
rc = hit["rect"]
cx, cy = (rc["x0"] + rc["x1"]) / 2, (rc["y0"] + rc["y1"]) / 2
check("旋转页命中中心像素=黑", px(pix, cx, cy) == (0, 0, 0), str(px(pix, cx, cy)))
check("旋转页命中左侧像素=黑", px(pix, rc["x0"] + 1, cy) == (0, 0, 0))
check("旋转页命中区域外=浅灰", px(pix, cx, rc["y1"] + 30)[0] > 200,
      str(px(pix, cx, rc["y1"] + 30)))
# 下载副本同位置也是黑的
red = fitz.open(stream=client.get(f"/api/{sid}/download").content, filetype="pdf")
rpix = red[1].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
check("下载副本旋转页命中处=黑", px(rpix, cx, cy) == (0, 0, 0))
red.close()

print("== 3. 裁切页（CropBox）命中位置对齐 ==")
doc = fitz.open()
page = doc.new_page(width=595, height=842)
page.draw_rect(fitz.Rect(0, 0, 595, 842), color=(0.9, 0.9, 0.9),
               fill=(0.9, 0.9, 0.9), width=0)
page.insert_text((150, 200), "CROPSECRET visible text", fontsize=20,
                 color=(0.1, 0.1, 0.5))
page.set_cropbox(fitz.Rect(100, 100, 500, 700))
doc.save("/tmp/search_crop.pdf")
doc.close()
with open("/tmp/search_crop.pdf", "rb") as f:
    r = client.post("/api/upload", files={"file": ("crop.pdf", f, "application/pdf")})
csid = r.json()["session"]
r = search(csid, "CROPSECRET")
res = r.json()
check("裁切页命中 1 处", res["hitCount"] == 1)
hit = res["hits"][0]
rc = hit["rect"]
# 文字在 MediaBox (150,200) 处，CropBox 原点 (100,100) -> 显示坐标约 (50,78)
check("命中显示坐标约为 (50,~78)",
      abs(rc["x0"] - 50) < 2 and 70 < rc["y0"] < 85, str(rc))
r = apply(csid, res["searchId"], [hit["id"]])
check("裁切页提交 200", r.status_code == 200 and r.json()["added"] == 1)
pix = fitz.Pixmap(io.BytesIO(client.get(
    f"/api/{csid}/page/0.png?zoom=2&redacted=true").content))
cx, cy = (rc["x0"] + rc["x1"]) / 2, (rc["y0"] + rc["y1"]) / 2
check("裁切页命中中心像素=黑", px(pix, cx, cy) == (0, 0, 0), str(px(pix, cx, cy)))
check("裁切页命中区域外=浅灰", px(pix, cx, rc["y1"] + 40)[0] > 200)

print("== 4. 提交只推进添框页版本：复核失效仅限这些页、旧交付包撤销 ==")
up = upload("/tmp/search4.pdf", rotations=(0, 0, 0))
sid = up["session"]
for p in range(3):
    client.post(f"/api/{sid}/review/confirm",
                json={"page": p, "version": baked_version(sid, p)})
r = client.post(f"/api/{sid}/delivery", params={"filename": "s4.pdf"})
check("定版包生成 200", r.status_code == 200)
did = r.json()["deliveryId"]

r = search(sid, "TOPSECRET")
res = r.json()
page0_hits = [h["id"] for h in res["hits"] if h["page"] == 0]
r = apply(sid, res["searchId"], page0_hits)
out = r.json()
check("提交 200，添加 6 框", r.status_code == 200 and out["added"] == 6, str(out.get("added")))
check("仅第 1 页添框", out["addedPages"] == [0])
check("回包携带 boxes/versions/reviews/delivery",
      all(k in out for k in ("boxes", "versions", "reviews", "delivery")))
st = client.get(f"/api/{sid}/state").json()
check("第 1 页版本 0->1", st["versions"]["0"] == "1" or st["versions"]["0"] == 1)
check("第 2、3 页版本不变",
      st["versions"]["1"] == 0 and st["versions"]["2"] == 0)
check("第 1 页确认失效", st["reviews"]["0"]["confirmed"] is False)
check("第 2、3 页确认保留",
      st["reviews"]["1"]["confirmed"] is True and st["reviews"]["2"]["confirmed"] is True)
check("旧交付包被撤销", st["delivery"]["revoked"] is True)
check("旧包下载 410",
      client.get(f"/api/{sid}/delivery/{did}").status_code == 410)
boxes0 = st["boxes"]["0"]
check("第 1 页 6 个框均为黑色",
      len(boxes0) == 6 and all(b["color"] == "black" for b in boxes0))

print("== 5. 冲突：检索后所选页的框变化 -> 整批拒绝，不修改任何页面 ==")
up = upload("/tmp/search5.pdf", rotations=(0, 0, 0))
sid = up["session"]
r = search(sid, "TOPSECRET")
res = r.json()
# 检索后在第 2 页手工加框
client.post(f"/api/{sid}/boxes", json={
    "page": 1, "rect": {"x0": 10, "y0": 10, "x1": 60, "y1": 60, "color": "red"}})
sel = [h["id"] for h in res["hits"] if h["page"] in (0, 1)]
r = apply(sid, res["searchId"], sel)
check("整批拒绝 409", r.status_code == 409, str(r.status_code))
check("提示重新检索", "重新检索" in r.json()["detail"], r.json()["detail"])
st = client.get(f"/api/{sid}/state").json()
check("第 1 页未添任何框（整批拒绝）", st["boxes"]["0"] == [])
check("第 2 页只有手工框", len(st["boxes"]["1"]) == 1)
check("第 1 页版本仍为 0", st["versions"]["0"] == 0)
check("第 2 页版本=1（仅手工改动）", st["versions"]["1"] == 1)
# 重新检索后提交成功
r = search(sid, "TOPSECRET")
res2 = r.json()
sel = [h["id"] for h in res2["hits"] if h["page"] in (0, 1)]
r = apply(sid, res2["searchId"], sel)
check("重新检索后提交 200", r.status_code == 200 and r.json()["added"] == 12,
      str(r.status_code))

print("== 6. 只接受本会话该次检索的命中 ==")
r = search(sid, "TOPSECRET")
sid_a = r.json()["searchId"]
r = search(sid, "SECRET")
res_b = r.json()
r = apply(sid, sid_a, ["h0"])
check("旧 searchId 409", r.status_code == 409, str(r.status_code))
r = apply(sid, res_b["searchId"], ["h999"])
check("未知命中 id 400", r.status_code == 400, str(r.status_code))
r = apply(sid, "deadbeef", ["h0"])
check("伪造 searchId 409", r.status_code == 409, str(r.status_code))
st = client.get(f"/api/{sid}/state").json()
check("非法提交未改变版本", st["versions"] == versions(sid))

print("== 7. 完全覆盖跳过；部分覆盖仍添加；只推进实际添框页 ==")
up = upload("/tmp/search7.pdf", rotations=(0, 0, 0))
sid = up["session"]
r = search(sid, "TOPSECRET")
res = r.json()
hit0 = next(h for h in res["hits"] if h["page"] == 0)
rc0 = hit0["rect"]
# 手工框完全覆盖第 1 页首个命中（外扩 2 点）
client.post(f"/api/{sid}/boxes", json={"page": 0, "rect": {
    "x0": rc0["x0"] - 2, "y0": rc0["y0"] - 2,
    "x1": rc0["x1"] + 2, "y1": rc0["y1"] + 2, "color": "black"}})
hit1 = next(h for h in res["hits"] if h["page"] == 1)
rc1 = hit1["rect"]
# 手工框只覆盖第 2 页首个命中的左半
client.post(f"/api/{sid}/boxes", json={"page": 1, "rect": {
    "x0": rc1["x0"], "y0": rc1["y0"],
    "x1": (rc1["x0"] + rc1["x1"]) / 2, "y1": rc1["y1"], "color": "black"}})
v_before = versions(sid)
# 重新检索取得新快照；只提交被完全覆盖的命中 -> 不改版本
r = search(sid, "TOPSECRET")
res2 = r.json()
covered_hit = next(h for h in res2["hits"]
                   if h["page"] == 0 and h["rect"] == rc0)
r = apply(sid, res2["searchId"], [covered_hit["id"]])
out = r.json()
check("全被覆盖：added=0", r.status_code == 200 and out["added"] == 0)
check("全被覆盖：skippedCovered=1", out["skippedCovered"] == 1)
check("全被覆盖：版本不变", versions(sid) == v_before)
# 混合提交：被覆盖的第 1 页命中 + 半覆盖的第 2 页命中 + 未覆盖的第 3 页命中
hit2 = next(h for h in res2["hits"] if h["page"] == 2)
half_hit = next(h for h in res2["hits"] if h["page"] == 1 and h["rect"] == rc1)
r = apply(sid, res2["searchId"],
          [covered_hit["id"], half_hit["id"], hit2["id"]])
out = r.json()
check("混合提交 added=2（覆盖的跳过）", out["added"] == 2, str(out))
check("混合提交 skippedCovered=1", out["skippedCovered"] == 1)
check("只推进第 2、3 页", out["addedPages"] == [1, 2])
v_after = versions(sid)
check("第 1 页版本未再推进", v_after["0"] == v_before["0"])
check("第 2、3 页版本各 +1",
      v_after["1"] == v_before["1"] + 1 and v_after["2"] == v_before["2"] + 1)

print("== 8. 空选择 / 无命中提交：200 但不改版本 ==")
up = upload("/tmp/search8.pdf", rotations=(0, 0, 0))
sid = up["session"]
r = search(sid, "TOPSECRET")
res = r.json()
v_before = versions(sid)
r = apply(sid, res["searchId"], [])
check("空选择 200 added=0", r.status_code == 200 and r.json()["added"] == 0,
      str(r.status_code))
r = search(sid, "zzz-none")
res_none = r.json()
r = apply(sid, res_none["searchId"], [])
check("无命中提交 200 added=0", r.status_code == 200 and r.json()["added"] == 0)
check("版本始终不变", versions(sid) == v_before)
st = client.get(f"/api/{sid}/state").json()
check("无任何框", all(st["boxes"][str(p)] == [] for p in range(3)))

print("== 9. 跨行短语：每个行片段各为一个命中，提交后都变黑 ==")
doc = fitz.open()
page = doc.new_page(width=595, height=842)
page.draw_rect(fitz.Rect(0, 0, 595, 842), color=(0.9, 0.9, 0.9),
               fill=(0.9, 0.9, 0.9), width=0)
page.insert_textbox(fitz.Rect(72, 220, 200, 400),
                    "word1 word2 word3 word4 word5 word6 word7 word8",
                    fontsize=14, color=(0.1, 0.1, 0.5))
doc.save("/tmp/search9.pdf")
doc.close()
with open("/tmp/search9.pdf", "rb") as f:
    r = client.post("/api/upload", files={"file": ("w.pdf", f, "application/pdf")})
wsid = r.json()["session"]
r = search(wsid, "word2 word3 word4")
res = r.json()
check("跨行短语产生 2 个行片段命中", res["hitCount"] == 2, str(res["hitCount"]))
r = apply(wsid, res["searchId"], [h["id"] for h in res["hits"]])
check("两个片段都成框", r.json()["added"] == 2)
pix = fitz.Pixmap(io.BytesIO(client.get(
    f"/api/{wsid}/page/0.png?zoom=2&redacted=true").content))
ok = True
for h in res["hits"]:
    rc = h["rect"]
    c = px(pix, (rc["x0"] + rc["x1"]) / 2, (rc["y0"] + rc["y1"]) / 2)
    ok = ok and c == (0, 0, 0)
check("两行片段中心像素均=黑", ok)

print("== 10. 命中数上限截断 ==")
doc = fitz.open()
page = doc.new_page(width=595, height=842)
for j in range(60):
    # 空格隔开 -> 每个 e 一个命中（连续相同字符会被合并为一个命中区域）
    page.insert_text((36, 20 + j * 13), "e " * 40, fontsize=10)
doc.save("/tmp/search_dense.pdf")
doc.close()
with open("/tmp/search_dense.pdf", "rb") as f:
    r = client.post("/api/upload", files={"file": ("d.pdf", f, "application/pdf")})
dsid = r.json()["session"]
r = search(dsid, "e")
res = r.json()
check("命中截断到上限", res["hitCount"] == MAX_SEARCH_HITS, str(res["hitCount"]))
check("truncated 标记为真", res["truncated"] is True)

print(f"\n结果：{len(PASS)} 通过，{len(FAIL)} 失败")
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
print("全部通过 ✅")
