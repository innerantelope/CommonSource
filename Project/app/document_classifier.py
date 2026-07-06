from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from llm_provider import LLMProviderError, classify as llm_classify


TAXONOMY: Dict[str, Dict[str, Any]] = {
    "Maternal & Child Health": {
        "keywords": (
            "maternal", "mother", "pregnancy", "pregnant", "antenatal", "anc", "postnatal", "pnc",
            "neonatal", "newborn", "infant", "child health", "jaundice", "immunization", "vaccination",
            "asha", "anganwadi", "delivery", "breastfeeding",
            "मातृ", "गर्भावस्था", "गर्भवती", "प्रसव", "नवजात", "शिशु", "बच्चा", "टीकाकरण",
            "स्तनपान", "आंगनवाड़ी",
        ),
        "tags": ("Neonatal Care", "Newborn Care", "Child Health", "Vaccination"),
    },
    "Nutrition & Anemia": {
        "keywords": (
            "nutrition", "anemia", "anaemia", "iron", "folic", "ifa", "poshan", "poshan maah",
            "malnutrition", "supplementary nutrition", "mid-day meal", "hemoglobin", "hb",
            "एनीमिया", "एनीमिक", "खून की कमी", "रक्त की कमी", "हीमोग्लोबिन", "आयरन",
            "पोषण", "कुपोषण",
        ),
        "tags": ("Nutrition", "Anemia", "Poshan Maah"),
    },
    "Menstrual Health & Hygiene": {
        "keywords": (
            "menstrual", "menstruation", "period", "sanitary pad", "sanitation pad", "mhm",
            "hygiene", "menstrual hygiene", "cloth pad",
            "माहवारी", "मासिक धर्म", "पीरियड", "सेनेटरी", "स्वच्छता",
        ),
        "tags": ("Menstrual Health", "Menstrual Hygiene"),
    },
    "Tuberculosis": {
        "keywords": (
            "tb", "tuberculosis", "nikshay", "nikshay mitra", "tb mukt bharat", "dots",
            "sputum", "pulmonary tb", "latent tb",
            "टीबी", "क्षय रोग", "तपेदिक", "निक्षय",
        ),
        "tags": ("Tuberculosis", "TB Prevention"),
    },
    "Neglected Tropical Diseases": {
        "keywords": (
            "neglected tropical", "ntd", "filariasis", "leprosy", "kala azar", "lymphatic",
            "deworming", "soil transmitted", "schistosomiasis",
            "फाइलेरिया", "कुष्ठ", "काला ज्वर", "कृमि", "कृमि मुक्ति",
        ),
        "tags": ("Neglected Tropical Diseases", "Deworming"),
    },
    "Vector-Borne Diseases": {
        "keywords": (
            "dengue", "malaria", "mosquito", "aes", "acute encephalitis", "chikungunya",
            "japanese encephalitis", "vector borne", "larvae", "fogging",
            "डेंगू", "मलेरिया", "मच्छर", "चिकनगुनिया", "जापानी इंसेफेलाइटिस",
        ),
        "tags": ("Dengue", "Malaria", "Vector Control"),
    },
    "Adolescent Health": {
        "keywords": (
            "adolescent", "teen", "kishori", "rkssk", "peer educator", "life skills",
            "school health", "adolescence",
            "किशोर", "किशोरी", "आरकेएसके", "स्कूल स्वास्थ्य",
        ),
        "tags": ("Adolescent Health", "School Health"),
    },
    "Sexual & Reproductive Health": {
        "keywords": (
            "sexual health", "reproductive health", "srh", "sti", "std", "hiv", "hpv",
            "cervical cancer", "safe sex", "reproductive rights",
            "यौन", "प्रजनन स्वास्थ्य", "एचआईवी", "एचपीवी", "गर्भाशय ग्रीवा",
        ),
        "tags": ("Sexual Health", "Reproductive Health", "HPV Vaccine", "Cervical Cancer Prevention"),
    },
    "Family Planning": {
        "keywords": (
            "family planning", "contraception", "contraceptive", "sterilization", "iucd",
            "condom", "spacing", "mission parivar vikas",
            "परिवार नियोजन", "गर्भनिरोध", "कंडोम", "नसबंदी", "आईयूसीडी",
        ),
        "tags": ("Family Planning", "Contraception"),
    },
    "Government Health Schemes": {
        "keywords": (
            "ayushman bharat", "abha", "abha id", "pmjay", "janani suraksha", "jsy",
            "nhm", "health scheme", "government scheme", "health card", "asha incentive",
            "आयुष्मान", "आभा", "पीएमजय", "जननी सुरक्षा", "स्वास्थ्य योजना", "आशा",
        ),
        "tags": ("Ayushman Bharat", "ABHA ID", "Government Health Schemes"),
    },
    "Seasonal Ailments": {
        "keywords": (
            "seasonal", "fever", "heat wave", "heatwave", "heat stroke", "hot weather", "diarrhea", "diarrhoea",
            "cholera", "flu", "influenza", "cold wave", "monsoon disease",
            "बुखार", "लू", "गर्मी", "तेज धूप", "धूप", "दस्त", "हैजा", "फ्लू", "मानसून",
        ),
        "tags": ("Seasonal Ailments", "Heat Wave"),
    },
}

