#!/usr/bin/env python3
"""
Atomi Dash Cam auto-downloader for Linux.

Downloads video clips from an Atomi Smart Dash Cam (4K Dual Lens, model
AT032024 / AT2091 and similar) over the camera's WiFi hotspot, to a Linux
machine (Fedora, Ubuntu, or any distro with Python 3 + nmcli).

Why Python: it ships preinstalled on Fedora, has the best HTTP and file
handling for this kind of glue script, and is easy to read and extend.
Bash gets messy once you parse JSON/HTML listings and handle retries;
a compiled language is overkill for a downloader.

How the camera works
--------------------
The Atomi dash cam creates its own WiFi hotspot. The official way to get
videos off it is the Atomi Dash phone app (com.adse.atomi). Atomi publishes
no HTTP API, so this script probes the camera's web server for a file
listing the same way the app does. If the probe finds a listing, downloads
are fully automatic. If the probe finds nothing, the camera's file server
is not reachable over plain HTTP and you fall back to the app (or to
Waydroid on Linux).

Usage
-----
1. Turn on WiFi on the dash cam (on the camera itself). The screen shows
   the hotspot SSID and password.
2. Run the probe first (read-only, downloads nothing):

       python3 atomi_download.py --probe

   This prints the camera IP and every endpoint that answered, so you can
   see whether the camera serves a file listing.

3. Download new clips (skips files already downloaded):

       python3 atomi_download.py --dest ~/dashcam

4. Full automatic mode on a box with one WiFi adapter: join the dashcam
   hotspot, download, then rejoin home WiFi and sync. Provide the SSIDs
   and let nmcli do the switching:

       python3 atomi_download.py --dest ~/dashcam \\
           --cam-ssid ATOMI_DASH_XXXX --cam-pass <hotspot-password> \\
           --home-ssid MyHomeWiFi

   The hotspot password is shown on the dash cam screen when WiFi is on.
   Never put the password on the command line in shared history; prefer
   a config file or the ATOMI_CAM_PASS environment variable.

   The script stores a manifest (downloaded.json) in the destination
   directory and only fetches files it has not seen before.

Requirements
------------
- Python 3.8+ (ships with Fedora).
- `requests` (pip install requests) for HTTP, or the script falls back
  to urllib from the standard library if requests is missing.
- `nmcli` (NetworkManager) only if you use --cam-ssid / --home-ssid
  for WiFi switching. Not needed if the machine is already on the
  dashcam hotspot.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
except ImportError:  # Fall back to urllib when requests is absent.
    requests = None

import urllib.request
import urllib.error

LOG = logging.getLogger("atomi_download")

# Candidate endpoints the camera's web server might answer on.
# Atomi publishes no API, so probe the common dashcam patterns plus a
# plain directory listing. The first endpoint that returns a usable file
# list wins. Add the endpoint your camera answers on here if you find one.
PROBE_ENDPOINTS = [
    # Generic file listings / DCIM-style paths.
    "/",
    "/DCIM/",
    "/DCIM/100MEDIA/",
    "/SD/",
    "/sd/",
    "/mnt/sd/",
    "/card/",
    "/files/",
    "/list/",
    "/api/list",
    "/api/files",
    # Novatek-style CGI (common in budget dash cams).
    "/cgi-bin/Config.cgi?action=get&property=Camera.Preview.MJPEG",
    "/cgi-bin/list.cgi",
    "/cgi-bin/filelist.cgi",
    "/cgi-bin/Dir.cgi",
    # Ambarella-style.
    "/cgi-bin/get_file_list.cgi",
    "/cgi-bin/download.cgi",
    # Generic JSON.
    "/json/list",
    "/v1/files",
]

# File extensions to download.
MEDIA_EXTS = {".mp4", ".mov", ".avi", ".jpg", ".jpeg", ".thm"}

# Default camera IPs to try (gateway of the hotspot is usually .1).
DEFAULT_CAM_IPS = ["192.168.1.1", "192.168.0.1", "192.168.43.1", "192.168.2.1"]

TIMEOUT = 10          # seconds per HTTP request
MAX_RETRIES = 3       # download retries per file
RETRY_DELAY = 5       # seconds between retries


def http_get(url):
    """GET a URL. Returns (status, bytes) or (None, None) on failure."""
    if requests is not None:
        try:
            r = requests.get(url, timeout=TIMEOUT)
            return r.status_code, r.content
        except requests.RequestException as exc:
            LOG.debug("GET %s failed: %s", url, exc)
            return None, None
    # urllib fallback.
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "atomi_download/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except (urllib.error.URLError, OSError) as exc:
        LOG.debug("GET %s failed: %s", url, exc)
        return None, None


def gateway_ip():
    """Return the default gateway IP (the dashcam when on its hotspot)."""
    try:
        out = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    except (subprocess.SubprocessError, OSError) as exc:
        LOG.debug("gateway lookup failed: %s", exc)
    return None


def find_camera(cam_ip=None):
    """Return the camera base URL, or None if no candidate answers."""
    candidates = []
    if cam_ip:
        candidates.append(cam_ip)
    gw = gateway_ip()
    if gw and gw not in candidates:
        candidates.append(gw)
    for ip in DEFAULT_CAM_IPS:
        if ip not in candidates:
            candidates.append(ip)
    for ip in candidates:
        url = f"http://{ip}/"
        status, _ = http_get(url)
        if status is not None:
            LOG.info("camera answered at %s (HTTP %s)", url, status)
            return f"http://{ip}"
    return None


def extract_files(base_url, path, content):
    """
    Extract downloadable file paths from a listing response.
    Handles JSON lists and HTML directory listings.
    Returns a list of URL paths (e.g. '/DCIM/100MEDIA/FILE0001.MP4').
    """
    files = []
    text = content.decode("utf-8", errors="ignore")
    # JSON: look for .mp4/.jpg paths or filenames.
    try:
        data = json.loads(text)
        def walk(node):
            if isinstance(node, dict):
                for key in ("path", "name", "file", "url", "filename"):
                    val = node.get(key)
                    if isinstance(val, str) and Path(val).suffix.lower() in MEDIA_EXTS:
                        files.append(val)
                for val in node.values():
                    walk(val)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
            elif isinstance(node, str) and Path(node).suffix.lower() in MEDIA_EXTS:
                files.append(node)
        walk(data)
        if files:
            return files
    except (json.JSONDecodeError, ValueError):
        pass
    # HTML: hrefs ending in a media extension.
    for href in re.findall(r'href=["\']([^"\']+)["\']', text, flags=re.I):
        if Path(href).suffix.lower() in MEDIA_EXTS:
            files.append(href)
    # Plain text: bare filenames / paths.
    for token in re.findall(r'[\w\-/\.]+(?:\.mp4|\.mov|\.avi|\.jpg|\.jpeg)', text, flags=re.I):
        files.append(token)
    # Deduplicate, keep order.
    seen = set()
    out = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def probe(base_url):
    """Probe the camera for a file listing. Returns {endpoint: [files]}."""
    found = {}
    for endpoint in PROBE_ENDPOINTS:
        url = urljoin(base_url + "/", endpoint.lstrip("/"))
        status, content = http_get(url)
        if status is None or content is None:
            continue
        files = extract_files(base_url, endpoint, content)
        if files:
            found[endpoint] = files
            LOG.info("endpoint %s returned %d file(s)", endpoint, len(files))
        else:
            LOG.debug("endpoint %s answered HTTP %s but no file list", endpoint, status)
    return found


def download_file(url, dest_path):
    """Download one file with retries. Returns True on success."""
    for attempt in range(1, MAX_RETRIES + 1):
        status, content = http_get(url)
        if status == 200 and content:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest_path.with_suffix(dest_path.suffix + ".part")
            tmp.write_bytes(content)
            tmp.replace(dest_path)
            return True
        LOG.warning("download failed (attempt %d/%d): %s", attempt, MAX_RETRIES, url)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    return False


def wifi_connect(ssid, password=None):
    """Join a WiFi network with nmcli. Returns True on success."""
    cmd = ["nmcli", "device", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            LOG.info("joined WiFi %s", ssid)
            return True
        LOG.warning("nmcli connect %s failed: %s", ssid, r.stderr.strip())
    except (subprocess.SubprocessError, OSError) as exc:
        LOG.warning("nmcli failed: %s", exc)
    return False


def load_manifest(dest):
    mf = dest / "downloaded.json"
    if mf.exists():
        try:
            return json.loads(mf.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_manifest(dest, manifest):
    (dest / "downloaded.json").write_text(json.dumps(manifest, indent=2))


def main():
    ap = argparse.ArgumentParser(
        description="Download videos from an Atomi dash cam over WiFi.")
    ap.add_argument("--cam-ip", help="Camera IP (default: auto-detect gateway)")
    ap.add_argument("--dest", default=str(Path.home() / "dashcam"),
                    help="Destination directory (default: ~/dashcam)")
    ap.add_argument("--probe", action="store_true",
                    help="Probe only: list endpoints that answer, download nothing")
    ap.add_argument("--cam-ssid", help="Dashcam hotspot SSID (for nmcli switching)")
    ap.add_argument("--cam-pass", default=os.environ.get("ATOMI_CAM_PASS"),
                    help="Dashcam hotspot password (or ATOMI_CAM_PASS env)")
    ap.add_argument("--home-ssid", help="Home WiFi SSID to rejoin after download")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    dest = Path(args.dest).expanduser()

    # Optional: join the dashcam hotspot first.
    if args.cam_ssid:
        if not wifi_connect(args.cam_ssid, args.cam_pass):
            LOG.error("could not join dashcam hotspot %s", args.cam_ssid)
            return 1
        time.sleep(3)  # Let DHCP settle.

    base_url = find_camera(args.cam_ip)
    if not base_url:
        LOG.error("no camera answered. Is the dashcam WiFi on and is this "
                  "machine on the dashcam hotspot?")
        if args.home_ssid:
            wifi_connect(args.home_ssid)
        return 1
    LOG.info("camera at %s", base_url)

    listings = probe(base_url)
    if not listings:
        LOG.error("camera answered but no endpoint returned a file list. "
                  "The camera may need the Atomi Dash app, or its file "
                  "server uses a different endpoint. Try opening %s in a "
                  "browser while on the hotspot and note what loads.",
                  base_url)
        if args.home_ssid:
            wifi_connect(args.home_ssid)
        return 2

    if args.probe:
        for endpoint, files in listings.items():
            print(f"\n{endpoint} ({len(files)} files):")
            for f in files[:20]:
                print(f"  {f}")
            if len(files) > 20:
                print(f"  ... and {len(files) - 20} more")
        if args.home_ssid:
            wifi_connect(args.home_ssid)
        return 0

    # Download new files.
    dest.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(dest)
    downloaded = 0
    skipped = 0
    failed = 0
    for endpoint, files in listings.items():
        for rel in files:
            name = Path(rel).name
            # Skip files already downloaded (by name + size if known).
            if name in manifest:
                skipped += 1
                continue
            url = rel if rel.startswith("http") else urljoin(base_url + "/", rel.lstrip("/"))
            target = dest / name
            LOG.info("downloading %s", name)
            if download_file(url, target):
                manifest[name] = {"size": target.stat().st_size, "url": url}
                downloaded += 1
            else:
                LOG.error("failed: %s", name)
                failed += 1
    save_manifest(dest, manifest)
    LOG.info("done: %d downloaded, %d skipped (already have), %d failed -> %s",
             downloaded, skipped, failed, dest)

    # Optional: rejoin home WiFi so the box is back on the home network.
    if args.home_ssid:
        wifi_connect(args.home_ssid)

    return 0 if failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
