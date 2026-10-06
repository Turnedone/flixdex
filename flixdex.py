"""Flixdex: browse the current Netflix and HBO Max catalogs with IMDb and Rotten Tomatoes scores.

Catalog and images come from TMDB (streaming availability by JustWatch). IMDb ratings come from IMDb's free
daily ratings file, matched to titles via TMDB's IMDb ids. Rotten Tomatoes and Metacritic come from OMDb (movies
only: OMDb has almost no RT scores for series). Every lookup is stored on disk so a title is never looked up twice.
"""
import gzip
import json
import os
import sys
import threading
import time
import unicodedata
from array import array
from bisect import bisect_left
from collections import OrderedDict, deque
from pathlib import Path

from PySide6.QtCore import (QAbstractListModel, QLocale, QModelIndex, QObject, QRect, QRectF, QSize, Qt, QTimer, QUrl,
                            QUrlQuery, Signal)
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QKeySequence, QLinearGradient,
                           QPainter, QPainterPath, QPalette, QPen, QPixmap, QShortcut)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListView, QMainWindow, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
                               QSlider, QSpinBox, QStackedWidget, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

APP_DIR = Path(os.environ.get('APPDATA', Path.home())) / 'Flixdex'
IMG = 'https://image.tmdb.org/t/p/'
# Streaming services: TMDB watch-provider id, and the Wikidata property holding each title's id on that service.
PROVIDERS = [
    {'key': 'netflix', 'id': 8, 'name': 'Netflix', 'short': 'N', 'color': '#e50914', 'wd': 'P1874',
     'url': 'https://www.netflix.com/title/{}', 'search': 'https://www.netflix.com/search?q={}'},
    {'key': 'hbo', 'id': 1899, 'name': 'HBO Max', 'short': 'HBO', 'color': '#5b2fd6', 'wd': 'P8298',
     'url': 'https://play.hbomax.com/{}', 'search': 'https://play.hbomax.com/search?q={}'},
]
PROV = {p['key']: p for p in PROVIDERS}
CATALOG_TTL = 24 * 3600
MISS_RETRY = 30 * 24 * 3600  # re-check titles OMDb didn't have after this long
OMDB_DAILY = 1000
IMDB_URL = 'https://datasets.imdbws.com/title.ratings.tsv.gz'  # free for personal, non-commercial use
IMDB_TTL = 24 * 3600
MIN_VOTES = 1000  # IMDb sort ranks titles with fewer votes after the rest
ID_MISS_RETRY = 7 * 24 * 3600  # new titles often get their IMDb id on TMDB a little later
# TMDB's TV genre list has no Horror, Thriller or Romance, so series get those from their keywords.
KEYWORD_GENRES = {'horror': 'Horror', 'thriller': 'Thriller', 'romance': 'Romance'}
GENRE_SPLIT = {'Action & Adventure': ['Action', 'Adventure'], 'Sci-Fi & Fantasy': ['Science Fiction', 'Fantasy'],
               'War & Politics': ['War', 'Politics']}
FALLBACK_REGIONS = {'US': 'United States', 'GB': 'United Kingdom', 'CA': 'Canada', 'AU': 'Australia',
                    'IE': 'Ireland', 'DE': 'Germany', 'FR': 'France', 'ES': 'Spain', 'IT': 'Italy',
                    'NL': 'Netherlands', 'GR': 'Greece', 'CY': 'Cyprus', 'SE': 'Sweden', 'NO': 'Norway',
                    'DK': 'Denmark', 'FI': 'Finland', 'PL': 'Poland', 'PT': 'Portugal', 'BR': 'Brazil',
                    'MX': 'Mexico', 'AR': 'Argentina', 'IN': 'India', 'JP': 'Japan', 'KR': 'South Korea',
                    'TR': 'Turkey'}
DEFAULT_F = {'q': '', 'svc': 'all', 'type': 'all', 'genre': '', 'yMin': 0, 'yMax': 0, 'minImdb': 0.0, 'minRt': 0,
             'minTmdb': 0.0, 'sort': 'pop'}
SORT_OPTIONS = [('pop', 'Most popular'), ('imdb', 'IMDb rating'), ('rt', 'Rotten Tomatoes'), ('tmdb', 'TMDB rating'),
                ('new', 'Newest'), ('old', 'Oldest'), ('title', 'Title A–Z')]

C_BG, C_PANEL, C_PANEL2, C_LINE = '#0b0b10', '#13131a', '#1b1b24', '#272733'
C_TEXT, C_MUTED, C_ACCENT, C_IMDB = '#ececf2', '#9696a8', '#e50914', '#f5c518'
C_FRESH, C_ROTTEN = '#fa320a', '#7cb342'


def norm(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if not unicodedata.combining(c)).lower()


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    try:
        tmp.write_text(json.dumps(data, separators=(',', ':')), encoding='utf-8')
        tmp.replace(path)
    except OSError as e:
        print('save failed', path, e)


def today():
    return time.strftime('%Y-%m-%d')


# ---------------------------------------------------------------- network

class Net(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.nam = QNetworkAccessManager(self)
        self.img = QNetworkAccessManager(self)
        cache = QNetworkDiskCache(self)
        cache.setCacheDirectory(str(APP_DIR / 'image-cache'))
        cache.setMaximumCacheSize(500 * 1024 * 1024)
        self.img.setCache(cache)
        self.tmdb_key = ''

    def get(self, url, on_ok, on_err=None, headers=None, retries=3):
        req = QNetworkRequest(url if isinstance(url, QUrl) else QUrl(url))
        req.setRawHeader(b'Accept', b'application/json')
        for k, v in (headers or {}).items():
            req.setRawHeader(k.encode(), v.encode())
        reply = self.nam.get(req)

        def done():
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
            raw = bytes(reply.readAll())
            ok = reply.error() == QNetworkReply.NetworkError.NoError
            err = reply.errorString()
            reply.deleteLater()
            if status == 429 and retries > 0:
                QTimer.singleShot(1000 * (4 - retries), lambda: self.get(url, on_ok, on_err, headers, retries - 1))
                return
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = None
            if ok and body is not None:
                on_ok(body)
            elif on_err:
                msg = body.get('status_message') if isinstance(body, dict) else None
                on_err(status, msg or err, body)

        reply.finished.connect(done)

    def tmdb(self, path, params=None, on_ok=None, on_err=None, key=None):
        url = QUrl('https://api.themoviedb.org/3' + path)
        q, headers = QUrlQuery(), {}
        key = (key if key is not None else self.tmdb_key).strip()
        if key.startswith('eyJ'):
            headers['Authorization'] = 'Bearer ' + key
        else:
            q.addQueryItem('api_key', key)
        for k, v in (params or {}).items():
            q.addQueryItem(k, str(v))
        url.setQuery(q)
        self.get(url, on_ok, on_err, headers)

    def download(self, url, on_done, on_progress=None):
        reply = self.nam.get(QNetworkRequest(QUrl(url)))
        if on_progress:
            reply.downloadProgress.connect(on_progress)

        def done():
            ok = reply.error() == QNetworkReply.NetworkError.NoError
            data = bytes(reply.readAll()) if ok else None
            reply.deleteLater()
            on_done(data)

        reply.finished.connect(done)

    def image(self, url, on_done):
        req = QNetworkRequest(QUrl(url))
        req.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                         QNetworkRequest.CacheLoadControl.PreferCache)
        reply = self.img.get(req)

        def done():
            data = bytes(reply.readAll())
            ok = reply.error() == QNetworkReply.NetworkError.NoError
            reply.deleteLater()
            pm = QPixmap()
            on_done(pm if ok and pm.loadFromData(data) else None)

        reply.finished.connect(done)


class Bridge(QObject):
    """Carries results from worker threads back to the UI thread."""
    imdb_ready = Signal(object)


def parse_imdb_ratings(path):
    """Read title.ratings.tsv.gz into three parallel arrays sorted by numeric IMDb id (about 20 MB in memory).

    The parsed arrays are cached next to the file, so only the first start after each daily download pays
    for parsing.
    """
    path = Path(path)
    cache = path.with_name('imdb_ratings.bin')
    if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
        try:
            with open(cache, 'rb') as f:
                n = int.from_bytes(f.read(8), 'little')
                ids, rat, votes = array('I'), array('H'), array('I')
                for a in (ids, rat, votes):
                    a.fromfile(f, n)
            return ids, rat, votes
        except (OSError, EOFError, ValueError):
            pass

    ids, rat, votes = array('I'), array('H'), array('I')
    lines = gzip.decompress(path.read_bytes()).split(b'\n')
    for line in lines[1:]:
        if not line:
            continue
        tconst, avg, num = line.split(b'\t')
        ids.append(int(tconst[2:]))
        rat.append(int(avg.replace(b'.', b'')) if b'.' in avg else int(avg) * 10)  # always one decimal
        votes.append(int(num))
    del lines
    if any(ids[i] > ids[i + 1] for i in range(len(ids) - 1)):
        order = sorted(range(len(ids)), key=ids.__getitem__)
        ids, rat, votes = (array(a.typecode, (a[i] for i in order)) for a in (ids, rat, votes))
    if len(ids) < 100000:
        raise ValueError(f'IMDb ratings file looks incomplete ({len(ids)} rows)')
    try:
        with open(cache, 'wb') as f:
            f.write(len(ids).to_bytes(8, 'little'))
            for a in (ids, rat, votes):
                a.tofile(f)
    except OSError:
        pass
    return ids, rat, votes


# ---------------------------------------------------------------- grid

class TitleModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self.rows, self.pos = [], {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        it = self.rows[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return it
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return it['n']
        return None

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.pos = {it['k']: i for i, it in enumerate(rows)}
        self.endResetModel()

    def refresh(self, k):
        i = self.pos.get(k)
        if i is not None:
            idx = self.index(i)
            self.dataChanged.emit(idx, idx)


def px_font(size, weight=QFont.Weight.Normal):
    f = QFont(QApplication.font())
    f.setPixelSize(size)
    f.setWeight(weight)
    return f


class CardDelegate(QStyledItemDelegate):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.f_tag = px_font(9, QFont.Weight.Bold)
        self.f_title = px_font(13, QFont.Weight.DemiBold)
        self.f_sub = px_font(11)
        self.f_badge = px_font(11, QFont.Weight.Bold)
        self.f_imdb = px_font(8, QFont.Weight.Black)
        self.f_ph = px_font(13, QFont.Weight.DemiBold)

    def sizeHint(self, option, index):
        return self.win.view.gridSize()

    def paint(self, p, opt, index):
        it = index.data(Qt.ItemDataRole.UserRole)
        if not it:
            return
        p.save()
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
                         | QPainter.RenderHint.TextAntialiasing)
        r = opt.rect.adjusted(8, 8, -8, -8)
        pw, ph = r.width(), int(r.width() * 1.5)
        hover = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        sel = bool(opt.state & QStyle.StateFlag.State_Selected)
        prect = QRect(r.left(), r.top() - (3 if hover else 0), pw, ph)
        path = QPainterPath()
        path.addRoundedRect(QRectF(prect), 10, 10)

        p.setClipPath(path)
        pm = self.win.poster(it)
        if pm:
            # cover-crop the poster into the card
            sw, sh = pm.width(), pm.height()
            scale = max(pw / sw, ph / sh)
            cw, ch = pw / scale, ph / scale
            p.drawPixmap(QRectF(prect), pm, QRectF((sw - cw) / 2, (sh - ch) / 2, cw, ch))
        else:
            g = QLinearGradient(prect.topLeft(), prect.bottomRight())
            g.setColorAt(0, QColor('#1d1d28'))
            g.setColorAt(1, QColor('#121219'))
            p.fillRect(prect, g)
            p.setPen(QColor(C_MUTED))
            p.setFont(self.f_ph)
            p.drawText(prect.adjusted(12, 12, -12, -12), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       it['n'])
        p.setClipping(False)
        if hover or sel:
            p.setPen(QPen(QColor(C_TEXT) if sel else QColor(255, 255, 255, 70), 2 if sel else 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

        # type tag
        tv = it['t'] == 'tv'
        tag = 'SERIES' if tv else 'MOVIE'
        p.setFont(self.f_tag)
        tw = QFontMetrics(self.f_tag).horizontalAdvance(tag) + 12
        trect = QRect(prect.left() + 8, prect.top() + 8, tw, 18)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 185))
        p.drawRoundedRect(trect, 5, 5)
        p.setPen(QColor('#7dd3fc' if tv else '#ffffff'))
        p.drawText(trect, Qt.AlignmentFlag.AlignCenter, tag)

        # streaming-service badges, top right
        sx = prect.right() - 8
        for key in reversed(it.get('sv') or []):
            prov = PROV[key]
            sw = QFontMetrics(self.f_tag).horizontalAdvance(prov['short']) + 12
            srect = QRect(sx - sw, prect.top() + 8, sw, 18)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(prov['color']))
            p.drawRoundedRect(srect, 5, 5)
            p.setPen(QColor('#ffffff'))
            p.drawText(srect, Qt.AlignmentFlag.AlignCenter, prov['short'])
            sx -= sw + 4

        # text
        y = r.top() + ph + 8
        p.setFont(self.f_title)
        p.setPen(QColor(C_TEXT))
        p.drawText(QRect(r.left() + 2, y, pw - 4, 18), Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(self.f_title).elidedText(it['n'], Qt.TextElideMode.ElideRight, pw - 4))
        y += 19
        sub = ' · '.join(x for x in [str(it['y']) if it['y'] else '', ', '.join(it['g'][:2])] if x)
        p.setFont(self.f_sub)
        p.setPen(QColor(C_MUTED))
        p.drawText(QRect(r.left() + 2, y, pw - 4, 16), Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(self.f_sub).elidedText(sub, Qt.TextElideMode.ElideRight, pw - 4))
        y += 22
        p.setClipRect(QRect(r.left(), y, pw, 24))
        self.draw_badges(p, it, r.left() + 2, y, r.left() + pw)
        p.setClipping(False)
        p.restore()
        self.win.want_rating(it)

    def chip(self, p, x, y, w):
        p.setPen(QPen(QColor(C_LINE), 1))
        p.setBrush(QColor(C_PANEL2))
        p.drawRoundedRect(QRectF(x + .5, y + .5, w, 21), 5, 5)

    def draw_badges(self, p, it, x, y, right):
        r = self.win.scores(it)
        fm = QFontMetrics(self.f_badge)
        drawn = False
        if r.get('imdb') is not None:
            txt = f"{r['imdb']:.1f}"
            w = 34 + fm.horizontalAdvance(txt) + 8
            self.chip(p, x, y, w)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C_IMDB))
            p.drawRoundedRect(QRectF(x + 5, y + 5, 26, 12), 2, 2)
            p.setFont(self.f_imdb)
            p.setPen(QColor('#000'))
            p.drawText(QRectF(x + 5, y + 5, 26, 12), Qt.AlignmentFlag.AlignCenter, 'IMDb')
            p.setFont(self.f_badge)
            p.setPen(QColor(C_TEXT))
            p.drawText(QRectF(x + 35, y, w - 35, 22), Qt.AlignmentFlag.AlignVCenter, txt)
            x += w + 6
            drawn = True
        if r.get('rt') is not None and (not drawn or x + 24 + fm.horizontalAdvance(f"{r['rt']}%") + 8 < right):
            txt = f"{r['rt']}%"
            w = 24 + fm.horizontalAdvance(txt) + 8
            self.chip(p, x, y, w)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C_FRESH if r['rt'] >= 60 else C_ROTTEN))
            p.drawEllipse(QRectF(x + 7, y + 6, 10, 10))
            p.setFont(self.f_badge)
            p.setPen(QColor(C_TEXT))
            p.drawText(QRectF(x + 22, y, w - 22, 22), Qt.AlignmentFlag.AlignVCenter, txt)
            drawn = True
        if drawn:
            return
        if self.win.rating_pending(it):
            self.chip(p, x, y, 54)
            p.setFont(self.f_badge)
            p.setPen(QColor(C_MUTED))
            p.drawText(QRectF(x, y, 54, 22), Qt.AlignmentFlag.AlignCenter, '···')
        elif it['vc'] >= 5:
            txt = f"TMDB {it['v']:.1f}"
            w = fm.horizontalAdvance(txt) + 14
            self.chip(p, x, y, w)
            p.setFont(self.f_badge)
            p.setPen(QColor(C_MUTED))
            p.drawText(QRectF(x, y, w, 22), Qt.AlignmentFlag.AlignCenter, txt)


