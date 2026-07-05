"""OCR + visual card-recognition engine shared by /scan/native and /scan/code.

Moved verbatim from api.py (2026-07-05 router split) — the recognition logic is
known-good and intentionally unchanged (see CLAUDE.md scanner notes)."""

import base64
import io
import re
import unicodedata

from PIL import Image
from rapidfuzz import process, fuzz

from fab_api.core import HERE, SCAN_DEBUG_DIR, get_conn

# easyocr reader — loaded lazily on first /scan request (takes ~5s first time)
_easyocr_reader = None

def _get_easyocr():
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr
        _easyocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _easyocr_reader

# Card name list for fuzzy matching — refreshed on first /scan request
_card_names: list[str] = []

def _get_card_names():
    global _card_names
    if not _card_names:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT name FROM gold.gold_cards ORDER BY name")
                _card_names = [r["name"] for r in cur.fetchall()]
    return _card_names

def _save_scan_debug_image(img: Image.Image, engine: str, raw_text: str = "",
                           parsed=None, display_id: str | None = None) -> str:
    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = f"{ts}_{engine}"
    img_path = SCAN_DEBUG_DIR / f"{stem}.jpg"
    meta_path = SCAN_DEBUG_DIR / f"{stem}.txt"
    img.convert("RGB").save(img_path, format="JPEG", quality=94)
    meta_path.write_text(
        "\n".join([
            f"engine={engine}",
            f"size={img.width}x{img.height}",
            f"raw_text={raw_text!r}",
            f"parsed={parsed!r}",
            f"display_id={display_id!r}",
        ]) + "\n",
        encoding="utf-8",
    )
    return str(img_path.relative_to(HERE))

def _decode_b64_image(image_b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(image_b64)))

def _crop_name_region(img: Image.Image) -> Image.Image:
    """
    The frontend already crops + 2× scales the name region before sending,
    so we receive a pre-cropped image. Apply sharpening for easyocr.
    """
    from PIL import ImageFilter, ImageEnhance
    img = img.convert("RGB")
    img = ImageEnhance.Sharpness(img).enhance(2.0)
    img = ImageEnhance.Contrast(img).enhance(1.3)
    return img

def _ocr_easyocr(img: Image.Image) -> str:
    import numpy as np
    reader = _get_easyocr()
    arr = np.array(img.convert("RGB"))
    results = reader.readtext(arr, detail=0, paragraph=True)
    return " ".join(results).strip()

def _find_card_quad(edges_map, arr_shape, min_frac=0.10, max_frac=0.97):
    """
    Given a binary edge map, find the largest quadrilateral that looks like a card.
    Returns ordered corner points (float32 4×2) or None.
    """
    import numpy as np
    import cv2

    h, w   = arr_shape[:2]
    img_area = h * w

    contours, _ = cv2.findContours(edges_map, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    for cnt in sorted(contours, key=cv2.contourArea, reverse=True)[:10]:
        area = cv2.contourArea(cnt)
        if area < img_area * min_frac or area > img_area * max_frac:
            continue

        # Convex hull first — cleans up jagged contours caused by card art edges
        hull  = cv2.convexHull(cnt)
        peri  = cv2.arcLength(hull, True)
        for eps in (0.02, 0.03, 0.04, 0.05):   # try looser fits if strict fails
            approx = cv2.approxPolyDP(hull, eps * peri, True)
            if len(approx) == 4:
                pts = approx.reshape(4, 2).astype(np.float32)
                # Sanity check: polygon should be roughly card-shaped (portrait or landscape)
                xs, ys  = pts[:, 0], pts[:, 1]
                bw, bh  = float(np.max(xs) - np.min(xs)), float(np.max(ys) - np.min(ys))
                ratio   = min(bw, bh) / max(bw, bh) if max(bw, bh) > 0 else 0
                if 0.45 < ratio < 0.90:
                    return pts
    return None


def _order_corners(pts):
    import numpy as np
    s    = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).ravel()
    return np.array([
        pts[np.argmin(s)],     # TL
        pts[np.argmin(diff)],  # TR
        pts[np.argmax(s)],     # BR
        pts[np.argmax(diff)],  # BL
    ], dtype=np.float32)


