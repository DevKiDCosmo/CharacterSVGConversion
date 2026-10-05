#!/usr/bin/env python3
"""
Comprehensive SVG to Real Bitmap & Vector Font Pipeline.

Converts SVGs into single, full, installable font products:
  1. Full Installable Bitmap Font:
     - <FontName>-Bitmap.ttf  (Standard TrueType Font converted from bitmap data; installable in macOS Font Book, Windows, Word, Photoshop, Figma)
     - <FontName>-Bitmap.woff2 (Compressed Web Font for CSS @font-face)
  2. Full Installable Vector Font:
     - <FontName>-Vector.otf  (OpenType CFF outlines for print & graphic design)
     - <FontName>-Vector.ttf  (TrueType glyf outlines)
     - <FontName>-Vector.woff2 (Compressed Web Font for CSS @font-face)
  3. Color Bitmap Font:
     - <FontName>-ColorBitmap.ttf (OpenType sbix table with embedded PNG strikes)
  4. Visual Specimen & Documentation:
     - <FontName>-Specimen.pdf (AFDKO tx character synopsis sheet)
     - specimen.html (Minimalist black & white list gallery with only full product downloads)

Usage:
    ./.v2/bin/python pipeline.py
    ./.v2/bin/python pipeline.py --sizes 32,64,128
"""

import os
import sys
import glob
import re
import json
import zlib
import struct
import shutil
import argparse
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# fontTools
try:
    from fontTools.ttLib import TTFont, newTable
    from fontTools.ttLib.tables.sbixStrike import Strike
    from fontTools.ttLib.tables.sbixGlyph import Glyph
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    HAS_FONTTOOLS = True
except ImportError:
    HAS_FONTTOOLS = False

# brotli
try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False

# bdflib
try:
    from bdflib import model as bdf_model, writer as bdf_writer
    HAS_BDFLIB = True
except ImportError:
    HAS_BDFLIB = False


# ==============================================================================
# Helpers
# ==============================================================================

def print_header(title):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)

def print_step(step_num, total_steps, description):
    print(f"\n[{step_num}/{total_steps}] {description}...")

