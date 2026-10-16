# Stickle

**Sticky notes for your desktop — the same notes on every computer, Windows, Linux or macOS, through your own cloud.**

[한국어](README.ko.md) · [Website](https://stickle.linkro.co) ·
[Microsoft Store](https://apps.microsoft.com/detail/9MZ6BHN3JKFR)

> **Status: preview.** The 0.4 preview is out: notes on one computer, for Windows and Linux,
> with categories and marks, now shown as icons, notes that line up, find on the desktop, and
> easier lists and code. Syncing between computers is not there yet — see
> [Try the preview](#try-the-preview).
> To hear about new versions, click **Watch → Custom → Releases** at the top of this page.
> A star tells us you are interested, but does not notify you.

## Why Stickle

- **No account, no Stickle server.** Your notes sync through a folder you already sync for yourself —
  Dropbox, OneDrive, Google Drive, iCloud Drive, Syncthing, Nextcloud and the like.
  Your notes never pass through us, and we could not read them if we tried.
- **Private by design.** Notes are encrypted on your computer, with optional end-to-end
  encryption for sync. There is no analytics or telemetry of any kind. The only other
  connection the app makes is a daily update check that you can turn off.
- **Works offline.** Every feature works without a network, and notes sync by themselves
  once you are back online.
- **Notes are plain Markdown.** Type `# heading`, `**bold**`, `==highlight==` or `- [ ]` and
  the note shows it formatted, with boxes you can tick; double-click to edit the text. Your
  notes stay ordinary text you can read anywhere — no special format to be locked into.
  Not into Markdown? Just write.
- **Notes never disappear without you knowing.** When two computers change the same note, both versions
  are kept. Deleted notes stay in the trash for a year, and a recovery key opens your notes
  if you forget your password.
- **Free, with every feature.** No ads, no locked features, no paid tier. Stickle is free
  software (GPL) and is funded by donations only.
- **Stays out of your way.** No reminders, banners or pop-ups asking for anything.
- **Just works.** Nothing else to install: every library and font the app needs comes with
  it. Input methods work, tested with Korean on Windows and with fcitx5 and IBus on Linux.

## Roadmap

| Version | What you get |
| --- | --- |
| 0.1 *(preview out)* | Everyday sticky notes on a single computer: colours, formatting, notes that remember where they were, always on top, tray icon, start with your computer |
| 0.2 *(preview out)* | Search, a trash that keeps notes for a year, shortcuts from anywhere, see-through and locked notes, full keyboard use, portable mode; on the [Microsoft Store](https://apps.microsoft.com/detail/9MZ6BHN3JKFR) |
| 0.3 *(preview out)* | Categories and marks, one category on the desktop, find on the desktop, notes that line up (Shift moves them together), easier lists and code blocks, the shortcuts at a glance, a new icon |
| 0.4 *(preview out)* | Category and mark icons, About Stickle and ways to support it, a faded tray icon while notes are hidden |
| 0.5 *(next)* | Automatic backups you can restore, export of every note (Markdown and JSON), bringing notes over from Windows Sticky Notes and other sticky-note apps, a daily update check you can turn off, sending feedback from the app |
| On the way to 1.0 | Reminders, including times written in a note; secret notes behind a password of their own; then syncing through a folder you already sync, first as an option to try, followed by end-to-end encryption and locking out a lost computer |
| 1.0 | Sync between your computers, ready for everyday use; macOS; packages for Linux (Flathub, AppImage, deb) and macOS (Homebrew) |
| Later | WebDAV and S3 storage, what-you-see-is-what-you-get editing, tags, shared notes and a browser extension, a read-only viewer for phones |

There are no dates, and plans and their order may change.

## Try the preview

Previews are under [Releases](https://github.com/linkro-app/Stickle/releases), marked
*pre-release*. macOS builds are on the way.

- **Windows** (10 or 11): get Stickle from the
  [Microsoft Store](https://apps.microsoft.com/detail/9MZ6BHN3JKFR). Microsoft signs it, so
  Windows installs it without a warning and keeps it up to date. The Store version and the zip
  below keep notes in the same folder (`%APPDATA%\Stickle`), so moving from one to the other
  keeps your notes.

  Or download the zip for your computer (`x64`, or `arm64` for ARM
  laptops), unzip it anywhere and run `stickle.exe`. The zip is not signed yet, so Windows
  may say it protected your PC: choose **More info → Run anyway**. When **Start Stickle
  when I log in** was on, Microsoft Defender sometimes blocked 0.3 as
  `Trojan:Win32/Bearfoos.A!ml`: its machine learning mistook an unsigned program that starts
  itself at login for a trojan. 0.4 is not blocked by an up-to-date Defender, but this can
  change from build to build, so until signed builds are out that option starts off on
  Windows. If
  Defender blocks Stickle for you, please
  [open an issue](https://github.com/linkro-app/Stickle/issues/new) so we know.
- **Linux**: download the AppImage, make it executable (`chmod +x Stickle-x86_64.AppImage`,
  or in its file properties) and run it. Stickle offers to add itself to your application list.
  With automatic login, the desktop's keyring stays locked, so when Stickle starts with you it
  asks for your login password to unlock it, as any app using the keyring does. To avoid that,
  turn automatic login off, or give the login keyring an empty password.
- The first start asks a few questions and shows a **recovery key**. Keep it somewhere safe,
  away from this computer: it opens your notes if the key stored on this computer is ever lost.
- Downloads can be checked against `SHA256SUMS.txt` in the same release.

New in 0.4:

- **Category icons**: give each category an icon when you make it, or change it later. A
  note's title bar shows its category's icon instead of the name, so a long name no longer
  fills the bar; a note without a category shows Stickle's own icon, and a strip of tape
  across its corner means the note stays on top. Marks use the same set of icons, and the
  Stickle window shows them beside their names.
- **About Stickle**, from the Stickle window or the tray: the version, links and the license.
- **Support Stickle…** in the tray and in About Stickle. The Microsoft Store copy opens the
  Store's own purchase window; the other copies open the [website](https://stickle.linkro.co).
  Every feature stays free either way, and the app keeps nothing about it.
- While every note is hidden for a moment (`Ctrl+Alt+H`), the **tray icon fades**, so an empty
  desktop is not taken for lost notes.
- `Esc` closes the Stickle window. Over a note the pointer is an arrow, and a hand over boxes
  and links.
- Notes made with the 0.3, 0.2 or 0.1.2 preview open as they were: just replace the program. A
  copy of the notes from before is kept in the `backups` folder where the notes are
  (`%APPDATA%\Stickle\backups` on Windows, `~/.local/share/stickle/backups` on Linux).

0.3 brought categories and marks, one category on the desktop, find on the desktop, notes that
line up, easier lists and code blocks, and the shortcuts at a glance (`F1` or `Ctrl+/`).
0.2 brought search, a trash, shortcuts from anywhere (`Ctrl+Alt+N`, `Ctrl+Alt+S`,
`Ctrl+Alt+H`), see-through and locked notes, full keyboard use and the portable mode below.

### Portable use

To carry Stickle and its notes on a USB stick, on Windows download the `-portable.zip`,
unzip it anywhere and run `stickle.exe`. It is the same program with a `stickle-data`
folder next to `stickle.exe`. Any other copy of Stickle becomes portable the same way:
create an empty folder named `stickle-data` in the folder that holds `stickle.exe` (on
Linux, the folder that holds the AppImage).

Stickle then keeps its settings and notes only in that folder: it always asks for a
password, since it does not use this computer's password store, and it does not start at
login or add itself to the application list. On Wayland it has no shortcuts from anywhere,
which the desktop would have to keep.

What a preview is, and is not:

- Notes are kept on this computer only, encrypted. There is no syncing or backup yet.
- Later versions are meant to open notes made with a preview, but the way notes are stored
  may still change, and a preview can have bugs. Keep anything important elsewhere too.

## Help test

Reports from real computers are the most useful help right now, especially from macOS,
KDE, less common Linux distributions, input methods, and multi-monitor or high-DPI setups.
[Open an issue](https://github.com/linkro-app/Stickle/issues/new) and include:

- your operating system and version, and on Linux your desktop (GNOME, KDE, …) and input method;
- what you did, what you expected, and what happened instead;
- if Stickle shows a problem screen, the text from its **Copy details** button;
- if possible, the log file. It contains no personal information such as note text or
  folder paths:

| System | Log file |
| --- | --- |
| Windows | `%APPDATA%\Stickle\logs\stickle.log` |
| Linux | `~/.local/share/stickle/logs/stickle.log` |
| macOS | `~/Library/Application Support/Stickle/logs/stickle.log` |

## Run from source

With [uv](https://docs.astral.sh/uv/) installed:

```
uv sync
uv run python -m stickle
```

Tests: `uv run pytest`.

## Support Stickle

Every feature is free, whether or not you support Stickle. If it is useful to you, you can
support its development through [Ko-fi](https://ko-fi.com/linkro), by card or PayPal, or
[GitHub Sponsors](https://github.com/sponsors/tkitbiz).
Translating, reporting bugs and telling others help too.

## Privacy

Stickle collects nothing and has no server: see the [privacy policy](PRIVACY.md).

## License

[GPL-3.0-or-later](LICENSE).

The icons for categories and marks come from [Lucide](https://lucide.dev),
under the [ISC License](src/stickle/app/icons/lucide/LICENSE).
