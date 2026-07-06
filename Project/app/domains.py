"""
domains.py — GAT Platform domain-pack taxonomy.

GAT is an in-house platform for uncertainty modelling, scenario stress-testing,
and facilitated simulation design across transition domains.

Decarbonisation is one domain pack. Others sit beside it as equal peers.
This module defines the canonical domain taxonomy and helpers for classification.
"""

from __future__ import annotations
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Domain pack definitions
# Each pack has: id, label, description, keywords (for lightweight auto-tagging)
# ---------------------------------------------------------------------------

DOMAIN_PACKS: Dict[str, Dict] = {
    "decarbonisation": {
        "id": "decarbonisation",
        "label": "Decarbonisation & Energy Transition",
        "description": (
            "Net-zero pathways, fossil fuel phase-out, renewable energy systems, "
            "carbon pricing, just transition for fossil-fuel-dependent communities."
        ),
        "keywords": [
            "decarbonisation", "decarbonization", "net zero", "net-zero", "carbon",
            "emissions", "fossil fuel", "renewable energy", "climate policy",
            "energy transition", "just transition", "carbon tax", "cap and trade",
            "stranded assets", "green economy", "Paris agreement",
        ],
    },
    "future_of_work": {
        "id": "future_of_work",
        "label": "Future of Work",
        "description": (
            "Labour market transformation, skills and retraining, precarious employment, "
            "platform economies, worker representation, and the changing nature of work."
        ),
        "keywords": [
            "future of work", "labour market", "labor market", "employment", "unemployment",
            "skills", "reskilling", "upskilling", "gig economy", "platform work",
            "precarious work", "trade unions", "worker rights", "automation of work",
            "remote work", "hybrid work", "workforce", "job displacement",
        ],
    },
    "democracy_and_civic": {
        "id": "democracy_and_civic",
        "label": "Democracy, Civic Rights & Political Economy",
        "description": (
            "Democratic systems, political participation, caste and identity in politics, "
            "civic rights, constitutional order, social movements, and political economy "
            "of inequality and power."
        ),
        "keywords": [
            "democracy", "democratic", "caste", "dalit", "brahmin", "adivasi",
            "civic rights", "civil rights", "political participation", "suffrage",
            "constitution", "parliament", "legislature", "political party", "election",
            "political economy", "social movement", "protest", "collective rights",
            "citizenship", "representation", "majoritarianism", "minority rights",
            "communalism", "secularism", "federalism", "decentralisation", "panchayat",
            "civic engagement", "public sphere", "deliberative democracy",
            "liberal democracy", "illiberal", "authoritarianism", "populism",
        ],
    },
    "industrial_transition": {
        "id": "industrial_transition",
        "label": "Industrial Transition",
        "description": (
            "Manufacturing sector transformation, supply chain restructuring, industrial policy, "
            "green industrialisation, and regional economic adaptation."
        ),
        "keywords": [
            "industrial", "manufacturing", "supply chain", "industrial policy", "value chain",
            "deindustrialisation", "reindustrialisation", "green manufacturing",
            "reshoring", "near-shoring", "factory", "production", "industry 4.0",
        ],
    },
    "ai_and_automation": {
        "id": "ai_and_automation",
        "label": "AI & Automation",
        "description": (
            "Artificial intelligence deployment, algorithmic governance, automation effects "
            "on employment and society, AI regulation, and technology transition governance."
        ),
        "keywords": [
            "artificial intelligence", "AI", "machine learning", "automation", "robot",
            "algorithm", "algorithmic", "digital transformation", "technology disruption",
            "AI governance", "AI regulation", "large language model", "LLM",
            "generative AI", "autonomous systems",
        ],
    },
    "local_economies": {
        "id": "local_economies",
        "label": "Local Economies & Place-Based Development",
        "description": (
            "Regional economic resilience, rural-urban dynamics, local enterprise, "
            "community wealth building, and spatially uneven development."
        ),
        "keywords": [
            "local economy", "regional development", "place-based", "rural", "urban",
            "community wealth", "local enterprise", "small business", "SME", "MSME",
            "regional inequality", "levelling up", "economic geography",
            "anchor institutions", "cooperative", "social enterprise",
        ],
    },
    "labour": {
        "id": "labour",
        "label": "Labour & Livelihoods",
        "description": (
            "Decent work, income security, informal economy, social protection, "
            "livelihood diversification, and labour rights in the Global South."
        ),
        "keywords": [
            "livelihoods", "decent work", "informal economy", "informal sector",
            "social protection", "safety net", "household income", "poverty",
            "labour rights", "labour standards", "wages", "ILO",
            "livelihood diversification", "subsistence", "food security",
            "smallholder", "migrant labour", "bonded labour", "domestic work",
        ],
    },
    "climate_adaptation": {
        "id": "climate_adaptation",
        "label": "Climate Adaptation & Resilience",
        "description": (
            "Adaptation to climate impacts, disaster risk reduction, resilient infrastructure, "
            "loss and damage, nature-based solutions, and community adaptation."
        ),
        "keywords": [
            "climate adaptation", "climate resilience", "adaptation", "resilience",
            "disaster risk", "flood", "drought", "extreme weather", "loss and damage",
            "nature-based solutions", "ecosystem services", "climate finance",
            "WASH", "food security", "climate vulnerability",
        ],
    },
    "digital_governance": {
        "id": "digital_governance",
        "label": "Digital Governance",
        "description": (
            "Digital public infrastructure, data governance, internet regulation, "
            "e-government, digital rights, and the governance of emerging technologies."
        ),
        "keywords": [
            "digital governance", "data governance", "digital rights", "internet regulation",
            "e-government", "digital public infrastructure", "data protection", "GDPR",
            "digital identity", "cybersecurity", "open data", "platform regulation",
            "content moderation", "surveillance",
        ],
    },
    "fiction_worldbuilding": {
        "id": "fiction_worldbuilding",
        "label": "Fiction & Worldbuilding",
        "description": (
            "Fiction, storytelling craft, worldbuilding technique, character design, "
            "and narrative structures that inform simulation scenario writing. "
            "Source material for writing prompts, dramatic tension, actor voice, "
            "and setting texture."
        ),
        "keywords": [
            "novel", "fiction", "story", "narrator", "character", "plot", "scene",
            "dialogue", "setting", "atmosphere", "tension", "protagonist", "antagonist",
            "worldbuilding", "narrative", "prose", "chapter", "literary",
            "short story", "detective", "mystery", "thriller", "magic",
            "imagination", "metaphor", "writing craft", "point of view",
            "dramatic", "conflict", "arc", "voice", "genre",
        ],
    },
}

