"""Generate synthetic KYC packets — no real PII, no real ID images.

Two cases:
  - case_legit:  application, ID image, selfie, and utility bill all agree.
                 The selfie and the ID photo show the same person.
  - case_fraud:  the application claims one identity; the ID image and bill
                 leak a different one. The selfie and the ID photo show
                 different people. The agent should flag this.

The avatars and ID design are intentionally stylized — clearly not real
documents, but professional enough that a viewer reads them as "ID"
during a demo recording.

Idempotent: re-running overwrites in place. Run automatically by run.py
if samples are missing.
"""

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


# ---------- font lookup ----------------------------------------------------

def _font(bold: bool, size: int) -> ImageFont.ImageFont:
    candidates = (
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    )
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except OSError:
            continue
    return ImageFont.load_default()


# ---------- avatar ---------------------------------------------------------

# Two distinct "people". "person_a" gets long hair, warm skin tone, teal shirt.
# "person_b" gets short hair, slightly darker skin, navy shirt. Enough visual
# difference that a viewer can immediately tell they're not the same person
# in the case_fraud packet.
_AVATARS = {
    "person_a": {
        "skin": "#f0c89e",
        "hair": "#3a2010",
        "shirt": "#4f7a8a",
        "long_hair": True,
    },
    "person_b": {
        "skin": "#d9a87a",
        "hair": "#15100a",
        "shirt": "#36465c",
        "long_hair": False,
    },
}


