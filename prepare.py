#!/usr/bin/env python3
"""
이달의 사진 — 사진 준비 스크립트

하는 일
  1. 원본/ 폴더의 사진을 웹용 크기로 줄여서 photos/ 에 저장
  2. 아카이브용 작은 썸네일도 같이 생성
  3. 사이트가 읽는 manifest.json 생성

폴더 구조 (원본/ 안에 이렇게 넣으면 됨)
  원본/
    2026-09/
      필름/
        골목.jpg
        비 오는 날__정희록.jpg
      디지털/
      폰카/

파일 이름 규칙
  제목.jpg                     → 제목만
  제목__촬영자.jpg             → 밑줄 두 개로 촬영자 구분 (아카이브에만 표시)
  수상_제목.jpg                → 아카이브에서 수상작으로 표시

사용법
  python3 prepare.py                      # 가장 최근 달을 투표 대상으로, 마감 7일 뒤
  python3 prepare.py --days 10            # 마감 10일 뒤
  python3 prepare.py --deadline 2026-09-14
  python3 prepare.py --month 2026-09      # 투표 대상 달을 직접 지정
  python3 prepare.py --bake-only          # 수상작 선정 뒤(14~15일경), 사진은 안 건드리고
                                           # 하루 넘게 지난 수상작만 manifest.json 에 고정
"""

import argparse
import csv
import hashlib
import json
import re
import io
import sys
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from PIL import Image, ImageCms, ImageFilter, ImageOps
except ImportError:
    sys.exit("Pillow이 필요합니다. 터미널에서 실행하세요:\n\n    pip3 install Pillow\n")

# 아이폰 HEIC 사진 지원 (설치되어 있으면 자동으로 켜짐)
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_OK = True
except ImportError:
    HEIC_OK = False

KST = timezone(timedelta(hours=9))

SRC_DIR = Path("원본")
OUT_DIR = Path("photos")
MANIFEST = Path("manifest.json")
ROSTER_DIR = Path("명단")   # 촬영자 명단. 깃허브에 올리지 않습니다.

DISPLAY_MAX = 2200   # 투표 화면용 긴 변 픽셀
DISPLAY_Q = 88
THUMB_MAX = 800      # 아카이브 격자용
THUMB_Q = 80
SHARPEN = True       # 축소 뒤 가볍게 선명하게
FORCE = False        # 설정이 바뀌면 전체를 다시 굽는다

SRGB = ImageCms.createProfile("sRGB")

# 폴더 이름 → (내부 키, 화면 표시 이름). 한글/영어 둘 다 인식
CATEGORIES = [
    ("film", "필름", {"필름", "film"}),
    ("digital", "디지털", {"디지털", "digital"}),
    ("phone", "폰카", {"폰카", "폰카메라", "휴대폰", "phone", "mobile"}),
]

EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"}
if HEIC_OK:
    EXTS.add(".heic")


def norm(s):
    """맥은 한글 파일명을 자모 단위로 쪼개 저장하므로 비교 전에 형태를 통일한다."""
    return unicodedata.normalize("NFC", s).strip()


def parse_filename(stem):
    """파일 이름에서 제목 / 촬영자 / 수상 여부를 뽑아낸다."""
    stem = norm(stem)
    award = False
    if stem.startswith("수상_"):
        award = True
        stem = stem[len("수상_"):]
    photographer = None
    if "__" in stem:
        title, photographer = stem.split("__", 1)
        title = title.strip()
        photographer = photographer.strip() or None
    else:
        title = stem.strip()
    # 제목 끝의 # 뒤는 파일을 구분하려고 붙인 메모로 보고 화면에서는 지운다.
    # 같은 사람이 "무제" 두 점을 낼 때 "무제 #1", "무제 #2" 로 저장하면 된다.
    # 괄호는 건드리지 않는다. 설야(雪夜) 같은 제목이 흔하다.
    # 공백 뒤에 오면서 제목 맨 끝에 붙은 것만 잘라낸다. 제목 안의 #은 그대로 둔다.
    trimmed = re.sub(r"\s+#\S*$", "", title).strip()
    if trimmed:
        title = trimmed
    return title, photographer, award


def sort_key(path):
    return path.name.lower()


