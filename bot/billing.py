"""Diamond tariffs and shop packages (RUB prices for YooKassa invoices)."""

from __future__ import annotations

from dataclasses import dataclass

from bot.config import settings
from bot.formats import CutMode


@dataclass(frozen=True)
class DiamondPackage:
    id: str
    diamonds: int
    price_rub: int
    title: str
    subtitle: str

    @property
    def price_kopecks(self) -> int:
        return int(self.price_rub) * 100


# Pack pricing (~₽/💎 drops with size).
# Telegram Payments RUB min_amount = 6500 kopecks (65 ₽) → S cannot be cheaper.
# S 50💎 / 69₽ ≈ 1.38₽ · M 150/149 ≈ 0.99₽ · L 500/399 ≈ 0.80₽
DIAMOND_PACKAGES: tuple[DiamondPackage, ...] = (
    DiamondPackage(
        "s",
        50,
        69,
        "S · 50 💎",
        "69 ₽ · пара AI-обработок",
    ),
    DiamondPackage(
        "m",
        150,
        149,
        "M · 150 💎",
        "149 ₽ · выгоднее за 💎",
    ),
    DiamondPackage(
        "l",
        500,
        399,
        "L · 500 💎",
        "399 ₽ · максимум выгоды",
    ),
)


def package_by_id(package_id: str) -> DiamondPackage | None:
    for pkg in DIAMOND_PACKAGES:
        if pkg.id == package_id:
            return pkg
    return None


def parse_invoice_payload(payload: str) -> str | None:
    """Return package id from invoice payload `diamonds:<id>`."""
    raw = (payload or "").strip()
    if not raw.startswith("diamonds:"):
        return None
    pkg_id = raw.removeprefix("diamonds:").strip()
    if package_by_id(pkg_id) is None:
        return None
    return pkg_id


def estimate_job_cost(
    cut_mode: CutMode,
    *,
    burn_subs: bool,
    clip_count: int,
) -> int:
    """Total diamonds for a job given mode, subtitles, and clip count."""
    clips = max(0, int(clip_count))
    if cut_mode == CutMode.SEQUENTIAL:
        base = int(settings.price_sequential_base)
    else:
        base = int(settings.price_ai_base)
    subs = 0
    if burn_subs and clips > 0:
        subs = clips * int(settings.price_subtitles_per_clip)
    return base + subs


def minimum_job_cost(cut_mode: CutMode) -> int:
    """Cheapest possible cost for a mode (no subtitles)."""
    if cut_mode == CutMode.SEQUENTIAL:
        return int(settings.price_sequential_base)
    return int(settings.price_ai_base)


def tariffs_text() -> str:
    """User-facing tariff sheet."""
    ai = int(settings.price_ai_base)
    seq = int(settings.price_sequential_base)
    subs = int(settings.price_subtitles_per_clip)
    packs = "\n".join(
        f"• {p.title} — {p.price_rub} ₽" for p in DIAMOND_PACKAGES
    )
    return (
        "Тарифы (💎):\n\n"
        f"• Яркие моменты (AI) — {ai} 💎\n"
        f"• Нарезка подряд — {seq} 💎\n"
        f"• Субтитры — +{subs} 💎 за каждый клип\n"
        "• Формат (оригинал / вертикальный) — бесплатно\n\n"
        "Примеры:\n"
        f"• AI без субтитров — {ai} 💎\n"
        f"• AI + субтитры на 3 клипа — {ai + 3 * subs} 💎\n"
        f"• 10 кусков подряд без субтитров — {seq} 💎\n"
        f"• 10 кусков + субтитры — {seq + 10 * subs} 💎\n\n"
        "Пакеты алмазов:\n"
        f"{packs}"
    )
