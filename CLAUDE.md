# Multi Magnet Link Comparison — Claude Code Development Specs

## Goal

Extend the existing local multi-file comparison system to support:

1. Single magnet link analysis
2. Multi magnet link comparison
3. Metadata extraction
4. Dolby Vision/HDR validation
5. Codec/container analysis
6. Torrent source quality scoring
7. Side-by-side comparison UI
8. Batch processing
9. Verification pipeline
10. Exportable reports

This should work WITHOUT downloading the full media file.

---

# High-Level Architecture

```text
Frontend (React/Vite)
    ↓
FastAPI Backend
    ↓
Magnet Resolver Layer
    ↓
Torrent Metadata Extractor
    ↓
Media Probe Layer
    ↓
Comparison Engine
    ↓
Scoring + Verification Engine
    ↓
UI Results + JSON Export
```

---

# Core Features

## 1. Single Magnet Analysis

Input:

```text
magnet:?xt=urn:btih:...
```

Output:

* Torrent Name
* Info Hash
* Trackers
* File List
* File Sizes
* Video Codec
* Audio Codec
* Container
* Resolution
* HDR Format
* Dolby Vision Profile
* Bitrate
* Source Platform
* Release Group
* MediaInfo Summary
* DV/HDR Validation
* Quality Score

---

# 2. Multi Magnet Comparison

Input:

```text
[
  magnet1,
  magnet2,
  magnet3
]
```

Output:

| Feature            | Torrent A | Torrent B |
| ------------------ | --------- | --------- |
| Size               |           |           |
| Container          |           |           |
| DV Profile         |           |           |
| HDR10+             |           |           |
| Audio              |           |           |
| Bitrate            |           |           |
| Source             |           |           |
| Streaming Platform |           |           |
| IMAX               |           |           |
| Atmos              |           |           |
| Recommendation     |           |           |

---

# Important Requirement

The system MUST support comparing:

* WEB-DL vs WEBRip
* DV vs HDR10+
* MKV vs MP4
* Different release groups
* Different streaming platforms
* Hybrid releases
* Fake DV releases
* Re-encoded releases
* Single layer vs dual layer DV

---

# Backend Implementation

# Stack

## Python Dependencies

```bash
pip install fastapi uvicorn qbittorrent-api torf libtorrent requests pymediainfo guessit rapidfuzz
```

Optional:

```bash
pip install enzyme mkvtoolnix-python ffmpeg-python
```

---

# Folder Structure

```text
backend/
│
├── app/
│   ├── main.py
│   ├── routes/
│   │   ├── magnet.py
│   │   ├── compare.py
│   │   └── analyze.py
│   │
│   ├── services/
│   │   ├── magnet_parser.py
│   │   ├── torrent_metadata.py
│   │   ├── mediainfo_service.py
│   │   ├── dv_parser.py
│   │   ├── hdr_parser.py
│   │   ├── comparison_engine.py
│   │   ├── scoring_engine.py
│   │   └── recommendation_engine.py
│   │
│   ├── models/
│   │   ├── magnet_models.py
│   │   └── comparison_models.py
│   │
│   └── utils/
│       ├── regex_patterns.py
│       ├── normalization.py
│       └── formatting.py
│
└── temp/
```

---

# Phase 1 — Magnet Parsing

## Objective

Extract:

* info hash
* display name
* trackers
* encoded metadata

## Example Output

```json
{
  "name": "Avatar.Fire.And.Ash.2025.2160p.AMZN.WEB-DL.DV.HDR10+",
  "info_hash": "7A156901B9E18B266BB7ED78304E42763D809BD4",
  "trackers": [
    "udp://tracker.opentrackr.org:1337/announce"
  ]
}
```

---

# Suggested Implementation

## magnet_parser.py

```python
from urllib.parse import parse_qs, urlparse, unquote


def parse_magnet(magnet: str):
    query = urlparse(magnet).query
    params = parse_qs(query)

    xt = params.get("xt", [""])[0]
    dn = unquote(params.get("dn", [""])[0])
    trackers = params.get("tr", [])

    info_hash = xt.split(":")[-1]

    return {
        "name": dn,
        "info_hash": info_hash,
        "trackers": trackers,
    }
```

---

# Phase 2 — Torrent Metadata Retrieval

## Goal

Retrieve torrent metadata WITHOUT downloading full media.

Possible Approaches:

1. qBittorrent Web API
2. libtorrent metadata fetch
3. DHT metadata retrieval

---

# Recommended Approach

Use:

```python
libtorrent
```

because:

* lightweight
* no full download required
* direct metadata extraction
* better automation

