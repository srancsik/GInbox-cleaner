"""Windows UI for triaging a Gmail inbox by sender.

Workflow: connect -> fetch senders (toplist) -> mark senders Keep/Delete ->
delete all mail from Delete-marked senders, either by moving to Gmail Trash
or permanently. Decisions persist in sender_decisions.csv across runs.
"""

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet

from PySide6.QtCore import (
    QObject, QThread, Signal, Slot, Qt, QUrl, QTimer, QSize,
    QPointF, QRectF, QLineF,
)
from PySide6.QtGui import (
    QColor, QBrush, QDesktopServices, QFont, QIcon, QImage, QPainter,
    QPen, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLineEdit, QPushButton, QLabel, QCheckBox, QProgressBar,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QRadioButton,
    QMessageBox, QHeaderView, QComboBox, QStyledItemDelegate, QDialog,
    QFileDialog,
)

import gmail_client
from credentials import CredentialStore, secure_write

__version__ = "2.1.3"


def _data_dir():
    """Per-user writable data dir, unless running as an explicit portable.

    A 'portable.flag' file next to the exe/script opts into portable mode
    (data travels with the bundle); GINBOX_CLEANER_DATA overrides entirely
    (used by tests). Everything else lands in the per-user data dir so
    nothing - especially credentials - is ever written next to the code.
    """
    override = os.environ.get("GINBOX_CLEANER_DATA")
    if override:
        return Path(override)
    here = Path(sys.executable if getattr(sys, "frozen", False)
                else __file__).resolve().parent
    if (here / "portable.flag").exists():
        return here
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA")
                    or (Path.home() / "AppData/Roaming"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME")
                    or (Path.home() / ".local/share"))
    return base / "ginbox-cleaner"


def _chmod_private(path, is_dir=False):
    """Owner-only access on POSIX (0700 dirs / 0600 files)."""
    if os.name == "posix":
        try:
            os.chmod(path, 0o700 if is_dir else 0o600)
        except OSError:
            pass


BASE_DIR = _data_dir()
if not BASE_DIR.exists():
    # Migrate the whole data dir from the pre-rename app names.
    for _old in ("gcleaner", "gmail-cleaner"):
        _OLD_DIR = Path.home() / ".local/share" / _old
        if _OLD_DIR.is_dir():
            shutil.copytree(_OLD_DIR, BASE_DIR)
            try:  # never leave a password in the old copy either
                _oc = _OLD_DIR / "config.json"
                _ocfg = json.loads(_oc.read_text(encoding="utf-8"))
                if "app_password" in _ocfg:
                    _ocfg.pop("app_password", None)
                    _oc.write_text(json.dumps(_ocfg, indent=2),
                                   encoding="utf-8")
            except Exception:
                pass
            break
BASE_DIR.mkdir(parents=True, exist_ok=True)
_chmod_private(BASE_DIR, is_dir=True)

if not getattr(sys, "frozen", False):
    # Earlier source-mode runs stored data next to the script. Move it to
    # the real data dir so secrets never sit beside the code again.
    _SCRIPT_DIR = Path(__file__).resolve().parent
    if _SCRIPT_DIR != BASE_DIR:
        for _name in ("config.json", "secrets.json", "accounts"):
            _src, _dst = _SCRIPT_DIR / _name, BASE_DIR / _name
            if _src.exists() and not _dst.exists():
                shutil.move(str(_src), str(_dst))
        for _name in ("sender_decisions.csv", "skiplist.csv",
                      "trash_errors.log"):
            _src, _dst = _SCRIPT_DIR / _name, BASE_DIR / _name
            if _src.exists() and not _dst.exists():
                shutil.copy2(_src, _dst)

CONFIG_FILE = BASE_DIR / "config.json"
ACCOUNTS_DIR = BASE_DIR / "accounts"
# Legacy global files, migrated into accounts/<user>/ on first login.
DECISIONS_FILE = BASE_DIR / "sender_decisions.csv"
SKIPLIST_FILE = BASE_DIR / "skiplist.csv"
ERROR_LOG = BASE_DIR / "trash_errors.log"

UNDECIDED, KEEP, DELETE, DELETED = "Undecided", "Keep", "Delete", "Deleted"

# decision -> (row background, decision-cell foreground)
DECISION_STYLES = {
    KEEP: ("#e8f5e9", "#2e7d32"),      # light green
    DELETE: ("#fce8e6", "#c5221f"),    # light red
    UNDECIDED: ("#fff3e0", "#e65100"), # light orange
    DELETED: ("#e3f2fd", "#1565c0"),   # light blue (already cleaned)
}

PRIMARY, DANGER, MUTED = "#1a73e8", "#d93025", "#5f6368"

# ---------- translations ----------

LANG = "en"