GENERAL_TAXONOMY: Dict[str, Dict[str, Any]] = {
    "Education & Training": {
        "keywords": (
            "education", "school", "student", "teacher", "curriculum", "learning", "training",
            "workshop", "module", "classroom", "lesson", "scholarship", "university", "college",
            "शिक्षा", "विद्यालय", "स्कूल", "छात्र", "शिक्षक", "प्रशिक्षण",
        ),
        "tags": ("Education", "Training", "Learning"),
    },
    "Governance & Policy": {
        "keywords": (
            "policy", "government", "governance", "public policy", "scheme", "department",
            "administration", "minister", "ministry", "panchayat", "municipal", "guideline",
            "नीति", "सरकार", "शासन", "पंचायत", "विभाग", "योजना",
        ),
        "tags": ("Governance", "Policy", "Public Administration"),
    },
    "Law & Justice": {
        "keywords": (
            "law", "legal", "court", "justice", "rights", "act", "rules", "regulation",
            "case", "petition", "compliance", "contract", "tribunal",
            "कानून", "न्याय", "अधिकार", "अदालत", "नियम",
        ),
        "tags": ("Law", "Justice", "Rights"),
    },
    "Economy & Livelihoods": {
        "keywords": (
            "economy", "livelihood", "employment", "jobs", "income", "finance", "market",
            "bank", "loan", "savings", "business", "entrepreneur", "trade", "industry",
            "रोजगार", "आजीविका", "आय", "बाजार", "ऋण", "व्यवसाय",
        ),
        "tags": ("Livelihoods", "Employment", "Finance"),
    },
    "Agriculture & Food Systems": {
        "keywords": (
            "agriculture", "farmer", "crop", "soil", "irrigation", "seed", "harvest",
            "livestock", "dairy", "fishery", "organic farming", "mandi",
            "कृषि", "किसान", "फसल", "सिंचाई", "बीज", "मिट्टी", "पशुपालन",
        ),
        "tags": ("Agriculture", "Farmers", "Food Systems"),
    },
    "Environment & Climate": {
        "keywords": (
            "environment", "climate", "pollution", "water", "waste", "forest", "biodiversity",
            "conservation", "disaster", "flood", "drought", "sustainability", "energy",
            "पर्यावरण", "जलवायु", "प्रदूषण", "पानी", "कचरा", "बाढ़", "सूखा",
        ),
        "tags": ("Environment", "Climate", "Sustainability"),
    },
    "Technology & Digital": {
        "keywords": (
            "technology", "digital", "software", "internet", "mobile", "app", "data",
            "database", "ai", "artificial intelligence", "cyber", "platform", "automation",
            "तकनीक", "डिजिटल", "इंटरनेट", "मोबाइल", "डेटा", "साइबर",
        ),
        "tags": ("Technology", "Digital", "Data"),
    },
    "Science & Research": {
        "keywords": (
            "research", "study", "survey", "methodology", "data analysis", "evidence",
            "experiment", "findings", "journal", "paper", "science",
            "शोध", "अध्ययन", "सर्वे", "विज्ञान", "निष्कर्ष",
        ),
        "tags": ("Research", "Evidence", "Science"),
    },
    "Culture & Arts": {
        "keywords": (
            "culture", "art", "music", "film", "theatre", "literature", "festival",
            "heritage", "language", "poetry", "story", "artist", "museum",
            "संस्कृति", "कला", "संगीत", "फिल्म", "साहित्य", "त्योहार", "कहानी",
        ),
        "tags": ("Culture", "Arts", "Heritage"),
    },
    "Media & Communication": {
        "keywords": (
            "media", "radio", "podcast", "broadcast", "journalism", "newsroom", "campaign",
            "communication", "message", "script", "interview", "episode", "anchor",
            "मीडिया", "रेडियो", "पत्रकारिता", "संचार", "संदेश", "कार्यक्रम",
        ),
        "tags": ("Media", "Communication", "Radio"),
    },
    "Community Development": {
        "keywords": (
            "community", "village", "local", "self help group", "shg", "ngo", "civil society",
            "participation", "volunteer", "awareness", "mobilization", "grassroots",
            "समुदाय", "गांव", "स्थानीय", "स्वयं सहायता समूह", "जागरूकता",
        ),
        "tags": ("Community", "Awareness", "Grassroots"),
    },
    "Sports & Recreation": {
        "keywords": (
            "sports", "game", "tournament", "player", "team", "match", "coach",
            "fitness", "yoga", "recreation", "athlete",
            "खेल", "मैच", "टीम", "खिलाड़ी", "योग",
        ),
        "tags": ("Sports", "Fitness", "Recreation"),
    },
}

