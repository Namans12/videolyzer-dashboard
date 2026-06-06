"""Ad-hoc verification for the Deep HDR10+ scan feature.

Stage 1: probe environment (file, tools, libtorrent, disk).
Stage 2: analyze the LOCAL file -> ground-truth HDR10+ / DV / container.
Stage 3 (optional, --magnet): run the magnet with enable_hdr10plus on/off,
         watching temp-drive free space to confirm sparse behaviour.
"""
from __future__ import annotations
import os, sys, shutil, tempfile, threading, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _disk_alloc(path: str) -> int:
    """Actual allocated-on-disk bytes for a (possibly sparse) file on Windows.
    Uses GetCompressedFileSizeW — returns physical allocation, not logical size.
    """
    if os.name != "nt":
        st = os.stat(path)
        return getattr(st, "st_blocks", 0) * 512
    import ctypes
    from ctypes import wintypes
    GetCompressedFileSizeW = ctypes.windll.kernel32.GetCompressedFileSizeW
    GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    GetCompressedFileSizeW.restype = wintypes.DWORD
    high = wintypes.DWORD(0)
    low = GetCompressedFileSizeW(path, ctypes.byref(high))
    return (high.value << 32) | low

LOCAL = r"D:\SORT\PHM\01. Project.Hail.Mary.2026.2160p.iT.WEB-DL.DV.HDR10+.MULTi[Ben The Men].mp4"
MAGNET = (
    "magnet:?xt=urn:btih:6D586B4534782952644758B6641AEA0BDBC373F2"
    "&dn=Project.Hail.Mary.2026.2160p.iT.WEB-DL.DV.HDR10%2B.MULTi.DDP5.1.Atmos.H265.MP4-BTM"
)


def stage_env():
    print("=== ENVIRONMENT ===")
    print("local exists:", os.path.isfile(LOCAL))
    if os.path.isfile(LOCAL):
        print("local size GB:", round(os.path.getsize(LOCAL) / 1024 ** 3, 2))
    for t in ("mediainfo", "ffmpeg", "ffprobe", "hdr10plus_tool", "dovi_tool"):
        print(f"  {t:16}", shutil.which(t) or "NOT ON PATH")
    try:
        import libtorrent as lt
        print("  libtorrent:", lt.version)
    except Exception as e:
        print("  libtorrent: MISSING", e)
    for d in ("C:\\", "D:\\"):
        try:
            u = shutil.disk_usage(d)
            print(f"  disk {d} free GB:", round(u.free / 1024 ** 3, 1))
        except OSError:
            pass
    print("  tempdir:", tempfile.gettempdir())


def stage_local():
    print("\n=== LOCAL ANALYSIS ===")
    from analysis import analyze_file
    r = analyze_file(LOCAL, skip_dovi_scan=False)
    keys = [
        "container", "codec", "resolution", "bit_depth", "hdr_format",
        "dolby_vision", "dv_profile", "hdr10_plus", "audio_codec",
        "audio_channels", "atmos", "bitrate_mbps", "file_size_gb",
        "tv_score", "score", "confidence",
    ]
    for k in keys:
        if k in r:
            print(f"  {k:16}: {r[k]}")
    h = r.get("hdr10_plus_scan") or {}
    print("  hdr10plus_scan  :", {k: h.get(k) for k in
          ("status", "confirmed", "frames", "slices_scanned", "slices_with_hdr10_plus")})
    print("  headline        :", h.get("headline"))
    return r


def stage_magnet(enable: bool):
    print(f"\n=== MAGNET (enable_hdr10plus={enable}) ===")
    # Redirect temp to D: (roomy) so a hypothetical sparse failure can't touch
    # C:, and so the watcher measures the drop on a drive with headroom.
    safe_tmp = r"D:\vlztmp"
    os.makedirs(safe_tmp, exist_ok=True)
    tempfile.tempdir = safe_tmp
    from magnet import run_magnet_job_threaded
    tmpdrive = os.path.splitdrive(safe_tmp)[0] + "\\"
    print("   temp ->", safe_tmp)
    free0 = shutil.disk_usage(tmpdrive).free
    peak_drop = [0]
    stop = [False]

    def watch():
        while not stop[0]:
            drop = free0 - shutil.disk_usage(tmpdrive).free
            if drop > peak_drop[0]:
                peak_drop[0] = drop
            time.sleep(0.5)

    # Also track the biggest *.mp4/*.mkv torrent file's allocated-on-disk size
    # (sparse check) vs its logical size, by scanning vlztmp during the run.
    import subprocess as _sp
    max_alloc = [0]; logical = [0]

    def alloc_watch():
        while not stop[0]:
            try:
                for root, _d, fnames in os.walk(safe_tmp):
                    for fn in fnames:
                        if fn.lower().endswith((".mp4", ".mkv")):
                            p = os.path.join(root, fn)
                            try:
                                logical[0] = max(logical[0], os.path.getsize(p))
                                # GetCompressedFileSize via fsutil is slow; use
                                # st_blocks-free approach: compare to du via
                                # `fsutil file queryallocatedranges` would be
                                # ideal but noisy. Use os.stat alloc on win:
                                a = _disk_alloc(p)
                                if a > max_alloc[0]:
                                    max_alloc[0] = a
                            except OSError:
                                pass
            except OSError:
                pass
            time.sleep(1.0)

    aw = threading.Thread(target=alloc_watch, daemon=True); aw.start()
    w = threading.Thread(target=watch, daemon=True); w.start()
    t0 = time.time()
    try:
        out = run_magnet_job_threaded(
            MAGNET, skip_dovi_scan=False,
            emit=lambda m: print("   [emit]", m),
            cancel_check=lambda: False,
            enable_hdr10plus=enable,
        )
    finally:
        stop[0] = True
    print(f"   elapsed: {time.time()-t0:.0f}s   peak temp-drive drop: "
          f"{peak_drop[0]/1024**2:.0f} MB")
    print(f"   torrent file: logical {logical[0]/1024**3:.2f} GB  vs  "
          f"allocated-on-disk {max_alloc[0]/1024**2:.0f} MB  "
          f"({'SPARSE OK' if max_alloc[0] < 2*1024**3 else 'NOT SPARSE!'})")
    facts = {f.get("label"): f.get("value") for f in out.get("analyses", [{}])[0].get("media_facts", [])} if out.get("analyses") else {}
    for a in out.get("analyses", []):
        cont = next((f.get("value") for f in a.get("media_facts", [])
                     if f.get("label") == "Container"), "?")
        print(f"   FILE {a.get('file')}")
        print(f"     container={cont} dv_profile={a.get('dv_profile')} "
              f"bl={a.get('bl')} rpu={a.get('rpu')} "
              f"bitrate={a.get('bitrate_mbps')} size={a.get('file_size_gb')}GB")
        print(f"     HDR10+  status={a.get('hdr10_plus_status')} "
              f"confirmed={a.get('hdr10_plus_confirmed')} "
              f"frames={a.get('hdr10_plus_frames')}")
    return out


if __name__ == "__main__":
    stage_env()
    if "--local" in sys.argv:
        stage_local()
    if "--magnet-off" in sys.argv:
        stage_magnet(False)
    if "--magnet-on" in sys.argv:
        stage_magnet(True)
