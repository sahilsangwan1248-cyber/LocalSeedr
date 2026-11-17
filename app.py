"""LocalSeedr - a personal, local Seedr-style downloader that runs on your own computer.

Paste a direct link, magnet link or .torrent URL (or upload a .torrent file);
files are downloaded to ~/LocalSeedr/downloads on this machine and can be browsed,
streamed and saved from the web UI. No size limits besides your free disk.

Settings (environment variables): LOCALSEEDR_PORT (default 8765), LOCALSEEDR_HOST (default 127.0.0.1),
LOCALSEEDR_HOME (default ~/LocalSeedr), LOCALSEEDR_DIR (downloads folder), LOCALSEEDR_CONNS (max connections
per direct download, default 16), LOCALSEEDR_NO_BROWSER=1 (do not open the browser on start).
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from urllib.parse import unquote, urlparse

import libtorrent as lt
import requests
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
# Kept outside OneDrive on purpose so huge downloads are not synced to the cloud.
HOME = os.environ.get("LOCALSEEDR_HOME", os.path.join(os.path.expanduser("~"), "LocalSeedr"))
DOWNLOADS = os.environ.get("LOCALSEEDR_DIR", os.path.join(HOME, "downloads"))
DATA = os.path.join(HOME, "data")
TORRENTS = os.path.join(DATA, "torrents")
STATE_FILE = os.path.join(DATA, "tasks.json")
for d in (DOWNLOADS, TORRENTS):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = None  # no upload limit

lock = threading.RLock()
tasks = {}  # id -> dict (persisted fields + runtime fields)

# --------------------------------------------------------------------------- torrent session
ses = lt.session({
    "listen_interfaces": "0.0.0.0:6881,[::]:6881",
    "enable_dht": True,
    "enable_lsd": True,
    "enable_upnp": True,
    "enable_natpmp": True,
    "dht_bootstrap_nodes": "router.bittorrent.com:6881,dht.transmissionbt.com:6881,router.utorrent.com:6881",
    "alert_mask": lt.alert.category_t.port_mapping_notification,  # to report whether UPnP/NAT-PMP opened our port
    # faster peer discovery
    "torrent_connect_boost": 100,  # connect to up to 100 peers immediately when a torrent starts (default 30)
    "dht_search_branching": 10,  # query more DHT nodes in parallel per lookup (default 5)
    "dht_aggressive_lookups": True,
    # throughput tuning (defaults are conservative)
    "connections_limit": 1000,
    "active_downloads": 20,
    "active_limit": 60,
    "connection_speed": 100,
    "max_out_request_queue": 1500,
    "max_queued_disk_bytes": 16 * 1024 * 1024,
    "send_buffer_watermark": 3 * 1024 * 1024,
    "send_buffer_low_watermark": 1024 * 1024,
    "aio_threads": 8,
    "request_timeout": 20,
    "peer_connect_timeout": 10,
    "max_peerlist_size": 8000,
    "mixed_mode_algorithm": 0,  # prefer TCP, never throttle it for uTP
    "min_reconnect_time": 5,
    "dht_announce_interval": 900,
    "announce_to_all_trackers": True,
    "announce_to_all_tiers": True,
    "strict_end_game_mode": True,
    "prefer_udp_trackers": True,
    "download_rate_limit": 0,
    "upload_rate_limit": 0,
})
handles = {}  # task id -> torrent_handle
PUBLIC_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://explodie.org:6969/announce",
]


# --------------------------------------------------------------------------- download folders
SETTINGS_FILE = os.path.join(DATA, "settings.json")
settings = {"default_dir": DOWNLOADS, "recent_dirs": []}
try:
    with open(SETTINGS_FILE, encoding="utf-8") as _f:
        settings.update(json.load(_f))
except (OSError, ValueError):
    pass
if "LOCALSEEDR_DIR" in os.environ:
    settings["default_dir"] = DOWNLOADS  # an explicit environment setting wins over the saved one


def save_settings():
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=1)
    os.replace(tmp, SETTINGS_FILE)


def default_dir():
    return settings["default_dir"]


def resolve_dir(raw, create=True):
    """Turn a user-typed folder into an absolute, writable path (created if missing). Raises ValueError."""
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return default_dir()
    d = os.path.expanduser(raw)
    if not os.path.isabs(d):
        raise ValueError("Use a full folder path, for example " + os.path.join(os.path.expanduser("~"), "Downloads"))
    d = os.path.normpath(d)
    if os.path.exists(d) and not os.path.isdir(d):
        raise ValueError(f"{d} is a file, not a folder")
    if not create:
        return d
    try:
        os.makedirs(d, exist_ok=True)
        probe_path = os.path.join(d, f".localseedr-write-test-{uuid.uuid4().hex[:6]}")
        with open(probe_path, "wb"):
            pass
        os.remove(probe_path)
    except OSError as e:
        raise ValueError(f"Cannot save to {d}: {e.strerror or e}") from None
    return d


def remember_dir(d):
    recent = [x for x in settings["recent_dirs"] if os.path.normcase(x) != os.path.normcase(d)]
    settings["recent_dirs"] = [d] + recent[:7]
    save_settings()


def save_state():
    with lock:
        keep = {}
        for tid, t in tasks.items():
            keep[tid] = {k: t[k] for k in ("id", "kind", "source", "name", "added", "status", "paused", "size", "dir")
                         if k in t}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(keep, f)
        os.replace(tmp, STATE_FILE)


def add_torrent_handle(tid, source, kind, save_dir):
    """kind: magnet | torrent (source = path to .torrent file)."""
    if kind == "magnet":
        atp = lt.parse_magnet_uri(source)
    else:
        with open(source, "rb") as f:
            atp = lt.add_torrent_params()
            atp.ti = lt.torrent_info(lt.bdecode(f.read()))
    atp.save_path = save_dir
    # Add well-known public trackers so even tracker-less magnets find many more peers.
    have = set(atp.trackers)
    extra = [x for x in PUBLIC_TRACKERS if x not in have]
    atp.trackers = list(atp.trackers) + extra
    atp.tracker_tiers = list(atp.tracker_tiers) + [1] * (len(atp.trackers) - len(atp.tracker_tiers))
    # Not auto-managed: otherwise libtorrent's queue silently resumes torrents we paused (incl. finished ones,
    # which would then seed). We run every torrent at once anyway.
    # libtorrent's default flags also include `paused` (the queue manager normally starts torrents), so without
    # auto-management we must clear it ourselves or the torrent never starts.
    atp.flags &= ~lt.torrent_flags.auto_managed
    atp.flags &= ~lt.torrent_flags.paused
    h = ses.add_torrent(atp)
    handles[tid] = h
    return h


def new_task(kind, source, name, save_dir):
    tid = uuid.uuid4().hex[:10]
    t = {"id": tid, "kind": kind, "source": source, "name": name, "added": time.time(),
         "status": "queued", "paused": False, "size": 0, "dir": save_dir}
    with lock:
        tasks[tid] = t
    return t


def add_source(url, save_dir):
    url = url.strip()
    if url.startswith("magnet:"):
        m = re.search(r"dn=([^&]+)", url)
        name = unquote(m.group(1).replace("+", " ")) if m else "Magnet (fetching metadata)"
        t = new_task("magnet", url, name, save_dir)
        add_torrent_handle(t["id"], url, "magnet", save_dir)
    elif re.match(r"^https?://", url, re.I):
        if urlparse(url).path.lower().endswith(".torrent"):
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            return add_torrent_bytes(r.content, save_dir)
        t = new_task("http", url, os.path.basename(unquote(urlparse(url).path)) or "download", save_dir)
        start_http(t["id"])
    else:
        raise ValueError("Unsupported link. Use a magnet:, http(s):// or .torrent URL.")
    save_state()
    return t


def add_torrent_bytes(data, save_dir):
    info = lt.torrent_info(lt.bdecode(data))  # validates
    path = os.path.join(TORRENTS, str(uuid.uuid4().hex) + ".torrent")
    with open(path, "wb") as f:
        f.write(data)
    t = new_task("torrent", path, info.name(), save_dir)
    t["size"] = info.total_size()
    add_torrent_handle(t["id"], path, "torrent", save_dir)
    save_state()
    return t


# --------------------------------------------------------------------------- http downloader
def unique_path(name, folder):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "download"
    base, ext = os.path.splitext(name)
    p, i = os.path.join(folder, name), 1
    while os.path.exists(p) or os.path.exists(p + ".part"):
        p = os.path.join(folder, f"{base} ({i}){ext}")
        i += 1
    return p


def filename_from_headers(resp, url):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        return unquote(m.group(1).strip())
    return os.path.basename(unquote(urlparse(resp.url or url).path)) or "download"


CONNS = int(os.environ.get("LOCALSEEDR_CONNS", "16"))  # upper bound on connections per direct download
START_CONNS = 4  # adaptive: start here, add more only while they actually raise the speed
MIN_PARALLEL = 4 * 1024 * 1024  # files smaller than this use a single connection
UA = {"User-Agent": "Mozilla/5.0 LocalSeedr"}
THROTTLE_CODES = {403, 429, 503, 509}


def probe(url):
    """Ask for byte 0 only: tells us the name, the total size and whether Range is supported."""
    with requests.get(url, headers={**UA, "Range": "bytes=0-0"}, stream=True, timeout=(15, 30), allow_redirects=True) as r:
        name = filename_from_headers(r, url)
        if r.status_code == 206:
            m = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range", ""))
            return {"name": name, "total": int(m.group(1)) if m else 0, "ranged": bool(m)}
        r.raise_for_status()
        return {"name": name, "total": int(r.headers.get("Content-Length", 0)), "ranged": False}


def write_sidecar(part, total, segs):
    tmp = part + ".json.tmp"
    with open(tmp, "w") as f:
        json.dump({"total": total, "segs": [list(s[:3]) for s in list(segs)]}, f)
    os.replace(tmp, part + ".json")


# A segment is [start, end, next_byte, active]. Workers claim segments; an idle worker either picks up an
# orphaned segment (its connection was dropped) or splits the largest active one and takes the back half.
def claim(t):
    with t["_seglock"]:
        for s in t["_segs"]:
            if not s[3] and s[2] <= s[1]:
                s[3] = 1
                return s
        active = [s for s in t["_segs"] if s[3] and s[2] <= s[1]]
        if not active:
            return None
        victim = max(active, key=lambda s: s[1] - s[2])
        remain = victim[1] - victim[2] + 1
        if remain < 2 * 1024 * 1024:
            return None
        mid = victim[2] + remain // 2
        new = [mid, victim[1], mid, 1]
        victim[1] = mid - 1
        t["_segs"].append(new)
        return new


def seg_worker(t, stop):
    sess = requests.Session()  # keep-alive: later segments reuse the TCP/TLS connection
    sess.headers.update(UA)
    seg = None
    try:
        seg = claim(t)
        while seg is not None and not stop.is_set():
            result = run_seg(t, seg, stop, sess)
            seg[3] = 0  # done, or hand the rest of this segment back for another connection
            if result != "done":
                if result != "stopped":
                    print(f"[http {t['name']}] connection {result}: {t.get('_err')}", file=sys.stderr, flush=True)
                if result == "throttled":
                    t["_throttled"] = True
                seg = None
                return
            seg = claim(t)
    finally:
        if seg is not None:
            seg[3] = 0  # never leave a segment marked as owned by a dead connection
        sess.close()


def run_seg(t, seg, stop, sess):
    """Download bytes seg[2]..seg[1]. Returns done | stopped | throttled | failed."""
    for attempt in range(5):
        if stop.is_set():
            return "stopped"
        if seg[2] > seg[1]:
            return "done"
        try:
            with sess.get(t["source"], headers={"Range": f"bytes={seg[2]}-{seg[1]}"}, stream=True,
                          timeout=(15, 30), allow_redirects=True) as r:
                if r.status_code in THROTTLE_CODES:
                    t["_err"] = f"server limited connections ({r.status_code})"
                    t["_throttled"] = True
                    if t.get("_conns", 0) > t.get("_target", 1):
                        return "throttled"  # we are above the target: shed this connection
                    ra = r.headers.get("Retry-After", "")
                    wait = float(ra) if ra.isdigit() else 2.0 * (attempt + 1)
                    r.close()
                    time.sleep(min(wait, 30))  # keep our segment and wait, rather than re-requesting elsewhere
                    continue
                if r.status_code != 206:
                    raise OSError(f"server answered {r.status_code} to a Range request")
                with t["_rlock"]:
                    t["_recv"] += 1  # connections the server is actually feeding right now
                try:
                    with open(t["_part"], "r+b") as f:
                        f.seek(seg[2])
                        for chunk in r.iter_content(128 * 1024):  # small steps = smooth speed readings for the ramp
                            if stop.is_set():
                                return "stopped"
                            n = min(len(chunk), seg[1] - seg[2] + 1)  # seg[1] may shrink when another connection steals our tail
                            if n <= 0:
                                break
                            f.write(chunk[:n])
                            seg[2] += n
                            if seg[2] > seg[1]:
                                break
                finally:
                    with t["_rlock"]:
                        t["_recv"] -= 1
            if seg[2] > seg[1]:
                return "done"
        except requests.ConnectionError as e:
            # refused/reset while other connections are fine usually means a per-IP connection limit
            t["_err"] = str(e)
            if attempt >= 1 and t.get("_conns", 0) > 2:
                return "throttled"
            time.sleep(1 + attempt)
        except (requests.RequestException, OSError) as e:
            t["_err"] = str(e)
            time.sleep(1 + attempt)
    return "failed"


def http_parallel(tid):
    t = tasks[tid]
    stop, part = t["_stop"], t["_part"]
    with open(part + ".json") as f:
        meta = json.load(f)
    segs, total = [list(s[:3]) + [0] for s in meta["segs"]], meta["total"]
    t.update(size=total, status="downloading", _segs=segs, _seglock=threading.Lock(), _throttled=False, _err=None,
             _target=START_CONNS, _recv=0, _rlock=threading.Lock())
    workers = []

    def spawn(n):
        for _ in range(n):
            th = threading.Thread(target=seg_worker, args=(t, stop), daemon=True)
            th.start()
            workers.append(th)

    def claimable():
        with t["_seglock"]:
            live = [s for s in segs if s[2] <= s[1]]
            return any(not s[3] for s in live) or any(s[1] - s[2] > 4 * 1024 * 1024 for s in live)

    done = lambda: sum(min(s[2], s[1] + 1) - s[0] for s in list(segs))  # noqa: E731
    # Controller: keep `target` connections running. Every 4 s try 2 more and keep them only if the average
    # speed rose by >= 8%. If the server pushes back (503/429/refused), drop target to what it accepted, then
    # probe upward again after 15 s quiet, since connection slots on many servers free up lazily.
    target, limit = START_CONNS, CONNS
    spawn(target)
    last, last_b = time.time(), done()
    t["_done"] = last_b
    window = []  # recent per-second rates
    base_rate, last_change, throttled_at = None, time.time(), 0.0
    progress_mark, progress_at = last_b, time.time()
    while not stop.is_set():
        time.sleep(1)
        alive = [w for w in workers if w.is_alive()]
        workers[:] = alive
        now, d = time.time(), done()
        rate = (d - last_b) / max(now - last, 0.001)
        last, last_b = now, d
        window = (window + [rate])[-5:]
        t.update(_rate=rate, _done=d, _conns=len(alive))
        write_sidecar(part, total, segs)
        if all(s[2] > s[1] for s in segs):
            break
        if d > progress_mark:
            progress_mark, progress_at = d, now
        elif now - progress_at > 90:
            break  # nothing arrived for 90 s on any connection: give up (resume later continues from here)
        if t["_throttled"]:
            t["_throttled"] = False
            if now - throttled_at > 3:  # count a burst of 429/503s as one push-back
                # settle on the number of connections the server is actually serving, not on a count that
                # still includes connections waiting to be shed (which would keep ratcheting down)
                target = limit = max(1, min(target, t["_recv"]))
            throttled_at, base_rate, last_change = now, None, now
        elif limit < CONNS and throttled_at and now - throttled_at > 30:
            limit, throttled_at = min(CONNS, limit + 2), 0.0  # probe upward again
        t["_target"] = target
        if now - last_change >= 5 and len(window) == 5 and target < limit and claimable():
            avg = sum(window) / len(window)
            if base_rate is None or avg >= base_rate * 1.08:
                base_rate, last_change = avg, now
                target = min(limit, target + 2)
            else:
                limit = target  # plateau: extra connections did not raise the speed
                last_change = now
        if len(alive) < target and claimable():
            spawn(target - len(alive))  # refill connections that finished or were dropped
    for w in workers:
        w.join(timeout=5)
    t["_done"], t["_rate"], t["_conns"] = done(), 0, 0
    write_sidecar(part, total, segs)
    if all(s[2] > s[1] for s in segs):
        os.replace(part, part[: -len(".part")])
        os.remove(part + ".json")
        t.update(status="completed", _part=None)
        save_state()
    elif not stop.is_set():
        t["status"] = "error"
        save_state()


def http_worker(tid):
    t = tasks[tid]
    try:
        part = t.get("_part")
        if part and os.path.exists(part + ".json"):
            return http_parallel(tid)  # resume a segmented download
        if not part:
            info = probe(t["source"])
            if info["ranged"] and info["total"] >= MIN_PARALLEL and CONNS > 1:
                final = unique_path(info["name"], t["dir"])
                part = final + ".part"
                total = info["total"]
                n = min(START_CONNS, max(2, total // (2 * 1024 * 1024)))
                step = -(-total // n)
                segs = [[i * step, min((i + 1) * step, total) - 1, i * step] for i in range(n) if i * step < total]
                with open(part, "wb") as f:
                    f.truncate(total)
                write_sidecar(part, total, segs)
                t.update(name=os.path.basename(final), _part=part)
                return http_parallel(tid)
    except (requests.RequestException, OSError) as e:
        t["_err"] = str(e)  # fall back to the plain single-connection downloader, which retries
    http_single(tid)


def http_single(tid):
    t = tasks[tid]
    stop = t["_stop"]
    try:
        for attempt in range(5):
            if stop.is_set():
                return
            part = t.get("_part")
            have = os.path.getsize(part) if part and os.path.exists(part) else 0
            headers = {"User-Agent": "Mozilla/5.0 LocalSeedr"}
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                with requests.get(t["source"], headers=headers, stream=True, timeout=(15, 30), allow_redirects=True) as r:
                    if r.status_code == 416 and part:  # already complete
                        have = os.path.getsize(part)
                        r.close()
                        t["size"] = have
                    else:
                        r.raise_for_status()
                        if not part:
                            final = unique_path(filename_from_headers(r, t["source"]), t["dir"])
                            t["name"] = os.path.basename(final)
                            part = t["_part"] = final + ".part"
                            have = 0
                        if have and r.status_code != 206:
                            have = 0  # server ignored Range; restart
                        total = int(r.headers.get("Content-Length", 0))
                        t["size"] = have + total if total else 0
                        t["status"] = "downloading"
                        t["_done"] = have
                        last, last_b = time.time(), have
                        with open(part, "ab" if have else "wb") as f:
                            for chunk in r.iter_content(1024 * 1024):
                                if stop.is_set():
                                    return
                                f.write(chunk)
                                t["_done"] += len(chunk)
                                now = time.time()
                                if now - last >= 1:
                                    t["_rate"] = (t["_done"] - last_b) / (now - last)
                                    last, last_b = now, t["_done"]
                os.replace(part, part[: -len(".part")])
                t.update(status="completed", _rate=0, size=t["_done"] if t.get("_done") else t["size"])
                t["_part"] = None
                save_state()
                return
            except (requests.RequestException, OSError) as e:
                t["_err"] = str(e)
                time.sleep(2 + attempt * 3)
        t["status"] = "error"
        save_state()
    finally:
        t["_rate"] = 0 if t["status"] != "downloading" else t.get("_rate", 0)


def start_http(tid):
    t = tasks[tid]
    t["_stop"] = threading.Event()
    t["status"] = "downloading"
    t["paused"] = False
    t["_done"] = t.get("_done", 0)
    threading.Thread(target=http_worker, args=(tid,), daemon=True).start()


# --------------------------------------------------------------------------- task view
STATES = {
    lt.torrent_status.checking_files: "checking",
    lt.torrent_status.downloading_metadata: "metadata",
    lt.torrent_status.downloading: "downloading",
    lt.torrent_status.finished: "completed",
    lt.torrent_status.seeding: "completed",
    lt.torrent_status.checking_resume_data: "checking",
}


def friendly_err(msg):
    """Turn raw Python/network error text into a short sentence a person can act on."""
    if not msg:
        return "Download failed. Click resume to try again."
    s = str(msg)
    m = re.search(r"\b(\d{3}) (?:Client|Server) Error", s)
    if m:
        code = int(m.group(1))
        return {401: "The server asks for a login (401).", 403: "The server refused access (403). The link may have expired.",
                404: "File not found on the server (404). Check the link.", 410: "The file is no longer available (410)."
                }.get(code, f"The server returned an error ({code}).")
    low = s.lower()
    if "server limited connections" in low:
        return s[0].upper() + s[1:]
    if "getaddrinfo" in low or "name or service not known" in low or "nodename nor servname" in low:
        return "Cannot find that website. Check the link or your internet connection."
    if "read timed out" in low or "timeout" in low:
        return "The server stopped responding."
    if "connection refused" in low or "newconnectionerror" in low or "max retries exceeded" in low:
        return "Cannot reach the server. Check the link or your internet connection."
    if "connection reset" in low or "connection aborted" in low or "remotedisconnected" in low:
        return "The server dropped the connection."
    if "ssl" in low or "certificate" in low:
        return "Secure connection failed (certificate problem)."
    if "no space left" in low or "errno 28" in low:
        return "The disk is full."
    if "permission denied" in low or "errno 13" in low:
        return "No permission to write to the download folder."
    return s[:140]


def view(t):
    v = {"id": t["id"], "kind": t["kind"], "name": t["name"], "added": t["added"], "paused": t["paused"],
         "dir": t.get("dir", DOWNLOADS)}
    if t["kind"] == "http":
        done, size = t.get("_done", 0), t.get("size", 0)
        v.update(status=t["status"], done=done, size=size, progress=(done / size) if size else (1 if t["status"] == "completed" else 0),
                 down=t.get("_rate", 0) if t["status"] == "downloading" else 0, up=0,
                 peers=t.get("_conns", 0) if t["status"] == "downloading" else 0,
                 error=friendly_err(t.get("_err")) if t["status"] == "error" else
                 ("Retrying: " + friendly_err(t["_err"]) if t["status"] == "downloading" and t.get("_err") and not t.get("_rate") else None))
    else:
        h = handles.get(t["id"])
        if h is None or not h.is_valid():
            v.update(status="error", done=0, size=0, progress=0, down=0, up=0, peers=0)
            return v
        s = h.status()
        finished = STATES.get(s.state) == "completed"
        if finished and t.get("status") != "completed" and (not t["paused"] or t.get("_soft_until")):
            # Seedr-style: stop as soon as the content is on disk, no seeding (also if it finished while soft-paused)
            hard_pause(t, h)
            t["paused"] = True
            t["status"] = "completed"
            t["size"] = s.total_wanted
            save_state()
        status = "paused" if t["paused"] and s.state != lt.torrent_status.downloading_metadata else STATES.get(s.state, "downloading")
        t["name"] = s.name or t["name"]
        v.update(name=t["name"], status=status, done=s.total_wanted_done, size=s.total_wanted, progress=s.progress,
                 down=0 if t["paused"] else s.download_rate, up=0 if t["paused"] else s.upload_rate,
                 peers=s.num_peers, seeds=s.num_seeds, error=None)
        if t["status"] == "completed":
            v["status"], v["progress"] = "completed", 1
    return v


# --------------------------------------------------------------------------- api
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}


@app.before_request
def guard():
    """Only LocalSeedr's own page may use the API.

    - Host check: blocks DNS-rebinding, where a malicious site points its own hostname at 127.0.0.1.
    - Custom header on every change: a web page on another site cannot add it without the browser asking
      us first (CORS preflight), which we never approve. Without this, any site you visit could quietly
      start a download into a folder of its choosing, e.g. your Startup folder.
    """
    if os.environ.get("LOCALSEEDR_HOST", "127.0.0.1") in LOOPBACK_NAMES:
        host = re.sub(r":\d+$", "", request.host or "").strip("[]").lower()
        if host not in LOOPBACK_NAMES:
            abort(403)
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-LocalSeedr") != "1":
        abort(403)


def folder_from_request(raw):
    d = resolve_dir(raw)
    remember_dir(d)
    return d


@app.post("/api/add")
def api_add():
    body = request.json or {}
    urls = [u for u in re.split(r"\s+", body.get("url", "")) if u]
    if not urls:
        return jsonify(error="No link provided"), 400
    try:
        save_dir = folder_from_request(body.get("dir"))
    except ValueError as e:
        return jsonify(added=[], errors=[str(e)]), 400
    added, errors = [], []
    for u in urls:
        try:
            added.append(add_source(u, save_dir)["id"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{u[:60]}: {e}")
    return jsonify(added=added, errors=errors), (200 if added else 400)


@app.post("/api/add-torrent")
def api_add_torrent():
    try:
        save_dir = folder_from_request(request.form.get("dir"))
    except ValueError as e:
        return jsonify(added=[], errors=[str(e)]), 400
    added, errors = [], []
    for f in request.files.getlist("file"):
        try:
            added.append(add_torrent_bytes(f.read(), save_dir)["id"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{f.filename}: {e}")
    return jsonify(added=added, errors=errors), (200 if added else 400)


def disk_info(path):
    """Free space of the drive `path` is (or would be) on; walks up to the nearest existing folder."""
    p = path
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        du = shutil.disk_usage(p)
        return {"free": du.free, "total": du.total, "used": du.used}
    except OSError:
        return {"free": 0, "total": 0, "used": 0}


@app.get("/api/tasks")
def api_tasks():
    with lock:
        items = [view(t) for t in sorted(tasks.values(), key=lambda x: -x["added"])]
    return jsonify(tasks=items, disk=disk_info(default_dir()))


@app.get("/api/settings")
def api_settings_get():
    return jsonify(default_dir=default_dir(), recent_dirs=settings["recent_dirs"], home=os.path.expanduser("~"))


@app.post("/api/settings")
def api_settings_set():
    try:
        d = resolve_dir((request.json or {}).get("default_dir"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    settings["default_dir"] = d
    remember_dir(d)  # also saves settings
    return jsonify(default_dir=d)


@app.get("/api/dirinfo")
def api_dirinfo():
    """Live check for the Save-to box: is the folder usable, and how much room is there."""
    try:
        d = resolve_dir(request.args.get("path"), create=False)
    except ValueError as e:
        return jsonify(ok=False, error=str(e))
    return jsonify(ok=True, path=d, exists=os.path.isdir(d), **disk_info(d))


PICKER_CODE = r"""
import sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw()
try:
    root.attributes("-topmost", True)
