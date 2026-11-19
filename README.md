# LocalSeedr

A Seedr-style downloader that runs on **your own computer**. Paste a magnet link, a `.torrent` link or a direct file link (or upload a `.torrent` file), and LocalSeedr downloads it to your own disk. You can then play, save or delete the files from a simple web page in your browser.

Everything stays on your machine. There is no account, no cloud storage and no size limit apart from your free disk space.

## Features

- **Torrents and magnet links**, powered by [libtorrent](https://www.libtorrent.org/)
  - Adds a maintained list of public trackers so magnets without trackers still find peers
  - Uses DHT, peer exchange and local peer discovery; re-announces on its own when a torrent has few peers
  - Stops as soon as the download finishes (no seeding afterwards)
  - Quick pause: for the first 10 minutes a paused torrent stays connected to its peers at a trickle, so Resume gets back to speed straight away; after that it stops fully
- **Direct links** (`http://` and `https://`)
  - Splits big files into parts and downloads them over several connections at once
  - Adjusts the number of connections to what each server allows, and backs off when a server says it is busy
  - Resumes after a pause, a crash or a restart
- **Choose where each download goes**: type a folder or pick one with **Browse**, and set a default folder
- **Files page**: browse every folder you downloaded into, play video and audio in the browser (with seeking), save to another folder, delete
- **Show in folder** opens a finished download in Explorer, Finder or your Linux file manager
- Shows free disk space, speed, ETA and peer counts, plus whether other peers can connect to you

## Requirements

- Windows, macOS or Linux
- **Python 3.9 to 3.13** from [python.org](https://www.python.org/downloads/)
  (On Windows, tick **"Add python.exe to PATH"** in the installer.)
- macOS: Apple Silicon (M1 or newer) with macOS 14+, or Intel with macOS 15+. These are the Macs libtorrent publishes ready-made packages for.

The first start needs an internet connection to install the Python packages listed in `requirements.txt` (Flask, requests and libtorrent). They go into a private `.venv` folder inside this project, so your system Python is not touched.

## Getting started

Download this repository (green **Code** button, then **Download ZIP**, then unzip it), or clone it:

```bash
git clone <this repository's URL>
cd LocalSeedr
```

### Windows

Double-click **`start.bat`**.

### macOS

Double-click **`start.command`**.

The first time, macOS may say it cannot verify the developer. In that case, right-click `start.command`, choose **Open**, then **Open** again. You only need to do this once.

If double-clicking does nothing, open Terminal in the folder and run:

```bash
bash start.sh
```

### Linux

```bash
bash start.sh
```

The first run takes a minute to set things up. After that, your browser opens **http://127.0.0.1:8765** by itself. Keep the window that opened open while you use LocalSeedr. Closing it stops LocalSeedr, and your transfers continue the next time you start it.

## Where your files go

Under the link box there is a **Save to** field:

- It starts with your default folder. Change it before clicking **Add** (or **Upload .torrent**) to save that download somewhere else, for example an external drive.
- **Browse...** opens your system's folder picker. If you cannot see it, it may be behind the browser; check the taskbar or Dock.
- **Make default** uses the folder in the box for all new downloads from now on.
- Folders you used recently are suggested as you type, and a folder that does not exist yet is created.
- Below the box you can see how much space is free on that drive.

Changing the folder never moves downloads you already have. Each transfer remembers its own folder, shown under its name.

| What | Where |
|---|---|
| Downloads (built-in default) | `~/LocalSeedr/downloads` (on Windows: `C:\Users\<you>\LocalSeedr\downloads`) |
| Transfer list, settings, saved `.torrent` files, peer cache | `~/LocalSeedr/data` |

## Settings

LocalSeedr works without any setup. To change something, set an environment variable before starting it:

| Variable | Default | What it does |
|---|---|---|
| `LOCALSEEDR_PORT` | `8765` | Port of the web page |
| `LOCALSEEDR_HOME` | `~/LocalSeedr` | Folder for downloads and data |
| `LOCALSEEDR_DIR` | `<home>/downloads` | Default download folder (overrides the one chosen in the page) |
| `LOCALSEEDR_CONNS` | `16` | Maximum connections per direct download |
| `LOCALSEEDR_HOST` | `127.0.0.1` | Network address the page listens on (see *Security*) |
| `LOCALSEEDR_NO_BROWSER` | not set | Set to `1` to stop the browser opening on start |
| `LOCALSEEDR_SOFT_PAUSE` | `600` | Seconds a paused torrent stays connected before it stops fully |
| `LOCALSEEDR_NO_PROXY` | not set | Set to `1` to ignore the computer's proxy settings for web downloads |

Example (macOS or Linux): `LOCALSEEDR_DIR="/Volumes/External/Downloads" bash start.sh`

## Getting the best torrent speed

- **Let other peers connect to you.** The header of the page shows *Incoming peers: reachable* or *blocked*. If it says blocked, allow LocalSeedr through your firewall. On Windows, if you once clicked **Cancel** on the firewall prompt for Python, Windows saved a *Block* rule; remove it in *Windows Defender Firewall > Allow an app through firewall*.
- **Phone hotspots** usually block incoming connections whatever you do, and their speed is capped by the mobile network. Home broadband over Wi-Fi or a cable is faster.
- Speed depends mostly on how many seeds a torrent has. If a torrent has very few seeds, no setting can make it fast.

## Security

- LocalSeedr only listens on `127.0.0.1`, so only the computer it runs on can open the page.
- Other websites open in your browser cannot use it either: LocalSeedr refuses any change that does not come from its own page, and ignores requests sent to it under another web address.
- **It has no login.** Do not set `LOCALSEEDR_HOST=0.0.0.0` or put it on the internet (port forwarding, tunnels and so on) unless you add authentication. Anyone who can reach the page can add downloads, read your downloaded files and delete them.

## Troubleshooting

- **"Python 3.9 to 3.13 is required"**: install a supported Python version from python.org. Python 3.14 is not supported yet because libtorrent has no ready-made package for it.
- **The page does not open**: go to http://127.0.0.1:8765 yourself. If another program already uses that port, start LocalSeedr with a different `LOCALSEEDR_PORT`.
- **Browse... says the folder picker is not available**: some Linux and Homebrew Python builds leave out Tk. Type the folder path into the box instead (or install your system's `python3-tk` package).
- **"Unable to connect to proxy" / "Cannot reach the proxy set on this computer"**: your computer is set to send web traffic through a proxy that is not running, often `127.0.0.1:9` left behind by a VPN, ad-blocker or proxy app that was switched off. LocalSeedr now notices this and connects directly. If it still fails, turn the proxy off: on Windows, *Settings > Network & internet > Proxy* (switch off "Use a proxy server"); on macOS, *System Settings > Network > (your network) > Details > Proxies*.
- **A direct download shows "retrying: ..."**: the server is busy or limiting connections. LocalSeedr keeps retrying and slows down on its own.
- **A torrent shows 0% after restarting LocalSeedr**: when you resume it, LocalSeedr checks the files already on disk and continues from there.

## Responsible use

LocalSeedr is a general-purpose download tool. Only download content you have the right to download. You are responsible for how you use it and for following the laws where you live.
