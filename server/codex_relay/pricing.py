"""API 等价估算费用。

见 01_DEVELOPMENT_SPEC.md 第 11.1 节。四种口径必须分开，本模块只算第一种：

    I = 输入 Token 总量
    H = 其中缓存命中的输入 Token
    O = 输出 Token 总量
    估价 = ((I-H) * P_input + H * P_cached + O * P_output) / 1,000,000

规则要求：
* 单价与币种保存在版本化规则里，**不从账号限额反推**。
* 校验 0 <= H <= I；reasoning_tokens 若包含在 O 中则不另收费。
* 用 Decimal 累加，按原始精度累加后统一展示舍入（不逐条四舍五入）。
* 没有匹配规则时 unavailable / partial，**不能**默认免费或拿相似模型代替。
* 缺 cache_write 数据而规则需要它时，必须显示排除项，不能假装完整。
* 价格更新只影响生效后的请求；历史记录保留当时的 price_version。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from .models import NormalizedUsage

MILLION = Decimal(1_000_000)

EstimateStatus = Literal["ok", "partial", "unavailable", "invalid"]


class PriceRuleError(Exception):
    pass


@dataclass(frozen=True)
class PriceRule:
    """一条不可变的估价规则。单价为每 100 万 Token 的十进制字符串。"""

    price_version: str
    model: str
    formula: str
    currency: str
    effective_from: str
    service_tier: str | None = None
    modality: str = "text"
    input_context_min: int = 0
    input_context_max: int | None = None
    cache_write_policy: str = "not_applicable"
    price_input: Decimal = Decimal(0)
    price_cached_input: Decimal | None = None
    price_output: Decimal | None = None
    price_cache_write: Decimal | None = None
    excluded_costs: tuple[str, ...] = ()
    source_url: str | None = None
    verified_at: str | None = None

    def covers(self, model: str, input_tokens: int | None, service_tier: str | None) -> bool:
        if model != self.model:
            return False
        if service_tier is not None and self.service_tier is not None:
            if service_tier != self.service_tier:
                return False
        if input_tokens is not None:
            if input_tokens < self.input_context_min:
                return False
            if self.input_context_max is not None and input_tokens > self.input_context_max:
                return False
        return True

    def specificity(self) -> int:
        """上下文区间越窄越具体，优先匹配。"""

        breadth = 0 if self.input_context_max is None else self.input_context_max - self.input_context_min
        tier_bonus = 1 if self.service_tier else 0
        return tier_bonus * 1_000_000 - breadth


@dataclass(frozen=True)
class EstimateResult:
    status: EstimateStatus
    estimated_cost: Decimal | None
    currency: str | None
    price_version: str | None
    reasoning: str | None = None
    excluded_costs: tuple[str, ...] = ()

    @property
    def display_cost(self) -> str | None:
        if self.estimated_cost is None:
            return None
        # 展示时统一舍入；累加阶段保持原始精度。
        return str(self.estimated_cost.quantize(Decimal("0.000001")))


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        raise PriceRuleError(f"规则缺少 {field}")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise PriceRuleError(f"规则 {field} 不是合法十进制：{value!r}") from exc
    if result < 0:
        raise PriceRuleError(f"规则 {field} 不能为负")
    return result


def rule_from_dict(raw: dict[str, Any]) -> PriceRule:
    if not isinstance(raw, dict):
        raise PriceRuleError("规则必须是对象")
    if raw.get("test_only"):
        raise PriceRuleError("不能把 test_only 规则加载为生产规则")

    prices = raw.get("prices_per_million")
    if not isinstance(prices, dict):
        raise PriceRuleError("规则缺少 prices_per_million 对象")

    cache_policy = raw.get("cache_write_policy", "not_applicable")
    if cache_policy not in ("not_applicable", "included_in_input", "separate"):
        raise PriceRuleError(f"未知 cache_write_policy：{cache_policy}")

    excluded = raw.get("excluded_costs") or []
    if not isinstance(excluded, list) or not all(isinstance(x, str) for x in excluded):
        raise PriceRuleError("excluded_costs 必须是字符串数组")

    return PriceRule(
        price_version=str(raw["price_version"]),
        model=str(raw["model"]),
        formula=str(raw.get("formula", "input_cached_output_v1")),
        currency=str(raw.get("currency", "USD")),
        effective_from=str(raw["effective_from"]),
        service_tier=raw.get("service_tier"),
        modality=str(raw.get("modality", "text")),
        input_context_min=int(raw.get("input_context_min", 0)),
        input_context_max=raw.get("input_context_max"),
        cache_write_policy=cache_policy,
        price_input=_decimal(prices.get("input"), "prices_per_million.input"),
        price_cached_input=(
            _decimal(prices["cached_input"], "prices_per_million.cached_input")
            if prices.get("cached_input") is not None
            else None
        ),
        price_output=(
            _decimal(prices["output"], "prices_per_million.output")
            if prices.get("output") is not None
            else None
        ),
        price_cache_write=(
            _decimal(prices["cache_write"], "prices_per_million.cache_write")
            if prices.get("cache_write") is not None
            else None
        ),
        excluded_costs=tuple(excluded),
        source_url=raw.get("source_url"),
        verified_at=raw.get("verified_at"),
    )


class PriceBook:
    """按 effective_from 与上下文区间筛选规则的集合。"""

    def __init__(self, rules: list[PriceRule] | None = None) -> None:
        self._rules: list[PriceRule] = list(rules or [])

    def __len__(self) -> int:
        return len(self._rules)

    @property
    def rules(self) -> list[PriceRule]:
        return list(self._rules)

    @classmethod
    def from_dicts(cls, raws: list[dict[str, Any]]) -> PriceBook:
        return cls([rule_from_dict(r) for r in raws])

    def add(self, rule: PriceRule) -> None:
        self._rules.append(rule)

    def select(
        self,
        model: str | None,
        *,
        input_tokens: int | None,
        service_tier: str | None,
        at: str,
    ) -> PriceRule | None:
        """选出在 at 时刻已生效、覆盖该模型与上下文区间的最具体规则。"""

        if not model:
            return None
        candidates = [
            rule
            for rule in self._rules
            if rule.effective_from <= at and rule.covers(model, input_tokens, service_tier)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda r: (r.specificity(), r.effective_from), reverse=True)
        return candidates[0]

    # --- 估价 ---------------------------------------------------------

    def estimate(
        self,
        usage: NormalizedUsage,
        *,
        model: str | None,
        at: str,
        service_tier: str | None = None,
    ) -> EstimateResult:
        """对规范化 usage 估价。

        BILL03: 必要项缺失 -> partial / missing，不默认 0。
        BILL04: H>I 或负 token -> invalid，绝不产出看似正常的价格。
        BILL05: 没有匹配规则 -> unavailable；没有价格不是免费。
        BILL06: 规则需要 cache_write 而上游未提供 -> unavailable（记排除项）。
        """

        if usage.status == "invalid":
            return EstimateResult("invalid", None, None, None, usage.problem or "invalid_usage")
        if usage.status == "missing" or usage.input_tokens is None or usage.output_tokens is None:
            return EstimateResult(
                "unavailable", None, None, None, "usage_missing_required_fields"
            )

        rule = self.select(
            model, input_tokens=usage.input_tokens, service_tier=service_tier, at=at
        )
        if rule is None:
            return EstimateResult("unavailable", None, None, None, "no_matching_price_rule")

        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        cached = usage.cached_input_tokens

        # 规则区分缓存读取价格，但上游没给 cached 明细：无法完整估价。
        if rule.price_cached_input is not None and cached is None:
            return EstimateResult(
                "unavailable",
                None,
                rule.currency,
                rule.price_version,
                "cached_input_tokens_not_reported",
                rule.excluded_costs,
            )

        if rule.price_output is None:
            return EstimateResult(
                "unavailable",
                None,
                rule.currency,
                rule.price_version,
                "rule_missing_output_price",
                rule.excluded_costs,
            )

        cache_write = usage.cache_write_tokens
        if rule.cache_write_policy == "separate":
            if cache_write is None or rule.price_cache_write is None:
                return EstimateResult(
                    "unavailable",
                    None,
                    rule.currency,
                    rule.price_version,
                    "cache_write_tokens_not_reported",
                    rule.excluded_costs,
                )

        cached_tokens = cached or 0
        # 再次防御：0 <= H <= I。normalize 已检查，这里不依赖调用方。
        if cached_tokens < 0 or cached_tokens > input_tokens:
            return EstimateResult("invalid", None, None, None, "cached_input_exceeds_input")

        uncached_input = input_tokens - cached_tokens
        if uncached_input < 0 or output_tokens < 0:
            return EstimateResult("invalid", None, None, None, "negative_token_count")

        cached_price = rule.price_cached_input if rule.price_cached_input is not None else rule.price_input

        # reasoning_tokens 已包含在 output_tokens 中，不另收费。
        total = (
            Decimal(uncached_input) * rule.price_input
            + Decimal(cached_tokens) * cached_price
            + Decimal(output_tokens) * rule.price_output
        )
        if rule.cache_write_policy == "separate" and cache_write and rule.price_cache_write is not None:
            total += Decimal(cache_write) * rule.price_cache_write

        cost = total / MILLION

        status: EstimateStatus = "ok"
        if rule.excluded_costs:
            # 有明确排除项时仍是可用估价，但必须带出排除说明。
            status = "ok"
        return EstimateResult(status, cost, rule.currency, rule.price_version, None, rule.excluded_costs)


def load_rules_for_db(db: Any, *, at: str | None = None) -> list[PriceRule]:
    """从 price_rules 表读取已生效规则。"""

    from .db import utc_now

    at = at or utc_now()
    rows = db.query(
        "SELECT * FROM price_rules WHERE effective_from <= ? ORDER BY effective_from",
        (at,),
    )
    rules: list[PriceRule] = []
    for row in rows:
        import json as _json

        rules.append(
            PriceRule(
                price_version=row["price_version"],
                model=row["model"],
                formula=row["formula"],
                currency=row["currency"],
                effective_from=row["effective_from"],
                service_tier=row["service_tier"],
                modality=row["modality"],
                input_context_min=row["input_context_min"],
                input_context_max=row["input_context_max"],
                cache_write_policy=row["cache_write_policy"],
                price_input=Decimal(row["price_input"]),
                price_cached_input=(
                    Decimal(row["price_cached_input"]) if row["price_cached_input"] is not None else None
                ),
                price_output=(
                    Decimal(row["price_output"]) if row["price_output"] is not None else None
                ),
                price_cache_write=(
                    Decimal(row["price_cache_write"]) if row["price_cache_write"] is not None else None
                ),
                excluded_costs=tuple(_json.loads(row["excluded_costs_json"] or "[]")),
                source_url=row["source_url"],
                verified_at=row["verified_at"],
            )
        )
    return rules


def insert_price_rule(db: Any, raw: dict[str, Any], *, created_by: str | None = None) -> PriceRule:
    """写入一条新规则。规则不可变：同一版本 + 同一适用范围视为冲突而不是覆盖。

    注意：SQLite 的 UNIQUE 约束里 NULL 互不相等，因此 `service_tier` 为 NULL 时
    唯一索引不会触发；这里显式做一次存在性检查（并发下仍有 UNIQUE 兜底，
    因为 UNIQUE 里的 NULL 差异不会让同一行重复成功）。
    """

    import json as _json

    from .db import utc_now

    rule = rule_from_dict(raw)
    now = utc_now()

    def _s(value: Decimal | None) -> str | None:
        return None if value is None else str(value)

    if rule.cache_write_policy == "separate" and rule.price_cache_write is None:
        raise PriceRuleError("cache_write_policy=separate 时必须提供 prices_per_million.cache_write")

    with db.write() as conn:
        duplicate = conn.execute(
            "SELECT rule_id FROM price_rules WHERE price_version = ? AND model = ? "
            "AND COALESCE(service_tier,'') = COALESCE(?,'') AND input_context_min = ?",
            (rule.price_version, rule.model, rule.service_tier, rule.input_context_min),
        ).fetchone()
        if duplicate is not None:
            raise PriceRuleError(
                "该 price_version 的同一适用范围规则已存在；价格规则不可变，请使用新的版本号。"
            )

        conn.execute(
            "INSERT INTO price_rules (price_version, model, service_tier, modality, "
            "input_context_min, input_context_max, formula, cache_write_policy, "
            "price_input, price_cached_input, price_output, price_cache_write, currency, "
            "excluded_costs_json, source_url, verified_at, effective_from, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rule.price_version,
                rule.model,
                rule.service_tier,
                rule.modality,
                rule.input_context_min,
                rule.input_context_max,
                rule.formula,
                rule.cache_write_policy,
                str(rule.price_input),
                _s(rule.price_cached_input),
                _s(rule.price_output),
                _s(rule.price_cache_write),
                rule.currency,
                _json.dumps(list(rule.excluded_costs), ensure_ascii=False),
                rule.source_url,
                rule.verified_at,
                rule.effective_from,
                created_by,
                now,
            ),
        )
    return rule