HU = {
    "Connect, review your senders, and clean up your inbox":
        "Csatlakozz az email fiókodhoz, nézd át a feladóidat, és takarítsd ki a postaládádat",
    "CAUTION: This app acts on your real mailbox. Prefer 'Move to "
    "Trash' - it stays recoverable for ~30 days. Permanent delete "
    "cannot be undone.":
        "FIGYELEM: Ez az alkalmazás a valódi postaládádon dolgozik. "
        "Inkább a 'Kukába helyezés' módot használd - az ~30 napig "
        "visszaállítható. A végleges törlés nem vonható vissza.",
    "Gmail login": "Gmail bejelentkezés",
    "App password": "Alkalmazásjelszó",
    " Get app password": " Alkalmazásjelszó igénylése",
    "Open myaccount.google.com/apppasswords in your browser":
        "A myaccount.google.com/apppasswords megnyitása a böngészőben",
    "Save credentials": "Bejelentkezési adatok mentése",
    " Connect": " Csatlakozás",
    "Not connected": "Nincs kapcsolat",
    "Stored in the OS credential manager":
        "A rendszer jelszókezelőjébe mentve",
    "(saved in {w})": "(mentve ide: {w})",
    "secrets.json": "secrets.json",
    "Windows Credential Manager": "Windows hitelesítőadat-kezelő",
    "GNOME Keyring": "GNOME Keyring",
    "KWallet": "KWallet",
    "No system keyring found": "Nem található rendszer-jelszókezelő",
    "No OS credential manager is available on this system. "
    "Save the app password in an owner-only secrets.json "
    "file instead?":
        "Ezen a rendszeren nem érhető el jelszókezelő. Elmented az "
        "alkalmazásjelszót egy csak a tulajdonos által olvasható "
        "secrets.json fájlba?",
    " Export CSV": " CSV exportálása",
    "Write a readable CSV copy of your decisions":
        "A döntéseid olvasható CSV-másolatának kiírása",
    "Export decisions": "Döntések exportálása",
    "Nothing to export.": "Nincs exportálható adat.",
    "Export failed": "Sikertelen exportálás",
    "CSV files (*.csv)": "CSV fájlok (*.csv)",
    " Import CSV": " CSV importálása",
    "Load decisions from a CSV file": "Döntések betöltése CSV-fájlból",
    "Import decisions": "Döntések importálása",
    "Connect to your account first.": "Előbb csatlakozz a fiókodhoz.",
    "Import failed": "Sikertelen importálás",
    "Imported {n} decisions.": "{n} döntés importálva.",
    "They are stored encrypted; use Export/Import CSV to "
    "move them between machines.<br><br>":
        "Titkosítva tárolódnak; az Export/Import CSV gépek között is "
        "átviheti őket.",
    "<b>Export / Import CSV</b><br>": "<b>Export / Import CSV</b><br>",
    "&bull; <b>Export CSV</b> writes a readable copy of "
    "all your decisions (sender + Keep/Delete) to a file "
    "you choose - handy as a backup or for moving to "
    "another machine.<br>":
        "&bull; Az <b>Export CSV</b> az összes döntésed "
        "(feladó + Megtart/Töröl) egy olvasható, általad választott "
        "fájlba írja - biztonsági mentésnek vagy másik gépre "
        "átvitelhez hasznos.<br>",
    "&bull; <b>Import CSV</b> reads such a file back in "
    "and merges it into your decisions for the account "
    "you are connected to - imported values win on "
     "conflicts, everything else is kept.<br>":
        "&bull; Az <b>Import CSV</b> beolvassa az ilyen fájlt, és "
        "beolvasztja az aktuálisan csatlakoztatott fiók "
        "döntéseibe - ütközésnél az importált érték nyer, minden "
        "más megmarad.<br>",
    "&bull; You must be <b>connected</b> to import, "
    "because decisions are stored per account. Older "
    "skiplist exports are accepted too - senders marked "
    "'y' become Keep.<br>":
        "&bull; Az importáláshoz <b>csatlakozva</b> kell lenned, "
        "mert a döntések fiókonként tárolódnak. A régi "
        "skiplist-exportok is működnek - az 'y'-nal jelölt "
        "feladók Megtart lesznek.<br>",
    "&bull; Anything that doesn't look like a valid email "
    "address is skipped, and the result is saved "
    "encrypted right away.<br><br>":
        "&bull; Amit nem e-mail címnek tűnik, az kihagyásra "
        "kerül, az eredmény pedig azonnal titkosítva "
        "mentődik.<br><br>",
    "Version {v}": "Verzió {v}",
    "Connecting...": "Csatlakozás...",
    "Connection failed": "Sikertelen csatlakozás",
    "Connected (trash folder: {trash})": "Csatlakozva (kuka: {trash})",
    "{total:,} emails from {senders:,} unique senders":
        "{total:,} levél {senders:,} egyedi feladótól",
    "Missing credentials": "Hiányzó adatok",
    "Enter your Gmail address and app password.":
        "Add meg a Gmail címed és az alkalmazásjelszót.",
    "Login failed": "Sikertelen bejelentkezés",
    "\n\nGmail requires an app password "
    "(myaccount.google.com/apppasswords), not your normal password.":
        "\n\nA Gmail a normál jelszó helyett alkalmazásjelszót igényel "
        "(myaccount.google.com/apppasswords).",
    "Inbox senders": "Beérkezett feladók",
    " Fetch senders": " Feladók lekérése",
    " Cancel": " Mégse",
    "Filter sender...": "Feladó szűrése...",
    "Emails: >100, <=10, =5": "Levelek: >100, <=10, =5",
    "Attach.: >0": "Csatolm.: >0",
    "All": "Összes",
    "Sender": "Feladó",
    "Emails": "Levelek",
    "Emails with Attachment": "Csatolmányos levelek",
    "Decision": "Döntés",
    "Estimated from Content-Type (multipart/mixed or non-text part)":
        "A Content-Type alapján becsülve (multipart/mixed vagy nem "
        "szöveges rész)",
    "Undecided": "Eldöntetlen",
    "Keep": "Megtart",
    "Delete": "Töröl",
    "Deleted": "Törölt",
    " Mark Keep": " Megtartásra jelöl",
    " Mark Delete": " Törlésre jelöl",
    " Mark Undecided": " Eldöntetlenre jelöl",
    "Select one or more rows first":
        "Először jelölj ki egy vagy több sort",
    "{n:,} senders ({e:,} emails)": "{n:,} feladó ({e:,} levél)",
    "Confirm marking for deletion": "Törlésre jelölés megerősítése",
    "{n} sender(s) will be marked Delete.\n\nAre you sure?":
        "{n} feladó lesz törlésre jelölve.\n\nBiztos vagy benne?",
    "Deletion": "Törlés",
    "Move to Trash (recoverable ~30 days)":
        "Kukába helyezés (~30 napig visszaállítható)",
    "Permanently delete (cannot be undone)":
        "Végleges törlés (nem vonható vissza)",
    " Delete marked senders": " Megjelölt feladók törlése",
    "The email address changed since you connected. Press Connect again "
    "to use the new account.":
        "Az e-mail cím megváltozott a csatlakozás óta. Az új fiók "
        "használatához nyomj újra Csatlakozást.",
    "Nothing to delete": "Nincs törlendő",
    "No senders are marked Delete (with mail in Inbox).":
        "Egy feladó sincs törlésre jelölve (levéllel a Beérkezettekben).",
    "Confirm deletion": "Törlés megerősítése",
    "{s:,} senders / {e:,} emails will be moved to the Gmail Trash.":
        "{s:,} feladó / {e:,} levél a Gmail Kukába kerül.",
    "{s:,} senders / {e:,} emails will be PERMANENTLY DELETED "
    "(this cannot be undone).":
        "{s:,} feladó / {e:,} levél VÉGLEGESEN TÖRLŐDIK "
        "(nem vonható vissza).",
    "Undecided senders: {u:,} - they will be kept.":
        "Eldöntetlen feladók: {u:,} - ezek megmaradnak.",
    "WARNING: permanent deletion is IRREVERSIBLE.":
        "FIGYELEM: a végleges törlés VISSZAFORDÍTHATATLAN.",
    "Deletion complete": "Törlés kész",
    "Deleted {n:,} emails from {m:,} senders.":
        "{n:,} levél törölve {m:,} feladótól.",
    "Failed senders: {x:,}": "Sikertelen feladók: {x:,}",
    " (see {name})": " (lásd: {name})",
    "Operation cancelled": "Művelet megszakítva",
    "Operation failed": "Sikertelen művelet",
    "Could not save decisions: {exc}":
        "A döntések mentése nem sikerült: {exc}",
    "Help - what does this app do?":
        "Súgó - mit csinál ez az alkalmazás?",
    "About GInbox Cleaner": "A GInbox Cleaner névjegye",
    " Got it": " Rendben",
    "<b>What it does</b><br>": "<b>Mit csinál?</b><br>",
    "Connects to your Gmail over IMAP and ranks the senders in your "
    "Inbox by message count. You classify senders, then clean out "
    "the unwanted ones in bulk:<br><br>":
        "IMAP-on keresztül csatlakozik a Gmail fiókodhoz, és az "
        "üzenetszám szerint sorba rendezi a Beérkezett mappa feladóit. "
        "A feladókat osztályozod, majd a nem kívántakat tömegesen "
        "törlöd:<br><br>",
    "&bull; <b>Keep</b> - never touched by the cleanup.<br>":
        "&bull; <b>Megtart</b> - a takarítás nem nyúl hozzá.<br>",
    "&bull; <b>Delete</b> - all their mail is removed on cleanup.<br>":
        "&bull; <b>Töröl</b> - az összes levelük törlődik a takarításkor.<br>",
    "&bull; <b>Undecided</b> - left alone until you decide.<br><br>":
        "&bull; <b>Eldöntetlen</b> - békén hagyja, amíg nem döntesz.<br><br>",
    "Your decisions are saved per Gmail account and re-applied on the "
    "next run.<br>":
        "A döntéseid Gmail fiókonként mentődnek, és a következő "
        "futtatáskor újra alkalmazódnak.<br><br>",
    "<b>How to use</b><br>": "<b>Használat</b><br>",
    "1. Enter your Gmail address and an <b>app password</b> - not "
    "your normal password. 'Get app password' opens the Google "
    "page to create one.<br>":
        "1. Add meg a Gmail címed és egy <b>alkalmazásjelszót</b> - nem "
        "a normál jelszavad. Az 'Alkalmazásjelszó igénylése' gomb "
        "megnyitja a Google oldalt a létrehozáshoz.<br>",
    "2. Click <b>Connect</b>, then <b>Fetch senders</b>.<br>":
        "2. Kattints a <b>Csatlakozás</b>, majd a <b>Feladók "
        "lekérése</b> gombra.<br>",
    "3. Mark rows with the buttons, or right-click a row to cycle "
    "Keep/Delete/Undecided. Use the filters to narrow the list.<br>":
        "3. Jelöld a sorokat a gombokkal, vagy kattints jobb gombbal egy "
        "sorra a Megtart/Töröl/Eldöntetlen körforgásához. A szűrőkkel "
        "szűkítheted a listát.<br>",
    "4. Choose <b>Move to Trash</b> (recoverable ~30 days) or "
    "<b>Permanently delete</b>, then click <b>Delete marked "
    "senders</b>.<br><br>":
        "4. Válaszd a <b>Kukába helyezés</b> (~30 napig "
        "visszaállítható) vagy a <b>Végleges törlés</b> lehetőséget, "
        "majd kattints a <b>Megjelölt feladók törlése</b> "
        "gombra.<br><br>",
    "<b>Use carefully</b><br>": "<b>Óvatosan használd</b><br>",
    "This acts on your real mailbox. Only mail moved to Trash can "
    "be recovered - permanent deletion cannot be undone.":
        "Ez a valódi postaládádon működik. Csak a Kukába helyezett "
        "levelek állíthatók vissza - a végleges törlés nem vonható "
        "vissza.",
    "Moving to Trash": "Kukába helyezés",
    "Permanently deleting": "Végleges törlés",
    "Please wait - processing your mailbox.":
        "Kérlek, várj - a postaláda feldolgozása folyamatban.",
    "{done} / {total} senders": "{done} / {total} feladó",
}


def tr(s):
    """Translate a UI string to the current language (English fallback)."""
    return HU.get(s, s) if LANG == "hu" else s


def decision_label(d):
    """Display text for an internal decision value."""
    return tr(d)


_CSV_FORMULA_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    """Prefix a leading formula char so exported CSVs stay inert when
    opened in Excel/LibreOffice (sender addresses are attacker input)."""
    s = str(value)
    return "'" + s if s[:1] in _CSV_FORMULA_CHARS else s


def _csv_unsafe(value):
    """Reverse _csv_safe on read so decisions round-trip unchanged."""
    s = str(value)
    return s[1:] if s.startswith("'") and s[1:2] in _CSV_FORMULA_CHARS else s


def _read_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_config(cfg):
    secure_write(CONFIG_FILE, json.dumps(cfg, indent=2).encode("utf-8"))