def _rectify_card(img: Image.Image) -> tuple:
    """
    Detect the card's four corners and apply a perspective warp to 400×560.

    Tries three preprocessing pipelines in order:
      1. Bilateral filter + double Canny   (handles most backgrounds)
      2. CLAHE equalisation + Canny        (handles dark/uneven lighting)
      3. Adaptive threshold                (handles flat, low-contrast cards)

    Fallback: if no quad found, crop to the center 80% of the frame
    (where the UI overlay guides the user to place the card) — this still
    gives Claude a much better image than the raw full frame.

    Returns (image, "rectified" | "cropped" | "raw").
    """
    import numpy as np
    import cv2

    arr  = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    kernel5 = np.ones((5, 5), np.uint8)

    def _try(edge_map):
        dilated = cv2.dilate(edge_map, kernel5, iterations=2)
        return _find_card_quad(dilated, arr.shape)

    # Pipeline 1: bilateral + double Canny
    bil    = cv2.bilateralFilter(gray, 9, 75, 75)
    e1 = cv2.bitwise_or(cv2.Canny(bil, 20, 60), cv2.Canny(bil, 60, 180))
    pts = _try(e1)

    # Pipeline 2: CLAHE + Canny (improves low-light / dark card backs)
    if pts is None:
        clahe  = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        eq     = clahe.apply(gray)
        e2     = cv2.Canny(cv2.GaussianBlur(eq, (5, 5), 0), 30, 90)
        pts    = _try(e2)

    # Pipeline 3: adaptive threshold (handles uniform backgrounds very well)
    if pts is None:
        thresh = cv2.adaptiveThreshold(gray, 255,
                                       cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY_INV, 21, 4)
        pts    = _try(thresh)

    if pts is not None:
        ordered = _order_corners(pts)
        W, H    = 400, 560
        dst     = np.array([[0, 0], [W, 0], [W, H], [0, H]], dtype=np.float32)
        M       = cv2.getPerspectiveTransform(ordered, dst)
        warped  = cv2.warpPerspective(arr, M, (W, H))
        return Image.fromarray(warped), "rectified"

    # Fallback: crop to 80% centre region matching the UI overlay
    pad_x = int(w * 0.10)
    pad_y = int(h * 0.10)
    cropped = arr[pad_y: h - pad_y, pad_x: w - pad_x]
    return Image.fromarray(cropped), "cropped"


def _normalize(text: str) -> str:
    """Strip diacritics and lowercase — so ō→o, é→e, etc. OCR reads plain ASCII."""
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode().lower()

def _fuzzy_match(raw_text: str, top_n: int = 5, cutoff: int = 40) -> list[dict]:
    """
    Match OCR/Claude text against known card names using rapidfuzz WRatio.
    Returns empty list when no match exceeds the cutoff — callers can then
    show "not in database" rather than a wrong card.
    """
    names = _get_card_names()
    norm_query = _normalize(raw_text)
    norm_names = {_normalize(n): n for n in names}

    hits = process.extract(
        norm_query,
        list(norm_names.keys()),
        scorer=fuzz.WRatio,
        limit=top_n,
        score_cutoff=cutoff,
    )

    results = []
    for norm, score, _ in hits:
        original = norm_names[norm]
        results.append({"name": original, "score": round(score / 100, 3)})

    results.sort(key=lambda x: -x["score"])
    return results[:top_n]

# ── Card-code (display_id) scanning ───────────────────────────────────────────
# The bottom-left of every FaB card carries a code like "R EN · HVY050". The
# trailing token (HVY050) IS our gold.display_id, so reading it pins the exact
# set+number deterministically — far more reliable than OCRing the stylised name.
# Because EVERY valid code is known, we snap a noisy OCR read to the nearest real
# display_id: structured snap (set code, then number) auto-corrects the common
# OCR confusions (O/0, I/1, S/5, B/8, Z/2, G/6) without needing a trained model.

_code_index: dict | None = None

# Per-character confusions on the small, plain corner font. Applied to the NUMBER
# part only (digits ↔ look-alike letters); letters are snapped via the known set list.
_OCR_DIGIT_FIX = str.maketrans({
    "O": "0", "Q": "0", "D": "0", "U": "0",
    "I": "1", "L": "1", "|": "1", "T": "1", "/": "1",
    "Z": "2", "S": "5", "G": "6", "B": "8",
})
_OCR_SET_FIX = str.maketrans({
    "0": "O", "5": "S", "1": "I", "7": "T", "8": "B", "6": "G",
})