except Exception:
    pass
root.update()
start = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
print(filedialog.askdirectory(initialdir=start, title="Choose where LocalSeedr saves downloads", mustexist=False) or "")
"""
picker_lock = threading.Lock()


@app.post("/api/pick-folder")
def api_pick_folder():
    """Open the operating system's folder picker. Runs in a separate process because macOS only allows
    windows on a program's main thread, and Flask answers requests on worker threads."""
    import subprocess
    if not picker_lock.acquire(blocking=False):
        return jsonify(error="A folder picker is already open (check your taskbar or Dock)."), 409
    try:
        start = (request.json or {}).get("start") or default_dir()
        r = subprocess.run([sys.executable, "-c", PICKER_CODE, start if os.path.isdir(start) else ""],
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            return jsonify(error="The folder picker is not available on this computer. Type the folder path instead."), 501
        chosen = r.stdout.strip()
        return jsonify(path=os.path.normpath(chosen) if chosen else None)
    except subprocess.TimeoutExpired:
        return jsonify(path=None)
    finally:
        picker_lock.release()


def reveal(path):
    """Show a file or folder in Explorer / Finder / the Linux file manager."""
    import subprocess
    if sys.platform.startswith("win"):
        if os.path.isdir(path):
            os.startfile(path)  # noqa: S606 - local path the app itself wrote
        else:
            subprocess.Popen(["explorer", "/select,", path])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if os.path.isfile(path) else ["open", path])
    else:
        subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])