def _make_face(who: str, size: tuple[int, int] = (480, 480)) -> Image.Image:
    style = _AVATARS[who]
    skin = style["skin"]
    hair = style["hair"]
    shirt = style["shirt"]
    long_hair = style["long_hair"]

    W, H = size
    img = Image.new("RGB", (W, H), color="#dde6f0")
    draw = ImageDraw.Draw(img)

    # vertical gradient background — neutral, like a photo backdrop
    for y in range(H):
        t = y / H
        r = int(0xdd * (1 - t) + 0xee * t)
        g = int(0xe6 * (1 - t) + 0xee * t)
        b = int(0xf0 * (1 - t) + 0xdc * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    cx = W // 2

    # shoulders / shirt
    draw.rounded_rectangle(
        [(cx - 200, H - 100), (cx + 200, H + 20)],
        radius=40,
        fill=shirt,
    )
    # neck
    draw.rectangle(
        [(cx - 32, H - 175), (cx + 32, H - 100)],
        fill=skin,
    )

    # hair backdrop (long hair frames the face)
    if long_hair:
        draw.ellipse(
            [(cx - 130, 80), (cx + 130, H - 90)],
            fill=hair,
        )

    # face oval
    draw.ellipse([(cx - 95, 110), (cx + 95, 320)], fill=skin)

    # hair on top
    if long_hair:
        draw.chord([(cx - 95, 90), (cx + 95, 220)], start=180, end=360, fill=hair)
    else:
        draw.chord([(cx - 95, 95), (cx + 95, 230)], start=180, end=360, fill=hair)

    # eyes
    eye_y = 200
    for dx in (-35, 35):
        draw.ellipse(
            [(cx + dx - 14, eye_y - 9), (cx + dx + 14, eye_y + 9)],
            fill="white",
        )
        draw.ellipse(
            [(cx + dx - 8, eye_y - 8), (cx + dx + 8, eye_y + 8)],
            fill="#2a1810",
        )
        draw.ellipse(
            [(cx + dx - 4, eye_y - 6), (cx + dx - 1, eye_y - 3)],
            fill="white",
        )

    # brows
    for dx in (-35, 35):
        draw.line(
            [(cx + dx - 18, 178), (cx + dx + 18, 176)],
            fill=hair,
            width=4,
        )

    # nose
    draw.line([(cx - 5, 220), (cx - 8, 252)], fill="#b88862", width=2)
    draw.arc([(cx - 12, 246), (cx + 6, 262)], start=0, end=180, fill="#b88862", width=2)

    # mouth
    draw.arc([(cx - 25, 272), (cx + 25, 298)], start=0, end=180, fill="#7a3030", width=4)

    return img


# ---------- driver license -------------------------------------------------

def _render_id(
    path: Path,
    *,
    name: str,
    dob: str,
    ident: str,
    sex: str,
    exp: str,
    photo: Image.Image,
) -> None:
    W, H = 760, 470
    img = Image.new("RGB", (W, H), color="#e8eff5")
    draw = ImageDraw.Draw(img)

    f_state = _font(bold=True, size=18)
    f_title = _font(bold=True, size=24)
    f_label = _font(bold=True, size=11)
    f_field_b = _font(bold=True, size=15)
    f_name = _font(bold=True, size=22)

    # subtle horizontal background bands ("guilloche")
    for y in range(60, H, 6):
        draw.line([(0, y), (W, y)], fill="#dee6ee", width=1)

    # navy header
    draw.rectangle([(0, 0), (W, 60)], fill="#0a3d62")
    draw.text((25, 8), "OREGON", fill="#fac748", font=f_state)
    draw.text((25, 30), "DRIVER LICENSE", fill="white", font=f_title)
    # gold star
    draw.regular_polygon((215, 30, 13), n_sides=5, rotation=-90, fill="#fac748")

    # photo box
    px, py, pw, ph = 25, 90, 180, 230
    thumb = photo.resize((pw, ph))
    img.paste(thumb, (px, py))
    draw.rectangle([(px - 1, py - 1), (px + pw, py + ph)], outline="#5a6b80", width=2)

    # field grid right of photo
    fx = px + pw + 30

    def labeled(x: int, y: int, label: str, value: str) -> None:
        draw.text((x, y), label, fill="#0a3d62", font=f_label)
        draw.text((x, y + 13), value, fill="black", font=f_field_b)

    iss = _shift_year(exp, -6)
    labeled(fx,         95,  "DL 4d",  ident)
    labeled(fx + 200,   95,  "CLASS",  "C")
    labeled(fx,         140, "EXP",    exp)
    labeled(fx + 200,   140, "ISS",    iss)
    labeled(fx,         185, "SEX",    sex)
    labeled(fx + 200,   185, "HGT",    "5'-06\"" if sex == "F" else "5'-11\"")
    labeled(fx,         230, "EYES",   "BRN")
    labeled(fx + 200,   230, "HAIR",   "BRN")
    labeled(fx,         275, "ENDORSE", "NONE")
    labeled(fx + 200,   275, "RESTR",   "NONE")

    # name / dob / id row below photo
    name_y = py + ph + 25
    draw.text((25,  name_y),     "NAME:", fill="#0a3d62", font=f_label)
    draw.text((25,  name_y + 14), name,   fill="black",  font=f_name)
    draw.text((300, name_y),     "DOB:",  fill="#0a3d62", font=f_label)
    draw.text((300, name_y + 14), dob,    fill="black",  font=f_name)
    draw.text((520, name_y),     "ID:",   fill="#0a3d62", font=f_label)
    draw.text((520, name_y + 14), ident,  fill="black",  font=f_name)

    # signature line
    sig_y = H - 30
    draw.line([(25, sig_y), (300, sig_y)], fill="#94a3b8", width=1)
    draw.text((25, sig_y + 4), "signature", fill="#94a3b8", font=f_label)

    # fake barcode (deterministic per ID so re-runs match)
    rng = random.Random(ident)
    bx = W - 290
    while bx < W - 30:
        w = rng.choice([1, 2, 3])
        h = rng.choice([24, 28, 32])
        draw.rectangle([(bx, sig_y - h + 4), (bx + w, sig_y + 4)], fill="black")
        bx += w + 1

    img.save(path, "JPEG", quality=92)


def _shift_year(yyyy_mm_dd: str, delta: int) -> str:
    y, m, d = yyyy_mm_dd.split("-")
    return f"{int(y) + delta:04d}-{m}-{d}"


# ---------- selfie ---------------------------------------------------------

def _render_selfie(path: Path, who: str) -> None:
    img = _make_face(who, size=(480, 480))
    img.save(path, "JPEG", quality=90)


# ---------- utility bill ---------------------------------------------------

def _render_utility_bill(path: Path, *, name: str, address: str) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)

    # header band
    c.setFillColorRGB(0.04, 0.24, 0.38)
    c.rect(50, 720, 510, 60, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(60, 745, "Pacific Northwest Electric Co.")
    c.setFont("Helvetica", 10)
    c.drawString(60, 728, "Customer Statement")

    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", 11)
    c.drawString(50, 698, "Statement period: Apr 01 – Apr 30, 2026")
    c.drawString(50, 683, "Statement date:   May 03, 2026")
    c.drawString(50, 668, "Account #:        4082-1573-9911")

    c.line(50, 658, 560, 658)

    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, 638, "Account holder")
    c.setFont("Helvetica", 12)
    c.drawString(50, 622, name)

    c.setFont("Helvetica-Bold", 12)
    c.drawString(310, 638, "Service address")
    c.setFont("Helvetica", 12)
    c.drawString(310, 622, address)

    c.line(50, 600, 560, 600)

    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, 580, "Description")
    c.drawRightString(540, 580, "Amount")
    c.line(50, 575, 560, 575)

    items = [
        ("Energy charges (642 kWh × $0.118)", "$75.76"),
        ("Distribution fee", "$8.42"),
        ("Public purpose charge", "$1.94"),
        ("State utility tax", "$1.30"),
    ]
    c.setFont("Helvetica", 11)
    y = 558
    for desc, amt in items:
        c.drawString(50, y, desc)
        c.drawRightString(540, y, amt)
        y -= 16

    c.line(50, y - 4, 560, y - 4)

    c.setFont("Helvetica-Bold", 13)
    c.drawString(50, y - 24, "Amount due")
    c.drawRightString(540, y - 24, "$87.42")
    c.setFont("Helvetica", 12)
    c.drawString(50, y - 44, "Due date:  May 22, 2026")

    c.save()


