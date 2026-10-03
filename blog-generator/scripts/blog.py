"""部落格文章工具：照片前處理、本機預覽、發佈到 WordPress 草稿。

用法（TRIP 可以是完整路徑，或 trips/ 底下的資料夾名稱）：
    blog.py prepare TRIP    轉檔/縮圖照片，產生 .processed/（上傳用）與 .thumbs/（給 Claude 看）
    blog.py preview TRIP    把 post.html 轉成 preview.html 並用瀏覽器打開
    blog.py publish TRIP    上傳照片、建立或更新 WordPress 草稿
    blog.py pull TRIP       使用者在 WordPress 後台改過草稿時，把線上版本拉回 post.html
    blog.py site            列出 WordPress 上的分類與常用標籤

旅程資料夾結構：
    trips/<旅程>/
        *.jpg *.HEIC ...   使用者直接放在旅程資料夾裡的原始照片
        notes.md           問答紀錄
        post.html          文章內容（Gutenberg 格式，照片用 [[...]] 佔位）
        meta.json          標題、分類、標籤、封面、照片描述（photos）、post_id
        .processed/ .thumbs/ .uploads.json preview.html  （腳本產生）

post.html 中的照片佔位語法（每個佔位單獨一行，檔名為旅程資料夾裡的原始檔名）：
    [[gallery: a.jpg, b.HEIC, c.jpg]]          # 產生 Jetpack 並排圖庫（tiled-gallery）
    [[media-text: a.jpg | 圖說文字 | left]]     # left/right 為圖片位置，預設 left
    [[image: a.jpg | 替代文字]]
"""
import argparse
import base64
import html
import json
import os
import re
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRIPS = ROOT / "trips"
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}
UPLOAD_MAX_PX = 2000
THUMB_MAX_PX = 640
PLACEHOLDER = re.compile(r"^[ \t]*\[\[(gallery|media-text|image):(.*?)\]\][ \t]*$", re.M)


# ---------- 共用 ----------

def die(msg):
    print(f"❌ {msg}")
    sys.exit(1)


def resolve_trip(arg):
    p = Path(arg).expanduser()
    if not p.is_absolute() and not p.exists():
        p = TRIPS / arg
    p = p.resolve()
    if not p.is_dir():
        die(f"找不到旅程資料夾：{p}")
    return p


def photo_files(trip):
    return sorted(f for f in trip.iterdir() if f.suffix.lower() in PHOTO_EXTS and not f.name.startswith("."))


def processed_name(src):
    """原始檔名 -> 處理後的 jpg 檔名。同名不同副檔名時保留副檔名避免衝突。"""
    siblings = [f for f in src.parent.iterdir() if f.stem == src.stem and f.suffix.lower() in PHOTO_EXTS]
    stem = src.stem if len(siblings) == 1 else f"{src.stem}_{src.suffix.lstrip('.').lower()}"
    return stem + ".jpg"


def find_photo(trip, name):
    name = name.strip()
    src = trip / name
    if src.exists():
        return src
    # 容許大小寫或副檔名不同（例如寫 IMG_1.jpg 但實際是 IMG_1.HEIC）
    for f in photo_files(trip):
        if f.name.lower() == name.lower() or f.stem.lower() == Path(name).stem.lower():
            return f
    die(f"post.html 引用的照片不存在：{name}")


def capture_date(path, jpeg=None):
    """回傳拍攝時間。讀 EXIF DateTimeOriginal（HEIC 改讀轉檔後的 jpeg）；
    不用 sips 的 creation，那是 EXIF DateTime，照片被編輯或從相機傳輸後會變成處理時間。"""
    try:
        from PIL import Image
        exif = Image.open(jpeg or path).getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)
        if raw:
            return raw[:16].replace(":", "-", 2)
    except Exception:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M") + " (檔案時間)"


def rotation(src):
    """旅程資料夾裡的 rotate.json 指定要轉正的照片，例如 {"IMG_1.HEIC": 90}（順時針度數）。"""
    f = src.parent / "rotate.json"
    if not f.exists():
        return 0, 0
    return json.loads(f.read_text(encoding="utf-8")).get(src.name, 0), f.stat().st_mtime