@app.post("/api/tasks/<tid>/reveal")
def api_task_reveal(tid):
    t = tasks.get(tid) or abort(404)
    target = os.path.join(t["dir"], t["name"])
    reveal(target if os.path.exists(target) else t["dir"])
    return jsonify(ok=True)


# Pausing a torrent for real closes every peer connection, and resuming then takes 1-3 minutes to get back
# to speed (reconnect, handshake, wait to be unchoked by seeds, TCP slow start). So Pause first only throttles
# the torrent to a trickle and keeps its connections; after SOFT_PAUSE_SECS it becomes a real pause.
SOFT_PAUSE_SECS = int(os.environ.get("LOCALSEEDR_SOFT_PAUSE", "600"))
TRICKLE = 2048  # bytes/s each way while soft-paused: keeps us "interested" and connected, uses next to no data


def soft_pause(t, h):
    h.set_download_limit(TRICKLE)
    h.set_upload_limit(TRICKLE)
    t["_soft_until"] = time.time() + SOFT_PAUSE_SECS


def hard_pause(t, h):
    t["_soft_until"] = None
    h.pause()


def unpause(t, h):
    t["_soft_until"] = None
    h.set_download_limit(-1)  # unlimited
    h.set_upload_limit(-1)
    h.resume()  # no-op if it was only soft-paused