def check_dependencies():
    tools = {}
    try:
        res = subprocess.run(["inkscape", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        tools["inkscape"] = res.stdout.strip().split("\n")[0] if res.returncode == 0 else None
    except FileNotFoundError:
        tools["inkscape"] = None

    try:
        res = subprocess.run(["fontforge", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        tools["fontforge"] = "FontForge available" if res.returncode == 0 else None
    except FileNotFoundError:
        tools["fontforge"] = None

    script_dir = Path(__file__).resolve().parent
    v2_tx = script_dir / ".v2" / "bin" / "tx"
    venv_tx = script_dir / ".venv" / "bin" / "tx"
    tx_cmd = str(v2_tx) if v2_tx.exists() else (str(venv_tx) if venv_tx.exists() else "tx")

    try:
        res = subprocess.run([tx_cmd, "-u"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        tools["tx"] = tx_cmd if res.returncode == 0 or "tx" in res.stdout or "tx" in res.stderr else None
    except FileNotFoundError:
        tools["tx"] = None

    return tools


def parse_svg_glyph_info(filepath):
    filename = os.path.basename(filepath)
    stem, _ = os.path.splitext(filename)

    patterns = [
        r'^[Uu]\+([0-9A-Fa-f]{4,6})(?:[-_](.*))?$',
        r'^[Uu]ni([0-9A-Fa-f]{4,6})(?:[-_](.*))?$',
        r'^0[Xx]([0-9A-Fa-f]{4,6})(?:[-_](.*))?$',
        r'^[Uu]([0-9A-Fa-f]{4,6})(?:[-_](.*))?$',
        r'^([0-9A-Fa-f]{4,6})(?:[-_](.*))?$'
    ]

    for p in patterns:
        m = re.match(p, stem)
        if m:
            hex_str = m.group(1).upper()
            try:
                unicode_val = int(hex_str, 16)
            except ValueError:
                continue

            standard_glyph_name = f"uni{hex_str}" if len(hex_str) == 4 else f"u{hex_str}"
            label = m.group(2) if m.lastindex >= 2 and m.group(2) else ""
            clean_label = re.sub(r'[^a-zA-Z0-9_-]', '_', label) if label else standard_glyph_name

            return {
                "filepath": str(filepath),
                "filename": filename,
                "hex_str": hex_str,
                "unicode_val": unicode_val,
                "unicode_hex": f"U+{hex_str}",
                "glyph_name": standard_glyph_name,
                "label": clean_label or standard_glyph_name
            }
    return None


# ==============================================================================
# Robust RFC 2083 PNG Scanline Decoder (Unfilters PNG into exact 2D pixel matrix)
# ==============================================================================

def paeth_predictor(a, b, c):
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    elif pb <= pc:
        return b
    else:
        return c


def decode_png_pixels(png_path):
    """
    Decodes an RGBA or Grayscale PNG directly using Python's standard library (zlib).
    Unfilters all scanlines according to RFC 2083 (Paeth, Sub, Up, Average, None).
    Returns (width, height, binary_pixel_matrix_2d, active_pixel_count).
    where binary_pixel_matrix_2d[y][x] == 1 for dark/opaque foreground, 0 for background.
    """
    with open(png_path, "rb") as f:
        data = f.read()

    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError(f"Not a valid PNG file: {png_path}")

    pos = 8
    width, height = 0, 0
    idat = b''
    color_type = 6

    while pos < len(data):
        length, chunk_type = struct.unpack(">I4s", data[pos:pos+8])
        pos += 8
        content = data[pos:pos+length]
        pos += length + 4
        if chunk_type == b'IHDR':
            width, height, bit_depth, color_type = struct.unpack(">IIBB", content[:10])
        elif chunk_type == b'IDAT':
            idat += content
        elif chunk_type == b'IEND':
            break

    raw = zlib.decompress(idat)
    bpp = 4 if color_type == 6 else (3 if color_type == 2 else 1)
    line_len = width * bpp
    stride = 1 + line_len

    recon = bytearray(height * line_len)
    for y in range(height):
        filter_type = raw[y * stride]
        scanline = raw[y * stride + 1 : (y + 1) * stride]
        prior_y = y - 1

        for x in range(line_len):
            filt_val = scanline[x]
            left = recon[y * line_len + x - bpp] if x >= bpp else 0
            up = recon[prior_y * line_len + x] if prior_y >= 0 else 0
            up_left = recon[prior_y * line_len + x - bpp] if (prior_y >= 0 and x >= bpp) else 0

            if filter_type == 0:
                val = filt_val
            elif filter_type == 1:
                val = (filt_val + left) & 0xff
            elif filter_type == 2:
                val = (filt_val + up) & 0xff
            elif filter_type == 3:
                val = (filt_val + ((left + up) >> 1)) & 0xff
            elif filter_type == 4:
                val = (filt_val + paeth_predictor(left, up, up_left)) & 0xff
            else:
                val = filt_val

            recon[y * line_len + x] = val

    pixel_matrix = []
    active_count = 0

    for y in range(height):
        row = []
        for x in range(width):
            px_off = y * line_len + x * bpp
            if color_type == 6:
                r, g, b, a = recon[px_off:px_off+4]
                is_on = 1 if (a > 64 and (r < 180 or g < 180 or b < 180)) else 0
            elif color_type == 2:
                r, g, b = recon[px_off:px_off+3]
                is_on = 1 if (r < 128 or g < 128 or b < 128) else 0
            else:
                is_on = 1 if recon[px_off] < 128 else 0

            row.append(is_on)
            if is_on:
                active_count += 1
        pixel_matrix.append(row)

    return width, height, pixel_matrix, active_count


# ==============================================================================
# Rasterization (Inkscape Multi-size PNGs)
# ==============================================================================

def render_single_svg_png(svg_path, output_png, size):
    os.makedirs(os.path.dirname(output_png), exist_ok=True)
    cmd = [
        "inkscape",
        "--export-type=png",
        f"--export-width={size}",
        f"--export-height={size}",
        f"--export-filename={output_png}",
        svg_path
    ]
    env = os.environ.copy()
    env["INKSCAPE_PROFILE_DIR"] = "/tmp"
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    return res.returncode == 0 and os.path.exists(output_png)


def rasterize_all_svgs(glyph_list, sizes, build_dir, max_workers=4):
    png_mappings = {s: {} for s in sizes}
    tasks = []

    for item in glyph_list:
        hex_str = item["hex_str"]
        g_name = item["glyph_name"]
        svg_path = item["filepath"]

        for size in sizes:
            out_dir = os.path.join(build_dir, "bitmaps", f"{size}px")
            png_filename = f"{hex_str}_{size}.png"
            png_path = os.path.join(out_dir, png_filename)
            png_mappings[size][g_name] = png_path
            tasks.append((svg_path, png_path, size, item["filename"]))

    print(f" -> Rendering {len(tasks)} PNG image strikes with Inkscape ({sizes} px)...")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {
            executor.submit(render_single_svg_png, svg, out, size): (svg, out, size, name)
            for (svg, out, size, name) in tasks
        }
        for future in as_completed(future_to_task):
            svg, out, size, name = future_to_task[future]
            success = future.result()
            if not success:
                print(f"    [WARN] Failed to render {name} at {size}px")

    return png_mappings


# ==============================================================================
# Full Product: Installable TrueType Bitmap Font (.ttf & .woff2)
# ==============================================================================

def generate_installable_bitmap_ttf(glyph_list, png_mappings, sizes, build_dir, font_name, family_name):
    """
    Converts the bitmap pixel data (from SVG raster) into single,
    complete, standard TrueType Font files (.ttf) for all resolutions (32px, 64px, 128px).
    These can be installed directly with a double-click in macOS Font Book or Windows,
    and used normally in Microsoft Word, Photoshop, Figma, Illustrator, etc.
    """
    if not HAS_FONTTOOLS:
        print("    [WARN] fonttools not installed; skipping TTF bitmap font.")
        return {}

    if isinstance(sizes, int):
        sizes = (sizes,)

    generated = {}

    for size in sizes:
        em = 1024
        pixel_size = em // size
        ascent = int(em * 0.8)
        descent = em - ascent

        glyph_order = ['.notdef'] + [item['glyph_name'] for item in glyph_list]
        cmap = {item['unicode_val']: item['glyph_name'] for item in glyph_list}

        fb = FontBuilder(em, isTTF=True)
        fb.setupGlyphOrder(glyph_order)
        fb.setupCharacterMap(cmap)

        # .notdef
        pen = TTGlyphPen(None)
        pen.moveTo((100, 0))
        pen.lineTo((100, ascent))
        pen.lineTo((em - 100, ascent))
        pen.lineTo((em - 100, 0))
        pen.closePath()
        notdef_glyph = pen.glyph()

        glyf_dict = {'.notdef': notdef_glyph}
        h_metrics = {'.notdef': (em, 100)}

        for item in glyph_list:
            g_name = item['glyph_name']
            png_path = png_mappings[size].get(g_name)
            if not png_path or not os.path.exists(png_path):
                continue

            w, h, px_matrix, _ = decode_png_pixels(png_path)
            pen = TTGlyphPen(None)

            for y in range(h):
                font_y = ascent - (y + 1) * pixel_size
                for x in range(w):
                    if px_matrix[y][x]:
                        font_x = x * pixel_size
                        pen.moveTo((font_x, font_y))
                        pen.lineTo((font_x + pixel_size, font_y))
                        pen.lineTo((font_x + pixel_size, font_y + pixel_size))
                        pen.lineTo((font_x, font_y + pixel_size))
                        pen.closePath()

            glyf_dict[g_name] = pen.glyph()
            h_metrics[g_name] = (em, 0)

        fb.setupGlyf(glyf_dict)
        fb.setupHorizontalMetrics(h_metrics)
        fb.setupHorizontalHeader(ascent=ascent, descent=-descent)
        fb.setupNameTable({
            'familyName': f"{family_name} Bitmap {size}px",
            'styleName': 'Regular',
            'fullName': f"{family_name} Bitmap {size}px",
            'uniqueFontIdentifier': f"{font_name}Bitmap{size}-Regular",
            'version': 'Version 1.000',
            'psName': f"{font_name}-Bitmap-{size}"
        })
        fb.setupOS2(sTypoAscender=ascent, sTypoDescender=-descent, usWinAscent=ascent, usWinDescent=descent)
        fb.setupPost()

        ttf_path = os.path.join(build_dir, f"{font_name}-Bitmap-{size}.ttf")
        fb.save(ttf_path)

        if HAS_BROTLI:
            woff2_path = os.path.join(build_dir, f"{font_name}-Bitmap-{size}.woff2")
            fb.font.flavor = "woff2"
            fb.save(woff2_path)

        generated[size] = ttf_path
        print(f" -> Generated Full Installable Bitmap Font: {font_name}-Bitmap-{size}.ttf")

    # Also keep default alias for 32px as <font_name>-Bitmap.ttf
    p_32_ttf = os.path.join(build_dir, f"{font_name}-Bitmap-32.ttf")
    def_ttf = os.path.join(build_dir, f"{font_name}-Bitmap.ttf")
    if os.path.exists(p_32_ttf):
        shutil.copy2(p_32_ttf, def_ttf)
    p_32_woff2 = os.path.join(build_dir, f"{font_name}-Bitmap-32.woff2")
    def_woff2 = os.path.join(build_dir, f"{font_name}-Bitmap.woff2")
    if os.path.exists(p_32_woff2):
        shutil.copy2(p_32_woff2, def_woff2)

    return generated


# ==============================================================================
# Full Product: Installable Vector Font (OTF, TTF, WOFF2)
# ==============================================================================

def run_vector_fontforge_worker(glyph_list, font_config, build_dir):
    font_name = font_config["fontname"]
    worker_data = {
        "glyphs": [
            {
                "unicode": item["unicode_val"],
                "glyph_name": item["glyph_name"],
                "filepath": item["filepath"]
            }
            for item in glyph_list
        ],
        "config": font_config,
        "build_dir": os.path.abspath(build_dir)
    }

    worker_json_path = os.path.join(build_dir, "_ff_vector_data.json")
    with open(worker_json_path, "w") as f:
        json.dump(worker_data, f)

    worker_script_path = os.path.join(build_dir, "_ff_vector_worker.py")
    worker_script_code = f"""import sys
import os
import json
import fontforge
import psMat

with open('{worker_json_path}', 'r') as f:
    data = json.load(f)

cfg = data['config']
build_dir = data['build_dir']

font = fontforge.font()
font.fontname = cfg['fontname'] + "-Vector"
font.familyname = cfg['familyname'] + " Vector"
font.fullname = cfg['fullname'] + " Vector"
font.weight = cfg.get('weight', 'Regular')
font.version = cfg.get('version', '1.000')
font.encoding = 'UnicodeFull'

font.ascent = cfg.get('ascent', 800)
font.descent = cfg.get('descent', 200)
lsb = cfg.get('left_side_bearing', 50)
rsb = cfg.get('right_side_bearing', 50)

for item in data['glyphs']:
    glyph = font.createChar(item['unicode'], item['glyph_name'])
    glyph.importOutlines(item['filepath'])
    
    # 10x Scale Overlap Fix Workaround
    glyph.transform(psMat.scale(10.0))
    glyph.round()
    glyph.correctDirection()
    glyph.removeOverlap()
    glyph.transform(psMat.scale(0.1))
    glyph.round()
    glyph.correctDirection()
    glyph.left_side_bearing = lsb
    glyph.right_side_bearing = rsb

# Pure Vector OTF & TTF
font.generate(os.path.join(build_dir, "{font_name}-Vector.otf"))
font.generate(os.path.join(build_dir, "{font_name}-Vector.ttf"))
font.close()
"""
    with open(worker_script_path, "w") as f:
        f.write(worker_script_code)

    subprocess.run(["fontforge", "-script", worker_script_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if os.path.exists(worker_json_path):
        os.remove(worker_json_path)
    if os.path.exists(worker_script_path):
        os.remove(worker_script_path)

    print(f" -> Generated Full Installable Vector Fonts: {font_name}-Vector.otf & {font_name}-Vector.ttf")


def export_web_fonts(otf_path, build_dir, font_name):
    if not HAS_FONTTOOLS or not os.path.exists(otf_path):
        return
    font = TTFont(otf_path)
    woff2_path = os.path.join(build_dir, f"{font_name}-Vector.woff2")
    if HAS_BROTLI:
        font.flavor = "woff2"
        font.save(woff2_path)
        print(f" -> Generated Full Vector Web Font: {font_name}-Vector.woff2")


# ==============================================================================
# Full Product: Color Bitmap Font (sbix TTF)
# ==============================================================================

def embed_sbix_table(ttf_source_path, ttf_dest_path, png_mappings, descent, em, font_name, family_name):
    if not HAS_FONTTOOLS or not os.path.exists(ttf_source_path):
        return False

    ttfont = TTFont(ttf_source_path)
    sbix_table = newTable('sbix')
    sbix_table.version = 1
    sbix_table.flags = 1
    sbix_table.strikes = {}

    glyph_order_set = set(ttfont.getGlyphOrder())
    total_embedded = 0

    for size, mapping_dict in png_mappings.items():
        strike = Strike()
        strike.ppem = int(size)
        strike.resolution = 72
        strike.glyphs = {}
        offset_y = int(size * (descent / em))

        for g_name, png_path in mapping_dict.items():
            if g_name not in glyph_order_set or not os.path.exists(png_path):
                continue

            with open(png_path, "rb") as f:
                img_data = f.read()

            glyph_item = Glyph()
            glyph_item.glyphName = g_name
            glyph_item.graphicType = "png "
            glyph_item.imageData = img_data
            glyph_item.originOffsetX = 0
            glyph_item.originOffsetY = offset_y

            strike.glyphs[g_name] = glyph_item
            total_embedded += 1

        sbix_table.strikes[size] = strike

    ttfont['sbix'] = sbix_table
    ttfont.save(ttf_dest_path)
    print(f" -> Generated Full Color Bitmap Font (sbix TTF): {os.path.basename(ttf_dest_path)}")
    return True


# ==============================================================================
# Raw Backend Intermediate Formats (BDF, BMFont for toolchains)
# ==============================================================================

def generate_direct_bdf_fonts(glyph_list, sizes, png_mappings, build_dir, font_name, family_name):
    if not HAS_BDFLIB:
        return {}
    bdf_dir = os.path.join(build_dir, "bdf")
    os.makedirs(bdf_dir, exist_ok=True)
    generated_bdfs = {}

    for size in sizes:
        font = bdf_model.Font(font_name.encode("utf-8"), size, 75, 75)
        ascent = int(size * 0.8)
        descent = size - ascent
        font[b'FAMILY_NAME'] = family_name.encode("utf-8")
        font[b'FONT_ASCENT'] = ascent
        font[b'FONT_DESCENT'] = descent
        font[b'DEFAULT_CHAR'] = glyph_list[0]["unicode_val"]

        for item in glyph_list:
            g_name = item["glyph_name"]
            u_val = item["unicode_val"]
            png_path = png_mappings[size].get(g_name)
            if not png_path or not os.path.exists(png_path):
                continue

            w, h, px_matrix, _ = decode_png_pixels(png_path)
            rows = []
            for y in range(h):
                row_bits = 0
                for x in range(w):
                    row_bits = (row_bits << 1) | px_matrix[y][x]
                rows.append(row_bits)

            font.new_glyph_from_data(
                name=g_name.encode("utf-8"),
                data=rows,
                bbX=0,
                bbY=-descent,
                bbW=w,
                bbH=h,
                advance=w,
                codepoint=u_val
            )

        bdf_path = os.path.join(bdf_dir, f"{font_name}-{size}.bdf")
        with open(bdf_path, "wb") as f:
            bdf_writer.write_bdf(font, f)
        generated_bdfs[size] = bdf_path

    return generated_bdfs


def generate_bmfont_descriptors(glyph_list, sizes, png_mappings, build_dir, font_name):
    bmfont_dir = os.path.join(build_dir, "bmfont")
    os.makedirs(bmfont_dir, exist_ok=True)

    for size in sizes:
        size_dir = os.path.join(bmfont_dir, f"{size}px")
        os.makedirs(size_dir, exist_ok=True)
        fnt_path = os.path.join(size_dir, f"{font_name}.fnt")

        lines = [
            f'info face="{font_name}" size={size} bold=0 italic=0 charset="" unicode=1 stretchH=100 smooth=1 aa=1 padding=0,0,0,0 spacing=1,1 outline=0',
            f'common lineHeight={size} base={int(size * 0.8)} scaleW=512 scaleH=512 pages={len(glyph_list)} packed=0 alphaChnl=0 redChnl=0 greenChnl=0 blueChnl=0'
        ]

        page_entries = []
        char_entries = []

        for idx, item in enumerate(glyph_list):
            g_name = item["glyph_name"]
            u_val = item["unicode_val"]
            src_png = png_mappings[size].get(g_name)

            if src_png and os.path.exists(src_png):
                png_name = os.path.basename(src_png)
                dest_png = os.path.join(size_dir, png_name)
                if not os.path.exists(dest_png):
                    shutil.copy2(src_png, dest_png)

                page_entries.append(f'page id={idx} file="{png_name}"')
                char_entries.append(
                    f'char id={u_val} x=0 y=0 width={size} height={size} xoffset=0 yoffset=0 xadvance={size} page={idx} chnl=15'
                )

        lines.extend(page_entries)
        lines.append(f'chars count={len(char_entries)}')
        lines.extend(char_entries)

        with open(fnt_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def generate_pdf_synopsis(tx_bin, otf_path, output_pdf):
    if not tx_bin or not os.path.exists(otf_path):
        return False
    cmd = [tx_bin, "-pdf", otf_path, output_pdf]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode == 0 and os.path.exists(output_pdf):
        print(f" -> Generated Character Specimen PDF: {os.path.basename(output_pdf)}")
        return True
    return False


# ==============================================================================
# Minimalist Modern Black & White List Gallery Specimen
# ==============================================================================

def generate_html_specimen(glyph_list, sizes, png_mappings, build_dir, font_config):
    specimen_path = os.path.join(build_dir, "specimen.html")
    font_name = font_config["fontname"]
    family_name = font_config["familyname"]

    gallery_items_html = []

    for item in glyph_list:
        g_name = item["glyph_name"]
        u_hex = item["unicode_hex"]
        u_val = item["unicode_val"]
        label = item["label"]
        char_symbol = chr(u_val)

        svg_rel_path = os.path.relpath(item["filepath"], build_dir)

        gallery_items_html.append(f"""
        <div class="gallery-item" data-hex="{u_hex.lower()}" data-name="{label.lower()}">
            <!-- Col 1: Identity & Metadata -->
            <div class="item-meta">
                <span class="mono-code">{u_hex}</span>
                <span class="item-name">{label}</span>
                <span class="meta-tag">NAME: {g_name}</span>
                <span class="meta-tag">DEC: {u_val}</span>
                <button class="minimal-btn" onclick="copyValue(this, '{u_hex}')">COPY HEX</button>
            </div>

            <!-- Col 2: 32px Bitmap Font -->
            <div class="preview-unit">
                <span class="col-caption">32px BITMAP</span>
                <div class="glyph-stage">
                    <span class="bitmap-glyph bm-32">{char_symbol}</span>
                </div>
            </div>

            <!-- Col 3: 64px Bitmap Font -->
            <div class="preview-unit">
                <span class="col-caption">64px BITMAP</span>
                <div class="glyph-stage">
                    <span class="bitmap-glyph bm-64">{char_symbol}</span>
                </div>
            </div>

            <!-- Col 4: 128px Bitmap Font -->
            <div class="preview-unit">
                <span class="col-caption">128px BITMAP</span>
                <div class="glyph-stage">
                    <span class="bitmap-glyph bm-128">{char_symbol}</span>
                </div>
            </div>

            <!-- Col 5: Vector Font -->
            <div class="preview-unit">
                <span class="col-caption">VECTOR (OTF)</span>
                <div class="glyph-stage">
                    <span class="vector-glyph">{char_symbol}</span>
                </div>
            </div>

            <!-- Col 6: Source SVG -->
            <div class="preview-unit">
                <span class="col-caption">SOURCE SVG</span>
                <div class="glyph-stage">
                    <img src="{svg_rel_path}" class="svg-glyph" alt="SVG">
                </div>
            </div>
        </div>
        """)

    all_rows = "\n".join(gallery_items_html)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{font_config['fullname']} — Specimen</title>
    <style>
        @font-face {{
            font-family: '{font_name}Bitmap32';
            src: url('{font_name}-Bitmap-32.woff2') format('woff2'),
                 url('{font_name}-Bitmap-32.ttf') format('truetype');
            font-weight: normal;
            font-style: normal;
        }}

        @font-face {{
            font-family: '{font_name}Bitmap64';
            src: url('{font_name}-Bitmap-64.woff2') format('woff2'),
                 url('{font_name}-Bitmap-64.ttf') format('truetype');
            font-weight: normal;
            font-style: normal;
        }}

        @font-face {{
            font-family: '{font_name}Bitmap128';
            src: url('{font_name}-Bitmap-128.woff2') format('woff2'),
                 url('{font_name}-Bitmap-128.ttf') format('truetype');
            font-weight: normal;
            font-style: normal;
        }}

        @font-face {{
            font-family: '{font_name}Bitmap';
            src: url('{font_name}-Bitmap.woff2') format('woff2'),
                 url('{font_name}-Bitmap.ttf') format('truetype');
            font-weight: normal;
            font-style: normal;
        }}

        @font-face {{
            font-family: '{font_name}Vector';
            src: url('{font_name}-Vector.woff2') format('woff2'),
                 url('{font_name}-Vector.otf') format('opentype');
            font-weight: normal;
            font-style: normal;
        }}

        :root {{
            --bg: #000000;
            --surface: #0a0a0a;
            --surface-elevated: #111111;
            --border: #1a1a1a;
            --border-hover: #333333;
            --text-primary: #ffffff;
            --text-secondary: #888888;
            --text-muted: #555555;
            --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            --font-mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, monospace;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            background: var(--bg);
            color: var(--text-primary);
            font-family: var(--font-sans);
            padding: 3.5rem 2.5rem;
            max-width: 1400px;
            margin: 0 auto;
            line-height: 1.4;
            -webkit-font-smoothing: antialiased;
        }}

        /* Header */
        header {{
            border-bottom: 1px solid var(--border);
            padding-bottom: 2rem;
            margin-bottom: 2.5rem;
        }}

        .header-top {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            flex-wrap: wrap;
            gap: 1rem;
            margin-bottom: 1.5rem;
        }}

        .font-title {{
            font-size: 1.75rem;
            font-weight: 600;
            letter-spacing: -0.02em;
            color: #fff;
        }}

        .font-meta-tag {{
            font-family: var(--font-mono);
            font-size: 0.75rem;
            color: var(--text-muted);
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }}

        /* Clean Full Product Downloads Strip */
        .downloads-strip {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
            flex-wrap: wrap;
        }}

        .downloads-label {{
            font-family: var(--font-mono);
            font-size: 0.65rem;
            color: var(--text-muted);
            letter-spacing: 0.1em;
            text-transform: uppercase;
            margin-right: 0.35rem;
        }}

        .download-pill {{
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-secondary);
            font-family: var(--font-mono);
            font-size: 0.75rem;
            padding: 0.4rem 0.75rem;
            border-radius: 4px;
            text-decoration: none;
            transition: all 0.15s ease;
        }}

        .download-pill:hover {{
            background: #fff;
            color: #000;
            border-color: #fff;
        }}

        .download-pill.primary {{
            border-color: var(--border-hover);
            color: #fff;
        }}

        /* Controls */
        .controls-bar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2rem;
            gap: 1rem;
            flex-wrap: wrap;
        }}

        .filter-input {{
            background: var(--surface);
            border: 1px solid var(--border);
            color: #fff;
            font-family: var(--font-mono);
            font-size: 0.8rem;
            padding: 0.5rem 0.85rem;
            border-radius: 4px;
            width: 260px;
            transition: border-color 0.15s ease;
        }}

        .filter-input:focus {{
            outline: none;
            border-color: var(--border-hover);
        }}

        .count-indicator {{
            font-family: var(--font-mono);
            font-size: 0.75rem;
            color: var(--text-muted);
        }}

        /* List Gallery */
        .gallery-list {{
            display: flex;
            flex-direction: column;
            gap: 1px;
            background: var(--border);
            border: 1px solid var(--border);
            border-radius: 6px;
            overflow: hidden;
            margin-bottom: 3.5rem;
        }}

        .gallery-item {{
            background: var(--bg);
            padding: 1.75rem 2rem;
            display: grid;
            grid-template-columns: 190px repeat(5, minmax(105px, 1fr));
            gap: 1.5rem;
            align-items: center;
            transition: background 0.15s ease;
        }}

        .gallery-item:hover {{
            background: var(--surface);
        }}

        @media (max-width: 1050px) {{
            .gallery-item {{
                grid-template-columns: 1fr 1fr 1fr;
                gap: 1.25rem;
            }}
        }}

        @media (max-width: 600px) {{
            .gallery-item {{
                grid-template-columns: 1fr 1fr;
            }}
        }}

        .item-meta {{
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
        }}

        .mono-code {{
            font-family: var(--font-mono);
            font-size: 1.25rem;
            font-weight: 700;
            color: #fff;
            letter-spacing: -0.01em;
        }}

        .item-name {{
            font-size: 0.95rem;
            color: var(--text-secondary);
            font-weight: 500;
        }}

        .meta-tag {{
            font-family: var(--font-mono);
            font-size: 0.65rem;
            color: var(--text-muted);
            letter-spacing: 0.05em;
        }}

        .minimal-btn {{
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-secondary);
            font-family: var(--font-mono);
            font-size: 0.65rem;
            padding: 0.3rem 0.55rem;
            border-radius: 3px;
            cursor: pointer;
            width: fit-content;
            margin-top: 0.5rem;
            transition: all 0.15s ease;
        }}

        .minimal-btn:hover {{
            border-color: #fff;
            color: #fff;
        }}

        .col-caption {{
            font-family: var(--font-mono);
            font-size: 0.65rem;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            color: var(--text-muted);
            margin-bottom: 0.6rem;
            display: block;
        }}

        .preview-unit {{
            display: flex;
            flex-direction: column;
        }}

        .glyph-stage {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 4px;
            width: 110px;
            height: 110px;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }}

        .bitmap-glyph.bm-32 {{
            font-family: '{font_name}Bitmap32', monospace;
            font-size: 72px;
            color: #ffffff;
            line-height: 1;
        }}

        .bitmap-glyph.bm-64 {{
            font-family: '{font_name}Bitmap64', monospace;
            font-size: 72px;
            color: #ffffff;
            line-height: 1;
        }}

        .bitmap-glyph.bm-128 {{
            font-family: '{font_name}Bitmap128', monospace;
            font-size: 72px;
            color: #ffffff;
            line-height: 1;
        }}

        .vector-glyph {{
            font-family: '{font_name}Vector', sans-serif;
            font-size: 72px;
            color: #ffffff;
            line-height: 1;
        }}

        .svg-glyph {{
            max-width: 76px;
            max-height: 76px;
            filter: invert(1);
        }}

        /* Minimalist Typographic Playground */
        .playground {{
            border-top: 1px solid var(--border);
            padding-top: 2.5rem;
        }}

        .playground-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.25rem;
            flex-wrap: wrap;
            gap: 1rem;
        }}

        .playground-title {{
            font-size: 0.75rem;
            font-family: var(--font-mono);
            letter-spacing: 0.1em;
            text-transform: uppercase;
            color: var(--text-muted);
        }}

        .playground-controls {{
            display: flex;
            align-items: center;
            gap: 1rem;
            flex-wrap: wrap;
        }}

        .slider-wrap {{
            display: flex;
            align-items: center;
            gap: 0.75rem;
            font-family: var(--font-mono);
            font-size: 0.75rem;
            color: var(--text-secondary);
        }}

        input[type="range"] {{
            accent-color: #ffffff;
            cursor: pointer;
        }}

        .toggle-btn {{
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-secondary);
            font-family: var(--font-mono);
            font-size: 0.7rem;
            padding: 0.35rem 0.65rem;
            border-radius: 4px;
            cursor: pointer;
            transition: all 0.15s ease;
        }}

        .toggle-btn.active {{
            background: #fff;
            color: #000;
            border-color: #fff;
        }}

        .live-textarea {{
            width: 100%;
            background: var(--surface);
            border: 1px solid var(--border);
            color: #ffffff;
            padding: 1.5rem;
            border-radius: 4px;
            font-family: '{font_name}Bitmap32', monospace;
            font-size: 56px;
            line-height: 1.2;
            resize: vertical;
            min-height: 150px;
            transition: border-color 0.15s ease;
        }}

        .live-textarea:focus {{
            outline: none;
            border-color: var(--border-hover);
        }}

        /* Installation Guide Strip */
        .install-guide {{
            margin-top: 3.5rem;
            border-top: 1px solid var(--border);
            padding-top: 2rem;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 2rem;
            font-size: 0.85rem;
            color: var(--text-secondary);
        }}

        .guide-step strong {{
            color: #fff;
            font-size: 0.9rem;
            display: block;
            margin-bottom: 0.35rem;
        }}
    </style>