class CardView(QListView):
    def __init__(self):
        super().__init__()
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setWrapping(True)
        self.setSpacing(0)
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(40)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.empty_text = ''
        self.setGridSize(QSize(180, 350))

    def resizeEvent(self, e):
        self.update_grid()
        super().resizeEvent(e)

    def update_grid(self):
        w = self.viewport().width() - 1
        min_w = 175 if w > 700 else 150
        cols = max(1, w // min_w)
        cw = w // cols
        ch = 16 + int((cw - 16) * 1.5) + 76
        if self.gridSize() != QSize(cw, ch):
            self.setGridSize(QSize(cw, ch))

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.empty_text and self.model() and self.model().rowCount() == 0:
            p = QPainter(self.viewport())
            p.setPen(QColor(C_MUTED))
            p.setFont(px_font(15))
            p.drawText(self.viewport().rect().adjusted(20, 20, -20, -20),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.empty_text)


# ---------------------------------------------------------------- dialogs

class Hero(QWidget):
    def __init__(self):
        super().__init__()
        self.pm = None
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_pixmap(self, pm):
        self.pm = pm
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = self.rect()
        p.fillRect(r, QColor('#000'))
        if self.pm:
            sw, sh = self.pm.width(), self.pm.height()
            scale = max(r.width() / sw, r.height() / sh)
            cw, ch = r.width() / scale, r.height() / scale
            p.drawPixmap(QRectF(r), self.pm, QRectF((sw - cw) / 2, (sh - ch) / 3, cw, ch))
        g = QLinearGradient(0, r.height(), 0, 0)
        g.setColorAt(0, QColor(C_PANEL))
        g.setColorAt(0.65, QColor(19, 19, 26, 0))
        p.fillRect(r, g)


def score_box(label, value, extra=''):
    box = QFrame()
    box.setObjectName('score')
    lay = QVBoxLayout(box)
    lay.setContentsMargins(14, 8, 14, 8)
    lay.setSpacing(0)
    a = QLabel(label.upper())
    a.setObjectName('muted')
    a.setStyleSheet('font-size:10px;font-weight:700')
    b = QLabel(value)
    b.setStyleSheet('font-size:20px;font-weight:700')
    lay.addWidget(a)
    lay.addWidget(b)
    if extra:
        c = QLabel(extra)
        c.setObjectName('muted')
        c.setStyleSheet('font-size:11px')
        lay.addWidget(c)
    return box


class DetailDialog(QDialog):
    def __init__(self, win, it):
        super().__init__(win)
        self.win, self.it = win, it
        self.setWindowTitle(it['n'])
        self.resize(860, 680)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        page = QWidget()
        page.setObjectName('detailPage')
        scroll.setWidget(page)
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 24)
        self.hero = Hero()
        v.addWidget(self.hero)

        body = QHBoxLayout()
        body.setContentsMargins(28, 0, 28, 0)
        body.setSpacing(24)
        v.addLayout(body)
        self.poster = QLabel()
        self.poster.setFixedSize(150, 225)
        self.poster.setStyleSheet(f'background:{C_PANEL2};border-radius:10px')
        self.poster.setScaledContents(True)
        body.addWidget(self.poster, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(8)
        body.addLayout(col, 1)
        title = QLabel(it['n'])
        title.setWordWrap(True)
        title.setStyleSheet('font-size:26px;font-weight:800')
        col.addWidget(title)
        self.meta = QLabel(self.meta_text())
        self.meta.setObjectName('muted')
        col.addWidget(self.meta)
        self.scores = QHBoxLayout()
        self.scores.setSpacing(10)
        col.addLayout(self.scores)
        if it['g']:
            genres = QLabel('  ·  '.join(it['g']))
            genres.setObjectName('muted')
            col.addWidget(genres)
        self.overview = QLabel('…')
        self.overview.setWordWrap(True)
        self.overview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.overview.setStyleSheet('color:#cfcfda;font-size:14px;line-height:150%')
        col.addWidget(self.overview)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        q = QUrl.toPercentEncoding(it['n']).data().decode()
        self.watch = {}  # service key -> [button, url]; search until Wikidata gives a direct link
        for key in it.get('sv') or ['netflix']:
            prov = PROV[key]
            b = QPushButton(f"▶  Find on {prov['name']}")
            b.setObjectName('primary')
            b.setStyleSheet(f"background:{prov['color']};border-color:{prov['color']}")
            b.setToolTip(f"Searches {prov['name']} for this title")
            self.watch[key] = [b, prov['search'].format(q)]
            b.clicked.connect(lambda _=False, key=key: QDesktopServices.openUrl(QUrl(self.watch[key][1])))
            btns.addWidget(b)
        self.imdb_btn = QPushButton('IMDb')
        self.imdb_btn.clicked.connect(self.open_imdb)
        btns.addWidget(self.imdb_btn)
        rt = QPushButton('Rotten Tomatoes')
        rt.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl(f'https://www.rottentomatoes.com/search?search={q}')))
        btns.addWidget(rt)
        tm = QPushButton('TMDB')
        tm.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(f"https://www.themoviedb.org/{it['t']}/{it['id']}")))
        btns.addWidget(tm)
        btns.addStretch()
        col.addSpacing(8)
        col.addLayout(btns)
        col.addStretch()

        self.refresh_scores()
        if it['b']:
            win.net.image(IMG + 'w1280' + it['b'], lambda pm: pm and self.hero.set_pixmap(pm))
        if it['p']:
            cached = win.poster(it)
            if cached:
                self.poster.setPixmap(cached)
            else:
                win.net.image(IMG + 'w342' + it['p'], lambda pm: pm and self.poster.setPixmap(pm))
        win.want_rating(it, urgent=True)
        win.service_links(it, self.got_links)
        win.net.tmdb(f"/{it['t']}/{it['id']}", on_ok=self.got_details,
                     on_err=lambda *a: self.overview.setText(''))

    def meta_text(self, extra=()):
        base = ['Series' if self.it['t'] == 'tv' else 'Movie'] + ([str(self.it['y'])] if self.it['y'] else [])
        rated = (self.win.ratings.get(self.it['k']) or {}).get('rated')
        return ' · '.join(base + list(extra) + ([rated] if rated else []))

    def got_links(self, links):
        for key, sid in links.items():
            if sid and key in self.watch:
                prov = PROV[key]
                self.watch[key][1] = prov['url'].format(sid)
                self.watch[key][0].setText(f"▶  Watch on {prov['name']}")
                self.watch[key][0].setToolTip(self.watch[key][1])

    def got_details(self, d):
        self.overview.setText(d.get('overview') or 'No description available.')
        extra = []
        if self.it['t'] == 'movie' and d.get('runtime'):
            extra.append(f"{d['runtime'] // 60}h {d['runtime'] % 60}m")
        if self.it['t'] == 'tv':
            n = d.get('number_of_seasons')
            if n:
                extra.append(f"{n} season{'s' if n > 1 else ''}")
            if d.get('status'):
                extra.append('Ongoing' if d['status'] == 'Returning Series' else d['status'])
        self.meta.setText(self.meta_text(extra))

    def refresh_scores(self):
        while self.scores.count():
            w = self.scores.takeAt(0).widget()
            if w:
                w.deleteLater()
        it, win = self.it, self.win
        r = win.scores(it)
        imdb_dash = '…' if win.imdb_pending(it) else '—'
        rt_dash = '…' if win.rt_pending(it) else '—'
        self.scores.addWidget(score_box('IMDb', f"{r['imdb']:.1f}/10" if r.get('imdb') is not None else imdb_dash,
                                        f"{r['iv']:,} votes" if r.get('iv') else ''))
        if win.omdb_on() or r.get('rt') is not None:
            self.scores.addWidget(score_box('Rotten Tomatoes', f"{r['rt']}%" if r.get('rt') is not None else rt_dash))
        if r.get('mc'):
            self.scores.addWidget(score_box('Metacritic', str(r['mc'])))
        self.scores.addWidget(score_box('TMDB', f"{it['v']:.1f}" if it['vc'] else '—',
                                        f"{it['vc']:,} votes" if it['vc'] else ''))
        self.scores.addStretch()
        self.imdb_btn.setVisible(bool(r.get('id')))

    def open_imdb(self):
        imdb = self.win.imdb_id(self.it)
        if imdb:
            QDesktopServices.openUrl(QUrl(f'https://www.imdb.com/title/{imdb}/'))


class SettingsDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle('Settings')
        self.setMinimumWidth(500)
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 22)
        v.setSpacing(6)
        h = QLabel('Settings')
        h.setStyleSheet('font-size:20px;font-weight:800')
        v.addWidget(h)
        v.addSpacing(10)

        def field(label, value, hint):
            v.addWidget(QLabel(label))
            e = QLineEdit(value)
            e.setStyleSheet('font-family:Consolas,monospace')
            v.addWidget(e)
            lh = QLabel(hint)
            lh.setObjectName('muted')
            lh.setOpenExternalLinks(True)
            lh.setWordWrap(True)
            v.addWidget(lh)
            v.addSpacing(10)
            return e

        s = win.settings
        self.tmdb = field('TMDB API key or read access token', s['tmdb'],
                          'Free at <a href="https://www.themoviedb.org/settings/api">themoviedb.org/settings/api</a>')
        self.omdb = None if not win.omdb_on() else field('OMDb API key (for Rotten Tomatoes & Metacritic)', s['omdb'],
                          'Free at <a href="https://www.omdbapi.com/apikey.aspx">omdbapi.com/apikey.aspx</a>. '
                          'Optional: IMDb scores are free without it. Used for movies only.')
        v.addWidget(QLabel('Country'))
        self.region = QComboBox()
        v.addWidget(self.region)
        self.fill_regions(win.regions or FALLBACK_REGIONS)
        if not win.regions and s['tmdb']:
            win.net.tmdb('/watch/providers/regions', on_ok=self.got_regions)
        text = f'{len(win.imdb_ids):,} titles matched to IMDb'
        if win.omdb_on():
            rt = sum(1 for r in win.ratings.values() if r.get('rt') is not None)
            text += (f' · {rt:,} Rotten Tomatoes scores saved · '
                     f'{win.lookups_today():,}/{OMDB_DAILY:,} OMDb lookups used today')
        info = QLabel(text)
        info.setWordWrap(True)
        info.setObjectName('muted')
        v.addSpacing(10)
        v.addWidget(info)
        self.err = QLabel('')
        self.err.setStyleSheet('color:#ff3d47')
        self.err.setWordWrap(True)
        v.addWidget(self.err)
        v.addSpacing(6)
        row = QHBoxLayout()
        refresh = QPushButton('Refresh catalog')
        refresh.setToolTip('Re-download the Netflix and HBO Max catalogs (scores you already have are kept)')
        refresh.clicked.connect(self.refresh_catalog)
        row.addWidget(refresh)
        row.addStretch()
        cancel = QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self.save = QPushButton('Save')
        self.save.setObjectName('primary')
        self.save.setDefault(True)
        self.save.clicked.connect(self.on_save)
        row.addWidget(self.save)
        v.addLayout(row)

    def fill_regions(self, regions):
        cur = self.region.currentData() or self.win.settings['region']
        self.region.clear()
        if cur not in regions:
            self.region.addItem(cur, cur)
        for code, name in sorted(regions.items(), key=lambda x: x[1]):
            self.region.addItem(name, code)
        self.region.setCurrentIndex(max(0, self.region.findData(cur)))

    def got_regions(self, d):
        self.win.regions = {r['iso_3166_1']: r['english_name'] for r in d.get('results', [])}
        if self.win.regions:
            self.fill_regions(self.win.regions)

    def refresh_catalog(self):
        self.accept()
        self.win.load_catalog(force=True)

    def on_save(self):
        tmdb = self.tmdb.text().strip()
        omdb = self.omdb.text().strip() if self.omdb else self.win.settings.get('omdb', '')
        region = self.region.currentData() or self.win.settings['region']
        if not tmdb:
            self.err.setText('A TMDB key is required.')
            return
        if tmdb != self.win.settings['tmdb']:
            self.save.setEnabled(False)
            self.save.setText('Checking…')

            def bad(status, msg, body):
                self.save.setEnabled(True)
                self.save.setText('Save')
                self.err.setText(f'TMDB rejected that key: {msg}')

            self.win.net.tmdb('/configuration', on_ok=lambda d: self.commit(tmdb, omdb, region), on_err=bad, key=tmdb)
        else:
            self.commit(tmdb, omdb, region)

    def commit(self, tmdb, omdb, region):
        self.accept()
        self.win.apply_settings(tmdb, omdb, region)