@app.post("/api/tasks/<tid>/<action>")
def api_task_action(tid, action):
    t = tasks.get(tid) or abort(404)
    if action == "pause":
        t["paused"] = True
        if t["kind"] == "http":
            t["_stop"].set()
            t["status"] = "paused"
        elif tid in handles:
            soft_pause(t, handles[tid])
    elif action == "resume":
        t["paused"] = False
        if t["kind"] == "http":
            if t["status"] in ("paused", "error"):
                start_http(tid)
        elif tid in handles:
            unpause(t, handles[tid])
    else:
        abort(400)
    save_state()
    return jsonify(ok=True)


@app.delete("/api/tasks/<tid>")
def api_task_delete(tid):
    t = tasks.get(tid) or abort(404)
    with_files = request.args.get("files") == "1"
    if t["kind"] == "http":
        t["_stop"].set()
        part = t.get("_part")
        if part and os.path.exists(part) and (with_files or t["status"] != "completed"):
            time.sleep(0.3)
            for p in (part, part + ".json"):
                if os.path.exists(p):
                    os.remove(p)
        final = os.path.join(t["dir"], t["name"])
        if with_files and t["status"] == "completed" and os.path.isfile(final):
            os.remove(final)
    else:
        h = handles.pop(tid, None)
        if h is not None:
            ses.remove_torrent(h, lt.session.delete_files if with_files else 0)
        if t["kind"] == "torrent" and os.path.exists(t["source"]):
            os.remove(t["source"])
    with lock:
        tasks.pop(tid, None)
    save_state()
    return jsonify(ok=True)