def _get_code_index():
    """Build the display_id snap index from gold (cached for the process).

    Returns {set_codes: set[str], by_set_num: {(set_code, int): display_id}}.
    The number is the digit run of display_id after the set-code prefix, so we can
    snap a parsed (letters, number) pair straight back to a real printing's code.
    """
    global _code_index
    if _code_index is None:
        set_codes: set[str] = set()
        by_set_num: dict[tuple[str, int], str] = {}
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT display_id, set_id FROM gold.gold_cards "
                    "WHERE display_id IS NOT NULL AND set_id IS NOT NULL"
                )
                for r in cur.fetchall():
                    did = (r["display_id"] or "").upper().strip()
                    sid = (r["set_id"] or "").upper().strip()
                    if not did or not sid:
                        continue
                    set_codes.add(sid)
                    tail = did[len(sid):] if did.startswith(sid) else did
                    m = re.search(r"\d+", tail)
                    if m:
                        by_set_num[(sid, int(m.group()))] = did
        _code_index = {"set_codes": set_codes, "by_set_num": by_set_num}
    return _code_index

def _parse_code(raw: str):
    """Pull the (letters, number) set-code token out of an OCR string.

    'R EN · HVY050' / 'R EN | HVY 050' / 'HVY050' all yield ('HVY', '050').
    Takes the LAST set+number token — the code sits at the end after the rarity
    ('R'/'C'/...) and language ('EN') tokens, which never carry trailing digits.
    """
    s = re.sub(r"[^A-Z0-9 ]+", " ", (raw or "").upper())
    compact = re.sub(r"[^A-Z0-9]+", "", s)
    compact_set = compact.translate(_OCR_SET_FIX)
    idx = _get_code_index()

    # Best path: find a real set code embedded in the OCR output. This handles
    # reads like "CCRU117", where the rarity C and set code get glued together.
    # Require a full 3-character number token. A one-digit garbage read like
    # "BET7" must not snap to BET007.
    embedded = []
    for set_code in idx["set_codes"]:
        for m in re.finditer(re.escape(set_code), compact_set):
            tail = compact[m.end(): m.end() + 4]
            n = re.match(r"([0-9OISBZGLDUQT|/]{3})", tail)
            if n and re.search(r"\d", n.group(1)):
                embedded.append((m.start(), set_code, n.group(1)))
    if embedded:
        _, letters, num_raw = max(embedded, key=lambda item: item[0])
        return letters, num_raw

    best = None
    # optional leading digit (e.g. '1HP'), 2-4 letters, then a full 3-character
    # collector number token.
    for m in re.finditer(r"([0-9]?[A-Z]{2,4})\s*([0-9OISBZGLDUQT|/]{3})\b", s):
        best = (m.group(1), m.group(2))
    return best

def _snap_code(letters: str, num_raw: str):
    """Snap a parsed (letters, number) to a real display_id, or None.

    1. Snap letters to the nearest known set code (exact, else fuzzy ≥50).
    2. Snap the number (after digit-confusion fixup) to the nearest real number
       in that set, accepting only if within 3 (guards against wild misreads).
    """
    idx = _get_code_index()
    letters = letters.upper()
    if not re.search(r"\d", num_raw):
        return None
    digits = re.sub(r"\D", "", num_raw.upper().translate(_OCR_DIGIT_FIX))
    if len(digits) != 3:
        return None
    n = int(digits)

    if letters in idx["set_codes"]:
        set_code = letters
    else:
        cand = process.extractOne(letters, list(idx["set_codes"]),
                                  scorer=fuzz.ratio, score_cutoff=50)
        set_code = cand[0] if cand else None
    if not set_code:
        return None

    if (set_code, n) in idx["by_set_num"]:
        return idx["by_set_num"][(set_code, n)]
    nums = [k[1] for k in idx["by_set_num"] if k[0] == set_code]
    if not nums:
        return None
    nearest = min(nums, key=lambda x: abs(x - n))
    if abs(nearest - n) <= 3:
        return idx["by_set_num"][(set_code, nearest)]
    return None

def _ocr_code_tesseract(img: Image.Image, fast: bool = False) -> str:
    """OCR the (already bottom-left-cropped) code strip with Tesseract.

    Restricts to the code alphabet and treats it as a single line — far more
    accurate and ~30-80ms vs a general-purpose model on this tiny fixed region.
    """
    import pytesseract
    from PIL import ImageOps, ImageEnhance

    g = ImageOps.grayscale(img.convert("RGB"))
    g = ImageOps.autocontrast(g)

    # The footer is tiny even in good phone photos. Upscale before OCR and try a
    # few contrast profiles; the parser snaps the combined noisy text to a real
    # display_id, so extra candidates are useful.
    scale = max(2, min(6, int(1800 / max(1, g.width))))
    if scale > 1:
        g = g.resize((g.width * scale, g.height * scale), Image.Resampling.LANCZOS)
    g = ImageEnhance.Sharpness(g).enhance(2.5)
    g = ImageEnhance.Contrast(g).enhance(1.8)

    threshold = g.point(lambda p: 255 if p > 150 else 0)
    inverted = ImageOps.invert(g)
    inverted_threshold = inverted.point(lambda p: 255 if p > 120 else 0)
    variants = [g, threshold] if fast else [g, threshold, inverted, inverted_threshold]

    cfgs = ["--psm 7 --oem 1"] if fast else [
        "--psm 7 --oem 1",
        "--psm 6 --oem 1",
        "--psm 13 --oem 1",
    ]
    whitelist = "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "
    reads = []
    try:
        for variant in variants:
            for cfg in cfgs:
                text = pytesseract.image_to_string(variant, config=f"{cfg} {whitelist}").strip()
                if text:
                    reads.append(text)
    except pytesseract.TesseractNotFoundError:
        raise ValueError(
            "Tesseract OCR is not installed. Run: sudo apt-get install -y tesseract-ocr"
        )
    except pytesseract.TesseractError:
        return ""
    return " | ".join(dict.fromkeys(reads))

