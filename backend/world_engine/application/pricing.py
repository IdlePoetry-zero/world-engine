"""Bundled public pricing snapshot for the official DeepSeek API."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from world_engine.adapters.models.contracts import PriceVersion, ProviderConfig, ProviderKind


VERIFIED_FROM = datetime(2026, 11, 14, tzinfo=timezone.utc)
REVIEW_AFTER = VERIFIED_FROM + timedelta(days=30)
SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
HOLIDAY_CALENDAR_SOURCE = "https://www.beijing.gov.cn/zhengce/zhengcefagui/202511/t20251104_4258873.html"


def bundled_price(config: ProviderConfig, model: str, attempt_start: datetime) -> PriceVersion | None:
    if config.kind != ProviderKind.DEEPSEEK or config.base_url.rstrip("/") != "https://api.deepseek.com":
        return None
    if model != "deepseek-flash" or attempt_start.tzinfo is None or attempt_start.utcoffset() is None:
        return None
    utc = attempt_start.astimezone(timezone.utc)
    if not VERIFIED_FROM <= utc < REVIEW_AFTER:
        return None
    # The official 2026 holiday schedule (HOLIDAY_CALENDAR_SOURCE) has no
    # public holidays in this verified price window. Do not extrapolate past it.
    peak = utc.weekday() < 5 and (1 <= utc.hour < 4 or 6 <= utc.hour < 10)
    return PriceVersion(
        provider_id=config.provider_id, model_id=model, currency="USD",
        source_url=SOURCE, effective_from=VERIFIED_FROM,
        cached_input_per_million=Decimal("0.006" if peak else "0.003"),
        input_per_million=Decimal("0.3" if peak else "0.15"),
        output_per_million=Decimal("1.2" if peak else "0.6"),
    )