---

# Metadata Flow

```text
Magnet Link
    ↓
Metadata Handshake
    ↓
Torrent Metadata
    ↓
File Tree
    ↓
Largest Video File
    ↓
Partial Download (optional)
    ↓
MediaInfo
```

---

# Phase 3 — MediaInfo Extraction

# Goal

Extract:

* Codec
* Profile
* Bit depth
* HDR format
* DV profile
* Audio channels
* Atmos
* Frame rate
* Bitrate
* Resolution
* Container

---

# Critical Requirement

The app MUST detect:

| Type            | Detect |
| --------------- | ------ |
| HDR10           | Yes    |
| HDR10+          | Yes    |
| Dolby Vision    | Yes    |
| DV Profile      | Yes    |
| Atmos           | Yes    |
| DTS:X           | Yes    |
| Hybrid Releases | Yes    |
| FEL/MEL         | Yes    |

---

# Recommended Tools

## MediaInfo CLI

```bash
mediainfo file.mkv
```

## FFprobe

```bash
ffprobe file.mkv
```

## dovi_tool

For Dolby Vision parsing.

---

# Dolby Vision Parsing

## Detect These

| Profile | Meaning                   |
| ------- | ------------------------- |
| 5       | Streaming DV              |
| 7       | UHD BluRay DV             |
| 8       | HDR Compatible DV         |
|         |                           |
| MEL     | Minimum Enhancement Layer |
| FEL     | Full Enhancement Layer    |

---

# Example Detection Output

```json
{
  "dolby_vision": true,
  "dv_profile": 8,
  "dv_layer": "MEL",
  "hdr10_compatible": true
}
```

---

# Phase 4 — Filename Intelligence

# Goal

Extract intelligence directly from release name.

Example:

```text
Avatar.Fire.And.Ash.2025.2160p.AMZN.WEB-DL.DV.HDR10+.DDP5.1.Atmos.H265.MP4-BTM
```

Should extract:

| Field      | Value  |
| ---------- | ------ |
| Platform   | AMZN   |
| Source     | WEB-DL |
| Resolution | 2160p  |
| DV         | true   |
| HDR10+     | true   |
| Audio      | DDP5.1 |
| Atmos      | true   |
| Codec      | H265   |
| Container  | MP4    |
| Group      | BTM    |

---

# Recommendation

Use:

```python
guessit
```

PLUS

custom regex patterns.

---

# Phase 5 — Comparison Engine

# Goal

Generate intelligent side-by-side comparisons.

---

# Comparison Categories

## Video

* Codec
* Bitrate
* Bit depth
* Resolution
* HDR support
* DV profile
* Frame rate

## Audio

* Codec
* Channels
* Atmos
* DTS:X
* Bitrate

## Container

* MKV
* MP4

## Source

* WEB-DL
* WEBRip
* BluRay
* Remux

## Release Quality

* Trusted group
* Bitrate efficiency
* Metadata integrity
* Fake DV detection

---

# Example Output

```json
{
  "winner": "Torrent B",
  "reasons": [
    "Higher bitrate",
    "HDR10+ support",
    "Atmos audio",
    "Better DV compatibility"
  ]
}
```

---

# Phase 6 — Verification Engine

# Goal

Detect fake or misleading releases.

---

# Detect

| Problem           | Detection                 |
| ----------------- | ------------------------- |
| Fake DV           | DV tag but no DV metadata |
| Re-encoded WEB-DL | Low bitrate               |
| Wrong HDR label   | Metadata mismatch         |
| Fake Atmos        | Track metadata mismatch   |
| Broken DV profile | Invalid RPU               |

---

# Suggested Logic

```python
if "DV" in filename and not mediainfo.dv_detected:
    flags.append("Fake Dolby Vision")
```

---

# Phase 7 — Recommendation Engine

# Goal

Recommend best release.

---

# Scoring System

| Metric                  | Weight |
| ----------------------- | ------ |
| Video Quality           | 40     |
| Audio Quality           | 25     |
| HDR/DV                  | 20     |
| Source Quality          | 10     |
| Container/Compatibility | 5      |

---

# Example Recommendation

```text
Recommended Release:

Avatar.Fire.And.Ash.2025.2160p.AMZN.WEB-DL.DV.HDR10+.DDP5.1.Atmos.H265.MP4-BTM

Reason:
- Better HDR compatibility
- Atmos support
- More efficient encoding
- Better streaming device compatibility
```

---

# Frontend Requirements

# Stack

```text
React + Vite + Tailwind + shadcn/ui
```

---

# Main Screens