# The Files view can browse every folder LocalSeedr has saved into (and nothing else).
def known_roots():
    paths = [default_dir()] + [t.get("dir") for t in list(tasks.values())] + settings["recent_dirs"] + [DOWNLOADS]
    out, seen = [], set()
    for p in paths:
        key = os.path.normcase(os.path.realpath(p)) if p else None
        if key and key not in seen and os.path.isdir(p):
            seen.add(key)
            out.append(p)
    return out


def root_id(path):
    import hashlib
    return hashlib.sha1(os.path.normcase(os.path.realpath(path)).encode("utf-8")).hexdigest()[:10]


def safe(rid, rel):
    for r in known_roots():
        if root_id(r) == rid:
            root = os.path.realpath(r)
            break
    else:
        abort(404)
    p = os.path.realpath(os.path.join(root, rel or ""))
    if os.path.normcase(p) != os.path.normcase(root) and not os.path.normcase(p).startswith(os.path.normcase(root) + os.sep):
        abort(403)
    return p, root


@app.get("/api/roots")
def api_roots():
    d = os.path.normcase(os.path.realpath(default_dir()))
    return jsonify(roots=[{"id": root_id(r), "path": r, "default": os.path.normcase(os.path.realpath(r)) == d,
                           "free": disk_info(r)["free"]} for r in known_roots()])