def to_srgb(im):
    """원본 색 프로파일을 sRGB로 변환한다.

    AdobeRGB 로 저장된 사진을 프로파일만 떼고 내보내면 브라우저가 sRGB 로
    해석해서 채도가 눈에 띄게 빠진다. 색 관리 문제라 화질을 올려도 안 고쳐진다.
    """
    icc = im.info.get("icc_profile")
    if not icc or im.mode == "L":
        return im
    try:
        src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        return ImageCms.profileToProfile(im, src_profile, SRGB, outputMode="RGB")
    except Exception:
        return im                              # 프로파일이 깨졌으면 원본 그대로


def resize_one(src, dst, max_side, quality, sharpen=True):
    """이미 만들어져 있고 원본보다 최신이면 건너뛴다. (매달 전체를 다시 굽지 않게)"""
    if not FORCE and dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        with Image.open(dst) as im:
            return im.size
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)      # 세로 사진 방향 보정
        im = to_srgb(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        if sharpen and SHARPEN:
            # 축소하면 반드시 물러진다. 인쇄용이 아니라 화면용이라 약하게만.
            im = im.filter(ImageFilter.UnsharpMask(radius=0.6, percent=55, threshold=3))
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 4:2:0. 사진에서는 4:4:4 와 육안 차이가 거의 없는데,
        # 필름 그레인이 있으면 용량이 두 배 가까이 늘어난다.
        im.save(dst, "JPEG", quality=quality, optimize=True, progressive=True,
                subsampling=2)
        return im.size


def build_month(month_id, hide_by=False):
    month_src = SRC_DIR / month_id
    entries = [p for p in month_src.iterdir() if not p.name.startswith(".")]
    dirs = {norm(p.name).lower(): p for p in entries if p.is_dir()}
    loose = [p for p in entries if p.is_file() and p.suffix.lower() in EXTS]

    categories = []
    total = 0
    skipped_heic = 0
    used = set()
    roster = []
    low_res = []

    for key, label, aliases in CATEGORIES:
        folder = None
        for a in aliases:
            hit = dirs.get(norm(a).lower())
            if hit is not None:
                folder = hit
                break
        if folder is None:
            continue

        files = []
        for p in sorted(folder.iterdir(), key=sort_key):
            if p.name.startswith("."):
                continue
            if p.suffix.lower() in EXTS:
                files.append(p)
            elif p.suffix.lower() == ".heic":
                skipped_heic += 1
        if not files:
            continue

        photos = []
        for i, src in enumerate(files, start=1):
            title, photographer, award = parse_filename(src.stem)
            # 파일 이름에서 고정 번호를 뽑는다. 나중에 사진을 추가해도
            # 기존 사진의 번호가 밀리지 않아 이미 받은 표가 어긋나지 않는다.
            tag = hashlib.sha1(norm(src.name).encode("utf-8")).hexdigest()[:6]
            pid = f"{key}-{tag}"
            big = OUT_DIR / month_id / f"{pid}.jpg"
            small = OUT_DIR / month_id / "thumbs" / f"{pid}.jpg"

            w, h = resize_one(src, big, DISPLAY_MAX, DISPLAY_Q)
            resize_one(src, small, THUMB_MAX, THUMB_Q, sharpen=False)

            with Image.open(src) as probe:
                if max(ImageOps.exif_transpose(probe).size) < DISPLAY_MAX:
                    low_res.append((label, title, max(probe.size)))
            used.add(big.resolve())
            used.add(small.resolve())

            entry = {
                "id": pid,
                "title": title,
                "src": str(big).replace("\\", "/"),
                "thumb": str(small).replace("\\", "/"),
                "w": w,
                "h": h,
            }
            # 투표가 열려 있는 달에는 촬영자를 공개 파일에 넣지 않는다
            if photographer and not hide_by:
                entry["by"] = photographer
            if award:
                entry["award"] = True
            photos.append(entry)
            roster.append({
                "부문": label, "사진ID": pid, "제목": title,
                "촬영자": photographer or "",
            })
            total += 1

        categories.append({"key": key, "label": label, "photos": photos})

    if not categories:
        # 왜 못 찾았는지 알려 준다
        if loose:
            why = f"사진 {len(loose)}장이 부문 폴더 밖에 있음 — 필름/디지털/폰카 폴더 안으로 옮기세요"
        elif dirs:
            names = ", ".join(sorted(norm(p.name) for p in dirs.values()))
            why = f"부문 폴더 이름이 안 맞음 (찾은 폴더: {names}) — 필름/디지털/폰카 중 하나여야 합니다"
        else:
            why = "폴더가 비어 있음"
        return None, 0, why, []

    # 지난 실행에서 남은, 지금은 쓰지 않는 파일을 정리한다
    removed = 0
    for old in (OUT_DIR / month_id).rglob("*.jpg"):
        if old.resolve() not in used:
            old.unlink()
            removed += 1

    note = ""
    if low_res:
        note = "원본이 작은 사진 " + ", ".join(f"{t}({px}px)" for _, t, px in low_res)
    if removed:
        note = (note + " / " if note else "") + f"쓰지 않는 파일 {removed}개 정리함"
    if skipped_heic:
        note = (note + " / " if note else "") + \
            f"HEIC {skipped_heic}장 건너뜀 — pip3 install pillow-heif 후 다시 실행하세요"

    y, m = month_id.split("-")
    return {
        "id": month_id,
        "year": int(y),
        "month": int(m),
        "categories": categories,
        "count": total,
    }, total, note, roster


def write_roster(month_id, rows):
    """촬영자 명단을 로컬에만 저장한다. 사이트에는 올라가지 않는다."""
    if not rows:
        return
    ROSTER_DIR.mkdir(exist_ok=True)
    path = ROSTER_DIR / f"{month_id}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["부문", "사진ID", "제목", "촬영자"])
        w.writeheader()
        w.writerows(rows)


def bake_awards(months):
    """하루 넘게 지난 수상작을 manifest.json 에 고정해 둔다.

    archive.html 은 이 플래그가 이미 있으면 구글 시트를 다시 불러오지 않는다.
    휴대폰에서 매번 시트를 불러오느라 느려지는 걸 막기 위한 것.
    """
    try:
        text = Path("index.html").read_text(encoding="utf-8")
    except OSError:
        return 0
    match = re.search(r'ENDPOINT:\s*"([^"]+)"', text)
    endpoint = match.group(1) if match else ""
    if not re.match(r"^https://script\.google\.com/macros/.+/exec$", endpoint):
        return 0

    ctx = None
    try:
        import certifi
        import ssl
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    try:
        with urllib.request.urlopen(endpoint + "?awards=1", timeout=8, context=ctx) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  수상작 정보를 불러오지 못해 건너뜀 — {e}")
        return 0
    if not data.get("ok") or not isinstance(data.get("awards"), list):
        return 0

    by_id = {mo["id"]: mo for mo in months}
    now = datetime.now(timezone.utc)
    baked = 0
    for a in data["awards"]:
        try:
            at = datetime.fromisoformat(str(a.get("at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if now - at < timedelta(days=1):
            continue
        month = by_id.get(a.get("month"))
        if not month:
            continue
        for cat in month["categories"]:
            if cat["label"] != a.get("category"):
                continue
            for p in cat["photos"]:
                if p["id"] == a.get("id") and not p.get("award"):
                    p["award"] = True
                    baked += 1
    return baked


def main():
    global DISPLAY_MAX, DISPLAY_Q, SHARPEN, FORCE

    ap = argparse.ArgumentParser(description="이달의 사진 준비 스크립트")
    ap.add_argument("--month", help="투표를 받을 달 (예: 2026-09). 기본값은 가장 최근 달")
    ap.add_argument("--deadline", help="마감 날짜 (예: 2026-09-14). 그날 23시 59분에 마감")
    ap.add_argument("--days", type=int, default=7, help="오늘부터 며칠 뒤 마감할지 (기본 7)")
    ap.add_argument("--size", type=int, help=f"투표 화면용 긴 변 픽셀 (기본 {DISPLAY_MAX})")
    ap.add_argument("--quality", type=int, help=f"JPEG 품질 1-100 (기본 {DISPLAY_Q})")
    ap.add_argument("--no-sharpen", action="store_true", help="축소 뒤 선명하게 하지 않기")
    ap.add_argument("--rebuild", action="store_true", help="이미 만든 사진도 전부 다시 굽기")
    ap.add_argument("--bake-only", action="store_true",
                     help="사진은 다시 만들지 않고, 하루 넘게 지난 수상작만 manifest.json 에 고정하기")
    args = ap.parse_args()

    if args.bake_only:
        if not MANIFEST.exists():
            sys.exit(f"'{MANIFEST}' 가 없습니다. 먼저 한 번 prepare.py 를 실행해 주세요.")
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        baked = bake_awards(manifest["months"])
        if not baked:
            print("새로 고정할 수상작이 없습니다 (아직 하루가 안 지났거나, 이미 고정됨).")
            return
        MANIFEST.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"수상작 {baked}건을 manifest.json 에 고정했습니다.")
        return

    if args.size:
        DISPLAY_MAX = args.size
    if args.quality:
        DISPLAY_Q = args.quality
    if args.no_sharpen:
        SHARPEN = False

    # 화질 설정이 지난번과 다르면 전체를 다시 굽는다.
    # 그러지 않으면 옛 설정으로 만든 사진이 그대로 남는다.
    stamp = f"{DISPLAY_MAX}/{DISPLAY_Q}/{THUMB_MAX}/{THUMB_Q}/{int(SHARPEN)}"
    stamp_file = OUT_DIR / ".settings"
    if args.rebuild or (stamp_file.exists() and stamp_file.read_text() != stamp):
        FORCE = True

    if not SRC_DIR.exists():
        sys.exit(f"'{SRC_DIR}' 폴더가 없습니다. 이 스크립트와 같은 위치에 만들어 주세요.")

    month_ids = sorted(
        p.name for p in SRC_DIR.iterdir()
        if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}", p.name)
    )
    if not month_ids:
        sys.exit(f"'{SRC_DIR}' 안에 2026-09 형식의 폴더가 없습니다.")

    voting_month = args.month or month_ids[-1]
    if voting_month not in month_ids:
        sys.exit(f"'{voting_month}' 폴더를 찾을 수 없습니다. 있는 달: {', '.join(month_ids)}")

    if args.deadline:
        d = datetime.strptime(args.deadline, "%Y-%m-%d")
    else:
        d = datetime.now(KST) + timedelta(days=args.days)
    deadline = datetime(d.year, d.month, d.day, 23, 59, 0, tzinfo=KST)

    # 원본을 지운 달은 지난번 manifest 에서 그대로 이어받는다.
    # 그러지 않으면 아카이브에서 그 달이 통째로 사라진다.
    kept = {}
    if MANIFEST.exists():
        try:
            old = json.loads(MANIFEST.read_text(encoding="utf-8"))
            for m in old.get("months", []):
                if m["id"] not in month_ids and (OUT_DIR / m["id"]).exists():
                    kept[m["id"]] = m
        except Exception:
            pass

    months = []
    print()
    for mid in reversed(month_ids):          # 최신 달이 앞으로
        data, n, note, roster = build_month(mid, hide_by=(mid == voting_month))
        if data is None:
            print(f"  {mid}  건너뜀 — {note}")
            continue
        months.append(data)
        cats = "  ".join(f"{c['label']} {len(c['photos'])}" for c in data["categories"])
        mark = "  ← 이번 투표" if mid == voting_month else ""
        print(f"  {mid}  {cats}   (총 {n}점){mark}")
        if note:
            print(f"           {note}")
        write_roster(mid, roster)

    for mid, m in kept.items():
        months.append(m)
        print(f"  {mid}  원본 없음 — 지난 기록 유지 (총 {m['count']}점)")
    months.sort(key=lambda m: m["id"], reverse=True)

    baked = bake_awards(months)
    if baked:
        print(f"\n  하루 넘게 지난 수상작 {baked}건을 manifest.json 에 고정함")

    manifest = {
        "generated": datetime.now(KST).isoformat(timespec="seconds"),
        "voting": {"month": voting_month, "deadline": deadline.isoformat(timespec="minutes")},
        "months": months,
    }
    OUT_DIR.mkdir(exist_ok=True)
    stamp_file.write_text(stamp)

    MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    nojekyll = Path(".nojekyll")
    if not nojekyll.exists():
        nojekyll.touch()

    print(f"\n  manifest.json 저장 완료")
    print(f"  투표 마감: {deadline.strftime('%Y년 %-m월 %-d일 %H:%M')}")
    print(f"\n  이제 photos 폴더와 manifest.json을 깃허브에 올리면 됩니다.\n")


if __name__ == "__main__":
    main()
