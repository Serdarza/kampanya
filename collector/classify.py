"""Kampanya / hedef kitle sınıflandırması (kural tabanlı, metinde olmayanı üretmez)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .text import fold

# Kimlik → (etiket, desenler). Desenler katlanmış (fold) metinde aranır.
AUDIENCES: dict[str, tuple[str, list[str]]] = {
    "general_public_employee": (
        "Kamu Personeli",
        [
            r"kamu (?:personel|calisan|gorevli|kurum\w* (?:calisan|personel))\w*",
            r"devlet (?:memur|calisan|personel)\w*",
            r"kamu sektoru calisan\w*",
        ],
    ),
    "civil_servant": ("Memur", [r"\bmemur(?:lar|lara|larin|a)?\b", r"devlet memur\w*"]),
    "teacher": (
        "Öğretmen",
        [
            r"ogretmen\w*",
            r"\bmeb (?:personel|calisan|mensup)\w*",
            r"milli egitim bakanligi(?:na bagli| personel| calisan|nin personel)\w*",
            r"egitim (?:calisan|personel)\w*",
        ],
    ),
    "university": (
        "Akademisyen",
        [
            r"akademisyen\w*",
            r"ogretim (?:uyesi|uyeleri|gorevlisi|gorevlileri|elemani|elemanlari)\w*",
            r"universite (?:personel|calisan)\w*",
        ],
    ),
    "healthcare": (
        "Sağlık Çalışanı",
        [
            r"saglik (?:calisan|personel|mensup|profesyonel)\w*",
            r"saglik bakanligi(?:'?n[iı]n)? (?:calisan|personel)\w*",
            r"saglik bakanligi calisan\w*",
            r"eczaci\w*",
        ],
    ),
    "doctor": ("Hekim", [r"\bdoktor\w*", r"\bhekim\w*", r"dis hekim\w*"]),
    "nurse": ("Hemşire", [r"hemsire\w*", r"\bebe(?:ler|lere)?\b"]),
    "police": (
        "Emniyet",
        [
            r"\bpolis(?:ler|lere|e)?\b",
            r"emniyet (?:mensup|personel|teskilat|genel mudurlugu|calisan)\w*",
            r"\bemniyet\b",
            r"\bbekci\w*",
        ],
    ),
    "military": (
        "TSK",
        [
            r"\btsk\b",
            r"turk silahli kuvvetleri",
            r"silahli kuvvet\w*",
            r"\basker(?:i|ler|lere)?\b",
            r"\bsubay\w*",
            r"astsubay\w*",
            r"uzman (?:erbas|cavus)\w*",
            r"\bmsb\b",
            r"milli savunma bakanligi",
            r"sozlesmeli er\w*",
        ],
    ),
    "gendarmerie": ("Jandarma", [r"jandarma\w*"]),
    "coast_guard": ("Sahil Güvenlik", [r"sahil guvenlik\w*"]),
    "municipal": (
        "Belediye Personeli",
        [
            r"belediye(?:si|leri)? (?:personel|calisan)\w*",
            r"\bibb (?:personel|calisan)\w*",
            r"\bzabita\w*",
            r"itfaiye\w*",
        ],
    ),
    "public_worker": ("Kamu İşçisi", [r"kamu isci\w*", r"kamuda calisan isci\w*"]),
    "retired_public_employee": ("Emekli", [r"\bemekli\w*"]),
}

CATEGORY_LABELS = {
    "transport": "Ulaşım",
    "accommodation": "Konaklama",
    "education": "Eğitim",
    "technology": "Teknoloji",
    "automotive": "Otomotiv",
    "fuel": "Akaryakıt",
    "food": "Yeme-İçme",
    "health": "Sağlık",
    "culture": "Kültür-Sanat",
    "sports": "Spor",
    "travel": "Seyahat",
}

_OFFER = re.compile(
    r"(%\s?\d{1,3}|\d{1,3}\s?%|indirim|kampanya|avantaj|ozel fiyat|ozel tarife|ucretsiz|hediye"
    r"|\d[\d.]*\s?tl (?:indirim|iade)|ayricalik|firsat)"
)
_TARGETED = re.compile(
    r"(ozel|yonelik|kapsamaktadir|kapsamakta|yararlanabilir|faydalanabilir|gecerli|sunulmaktadir"
    r"|saglanmaktadir|uygulanmaktadir|anlasma|protokol|kimlig|kimlik kart|gorev belgesi|gorev yeri belgesi"
    r"|indirim|%\s?\d|\d\s?%|ayricalik|avantaj)"
)
_EXCLUSIVE = re.compile(r"(ozel|yonelik|kapsamaktadir|kapsamakta)")
_GENERIC_PUBLIC = re.compile(
    r"(tum musteri|butun musteri|herkese acik|tum kullanici|tum uyeler|tum yolcu|herkes icin"
    r"|tum vatandas)"
)
_NEWS_ONLY = re.compile(r"(ihale ilani|atama sonuc|sinav takvim|basvuru sonuc|duyuru metni|vefat)")
_STAFF_HINT = re.compile(r"(personel|calisan|mensup|gorevli|memur|ogretmen|emekli|uyelerimiz|uyelerine|sendika)")
_NON_STAFF = re.compile(r"(ogrenci|bursiyer|burslu)")


@dataclass
class Classification:
    is_campaign: bool
    is_public_employee: bool
    audiences: list[str] = field(default_factory=list)
    reason: str = ""


def detect_audiences(text: str) -> list[str]:
    t = fold(text)
    found = []
    for key, (_, patterns) in AUDIENCES.items():
        if any(re.search(p, t) for p in patterns):
            found.append(key)
    # "emekli" tek başına kamu personeli anlamına gelmez.
    if found == ["retired_public_employee"]:
        return []
    return found


def audience_labels(keys: list[str]) -> list[str]:
    return [AUDIENCES[k][0] for k in keys if k in AUDIENCES]


def classify(title: str, body: str, source_audiences: list[str], audience_from_source: bool) -> Classification:
    t = fold(f"{title}\n{body}")
    if _NEWS_ONLY.search(fold(title)):
        return Classification(False, False, reason="not a campaign (news/announcement)")
    if not _OFFER.search(t):
        return Classification(False, False, reason="no offer/discount signal")

    detected = detect_audiences(f"{title}\n{body}")
    if audience_from_source:
        # Resmî kurum sayfasında da olsa yalnızca öğrenci/bursiyer gibi personel dışı kitleler hariç tutulur.
        if not detected and _NON_STAFF.search(t) and not _STAFF_HINT.search(t):
            return Classification(True, False, reason="non-staff audience (students/scholars) in source text")
        # Gövdedeki "diş hekimi muayenesi" gibi hizmet adları kitle değildir; kaynağın kitlesi esastır.
        extra = [a for a in detected if a == "retired_public_employee"]
        return Classification(True, True, list(dict.fromkeys([*source_audiences, *extra])), "audience from source")

    if not detected:
        return Classification(True, False, reason="no public-employee audience in text")
    targeted = _targeted_audiences(t, _TARGETED)
    if not targeted:
        return Classification(True, False, detected, "audience mentioned but not targeted")
    if _GENERIC_PUBLIC.search(t) and not _targeted_audiences(t, _EXCLUSIVE):
        return Classification(True, False, detected, "general public campaign")
    if "retired_public_employee" in detected and "retired_public_employee" not in targeted:
        targeted.append("retired_public_employee")
    return Classification(True, True, targeted, "public-employee targeted")


def _targeted_audiences(t: str, near: re.Pattern[str], window: int = 80) -> list[str]:
    """Kitle ifadesinin hemen ardından hedefleme/teklif sözcüğü gelen kitleler.

    "Öğretmenler günü kutlu olsun" gibi geçişler kitle sayılmaz.
    """
    found = []
    for key, (_, patterns) in AUDIENCES.items():
        for p in patterns:
            if any(near.search(_same_sentence(t[m.end() : m.end() + window])) for m in re.finditer(p, t)):
                found.append(key)
                break
    if found == ["retired_public_employee"]:
        return []
    return found


def _same_sentence(s: str) -> str:
    return re.split(r"[.!?](?:\s|$)|\n", s, maxsplit=1)[0]