CATEGORY_TAXONOMY: Dict[str, Dict[str, Any]] = {**TAXONOMY, **GENERAL_TAXONOMY}

TAG_KEYWORDS: Dict[str, Iterable[str]] = {
    "HPV Vaccine": ("hpv", "human papillomavirus", "एचपीवी"),
    "Cervical Cancer Prevention": ("cervical cancer", "pap smear", "hpv", "गर्भाशय ग्रीवा"),
    "Vaccination": ("vaccine", "vaccination", "immunization", "immunisation", "टीकाकरण"),
    "TB Prevention": ("tb prevention", "tuberculosis prevention", "nikshay", "टीबी", "क्षय रोग", "निक्षय"),
    "Dengue": ("dengue", "डेंगू"),
    "Malaria": ("malaria", "मलेरिया"),
    "Menstrual Health": ("menstrual", "menstruation", "period", "माहवारी", "मासिक धर्म"),
    "Family Planning": ("family planning", "contraception", "contraceptive", "परिवार नियोजन", "गर्भनिरोध"),
    "Ayushman Bharat": ("ayushman bharat", "pmjay", "आयुष्मान", "पीएमजय"),
    "ABHA ID": ("abha", "abha id", "आभा"),
    "Poshan Maah": ("poshan maah", "poshan", "पोषण"),
    "Nutrition": ("nutrition", "malnutrition", "पोषण", "कुपोषण"),
    "Anemia": ("anemia", "anaemia", "hemoglobin", "haemoglobin", "एनीमिया", "एनीमिक", "खून की कमी", "रक्त की कमी"),
    "Heat Wave": ("heat wave", "heatwave", "hot weather", "लू", "गर्मी", "तेज धूप"),
    "Fever": ("fever", "बुखार"),
    "Diarrhea": ("diarrhea", "diarrhoea", "दस्त"),
}

DOCUMENT_TYPE_RULES: Dict[str, Iterable[str]] = {
    "Radio Script": (
        "rj:", "rj1:", "rj2:", "announcer:", "anchor:", "cue:", "sfx:", "music:",
        "radio script", "radio program", "रेडियो प्रोग्राम", "रेडियो कार्यक्रम", "आरजे",
    ),
    "Interview": ("interview", "interviewer:", "respondent:", "question:", "answer:", "q:", "a:", "साक्षात्कार"),
    "Health Bulletin": ("health bulletin", "bulletin", "public health advisory", "health update", "स्वास्थ्य बुलेटिन"),
    "Research Paper": ("abstract", "literature review", "methodology", "references", "bibliography", "study", "शोध", "अध्ययन"),
    "Training Material": ("training", "module", "session plan", "learning objective", "facilitator", "worksheet", "प्रशिक्षण", "मॉड्यूल"),
    "Transcript": ("transcript", "speaker 1:", "speaker 2:", "verbatim", "प्रतिलिपि"),
    "Case Study": ("case study", "case story", "background", "outcome", "lessons learned", "केस स्टडी"),
    "Report": ("executive summary", "findings", "recommendations", "assessment", "evaluation", "report", "कार्यकारी सारांश", "निष्कर्ष", "सिफारिश", "रिपोर्ट"),
    "Article": ("article", "byline", "news", "published"),
    "Archive Content": ("archive", "collection", "compiled"),
}

