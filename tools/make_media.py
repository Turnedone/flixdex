"""Regenerate the README media in docs/ (screenshots, demo GIF, banner, social preview).

Drives the real app offscreen. Needs Flixdex already set up on this machine (a TMDB key in
%APPDATA%\\Flixdex); it works on a temporary copy of that data with OMDb switched off, so the
images show exactly what a fresh install looks like. Requires Pillow (pip install pillow).

    .venv\\Scripts\\python tools\\make_media.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / 'docs'
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QPA_FONTDIR', r'C:\Windows\Fonts')

# --- work on a temporary copy of the app data, without OMDb or saved Rotten Tomatoes scores
src = Path(os.environ['APPDATA']) / 'Flixdex'
tmp = Path(tempfile.mkdtemp(prefix='flixdex-media-'))
(tmp / 'Flixdex').mkdir()
for f in src.iterdir():
    if f.is_file() and f.name != 'ratings.json':
        shutil.copy2(f, tmp / 'Flixdex' / f.name)
settings = json.loads((tmp / 'Flixdex' / 'settings.json').read_text(encoding='utf-8'))
settings['omdb'] = ''
settings['filters'] = {'svc': 'all', 'type': 'all', 'genre': '', 'sort': 'imdb'}
(tmp / 'Flixdex' / 'settings.json').write_text(json.dumps(settings), encoding='utf-8')
os.environ['APPDATA'] = str(tmp)

sys.path.insert(0, str(ROOT))
import flixdex as fx  # noqa: E402  (APPDATA must be set first)
from PIL import Image, ImageEnhance  # noqa: E402
from PySide6.QtCore import QBuffer, QEventLoop, QIODevice, QRectF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def to_pil(widget):
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    widget.grab().toImage().save(buf, 'PNG')
    return Image.open(io.BytesIO(bytes(buf.data()))).convert('RGB')


app = QApplication(sys.argv)
fx.setup_app(app)
w = fx.MainWindow()
w.resize(1280, 800)
w.show()
for _ in range(180):  # catalog, IMDb file and IMDb matching
    wait(1000)
    if not w.loading and w.imdb_table and not w.id_job:
        break
w.apply_filters()
wait(3000)  # posters


def click(group, prop, value):
    for b in group.buttons():
        if b.property(prop) == value:
            b.click()
    wait(2500)


def detail_frame(name, base):
    it = next(i for i in w.items if i['n'] == name)
    w.open_detail(it)
    wait(4000)
    d = to_pil(w.detail)
    w.detail.close()
    frame = ImageEnhance.Brightness(base).enhance(0.35)
    frame.paste(d, ((frame.width - d.width) // 2, (frame.height - d.height) // 2))
    return frame, d


# --- demo GIF: (frame, milliseconds)
frames = []
main_shot = to_pil(w)
frames.append((main_shot, 2200))
click(w.svc_group, 'svc', 'hbo')
hbo_shot = to_pil(w)
frames.append((hbo_shot, 1800))
click(w.type_group, 't', 'tv')
frames.append((to_pil(w), 1800))
click(w.svc_group, 'svc', 'all')
click(w.type_group, 't', 'all')
for i, text in enumerate(['s', 'st', 'stra', 'strang', 'stranger']):
    w.search.setText(text)
    wait(900 if i == 4 else 400)
    frames.append((to_pil(w), 1400 if i == 4 else 220))
frame, detail_shot = detail_frame('Stranger Things', to_pil(w))
frames.append((frame, 3200))
w.search.setText('')
w.genre.setCurrentIndex(max(0, w.genre.findData('Horror')))
wait(2500)
frames.append((to_pil(w), 2200))

DOCS.mkdir(exist_ok=True)
main_shot.save(DOCS / 'screenshot.png')
hbo_shot.save(DOCS / 'screenshot-hbo.png')
detail_shot.save(DOCS / 'screenshot-detail.png')

gif = [f.resize((960, int(f.height * 960 / f.width)), Image.LANCZOS) for f, _ in frames]
pal = [g.convert('P', palette=Image.ADAPTIVE, colors=200) for g in gif]
pal[0].save(DOCS / 'demo.gif', save_all=True, append_images=pal[1:], duration=[ms for _, ms in frames],
            loop=0, optimize=True, disposal=1)


# --- banner and social preview: logo and tagline over a wall of top-rated posters
def poster_wall(n):
    def rank(it):
        s = w.scores(it)
        return (s.get('iv') or 0) >= 50000, s.get('imdb') or 0

    rows = [it for it in sorted(w.items, key=rank, reverse=True) if it['p']][:n]
    out = []
    for it in rows:
        data = urllib.request.urlopen(fx.IMG + 'w342' + it['p'], timeout=20).read()
        pm = QPixmap()
        pm.loadFromData(data)
        out.append(pm)
    return out


posters = poster_wall(24)


def hero(width, height, title_px, tag_px, out):
    img = QImage(width, height, QImage.Format.Format_RGB32)
    img.fill(QColor(fx.C_BG))
    p = QPainter(img)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
                     | QPainter.RenderHint.TextAntialiasing)
    # tilted poster wall on the right
    p.save()
    p.translate(width * 0.42, -height * 0.25)
    p.rotate(-8)
    pw, ph, gap = 150, 225, 14
    cols = 6
    for i, pm in enumerate(posters):
        r, c = divmod(i, cols)
        x = c * (pw + gap) + (r % 2) * (pw // 2)
        y = r * (ph + gap)
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, pw, ph), 10, 10)
        p.setClipPath(path)
        p.drawPixmap(QRectF(x, y, pw, ph), pm, QRectF(pm.rect()))
        p.setClipping(False)
    p.restore()
    # fade the wall into the background on the left
    g = QLinearGradient(0, 0, width, 0)
    g.setColorAt(0.0, QColor(11, 11, 16, 255))
    g.setColorAt(0.42, QColor(11, 11, 16, 245))
    g.setColorAt(0.75, QColor(11, 11, 16, 120))
    g.setColorAt(1.0, QColor(11, 11, 16, 60))
    p.fillRect(img.rect(), g)
    # logo, name, tagline
    x0 = int(width * 0.06)
    cy = height // 2
    icon = fx.app_icon().pixmap(title_px, title_px)
    p.drawPixmap(x0, cy - title_px - 6, icon)
    f = QFont('Segoe UI', 1, QFont.Weight.Black)
    f.setPixelSize(title_px)
    p.setFont(f)
    p.setPen(QColor(fx.C_TEXT))
    tx = x0 + title_px + 16
    p.drawText(tx, cy - 14, 'Flix')
    p.setPen(QColor(fx.C_ACCENT))
    p.drawText(tx + p.fontMetrics().horizontalAdvance('Flix'), cy - 14, 'dex')
    f2 = QFont('Segoe UI', 1, QFont.Weight.DemiBold)
    f2.setPixelSize(tag_px)
    p.setFont(f2)
    p.setPen(QColor(fx.C_TEXT))
    p.drawText(x0, cy + tag_px + 14, 'Every Netflix & HBO Max title, rated.')
    f3 = QFont('Segoe UI', 1)
    f3.setPixelSize(int(tag_px * 0.62))
    p.setFont(f3)
    p.setPen(QColor(fx.C_MUTED))
    p.drawText(x0, cy + tag_px * 2 + 26, 'IMDb ratings for every title · Free · Windows')
    p.end()
    img.save(str(out))


hero(1280, 400, 84, 30, DOCS / 'banner.png')
hero(1280, 640, 110, 40, DOCS / 'social-preview.png')

w.close()
shutil.rmtree(tmp, ignore_errors=True)
for f in sorted(DOCS.iterdir()):
    print(f'{f.name:28} {f.stat().st_size / 1024:8.0f} KB')