@app.get("/api/files")
def api_files():
    p, _ = safe(request.args.get("root", ""), request.args.get("path", ""))
    if not os.path.isdir(p):
        abort(404)
    out = []
    for e in os.scandir(p):
        if e.name.endswith((".part", ".parts")):
            continue
        st = e.stat()
        out.append({"name": e.name, "dir": e.is_dir(), "size": st.st_size if e.is_file() else folder_size(e.path),
                    "mtime": st.st_mtime})
    out.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return jsonify(files=out)


def folder_size(path):
    total = 0
    for r, _, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return total


@app.get("/files/<rid>/<path:rel>")
def serve_file(rid, rel):
    p, _ = safe(rid, rel)
    if not os.path.isfile(p):
        abort(404)
    return send_file(p, as_attachment=request.args.get("dl") == "1", conditional=True)  # supports Range / seeking


@app.delete("/api/files")
def api_file_delete():
    p, root = safe(request.args.get("root", ""), request.args.get("path", ""))
    if os.path.normcase(p) == os.path.normcase(root) or not os.path.exists(p):
        abort(400)  # never delete a whole download folder itself
    shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    return jsonify(ok=True)


# --------------------------------------------------------------------------- startup
# --------------------------------------------------------------------------- peer discovery boosters
TRACKER_LIST_URL = "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
SESSION_STATE = os.path.join(DATA, "session.state")