STOPWORDS = {
    "about", "after", "also", "and", "are", "because", "been", "being", "between", "from", "have",
    "into", "that", "the", "their", "there", "this", "through", "with", "without", "your", "for",
    "was", "were", "will", "shall", "can", "could", "should", "health", "document", "content",
    "http", "https", "www", "com", "org", "file", "view", "sharing", "drive", "google", "docx",
    "pdf", "txt", "uploaded", "archive", "source", "title", "sample",
    "article", "report", "guide", "explains", "covers", "unknown", "system",
}

HINDI_STOPWORDS = {
    "और", "या", "है", "हैं", "था", "थी", "थे", "को", "का", "की", "के", "में", "से", "पर",
    "यह", "इस", "उस", "एक", "हम", "आप", "वे", "तो", "भी", "ही", "लिए", "साथ", "आज",
    "जी", "नमस्कार", "धन्यवाद", "दोस्तों", "लोग", "बारे", "कर", "करें", "होगा",
}


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def content_hash(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha1(raw).hexdigest()


def _count_phrase(text: str, phrase: str) -> int:
    phrase = phrase.lower().strip()
    if not phrase:
        return 0
    if re.search(r"[^\x00-\x7F]", phrase):
        return text.count(phrase)
    if len(phrase) <= 3:
        return len(re.findall(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text))
    return text.count(phrase)


def classify_categories(text: str) -> List[Dict[str, Any]]:
    haystack = (text or "").lower()
    results: List[Dict[str, Any]] = []
    for category, config in CATEGORY_TAXONOMY.items():
        matched = []
        score = 0
        for keyword in config["keywords"]:
            count = _count_phrase(haystack, keyword)
            if count:
                matched.append(keyword)
                score += min(count, 3)
        if matched:
            confidence = min(0.98, 0.35 + (score / max(len(config["keywords"]), 1)))
            results.append({
                "category": category,
                "confidence_score": round(confidence, 3),
                "matched_keywords": matched[:12],
            })
    results.sort(key=lambda item: item["confidence_score"], reverse=True)
    return results[:5]


def classify_general_categories(text: str) -> List[Dict[str, Any]]:
    haystack = (text or "").lower()
    results: List[Dict[str, Any]] = []
    for category, config in GENERAL_TAXONOMY.items():
        matched = []
        score = 0
        for keyword in config["keywords"]:
            count = _count_phrase(haystack, keyword)
            if count:
                matched.append(keyword)
                score += min(count, 3)
        if matched:
            confidence = min(0.9, 0.3 + (score / max(len(config["keywords"]), 1)))
            results.append({
                "category": category,
                "confidence_score": round(confidence, 3),
                "matched_keywords": matched[:12],
            })
    results.sort(key=lambda item: item["confidence_score"], reverse=True)
    return results[:4]


def classify_tags(text: str, categories: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    haystack = (text or "").lower()
    tag_scores: Dict[str, Dict[str, Any]] = {}
    for category in categories:
        config = CATEGORY_TAXONOMY.get(category["category"], {})
        for tag in config.get("tags", ()):
            tag_scores[tag] = {
                "tag": tag,
                "category": category["category"],
                "confidence_score": max(float(category["confidence_score"]) - 0.05, 0.4),
            }
    for tag, phrases in TAG_KEYWORDS.items():
        count = sum(_count_phrase(haystack, phrase) for phrase in phrases)
        if count:
            current = tag_scores.get(tag, {"tag": tag, "category": "", "confidence_score": 0.0})
            current["confidence_score"] = max(float(current["confidence_score"]), min(0.98, 0.55 + 0.12 * count))
            tag_scores[tag] = current
    tags = list(tag_scores.values())
    tags.sort(key=lambda item: item["confidence_score"], reverse=True)
    return tags[:20]


def _clean_for_terms(text: str) -> str:
    text = re.sub(r"https?://\S+", " ", text or "")
    text = re.sub(r"\b[\w.-]+@[\w.-]+\.\w+\b", " ", text)
    text = re.sub(r"[_/\\|]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _title_case_tag(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip(" -_:;,.")
    if not value:
        return ""
    if re.search(r"[\u0900-\u097F]", value):
        return value[:80]
    small = {"and", "or", "of", "the", "in", "on", "for", "to", "with", "by"}
    parts = [part.lower() for part in value.split()]
    return " ".join(part if idx and part in small else part.capitalize() for idx, part in enumerate(parts))[:80]


def _add_tag_score(scores: Dict[str, float], tag: str, score: float) -> None:
    raw_tokens = re.findall(r"[\w\u0900-\u097F-]+", tag or "")
    clean_tokens = [
        token for token in raw_tokens
        if not re.fullmatch(r"[A-Za-z]{1,4}\d+[A-Za-z0-9-]*", token)
    ]
    tag = _title_case_tag(" ".join(clean_tokens))
    if not tag or len(tag) < 3:
        return
    key = tag.lower()
    scores[key] = max(scores.get(key, 0.0), score)


def generate_generic_tags(
    *,
    title: str,
    filename: str,
    text: str,
    existing_tags: List[Dict[str, Any]],
    limit: int = 16,
) -> List[Dict[str, Any]]:
    existing = {str(item.get("tag") or "").strip().lower() for item in existing_tags}
    scores: Dict[str, float] = {}
    filename_title = Path(filename or "").stem.replace("_", " ").replace("-", " ").strip()
    title_parts = [title or ""]
    if filename_title and filename_title.lower() != (title or "").strip().lower():
        title_parts.append(filename_title)
    title_text = _clean_for_terms(" ".join(title_parts))
    content = _clean_for_terms(" ".join([title_text, (text or "")[:30000]]))

    title_words = [
        word for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", title_text.lower())
        if word not in STOPWORDS and not word.isdigit()
    ]
    if 1 <= len(title_words) <= 5:
        _add_tag_score(scores, " ".join(title_words), 0.72)
    for word in title_words:
        _add_tag_score(scores, word, 0.42)

    words = [
        word for word in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", content.lower())
        if word not in STOPWORDS and not word.isdigit() and len(word) >= 4
    ]
    counts = Counter(words)
    if counts:
        max_count = max(counts.values())
        for word, count in counts.most_common(30):
            _add_tag_score(scores, word, 0.28 + 0.17 * (count / max_count))
    for size in (2, 3):
        phrase_counts = Counter(
            " ".join(words[idx:idx + size])
            for idx in range(0, max(len(words) - size + 1, 0))
            if len(set(words[idx:idx + size])) == size
        )
        for phrase, count in phrase_counts.most_common(20):
            if count >= 2 or phrase in " ".join(title_words):
                _add_tag_score(scores, phrase, 0.42 + min(0.2, count * 0.03))

    hindi_words = [
        word for word in re.findall(r"[\u0900-\u097F]{3,}", content)
        if word not in HINDI_STOPWORDS
    ]
    hindi_counts = Counter(hindi_words)
    for word, count in hindi_counts.most_common(25):
        _add_tag_score(scores, word, 0.3 + min(0.16, count * 0.03))
    for size in (2, 3):
        phrase_counts = Counter(
            " ".join(hindi_words[idx:idx + size])
            for idx in range(0, max(len(hindi_words) - size + 1, 0))
            if len(set(hindi_words[idx:idx + size])) == size
        )
        for phrase, count in phrase_counts.most_common(15):
            if count >= 2:
                _add_tag_score(scores, phrase, 0.42 + min(0.18, count * 0.03))

    items = [
        {"tag": _title_case_tag(tag), "category": "", "confidence_score": round(score, 3)}
        for tag, score in scores.items()
        if tag not in existing
    ]
    items.sort(key=lambda item: item["confidence_score"], reverse=True)
    return items[:limit]


def detect_language(text: str) -> str:
    sample = (text or "")[:20000]
    devanagari = len(re.findall(r"[\u0900-\u097F]", sample))
    latin = len(re.findall(r"[A-Za-z]", sample))
    total = devanagari + latin
    if total == 0:
        return "unknown"
    hindi_ratio = devanagari / total
    english_ratio = latin / total
    if hindi_ratio > 0.2 and english_ratio > 0.2:
        return "bilingual"
    if hindi_ratio >= english_ratio:
        return "hindi"
    return "english"


def classify_document_type(title: str, filename: str, text: str) -> str:
    haystack = " ".join([title or "", filename or "", (text or "")[:12000]]).lower()
    best_type = "Article"
    best_score = 0
    for doc_type, phrases in DOCUMENT_TYPE_RULES.items():
        score = sum(_count_phrase(haystack, phrase) for phrase in phrases)
        if score > best_score:
            best_type = doc_type
            best_score = score
    if best_score == 0:
        if len((text or "").split()) > 2500:
            return "Archive Content"
        return "Article"
    return best_type


def extract_title(filename: str, text: str, explicit_title: str = "") -> str:
    if explicit_title and explicit_title.strip():
        return explicit_title.strip()
    for line in (text or "").splitlines()[:20]:
        cleaned = re.sub(r"\s+", " ", line).strip(" -:\t")
        if 8 <= len(cleaned) <= 160 and len(cleaned.split()) <= 20:
            return cleaned
    return Path(filename or "Untitled").stem.replace("_", " ").replace("-", " ").strip().title() or "Untitled"


def extract_keywords(text: str, categories: List[Dict[str, Any]], tags: List[Dict[str, Any]], limit: int = 30) -> List[Dict[str, Any]]:
    scored: Dict[str, float] = {}
    for category in categories:
        for keyword in category.get("matched_keywords", []):
            scored[keyword] = max(scored.get(keyword, 0.0), float(category["confidence_score"]))
    for tag in tags:
        scored[tag["tag"]] = max(scored.get(tag["tag"], 0.0), float(tag["confidence_score"]))
    words = re.findall(r"[A-Za-z][A-Za-z][A-Za-z0-9-]{2,}", (text or "").lower())
    counts = Counter(w for w in words if w not in STOPWORDS and not w.isdigit())
    if counts:
        max_count = max(counts.values())
        for word, count in counts.most_common(40):
            scored.setdefault(word, round(0.25 + 0.45 * (count / max_count), 3))
    items = [
        {"keyword": key, "confidence_score": round(float(score), 3)}
        for key, score in scored.items()
        if key and len(key) > 1
    ]
    items.sort(key=lambda item: item["confidence_score"], reverse=True)
    return items[:limit]


def ai_metadata_suggestions(title: str, filename: str, text: str) -> Dict[str, Any]:
    if os.getenv("COMMONSOURCE_METADATA_AI", "1").lower() not in {"1", "true", "yes"}:
        return {}
    try:
        category_names = list(CATEGORY_TAXONOMY.keys())
        document_types = list(DOCUMENT_TYPE_RULES.keys())
        schema = {
            "type": "object",
            "properties": {
                "categories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string", "enum": category_names},
                            "confidence_score": {"type": "number"},
                        },
                        "required": ["category", "confidence_score"],
                    },
                    "maxItems": 5,
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 15,
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 25,
                },
                "document_type": {"type": "string", "enum": document_types},
                "language": {"type": "string", "enum": ["english", "hindi", "bilingual", "unknown"]},
            },
            "required": ["categories", "tags", "keywords", "document_type", "language"],
        }
        prompt = (
            "Classify this CommonSource document using only the supplied document text. "
            "Return ONLY a JSON object with keys categories, tags, keywords, document_type, and language. "
            "Each category must contain category and confidence_score. Do not invent subjects that are not present. "
            "Categories must come from the allowed taxonomy. Tags and keywords may cover any genre, "
            "not only health. Prefer specific, searchable phrases.\n\n"
            f"Allowed categories: {', '.join(category_names)}\n"
            f"Allowed document types: {', '.join(document_types)}\n"
            f"Title: {title}\nFilename: {filename}\n\nDocument text:\n{(text or '')[:12000]}"
        )
        result = llm_classify(
            prompt,
            schema=schema,
            preferred_model=os.getenv("COMMONSOURCE_LLM_MODEL", "gemini-2.5-flash"),
            max_tokens=900,
            timeout=float(os.getenv("COMMONSOURCE_METADATA_LLM_TIMEOUT", "45")),
        )
        raw = result.text or "{}"
        match = re.search(r"\{[\s\S]*\}", raw)
        payload = json.loads(match.group(0) if match else raw)
        payload["_provider"] = result.provider
        payload["_model"] = result.model
        return payload
    except (LLMProviderError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logging.getLogger(__name__).warning("[METADATA] LLM metadata generation failed: %s", exc)
        return {}


def classify_document(
    *,
    filename: str,
    text: str,
    explicit_title: str = "",
    source: str = "upload",
    content_sha1: str = "",
) -> Dict[str, Any]:
    title = extract_title(filename, text, explicit_title)
    combined = "\n".join([title, filename or "", text or ""])
    categories = classify_categories(combined)
    tags = classify_tags(combined, categories)
    local_language = detect_language(text)
    local_document_type = classify_document_type(title, filename, text)
    ai = ai_metadata_suggestions(title, filename, combined)
    known_categories = {item["category"] for item in categories}
    for item in ai.get("categories", []) if isinstance(ai.get("categories"), list) else []:
        category = item.get("category") if isinstance(item, dict) else item
        if category not in CATEGORY_TAXONOMY or category in known_categories:
            continue
        confidence = float(item.get("confidence_score") or 0.55) if isinstance(item, dict) else 0.55
        categories.append({
            "category": category,
            "confidence_score": round(min(max(confidence, 0.35), 0.75), 3),
            "matched_keywords": ["llm-secondary"],
        })
        known_categories.add(category)
    for tag in ai.get("tags", []) if isinstance(ai.get("tags"), list) else []:
        if isinstance(tag, str) and tag.strip() and not any(t["tag"].lower() == tag.strip().lower() for t in tags):
            tags.append({"tag": tag.strip()[:80], "category": "", "confidence_score": 0.7})
    categories.sort(key=lambda item: item["confidence_score"], reverse=True)
    tags.sort(key=lambda item: item["confidence_score"], reverse=True)
    generic_tags = generate_generic_tags(
        title=title,
        filename=filename,
        text=text,
        existing_tags=tags,
    )
    tags.extend(generic_tags)
    tags.sort(key=lambda item: item["confidence_score"], reverse=True)
    tags = tags[:20]
    if not categories:
        categories = [{"category": "Other", "confidence_score": 0.25, "matched_keywords": ["fallback"]}]
    keywords = extract_keywords(combined, categories, tags)
    known_keywords = {item["keyword"].lower() for item in keywords}
    for keyword in ai.get("keywords", []) if isinstance(ai.get("keywords"), list) else []:
        if isinstance(keyword, str) and keyword.strip() and keyword.strip().lower() not in known_keywords:
            keywords.append({"keyword": keyword.strip()[:100], "confidence_score": 0.65})
            known_keywords.add(keyword.strip().lower())
    keywords.sort(key=lambda item: item["confidence_score"], reverse=True)
    keywords = keywords[:30]
    ai_document_type = ai.get("document_type") if isinstance(ai.get("document_type"), str) else ""
    if local_document_type in {"Article", "Archive Content"} and ai_document_type in DOCUMENT_TYPE_RULES:
        local_document_type = ai_document_type
    ai_language = ai.get("language") if isinstance(ai.get("language"), str) else ""
    if local_language == "unknown" and ai_language in {"english", "hindi", "bilingual"}:
        local_language = ai_language
    chunks_estimate = max(1, len(text or "") // 900) if text else 0
    return {
        "title": title,
        "filename": filename,
        "language": local_language,
        "categories": categories,
        "tags": tags,
        "keywords": keywords,
        "word_count": len(re.findall(r"\S+", text or "")),
        "chunk_count": chunks_estimate,
        "document_type": local_document_type,
        "import_date": utc_now(),
        "source": source,
        "content_hash": content_sha1 or content_hash(text or ""),
        "classification_method": "commonsource-taxonomy+llm" if ai else "commonsource-taxonomy",
        "classification_provider": ai.get("_provider", "") if ai else "",
        "classification_model": ai.get("_model", "") if ai else "",
    }


def ensure_document_metadata_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS document_metadata (
            document_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            filename TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT '',
            document_type TEXT NOT NULL DEFAULT '',
            word_count INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            import_date TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(document_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_document_metadata_language ON document_metadata(language);
        CREATE INDEX IF NOT EXISTS idx_document_metadata_type ON document_metadata(document_type);
        CREATE INDEX IF NOT EXISTS idx_document_metadata_hash ON document_metadata(content_hash);

        CREATE TABLE IF NOT EXISTS document_categories (
            document_id TEXT NOT NULL,
            category TEXT NOT NULL,
            confidence_score REAL NOT NULL DEFAULT 0.5,
            matched_terms_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY(document_id, category),
            FOREIGN KEY(document_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_document_categories_category ON document_categories(category);

        CREATE TABLE IF NOT EXISTS document_tags (
            document_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            confidence_score REAL NOT NULL DEFAULT 0.5,
            category TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(document_id, tag),
            FOREIGN KEY(document_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_document_tags_tag ON document_tags(tag);

        CREATE TABLE IF NOT EXISTS document_keywords (
            document_id TEXT NOT NULL,
            keyword TEXT NOT NULL,
            confidence_score REAL NOT NULL DEFAULT 0.5,
            PRIMARY KEY(document_id, keyword),
            FOREIGN KEY(document_id) REFERENCES knowledge_assets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_document_keywords_keyword ON document_keywords(keyword);

        CREATE TABLE IF NOT EXISTS bulk_import_jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'queued',
            publisher_id TEXT NOT NULL DEFAULT '',
            total_files INTEGER NOT NULL DEFAULT 0,
            processed_files INTEGER NOT NULL DEFAULT 0,
            failed_files INTEGER NOT NULL DEFAULT 0,
            duplicate_files INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            options_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS bulk_import_items (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            stored_path TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            asset_id TEXT,
            error TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES bulk_import_jobs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_bulk_items_job_status ON bulk_import_items(job_id, status);
        CREATE INDEX IF NOT EXISTS idx_bulk_items_hash ON bulk_import_items(content_hash);
        """
    )


def persist_document_metadata(conn: sqlite3.Connection, document_id: str, metadata: Dict[str, Any], *, make_id) -> None:
    ensure_document_metadata_tables(conn)
    conn.execute("DELETE FROM document_categories WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM document_tags WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM document_keywords WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM article_tags WHERE article_id = ?", (document_id,))
    conn.execute(
        """
        INSERT OR REPLACE INTO document_metadata
          (document_id, title, filename, language, document_type, word_count, chunk_count,
           import_date, source, content_hash, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            metadata.get("title") or "",
            metadata.get("filename") or "",
            metadata.get("language") or "",
            metadata.get("document_type") or "",
            int(metadata.get("word_count") or 0),
            int(metadata.get("chunk_count") or 0),
            metadata.get("import_date") or utc_now(),
            metadata.get("source") or "",
            metadata.get("content_hash") or "",
            json.dumps(metadata, ensure_ascii=False),
        ),
    )
    for category in metadata.get("categories", []):
        conn.execute(
            """
            INSERT OR REPLACE INTO document_categories
              (document_id, category, confidence_score, matched_terms_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                document_id,
                category.get("category") or "",
                float(category.get("confidence_score") or 0.5),
                json.dumps(category.get("matched_keywords", []), ensure_ascii=False),
            ),
        )
    for tag in metadata.get("tags", []):
        tag_name = tag.get("tag") or ""
        if not tag_name:
            continue
        tag_slug = slugify(tag_name)
        existing_tag = conn.execute(
            "SELECT id FROM tags WHERE slug = ? OR lower(name) = lower(?) LIMIT 1",
            (tag_slug, tag_name),
        ).fetchone()
        tag_id = (existing_tag["id"] if hasattr(existing_tag, "keys") else existing_tag[0]) if existing_tag else f"tag_{tag_slug}"
        confidence = float(tag.get("confidence_score") or 0.5)
        conn.execute(
            """
            INSERT OR IGNORE INTO tags (id, name, slug, description, created_at)
            VALUES (?, ?, ?, '', ?)
            """,
            (tag_id, tag_name, tag_slug, utc_now()),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO article_tags (article_id, tag_id, confidence)
            VALUES (?, ?, ?)
            """,
            (document_id, tag_id, confidence),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO document_tags
              (document_id, tag, confidence_score, category)
            VALUES (?, ?, ?, ?)
            """,
            (document_id, tag_name, confidence, tag.get("category") or ""),
        )
    for keyword in metadata.get("keywords", []):
        key = keyword.get("keyword") or ""
        if not key:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO document_keywords
              (document_id, keyword, confidence_score)
            VALUES (?, ?, ?)
            """,
            (document_id, key, float(keyword.get("confidence_score") or 0.5)),
        )


def metadata_payload(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "category": ", ".join(item["category"] for item in metadata.get("categories", []) if item.get("category")),
        "categories": [item["category"] for item in metadata.get("categories", []) if item.get("category")],
        "tags": [item["tag"] for item in metadata.get("tags", []) if item.get("tag")],
        "keywords": [item["keyword"] for item in metadata.get("keywords", []) if item.get("keyword")],
        "language": metadata.get("language") or "",
        "document_type": metadata.get("document_type") or "",
        "word_count": int(metadata.get("word_count") or 0),
        "chunk_count": int(metadata.get("chunk_count") or 0),
    }
