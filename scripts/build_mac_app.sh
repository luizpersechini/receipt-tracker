#!/bin/bash
#
# Build a double-clickable macOS .app that launches Receipt Tracker:
#   - starts Ollama (if installed and not already running)
#   - starts the FastAPI server (if not already up)
#   - opens http://localhost:8000 in the default browser
#   - stays alive in the Dock; quitting it stops the server it started
#
# Usage:
#   scripts/build_mac_app.sh [output_dir]
#
# Default output_dir is ~/Applications. Re-run any time to rebuild.
#
set -euo pipefail

APP_NAME="Receipt Tracker"
PORT=8000

# Resolve the project root from this script's location so the launcher gets
# an absolute path regardless of where it's later run from (Finder gives a
# minimal environment with cwd = /).
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$HOME/Applications}"
APP_DIR="$OUT_DIR/$APP_NAME.app"
CONTENTS="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RES_DIR="$CONTENTS/Resources"

echo "Project : $PROJECT_ROOT"
echo "Output  : $APP_DIR"

rm -rf "$APP_DIR"
mkdir -p "$MACOS_DIR" "$RES_DIR"

# ── Info.plist ──────────────────────────────────────────────────────────────
cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>com.luiz.receipttracker</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# ── Launcher executable ─────────────────────────────────────────────────────
cat > "$MACOS_DIR/launcher" <<LAUNCHER
#!/bin/bash
# Finder launches with a bare environment — set a sane PATH so we can find
# python3, ollama, curl, and open.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PROJECT="$PROJECT_ROOT"
PORT=$PORT
URL="http://localhost:\${PORT}/"
LOG="\$PROJECT/data/app_launcher.log"

# The app requires Python 3.10+ (Starlette/Pillow security pins + union-type
# syntax). Prefer the project's venv; fall back to a system python only if
# it's new enough, otherwise bail with a clear message.
if [ -x "\$PROJECT/.venv/bin/python" ]; then
  PY="\$PROJECT/.venv/bin/python"
else
  PY=""
  for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    p="\$(command -v \$cand 2>/dev/null)" || true
    if [ -n "\$p" ] && "\$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PY="\$p"; break
    fi
  done
  if [ -z "\$PY" ]; then
    mkdir -p "\$PROJECT/data"
    echo "No Python 3.10+ found and no .venv. Create one: uv venv --python 3.13 && uv pip sync requirements.lock" > "\$LOG"
    osascript -e 'display alert "Receipt Tracker" message "No Python 3.10+ environment found.\n\nIn the project folder run:\n  uv venv --python 3.13\n  uv pip sync requirements.lock\n\nThen relaunch."'
    exit 1
  fi
fi

# Finder/LaunchServices may start this app under x86_64 (Rosetta). The
# installed Python packages are built for the native arch, so force the
# matching slice or imports like pydantic_core fail with an "incompatible
# architecture" error. A venv python is already arch-specific, so only
# apply the prefix when using a universal system python3.
ARCH_PREFIX=""
case "\$PY" in
  */.venv/bin/python) : ;;  # venv binary — no arch coercion needed
  *)
    if [ "\$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
      ARCH_PREFIX="arch -arm64"
    fi
    ;;
esac

cd "\$PROJECT" 2>/dev/null || {
  osascript -e 'display alert "Receipt Tracker" message "Project folder not found:\n'"\$PROJECT"'"'
  exit 1
}

# Start Ollama if installed and not already serving (the OCR text step uses it).
if command -v ollama >/dev/null 2>&1; then
  if ! curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    nohup ollama serve >/dev/null 2>&1 &
  fi
fi

# Already running? Just open the browser and exit.
if curl -sf "\$URL" -o /dev/null 2>&1; then
  open "\$URL"
  exit 0
fi

mkdir -p "\$PROJECT/data"
\$ARCH_PREFIX "\$PY" -m uvicorn src.main:app --host 127.0.0.1 --port "\$PORT" >"\$LOG" 2>&1 &
SERVER_PID=\$!
# When this app quits (Dock → Quit) or is interrupted, stop the server too.
trap 'kill \$SERVER_PID 2>/dev/null' EXIT INT TERM

# Wait for readiness — first run warms the OCR model, so allow ~40s.
for i in \$(seq 1 40); do
  curl -sf "\$URL" -o /dev/null 2>&1 && break
  kill -0 \$SERVER_PID 2>/dev/null || {
    osascript -e 'display alert "Receipt Tracker" message "Server failed to start. See data/app_launcher.log."'
    exit 1
  }
  sleep 1
done

open "\$URL"
wait \$SERVER_PID
LAUNCHER
chmod +x "$MACOS_DIR/launcher"

# ── Icon ────────────────────────────────────────────────────────────────────
# Render a 1024px master PNG (blue squircle + white receipt glyph) with PIL,
# then build a .icns via iconutil.
TMP_ICONSET="$(mktemp -d)/icon.iconset"
mkdir -p "$TMP_ICONSET"
MASTER_PNG="$(mktemp -d)/icon_1024.png"

/usr/bin/python3 - "$MASTER_PNG" <<'PY'
import sys
from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Rounded-rect "app tile" with a vertical blue gradient.
margin = 96
radius = 230
top = (59, 130, 246)     # #3B82F6
bot = (37, 99, 235)      # #2563EB
tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / (S - 1)
    r = int(top[0] + (bot[0] - top[0]) * t)
    g = int(top[1] + (bot[1] - top[1]) * t)
    b = int(top[2] + (bot[2] - top[2]) * t)
    gd.line([(0, y), (S, y)], fill=(r, g, b, 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle(
    [margin, margin, S - margin, S - margin], radius=radius, fill=255)
tile.paste(grad, (0, 0), mask)
img = Image.alpha_composite(img, tile)
d = ImageDraw.Draw(img)

# White receipt/document glyph, centered.
px0, py0, px1, py1 = 360, 300, 664, 724
fold = 88  # folded top-right corner size
white = (255, 255, 255, 255)
# Page body as a polygon with a folded corner.
page = [
    (px0, py0),
    (px1 - fold, py0),
    (px1, py0 + fold),
    (px1, py1),
    (px0, py1),
]
d.polygon(page, fill=white)
# Round-ish bottom by overpainting small corner squares is overkill; keep crisp.
# Folded-corner triangle, slightly darker so it reads as a fold.
d.polygon([(px1 - fold, py0), (px1, py0 + fold), (px1 - fold, py0 + fold)],
          fill=(214, 224, 245, 255))
# "Text" lines in accent blue.
line_color = (37, 99, 235, 255)
lx0, lx1 = px0 + 46, px1 - 46
lw = 26
for ly in (px0 and 430, 510, 590):
    d.rounded_rectangle([lx0, ly, lx1, ly + lw], radius=lw // 2, fill=line_color)

img.save(sys.argv[1])
PY

# Generate the iconset sizes from the master.
for sz in 16 32 64 128 256 512; do
  sips -z $sz $sz "$MASTER_PNG" --out "$TMP_ICONSET/icon_${sz}x${sz}.png" >/dev/null
  d2=$((sz * 2))
  sips -z $d2 $d2 "$MASTER_PNG" --out "$TMP_ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
done
# 1024 master doubles as 512@2x.
cp "$MASTER_PNG" "$TMP_ICONSET/icon_512x512@2x.png"

iconutil -c icns "$TMP_ICONSET" -o "$RES_DIR/icon.icns"

# Refresh Finder's icon cache for this bundle.
touch "$APP_DIR"

echo "Built: $APP_DIR"
echo "Tip: drag it to your Dock, or double-click to launch."