# ---------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.setWindowTitle('Flixdex')
        self.setWindowIcon(app_icon())
        self.resize(1360, 860)
        self.setMinimumSize(420, 500)

        saved = read_json(APP_DIR / 'settings.json', {})
        loc = (QLocale.system().name().split('_') + ['US'])[1] or 'US'
        self.settings = {'tmdb': '', 'omdb': '', 'region': loc.upper(), **saved}
        self.f = {**DEFAULT_F, **self.settings.get('filters', {}), 'q': ''}
        if not self.omdb_on():  # Rotten Tomatoes needs OMDb (rate-limited), which is off unless a key is configured
            self.f['minRt'] = 0
            if self.f['sort'] == 'rt':
                self.f['sort'] = 'imdb'
        store = read_json(APP_DIR / 'ratings.json', {})
        self.ratings = store.get('ratings', {})
        self.usage = store.get('usage', {})
        self.netflix_ids = read_json(APP_DIR / 'netflix.json', {})  # older Netflix-only link cache
        self.links = read_json(APP_DIR / 'links.json', {})  # k -> {service key: id or '', 'ts': float}
        ids = read_json(APP_DIR / 'imdb_ids.json', {})
        self.imdb_ids = ids.get('ids', {})    # k -> 'tt…'
        self.imdb_miss = ids.get('miss', {})  # k -> time TMDB had no IMDb id
        self.extra_genres = read_json(APP_DIR / 'genres.json', {})  # series k -> genres from TMDB keywords
        self.imdb_table = None                # (ids, ratings×10, votes) arrays from IMDb's ratings file
        self.imdb_downloading = self.imdb_parsing = self.imdb_failed = False
        self.dl_note = ''
        self.id_job = None
        self.bridge = Bridge()
        self.bridge.imdb_ready.connect(self.imdb_loaded)
        self.net = Net(self)
        self.net.tmdb_key = self.settings['tmdb']
        self.regions = None
        self.items, self.by_k = [], {}
        self.genre_names = {'movie': {}, 'tv': {}}
        self.loading, self.load_token = False, 0
        self.new_scores = 0
        self.detail = None
        # OMDb queue
        self.rq_queue, self.rq_pending, self.rq_failed = deque(), set(), set()
        self.rq_active, self.rq_stopped = 0, ''
        # posters (LRU of decoded pixmaps; the network layer keeps a disk cache)
        self.posters, self.poster_pending = OrderedDict(), set()

        self.save_timer = self.timer(1500, self.save_ratings)
        self.filter_timer = self.timer(0, self.filters_changed)
        self.notice_timer = self.timer(250, self.update_notice)
        self.resort_timer = self.timer(1200, self.auto_resort)
        self.repaint_timer = self.timer(60, lambda: self.view.viewport().update())
        self.ids_save_timer = self.timer(2000, self.save_ids)

        self.build_ui()
        self.sync_filter_ui()
        QShortcut(QKeySequence('Ctrl+F'), self, self.focus_search)
        QShortcut(QKeySequence('/'), self, self.focus_search)
        self.start(False)

    def timer(self, ms, fn):
        t = QTimer(self)
        t.setSingleShot(True)
        t.setInterval(ms)
        t.timeout.connect(fn)
        return t

    def omdb_on(self):
        """OMDb (Rotten Tomatoes, 1,000 lookups/day) is only used when a key is already in settings.json.
        The app never asks for one, so a fresh install only uses sources without a daily quota."""
        return bool(self.settings.get('omdb'))

    @staticmethod
    def kick(t):
        """Start a throttle timer without pushing it back, so a steady stream of events still fires it."""
        if not t.isActive():
            t.start()

    # ---- UI
    def build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        top = QFrame()
        top.setObjectName('top')
        th = QHBoxLayout(top)
        th.setContentsMargins(16, 10, 16, 10)
        th.setSpacing(10)
        logo = self.logo = QLabel(f'<span style="color:{C_TEXT}">Flix</span><span style="color:{C_ACCENT}">dex</span>')
        logo.setStyleSheet('font-size:20px;font-weight:800')
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(26, 26))
        th.addWidget(icon)
        th.addWidget(logo)
        th.addSpacing(12)
        self.search = QLineEdit()
        self.search.setObjectName('search')
        self.search.setPlaceholderText('Search movies & series…   (Ctrl+F)')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda t: self.set_filter(150, q=t))
        th.addWidget(self.search, 1)
        self.filters_btn = QPushButton('☰  Filters')
        self.filters_btn.setCheckable(True)
        self.filters_btn.setChecked(True)
        self.filters_btn.toggled.connect(lambda on: self.panel.setVisible(on))
        th.addWidget(self.filters_btn)
        self.settings_btn = QPushButton('⚙  Settings')
        self.settings_btn.clicked.connect(self.open_settings)
        th.addWidget(self.settings_btn)
        v.addWidget(top)

        body = QHBoxLayout()
        body.setContentsMargins(16, 14, 8, 0)
        body.setSpacing(16)
        v.addLayout(body, 1)

        self.panel = self.build_filters()
        body.addWidget(self.panel)

        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        self.stack.addWidget(self.build_welcome())

        browse = QWidget()
        bv = QVBoxLayout(browse)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(10)
        svc_seg = QFrame()
        svc_seg.setObjectName('segbox')
        vh = QHBoxLayout(svc_seg)
        vh.setContentsMargins(3, 3, 3, 3)
        vh.setSpacing(2)
        self.svc_group = QButtonGroup(self)
        for key, label in [(p['key'], p['name']) for p in PROVIDERS] + [('all', 'Both')]:
            b = QPushButton(label)
            b.setObjectName('seg')
            b.setCheckable(True)
            b.setProperty('svc', key)
            self.svc_group.addButton(b)
            vh.addWidget(b)
        self.svc_group.buttonClicked.connect(lambda b: self.set_filter(0, svc=b.property('svc')))
        bar.addWidget(svc_seg)
        seg = QFrame()
        seg.setObjectName('segbox')
        sh = QHBoxLayout(seg)
        sh.setContentsMargins(3, 3, 3, 3)
        sh.setSpacing(2)
        self.type_group = QButtonGroup(self)
        for key, label in [('all', 'All'), ('movie', 'Movies'), ('tv', 'Series')]:
            b = QPushButton(label)
            b.setObjectName('seg')
            b.setCheckable(True)
            b.setProperty('t', key)
            self.type_group.addButton(b)
            sh.addWidget(b)
        self.type_group.buttonClicked.connect(lambda b: self.set_filter(0, type=b.property('t')))
        bar.addWidget(seg)
        bar.addStretch()
        self.count = QLabel('')
        self.count.setObjectName('muted')
        bar.addWidget(self.count)
        bar.addSpacing(8)
        bv.addLayout(bar)

        self.notice = QFrame()
        self.notice.setObjectName('notice')
        nh = QHBoxLayout(self.notice)
        nh.setContentsMargins(14, 8, 8, 8)
        self.notice_lbl = QLabel()
        self.notice_lbl.setWordWrap(True)
        nh.addWidget(self.notice_lbl, 1)
        self.notice_btns = [QPushButton(), QPushButton()]
        self.notice_actions = [None, None]
        for i, b in enumerate(self.notice_btns):
            b.clicked.connect(lambda _=False, i=i: self.notice_actions[i] and self.notice_actions[i]())
            nh.addWidget(b)
        self.notice.hide()
        bv.addWidget(self.notice)

        self.model = TitleModel()
        self.view = CardView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(CardDelegate(self))
        self.view.clicked.connect(lambda idx: self.open_detail(idx.data(Qt.ItemDataRole.UserRole)))
        self.view.activated.connect(lambda idx: self.open_detail(idx.data(Qt.ItemDataRole.UserRole)))
        bv.addWidget(self.view, 1)
        self.stack.addWidget(browse)

        sb = self.statusBar()
        self.status_lbl = QLabel('')
        sb.addWidget(self.status_lbl)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(180)
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1000)
        self.progress.hide()
        sb.addPermanentWidget(self.progress)
        credit = self.credit = QLabel(f"Catalog: TMDB / JustWatch · Ratings: IMDb{', OMDb' if self.omdb_on() else ''}"
                                      " · Not affiliated with Netflix or HBO")
        credit.setObjectName('muted')
        sb.addPermanentWidget(credit)

    def build_filters(self):
        panel = QFrame()
        panel.setObjectName('panel')
        panel.setFixedWidth(260)
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(6)

        def head(text):
            row = QHBoxLayout()
            lbl = QLabel(text.upper())
            lbl.setObjectName('head')
            row.addWidget(lbl)
            row.addStretch()
            val = QLabel('')
            val.setStyleSheet('font-weight:600')
            row.addWidget(val)
            v.addLayout(row)
            return val

        head('Sort by')
        self.sort = QComboBox()
        for key, label in SORT_OPTIONS:
            if key != 'rt' or self.omdb_on():
                self.sort.addItem(label, key)
        self.sort.currentIndexChanged.connect(lambda: self.set_filter(0, sort=self.sort.currentData()))
        v.addWidget(self.sort)
        v.addSpacing(12)

        head('Genre')
        self.genre = QComboBox()
        self.genre.addItem('All genres', '')
        self.genre.currentIndexChanged.connect(lambda: self.set_filter(0, genre=self.genre.currentData() or ''))
        v.addWidget(self.genre)
        v.addSpacing(12)

        head('Release year')
        yr = QHBoxLayout()
        self.y_min, self.y_max = QSpinBox(), QSpinBox()
        for sp, key, any_text in [(self.y_min, 'yMin', 'From: any'), (self.y_max, 'yMax', 'To: any')]:
            sp.setRange(1899, 2100)
            sp.setSpecialValueText(any_text)
            sp.setValue(1899)
            sp.valueChanged.connect(lambda val, key=key: self.set_filter(400, **{key: 0 if val == 1899 else val}))
            yr.addWidget(sp)
        v.addLayout(yr)
        v.addSpacing(12)

        def slider(title, key, maximum, scale, fmt):
            val = head(title)
            s = QSlider(Qt.Orientation.Horizontal)
            s.setRange(0, maximum)
            s.valueChanged.connect(lambda x: self.set_filter(200, **{key: x * scale}))
            v.addWidget(s)
            v.addSpacing(12)
            return s, val, scale, fmt

        self.sliders = {'minImdb': slider('Min IMDb', 'minImdb', 18, 0.5, lambda x: f'{x:.1f}+')}
        if self.omdb_on():
            self.sliders['minRt'] = slider('Min Rotten Tomatoes', 'minRt', 20, 5, lambda x: f'{x}%+')
        self.sliders['minTmdb'] = slider('Min TMDB', 'minTmdb', 18, 0.5, lambda x: f'{x:.1f}+')
        reset = QPushButton('Reset filters')
        reset.clicked.connect(self.reset_filters)
        v.addWidget(reset)
        hint = QLabel('IMDb scores cover every title, free. Rotten Tomatoes scores fill in for movies over time, '
                      'and each is only looked up once, ever.' if self.omdb_on() else
                      "IMDb scores come from IMDb's free daily ratings file and cover every title.")
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        hint.setStyleSheet('font-size:11px;margin-top:6px')
        v.addWidget(hint)
        v.addStretch()
        return panel

    def build_welcome(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.addStretch()
        card = QFrame()
        card.setObjectName('panel')
        card.setMaximumWidth(620)
        cv = QVBoxLayout(card)
        cv.setContentsMargins(32, 28, 32, 28)
        cv.setSpacing(12)
        h = QLabel('Every Netflix & HBO Max title, rated.')
        h.setStyleSheet('font-size:28px;font-weight:800')
        cv.addWidget(h)
        t = QLabel(
            f'<p style="color:{C_MUTED}">Browse the current Netflix and HBO Max catalogs in your country with IMDb '
            f'ratings. You need one free API key:</p>'
            f'<p><b>TMDB</b> <span style="color:{C_MUTED}">for the catalog and posters. Free, with no daily limit:</span> '
            f'<a style="color:#ff3d47" href="https://www.themoviedb.org/settings/api">themoviedb.org/settings/api</a></p>'
            f'<p style="color:{C_MUTED}">IMDb ratings come from IMDb\'s free daily ratings file, so no other key '
            f'is needed.</p>')
        t.setWordWrap(True)
        t.setOpenExternalLinks(True)
        cv.addWidget(t)
        b = QPushButton('Add TMDB key')
        b.setObjectName('primary')
        b.clicked.connect(self.open_settings)
        cv.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(card)
        row.addStretch()
        outer.addLayout(row)
        outer.addStretch()
        return w

    def resizeEvent(self, e):
        super().resizeEvent(e)
        narrow = self.width() < 900
        if narrow != getattr(self, '_narrow', None):
            self._narrow = narrow
            if self.settings['tmdb']:
                self.filters_btn.setChecked(not narrow)
            self.update_top_labels()
        self.logo.setVisible(self.width() >= 560)
        self.credit.setVisible(self.width() >= 1000)

    def update_top_labels(self):
        """Icon-only header buttons on narrow windows so the search box keeps its room."""
        narrow = getattr(self, '_narrow', False)
        has_key = bool(self.settings['tmdb'])
        self.filters_btn.setText('☰' if narrow else '☰  Filters')
        label = f"Region {self.settings['region']}" if has_key else 'Settings'
        self.settings_btn.setText(f"⚙  {self.settings['region']}" if narrow and has_key else
                                  '⚙' if narrow else f'⚙  {label}')
        self.settings_btn.setToolTip('Settings')
        self.filters_btn.setToolTip('Show or hide filters')

    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    # ---- settings / boot
    def save_settings(self):
        write_json(APP_DIR / 'settings.json', {**self.settings, 'filters': {k: v for k, v in self.f.items() if k != 'q'}})

    def start(self, force):
        has_key = bool(self.settings['tmdb'])
        self.stack.setCurrentIndex(1 if has_key else 0)
        self.panel.setVisible(has_key and self.filters_btn.isChecked())
        self.filters_btn.setVisible(has_key)
        self.update_top_labels()
        self.update_status()
        if has_key:
            self.load_imdb_dataset()
            self.load_catalog(force)

    def open_settings(self):
        SettingsDialog(self).exec()

    def apply_settings(self, tmdb, omdb, region):
        catalog_changed = tmdb != self.settings['tmdb'] or region != self.settings['region']
        if omdb != self.settings['omdb']:
            self.rq_stopped = ''
            self.rq_failed.clear()
        self.settings.update(tmdb=tmdb, omdb=omdb, region=region)
        self.net.tmdb_key = tmdb
        self.save_settings()
        if catalog_changed or not self.items:
            self.start(False)
        else:
            self.view.viewport().update()
            self.prime_scores()
            self.update_notice()
            self.update_status()

    # ---- catalog
    def discover(self, prov, t, page, on_ok, on_err):
        params = {'with_watch_providers': prov['id'], 'watch_region': self.settings['region'],
                  'with_watch_monetization_types': 'flatrate', 'sort_by': 'popularity.desc',
                  'include_adult': 'false', 'page': page}
        if t == 'tv':
            params['include_null_first_air_dates'] = 'false'
        self.net.tmdb('/discover/' + t, params, on_ok, on_err)

    def to_item(self, t, r):
        date = r.get('release_date') if t == 'movie' else r.get('first_air_date')
        genres = []
        for gid in r.get('genre_ids') or []:
            name = self.genre_names[t].get(gid)
            for g in GENRE_SPLIT.get(name, [name] if name else []):
                if g not in genres:
                    genres.append(g)
        try:
            year = int((date or '')[:4])
        except ValueError:
            year = 0
        return {'k': ('m' if t == 'movie' else 't') + str(r['id']), 'id': r['id'], 't': t,
                'n': r.get('title') if t == 'movie' else r.get('name') or '', 'y': year,
                'p': r.get('poster_path') or '', 'b': r.get('backdrop_path') or '', 'g': genres,
                'pop': r.get('popularity') or 0, 'v': r.get('vote_average') or 0, 'vc': r.get('vote_count') or 0}

    def add_items(self, items, svc):
        for it in items:
            have = self.by_k.get(it['k'])
            if have:  # same title on several services
                if svc not in have['sv']:
                    have['sv'].append(svc)
                continue
            it['n'] = it['n'] or '?'
            it['s'] = norm(it['n'])
            it['sv'] = [svc]
            # Reuse a saved IMDb id so we never need the TMDB lookup again.
            saved = self.imdb_ids.get(it['k']) or (self.ratings.get(it['k']) or {}).get('id')
            if saved:
                it['im'] = saved
            self.add_extra_genres(it)
            self.by_k[it['k']] = it
            self.items.append(it)

    def catalog_path(self, prov):
        return APP_DIR / f"catalog_{self.settings['region']}_{prov['key']}.json"

    def read_catalog_cache(self, prov):
        cached = read_json(self.catalog_path(prov), None)
        if cached is None and prov['key'] == 'netflix':  # catalogs saved before HBO Max support
            cached = read_json(APP_DIR / f"catalog_{self.settings['region']}.json", None)
        if cached and time.time() - cached.get('ts', 0) < CATALOG_TTL:
            return cached['items']
        return None

    def load_catalog(self, force=False):
        """Load every service's catalog (from the daily cache when possible) and merge them by title."""
        self.load_token += 1
        token = self.load_token
        self.items, self.by_k = [], {}
        self.id_job = None
        todo = deque()
        for prov in PROVIDERS:
            cached = None if force else self.read_catalog_cache(prov)
            if cached is not None:
                self.add_items(cached, prov['key'])
            else:
                todo.append(prov)
        if not todo:
            return self.catalog_ready()

        self.loading = True
        if self.items:
            self.apply_filters()
        else:
            self.model.set_rows([])
        self.progress.setValue(10)
        self.progress.show()

        def fail(status, msg, body):
            if token != self.load_token:
                return
            self.load_token += 1  # cancel the rest
            self.loading = False
            self.progress.hide()
            self.view.empty_text = ''
            hint = ' Check your TMDB key in Settings.' if status == 401 else ''
            self.show_notice(f"Couldn't load the catalog: {msg}.{hint}", [('Open settings', self.open_settings),
                                                                         ('Retry', lambda: self.load_catalog(True))])

        def next_provider():
            if token != self.load_token:
                return
            if todo:
                self.fetch_provider(todo.popleft(), token, fail, next_provider)
            else:
                self.catalog_ready()

        if all(self.genre_names.values()):
            return next_provider()
        state = {'genres': 0}

        def got_genres(t, d):
            if token != self.load_token:
                return
            self.genre_names[t] = {g['id']: g['name'] for g in d.get('genres', [])}
            state['genres'] += 1
            if state['genres'] == 2:
                next_provider()

        for t in ('movie', 'tv'):
            self.net.tmdb(f'/genre/{t}/list', on_ok=lambda d, t=t: got_genres(t, d), on_err=fail)

    def fetch_provider(self, prov, token, fail, then):
        """Page through one service's catalog on TMDB (6 requests at a time), then save it for a day."""
        svc = prov['key']
        state = {'firsts': 0, 'done': 0, 'total': 1, 'active': 0, 'last': 0}
        jobs = deque()
        self.view.empty_text = f"Loading the {prov['name']} catalog…"
        self.progress.setValue(10)

        def got_first(t, d):
            if token != self.load_token:
                return
            self.add_items([self.to_item(t, r) for r in d.get('results', [])], svc)
            jobs.extend((t, p) for p in range(2, min(d.get('total_pages', 1), 500) + 1))
            state['firsts'] += 1
            if state['firsts'] == 2:
                state['total'] = len(jobs) + 2
                state['done'] = 2
                self.build_genre_options()
                self.apply_filters(keep=True)
                pump()

        def pump():
            if token != self.load_token:
                return
            while state['active'] < 6 and jobs:
                t, p = jobs.popleft()
                state['active'] += 1
                self.discover(prov, t, p, lambda d, t=t: page_done(t, d), lambda *a: page_done(None, None))
            if not jobs and state['active'] == 0:
                write_json(self.catalog_path(prov), {
                    'ts': time.time(),
                    'items': [{k: v for k, v in it.items() if k not in ('s', 'im', 'sv')}
                              for it in self.items if svc in it['sv']]})
                then()

        def page_done(t, d):
            if token != self.load_token:
                return
            state['active'] -= 1
            state['done'] += 1
            if d:
                self.add_items([self.to_item(t, r) for r in d.get('results', [])], svc)
            self.progress.setValue(int(1000 * state['done'] / state['total']))
            if time.time() - state['last'] > 1.5:
                state['last'] = time.time()
                self.apply_filters(keep=True)
            pump()

        for t in ('movie', 'tv'):
            self.discover(prov, t, 1, lambda d, t=t: got_first(t, d), fail)

    def catalog_ready(self):
        self.loading = False
        self.progress.hide()
        self.view.empty_text = ''
        self.build_genre_options()
        self.apply_filters(keep=True)
        self.prime_scores()
        self.resolve_imdb_ids()

    # ---- posters
    def poster(self, it):
        path = it['p']
        if not path:
            return None
        pm = self.posters.get(path)
        if pm is not None:
            self.posters.move_to_end(path)
            return pm
        if path not in self.poster_pending:
            self.poster_pending.add(path)
            self.net.image(IMG + 'w342' + path, lambda pm, path=path: self.got_poster(path, pm))
        return None

    def got_poster(self, path, pm):
        self.poster_pending.discard(path)
        if pm is None:
            return
        self.posters[path] = pm
        while len(self.posters) > 600:
            self.posters.popitem(last=False)
        self.kick(self.repaint_timer)

    # ---- IMDb ratings (IMDb's free daily file + IMDb ids from TMDB)
    def imdb_id(self, it):
        return it.get('im') or self.imdb_ids.get(it['k']) or (self.ratings.get(it['k']) or {}).get('id') or None

    def imdb_lookup(self, tt):
        if not self.imdb_table or not tt or not tt.startswith('tt'):
            return None
        try:
            n = int(tt[2:])
        except ValueError:
            return None
        ids, rat, votes = self.imdb_table
        i = bisect_left(ids, n)
        if i < len(ids) and ids[i] == n:
            return rat[i] / 10, votes[i]
        return None

    def scores(self, it):
        """Everything known about a title's ratings: IMDb from the daily file, RT/Metacritic from OMDb."""
        out = dict(self.ratings.get(it['k']) or {})
        tt = self.imdb_id(it)
        out['id'] = tt
        hit = self.imdb_lookup(tt)
        if hit:
            out['imdb'], out['iv'] = hit
        return out

    def imdb_pending(self, it):
        if self.imdb_table is None and (self.imdb_downloading or self.imdb_parsing):
            return True
        return bool(self.id_job) and not self.imdb_id(it)

    def load_imdb_dataset(self, force=False):
        if self.imdb_downloading:
            return
        path = APP_DIR / 'title.ratings.tsv.gz'
        if path.exists() and self.imdb_table is None and not self.imdb_parsing:
            self.parse_imdb(path)  # use yesterday's file right away while a fresh one downloads
        if path.exists() and time.time() - path.stat().st_mtime < IMDB_TTL and not force:
            return
        self.imdb_downloading = True
        self.kick(self.notice_timer)

        def progress(got, total):
            if total > 0:
                self.dl_note = f'Downloading IMDb ratings… {got * 100 // total}%'
                self.kick(self.notice_timer)

        def done(data):
            self.imdb_downloading = False
            self.dl_note = ''
            if not data:
                self.imdb_failed = self.imdb_table is None
                self.kick(self.notice_timer)
                return
            part = path.with_suffix('.part')
            try:
                part.write_bytes(data)
                part.replace(path)
            except OSError as e:
                print('could not save IMDb ratings', e)
                return
            self.parse_imdb(path)

        self.net.download(IMDB_URL, done, progress)

    def parse_imdb(self, path):
        self.imdb_parsing = True

        def work():
            try:
                self.bridge.imdb_ready.emit(parse_imdb_ratings(path))
            except Exception as e:  # corrupt or partial file
                self.bridge.imdb_ready.emit(e)

        threading.Thread(target=work, daemon=True).start()

    def imdb_loaded(self, result):
        self.imdb_parsing = False
        if isinstance(result, Exception):
            print('IMDb ratings file unreadable:', result)
            self.imdb_failed = self.imdb_table is None
            if not self.imdb_downloading:
                (APP_DIR / 'title.ratings.tsv.gz').unlink(missing_ok=True)
        else:
            self.imdb_table, self.imdb_failed = result, False
        self.view.viewport().update()
        if self.detail and self.detail.isVisible():
            self.detail.refresh_scores()
        if self.imdb_mode():
            self.apply_filters(keep=True)
        self.kick(self.notice_timer)

    def add_extra_genres(self, it):
        for g in self.extra_genres.get(it['k']) or []:
            if g not in it['g']:
                it['g'].append(g)

    def needs_lookup(self, it, now):
        need_id = not self.imdb_id(it) and now - self.imdb_miss.get(it['k'], 0) > ID_MISS_RETRY
        return need_id or (it['t'] == 'tv' and it['k'] not in self.extra_genres)

    def resolve_imdb_ids(self):
        """Find every title's IMDb id, plus series' keyword genres (TMDB has no daily limit), most popular first."""
        now = time.time()
        todo = deque(sorted((it for it in self.items if self.needs_lookup(it, now)), key=lambda i: -i['pop']))
        if not todo:
            self.id_job = None
            return
        job = self.id_job = {'todo': todo, 'active': 0, 'done': 0, 'total': len(todo)}

        def pump():
            if self.id_job is not job:
                return
            while job['active'] < 8 and job['todo']:
                it = job['todo'].popleft()
                if not self.needs_lookup(it, time.time()):
                    job['done'] += 1
                    continue
                job['active'] += 1
                ok, err = (lambda d, it=it: done(it, d)), (lambda *a, it=it: done(it, None))
                if it['t'] == 'tv':  # one request gets both the IMDb id and the keywords
                    self.net.tmdb(f"/tv/{it['id']}", {'append_to_response': 'external_ids,keywords'}, ok, err)
                else:
                    self.net.tmdb(f"/movie/{it['id']}/external_ids", on_ok=ok, on_err=err)
            if not job['todo'] and job['active'] == 0:
                self.id_job = None
                self.save_ids()
                self.build_genre_options()
                if self.imdb_mode() or self.f['genre']:
                    self.apply_filters(keep=True)
                self.kick(self.notice_timer)

        def done(it, d):
            if self.id_job is not job:
                return
            job['active'] -= 1
            job['done'] += 1
            if d is not None:
                if it['t'] == 'tv':
                    kws = [k.get('name', '').lower() for k in (d.get('keywords') or {}).get('results') or []]
                    extra = sorted({g for word, g in KEYWORD_GENRES.items() if any(word in k for k in kws)})
                    self.extra_genres[it['k']] = extra
                    self.add_extra_genres(it)
                    d = d.get('external_ids') or {}
                tt = d.get('imdb_id')
                if tt:
                    if self.imdb_ids.get(it['k']) != tt:
                        self.imdb_ids[it['k']] = it['im'] = tt
                        if self.imdb_lookup(tt):
                            self.score_arrived('imdb')
                elif not self.imdb_id(it):
                    self.imdb_miss[it['k']] = time.time()
                self.kick(self.ids_save_timer)
                self.kick(self.repaint_timer)
            self.kick(self.notice_timer)
            pump()

        pump()

    def save_ids(self):
        write_json(APP_DIR / 'imdb_ids.json', {'ids': self.imdb_ids, 'miss': self.imdb_miss})
        write_json(APP_DIR / 'genres.json', self.extra_genres)

    # ---- Rotten Tomatoes + Metacritic (OMDb, movies only)
    def rt_pending(self, it):
        k = it['k']
        if k in self.rq_pending:
            return True
        return (it['t'] == 'movie' and bool(self.settings['omdb']) and not self.rq_stopped
                and k not in self.rq_failed and not self.rating_known(k))

    def rating_known(self, k):
        r = self.ratings.get(k)
        if not r:
            return False
        # A miss (OMDb had nothing) gets one re-check after MISS_RETRY; real scores are kept forever.
        if r.get('imdb') is None and r.get('rt') is None and time.time() - r.get('ts', 0) > MISS_RETRY:
            return False
        return True

    def rating_pending(self, it):
        return self.imdb_pending(it) or self.rt_pending(it)

    def lookups_today(self):
        return self.usage.get(today(), 0)

    def want_rating(self, it, urgent=False):
        k = it['k']
        # Series are skipped: OMDb returned an RT score for 2 of 576 series, and IMDb comes from the free file.
        if (it['t'] != 'movie' or not self.settings['omdb'] or self.rq_stopped or k in self.rq_pending
                or k in self.rq_failed or self.rating_known(k)):
            return
        self.rq_pending.add(k)
        (self.rq_queue.appendleft if urgent else self.rq_queue.append)(it)
        QTimer.singleShot(0, self.pump_ratings)

    def pump_ratings(self):
        while self.rq_active < 4 and self.rq_queue and not self.rq_stopped:
            it = self.rq_queue.popleft()
            self.rq_active += 1
            self.fetch_rating(it)

    def fetch_rating(self, it):
        k = it['k']

        def finish(saved):
            self.rq_active -= 1
            self.rq_pending.discard(k)
            if not saved:
                self.rq_failed.add(k)  # don't hammer a failing title this session
            self.model.refresh(k)
            if self.detail and self.detail.isVisible() and self.detail.it['k'] == k:
                self.detail.refresh_scores()
            if saved:
                self.kick(self.save_timer)
                self.score_arrived('rt')
            self.kick(self.notice_timer)
            self.pump_ratings()

        def store(entry):
            self.ratings[k] = {**entry, 'ts': time.time()}
            finish(True)

        def got_ids(d):
            imdb = d.get('imdb_id')
            if not imdb:
                return store({})  # no IMDb page → nothing OMDb could tell us
            self.imdb_ids[k] = it['im'] = imdb
            self.kick(self.ids_save_timer)
            ask_omdb(imdb)

        def ask_omdb(imdb):
            if self.rq_stopped:
                return finish(False)
            url = QUrl('https://www.omdbapi.com/')
            q = QUrlQuery()
            q.addQueryItem('apikey', self.settings['omdb'])
            q.addQueryItem('i', imdb)
            url.setQuery(q)
            self.usage = {today(): self.lookups_today() + 1}
            self.update_status()
            self.net.get(url, lambda d: got_omdb(imdb, d),
                         lambda s, m, b: got_omdb(imdb, b) if isinstance(b, dict) else finish(False))

        def got_omdb(imdb, d):
            if d.get('Response') == 'False':
                err = (d.get('Error') or '').lower()
                if 'limit' in err:
                    self.usage = {today(): max(self.lookups_today(), OMDB_DAILY)}
                    self.stop_ratings('OMDb daily limit reached. Scores resume tomorrow. '
                                      'Everything fetched so far is saved.')
                    return finish(False)
                if 'invalid api key' in err or 'no api key' in err:
                    self.stop_ratings('Your OMDb key was rejected. If it is new, click the activation link '
                                      'OMDb emailed you.')
                    return finish(False)
                return store({'id': imdb})  # OMDb doesn't know it: remember the miss
            rt = next((x for x in d.get('Ratings') or [] if x.get('Source') == 'Rotten Tomatoes'), None)

            def num(s, cast):
                try:
                    return cast(str(s).replace(',', '').rstrip('%'))
                except (TypeError, ValueError):
                    return None

            store({'id': imdb, 'imdb': num(d.get('imdbRating'), float), 'iv': num(d.get('imdbVotes'), int),
                   'rt': num(rt['Value'], int) if rt else None, 'mc': num(d.get('Metascore'), int),
                   'rated': d.get('Rated') if d.get('Rated') not in (None, 'N/A') else ''})

        imdb = self.imdb_id(it)
        if imdb:
            ask_omdb(imdb)
        else:
            self.net.tmdb(f"/{it['t']}/{it['id']}/external_ids", on_ok=got_ids, on_err=lambda *a: finish(False))

    def stop_ratings(self, msg):
        self.rq_stopped = msg
        for it in self.rq_queue:
            self.rq_pending.discard(it['k'])
        self.rq_queue.clear()
        self.view.viewport().update()
        self.kick(self.notice_timer)

    def save_ratings(self):
        write_json(APP_DIR / 'ratings.json', {'ratings': self.ratings, 'usage': self.usage})

    def imdb_mode(self):
        return self.f['sort'] == 'imdb' or self.f['minImdb'] > 0

    def rt_mode(self):
        return self.f['sort'] == 'rt' or self.f['minRt'] > 0

    def score_mode(self):
        return self.imdb_mode() or self.rt_mode()

    def score_arrived(self, kind):
        if not (self.imdb_mode() if kind == 'imdb' else self.rt_mode()):
            return
        self.new_scores += 1
        if self.view.verticalScrollBar().value() < 400:
            self.kick(self.resort_timer)

    def auto_resort(self):
        if self.view.verticalScrollBar().value() < 400:
            self.apply_filters(keep=True)
        else:
            self.update_notice()

    def missing_scores(self):
        """Movies matching every non-score filter with no OMDb result yet, most popular first."""
        f, q = self.f, norm(self.f['q']).strip()
        out = [it for it in self.items if it['t'] == 'movie' and not self.rating_known(it['k'])
               and (f['svc'] == 'all' or f['svc'] in it['sv'])
               and (f['type'] == 'all' or it['t'] == f['type']) and (not q or q in it['s'])
               and (not f['genre'] or f['genre'] in it['g'])
               and (not f['yMin'] or it['y'] >= f['yMin']) and (not f['yMax'] or (it['y'] and it['y'] <= f['yMax']))
               and (not f['minTmdb'] or (it['vc'] >= 10 and it['v'] >= f['minTmdb']))]
        out.sort(key=lambda it: -it['pop'])
        return out

    def prime_scores(self):
        """In RT mode, fetch the most popular unscored movies first so the top of the list means something."""
        if not self.rt_mode() or not self.settings['omdb'] or self.rq_stopped:
            return
        for it in reversed(self.missing_scores()[:150]):
            self.want_rating(it, urgent=True)
        self.kick(self.notice_timer)

    # ---- filtering
    def set_filter(self, delay=0, **patch):
        self.f.update(patch)
        self.sync_filter_labels()
        self.filter_timer.start(delay)

    def filters_changed(self):
        self.save_settings()
        self.apply_filters()
        self.prime_scores()

    def reset_filters(self):
        self.f = {**DEFAULT_F, 'q': self.f['q']}
        self.sync_filter_ui()
        self.filter_timer.start(0)

    def sync_filter_labels(self):
        for key, (s, val, scale, fmt) in self.sliders.items():
            val.setText(fmt(self.f[key]) if self.f[key] else 'Any')

    def sync_filter_ui(self):
        f = self.f
        widgets = [self.search, self.sort, self.genre, self.y_min, self.y_max] + [s[0] for s in self.sliders.values()]
        for w in widgets:
            w.blockSignals(True)
        self.search.setText(f['q'])
        self.sort.setCurrentIndex(max(0, self.sort.findData(f['sort'])))
        idx = self.genre.findData(f['genre'])
        if idx < 0 and f['genre']:
            self.genre.addItem(f['genre'], f['genre'])
            idx = self.genre.count() - 1
        self.genre.setCurrentIndex(max(0, idx))
        self.y_min.setValue(f['yMin'] or 1899)
        self.y_max.setValue(f['yMax'] or 1899)
        for key, (s, val, scale, fmt) in self.sliders.items():
            s.setValue(round(f[key] / scale))
        for w in widgets:
            w.blockSignals(False)
        for b in self.type_group.buttons():
            b.setChecked(b.property('t') == f['type'])
        for b in self.svc_group.buttons():
            b.setChecked(b.property('svc') == f['svc'])
        self.sync_filter_labels()

    def build_genre_options(self):
        counts = {}
        for it in self.items:
            for g in it['g']:
                counts[g] = counts.get(g, 0) + 1
        self.genre.blockSignals(True)
        self.genre.clear()
        self.genre.addItem('All genres', '')
        for g in sorted(counts):
            self.genre.addItem(f'{g}  ({counts[g]:,})', g)
        idx = self.genre.findData(self.f['genre'])
        if idx < 0 and self.f['genre']:
            self.genre.addItem(self.f['genre'], self.f['genre'])
            idx = self.genre.count() - 1
        self.genre.setCurrentIndex(max(0, idx))
        self.genre.blockSignals(False)

    def apply_filters(self, keep=False):
        f, q = self.f, norm(self.f['q']).strip()
        cache = {}

        def S(it):
            r = cache.get(it['k'])
            if r is None:
                r = cache[it['k']] = self.scores(it)
            return r

        def ok(it):
            if f['svc'] != 'all' and f['svc'] not in it['sv']:
                return False
            if f['type'] != 'all' and it['t'] != f['type']:
                return False
            if q and q not in it['s']:
                return False
            if f['genre'] and f['genre'] not in it['g']:
                return False
            if f['yMin'] and (not it['y'] or it['y'] < f['yMin']):
                return False
            if f['yMax'] and (not it['y'] or it['y'] > f['yMax']):
                return False
            if f['minTmdb'] and not (it['vc'] >= 10 and it['v'] >= f['minTmdb']):
                return False
            r = S(it) if f['minImdb'] or f['minRt'] else {}
            if f['minImdb'] and not ((r.get('imdb') or 0) >= f['minImdb']):
                return False
            if f['minRt'] and not ((r.get('rt') if r.get('rt') is not None else -1) >= f['minRt']):
                return False
            return True

        def sc(it, key):
            v = S(it).get(key)
            return -1 if v is None else v

        keys = {
            'pop': lambda it: -it['pop'],
            # titles with only a handful of votes (often a perfect 9.8) go after the well-established ones
            'imdb': lambda it: ((S(it).get('iv') or 0) < MIN_VOTES, -sc(it, 'imdb'), -it['pop']),
            'rt': lambda it: (-sc(it, 'rt'), -sc(it, 'imdb'), -it['pop']),
            'tmdb': lambda it: (-(it['v'] if it['vc'] >= 30 else -1), -it['pop']),
            'new': lambda it: (-it['y'], -it['pop']),
            'old': lambda it: (it['y'] or 9999, -it['pop']),
            'title': lambda it: it['s'],
        }
        rows = sorted(filter(ok, self.items), key=keys.get(f['sort'], keys['pop']))
        sb = self.view.verticalScrollBar()
        pos = sb.value() if keep else 0
        self.model.set_rows(rows)
        QTimer.singleShot(0, lambda: sb.setValue(pos))
        self.new_scores = 0
        if not rows and not self.loading:
            self.view.empty_text = 'No titles match these filters.'
        loading = ' · loading catalog…' if self.loading else ''
        self.count.setText(f'<b style="color:{C_TEXT}">{len(rows):,}</b> of {len(self.items):,} titles{loading}')
        self.update_notice()

    # ---- notice + status
    def show_notice(self, text, actions=()):
        self.notice_lbl.setText(text)
        for i, b in enumerate(self.notice_btns):
            if i < len(actions):
                b.setText(actions[i][0])
                self.notice_actions[i] = actions[i][1]
                b.show()
            else:
                self.notice_actions[i] = None
                b.hide()
        self.notice.show()

    def update_notice(self):
        self.update_status()
        parts, acts = [], []
        if self.imdb_mode():
            if self.imdb_table is None and (self.imdb_downloading or self.imdb_parsing):
                parts.append(self.dl_note or 'Loading IMDb ratings…')
            elif self.imdb_failed:
                parts.append("Couldn't load IMDb's ratings file. Showing saved scores only.")
                acts.append(('Retry', lambda: self.load_imdb_dataset(force=True)))
            if self.id_job:
                parts.append(f"Matching titles to IMDb: {self.id_job['done']:,} of {self.id_job['total']:,}.")
        if self.rt_mode():
            if self.f['type'] == 'tv':
                parts.append('Rotten Tomatoes scores are only available for movies.')
            elif not self.settings['omdb']:
                parts.append('Rotten Tomatoes scores need an OMDb key.')
                acts.append(('Add key', self.open_settings))
            elif self.rq_stopped:
                parts.append(self.rq_stopped)
            else:
                missing = self.missing_scores()
                if missing:
                    queued = f' (fetching, {len(self.rq_queue):,} queued)' if self.rq_queue else ''
                    parts.append(f"{len(missing):,} matching movies don't have an RT score yet{queued}. "
                                 f"They're listed after the scored ones.")
                    if not self.rq_queue:
                        n = min(len(missing), max(0, OMDB_DAILY - self.lookups_today()))
                        if n:
                            acts.append((f'Fetch {n:,} more', lambda: self.fetch_missing(n)))
        if self.new_scores and self.score_mode():
            parts.append(f'{self.new_scores:,} new scores.')
            acts.append(('↻ Update results', lambda: self.apply_filters(keep=True)))
        if parts:
            return self.show_notice(' '.join(parts), acts[-2:])
        self.notice.hide()

    def fetch_missing(self, n):
        for it in self.missing_scores()[:n]:
            self.want_rating(it)
        self.update_notice()

    def update_status(self):
        if not self.settings['tmdb']:
            self.status_lbl.setText('')
            return
        imdb = sum(1 for it in self.items if self.scores(it).get('imdb') is not None)
        rt = sum(1 for it in self.items if (self.ratings.get(it['k']) or {}).get('rt') is not None)
        parts = [f'IMDb: {imdb:,} of {len(self.items):,} titles']
        if self.id_job:
            parts[0] += f" (matching {self.id_job['done']:,}/{self.id_job['total']:,})"
        elif self.imdb_table is None and (self.imdb_downloading or self.imdb_parsing):
            parts[0] += ' (loading…)'
        if self.settings['omdb']:
            limit = '  (daily limit reached)' if 'limit' in self.rq_stopped else ''
            parts.append(f'RT: {rt:,} movies')
            parts.append(f'OMDb lookups today: {self.lookups_today():,}/{OMDB_DAILY:,}{limit}')
        self.status_lbl.setText('  ·  '.join(parts))

    # ---- detail
    def open_detail(self, it):
        if not it:
            return
        if self.detail:
            self.detail.close()
        self.detail = DetailDialog(self, it)
        self.detail.show()

    # ---- direct "Watch on …" links (Wikidata stores each service's title id, keyed by TMDB/IMDb id)
    def service_links(self, it, cb):
        """Calls cb({service key: id or ''}) for the services the title is on; answers are saved for good."""
        k = it['k']
        want = it.get('sv') or ['netflix']
        saved = self.links.get(k)
        if saved is None and k in self.netflix_ids:  # saved before HBO Max support
            old = self.netflix_ids[k]
            saved = {'netflix': old.get('id', ''), 'ts': old.get('ts', 0)}
        if saved and all(key in saved for key in want) and (
                all(saved[key] for key in want) or time.time() - saved.get('ts', 0) < MISS_RETRY):
            return cb(saved)
        tmdb_prop = 'P4947' if it['t'] == 'movie' else 'P4983'
        imdb = self.imdb_id(it)
        where = f'{{ ?i wdt:{tmdb_prop} "{int(it["id"])}" }}'
        if imdb and imdb.startswith('tt') and imdb[2:].isdigit():
            where += f' UNION {{ ?i wdt:P345 "{imdb}" }}'
        optional = ' '.join(f'OPTIONAL {{ ?i wdt:{p["wd"]} ?{p["key"]} }}' for p in PROVIDERS)
        query = f'SELECT {" ".join("?" + p["key"] for p in PROVIDERS)} WHERE {{ {where} {optional} }}'
        url = QUrl('https://query.wikidata.org/sparql')
        q = QUrlQuery()
        q.addQueryItem('query', query)
        q.addQueryItem('format', 'json')
        url.setQuery(q)

        def ok(d):
            found = {p['key']: '' for p in PROVIDERS}
            for row in (d.get('results') or {}).get('bindings') or []:
                for key in found:
                    if not found[key] and key in row:
                        found[key] = row[key]['value']
            self.links[k] = {**found, 'ts': time.time()}
            write_json(APP_DIR / 'links.json', self.links)
            cb(found)

        self.net.get(url, ok, lambda *a: cb({}),
                     headers={'User-Agent': 'Flixdex/1.0 (personal desktop app)',
                              'Accept': 'application/sparql-results+json'})

    def closeEvent(self, e):
        self.save_ratings()
        self.save_ids()
        self.save_settings()
        super().closeEvent(e)


# ---------------------------------------------------------------- theme

def app_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(C_ACCENT))
    p.drawRoundedRect(QRectF(4, 10, 56, 44), 10, 10)
    p.setBrush(QColor('#fff'))
    path = QPainterPath()
    path.moveTo(26, 22)
    path.lineTo(44, 32)
    path.lineTo(26, 42)
    path.closeSubpath()
    p.drawPath(path)
    p.end()
    return QIcon(pm)