</head>
<body>

    <header>
        <div class="header-top">
            <h1 class="font-title">{font_config['fullname']}</h1>
            <span class="font-meta-tag">{font_name} / {len(glyph_list)} GLYPH(S)</span>
        </div>

        <div class="downloads-strip">
            <span class="downloads-label">BITMAP TTF:</span>
            <a href="{font_name}-Bitmap-32.ttf" class="download-pill primary" download>32PX (.TTF)</a>
            <a href="{font_name}-Bitmap-64.ttf" class="download-pill primary" download>64PX (.TTF)</a>
            <a href="{font_name}-Bitmap-128.ttf" class="download-pill primary" download>128PX (.TTF)</a>
            <span class="downloads-label" style="margin-left: 0.5rem;">VECTOR:</span>
            <a href="{font_name}-Vector.otf" class="download-pill" download>VECTOR (.OTF)</a>
            <a href="{font_name}-Vector.ttf" class="download-pill" download>VECTOR (.TTF)</a>
            <a href="{font_name}-Vector.woff2" class="download-pill" download>VECTOR (.WOFF2)</a>
            <span class="downloads-label" style="margin-left: 0.5rem;">OTHER:</span>
            <a href="{font_name}-ColorBitmap.ttf" class="download-pill" download>COLOR SBIX (.TTF)</a>
            <a href="{font_name}-Specimen.pdf" class="download-pill" download>SPECIMEN (.PDF)</a>
        </div>
    </header>

    <div class="controls-bar">
        <input type="text" class="filter-input" id="searchBox" placeholder="Filter codepoint or name..." oninput="filterRows()">
        <span class="count-indicator" id="countDisplay">{len(glyph_list)} GLYPH(S)</span>
    </div>

    <div class="gallery-list" id="galleryContainer">
        {all_rows}
    </div>

    <div class="playground">
        <div class="playground-header">
            <span class="playground-title">TYPOGRAPHIC PLAYGROUND</span>
            <div class="playground-controls">
                <button class="toggle-btn active" id="btnBm32" onclick="setTypingFont('bm32')">32PX BITMAP</button>
                <button class="toggle-btn" id="btnBm64" onclick="setTypingFont('bm64')">64PX BITMAP</button>
                <button class="toggle-btn" id="btnBm128" onclick="setTypingFont('bm128')">128PX BITMAP</button>
                <button class="toggle-btn" id="btnVector" onclick="setTypingFont('vector')">VECTOR (OTF)</button>
                <div class="slider-wrap">
                    <label for="sizeSlider">SIZE:</label>
                    <input type="range" id="sizeSlider" min="20" max="144" value="56" oninput="updateFontSize(this.value)">
                    <span id="sizeVal">56px</span>
                </div>
            </div>
        </div>
        <textarea class="live-textarea" id="liveText">{chr(glyph_list[0]['unicode_val'])} {chr(glyph_list[0]['unicode_val'])} {chr(glyph_list[0]['unicode_val'])}</textarea>
    </div>

    <div class="install-guide">
        <div class="guide-step">
            <strong>1. Install TrueType Bitmap Fonts</strong>
            Download <code>{font_name}-Bitmap-32.ttf</code>, <code>64.ttf</code>, or <code>128.ttf</code>. Double-click and click "Install Font" in macOS Font Book or Windows.
        </div>
        <div class="guide-step">
            <strong>2. Use in Standard Software</strong>
            Open Microsoft Word, Photoshop, Illustrator, or Figma. Select <code>{family_name} Bitmap 32px</code>, <code>64px</code>, or <code>128px</code> directly from the font family dropdown.
        </div>
        <div class="guide-step">
            <strong>3. Web & App Integration</strong>
            Use the corresponding <code>.woff2</code> web fonts with standard CSS <code>@font-face</code> for razor-sharp pixel typography.
        </div>
    </div>

    <script>
        function filterRows() {{
            const q = document.getElementById('searchBox').value.trim().toLowerCase();
            const rows = document.querySelectorAll('.gallery-item');
            let visible = 0;
            rows.forEach(r => {{
                const hex = r.getAttribute('data-hex');
                const name = r.getAttribute('data-name');
                if (hex.includes(q) || name.includes(q)) {{
                    r.style.display = 'grid';
                    visible++;
                }} else {{
                    r.style.display = 'none';
                }}
            }});
            document.getElementById('countDisplay').textContent = `${{visible}} OF ${{rows.length}} GLYPH(S)`;
        }}

        function copyValue(btn, text) {{
            navigator.clipboard.writeText(text).then(() => {{
                const prev = btn.textContent;
                btn.textContent = 'COPIED';
                setTimeout(() => btn.textContent = prev, 1400);
            }});
        }}

        function updateFontSize(val) {{
            document.getElementById('sizeVal').textContent = val + 'px';
            document.getElementById('liveText').style.fontSize = val + 'px';
        }}

        function setTypingFont(type) {{
            const ta = document.getElementById('liveText');
            const btns = ['btnBm32', 'btnBm64', 'btnBm128', 'btnVector'];
            btns.forEach(id => {{
                const el = document.getElementById(id);
                if (el) el.classList.remove('active');
            }});

            if (type === 'bm32') {{
                ta.style.fontFamily = "'{font_name}Bitmap32', monospace";
                document.getElementById('btnBm32').classList.add('active');
            }} else if (type === 'bm64') {{
                ta.style.fontFamily = "'{font_name}Bitmap64', monospace";
                document.getElementById('btnBm64').classList.add('active');
            }} else if (type === 'bm128') {{
                ta.style.fontFamily = "'{font_name}Bitmap128', monospace";
                document.getElementById('btnBm128').classList.add('active');
            }} else {{
                ta.style.fontFamily = "'{font_name}Vector', sans-serif";
                document.getElementById('btnVector').classList.add('active');
            }}
        }}
    </script>