def _crop_relative_pil(img: Image.Image, x: float, y: float, w: float, h: float) -> Image.Image:
    left = max(0, min(img.width - 1, int(img.width * x)))
    top = max(0, min(img.height - 1, int(img.height * y)))
    right = max(left + 1, min(img.width, int(img.width * (x + w))))
    bottom = max(top + 1, min(img.height, int(img.height * (y + h))))
    return img.crop((left, top, right, bottom))

def _read_footer_code(img: Image.Image) -> dict:
    """Search the native footer crop for the printed display_id code.

    The code line sits roughly 2-5 mm from the card bottom and can be left or
    centered. The Android app sends a broad footer band; focused sub-crops avoid
    OCRing copyright text, class text, border texture, and art noise all together.
    """
    windows = [
        ("left_code",   0.03, 0.33, 0.46, 0.26, True),
        ("center_code", 0.18, 0.33, 0.64, 0.26, True),
        ("wide_code",   0.02, 0.25, 0.96, 0.40, True),
        ("lower_left",  0.03, 0.45, 0.50, 0.25, True),
        ("lower_wide",  0.02, 0.40, 0.96, 0.35, True),
        ("full_footer", 0.00, 0.00, 1.00, 1.00, False),
    ]

    attempts = []
    for label, x, y, w, h, fast in windows:
        crop = _crop_relative_pil(img, x, y, w, h)
        raw = _ocr_code_tesseract(crop, fast=fast)
        parsed = _parse_code(raw)
        display_id = _snap_code(*parsed) if parsed else None
        attempts.append({
            "window": label,
            "raw_text": raw,
            "parsed": parsed,
            "display_id": display_id,
        })
        if display_id:
            return {
                "raw_text": raw,
                "parsed": parsed,
                "display_id": display_id,
                "window": label,
                "attempts": attempts,
            }

    combined_raw = " | ".join(a["raw_text"] for a in attempts if a["raw_text"])
    parsed = _parse_code(combined_raw)
    display_id = _snap_code(*parsed) if parsed else None
    return {
        "raw_text": combined_raw,
        "parsed": parsed,
        "display_id": display_id,
        "window": "combined" if display_id else None,
        "attempts": attempts,
    }

