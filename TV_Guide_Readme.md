# Description

Sony’s K-65XR80M2 support/spec pages and the Dolby Vision Profiles and Levels PDF. For your BRAVIA 8 II 2025, the practical read is:

Display / Processing

4K OLED/QD-OLED panel, 3840 x 2160, 120 Hz refresh.
Sony lists XR Processor, XR TRILUMINOS Max, XR Contrast Booster 25, XR Clear Image, and XR OLED Motion.
HDR support is HDR10, HLG, and Dolby Vision, and the TV includes Dolby Vision picture modes.
Audio

Acoustic Surface Audio+ with actuator x2 and subwoofer x2, rated 15 W + 15 W + 10 W + 10 W.
Voice Zoom 3, Dolby Atmos, and DTS support are listed, including DTS:X and DTS-HD formats.
USB / Hard Disk playback

The help guide’s video table is explicitly for USB / Home Network playback.
USB storage formats supported are FAT16, FAT32, exFAT, and NTFS; USB HDD recording is not supported, but file playback from an external drive is.
For video files, Sony lists support for MPEG1, MPEG2, AVC/H.264, HEVC/H.265 Main10, MPEG4, Motion JPEG, VP8, and VP9 across common containers like MPG, M2TS, MP4, MKV, MOV, AVI, 3GP, and WebM.
Practical limits: many legacy formats top out at 1080p, while the HEVC/MP4/MKV rows go to 4K60, and some HEVC/XAVC rows go up to 4K120; external subtitles are supported, including .srt and .ass/.ssa.
For audio tracks in files, Sony lists AAC-LC, HE-AAC, AC3, E-AC3, AC4, DTS core, Vorbis, WMA9, LPCM, and PCM variants, with sample-rate limits called out separately.
Dolby Vision profile takeaway

Dolby’s PDF says the active profile families are 5, 7, 8, 9, 10, and 20; profiles 0, 1, 2, 3, 4, and 6 are legacy or not for new apps.
In practice, the important ones for local files are usually profile 5 and the single-layer profile 8/10 families, while profile 7 is the Blu-ray dual-layer family and profile 20 is the 3D/cinema-sourced family.
Sony confirms Dolby Vision support at the TV level, but its USB playback docs do not publish a separate Dolby Vision profile matrix, so whether a given file plays depends on the exact container, codec, and DV profile.
The Dolby PDF also notes that Dolby Vision levels are separate from AVC/HEVC levels, so you should not compare them directly.

For USB playback on the BRAVIA 8 II, Sony documents:

Display/processing: 4K OLED, 3840 x 2160, 120 Hz, XR Processor, XR TRILUMINOS Max, XR Contrast Booster 25, XR Clear Image, XR OLED Motion.
Audio: Acoustic Surface Audio+ with actuator x2 and subwoofer x2, 15 W + 15 W + 10 W + 10 W, Voice Zoom 3, Dolby Atmos, DTS:X.
USB/media: 2 side USB ports, USB drive formats FAT16/FAT32/exFAT/NTFS, no USB HDD recording, and external subtitles support for .srt, .ass, and .ssa.
Supported file types: MPG/MPEG, M2TS/MTS, MP4, MKV, MOV, AVI, ASF/WMV, 3GP/3G2, and WebM.
Main video codecs in those files: AVC/H.264, HEVC/H.265 Main10, VP8, VP9, plus legacy MPEG formats.
For Dolby Vision, Sony does not publish a USB-specific profile whitelist. Based on Dolby’s profile families and Sony’s USB codec matrix, the best bets are HEVC-based Dolby Vision files, especially profile 5 and profile 8.x in a supported container. I would not count on profile 7 Blu-ray dual-layer, profile 9 AVC-based DV, profile 10 AV1-based DV, or profile 20 3D/cinema content over USB. Dolby Vision levels are separate from codec levels, so panel support for 4K120 does not automatically mean every DV file at that level will play from USB.

For accuracy, use Sony’s built-in USB video player first. That is the documented playback path and the most reliable way to get faithful output. Download an external app only if you need a different file browser or subtitle workflow, or if the native player rejects the file. An external app will not add unsupported Dolby Vision profile support.

Best practical setup for pure 4K Dolby Vision off USB on your BRAVIA 8 II:

Use the Sony built-in player first. It is the most reliable path for the TV’s native Dolby Vision pipeline, and Sony’s own USB docs already cover the drive formats and HEVC playback you need.
If you want a downloadable app, use Just Player second. Its official README says HDR10+ and Dolby Vision playback on compatible hardware, and it explicitly notes Dolby Vision profile 7 playback as HDR HEVC.
Use Vimu if you care more about browsing, subtitles, SMB/NFS/USB convenience, and refresh-rate matching. The docs I checked did not publish an explicit Dolby Vision profile matrix, so I would not pick it before Just Player for DV-first playback.
Put Kodi last for this use case. Kodi’s official wiki focuses more on general playback and HDR-to-SDR tone mapping than on reliable local Dolby Vision passthrough, so it is not the accuracy-first choice.
Best profiles and containers for USB:

Best: Dolby Vision profile 5, single-layer HEVC, ideally in MP4.
Also good: profile 8.x single-layer HEVC, preferably MP4; MKV can work, but it is less predictable for DV triggering.
Not ideal: profile 7 if you want true DV metadata preserved end to end. Some players treat it as HDR HEVC/base-layer playback instead.
Avoid for this use case: profiles 9, 10, and 20.
Bottom line: remux to MP4 if you can, target profile 5 or 8.x, and use the Sony built-in player first. Install Just Player only if you need a fallback or better file/subtitle handling.

Sony does not appear to publish a USB Dolby Vision profile whitelist for this model, so this is the practical ranking from the TV’s USB docs, Dolby’s profile spec, and the players’ own documentation.