def _saved_language():
    return _read_config().get("language", "en")


def _flag_canvas(w, h):
    pm = QPixmap(w * 2, h * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    return pm, p


def flag_hu(w=24):
    h = w * 2 // 3
    pm, p = _flag_canvas(w, h)
    stripe = h / 3
    for i, c in enumerate(("#ce2939", "#ffffff", "#477050")):
        p.fillRect(QRectF(0, i * stripe, w, stripe + 0.5), QColor(c))
    p.setPen(QPen(QColor("#dadce0"), 1))
    p.setBrush(Qt.NoBrush)
    p.drawRect(QRectF(0.5, 0.5, w - 1, h - 1))
    p.end()
    return QIcon(pm)


def flag_en(w=24):
    """Simplified Union Jack (emoji flags don't render on Windows)."""
    h = w * 2 // 3
    pm, p = _flag_canvas(w, h)
    p.fillRect(QRectF(0, 0, w, h), QColor("#012169"))
    p.setPen(QPen(QColor("#ffffff"), h * 0.18, Qt.SolidLine, Qt.FlatCap))
    p.drawLine(QLineF(-1, -1, w + 1, h + 1))
    p.drawLine(QLineF(w + 1, -1, -1, h + 1))
    p.setPen(QPen(QColor("#ffffff"), h * 0.30, Qt.SolidLine, Qt.FlatCap))
    p.drawLine(QLineF(w / 2, -1, w / 2, h + 1))
    p.drawLine(QLineF(-1, h / 2, w + 1, h / 2))
    p.setPen(QPen(QColor("#c8102e"), h * 0.16, Qt.SolidLine, Qt.FlatCap))
    p.drawLine(QLineF(w / 2, -1, w / 2, h + 1))
    p.drawLine(QLineF(-1, h / 2, w + 1, h / 2))
    p.setPen(QPen(QColor("#dadce0"), 1))
    p.setBrush(Qt.NoBrush)
    p.drawRect(QRectF(0.5, 0.5, w - 1, h - 1))
    p.end()
    return QIcon(pm)

STYLESHEET = """
QMainWindow, QDialog, QMessageBox { background: #f1f3f4; }
QWidget { color: #202124; }
QGroupBox {
    background: #ffffff; border: 1px solid #e3e6ea; border-radius: 10px;
    margin-top: 16px; padding-top: 16px;
}
QGroupBox::title {
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 14px; padding: 0 4px; color: #5f6368; font-weight: bold;
}
QLineEdit, QComboBox {
    background: #ffffff; border: 1px solid #dadce0; border-radius: 6px;
    padding: 6px 10px; selection-background-color: #e8f0fe;
}
QLineEdit:focus, QComboBox:focus { border: 2px solid #1a73e8; }
QComboBox QAbstractItemView {
    background: #ffffff; border: 1px solid #dadce0;
    selection-background-color: #e8f0fe;
}
QPushButton {
    background: #ffffff; border: 1px solid #dadce0; border-radius: 6px;
    padding: 7px 16px; color: #202124;
}
QPushButton:hover { background: #f8f9fa; border-color: #bdc1c6; }
QPushButton:pressed { background: #eceff1; }
QPushButton:disabled { color: #9aa0a6; background: #f1f3f4; border-color: #e3e6ea; }
QPushButton#primary {
    background: #1a73e8; color: #ffffff; border: none; font-weight: bold;
}
QPushButton#primary:hover { background: #1765cc; }
QPushButton#primary:pressed { background: #1556b0; }
QPushButton#primary:disabled { background: #aecbfa; color: #ffffff; }
QPushButton#danger {
    background: #d93025; color: #ffffff; border: none; font-weight: bold;
}
QPushButton#danger:hover { background: #b7271e; }
QPushButton#danger:pressed { background: #9c1f16; }
QPushButton#danger:disabled { background: #f4b6ae; color: #ffffff; }
QTableWidget {
    background: #ffffff; border: 1px solid #e3e6ea; border-radius: 10px;
    gridline-color: #f1f3f4; alternate-background-color: #fafafa;
}
QTableWidget::item { padding: 4px; border: none; }
QTableWidget::item:selected { background: #e8f0fe; color: #202124; }
QHeaderView::section {
    background: #ffffff; border: none; border-bottom: 2px solid #e3e6ea;
    padding: 7px 6px; font-weight: bold; color: #5f6368;
}
QTableCornerButton::section { background: #ffffff; border: none; }
QProgressBar {
    border: none; background: #e8eaed; border-radius: 5px;
    min-height: 10px; max-height: 10px; text-align: center;
    color: #5f6368; font-size: 10px;
}
QProgressBar::chunk { background: #1a73e8; border-radius: 5px; }
QCheckBox, QRadioButton { spacing: 6px; }
QCheckBox::indicator { width: 15px; height: 15px; }
QRadioButton::indicator { width: 18px; height: 18px; }
QCheckBox::indicator {
    border: 2px solid #80868b; border-radius: 3px; background: #ffffff;
}
QCheckBox::indicator:hover { border-color: #1a73e8; }
QCheckBox::indicator:checked {
    background: #1a73e8; border-color: #1a73e8; image: url('%%CHECK_IMG%%');
}
QCheckBox::indicator:disabled { border-color: #dadce0; background: #f1f3f4; }
QRadioButton::indicator { image: url('%%RADIO_OFF%%'); }
QRadioButton::indicator:hover { image: url('%%RADIO_HOVER%%'); }
QRadioButton::indicator:checked { image: url('%%RADIO_IMG%%'); }
QRadioButton::indicator:disabled { image: url('%%RADIO_DIS%%'); }
QWidget#warnbar {
    background: #fef7e0; border: 1px solid #f9ab00; border-radius: 8px;
}
QLabel#warntext { color: #7a5c00; font-weight: bold; }
QPushButton#help {
    background: #ffffff; border: 1px solid #dadce0; border-radius: 15px;
    font-weight: bold; color: #5f6368; padding: 0px;
}
QPushButton#help:hover { border-color: #1a73e8; color: #1a73e8; }
QPushButton#flag {
    background: #ffffff; border: 1px solid #dadce0; border-radius: 4px;
    padding: 0px;
}
QPushButton#flag:hover { border-color: #bdc1c6; }
QPushButton#flag[active="true"] { border: 2px solid #1a73e8; }
QLabel#title { font-size: 20px; font-weight: bold; }
QLabel#subtitle { color: #5f6368; }
QLabel#summary { color: #202124; }
QStatusBar { background: #f1f3f4; color: #5f6368; }
QToolTip {
    background: #3c4043; color: #ffffff; border: none; padding: 5px 8px;
}
QMenu {
    background: #ffffff; border: 1px solid #e3e6ea; border-radius: 6px;
    padding: 4px;
}
QMenu::item { padding: 6px 24px; border-radius: 4px; }
QMenu::item:selected { background: #e8f0fe; }
"""

CANCELLED = "__cancelled__"


# ---------- lightweight vector icons (no asset files needed) ----------

def _icon_canvas(size, color):
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(color), 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    u = size / 18.0  # icons designed on an 18x18 grid
    return pm, p, u


def make_icon(kind, color=MUTED, size=16):
    """Draw a simple line-style icon and return it as QIcon."""
    pm, p, u = _icon_canvas(size, color)

    def line(x1, y1, x2, y2):
        p.drawLine(QLineF(x1 * u, y1 * u, x2 * u, y2 * u))

    def circle(cx, cy, r):
        p.drawEllipse(QPointF(cx * u, cy * u), r * u, r * u)

    def poly(pts):
        p.drawPolyline(QPolygonF([QPointF(x * u, y * u) for x, y in pts]))

    if kind == "key":
        circle(6, 6, 3)
        line(8.2, 8.2, 15, 15)
        line(12, 12, 14.5, 9.5)
        line(14.5, 14.5, 17, 12)
    elif kind == "globe":
        circle(9, 9, 6.5)
        p.drawEllipse(QRectF(6 * u, 2.5 * u, 6 * u, 13 * u))
        line(2.5, 9, 15.5, 9)
    elif kind == "download":
        line(9, 3, 9, 11.5)
        poly([(5.5, 8), (9, 11.5), (12.5, 8)])
        poly([(4, 13), (4, 15), (14, 15), (14, 13)])
    elif kind == "upload":
        line(9, 13, 9, 6.5)
        poly([(5.5, 10), (9, 6.5), (12.5, 10)])
        poly([(4, 13), (4, 15), (14, 15), (14, 13)])
    elif kind == "x":
        line(5, 5, 13, 13)
        line(13, 5, 5, 13)
    elif kind == "check":
        poly([(3.5, 9.5), (7.5, 13.5), (14.5, 5)])
    elif kind == "trash":
        line(3.5, 4.5, 14.5, 4.5)
        line(7, 4.5, 7, 2.5)
        line(11, 4.5, 11, 2.5)
        line(7, 2.5, 11, 2.5)
        poly([(4.5, 6), (5.5, 15.5), (12.5, 15.5), (13.5, 6)])
        line(7.5, 8, 8, 13.5)
        line(10.5, 8, 10, 13.5)
    elif kind == "reset":
        p.drawArc(QRectF(4 * u, 4 * u, 10 * u, 10 * u), 50 * 16, 295 * 16)
        poly([(10, 1.5), (13.6, 4.6), (9.6, 6.4)])
    elif kind == "search":
        circle(7.5, 7.5, 4.5)
        line(10.8, 10.8, 15, 15)
    elif kind == "mail":
        p.drawRoundedRect(QRectF(2.5 * u, 4 * u, 13 * u, 10 * u), 1.5, 1.5)
        poly([(3.5, 5.5), (9, 9.5), (14.5, 5.5)])
    elif kind == "warn":
        p.drawPolygon(QPolygonF([QPointF(9 * u, 2.5 * u),
                                 QPointF(16.5 * u, 15.5 * u),
                                 QPointF(1.5 * u, 15.5 * u)]))
        line(9, 7, 9, 11.5)
        p.drawPoint(QPointF(9 * u, 13.8 * u))
    p.end()
    return QIcon(pm)


def make_logo(size=40):
    """Rounded blue tile with a white envelope glyph."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(PRIMARY))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.25, size * 0.25)
    p.setPen(QPen(Qt.white, size * 0.075, Qt.SolidLine,
                  Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    m = size * 0.24
    p.drawRoundedRect(QRectF(m, size * 0.30, size - 2 * m, size * 0.42),
                      size * 0.05, size * 0.05)
    p.drawPolyline(QPolygonF([
        QPointF(m * 1.15, size * 0.34),
        QPointF(size * 0.5, size * 0.55),
        QPointF(size - m * 1.15, size * 0.34),
    ]))
    p.end()
    return pm


class TintDelegate(QStyledItemDelegate):
    """Paints per-item BackgroundRole colors.

    The app stylesheet suppresses item backgrounds, so rows are tinted by
    filling the cell rect before the normal item painting.
    """

    def paint(self, painter, option, index):
        bg = index.data(Qt.BackgroundRole)
        if bg:
            color = bg.color() if isinstance(bg, QBrush) else QColor(bg)
            painter.fillRect(option.rect, color)
        super().paint(painter, option, index)


class Worker(QObject):
    """Runs a gmail_client job off the UI thread."""

    progress = Signal(int, int)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn, self._args, self._kwargs = fn, args, kwargs
        self._cancelled = False

    @Slot()
    def run(self):
        try:
            result = self._fn(*self._args,
                              progress_cb=self.progress.emit,
                              cancel=lambda: self._cancelled,
                              **self._kwargs)
        except gmail_client.CancelledError:
            self.failed.emit(CANCELLED)
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.finished.emit(result)

    def cancel(self):
        self._cancelled = True


class HelpDialog(QDialog):
    """Startup help/about popup, reopenable via the '?' header button."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("About GInbox Cleaner"))
        self.setModal(True)
        self.setMinimumWidth(540)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(make_logo(36))
        t = QLabel("GInbox Cleaner")
        t.setObjectName("title")
        head.addWidget(logo)
        head.addSpacing(8)
        head.addWidget(t)
        head.addStretch(1)
        head.addWidget(QLabel(tr("Version {v}").format(v=__version__)))
        lay.addLayout(head)

        body = QLabel(
            tr("<b>What it does</b><br>")
            + tr("Connects to your Gmail over IMAP and ranks the senders in "
                 "your Inbox by message count. You classify senders, then "
                 "clean out the unwanted ones in bulk:<br><br>")
            + tr("&bull; <b>Keep</b> - never touched by the cleanup.<br>")
            + tr("&bull; <b>Delete</b> - all their mail is removed on "
                 "cleanup.<br>")
            + tr("&bull; <b>Undecided</b> - left alone until you "
                 "decide.<br><br>")
            + tr("Your decisions are saved per Gmail account and "
                 "re-applied on the next run.<br>")
            + tr("They are stored encrypted; use Export/Import CSV to "
                 "move them between machines.<br><br>")
            + tr("<b>Export / Import CSV</b><br>")
            + tr("&bull; <b>Export CSV</b> writes a readable copy of "
                 "all your decisions (sender + Keep/Delete) to a file "
                 "you choose - handy as a backup or for moving to "
                 "another machine.<br>")
            + tr("&bull; <b>Import CSV</b> reads such a file back in "
                 "and merges it into your decisions for the account "
                 "you are connected to - imported values win on "
                 "conflicts, everything else is kept.<br>")
            + tr("&bull; You must be <b>connected</b> to import, "
                 "because decisions are stored per account. Older "
                 "skiplist exports are accepted too - senders marked "
                 "'y' become Keep.<br>")
            + tr("&bull; Anything that doesn't look like a valid email "
                 "address is skipped, and the result is saved "
                 "encrypted right away.<br><br>")
            + tr("<b>How to use</b><br>")
            + tr("1. Enter your Gmail address and an <b>app password</b> - "
                 "not your normal password. 'Get app password' opens the "
                 "Google page to create one.<br>")
            + tr("2. Click <b>Connect</b>, then <b>Fetch senders</b>.<br>")
            + tr("3. Mark rows with the buttons, or right-click a row to "
                 "cycle Keep/Delete/Undecided. Use the filters to narrow "
                 "the list.<br>")
            + tr("4. Choose <b>Move to Trash</b> (recoverable ~30 days) or "
                 "<b>Permanently delete</b>, then click <b>Delete marked "
                 "senders</b>.<br><br>")
            + tr("<b>Use carefully</b><br>")
            + tr("This acts on your real mailbox. Only mail moved to Trash "
                 "can be recovered - permanent deletion cannot be undone."))
        body.setWordWrap(True)
        lay.addWidget(body)

        ok = QPushButton(tr(" Got it"))
        ok.setObjectName("primary")
        ok.setIcon(make_icon("check", "#ffffff"))
        ok.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(ok)
        lay.addLayout(row)


class ProgressDialog(QDialog):
    """Progress window shown while a long delete operation runs.

    Non-modal (shown with show()) so worker signals keep flowing.
    No close button - the only way out is the Cancel button.
    """

    def __init__(self, trash_mode, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags()
                            & ~Qt.WindowContextHelpButtonHint
                            & ~Qt.WindowCloseButtonHint)
        title = tr("Moving to Trash") if trash_mode \
            else tr("Permanently deleting")
        self.setWindowTitle(title)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        head = QLabel(title)
        head.setObjectName("title")
        lay.addWidget(head)
        detail = QLabel(tr("Please wait - processing your mailbox."))
        detail.setWordWrap(True)
        lay.addWidget(detail)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        lay.addWidget(self.bar)
        self.count_lbl = QLabel("")
        lay.addWidget(self.count_lbl)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_btn = QPushButton(tr(" Cancel"))
        self.cancel_btn.setIcon(make_icon("x"))
        row.addWidget(self.cancel_btn)
        lay.addLayout(row)

    def update_progress(self, done, total):
        self.bar.setMaximum(max(total, 1))
        self.bar.setValue(done)
        self.count_lbl.setText(
            tr("{done} / {total} senders")
            .format(done=done, total=total))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GInbox Cleaner")
        self.resize(980, 720)
        self.connected = False
        self.email = ""
        self.password = ""
        self.trash_mailbox = None
        self.decisions = {}
        self._account = None
        self.creds = CredentialStore(BASE_DIR / "secrets.json")
        self._file_ok = False  # user consented to file fallback this run
        self.counts = Counter()
        self.att_counts = Counter()
        self._thread = None
        self._worker = None
        self._on_done_cb = None
        self._on_fail_cb = None
        self._last_total = 0
        self._del_dlg = None
        global LANG
        LANG = _saved_language()
        self._build_ui()
        self._update_flag_state()
        self._load_config()
        self._update_summary()
        QTimer.singleShot(200, self._show_help)

    # ---------- UI construction ----------

    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 10)
        root.setSpacing(10)

        # ---- header ----
        header = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(make_logo(44))
        titles = QVBoxLayout()
        titles.setSpacing(0)
        t1 = QLabel("GInbox Cleaner")
        t1.setObjectName("title")
        self.subtitle_lbl = QLabel(
            tr("Connect, review your senders, and clean up your inbox"))
        self.subtitle_lbl.setObjectName("subtitle")
        titles.addWidget(t1)
        titles.addWidget(self.subtitle_lbl)
        header.addWidget(logo)
        header.addSpacing(10)
        header.addLayout(titles)
        header.addStretch(1)
        self.lang_btns = {}
        for code, maker in (("en", flag_en), ("hu", flag_hu)):
            b = QPushButton()
            b.setObjectName("flag")
            b.setIcon(maker(24))
            b.setIconSize(QSize(24, 16))
            b.setFixedSize(34, 26)
            b.setToolTip("English" if code == "en" else "Magyar")
            b.clicked.connect(
                lambda _c=False, cc=code: self._set_language(cc))
            self.lang_btns[code] = b
            header.addWidget(b)
        header.addSpacing(6)
        self.help_btn = QPushButton("?")
        self.help_btn.setObjectName("help")
        self.help_btn.setFixedSize(30, 30)
        self.help_btn.setToolTip(tr("Help - what does this app do?"))
        self.help_btn.clicked.connect(self._show_help)
        header.addWidget(self.help_btn)
        root.addLayout(header)

        # ---- caution banner ----
        warnbar = QWidget()
        warnbar.setObjectName("warnbar")
        wl = QHBoxLayout(warnbar)
        wl.setContentsMargins(10, 6, 10, 6)
        wl.setSpacing(8)
        wi = QLabel()
        wi.setPixmap(make_icon("warn", "#f9ab00", 18).pixmap(18, 18))
        self.warn_lbl = QLabel(
            tr("CAUTION: This app acts on your real mailbox. Prefer 'Move to "
               "Trash' - it stays recoverable for ~30 days. Permanent delete "
               "cannot be undone."))
        self.warn_lbl.setObjectName("warntext")
        self.warn_lbl.setWordWrap(True)
        wl.addWidget(wi)
        wl.addWidget(self.warn_lbl, 1)
        root.addWidget(warnbar)

        # ---- login card ----
        self.login_box = QGroupBox(tr("Gmail login"))
        form = QHBoxLayout()
        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("you@gmail.com")
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.pass_edit.setPlaceholderText(tr("App password"))
        self.get_pw_btn = QPushButton(tr(" Get app password"))
        self.get_pw_btn.setIcon(make_icon("key"))
        self.get_pw_btn.setToolTip(
            tr("Open myaccount.google.com/apppasswords in your browser"))
        self.save_cred = QCheckBox(tr("Save credentials"))
        self.save_cred.setToolTip(
            tr("Stored in the OS credential manager"))
        self.connect_btn = QPushButton(tr(" Connect"))
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setIcon(make_icon("globe", "#ffffff"))
        self.status_lbl = QLabel(tr("Not connected"))
        self.cred_hint = QLabel("")
        self.cred_hint.setObjectName("muted")
        for w in (QLabel("Email:"), self.email_edit, QLabel("App password:"),
                  self.pass_edit, self.get_pw_btn, self.save_cred,
                  self.cred_hint, self.connect_btn, self.status_lbl):
            form.addWidget(w)
        form.setStretch(1, 1)
        form.setStretch(3, 1)
        self.login_box.setLayout(form)
        root.addWidget(self.login_box)

        # ---- senders card ----
        self.senders_box = QGroupBox(tr("Inbox senders"))
        sv = QVBoxLayout()

        fetch_row = QHBoxLayout()
        self.fetch_btn = QPushButton(tr(" Fetch senders"))
        self.fetch_btn.setObjectName("primary")
        self.fetch_btn.setIcon(make_icon("download", "#ffffff"))
        self.fetch_btn.setEnabled(False)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.cancel_btn = QPushButton(tr(" Cancel"))
        self.cancel_btn.setIcon(make_icon("x"))
        self.cancel_btn.setEnabled(False)
        fetch_row.addWidget(self.fetch_btn)
        fetch_row.addWidget(self.progress, 1)
        fetch_row.addWidget(self.cancel_btn)
        sv.addLayout(fetch_row)

        filter_row = QHBoxLayout()
        search_lbl = QLabel()
        search_lbl.setPixmap(
            make_icon("search", MUTED, 14).pixmap(14, 14))
        self.f_sender = QLineEdit()
        self.f_sender.setPlaceholderText(tr("Filter sender..."))
        self.f_emails = QLineEdit()
        self.f_emails.setPlaceholderText(tr("Emails: >100, <=10, =5"))
        self.f_att = QLineEdit()
        self.f_att.setPlaceholderText(tr("Attach.: >0"))
        self.f_decision = QComboBox()
        self.f_decision.addItem(tr("All"), None)
        for d in (UNDECIDED, KEEP, DELETE, DELETED):
            self.f_decision.addItem(decision_label(d), d)
        filter_row.addWidget(search_lbl)
        filter_row.addWidget(self.f_sender, 4)
        filter_row.addWidget(self.f_emails, 2)
        filter_row.addWidget(self.f_att, 2)
        filter_row.addWidget(self.f_decision, 2)
        sv.addLayout(filter_row)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            [tr("Sender"), tr("Emails"), tr("Emails with Attachment"),
             tr("Decision")])
        self.table.horizontalHeaderItem(2).setToolTip(
            tr("Estimated from Content-Type (multipart/mixed or "
               "non-text part)"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setItemDelegate(TintDelegate(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        for col in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeToContents)
        self.table.setSortingEnabled(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_right_click)
        sv.addWidget(self.table, 1)

        mark_row = QHBoxLayout()
        self.keep_btn = QPushButton(tr(" Mark Keep"))
        self.keep_btn.setIcon(make_icon("check", "#137333"))
        self.delete_mark_btn = QPushButton(tr(" Mark Delete"))
        self.delete_mark_btn.setIcon(make_icon("trash", DANGER))
        self.undecided_btn = QPushButton(tr(" Mark Undecided"))
        self.undecided_btn.setIcon(make_icon("reset"))
        self.export_btn = QPushButton(tr(" Export CSV"))
        self.export_btn.setIcon(make_icon("download"))
        self.export_btn.setToolTip(
            tr("Write a readable CSV copy of your decisions"))
        self.import_btn = QPushButton(tr(" Import CSV"))
        self.import_btn.setIcon(make_icon("upload"))
        self.import_btn.setToolTip(
            tr("Load decisions from a CSV file"))
        for b in (self.keep_btn, self.delete_mark_btn, self.undecided_btn,
                  self.export_btn, self.import_btn):
            mark_row.addWidget(b)
        mark_row.addStretch(1)
        self.summary_lbl = QLabel("")
        self.summary_lbl.setObjectName("summary")
        self.summary_lbl.setTextFormat(Qt.RichText)
        mark_row.addWidget(self.summary_lbl)
        sv.addLayout(mark_row)

        self.senders_box.setLayout(sv)
        root.addWidget(self.senders_box, 1)

        # ---- deletion card ----
        self.exec_box = QGroupBox(tr("Deletion"))
        erow = QHBoxLayout()
        self.mode_trash = QRadioButton(
            tr("Move to Trash (recoverable ~30 days)"))
        self.mode_perm = QRadioButton(
            tr("Permanently delete (cannot be undone)"))
        self.mode_trash.setChecked(True)
        self.run_delete_btn = QPushButton(tr(" Delete marked senders"))
        self.run_delete_btn.setObjectName("danger")
        self.run_delete_btn.setIcon(make_icon("trash", "#ffffff"))
        self.run_delete_btn.setEnabled(False)
        erow.addWidget(self.mode_trash)
        erow.addWidget(self.mode_perm)
        erow.addStretch(1)
        erow.addWidget(self.run_delete_btn)
        self.exec_box.setLayout(erow)
        root.addWidget(self.exec_box)

        self.setCentralWidget(central)

        self.connect_btn.clicked.connect(self.on_connect)
        self.get_pw_btn.clicked.connect(self.on_get_password)
        self.fetch_btn.clicked.connect(self.on_fetch)
        self.cancel_btn.clicked.connect(self.on_cancel)
        self.keep_btn.clicked.connect(lambda: self._mark_selected(KEEP))
        self.delete_mark_btn.clicked.connect(lambda: self._mark_selected(DELETE))
        self.undecided_btn.clicked.connect(lambda: self._mark_selected(UNDECIDED))
        self.export_btn.clicked.connect(self.on_export)
        self.import_btn.clicked.connect(self.on_import)
        self.f_sender.textChanged.connect(self._apply_filters)
        self.f_emails.textChanged.connect(self._apply_filters)
        self.f_att.textChanged.connect(self._apply_filters)
        self.f_decision.currentTextChanged.connect(self._apply_filters)
        self.run_delete_btn.clicked.connect(self.on_delete)

    # ---------- config & decision persistence ----------

    def _load_config(self):
        cfg = _read_config()
        email = cfg.get("email", "")
        self.email_edit.setText(email)
        # One-time migration: plaintext passwords written by older
        # versions move into the credential store and out of the file.
        if "app_password" in cfg:
            if email:
                self.creds.set(email, cfg["app_password"])
            cfg.pop("app_password", None)
            try:
                _write_config(cfg)
            except Exception:
                pass
        if email:
            pw = self.creds.get(email)
            if pw:
                self.pass_edit.setText(pw)
                self.save_cred.setChecked(True)
        self._update_cred_hint()

    def _update_cred_hint(self):
        """Show where the saved password actually lives, next to the
        'Save credentials' box."""
        email = self.email_edit.text().strip().lower()
        if email and self.creds.get(email):
            where = tr(self.creds.backend_label or "secrets.json")
            self.cred_hint.setText(tr("(saved in {w})").format(w=where))
        else:
            self.cred_hint.setText("")

    def _persist_login(self, email, password):
        """Honor the 'Save credentials' checkbox after a successful login."""
        cfg = _read_config()
        if self.save_cred.isChecked() \
                and self.creds.backend_label is None and not self._file_ok:
            # No OS keyring: warn once before writing a file fallback
            btn = QMessageBox.question(
                self, tr("No system keyring found"),
                tr("No OS credential manager is available on this system. "
                   "Save the app password in an owner-only secrets.json "
                   "file instead?"),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if btn != QMessageBox.Yes:
                self.save_cred.setChecked(False)
            else:
                self._file_ok = True
        if self.save_cred.isChecked():
            self.creds.set(email, password)
            cfg["email"] = email
        else:
            self.creds.delete(email)
            cfg.pop("email", None)
        try:
            _write_config(cfg)
        except Exception:
            pass
        self._update_cred_hint()

    def _account_dir(self):
        # Hash the address so folder names don't reveal which accounts
        # use the app.
        slug = hashlib.sha256(
            self._account.encode("utf-8")).hexdigest()[:16]
        return ACCOUNTS_DIR / slug

    def _load_account(self, email):
        """Switch the active account and load its saved decisions.

        On a user's first login, any global sender_decisions.csv /
        skiplist.csv from before per-account storage is moved into that
        account's folder, so old rules are attributed to the first account
        that claims them and never leak into other accounts.
        """
        self._account = email.strip().lower()
        # Rename a pre-2.1 slugged folder (which revealed the address)
        # to the hashed form.
        legacy_dir = ACCOUNTS_DIR / re.sub(r"[^A-Za-z0-9._-]", "_",
                                           self._account)
        if legacy_dir != self._account_dir() and legacy_dir.is_dir() \
                and not self._account_dir().exists():
            shutil.move(str(legacy_dir), str(self._account_dir()))
        dec_file = self._account_dir() / "sender_decisions.csv"
        if not dec_file.exists() and DECISIONS_FILE.exists():
            self._account_dir().mkdir(parents=True, exist_ok=True)
            shutil.move(str(DECISIONS_FILE), dec_file)
            if SKIPLIST_FILE.exists():
                shutil.move(str(SKIPLIST_FILE),
                            self._account_dir() / "skiplist.csv")
        self.decisions = self._load_decisions()

    def _fernet(self):
        """Cipher for decisions.enc; the random key lives in the
        credential store (keyring, or the 0600 secrets.json fallback)."""
        key = self.creds.get("__decisions_key__")
        if not key:
            key = Fernet.generate_key().decode()
            self.creds.set("__decisions_key__", key)
        return Fernet(key.encode() if isinstance(key, str) else key)

    def _parse_decisions(self, text, decisions):
        for row in csv.DictReader(io.StringIO(text)):
            sender = _csv_unsafe(row.get("Email") or "").strip().lower()
            d = (row.get("Decision") or "").strip()
            if d == DELETED:
                d = DELETE  # keep intent: delete new mail too
            if sender and d in (KEEP, DELETE):
                decisions[sender] = d

    def _load_decisions(self):
        decisions = {}
        enc_file = self._account_dir() / "decisions.enc"
        csv_file = self._account_dir() / "sender_decisions.csv"
        if enc_file.exists():
            try:
                self._parse_decisions(
                    self._fernet().decrypt(enc_file.read_bytes())
                    .decode("utf-8"), decisions)
            except Exception:
                # lost key or corrupt file: start clean, never crash
                pass
            return decisions
        if csv_file.exists():
            try:
                self._parse_decisions(
                    csv_file.read_text(encoding="utf-8"), decisions)
            except Exception:
                pass
            # migrate the plaintext copy into the encrypted store
            self.decisions = decisions
            self._save_decisions()
        return decisions

    def _save_decisions(self):
        if not self._account:
            return
        try:
            adir = self._account_dir()
            adir.mkdir(parents=True, exist_ok=True)
            _chmod_private(adir, is_dir=True)
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["Email", "Decision"])
            for s, d in sorted(self.decisions.items()):
                if d != UNDECIDED:
                    w.writerow([s, d])
            secure_write(adir / "decisions.enc",
                         self._fernet().encrypt(
                             buf.getvalue().encode("utf-8")))
            # plaintext leftovers must not survive
            for name in ("sender_decisions.csv", "skiplist.csv"):
                stale = adir / name
                if stale.exists():
                    stale.unlink()
        except Exception as exc:
            self.statusBar().showMessage(
                tr("Could not save decisions: {exc}").format(exc=exc), 5000)

    def on_export(self):
        """Write a readable CSV copy of the decisions on demand."""
        rows = [(s, d) for s, d in sorted(self.decisions.items())
                if d != UNDECIDED]
        if not rows:
            QMessageBox.information(self, tr("Export decisions"),
                                    tr("Nothing to export."))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Export decisions"),
            str(Path.home() / "sender_decisions.csv"),
            tr("CSV files (*.csv)"))
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Email", "Decision"])
                for s, d in rows:
                    w.writerow([_csv_safe(s), d])
        except OSError as exc:
            QMessageBox.warning(self, tr("Export failed"), str(exc))

    def _import_csv(self, path):
        """Merge decisions from a CSV into self.decisions.

        Accepts the Export CSV format (Email,Decision) and skiplist-style
        exports (Email Address,Count,Skiplist -> Keep). Invalid senders
        and empty rows are skipped. Returns the number applied.
        """
        imported = 0
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if "Decision" in row:
                    sender = _csv_unsafe(row.get("Email") or "") \
                        .strip().lower()
                    d = (row.get("Decision") or "").strip()
                    if d == DELETED:
                        d = DELETE
                    ok = d in (KEEP, DELETE)
                else:  # skiplist-style export: kept senders
                    sender = _csv_unsafe(
                        row.get("Email Address") or "").strip().lower()
                    ok = (row.get("Skiplist") or "").strip() \
                        .lower().startswith("y")
                    d = KEEP
                if sender and ok \
                        and gmail_client.SENDER_RE.fullmatch(sender):
                    self.decisions[sender] = d
                    imported += 1
        return imported

    def on_import(self):
        """Load decisions from a CSV (e.g. made by Export CSV on another
        machine/install)."""
        if not self._account:
            QMessageBox.information(self, tr("Import decisions"),
                                    tr("Connect to your account first."))
            return
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Import decisions"), str(Path.home()),
            tr("CSV files (*.csv)"))
        if not path:
            return
        try:
            imported = self._import_csv(path)
        except Exception as exc:
            QMessageBox.warning(self, tr("Import failed"), str(exc))
            return
        self._populate_table()
        self._save_decisions()
        self.statusBar().showMessage(
            tr("Imported {n} decisions.").format(n=imported), 5000)

    # ---------- help popup ----------

    def _show_help(self):
        HelpDialog(self).exec()

    # ---------- language ----------

    def _set_language(self, code):
        global LANG
        if code == LANG:
            return
        LANG = code
        self._save_language(code)
        self._retranslate()
        self._update_flag_state()

    def _save_language(self, code):
        try:
            cfg = _read_config()
            cfg["language"] = code
            _write_config(cfg)
        except Exception:
            pass

    def _update_flag_state(self):
        for code, b in self.lang_btns.items():
            b.setProperty("active", "true" if code == LANG else "false")
            b.style().unpolish(b)
            b.style().polish(b)

    def _retranslate(self):
        self.subtitle_lbl.setText(
            tr("Connect, review your senders, and clean up your inbox"))
        self.warn_lbl.setText(
            tr("CAUTION: This app acts on your real mailbox. Prefer 'Move to "
               "Trash' - it stays recoverable for ~30 days. Permanent delete "
               "cannot be undone."))
        self.login_box.setTitle(tr("Gmail login"))
        self.pass_edit.setPlaceholderText(tr("App password"))
        self.get_pw_btn.setText(tr(" Get app password"))
        self.get_pw_btn.setToolTip(
            tr("Open myaccount.google.com/apppasswords in your browser"))
        self.save_cred.setText(tr("Save credentials"))
        self.save_cred.setToolTip(
            tr("Stored in the OS credential manager"))
        self.connect_btn.setText(tr(" Connect"))
        self.senders_box.setTitle(tr("Inbox senders"))
        self.fetch_btn.setText(tr(" Fetch senders"))
        self.cancel_btn.setText(tr(" Cancel"))
        self.f_sender.setPlaceholderText(tr("Filter sender..."))
        self.f_emails.setPlaceholderText(tr("Emails: >100, <=10, =5"))
        self.f_att.setPlaceholderText(tr("Attach.: >0"))
        prev = self.f_decision.currentData()
        self.f_decision.blockSignals(True)
        self.f_decision.clear()
        self.f_decision.addItem(tr("All"), None)
        for d in (UNDECIDED, KEEP, DELETE, DELETED):
            self.f_decision.addItem(decision_label(d), d)
        idx = self.f_decision.findData(prev)
        self.f_decision.setCurrentIndex(idx if idx >= 0 else 0)
        self.f_decision.blockSignals(False)
        self.table.setHorizontalHeaderLabels(
            [tr("Sender"), tr("Emails"), tr("Emails with Attachment"),
             tr("Decision")])
        self.table.horizontalHeaderItem(2).setToolTip(
            tr("Estimated from Content-Type (multipart/mixed or "
               "non-text part)"))
        self.keep_btn.setText(tr(" Mark Keep"))
        self.delete_mark_btn.setText(tr(" Mark Delete"))
        self.undecided_btn.setText(tr(" Mark Undecided"))
        self.export_btn.setText(tr(" Export CSV"))
        self.export_btn.setToolTip(
            tr("Write a readable CSV copy of your decisions"))
        self.import_btn.setText(tr(" Import CSV"))
        self.import_btn.setToolTip(
            tr("Load decisions from a CSV file"))
        self._update_cred_hint()
        self.exec_box.setTitle(tr("Deletion"))
        self.mode_trash.setText(tr("Move to Trash (recoverable ~30 days)"))
        self.mode_perm.setText(tr("Permanently delete (cannot be undone)"))
        self.run_delete_btn.setText(tr(" Delete marked senders"))
        self.help_btn.setToolTip(tr("Help - what does this app do?"))
        if self.counts and self._last_total:
            self.status_lbl.setText(
                tr("{total:,} emails from {senders:,} unique senders")
                .format(total=self._last_total, senders=len(self.counts)))
        elif self.connected:
            self.status_lbl.setText(
                tr("Connected (trash folder: {trash})")
                .format(trash=self.trash_mailbox))
        else:
            self.status_lbl.setText(tr("Not connected"))
        for row in range(self.table.rowCount()):
            it = self.table.item(row, 3)
            if it is not None:
                it.setText(decision_label(it.data(Qt.UserRole)))
        self._update_summary()

    # ---------- worker plumbing ----------

    def _start_worker(self, job, on_done):
        self._thread = QThread(self)
        self._worker = Worker(job)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_thread_finished)
        self._on_done_cb = on_done
        self._set_busy(True)
        self._thread.start()

    def _on_thread_finished(self):
        # Drop Python refs before the C++ objects are deleted, so
        # closeEvent/on_cancel never touch a dead object.
        self._worker = None
        self._thread = None

    def _on_progress(self, done, total):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        if self._del_dlg is not None:
            self._del_dlg.update_progress(done, total)

    def _on_worker_done(self, result):
        self._set_busy(False)
        cb, self._on_done_cb = self._on_done_cb, None
        if cb:
            cb(result)

    def _on_worker_failed(self, msg):
        self._set_busy(False)
        if self._del_dlg is not None:
            self._del_dlg.close()
            self._del_dlg = None
        if msg == CANCELLED:
            self.statusBar().showMessage(tr("Operation cancelled"), 5000)
        else:
            QMessageBox.critical(self, tr("Operation failed"), msg)

    def _set_busy(self, busy):
        self.cancel_btn.setEnabled(busy)
        for w in (self.connect_btn, self.fetch_btn if self.connected else None,
                  self.run_delete_btn if self.counts else None,
                  self.keep_btn, self.delete_mark_btn, self.undecided_btn):
            if w is not None:
                w.setEnabled(not busy)

    # ---------- actions ----------

    def on_get_password(self):
        QDesktopServices.openUrl(
            QUrl("https://myaccount.google.com/apppasswords"))

    def on_connect(self):
        email = self.email_edit.text().strip()
        password = self.pass_edit.text().strip()
        if not email or not password:
            QMessageBox.warning(
                self, tr("Missing credentials"),
                tr("Enter your Gmail address and app password."))
            return
        self.connect_btn.setEnabled(False)
        self.status_lbl.setText(tr("Connecting..."))
        QApplication.processEvents()
        try:
            imap = gmail_client.connect(email, password)
            try:
                trash = gmail_client.find_trash_mailbox(imap)
            finally:
                gmail_client.disconnect(imap)
        except Exception as exc:
            self.status_lbl.setText(tr("Connection failed"))
            self.status_lbl.setStyleSheet("color: #c5221f; font-weight: bold;")
            box = QMessageBox(
                QMessageBox.Critical, tr("Login failed"),
                f"{exc}" + tr(
                    "\n\nGmail requires an app password "
                    "(myaccount.google.com/apppasswords), not your normal "
                    "password."),
                QMessageBox.Ok, self)
            box.setTextFormat(Qt.PlainText)
            box.exec()
            self.connect_btn.setEnabled(True)
            return
        self.email, self.password, self.trash_mailbox = email, password, trash
        self._load_account(email)
        self.connected = True
        self.status_lbl.setText(
            tr("Connected (trash folder: {trash})").format(trash=trash))
        self.status_lbl.setStyleSheet("color: #137333; font-weight: bold;")
        self.connect_btn.setEnabled(True)
        self.fetch_btn.setEnabled(True)
        self._persist_login(email, password)

    def _account_changed(self):
        """True if the login fields no longer match the connected account."""
        return (not self.connected or self._account is None
                or self.email_edit.text().strip().lower() != self._account)

    def _require_same_account(self):
        if self._account_changed():
            QMessageBox.information(
                self, tr("Not connected"),
                tr("The email address changed since you connected. Press "
                   "Connect again to use the new account."))
            return False
        return True

    def on_fetch(self):
        if not self._require_same_account():
            return
        email, password = self.email, self.password

        def job(progress_cb, cancel):
            imap = gmail_client.connect(email, password)
            try:
                return gmail_client.fetch_sender_counts(
                    imap, progress_cb=progress_cb, cancel=cancel)
            finally:
                gmail_client.disconnect(imap)

        self._start_worker(job, self._on_fetched)

    def _on_fetched(self, result):
        counts, att_counts, total = result
        self.counts = counts
        self.att_counts = att_counts
        self._last_total = total
        self.status_lbl.setText(
            tr("{total:,} emails from {senders:,} unique senders")
            .format(total=total, senders=len(counts)))
        self._populate_table()
        self.run_delete_btn.setEnabled(True)

    def on_cancel(self):
        if self._worker:
            self._worker.cancel()
        self.cancel_btn.setEnabled(False)
        if self._del_dlg is not None:
            self._del_dlg.cancel_btn.setEnabled(False)

    def on_delete(self):
        if not self._require_same_account():
            return
        to_delete = [s for s, d in self.decisions.items()
                     if d == DELETE and self.counts.get(s, 0) > 0]
        if not to_delete:
            QMessageBox.information(
                self, tr("Nothing to delete"),
                tr("No senders are marked Delete (with mail in Inbox)."))
            return
        total_emails = sum(self.counts[s] for s in to_delete)
        undecided = sum(1 for s in self.counts
                        if self.decisions.get(s, UNDECIDED) == UNDECIDED)
        permanent = self.mode_perm.isChecked()
        if permanent:
            msg = tr(
                "{s:,} senders / {e:,} emails will be PERMANENTLY DELETED "
                "(this cannot be undone)."
            ).format(s=len(to_delete), e=total_emails)
        else:
            msg = tr(
                "{s:,} senders / {e:,} emails will be moved to the Gmail "
                "Trash."
            ).format(s=len(to_delete), e=total_emails)
        msg += "\n\n" + tr(
            "Undecided senders: {u:,} - they will be kept.").format(
            u=undecided)
        if permanent:
            msg += "\n\n" + tr(
                "WARNING: permanent deletion is IRREVERSIBLE.")
        answer = QMessageBox.warning(
            self, tr("Confirm deletion"), msg,
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Yes:
            return

        email, password, trash = self.email, self.password, self.trash_mailbox

        def job(progress_cb, cancel):
            imap = gmail_client.connect(email, password)
            try:
                return gmail_client.delete_senders(
                    imap, to_delete, permanent=permanent,
                    trash_mailbox=trash,
                    progress_cb=progress_cb, cancel=cancel)
            finally:
                gmail_client.disconnect(imap)

        self._del_dlg = ProgressDialog(not permanent, self)
        self._del_dlg.cancel_btn.clicked.connect(self.on_cancel)
        self._del_dlg.show()
        self._start_worker(job, self._on_deleted)

    def _on_deleted(self, result):
        if self._del_dlg is not None:
            self._del_dlg.close()
            self._del_dlg = None
        done, failed = result
        for sender, moved in done.items():
            if sender in self.counts:
                self.counts[sender] = max(0, self.counts[sender] - moved)
            if self.counts.get(sender, 0) == 0:
                self.decisions[sender] = DELETED
        self._populate_table()
        self._save_decisions()
        if failed:
            try:
                # counts only - never write sender addresses to disk
                with ERROR_LOG.open("a", encoding="utf-8") as f:
                    f.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] "
                            f"failed senders: {len(failed)}\n")
                _chmod_private(ERROR_LOG)
            except OSError:
                pass
        QMessageBox.information(
            self, tr("Deletion complete"),
            tr("Deleted {n:,} emails from {m:,} senders.").format(
                n=sum(done.values()), m=len(done)) + "\n"
            + tr("Failed senders: {x:,}").format(x=len(failed))
            + (tr(" (see {name})").format(name=ERROR_LOG.name)
               if failed else ""))

    # ---------- table ----------

    def _populate_table(self):
        # Fresh data -> fresh view: reset the filter row first, otherwise a
        # re-fetch can hide every row behind a stale filter (looks empty).
        for w_ in (self.f_sender, self.f_emails, self.f_att):
            w_.blockSignals(True)
            w_.clear()
            w_.blockSignals(False)
        self.f_decision.blockSignals(True)
        self.f_decision.setCurrentIndex(0)
        self.f_decision.blockSignals(False)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        rows = sorted(self.counts.items(), key=lambda kv: kv[1], reverse=True)
        self.table.setRowCount(len(rows))
        for row, (sender, count) in enumerate(rows):
            s_item = QTableWidgetItem(sender)
            c_item = QTableWidgetItem()
            c_item.setData(Qt.DisplayRole, count)
            a_item = QTableWidgetItem()
            a_item.setData(Qt.DisplayRole, self.att_counts.get(sender, 0))
            decision = self.decisions.get(sender, UNDECIDED)
            if decision == DELETED and count > 0:
                decision = DELETE
                self.decisions[sender] = DELETE
            d_item = QTableWidgetItem(decision_label(decision))
            d_item.setData(Qt.UserRole, decision)
            self.table.setItem(row, 0, s_item)
            self.table.setItem(row, 1, c_item)
            self.table.setItem(row, 2, a_item)
            self.table.setItem(row, 3, d_item)
            self._apply_row_style(row, decision)
        self.table.setSortingEnabled(True)
        self.table.sortItems(1, Qt.DescendingOrder)
        self._apply_filters()
        self._update_summary()

    def _apply_row_style(self, row, decision):
        """Tint the whole row by its decision state."""
        bg, fg = DECISION_STYLES[decision]
        brush = QBrush(QColor(bg))
        for col in range(self.table.columnCount()):
            self.table.item(row, col).setBackground(brush)
        self.table.item(row, 3).setForeground(QBrush(QColor(fg)))

    def _mark_selected(self, decision):
        rows = {idx.row() for idx in self.table.selectionModel().selectedRows()}
        if not rows:
            self.statusBar().showMessage(
                tr("Select one or more rows first"), 3000)
            return
        for row in rows:
            sender = self.table.item(row, 0).text()
            self.decisions[sender] = decision
            self.table.item(row, 3).setText(decision_label(decision))
            self.table.item(row, 3).setData(Qt.UserRole, decision)
            self._apply_row_style(row, decision)
        self._save_decisions()
        self._update_summary()

    def _on_right_click(self, pos):
        """Right-click cycles a row's decision: Keep -> Delete -> Undecided.

        If the row is part of the current selection, the same next state
        is applied to every selected row. Marking any sender for deletion
        asks for confirmation first.
        """
        row = self.table.indexAt(pos).row()
        if row < 0:
            return
        selected = {i.row() for i in
                    self.table.selectionModel().selectedRows()}
        targets = selected if row in selected else {row}
        clicked_sender = self.table.item(row, 0).text()
        current = self.decisions.get(clicked_sender, UNDECIDED)
        new = {KEEP: DELETE, DELETE: UNDECIDED,
               UNDECIDED: KEEP, DELETED: UNDECIDED}.get(current, KEEP)

        if new == DELETE:
            affected = [r for r in targets
                        if self.decisions.get(
                            self.table.item(r, 0).text(),
                            UNDECIDED) != DELETE]
            if affected:
                answer = QMessageBox.warning(
                    self, tr("Confirm marking for deletion"),
                    tr("{n} sender(s) will be marked Delete.\n\n"
                       "Are you sure?").format(n=len(affected)),
                    QMessageBox.Yes | QMessageBox.Cancel,
                    QMessageBox.Cancel)
                if answer != QMessageBox.Yes:
                    return

        for r in targets:
            sender = self.table.item(r, 0).text()
            self.decisions[sender] = new
            self.table.item(r, 3).setText(decision_label(new))
            self.table.item(r, 3).setData(Qt.UserRole, new)
            self._apply_row_style(r, new)
        self._save_decisions()
        self._update_summary()

    @staticmethod
    def _num_pred(expr):
        """Parse a numeric filter like '>100', '<=10', '=5' (plain N means >=N).

        Returns a predicate(value) or None if the field is empty/invalid.
        """
        m = re.match(r"^\s*([<>]=?|=)?\s*(\d+)\s*$", expr)
        if not m:
            return None
        op, n = m.group(1) or ">=", int(m.group(2))
        return {
            ">": lambda v: v > n,
            "<": lambda v: v < n,
            ">=": lambda v: v >= n,
            "<=": lambda v: v <= n,
            "=": lambda v: v == n,
        }[op]

    def _apply_filters(self):
        needle = self.f_sender.text().strip().lower()
        email_pred = self._num_pred(self.f_emails.text())
        att_pred = self._num_pred(self.f_att.text())
        want_d = self.f_decision.currentData()
        for row in range(self.table.rowCount()):
            sender = self.table.item(row, 0).text().lower()
            emails = self.table.item(row, 1).data(Qt.DisplayRole)
            attach = self.table.item(row, 2).data(Qt.DisplayRole)
            decision = self.table.item(row, 3).data(Qt.UserRole)
            hide = (
                (needle and needle not in sender)
                or (email_pred and not email_pred(emails))
                or (att_pred and not att_pred(attach))
                or (want_d is not None and decision != want_d)
            )
            self.table.setRowHidden(row, bool(hide))

    def _update_summary(self):
        buckets = {KEEP: [0, 0], DELETE: [0, 0], UNDECIDED: [0, 0]}
        for sender, count in self.counts.items():
            d = self.decisions.get(sender, UNDECIDED)
            if d == DELETED:
                d = DELETE if count > 0 else DELETED
            if d in buckets:
                buckets[d][0] += 1
                buckets[d][1] += count
        k, d, u = buckets[KEEP], buckets[DELETE], buckets[UNDECIDED]

        def chip(key, bucket):
            _, fg = DECISION_STYLES[key]
            return (f'<b style="color:{fg}">{decision_label(key)}:</b> '
                    + tr("{n:,} senders ({e:,} emails)").format(
                        n=bucket[0], e=bucket[1]))

        self.summary_lbl.setText(
            " &nbsp;|&nbsp; ".join(
                (chip(KEEP, k), chip(DELETE, d), chip(UNDECIDED, u))))

    def closeEvent(self, event):
        try:
            if self._thread is not None and self._thread.isRunning():
                if self._worker:
                    self._worker.cancel()
                self._thread.wait(3000)
        except RuntimeError:
            pass  # underlying Qt object already gone
        event.accept()