## 1. Magnet Input Page

Features:

* Paste single magnet
* Paste multiple magnets
* Auto parsing
* Validation
* Batch import

---

## 2. Comparison Dashboard

Features:

* Side-by-side cards
* Quality indicators
* DV/HDR badges
* Recommendation banner
* Expandable MediaInfo
* Score visualization

---

## 3. Advanced Technical View

Features:

* Full MediaInfo
* HDR metadata
* Dolby Vision metadata
* Audio object metadata
* Validation flags

---

# UI Suggestions

## Use Badges

```text
DV
HDR10+
Atmos
WEB-DL
REMUX
```

---

# Use Color Indicators

| Color  | Meaning    |
| ------ | ---------- |
| Green  | Best       |
| Yellow | Acceptable |
| Red    | Problem    |

---

# API Design

# POST /analyze-magnet

Input:

```json
{
  "magnet": "magnet:?xt=..."
}
```

Output:

```json
{
  "success": true,
  "data": {}
}
```

---

# POST /compare-magnets

Input:

```json
{
  "magnets": [
    "magnet1",
    "magnet2"
  ]
}
```

Output:

```json
{
  "comparison": {},
  "recommendation": {}
}
```

---

# Testing Strategy

# Use These Real-World Cases

## Case 1

Compare:

* WEB-DL DV HDR
* WEB-DL DV HDR10+

Expected:

* HDR10+ detection difference
* MP4 vs MKV difference
* Source/platform comparison

---

# Required Validation Tests

| Test               | Expected      |
| ------------------ | ------------- |
| Invalid magnet     | handled       |
| Missing trackers   | handled       |
| No metadata peers  | retry         |
| Fake DV            | flagged       |
| Broken MediaInfo   | graceful fail |
| Duplicate torrents | merged        |

---

# Performance Optimizations

## Cache Metadata

Avoid repeated metadata retrieval.

Use:

```python
redis
```

optional.

---

# Async Metadata Fetching

Use:

```python
asyncio
```

for batch comparison.

---

# Security Considerations

## Validate Magnet Links

Prevent:

* malformed URLs
* extremely long inputs
* injection attacks

---

# DO NOT

* auto-download copyrighted media
* auto-seed torrents
* expose local filesystem

---

# Advanced Future Features

## 1. Screenshot Comparison

Extract:

* tone mapping differences
* bitrate artifacts
* black crush
* color banding

---

## 2. AI Quality Estimation

Predict:

* re-encode quality
* compression damage
* sharpening artifacts

---

## 3. Streaming Platform Database

Map:

* AMZN
* DSNP
* ATVP
* NF
* HMAX

to known encoding characteristics.

---

# Suggested Implementation Order

## Phase Order

### Phase 1

* Magnet parser
* Input validation

### Phase 2

* Metadata retrieval
* File listing

### Phase 3

* MediaInfo extraction
* DV parsing

### Phase 4

* Comparison engine
* Scoring system

### Phase 5

* Frontend UI
* Batch comparison

### Phase 6

* Verification engine
* Fake release detection

### Phase 7

* Export system
* Reports

---

# Immediate First Task

Implement:

```python
parse_magnet()
```

Then:

1. extract torrent metadata
2. get file list
3. identify primary video file
4. run mediainfo
5. compare outputs

---

# Expected Comparison For Provided Test Cases

## Magnet A

Expected likely traits:

* iTunes WEB-DL
* MKV container
* DV + HDR
* H.265
* Atmos

## Magnet B

Expected likely traits:

* Amazon WEB-DL
* MP4 container
* DV + HDR10+
* Better compatibility
* Atmos

---

# Recommendation Logic Example

```python
if release.has_hdr10_plus:
    score += 10

if release.container == "MKV":
    score += 2

if release.audio_atmos:
    score += 8
```

---

# Claude Code Instructions

## Important

Claude Code should:

* implement incrementally
* test each phase independently
* create modular services
* avoid giant files
* use typed Pydantic models
* maintain clean async architecture
* add structured logging
* add retry handling for metadata retrieval

---

# Verification Checklist

## Must Verify

* Magnet parsing works
* Multi magnet comparison works
* Batch async processing works
* DV detection works
* HDR10+ detection works
* Atmos detection works
* Recommendation engine works
* Invalid links handled
* UI renders comparisons properly
* Export works

---

# Final Goal

Build a professional-grade torrent metadata comparison system capable of:

* advanced HDR/DV analysis
* intelligent release recommendation
* fake release detection
* automated metadata validation
* side-by-side comparison
* scalable batch processing

WITHOUT downloading entire media files.