# ---------- case specs -----------------------------------------------------

_LEGIT = {
    "application": {
        "name": "Jane Doe",
        "dob": "1985-03-12",
        "id": "D1234567",
        "address": "1500 NW Pine Ave, Portland, OR",
    },
    "id_image": {
        "name": "Jane Doe",
        "dob": "1985-03-12",
        "id": "D1234567",
        "sex": "F",
        "exp": "2030-03-12",
        "photo_who": "person_a",
    },
    "selfie_who": "person_a",        # same person as on the ID
    "bill_holder": "Jane Doe",
}

_FRAUD = {
    "application": {
        "name": "Jane Doe",
        "dob": "1985-03-12",
        "id": "D1234567",
        "address": "1500 NW Pine Ave, Portland, OR",
    },
    "id_image": {
        "name": "John Smith",
        "dob": "1990-08-04",
        "id": "S7654321",
        "sex": "M",
        "exp": "2028-08-04",
        "photo_who": "person_b",     # ID has John's photo
    },
    "selfie_who": "person_a",        # but the applicant claiming to be Jane took the selfie
    "bill_holder": "John Smith",
}


def _materialize(root: Path, case: str, spec: dict) -> None:
    case_dir = root / case
    case_dir.mkdir(parents=True, exist_ok=True)

    (case_dir / "application.json").write_text(
        json.dumps(spec["application"], indent=2) + "\n"
    )

    # selfie first — also reused as the ID photo when they're the same person
    _render_selfie(case_dir / "selfie.jpg", who=spec["selfie_who"])

    id_photo = _make_face(spec["id_image"]["photo_who"])
    _render_id(
        case_dir / "id.jpg",
        name=spec["id_image"]["name"],
        dob=spec["id_image"]["dob"],
        ident=spec["id_image"]["id"],
        sex=spec["id_image"]["sex"],
        exp=spec["id_image"]["exp"],
        photo=id_photo,
    )

    _render_utility_bill(
        case_dir / "utility_bill.pdf",
        name=spec["bill_holder"],
        address=spec["application"]["address"],
    )


def make_samples(root: Path) -> None:
    _materialize(root, "case_legit", _LEGIT)
    _materialize(root, "case_fraud", _FRAUD)
    print(f"generated samples in {root}")


if __name__ == "__main__":
    make_samples(Path(__file__).parent / "samples")