DOMAIN_ALIASES: Dict[str, str] = {
    "labour_and_livelihoods": "labour",
    "narrative_craft": "fiction_worldbuilding",
}

# Ordered list for display
DOMAIN_PACK_IDS: List[str] = [
    "decarbonisation",
    "fiction_worldbuilding",
    "labour",
    "future_of_work",
    "industrial_transition",
    "ai_and_automation",
    "local_economies",
    "climate_adaptation",
    "digital_governance",
    "democracy_and_civic",
]


def normalize_domain_pack_id(domain_pack_id: Optional[str]) -> Optional[str]:
    if not domain_pack_id:
        return domain_pack_id
    return DOMAIN_ALIASES.get(domain_pack_id, domain_pack_id)


def get_domain_pack(domain_pack_id: str) -> Optional[Dict]:
    normalized = normalize_domain_pack_id(domain_pack_id)
    if not normalized:
        return None
    return DOMAIN_PACKS.get(normalized)


# ---------------------------------------------------------------------------
# Lightweight domain classifier (keyword-based, no ML required)
# ---------------------------------------------------------------------------

def classify_domain(text: str, top_n: int = 3) -> List[Dict]:
    """
    Classify a document into domain packs based on keyword frequency.

    Returns a list of dicts: [{domain_id, label, score, matched_keywords}]
    sorted by score descending. Returns at most top_n results.
    This is the Phase 1 pre-AI approach: reliable, transparent, no ML needed.
    """
    text_lower = text.lower()
    scores = []
    for pack_id in DOMAIN_PACK_IDS:
        pack = DOMAIN_PACKS[pack_id]
        matched = [kw for kw in pack["keywords"] if kw.lower() in text_lower]
        score = len(matched)
        if score > 0:
            scores.append({
                "domain_id": pack_id,
                "label": pack["label"],
                "score": score,
                "matched_keywords": matched[:8],  # cap for storage
            })

    scores.sort(key=lambda x: x["score"], reverse=True)
    return scores[:top_n]


def primary_domain(text: str) -> Optional[str]:
    """Return the single best-matching domain pack id, or None."""
    results = classify_domain(text, top_n=1)
    return results[0]["domain_id"] if results else None