def _qss_assets():
    """Render the check/radio glyphs referenced by the stylesheet to PNGs.

    Written to the temp dir; regenerated every run so stale files and
    platform differences can't matter. Returns {kind: path-as-posix}.
    """
    out = Path(tempfile.gettempdir()) / "ginbox_cleaner_qss"
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    rings = {"radio": PRIMARY, "radio_off": "#80868b",
             "radio_hover": PRIMARY, "radio_dis": "#dadce0"}
    f = out / "check.png"
    pm = QPixmap(30, 30)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(Qt.white, 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPolyline(QPolygonF([QPointF(3.8, 7.8), QPointF(6.4, 10.8),
                              QPointF(11.2, 3.8)]))
    p.end()
    pm.save(str(f), "PNG")
    paths["check"] = f.as_posix()
    # QSS 'image:' is drawn at natural size for radio indicators, so the
    # PNG must be exactly the 18px indicator size. Painted on a 36px
    # canvas and downscaled for smooth edges.
    for name, color in rings.items():
        f = out / f"{name}.png"
        img = QImage(36, 36, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(2, 2)
        p.setPen(QPen(QColor(color), 2.4))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(2.4, 2.4, 13.2, 13.2))
        if name == "radio":
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(PRIMARY))
            p.drawEllipse(QRectF(5.7, 5.7, 6.6, 6.6))
        p.end()
        img.scaled(18, 18, Qt.IgnoreAspectRatio,
                   Qt.SmoothTransformation).save(str(f), "PNG")
        paths[name] = f.as_posix()
    return paths


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    _a = _qss_assets()
    app.setStyleSheet(
        STYLESHEET.replace("%%CHECK_IMG%%", _a["check"])
                  .replace("%%RADIO_IMG%%", _a["radio"])
                  .replace("%%RADIO_OFF%%", _a["radio_off"])
                  .replace("%%RADIO_HOVER%%", _a["radio_hover"])
                  .replace("%%RADIO_DIS%%", _a["radio_dis"]))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