def _printings_for_display_id(display_id: str) -> list[dict]:
    """Gold rows for one display_id (set+number) — same shape as /card-printings."""
    sql = """
        SELECT
            g.printing_unique_id, g.set_id,
            coalesce(s.set_name, g.set_id) AS set_name,
            g.edition, g.foiling, g.rarity, g.pitch, g.image_url,
            g.price_eur, g.tcg_price_usd, g.price_sek, g.cm_idproduct
        FROM gold.gold_cards g
        LEFT JOIN bronze.fab_sets s
            ON s.set_id = g.set_id AND s.edition = g.edition
        WHERE g.display_id = %s
        ORDER BY g.edition, g.foiling
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [display_id])
            rows = cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["price_eur"]     = float(d["price_eur"])     if d["price_eur"]     else None
        d["tcg_price_usd"] = float(d["tcg_price_usd"]) if d["tcg_price_usd"] else None
        d["price_sek"]     = int(d["price_sek"])       if d["price_sek"]     else None
        out.append(d)
    return out

# ORB descriptor cache — loaded once per process, keyed by filter params
_orb_cache: dict | None = None
_orb_cache_key: tuple | None = None


def _load_orb_candidates(no_decks: bool, one_per_set: bool, min_rarity: str) -> list[dict]:
    """
    Load ORB descriptors from DB filtered by smart mode options.
    Each row: {name, set_id, edition, is_foil, desc: np.ndarray}
    """
    import numpy as np

    # Rarity sets: C=all, R=rare and above, M=majestic and above
    # T=Token, P=Promo, B=Bonus, S=Special excluded from visual matching —
    # their non-standard layouts create disproportionate keypoint hits
    rarity_map = {
        "C": ("C", "R", "M", "L", "F", "V"),
        "R": ("R", "M", "L", "F", "V"),
        "M": ("M", "L", "F", "V"),
    }
    allowed_rarities = rarity_map.get(min_rarity, rarity_map["C"])

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                WITH deck_sets AS (
                    SELECT set_id FROM gold.gold_cards
                    GROUP BY set_id HAVING COUNT(DISTINCT name) < 50
                ),
                ranked AS (
                    SELECT
                        g.name,
                        g.set_id,
                        g.edition,
                        g.is_foil,
                        p.raw_data->>'image_url' AS image_url,
                        ROW_NUMBER() OVER (
                            PARTITION BY g.name, g.set_id
                            ORDER BY
                                CASE g.edition
                                    WHEN 'U' THEN 1
                                    WHEN 'N' THEN 2
                                    WHEN 'A' THEN 3
                                    WHEN 'F' THEN 4
                                    ELSE 5
                                END,
                                CASE g.is_foil WHEN false THEN 1 ELSE 2 END
                        ) AS rn
                    FROM gold.gold_cards g
                    JOIN bronze.fab_printings p
                      ON p.printing_unique_id = g.printing_unique_id
                    WHERE p.raw_data->>'image_url' IS NOT NULL
                      AND g.rarity = ANY(%(rarities)s)
                      AND (NOT %(no_decks)s
                           OR g.set_id NOT IN (SELECT set_id FROM deck_sets))
                )
                SELECT r.name, r.set_id, r.edition, r.is_foil,
                       d.descriptors, d.kp_count
                FROM ranked r
                JOIN bronze.card_orb_descriptors d ON d.image_url = r.image_url
                WHERE (%(one_per_set)s = false OR r.rn = 1)
                  AND d.kp_count > 0
            """, {
                "rarities": list(allowed_rarities),
                "no_decks": no_decks,
                "one_per_set": one_per_set,
            })
            rows = cur.fetchall()

    candidates = []
    for row in rows:
        raw = bytes(row["descriptors"])
        desc = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 32)
        candidates.append({
            "name":     row["name"],
            "set_id":   row["set_id"],
            "edition":  row["edition"],
            "is_foil":  row["is_foil"],
            "desc":     desc,
        })
    return candidates


def _visual_match(img: Image.Image, top_n: int = 5,
                  no_decks: bool = True, one_per_set: bool = True,
                  min_rarity: str = "M") -> list[dict]:
    """
    ORB feature matching: compare camera frame against pre-computed card descriptors.
    Score = number of good keypoint matches (Hamming distance < 60).
    Smart mode filters reduce the candidate set to speed up matching.
    """
    import numpy as np
    import cv2

    global _orb_cache, _orb_cache_key

    cache_key = (no_decks, one_per_set, min_rarity)
    if _orb_cache is None or _orb_cache_key != cache_key:
        _orb_cache = _load_orb_candidates(no_decks, one_per_set, min_rarity)
        _orb_cache_key = cache_key

    candidates = _orb_cache
    if not candidates:
        return []

    # Compute ORB on the incoming camera frame
    arr  = np.array(img.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    target_w = 400
    h, w = gray.shape
    if w != target_w:
        scale = target_w / w
        gray = cv2.resize(gray, (target_w, int(h * scale)), interpolation=cv2.INTER_AREA)

    orb = cv2.ORB_create(nfeatures=500)
    _, query_desc = orb.detectAndCompute(gray, None)

    if query_desc is None or len(query_desc) == 0:
        return []

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    # Score each candidate
    name_scores: dict[str, int] = {}
    for c in candidates:
        matches = matcher.match(query_desc, c["desc"])
        good    = sum(1 for m in matches if m.distance < 60)
        name    = c["name"]
        if good > name_scores.get(name, 0):
            name_scores[name] = good

    if not name_scores:
        return []

    max_good = max(name_scores.values())

    # Require a meaningful number of good matches — avoid noise matches
    if max_good < 12:
        return []

    # Only return cards with >= 70% of the best match count (clear winner zone)
    threshold = max(12, max_good * 0.70)
    scored = [
        {"name": n, "score": round(cnt / max_good, 3)}
        for n, cnt in name_scores.items()
        if cnt >= threshold
    ]
    scored.sort(key=lambda x: (-x["score"], x["name"]))
    return scored[:top_n]
