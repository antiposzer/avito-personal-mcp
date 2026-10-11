"""Explainable seller-type inference from public listing descriptions only."""

from __future__ import annotations

import re
import unicodedata

VERSION = "description-rules-v1"
LABELS = {"private": "Частный продавец", "shop": "Магазин", "unknown": "Не определено"}
LOOKALIKES = str.maketrans(
    {
        "a": "а",
        "b": "в",
        "c": "с",
        "e": "е",
        "h": "н",
        "i": "и",
        "k": "к",
        "m": "м",
        "o": "о",
        "p": "р",
        "t": "т",
        "x": "х",
        "y": "у",
    }
)

# Patterns operate on normalized lookalikes: Avito descriptions often mix
# visually similar Latin and Cyrillic letters inside Russian words.
SHOP_RULES = [
    (
        "self_store",
        "Продавец прямо представляет магазин",
        (
            r"наш(?:ем|их|и|его|а|е)?\s+(?:\w+\s+){0,2}(?:магазин|салон|центр)",
            r"(?:приходите|приезжайте|приглашаем|добро\s+пожаловать).{0,65}(?:магазин|салон)",
            r"(?:магазин|салон)\s*[:«\"]",
            r"(?:фирменн\w+|интернет[- ]?)\s+(?:магазин|салон)",
            r"магазин\s+оригинальн\w*\s+продукци",
        ),
    ),
    (
        "retail_network",
        "Указана сеть магазинов или салонов",
        (r"сеть\s+(?:\w+\s+){0,3}(?:магазин\w*|салон\w*|филиал\w*)",),
    ),
    (
        "assortment",
        "Предлагается ассортимент или прямые поставки",
        (
            r"(?:широк\w*|больш\w*)\s+(?:ассортимент|выбор)",
            r"прям\w*\s+поставк\w*\s+(?:с|от)\s+(?:завод|производител)",
            r"прям\w*\s+поставк\w*\s+из\s+заграниц",
        ),
    ),
    (
        "buyback",
        "Продавец предлагает выкуп или скупку техники",
        (
            r"(?:выкупим|оценим|принимаем).{0,65}(?:ваш|трейд|тrаdе)",
            r"скупка\s+(?:телефон|ирноnе|айфон|техник)",
            r"обменяй(?:те)?.{0,35}(?:трейд|тrаdе)",
        ),
    ),
    (
        "professional_sales",
        "Продавец описывает профессиональную торговлю техникой",
        (r"занимаюсь.{0,35}(?:продаж|яблочн\w*\s+продукци)",),
    ),
]
SUPPORT_RULES = [
    (
        "seller_warranty",
        "Продавец предоставляет свою гарантию",
        (
            r"(?:даем|предоставляем)\s+(?:\w+\s+){0,2}гаранти",
            r"с\s+гарантией\s+магазина",
        ),
    ),
    (
        "opening_hours",
        "Указаны часы работы продавца",
        (r"(?:работаем|режим\s+работы|время\s+работы|ежедневно|каждый\s+день).{0,45}\d{1,2}",),
    ),
    ("financing", "Предлагается рассрочка или кредит", (r"рассрочк|кредит",)),
    (
        "business_payment",
        "Принимается оплата на расчётный счёт или через терминал",
        (r"расчетн\w*\s+счет|по\s+терминалу",),
    ),
    ("trade_in", "Предлагается трейд-ин", (r"(?:трейд|тrаdе)\s*[-–—]?\s*(?:ин|иn)",)),
    ("stock", "Указано наличие товара", (r"в\s+наличии",)),
    ("staff", "Указаны менеджеры или сотрудники", (r"менеджер|сотрудник",)),
    ("sku", "Указан товарный артикул", (r"артикул",)),
]
PRIVATE_RULES = [
    (
        "own_phone",
        "Продавец пишет, что продаёт свой или личный телефон",
        (r"(?:продаю|продам)\s+(?:свой|свою|личный|личную)",),
    ),
    (
        "personal_use",
        "Описано личное использование телефона",
        (
            r"пользовал(?:ся|ась|ись)|\bносил(?:а|и)?\b|в\s+пользовании",
            r"использовал(?:ся|ась|и)|носился\s+бережно",
            r"с\s+первого\s+дня\s+носил(?:ся|ась)",
        ),
    ),
    (
        "purchase_history",
        "Указана личная история покупки",
        (
            r"(?:покупал|покупала|покупали|покупался|куплен).{0,40}(?:себе|ребенку|новым|магазин|20\d{2})",
        ),
    ),
    (
        "personal_reason",
        "Указана личная причина продажи",
        (r"личн\w*\s+обстоятельств|причин\w*.{0,45}(?:купили|купил|перешел|новой\s+модел)",),
    ),
]


def _normalized(description: str) -> tuple[str, list[int]]:
    normalized = []
    offsets = []
    for index, char in enumerate(description):
        if unicodedata.category(char) == "Cf":
            continue
        for transformed in unicodedata.normalize("NFKC", char).casefold().translate(LOOKALIKES):
            normalized.append("е" if transformed == "ё" else transformed)
            offsets.append(index)
    return "".join(normalized), offsets


def _evidence(description: str, text: str, offsets: list[int], rules: list) -> list[dict]:
    evidence = []
    for code, reason, patterns in rules:
        found = None
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                prefix = text[max(0, match.start() - 55) : match.start()]
                # A buyer mentioning where they bought the phone, or saying
                # "not a shop", does not represent that shop as their business.
                if code == "self_store" and re.search(
                    r"(?:купил|куплен|покупал|покупала|приобрел|приобретен).{0,45}$|не\s+$",
                    prefix,
                ):
                    continue
                found = match
                break
            if found is not None:
                break
        if found is not None:
            start = offsets[max(0, found.start() - 25)]
            end = offsets[min(len(offsets) - 1, found.end() + 40)] + 1
            excerpt = " ".join(description[start:end].split())
            evidence.append({"code": code, "reason": reason, "excerpt": excerpt})
    return evidence


def infer_seller_type(description: str | None) -> dict:
    """Infer a type with evidence; never treat absence of shop clues as private."""

    if not isinstance(description, str) or not description.strip():
        return {
            "type": "unknown",
            "label": LABELS["unknown"],
            "confidence": "low",
            "basis": "description",
            "evidence": [],
            "version": VERSION,
        }
    text, offsets = _normalized(description)
    strong = _evidence(description, text, offsets, SHOP_RULES)
    support = _evidence(description, text, offsets, SUPPORT_RULES)
    personal = _evidence(description, text, offsets, PRIVATE_RULES)
    distinctive = {"seller_warranty", "opening_hours", "business_payment", "staff", "sku"}
    business = len(support) >= 2 and any(e["code"] in distinctive for e in support)
    if strong or business:
        kind = "shop"
        evidence = strong + support
        confidence = "high" if strong else "medium"
    elif personal and len(support) < 2:
        kind = "private"
        evidence = personal
        confidence = "medium"
    else:
        kind = "unknown"
        evidence = support + personal
        confidence = "low"
    return {
        "type": kind,
        "label": LABELS[kind],
        "confidence": confidence,
        "basis": "description",
        "evidence": evidence,
        "version": VERSION,
    }
