# Bench-testing the ADS-B receiver on a dedicated Pi (e.g. Pi 5)

How to bring up and bench-test the ADS-B IN receiver stack after moving the
Nooelec NESDR Nano 2 dongles to a standalone receiver Pi. The **display** Pi
needs no changes — it just listens for GDL90 on UDP :4000 (see `shared/adsb.py`).
The receiver Pi runs the decoders + bridges that broadcast to :4000.

```
1090ES  ── NESDR ──► readsb ──────────── SBS :30003 ─► adsb_gdl90_bridge.py ─┐
978 UAT ── NESDR ──► dump978 ─┬─ raw :30978 ─► readsb (uat_in, traffic) ─────┤  GDL90/UDP
                              └─ raw :30978 ─► dump978_gdl90_bridge.py ───────┘  :4000 (broadcast)
                                              (FIS-B weather, uplink frames)      → display
```

## Why a separate Pi changes one thing

When the receiver lived on the *same* Pi as the display, the bridge's broadcast
reached the listener over loopback. On a dedicated receiver Pi the bridge
broadcasts to `255.255.255.255:4000`, so **both Pis must be on the same subnet**
and nothing may block UDP 4000. That's the only networking gotcha.

## Bring-up (receiver Pi)

```bash
git clone https://github.com/hbdrew/pfd-and-ahrs.git ~/PFD-and-AHRS
cd ~/PFD-and-AHRS
sudo bash tools/install_adsb.sh        # rtl-sdr + readsb (1090) + GDL90 bridge
sudo bash tools/install_dump978.sh     # dump978 (978 UAT) + FIS-B weather bridge
sudo bash tools/enable_978_traffic.sh  # fold 978 *traffic* into readsb's :30003
sudo reboot                            # so the DVB-T driver blacklist takes effect
```

Set each dongle's USB serial once so each decoder grabs the right band:

```bash
rtl_eeprom -d 0 -s 1090      # dongle on the 1090 antenna
rtl_eeprom -d 1 -s 978       # dongle on the 978 antenna
```

> **Decoder choice:** the 1090 decoder is **readsb** (wiedehopf's installer),
> not FlightAware's `dump1090-fa`. `dump1090-fa` isn't reliably packaged for
> current Raspberry Pi OS (Debian trixie / Pi 5 — the pinned piaware-repository
> `.deb` 404s). readsb serves the same SBS feed on :30003, so the bridge is
> identical, and readsb is built with UAT support for the 978-traffic fold-in.

## Bench test — work outward, each layer isolates a failure

### Layer 0 — software path, no antennas
```bash
python3 tools/adsb_gdl90_bridge.py --selftest    # bridge encode/decode round-trip
python3 shared/test_gdl90.py                      # GDL90 framing / CRC
python3 shared/test_adsb.py                       # listener + UDP loopback + geometry
```

### Layer 1 — SDR hardware alive
```bash
lsusb                 # both RTL2832U dongles enumerate
rtl_test -t           # expect SN: 1090 and SN: 978
```
`No E4000 tuner found, aborting` and `[R82XX] PLL not locked!` from `rtl_test -t`
are **normal** for R820T tuners — that test only probes for the old E4000. The
two devices enumerating with the right serials is the pass.

### Layer 2 — decoders running, pinned to the right dongle
```bash
systemctl status readsb dump978-978 dump978-gdl90
grep RECEIVER_OPTIONS /etc/default/readsb         # --device 1090
sudo ss -tlnp | grep -E '30003|30978'             # readsb SBS + dump978 raw listening
```
`readsb` SBS on :30003 is what the bridge consumes; dump978 raw on :30978 feeds
both the weather bridge and (via the `uat_in` connector) readsb for 978 traffic.
tar1090 web map: `http://<receiver-pi>/`.

### Layer 3 — bridge broadcasting to :4000
```bash
systemctl status adsb-gdl90.service
sudo tcpdump -n -i any udp port 4000              # packets leaving the receiver Pi
```

### Layer 4 — the display actually receives it (end-to-end)
On the display, the ADS-B status line reads `ADS-B <mode> R<n> I<n>` — watch the
**R** (radio) count climb. That single observation proves decode + LAN broadcast
+ listener all at once. (978 traffic shares the same **R** tally as 1090.)

Indoors you may see little/no real traffic — it's line-of-sight, not a fault.
To prove the whole path regardless of reception, inject guaranteed traffic from
a busy field (it emits the same GDL90/UDP and overrides the demo targets):
```bash
python3 tools/adsb_internet_feed.py --lat 33.94 --lon -118.40 --radius 80   # KLAX
```

## Troubleshooting notes (from a real Pi 5 bring-up)

- **`dump1090-fa.service` not-found / nothing on :30003:** no 1090 decoder was
  installed (only the 978 stack). The bridge runs but is *starved*. Install
  readsb (above) — it serves :30003 and the bridge reconnects on its retry loop.
- **readsb crash-loops right after `enable_978_traffic.sh`, log says
  `Error parsing the given command line parameters` with a literal `$NET_OPTIONS`
  in the invoked command:** the connector must live ON the existing
  `NET_OPTIONS=` line in `/etc/default/readsb`, not on a separate
  `NET_OPTIONS="$NET_OPTIONS …"` line — systemd's `EnvironmentFile=` does not
  expand `$NET_OPTIONS`. The current `enable_978_traffic.sh` edits it in place
  (and heals the old self-referential line). To fix by hand, append
  `--net-connector 127.0.0.1,30978,uat_in` inside the quotes of the real
  `NET_OPTIONS=` line, then `sudo systemctl restart readsb`.
- **Connector working:** `journalctl -u readsb` shows
  `UAT TCP input: Connection established: 127.0.0.1 port 30978`.

## NOTAMs (display Pi, not the receiver Pi)

NOTAMs are an internet source polled inside `pfd.py` (FAA NMS-API via
`shared/wx.py`), so credentials go on the **display** Pi. Easiest over SSH —
systemd env vars (leave the in-app NOTAM KEY/SECRET fields blank so these win):

```bash
sudo systemctl edit pfd.service
# In the drop-in (between the top comment markers):
#   [Service]
#   Environment="FAA_NOTAM_CLIENT_ID=your_client_id"
#   Environment="FAA_NOTAM_CLIENT_SECRET=your_client_secret"
#   Environment="FAA_NOTAM_ENV=preprod"
sudo systemctl restart pfd.service
```

Verify: MFD MET page → tap a station → NOTAM tab. **200 + empty list** = auth OK,
just no NOTAMs in the current view (pan to a busy area). **401** = key/secret or
ENV mismatch (pre-prod is the default and what most credentials are issued for).
