# Atomi Dash Cam Auto-Downloader

Download video clips from an Atomi Smart Dash Cam (4K Dual Lens, model
AT032024 / AT2091 and similar) over the camera's WiFi hotspot, to a Linux
machine. Written in Python because it ships preinstalled on Fedora, has
the best HTTP and file handling for this kind of glue script, and is easy
to read and extend.

## Why this exists

The Atomi dash cam creates its own WiFi hotspot. The official way to get
videos off it is the Atomi Dash phone app. That works, but it is manual:
turn on WiFi at the camera, join the hotspot, open the app, tap through
to download each clip. This script automates the download so a Linux box
near the car pulls new clips on its own.

Bluetooth does not work for this. The camera lists WiFi and USB only,
and Bluetooth is far too slow for video. WiFi is the path.

## How it works

1. Turn on WiFi on the dash cam (on the camera itself). The screen shows
   the hotspot SSID and password.
2. Run the probe (read-only, downloads nothing). It finds the camera and
   lists every endpoint that answers with a file list:

   ```sh
   python3 atomi_download.py --probe
   ```

   Atomi publishes no HTTP API, so the script probes the common dashcam
   endpoints plus plain directory listings. If the probe finds a listing,
   downloads are fully automatic. If it finds nothing, the camera's file
   server is not reachable over plain HTTP and the fallback is the Atomi
   Dash app (or Waydroid on Linux).

3. Download new clips (skips files already downloaded):

   ```sh
   python3 atomi_download.py --dest ~/dashcam
   ```

4. Full automatic mode on a box with one WiFi adapter: join the dashcam
   hotspot, download, then rejoin home WiFi and sync. Provide the SSIDs
   and let `nmcli` do the switching:

   ```sh
   python3 atomi_download.py --dest ~/dashcam \
       --cam-ssid ATOMI_DASH_XXXX --cam-pass <hotspot-password> \
       --home-ssid MyHomeWiFi
   ```

   The hotspot password is shown on the dash cam screen when WiFi is on.
   Prefer the `ATOMI_CAM_PASS` environment variable over the command
   line so the password does not land in shell history.

The script stores a manifest (`downloaded.json`) in the destination
directory and only fetches files it has not seen before.

## Requirements

- Python 3.8+ (ships with Fedora).
- `requests` (`pip install requests`) for HTTP, or the script falls back
  to `urllib` from the standard library if `requests` is missing.
- `nmcli` (NetworkManager) only if you use `--cam-ssid` / `--home-ssid`
  for WiFi switching. Not needed if the machine is already on the
  dashcam hotspot.

## Options

| Flag | What it does |
| --- | --- |
| `--cam-ip` | Camera IP (default: auto-detect the WiFi gateway) |
| `--dest` | Destination directory (default: `~/dashcam`) |
| `--probe` | Probe only: list endpoints that answer, download nothing |
| `--cam-ssid` | Dashcam hotspot SSID (for nmcli switching) |
| `--cam-pass` | Dashcam hotspot password (or `ATOMI_CAM_PASS` env) |
| `--home-ssid` | Home WiFi SSID to rejoin after download |
| `--verbose` / `-v` | Debug logging |

## Notes

- The dashcam WiFi has to be on at the camera. That part does not
  change. If the camera is hardwired with parking mode, WiFi may stay
  available; otherwise enable it each time the car is on.
- One WiFi adapter can only join one network at a time. The `--home-ssid`
  step rejoins home WiFi after the download so the box is back on your
  network. With two adapters (or Ethernet plus WiFi) the box can stay on
  both.
- Sync the downloaded clips to your phone with Syncthing, or to your
  home server with `rsync`.

## Blog post

Full write-up with the reasoning, the Bluetooth vs WiFi answer, and the
three automation options:

https://www.sjultra.com/automate-atomi-dash-cam-video-downloads/

## License

MIT. See LICENSE.
