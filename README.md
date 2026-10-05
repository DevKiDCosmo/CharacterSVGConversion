# SVG to Vector & Bitmap Font Pipeline

A professional, high-performance pipeline that converts individual character SVGs into both **modern vector fonts** (OTF, TTF, WOFF, WOFF2) and **multi-format bitmap fonts** (OpenType `sbix` color bitmaps, TrueType `EBDT`/`EBLC` embedded bitmaps, classic BDF fonts, and AngelCode BMFont).

---

## Features & Improvements

### 1. Vector Fonts
- **OpenType CFF (`glyphs.otf`)**: Clean PostScript outlines, optimal for desktop publishing and printing.
- **TrueType glyf (`glyphs.ttf`)**: Standard TrueType quadratic spline outlines.
- **Web Fonts (`glyphs.woff`, `glyphs.woff2`)**: Compressed web fonts using Brotli compression, ready for web `@font-face` usage.
- **Overlap & Winding Healing**: Applies a 10x integer scaling coordinate transform, contour direction correction, and `removeOverlap()` pass to eliminate FontForge's integer quantization hole bug on complex overlapping stroke polygons (such as Hanzi and Chữ Nôm characters).

### 2. Real Bitmap Fonts (Rendered Directly from SVG Pixels)
Unlike conventional pipelines that auto-trace or scan-convert internal FontForge vector glyph outlines, this pipeline **rasterizes the SVG directly via Inkscape** into crisp pixel matrices and builds authentic bitmap fonts directly from the pixel data:

- **Classic BDF Bitmap Fonts (`bdf/CustomIconFont-<size>.bdf`)**:
  - Full **ISO10646-1** Unicode character encoding.
  - Constructed directly from SVG raster bits (via `bdflib`), ensuring exact pixel fidelity without FontForge vector re-rasterization artifacts.
- **Pure TrueType Bitmap Fonts (`bitmap_pure_<size>.ttf`)**:
  - Contains **only** `EBDT` (Embedded Bitmap Data) and `EBLC` (Embedded Bitmap Location) tables, with **zero vector outlines**.
  - Generated directly from the SVG-rendered BDF.
- **Pure OpenType Bitmap Fonts (`otb/CustomIconFont-<size>.otb`)**:
  - Pure bitmap format standard for Linux, FreeType, X11, and terminal emulators.
- **TrueType & Web Pixel Fonts (`pixel_font_<size>.ttf`, `pixel_font_<size>.woff2`)**:
  - Every pixel from the SVG rasterization becomes a crisp, non-overlapping square block.
  - Can be used in **any modern web browser via `@font-face`** or installed on macOS/Windows/Linux, rendering as an authentic, pixelated bitmap font anywhere!
- **OpenType Color Bitmap (`bitmap_glyphs.ttf`)**:
  - Embedded Apple/Windows **`sbix`** table containing PNG strikes at configurable resolutions (32px, 64px, 128px) with accurate baseline offsets.
- **AngelCode BMFont (`bmfont/<size>px/CustomIconFont.fnt`)**:
  - Standard `.fnt` descriptor files accompanied by individual PNG glyph files for game engines (Unity, Unreal Engine, Godot, PixiJS, Phaser, Cocos).
- **Standalone Rasterized PNGs (`bitmaps/<size>px/<hex>_<size>.png`)**:
  - Multi-resolution PNG assets rendered directly from SVG.

### 3. Visual Quality Inspection & Specimen
- **PDF Character Synopsis (`bitmap_synopsis.pdf`)**: Generated via AFDKO `tx -pdf` displaying character matrix, bounding boxes, advance widths, and contour vectors.
- **Interactive HTML Specimen (`specimen.html`)**: Interactive dark-mode web application featuring:
  - Live `@font-face` rendering.
  - Interactive size slider (24px to 160px).
  - Real-time search/filter by Unicode hex or character name.
  - Side-by-side comparison of vector rendering vs 32px, 64px, and 128px bitmap strikes.

---

## File Naming Conventions

Place your SVG files in the `./svg` directory. Filenames can use any of the following standard patterns:

| Pattern | Example | Unicode Codepoint | Standard Glyph Name |
|---|---|---|---|
| `U+<HEX>-<name>.svg` | `U+E001-shoe.svg` | `U+E001` (0xE001) | `uniE001` |
| `U+<HEX>_<name>.svg` | `U+E001_shoe.svg` | `U+E001` (0xE001) | `uniE001` |
| `uni<HEX>.svg` | `uniE001.svg` | `U+E001` (0xE001) | `uniE001` |
| `u<HEX>.svg` | `uE001.svg` | `U+E001` (0xE001) | `uniE001` |
| `0x<HEX>.svg` | `0xE001.svg` | `U+E001` (0xE001) | `uniE001` |
| `<HEX>.svg` | `E001.svg` | `U+E001` (0xE001) | `uniE001` |

*Supports 4-digit to 6-digit hexadecimal codes (BMP U+0000–U+FFFF and Supplementary Planes U+10000–U+10FFFF).*

---

## Quick Start

Activate the virtual environment and run the pipeline:

```bash
# In the repository root:
./.v2/bin/python pipeline.py
```

### Command Line Options

```text
usage: pipeline.py [-h] [--input-dir INPUT_DIR] [--output-dir OUTPUT_DIR]
                   [--sizes SIZES] [--font-name FONT_NAME]
                   [--family-name FAMILY_NAME] [--full-name FULL_NAME]
                   [--version VERSION] [--ascent ASCENT] [--descent DESCENT]
                   [--bearing BEARING] [--workers WORKERS]

Options:
  -h, --help            Show this help message and exit
  --input-dir, -i       Directory containing SVG files (default: ./svg)
  --output-dir, -o      Output directory for build artifacts (default: ./build)
  --sizes, -s           Comma-separated pixel sizes for bitmaps (default: 32,64,128)
  --font-name           PostScript font name (default: CustomIconFont)
  --family-name         Font family name (default: Chu Nom Tam Chinh The Glyphs)
  --full-name           Full font name (default: Chu Nom Tam Chinh The Glyphs)
  --version             Font version string (default: 1.000)
  --ascent              Ascent in font units (default: 800)
  --descent             Descent in font units (default: 200)
  --bearing             Side bearings in font units (default: 50)
  --workers             Number of parallel rasterization workers (default: 4)
```

### Examples

Generate custom bitmap sizes (e.g. 16, 24, 32, 48, 64, 128):
```bash
./.v2/bin/python pipeline.py --sizes 16,24,32,48,64,128
```

Specify custom font metadata and directories:
```bash
./.v2/bin/python pipeline.py \
  --input-dir ./svg \
  --output-dir ./build \
  --font-name "NomFont" \
  --family-name "Nom Character Font" \
  --sizes 32,64,128
```

---

## Generated Directory Structure

## Generated Products

All full installable products are located directly in `build/`:

```text
build/
├── CustomIconFont-Bitmap.ttf      # Full TrueType Bitmap font (double-click installable in OS)
├── CustomIconFont-Bitmap.woff2    # Compressed Web Font of the bitmap font
├── CustomIconFont-Vector.otf      # Full OpenType Vector font (CFF outlines)
├── CustomIconFont-Vector.ttf      # Full TrueType Vector font (glyf outlines)
├── CustomIconFont-Vector.woff2    # Compressed Web Font of the vector font
├── CustomIconFont-ColorBitmap.ttf # OpenType sbix color bitmap font
├── CustomIconFont-Specimen.pdf    # Character specimen PDF
├── specimen.html                  # Minimalist black & white specimen viewer
├── bdf/                           # Backend classic BDF fonts
├── otb/                           # Backend OpenType Bitmap fonts
├── bmfont/                        # Backend AngelCode .fnt packages
└── bitmaps/                       # Backend standalone PNG strikes
```

### Installing and Using the Bitmap Font Normally

1. **Mac**: Double-click `CustomIconFont-Bitmap.ttf` and click **Install Font** in Font Book.
2. **Windows**: Right-click `CustomIconFont-Bitmap.ttf` and select **Install**.
3. **In Software**: Open **Microsoft Word, Photoshop, Illustrator, Figma**, or any text editor. In the font selector, pick `Chu Nom Tam Chinh The Glyphs Bitmap` and type normally!