def refresh_trackers():
    """Pull a maintained list of live public trackers and add them to every running torrent."""
    try:
        r = requests.get(TRACKER_LIST_URL, timeout=20)
        r.raise_for_status()
        fresh = [l.strip() for l in r.text.splitlines() if l.strip().startswith(("udp://", "http://", "https://"))][:40]
    except requests.RequestException:
        return
    if not fresh:
        return
    PUBLIC_TRACKERS[:] = fresh + [x for x in PUBLIC_TRACKERS if x not in fresh]
    for h in list(handles.values()):
        try:
            have = {x["url"] for x in h.trackers()}
            for u in fresh:
                if u not in have:
                    h.add_tracker({"url": u, "tier": 1})
        except Exception:  # noqa: BLE001
            pass


def save_session_state():
    """Persist DHT routing table so the next start finds peers immediately instead of re-bootstrapping."""
    try:
        with open(SESSION_STATE, "wb") as f:
            f.write(lt.bencode(ses.save_state()))
    except Exception:  # noqa: BLE001
        pass


def load_session_state():
    try:
        with open(SESSION_STATE, "rb") as f:
            ses.load_state(lt.bdecode(f.read()))
    except Exception:  # noqa: BLE001
        pass


DIAG = {"portmap": "waiting for router reply"}


def alert_pump():
    """Record UPnP / NAT-PMP results so the UI can say whether peers can connect to us."""
    while True:
        ses.wait_for_alert(1000)
        for a in ses.pop_alerts():
            if isinstance(a, lt.portmap_alert):
                DIAG["portmap"] = "open via router (" + a.message() + ")"
            elif isinstance(a, lt.portmap_error_alert):
                if not DIAG["portmap"].startswith("open"):
                    DIAG["portmap"] = "router did not open the port"