def sips_convert(src, dst, max_px, quality):
    dst.parent.mkdir(exist_ok=True)
    deg, rot_mtime = rotation(src)
    if dst.exists() and dst.stat().st_mtime >= max(src.stat().st_mtime, rot_mtime):
        return False
    cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality), "-Z", str(max_px)]
    r = subprocess.run(cmd + [str(src), "--out", str(dst)], capture_output=True, text=True)
    if r.returncode != 0:
        die(f"照片轉檔失敗 {src.name}：{r.stderr.strip()}")
    fix_orientation(src, dst, deg, quality)
    return True


# EXIF Orientation -> 要套用的轉換（同 PIL.ImageOps.exif_transpose）
_TRANSPOSE = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE",
              6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90"}


def fix_orientation(src, dst, deg, quality):
    """sips 不會套用部分相機（例如富士）寫在 EXIF 的方向，這裡補上；再套用 rotate.json 的角度。"""
    try:
        from PIL import Image
    except ImportError:
        if deg:
            subprocess.run(["sips", "-r", str(deg), str(dst)], capture_output=True)
        return
    orient = 1
    if src.suffix.lower() in (".jpg", ".jpeg", ".tif", ".tiff"):
        orient = Image.open(src).getexif().get(274, 1)
    if orient == 1 and not deg:
        return
    img = Image.open(dst)
    exif = img.getexif()
    if orient in _TRANSPOSE:
        img = img.transpose(getattr(Image.Transpose, _TRANSPOSE[orient]))
    if deg:
        img = img.rotate(-deg, expand=True)
    exif[274] = 1
    img.save(dst, quality=quality, exif=exif.tobytes())


def parse_placeholder(kind, body):
    parts = [p.strip() for p in body.split("|")]
    if kind == "gallery":
        return {"files": [f.strip() for f in parts[0].split(",") if f.strip()]}
    if kind == "media-text":
        pos = parts[2].lower() if len(parts) > 2 and parts[2] else "left"
        return {"files": [parts[0]], "text": parts[1] if len(parts) > 1 else "", "position": pos}
    return {"files": [parts[0]], "alt": parts[1] if len(parts) > 1 else ""}


def load_meta(trip):
    f = trip / "meta.json"
    if not f.exists():
        die(f"找不到 {f}")
    return json.loads(f.read_text(encoding="utf-8"))


def photo_desc(meta, src):
    return meta.get("photos", {}).get(src.name, "")