STYLE = f"""
QWidget {{ font-family: 'Segoe UI', sans-serif; font-size: 13px; }}
QMainWindow, QDialog, #detailPage {{ background: {C_BG}; }}
QDialog {{ background: {C_PANEL}; }}
#detailPage {{ background: {C_PANEL}; }}
QLabel {{ background: transparent; }}
QLabel#muted {{ color: {C_MUTED}; }}
QLabel#head {{ color: {C_MUTED}; font-size: 11px; font-weight: 700; }}
#top {{ background: #101016; border-bottom: 1px solid {C_LINE}; }}
#panel {{ background: {C_PANEL}; border: 1px solid {C_LINE}; border-radius: 12px; }}
#segbox {{ background: {C_PANEL}; border: 1px solid {C_LINE}; border-radius: 18px; }}
#notice {{ background: {C_PANEL}; border: 1px solid {C_LINE}; border-left: 3px solid {C_IMDB}; border-radius: 8px; }}
#score {{ background: {C_PANEL2}; border: 1px solid {C_LINE}; border-radius: 10px; }}
QLineEdit, QComboBox, QSpinBox {{ background: {C_PANEL2}; border: 1px solid {C_LINE}; border-radius: 8px;
    padding: 6px 10px; min-height: 22px; color: {C_TEXT}; selection-background-color: {C_ACCENT}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: #55556a; }}
QLineEdit#search {{ border-radius: 19px; padding: 7px 16px; font-size: 14px; background: {C_PANEL}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{ background: {C_PANEL2}; border: 1px solid {C_LINE}; color: {C_TEXT};
    selection-background-color: #2e2e3c; outline: none; padding: 4px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}
QPushButton {{ background: {C_PANEL2}; border: 1px solid {C_LINE}; border-radius: 8px; padding: 7px 14px;
    font-weight: 600; color: {C_TEXT}; }}
QPushButton:hover {{ border-color: #4a4a5c; background: #22222d; }}
QPushButton:checked {{ background: #2a2a36; }}
QPushButton:disabled {{ color: {C_MUTED}; }}
QPushButton#primary {{ background: {C_ACCENT}; border-color: {C_ACCENT}; color: white; }}
QPushButton#primary:hover {{ background: #ff2a35; }}
QPushButton#seg {{ background: transparent; border: none; border-radius: 15px; padding: 6px 18px; color: {C_MUTED}; }}
QPushButton#seg:hover {{ color: {C_TEXT}; }}
QPushButton#seg:checked {{ background: {C_TEXT}; color: #000; }}
QSlider::groove:horizontal {{ height: 4px; background: #2a2a36; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {C_ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: white; width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; }}
QListView {{ background: {C_BG}; border: none; outline: none; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2e2e3c; border-radius: 3px; min-height: 40px; }}
QScrollBar::handle:vertical:hover {{ background: #45455a; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}
QStatusBar {{ background: #101016; color: {C_MUTED}; border-top: 1px solid {C_LINE}; }}
QStatusBar QLabel {{ color: {C_MUTED}; padding: 0 8px; }}
QStatusBar::item {{ border: none; }}
QProgressBar {{ background: {C_PANEL2}; border: none; border-radius: 3px; max-height: 6px; }}
QProgressBar::chunk {{ background: {C_ACCENT}; border-radius: 3px; }}
QToolTip {{ background: {C_PANEL2}; color: {C_TEXT}; border: 1px solid {C_LINE}; padding: 4px 8px; }}
"""


def main():
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Flixdex.App')
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName('Flixdex')
    app.setStyle('Fusion')
    app.setFont(QFont('Segoe UI', 10))
    pal = QPalette()
    for role, color in [(QPalette.ColorRole.Window, C_BG), (QPalette.ColorRole.WindowText, C_TEXT),
                        (QPalette.ColorRole.Base, C_PANEL2), (QPalette.ColorRole.AlternateBase, C_PANEL),
                        (QPalette.ColorRole.Text, C_TEXT), (QPalette.ColorRole.Button, C_PANEL2),
                        (QPalette.ColorRole.ButtonText, C_TEXT), (QPalette.ColorRole.Highlight, C_ACCENT),
                        (QPalette.ColorRole.HighlightedText, '#ffffff'), (QPalette.ColorRole.ToolTipBase, C_PANEL2),
                        (QPalette.ColorRole.ToolTipText, C_TEXT), (QPalette.ColorRole.PlaceholderText, C_MUTED),
                        (QPalette.ColorRole.Link, '#ff3d47')]:
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