@app.get("/api/tasks/<tid>/debug")
def api_task_debug(tid):
    h = handles.get(tid) or abort(404)
    s = h.status()
    trackers = []
    for tr in h.trackers():
        msgs = []
        try:
            for ep in tr.get("endpoints", []):
                for ih in ep.get("info_hashes", [ep]):
                    if ih.get("message"):
                        msgs.append(ih["message"])
                    if ih.get("last_error") and ih["last_error"].get("value"):
                        msgs.append("error " + str(ih["last_error"].get("value")))
        except Exception as e:  # noqa: BLE001
            msgs.append(f"(unreadable: {e})")
        trackers.append({"url": tr["url"], "verified": tr.get("verified"), "fails": tr.get("fails"), "msg": msgs[:2]})
    peers = []
    for p in h.get_peer_info():
        peers.append({"ip": str(p.ip[0]), "down": p.down_speed, "seed": bool(p.flags & lt.peer_info.seed),
                      "connecting":bool(p.flags & lt.peer_info.connecting),
                      "handshake": bool(p.flags & lt.peer_info.handshake), "src": int(p.source)})
    return jsonify(save_path=s.save_path, swarm_seeds_reported=s.list_seeds, swarm_peers_reported=s.list_peers, connect_candidates=s.connect_candidates,
                   num_complete=s.num_complete, num_incomplete=s.num_incomplete, connected=s.num_peers,
                   trackers_working=sum(1 for x in trackers if x["verified"]), trackers_total=len(trackers),
                   trackers=trackers, peers=peers)


@app.get("/api/diag")
def api_diag():
    incoming = outgoing = 0
    for h in list(handles.values()):
        try:
            for p in h.get_peer_info():
                if p.flags & lt.peer_info.local_connection:
                    outgoing += 1
                else:
                    incoming += 1
        except Exception:  # noqa: BLE001
            pass
    try:
        st = ses.status()
        dht_nodes, has_incoming = st.dht_nodes, st.has_incoming_connections
    except Exception:  # noqa: BLE001
        dht_nodes, has_incoming = None, None
    return jsonify(listen_port=ses.listen_port(), incoming_peers=incoming, outgoing_peers=outgoing,
                   ever_had_incoming=has_incoming, dht_nodes=dht_nodes, port_mapping=DIAG["portmap"])


def swarm_monitor():
    """Every 30 s: torrents with few peers or slow speed get a fresh tracker + DHT announce."""
    tick = 0
    while True:
        time.sleep(30)
        tick += 1
        if tick % 10 == 1:
            refresh_trackers()
        if tick % 10 == 0:
            save_session_state()
        for tid, h in list(handles.items()):
            try:
                t = tasks.get(tid)
                if t and t["paused"] and t.get("_soft_until") and time.time() >= t["_soft_until"]:
                    hard_pause(t, h)  # paused for 10 minutes: now close the connections for real
                if not t or t["paused"]:
                    continue
                s = h.status()
                if s.is_finished:
                    continue
                if s.num_peers < 30 or s.download_rate < 500_000:
                    h.force_reannounce()
                    h.force_dht_announce()
            except Exception:  # noqa: BLE001
                pass


def restore():
    if not os.path.exists(STATE_FILE):
        return
    with open(STATE_FILE, encoding="utf-8") as f:
        saved = json.load(f)
    for tid, t in saved.items():
        t.setdefault("dir", DOWNLOADS)  # transfers from before folders were selectable lived in the built-in folder
        tasks[tid] = t
        try:
            if t["kind"] == "http":
                if t["status"] != "completed":
                    t["status"], t["paused"] = "paused", True
                    cand = os.path.join(t["dir"], t["name"] + ".part")
                    t["_part"] = cand if os.path.exists(cand) else None
                    t["_stop"] = threading.Event()
                    t["_done"] = os.path.getsize(t["_part"]) if t["_part"] else 0
                    side = (t["_part"] or "") + ".json"
                    if t["_part"] and os.path.exists(side):
                        with open(side) as f:
                            segs = json.load(f)["segs"]
                        t["_done"] = sum(min(s[2], s[1] + 1) - s[0] for s in segs)
                else:
                    t["_done"] = t.get("size", 0)
            else:
                h = add_torrent_handle(tid, t["source"], t["kind"], t["dir"])
                if t.get("paused"):
                    h.pause()
        except Exception as e:  # noqa: BLE001
            print("could not restore", tid, e)


if __name__ == "__main__":
    load_session_state()
    restore()
    threading.Thread(target=swarm_monitor, daemon=True).start()
    threading.Thread(target=alert_pump, daemon=True).start()
    import atexit
    atexit.register(save_session_state)
    host = os.environ.get("LOCALSEEDR_HOST", "127.0.0.1")
    port = int(os.environ.get("LOCALSEEDR_PORT", "8765"))  # not 5000: macOS uses it for AirPlay Receiver
    url = f"http://127.0.0.1:{port}" if host in ("0.0.0.0", "::") else f"http://{host}:{port}"
    print(f"LocalSeedr running at {url}  (default download folder: {default_dir()})")
    print("Keep this window open while you use LocalSeedr. Close it (or press Ctrl+C) to stop.")
    if os.environ.get("LOCALSEEDR_NO_BROWSER") != "1":
        import webbrowser
        threading.Timer(1.5, webbrowser.open, [url]).start()  # after the server is listening
    app.run(host=host, port=port, threaded=True)
