# GInbox Cleaner

Egy asztali alkalmazás, amely kilistázza a Gmail Beérkezett üzeneteid
feladóit üzenetszám szerint sorba rendezve, lehetővé teszi, hogy
mindegyiket **Megtart / Töröl / Eldöntetlen** jelöléssel lásd
el, majd a nem kívántakat tömegesen eltávolítja — áthelyezi őket a
Kukába vagy véglegesen törli, ahogy szeretnéd.

Angol felület / Magyar felület között az alkalmazásban lévő zászlógombokkal válthatsz.

---

## ⚠️ Ezt olvasd el először — figyelmeztetések

- **Az alkalmazás a valódi postafiókodon dolgozik.** A törlés nem
  visszafordítható, kivéve ha a *Áthelyezés a Kukába* módot használod.
  Inkább válaszd a Kukát — az oda helyezett levelek a Gmailben
  kb. 30 napig visszaállíthatók. **A végleges törlés nem vonható
  vissza.**
- Az alkalmazás kizárólag a **Beérkezett üzenetekhez** nyúl: a
  Küldött, a Spam, a Piszkozatok és más mappák tartalmát sem nem
  keresi át, sem nem módosítja.
- Csak az általad kifejezetten **Töröl**-ként megjelölt feladók
  érintettek, és csak *pontos* címegyezés alapján — például az `ann@x.com`
  sosem törli a `joann@x.com` címet.
- **Gmail alkalmazásjelszóra** van szükséged (nem a szokásos
  jelszavadra) — lásd a beállítást lent. Ehhez kétlépéses
  ellenőrzésnek kell bekapcsolva lennie a fiókodon.
- A binárisok önállóan futtathatók (~56–72 MB — a Python és a Qt
  mellékelve van).
- **A Windows-kiadások jelenleg önaláírtak** — a SmartScreen figyelmeztethet.
  Kattints a *További információ → Futtatás mindenképpen* gombra, vagy
  előbb ellenőrizd a SHA256-ellenőrzőösszeget.
- Nem áll kapcsolatban a Google-lel. A Gmail a Google LLC védjegye.

## Letöltések

| Mit szeretnél | Fájl | Hol |
|---|---|---|
| Linux Mint / Ubuntu / Debian | `ginbox-cleaner_*_amd64.deb` | [Linkek](https://github.com/srancsik/GInbox-cleaner/releases) |
| Windows (telepítés nélkül) | `ginbox-cleaner_*_portable_windows.zip` | [Linkek](https://github.com/srancsik/GInbox-cleaner/releases) |
| Linux (telepítés nélkül) | `ginbox-cleaner_*_portable_linux.zip` | [Linkek](https://github.com/srancsik/GInbox-cleaner/releases) |
| Python-forrás futtatása | `dist-source/` mappa ebben a repóban 

Futtatás előtt mindig ellenőrizd a `SHA256SUMS` fájlt:
`sha256sum -c SHA256SUMS` (Linux/macOS) vagy
`certutil -hashfile <fájl> SHA256` (Windows).

## Gmail-beállítás (egyszeri, kötelező)

1. Kapcsold be a **kétlépéses ellenőrzést** a Google-fiókodon.
2. Hozz létre egy alkalmazásjelszót itt: <https://myaccount.google.com/apppasswords>. A formátum: 16 karakter hosszú, például `abcd efgh ijkl mnop`.
3. Győződj meg róla, hogy az **IMAP engedélyezve van**: Gmail →
   Beállítások → Összes beállítás megtekintése → Továbbítás és POP/IMAP.

## Futtatás

**Linux (.deb):**

```bash
sudo dpkg -i ginbox-cleaner_*_amd64.deb
```

Ezután indítsd el a "GInbox Cleaner"-t a menüből, vagy futtasd a
`ginbox-cleaner` parancsot.

**Hordozható (Windows/Linux):** csomagold ki bárhova, futtasd a
`ginbox-cleaner.exe`-t vagy a `./ginbox-cleaner`-t. A mellékelt
`portable.flag` miatt az alkalmazás az összes adatát a saját mappájában
tartja — töröld, ha inkább a felhasználónkénti helyet használnád.

**Forrásból:**

```bash
cd dist-source
pip install -r requirements.txt
python ginbox-cleaner.py
```

## Hogyan működik

1. **Csatlakozás** — add meg a Gmail-címedet és az alkalmazásjelszót.
2. **Feladók lekérése** — az alkalmazás csak a Beérkezett üzenetek
   *fejléceit* olvassa (feladó és darabszám; a levéltörzseket sosem
   tölti le), majd sorba rendezi a feladókat.
3. **Jelölés** — gombokkal vagy jobb kattintással: Megtart →
   Töröl → Eldöntetlen.
4. **Megjelölt feladók törlése** — válassz Kuka vagy végleges törlés
   között; egy előrehaladási ablak mutatja az élő állapotot, és
   lehetővé teszi a megszakítást.

| | Áthelyezés a Kukába | Végleges törlés |
|---|---|---|
| Visszaállítható | Igen, ~30 nap | **Nem** |
| Mi történik | Átmásolja a Kukába, eltávolítja a Beérkezett üzenetekből | A Beérkezett üzenetek és a Kuka példányai is véglegesen törlődnek — semmi nem marad |

## Az adataid és az adatvédelem

- A döntések **Gmail-fiókonként** mentődnek, és **titkosítva tárolódnak**
  (`decisions.enc`); a kulcs az operációs rendszer hitelesítőadat-kezelőjében
  él (Windows Hitelesítőadat-kezelő / GNOME Keyring / KWallet).
- Az alkalmazásjelszó ugyanabba az OS-hitelesítőadat-tárba kerül —
  sosem egy olvasható konfigurációs fájlba. Keyring nélküli
  rendszereken egy csak a tulajdonos számára olvasható
  `secrets.json`-ra válik vissza (és ezt jelzi is).
- Adatmappa: `~/.local/share/ginbox-cleaner` (Linux),
  `%APPDATA%\ginbox-cleaner` (Windows) — `0700` jogosultsággal
  létrehozva, a fájlok `0600`-ra állítva.
- A **CSV Exportálás/Importálás** lehetővé teszi a döntések
  biztonsági mentését vagy átvitelét gépek és friss telepítések
  között.

## Licenc

MIT — lásd [LICENSE](LICENSE). Nem áll kapcsolatban a Google-lel.