def save_meta(trip, meta):
    (trip / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------- prepare ----------

def cmd_prepare(trip):
    files = photo_files(trip)
    if not files:
        die(f"{trip} 裡沒有照片")
    index = []
    converted = 0
    for src in files:
        name = processed_name(src)
        converted += sips_convert(src, trip / ".processed" / name, UPLOAD_MAX_PX, 85)
        sips_convert(src, trip / ".thumbs" / name, THUMB_MAX_PX, 70)
        index.append({"file": src.name, "taken": capture_date(src, trip / ".processed" / name), "thumb": f".thumbs/{name}"})
    index.sort(key=lambda x: x["taken"])
    (trip / ".thumbs" / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ {len(files)} 張照片處理完成（本次新轉檔 {converted} 張）")
    print("依拍攝時間排序：")
    for i in index:
        print(f"  {i['taken']}  {i['file']}  -> {i['thumb']}")
    sheets = contact_sheets(trip, index)
    if sheets:
        print(f"縮圖總表（每張 12 格，標有檔名）：{', '.join(sheets)}")


def contact_sheets(trip, index, cols=4, rows=3, cell=420):
    """把縮圖拼成總表，讓 Claude 一次看多張照片。沒有 Pillow 就略過。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return []
    for old in (trip / ".thumbs").glob("sheet_*.jpg"):
        old.unlink()
    font = ImageFont.load_default(size=20)
    label_h, per = 30, cols * rows
    names = []
    for s in range(0, len(index), per):
        chunk = index[s:s + per]
        sheet = Image.new("RGB", (cols * cell, rows * (cell + label_h)), "white")
        draw = ImageDraw.Draw(sheet)
        for k, item in enumerate(chunk):
            x, y = (k % cols) * cell, (k // cols) * (cell + label_h)
            img = Image.open(trip / item["thumb"])
            img.thumbnail((cell - 8, cell - 8))
            sheet.paste(img, (x + (cell - img.width) // 2, y + label_h + (cell - img.height) // 2))
            name = item["file"] if len(item["file"]) <= 30 else item["file"][:13] + "…" + item["file"][-14:]
            draw.text((x + 6, y + 4), f"{item['taken'][11:16]} {name}", fill="black", font=font)
        name = f"sheet_{s // per + 1:02d}.jpg"
        sheet.save(trip / ".thumbs" / name, quality=80)
        names.append(f".thumbs/{name}")
    return names


# ---------- 佔位 -> HTML ----------

def render(trip, content, image_for, photon=True):
    """把 [[...]] 佔位換成區塊。image_for(src_path) 回傳 {'id','url','alt','width','height','link'}。
    gallery 一律產生 Jetpack 並排圖庫；photon=False 時（本機預覽）圖片不走 Jetpack CDN。"""
    def repl(m):
        kind, opts = m.group(1), parse_placeholder(m.group(1), m.group(2))
        imgs = [image_for(find_photo(trip, f)) for f in opts["files"]]
        if kind == "gallery":
            return tiled_gallery_block(imgs, photon=photon)
        if kind == "media-text":
            return media_text_block(imgs[0], opts["text"], opts["position"])
        return image_block(imgs[0], opts["alt"] or imgs[0].get("alt", ""))
    return PLACEHOLDER.sub(repl, content)


def image_inner(img, alt=""):
    id_attr = f' class="wp-image-{img["id"]}"' if img.get("id") else ""
    return f'<figure class="wp-block-image size-large"><img src="{img["url"]}" alt="{html.escape(alt)}"{id_attr}/></figure>'


def image_block(img, alt=None):
    alt = img.get("alt", "") if alt is None else alt
    attrs = json.dumps({"id": img.get("id"), "sizeSlug": "large", "linkDestination": "none"})
    return f"<!-- wp:image {attrs} -->\n{image_inner(img, alt)}\n<!-- /wp:image -->"


# ---------- Jetpack 並排圖庫（tiled-gallery，矩形樣式）----------
# 移植自 Jetpack extensions/blocks/tiled-gallery/layout/mosaic/{ratios,resize}.js。
# 區塊的 HTML 必須和 Jetpack 編輯器存出來的一模一樣，否則編輯器會顯示「區塊內容無效」，
# 所以列與欄的分法要照 ratiosToMosaicRows 的規則算。

TILED_GUTTER = 4
TILED_EDITOR_WIDTH = 695  # 編輯器裡相簿的寬度（px），只影響欄寬百分比的小數


def _landscape(r): return 1 <= r < 2
def _portrait(r): return r < 1
def _mid(r): return 0.9 <= r < 2


def _fits(preds, ratios):
    return len(ratios) >= len(preds) and all(p(r) for p, r in zip(preds, ratios))


def _not_recent(shape, n, processed):
    return shape not in processed[-n:]


def mosaic_rows(ratios, is_wide=False):
    """回傳每一列的欄位分法，例如 [[1, 2], [1, 1, 1]]（數字是該欄疊幾張）。"""
    L, P = _landscape, _portrait
    rows, rest = [], list(ratios)
    while rest:
        n = len(rest)
        if n > 15 and _fits([L, L, P, L, L], rest) and _not_recent([2, 1, 2], 5, rows):
            nxt = [2, 1, 2]
        elif n > 15 and _fits([L, L, L, P, L, L, L], rest) and _not_recent([3, 1, 3], 5, rows):
            nxt = [3, 1, 3]
        elif n != 5 and _fits([P, L, L, P], rest) and _not_recent([1, 2, 1], 5, rows):
            nxt = [1, 2, 1]
        elif _fits([P, L, L, L], rest) and _not_recent([1, 3], 3, rows):
            nxt = [1, 3]
        elif _fits([L, L, L, P], rest) and _not_recent([3, 1], 3, rows):
            nxt = [3, 1]
        elif _fits([lambda r: r < 1.6, _mid, _mid], rest) and _not_recent([1, 2], 3, rows):
            nxt = [1, 2]
        elif (is_wide and (n == 5 or (n != 10 and n > 6)) and _not_recent([1] * 5, 1, rows)
              and sum(rest[:5]) < 5):
            nxt = [1] * 5
        elif ((_not_recent([1] * 4, 1, rows) and sum(rest[:4]) < 3.5 and n > 5)
              or (sum(rest[:4]) < 7 and n == 4)):
            nxt = [1] * 4
        elif (n >= 3 and n not in (4, 6) and _not_recent([1] * 3, 3, rows)
              and (sum(rest[:3]) < 2.5 or (sum(rest[:3]) < 5 and rest[0] == rest[2]) or is_wide)):
            nxt = [1] * 3
        elif _fits([_mid, _mid, lambda r: r < 1.6], rest) and _not_recent([2, 1], 3, rows):
            nxt = [2, 1]
        elif _fits([lambda r: r >= 2], rest):
            nxt = [1]
        elif n > 3:
            nxt = [1, 1]
        else:
            nxt = [1] * n
        rows.append(nxt)
        rest = rest[sum(nxt):]
    return rows


def _col_ratio(ratios):
    return 1 / sum(1 / r for r in ratios)


def row_widths(cols, width=TILED_EDITOR_WIDTH):
    """cols 是這一列每欄的圖片比例清單，回傳每欄寬度百分比（字串，5 位小數）。"""
    col_r = [_col_ratio(c) for c in cols]
    ratio = sum(col_r)
    weighted = sum(r * len(c) for r, c in zip(col_r, cols))
    avail = width - TILED_GUTTER * (len(cols) - 1)
    raw_h = (avail - weighted) / ratio
    widths = [(raw_h - TILED_GUTTER * (len(c) - 1)) * r for r, c in zip(col_r, cols)]
    diff = (avail - sum(widths)) / len(widths)
    return [f"{(w + diff) / avail * 100:.5f}" for w in widths]


def photon_url(url):
    """Jetpack 圖片 CDN 網址，和編輯器存檔時產生的一樣。"""
    return "https://i0.wp.com/" + re.sub(r"^https?://", "", url.split("?", 1)[0]) + "?ssl=1"


def attr_escape(text):
    """照 WordPress 區塊編輯器的方式跳脫屬性值（撇號不跳脫）。"""
    return html.escape(text, quote=False).replace('"', "&quot;")


def tiled_gallery_block(imgs, photon=True):
    ratios = [(i["width"] / i["height"]) if i.get("width") and i.get("height") else 1 for i in imgs]
    rows = mosaic_rows(ratios)
    col_widths, row_html, k = [], [], 0
    for row in rows:
        cols = []
        for size in row:
            cols.append(list(range(k, k + size)))
            k += size
        widths = row_widths([[ratios[i] for i in c] for c in cols])
        col_widths.append(widths)
        cells = []
        for c, w in zip(cols, widths):
            figs = "".join(
                f'<figure class="tiled-gallery__item"><img alt="{attr_escape(imgs[i].get("alt", ""))}"'
                f' data-height="{imgs[i].get("height", "")}" data-id="{imgs[i].get("id") or ""}"'
                f' data-link="{imgs[i].get("link", "")}" data-url="{imgs[i]["url"]}"'
                f' data-width="{imgs[i].get("width", "")}"'
                f' src="{photon_url(imgs[i]["url"]) if photon else imgs[i]["url"]}" data-amp-layout="responsive"/></figure>'
                for i in c)
            cells.append(f'<div class="tiled-gallery__col" style="flex-basis:{w}%">{figs}</div>')
        row_html.append(f'<div class="tiled-gallery__row">{"".join(cells)}</div>')
    attrs = {"columns": min(len(imgs), 3), "columnWidths": col_widths, "ids": [i.get("id") for i in imgs]}
    return (f'<!-- wp:jetpack/tiled-gallery {json.dumps(attrs, separators=(",", ":"))} -->\n'
            f'<div class="wp-block-jetpack-tiled-gallery aligncenter is-style-rectangular"><div class="">'
            f'<div class="tiled-gallery__gallery">{"".join(row_html)}</div></div></div>\n'
            f'<!-- /wp:jetpack/tiled-gallery -->')


def media_text_block(img, text, position):
    right = position == "right"
    attrs = {"mediaPosition": "right"} if right else {}
    attrs.update({"mediaId": img.get("id"), "mediaType": "image"})
    cls = "wp-block-media-text has-media-on-the-right is-stacked-on-mobile" if right else "wp-block-media-text is-stacked-on-mobile"
    id_cls = f'wp-image-{img["id"]} ' if img.get("id") else ""
    alt = html.escape(img.get("alt", ""))
    media = f'<figure class="wp-block-media-text__media"><img src="{img["url"]}" alt="{alt}" class="{id_cls}size-full"/></figure>'
    body = (f'<div class="wp-block-media-text__content"><!-- wp:paragraph -->\n<p>{text}</p>\n'
            f'<!-- /wp:paragraph --></div>')
    parts = body + media if right else media + body
    return f"<!-- wp:media-text {json.dumps(attrs)} -->\n<div class=\"{cls}\">{parts}</div>\n<!-- /wp:media-text -->"


# ---------- preview ----------

PREVIEW_CSS = """
body{max-width:760px;margin:40px auto;padding:0 16px;font:17px/1.9 -apple-system,"PingFang TC",sans-serif;color:#333;background:#fff}
h1{font-size:26px;line-height:1.5}
h2,h3{padding:6px 12px;font-size:17px}
img{max-width:100%;height:auto;display:block}
.wp-block-gallery{display:grid;grid-template-columns:repeat(var(--cols),1fr);gap:8px;margin:16px 0}
.wp-block-gallery img{width:100%;height:240px;object-fit:cover}
.wp-block-gallery figure,.wp-block-image{margin:0}
.tiled-gallery__gallery{margin:16px 0}
.tiled-gallery__row{display:flex;gap:4px;margin-bottom:4px}
.tiled-gallery__col{display:flex;flex-direction:column;gap:4px;min-width:0}
.tiled-gallery__item{margin:0;flex:1}.tiled-gallery__item img{width:100%;height:100%;object-fit:cover}
.wp-block-media-text{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:center;margin:16px 0}
.wp-block-media-text.has-media-on-the-right .wp-block-media-text__media{order:2}
.wp-block-media-text figure{margin:0}
.wp-block-quote{border-left:4px solid #41a8bf;margin:16px 0;padding:4px 16px;background:#f7fafb}
.has-medium-gray-background-color{background:#6d6d6d}.has-white-color{color:#fff}.has-black-color{color:#000}
table{border-collapse:collapse;width:100%}td{border:1px solid #ccc;padding:6px 10px;vertical-align:top}
.missing{background:#fff3cd;padding:2px 4px}
.has-link-color a{color:#000}
.reusable{color:#888;border:1px dashed #bbb;padding:6px 12px;font-size:14px}
@media (max-width:600px){.wp-block-media-text{grid-template-columns:1fr}.wp-block-media-text.has-media-on-the-right .wp-block-media-text__media{order:0}}
"""


def cmd_preview(trip):
    post = trip / "post.html"
    if not post.exists():
        die(f"找不到 {post}")
    meta = load_meta(trip)
    content = post.read_text(encoding="utf-8")

    def local(src):
        p = trip / ".processed" / processed_name(src)
        if not p.exists():
            sips_convert(src, p, UPLOAD_MAX_PX, 85)
        from PIL import Image
        with Image.open(p) as im:
            w, h = im.size
        return {"id": None, "url": os.path.relpath(p, trip), "alt": photo_desc(meta, src), "width": w, "height": h}

    body = render(trip, content, local, photon=False)
    body = re.sub(r'(<figure class="wp-block-gallery[^"]*columns-(\d)[^"]*")', r'\1 style="--cols:\2"', body)
    body = re.sub(r'<!-- wp:block \{"ref":(\d+)\} /-->', r'<div class="reusable">［可重複使用區塊 #\1，發佈後由 WordPress 顯示］</div>', body)
    body = body.replace("[待補]", '<span class="missing">[待補]</span>')
    cover = ""
    if meta.get("featured"):
        cover = f'<img src="{local(find_photo(trip, meta["featured"]))["url"]}" alt="封面">'
    page = (f'<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>預覽：{html.escape(meta.get("title", ""))}</title><style>{PREVIEW_CSS}</style></head>'
            f'<body>{cover}<h1>{html.escape(meta.get("title", ""))}</h1>{body}</body></html>')
    out = trip / "preview.html"
    out.write_text(page, encoding="utf-8")
    missing = content.count("[待補]")
    print(f"✅ 預覽已產生：{out}" + (f"（還有 {missing} 處 [待補]）" if missing else ""))
    webbrowser.open(out.as_uri())


# ---------- WordPress ----------

class WP:
    def __init__(self):
        import requests
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
        except ImportError:
            pass
        self.requests = requests
        self.base = os.environ.get("WP_URL", "").rstrip("/")
        user, pw = os.environ.get("WP_USER"), os.environ.get("WP_APP_PASSWORD")
        if not (self.base and user and pw):
            die("請在 .env 設定 WP_URL、WP_USER、WP_APP_PASSWORD")
        token = base64.b64encode(f"{user}:{pw}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}"}

    def call(self, method, path, **kw):
        headers = dict(self.headers, **kw.pop("headers", {}))
        r = self.requests.request(method, f"{self.base}/wp-json/wp/v2/{path}", headers=headers, timeout=120, **kw)
        if not r.ok:
            die(f"WordPress API 錯誤 {r.status_code} {method} {path}：{r.text[:300]}")
        return r.json()

    def upload(self, path, filename):
        with open(path, "rb") as f:
            m = self.call("POST", "media", data=f, headers={
                "Content-Disposition": f'attachment; filename="{filename}"', "Content-Type": "image/jpeg"})
        return self.media_info(m)

    def media_info(self, m):
        """媒體資料 -> 文章用的欄位。width/height 是原圖尺寸，並排圖庫用來排版。"""
        details = m.get("media_details", {})
        large = details.get("sizes", {}).get("large", {}).get("source_url")
        return {"id": m["id"], "url": large or m["source_url"], "width": details.get("width"),
                "height": details.get("height"), "link": m.get("link", "")}

    def describe(self, media_id, desc):
        """把照片描述寫進媒體庫的替代文字、標題、說明。"""
        self.call("POST", f"media/{media_id}", json={"alt_text": desc, "title": desc, "description": desc})

    def term_ids(self, taxonomy, names, create):
        ids = []
        for name in names:
            found = [t for t in self.call("GET", taxonomy, params={"search": name, "per_page": 100})
                     if html.unescape(t["name"]) == name]
            if found:
                ids.append(found[0]["id"])
            elif create:
                ids.append(self.call("POST", taxonomy, json={"name": name})["id"])
                print(f"  新增標籤：{name}")
            else:
                die(f"WordPress 上沒有「{name}」這個分類，請先用 `blog.py site` 查看現有分類")
        return ids


def cmd_publish(trip, force, allow_missing):
    meta = load_meta(trip)
    post = trip / "post.html"
    if not post.exists():
        die(f"找不到 {post}")
    content = post.read_text(encoding="utf-8")
    if "[待補]" in content and not allow_missing:
        die("文章還有 [待補]，補完再發佈（使用者同意先發草稿的話加 --allow-missing）")
    used = {find_photo(trip, f).name for m in PLACEHOLDER.finditer(content)
            for f in parse_placeholder(m.group(1), m.group(2))["files"]}
    if meta.get("featured"):
        used.add(find_photo(trip, meta["featured"]).name)
    no_desc = sorted(n for n in used if not meta.get("photos", {}).get(n))
    if no_desc:
        print(f"⚠️  {len(no_desc)} 張照片沒有描述（meta.json 的 photos）：{', '.join(no_desc)}")
    wp = WP()

    cache_f = trip / ".uploads.json"
    cache = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.exists() else {}

    def save_cache():
        cache_f.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    def remote(src):
        if src.name not in cache:
            name = processed_name(src)
            p = trip / ".processed" / name
            sips_convert(src, p, UPLOAD_MAX_PX, 85)
            print(f"  上傳 {src.name} ...")
            cache[src.name] = wp.upload(p, name)
            save_cache()
        item = cache[src.name]
        if not item.get("width") or "link" not in item:
            # 舊版快取沒有記尺寸和附件頁連結，補抓一次
            info = wp.media_info(wp.call("GET", f"media/{item['id']}"))
            item.update({k: info[k] for k in ("width", "height", "link")})
            save_cache()
        desc = photo_desc(meta, src)
        if desc and item.get("alt") != desc:
            wp.describe(item["id"], desc)
            item["alt"] = desc
            save_cache()
        return item

    print("處理照片 ...")
    final = render(trip, content, remote)
    data = {"title": meta["title"], "content": final, "status": "draft"}
    if meta.get("featured"):
        data["featured_media"] = remote(find_photo(trip, meta["featured"]))["id"]
    if meta.get("categories"):
        data["categories"] = wp.term_ids("categories", meta["categories"], create=False)
    if meta.get("tags"):
        data["tags"] = wp.term_ids("tags", meta["tags"], create=True)

    post_id = meta.get("post_id")
    if post_id:
        current = wp.call("GET", f"posts/{post_id}", params={"context": "edit"})
        if current["status"] != "draft":
            die(f"文章 {post_id} 已經是「{current['status']}」狀態，不會覆蓋。請到 WordPress 手動處理。")
        if current["modified_gmt"] != meta.get("last_published_gmt") and not force:
            die(f"文章 {post_id} 在 WordPress 上有被修改過，覆蓋會蓋掉那些修改。確定要覆蓋請加 --force")
        print(f"更新草稿 {post_id} ...")
        result = wp.call("POST", f"posts/{post_id}", json=data)
    else:
        print("建立草稿 ...")
        result = wp.call("POST", "posts", json=data)

    meta["post_id"] = result["id"]
    meta["last_published_gmt"] = result["modified_gmt"]
    save_meta(trip, meta)
    print(f"\n✅ 草稿已{'更新' if post_id else '建立'}！")
    print(f"文章 ID：{result['id']}")
    print(f"編輯連結：{wp.base}/wp-admin/post.php?post={result['id']}&action=edit")
    print(f"預覽連結：{result['link']}")


def cmd_pull(trip):
    """使用者在 WordPress 後台改過草稿時，把線上版本拉回 post.html，之後以它為基礎修改。"""
    meta = load_meta(trip)
    if not meta.get("post_id"):
        die("這篇還沒發佈過，沒有可以拉回的草稿")
    wp = WP()
    current = wp.call("GET", f"posts/{meta['post_id']}", params={"context": "edit"})
    post = trip / "post.html"
    if post.exists():
        backup = trip / f"post.before-pull-{datetime.now():%Y%m%d-%H%M%S}.html"
        post.rename(backup)
        print(f"原本的 post.html 備份為 {backup.name}")
    post.write_text(current["content"]["raw"], encoding="utf-8")
    meta["title"] = current["title"]["raw"]
    meta["last_published_gmt"] = current["modified_gmt"]
    save_meta(trip, meta)
    print(f"✅ 已從 WordPress 拉回文章 {meta['post_id']}（修改時間 {current['modified_gmt']} GMT）")


def cmd_site():
    wp = WP()
    cats = wp.call("GET", "categories", params={"per_page": 100})
    print("分類：" + "、".join(f"{html.unescape(c['name'])}({c['count']})" for c in cats))
    tags = wp.call("GET", "tags", params={"per_page": 60, "orderby": "count", "order": "desc"})
    print("常用標籤：" + "、".join(f"{html.unescape(t['name'])}({t['count']})" for t in tags))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="部落格文章工具")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prepare", "preview", "publish", "pull"):
        s = sub.add_parser(name)
        s.add_argument("trip")
        if name == "publish":
            s.add_argument("--force", action="store_true", help="覆蓋在 WordPress 上被修改過的草稿")
            s.add_argument("--allow-missing", action="store_true", help="文章還有 [待補] 也先發成草稿")
    sub.add_parser("site")
    a = ap.parse_args()
    if a.cmd == "site":
        cmd_site()
    elif a.cmd == "prepare":
        cmd_prepare(resolve_trip(a.trip))
    elif a.cmd == "pull":
        cmd_pull(resolve_trip(a.trip))
    elif a.cmd == "preview":
        cmd_preview(resolve_trip(a.trip))
    else:
        cmd_publish(resolve_trip(a.trip), a.force, a.allow_missing)