</body>
</html>
"""
    with open(specimen_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f" -> Generated Minimalist Product Specimen: {specimen_path}")


# ==============================================================================
# Verification
# ==============================================================================

def verify_pipeline_outputs(build_dir, font_name, sizes=(32, 64, 128)):
    print_header("Pipeline Verification: Full Product Deliverables")
    checkpoints = []

    # TrueType Bitmap Fonts (Per Size)
    for sz in sizes:
        sz_ttf = os.path.join(build_dir, f"{font_name}-Bitmap-{sz}.ttf")
        if os.path.exists(sz_ttf):
            checkpoints.append(f"[✓] Full Bitmap Font ({sz}px TTF): {os.path.basename(sz_ttf)} ({os.path.getsize(sz_ttf) / 1024:.1f} KB)")
        else:
            checkpoints.append(f"[✗] Full Bitmap Font ({sz}px TTF): MISSING")

        sz_woff2 = os.path.join(build_dir, f"{font_name}-Bitmap-{sz}.woff2")
        if os.path.exists(sz_woff2):
            checkpoints.append(f"[✓] Full Bitmap Web Font ({sz}px WOFF2): {os.path.basename(sz_woff2)} ({os.path.getsize(sz_woff2) / 1024:.1f} KB)")

    # Default Bitmap Font Alias (TTF)
    bm_ttf = os.path.join(build_dir, f"{font_name}-Bitmap.ttf")
    if os.path.exists(bm_ttf):
        checkpoints.append(f"[✓] Full Installable Bitmap Font (Default TTF): {os.path.basename(bm_ttf)} ({os.path.getsize(bm_ttf) / 1024:.1f} KB)")

    # Full Vector Font OTF
    vec_otf = os.path.join(build_dir, f"{font_name}-Vector.otf")
    if os.path.exists(vec_otf):
        checkpoints.append(f"[✓] Full Vector Font (OTF): {os.path.basename(vec_otf)} ({os.path.getsize(vec_otf) / 1024:.1f} KB)")

    # Full Vector Font TTF
    vec_ttf = os.path.join(build_dir, f"{font_name}-Vector.ttf")
    if os.path.exists(vec_ttf):
        checkpoints.append(f"[✓] Full Vector Font (TTF): {os.path.basename(vec_ttf)} ({os.path.getsize(vec_ttf) / 1024:.1f} KB)")

    # Full Vector Font WOFF2
    vec_woff2 = os.path.join(build_dir, f"{font_name}-Vector.woff2")
    if os.path.exists(vec_woff2):
        checkpoints.append(f"[✓] Full Vector Web Font (WOFF2): {os.path.basename(vec_woff2)} ({os.path.getsize(vec_woff2) / 1024:.1f} KB)")

    # Full Color Bitmap Font
    col_ttf = os.path.join(build_dir, f"{font_name}-ColorBitmap.ttf")
    if os.path.exists(col_ttf):
        checkpoints.append(f"[✓] Full Color Bitmap Font (sbix TTF): {os.path.basename(col_ttf)} ({os.path.getsize(col_ttf) / 1024:.1f} KB)")

    # Specimen PDF & HTML
    pdf_path = os.path.join(build_dir, f"{font_name}-Specimen.pdf")
    html_path = os.path.join(build_dir, "specimen.html")
    if os.path.exists(pdf_path):
        checkpoints.append(f"[✓] Specimen PDF: {os.path.basename(pdf_path)}")
    if os.path.exists(html_path):
        checkpoints.append(f"[✓] List Gallery Specimen: {os.path.basename(html_path)}")

    for cp in checkpoints:
        print(f"  {cp}")
    print("=" * 70)


# ==============================================================================
# Main Runner
# ==============================================================================

def run_pipeline(
    input_dir="./svg",
    output_dir="./build",
    sizes=(32, 64, 128),
    fontname="Chu Nom Tam Chinh The Glyphs",
    familyname="Chu Nom Tam Chinh The Glyphs",
    fullname="Chu Nom Tam Chinh The Glyphs",
    version="1.000",
    ascent=800,
    descent=200,
    bearing=50,
    max_workers=4
):
    print_header("SVG to Full Installable Bitmap & Vector Font Pipeline")

    deps = check_dependencies()
    print("Environment & Tool Check:")
    print(f"  - Inkscape:  {deps.get('inkscape') or '[NOT FOUND]'}")
    print(f"  - FontForge: {deps.get('fontforge') or '[NOT FOUND]'}")
    print(f"  - AFDKO tx:  {deps.get('tx') or '[NOT FOUND]'}")
    print(f"  - fonttools: {'Available' if HAS_FONTTOOLS else '[NOT FOUND]'}")
    print(f"  - brotli:    {'Available' if HAS_BROTLI else '[NOT FOUND]'}")

    if not deps.get("inkscape") or not deps.get("fontforge"):
        print("\n[ERROR] Both 'inkscape' and 'fontforge' are required to run the pipeline.")
        sys.exit(1)

    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    svg_paths = sorted(glob.glob(os.path.join(input_dir, "*.svg")))
    if not svg_paths:
        print(f"\n[ERROR] No SVG files found in '{input_dir}'.")
        return False

    glyph_list = [parse_svg_glyph_info(p) for p in svg_paths if parse_svg_glyph_info(p)]
    if not glyph_list:
        print("\n[ERROR] No valid SVG files with identifiable Unicode codepoints found.")
        return False

    print(f"\nDiscovered {len(glyph_list)} valid SVG glyph(s):")
    for g in glyph_list:
        print(f"  - {g['filename']} -> Codepoint {g['unicode_hex']} (Name: {g['glyph_name']})")

    font_config = {
        "fontname": fontname,
        "familyname": familyname,
        "fullname": fullname,
        "version": version,
        "ascent": ascent,
        "descent": descent,
        "em": ascent + descent,
        "left_side_bearing": bearing,
        "right_side_bearing": bearing
    }

    # Step 1: Rasterize SVG directly to PNG
    print_step(1, 5, f"Rasterizing SVGs directly with Inkscape ({sizes} px)")
    png_mappings = rasterize_all_svgs(glyph_list, sizes, output_dir, max_workers=max_workers)

    # Step 2: Build Full Installable TrueType Bitmap Fonts (32px, 64px, 128px .ttf & .woff2)
    print_step(2, 5, f"Building Full Installable Bitmap Fonts: {fontname}-Bitmap (Sizes: {sizes})")
    generate_installable_bitmap_ttf(glyph_list, png_mappings, sizes, output_dir, fontname, familyname)

    # Step 3: Build Full Installable Vector Fonts (OTF, TTF, WOFF2)
    print_step(3, 5, f"Building Full Installable Vector Fonts: {fontname}-Vector.otf & .ttf")
    run_vector_fontforge_worker(glyph_list, font_config, output_dir)
    vec_otf = os.path.join(output_dir, f"{fontname}-Vector.otf")
    export_web_fonts(vec_otf, output_dir, fontname)

    # Step 4: Build Color Bitmap Font (sbix TTF)
    print_step(4, 5, f"Building Full Color Bitmap Font: {fontname}-ColorBitmap.ttf")
    vec_ttf = os.path.join(output_dir, f"{fontname}-Vector.ttf")
    color_ttf = os.path.join(output_dir, f"{fontname}-ColorBitmap.ttf")
    embed_sbix_table(vec_ttf, color_ttf, png_mappings, font_config["descent"], font_config["em"], fontname, familyname)

    # Background toolchain formats (BDF, BMFont)
    generate_direct_bdf_fonts(glyph_list, sizes, png_mappings, output_dir, fontname, familyname)
    generate_bmfont_descriptors(glyph_list, sizes, png_mappings, output_dir, fontname)

    # Step 5: Specimen PDF & Minimalist List Gallery
    print_step(5, 5, "Generating Specimen PDF & Minimalist Product Gallery")
    specimen_pdf = os.path.join(output_dir, f"{fontname}-Specimen.pdf")
    generate_pdf_synopsis(deps.get("tx"), vec_otf, specimen_pdf)
    generate_html_specimen(glyph_list, sizes, png_mappings, output_dir, font_config)

    # Verification
    verify_pipeline_outputs(output_dir, fontname, sizes=sizes)
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Comprehensive Pipeline to convert SVGs into Full Installable Bitmap and Vector Fonts."
    )
    parser.add_argument("--input-dir", "-i", default="./svg", help="Directory containing SVG files (default: ./svg)")
    parser.add_argument("--output-dir", "-o", default="./build", help="Output directory for build artifacts (default: ./build)")
    parser.add_argument("--sizes", "-s", default="32,64,128", help="Comma-separated bitmap pixel sizes (default: 32,64,128)")
    parser.add_argument("--font-name", default="Chu Nom Tam Chinh The Glyphs", help="PostScript font name (default: Chu Nom Tam Chinh The Glyphs)")
    parser.add_argument("--family-name", default="Chu Nom Tam Chinh The Glyphs", help="Font family name")
    parser.add_argument("--full-name", default="Chu Nom Tam Chinh The Glyphs", help="Full font name")
    parser.add_argument("--version", default="1.000", help="Font version string (default: 1.000)")
    parser.add_argument("--ascent", type=int, default=800, help="Ascent metric in font units (default: 800)")
    parser.add_argument("--descent", type=int, default=200, help="Descent metric in font units (default: 200)")
    parser.add_argument("--bearing", type=int, default=50, help="Side bearings in font units (default: 50)")
    parser.add_argument("--workers", type=int, default=4, help="Parallel workers for rasterization (default: 4)")

    args = parser.parse_args()
    sizes = tuple(int(x.strip()) for x in args.sizes.split(",") if x.strip())

    run_pipeline(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        sizes=sizes,
        fontname=args.font_name,
        familyname=args.family_name,
        fullname=args.full_name,
        version=args.version,
        ascent=args.ascent,
        descent=args.descent,
        bearing=args.bearing,
        max_workers=args.workers
    )


if __name__ == "__main__":
    main()
