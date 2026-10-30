# Stickle

**Sticky notes for your desktop — the same notes on every computer, Windows, Linux or macOS, through your own cloud.**

[한국어](README.ko.md) · [Website](https://stickle.linkro.co) ·
[Microsoft Store](https://apps.microsoft.com/detail/9MZ6BHN3JKFR)

> **Status: preview.** The 0.6 preview is out: notes on one computer, for Windows and Linux,
> now with reminders (on the lunar and other calendars too), secret notes behind a password of
> their own, colours of your own, and notes that step aside for full-screen slide shows, videos
> and games. Syncing between computers is not there yet — see [Try the preview](#try-the-preview).
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
- **Stays out of your way.** No banners or pop-ups asking for anything; the only reminders
  are the ones you set yourself.
- **Just works.** Nothing else to install: every library and font the app needs comes with
  it. Input methods work, tested with Korean on Windows and with fcitx5 and IBus on Linux.

## Roadmap

| Version | What you get |
| --- | --- |
| 0.1 *(preview out)* | Everyday sticky notes on a single computer: colours, formatting, notes that remember where they were, always on top, tray icon, start with your computer |
| 0.2 *(preview out)* | Search, a trash that keeps notes for a year, shortcuts from anywhere, see-through and locked notes, full keyboard use, portable mode; on the [Microsoft Store](https://apps.microsoft.com/detail/9MZ6BHN3JKFR) |
| 0.3 *(preview out)* | Categories and marks, one category on the desktop, find on the desktop, notes that line up (Shift moves them together), easier lists and code blocks, the shortcuts at a glance, a new icon |
| 0.4 *(preview out)* | Category and mark icons, About Stickle and ways to support it, a faded tray icon while notes are hidden |
| 0.5 *(preview out)* | Daily backups you can go back to, export of every note (Markdown and JSON), bringing notes over from Microsoft Sticky Notes and other sticky-note apps, pasting with formatting, notes that line up when resized too, a daily update check you can turn off, reporting a problem or idea from the app |
| 0.6 *(preview out)* | Reminders, once or repeating or written in a line of a note, on the lunar and other calendars too; secret notes behind a password of their own; a Settings window, with a spare password beside the recovery key; colours of your own and random colours; notes that step aside for full-screen programs; bringing a Stickle export back in; the Stickle window renamed Stickle Manager |
| On the way to 1.0 | Syncing through a folder you already sync, first as an option to try, followed by end-to-end encryption and locking out a lost computer |
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
  itself at login for a trojan. 0.4 to 0.6 are not blocked by an up-to-date Defender, but
  this can change from build to build, so until signed builds are out that option starts off
  on Windows (turn it on in **Settings → General**). If Defender blocks Stickle for you, please
  [open an issue](https://github.com/linkro-app/Stickle/issues/new) so we know.
- **Linux**: download the AppImage, make it executable (`chmod +x Stickle-x86_64.AppImage`,
  or in its file properties) and run it. Stickle offers to add itself to your application list.
  With automatic login, the desktop's keyring stays locked, so when Stickle starts with you it
  asks for your login password to unlock it, as any app using the keyring does. To avoid that,
  turn automatic login off, or give the login keyring an empty password.
- The first start asks a few questions and shows a **recovery key**. Keep it somewhere safe,
  away from this computer: it opens your notes if the key stored on this computer is ever lost.
- Downloads can be checked against `SHA256SUMS.txt` in the same release.

New in 0.6:

- **Stickle Manager** is the new name of the window with the list of notes. Settings have a
  window of their own: **Settings…** in Stickle Manager (`Ctrl+,`) and in the tray menu, with
  General, Notes, Reminders, Protection, Shortcuts and Connections.
- **Reminders**: `Ctrl+R` in a note (or **Reminder…** in its menu) sets the note's reminder:
  in an hour, this evening, tomorrow morning, next Monday or a day and time you choose, once or
  every day, weekday, week, month or year. When it rings, the note comes forward with a bar on
  top: **Done**, or **Remind me again** to put it off. While editing, `Ctrl+Shift+R` writes a
  reminder into the line (`⏰2026-11-02 09:00`), so one note can hold several.
- **Reminders ring while Stickle is running.** Those that came while it was off, or while the
  computer slept, are listed as **missed reminders** when it starts, each saying how long ago.
- **Other calendars**: a reminder can follow the (Korean) lunar calendar, the Chinese lunar,
  Hebrew, Islamic (Umm al-Qura), Persian, Ethiopian, Julian, Indian national or Bengali
  calendar, and repeat by it. When a day does not come in some year or may come a day early,
  it rings on the earlier side.
- **Secret notes**: **Make it a secret note…** in the note menu keeps the note's text
  encrypted with the secret notes' password, under a title you give it. The text stays
  covered until you double-click the note (or press `Enter`) and type the password, and is
  covered again after a few minutes left alone (**Settings → Protection**). Forgot the
  password? **I forgot the password** takes your recovery key and sets a new one, so secret
  notes are turned on only once the recovery key is kept. **Export all notes…** asks whether
  secret notes go out locked or in the clear.
- **Protection** in Settings: make a new recovery key, or set a **spare password** to
  remember in case you forget where you put the recovery key; it opens your notes too.
  Stickle Manager tells you once when a key or password was changed on this computer.
- **Your own colours**: up to 12 more colours under the note menu's colours (**Settings →
  Notes**). **Give new notes a random colour** picks any colour but the one used just before.
  `Ctrl+Shift+]` and `Ctrl+Shift+[` step through a note's colours.
- **Out of the way of full screen**: while a slide show, a video or a game fills a screen, the
  notes on it go out of sight and come back when it ends; a reminder meanwhile comes as a
  system notification. **Full-screen exceptions…** keeps notes up over the programs you list.
  Windows and Linux with X11 only: under Wayland, use **Hide all notes for now** instead.
- **Bring a Stickle export back in**, on another computer or after a reinstall: **Data →
  Bring notes in from another app… → Stickle export…** and the export's folder. Notes come
  with their colours, categories, marks and reminders, and your own colours too; settings,
  places on screen and passwords stay behind. Secret notes exported locked come in with the
  secret notes' password or the recovery key of the day they were exported. Notes brought in
  once are not brought in again.
- Notes made with an earlier preview (0.1.2 to 0.5.1) open as they were: just replace the
  program. A copy of the notes from before is kept in the `backups` folder where the notes are
  (`%APPDATA%\Stickle\backups` on Windows, `~/.local/share/stickle/backups` on Linux).

0.5 brought daily backups you can go back to (**Data → Restore from a backup…**), export of
every note as Markdown and JSON, notes brought in from Microsoft Sticky Notes and other
sticky-note apps, pasting with formatting, notes that line up when resized too, reporting a
problem or idea from the tray menu, and a daily check for a new version that you can turn off
(**Settings → General**; the Microsoft Store keeps its copy up to date itself). 0.5.1 lets a
note whose text you erased go without the trash.
0.4 brought icons for categories and marks, About Stickle and ways to support it, and a tray
icon that fades while every note is hidden.
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

To move portable notes, move the whole `stickle-data` folder: the files that open the notes
are kept beside them, so a single file copied out of it does not open on its own.

### Differences between systems

Stickle works the same way everywhere, except for what the system itself decides:

- **No tray on GNOME** (the default on Fedora and some others): everything in the tray menu is
  in Stickle Manager too, and when every note is hidden Stickle Manager comes up by itself.
- **Wayland** (the default on Ubuntu and Fedora): Stickle runs through XWayland so that notes
  can stay on top and where you left them, and the desktop asks once before shortcuts from
  anywhere work. Stickle cannot tell when a program is full screen there: before a slide show
  or a video, use **Hide all notes for now** (`Ctrl+Alt+H`). Hiding for full screen works on
  Windows and on Linux with X11, such as Mint.
- **Full-screen exceptions** are named by the program's file on Windows (`msedge.exe`) and by
  the window's class on Linux (`firefox`).
- **Reminder notifications** come from the tray icon on Windows and as desktop notifications
  on Linux, even without a tray. Their sound follows the system's settings. On every system,
  reminders ring only while Stickle is running.
- Where the system cannot draw colour emoji (Fedora), the ⏰ in notes is drawn plain.
- **Enter in a window** presses the button that has the keyboard on Linux, as other Linux apps
  do, and the window's default button on Windows.
- **Starting at login** is off at first in the unsigned Windows zip (see above) and on in the
  Microsoft Store version and on Linux. The Store version does not check for new versions:
  the Store updates it. Only the AppImage offers to add Stickle to the application list.

What a preview is, and is not:

- Notes are kept on this computer only, encrypted, with a week of daily backups on the same
  computer. There is no syncing yet, so a backup does not survive losing the computer: export
  your notes now and then to keep a copy elsewhere.
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
under the [ISC License](src/stickle/app/icons/lucide/LICENSE). Each build has a `licenses`
folder next to the program, whose `THIRD-PARTY-NOTICES.txt` lists everything it includes
(Qt, Python and the rest) with their licenses.

To help, see [CONTRIBUTING.md](CONTRIBUTING.md); to report a security problem,
[SECURITY.md](SECURITY.md).
