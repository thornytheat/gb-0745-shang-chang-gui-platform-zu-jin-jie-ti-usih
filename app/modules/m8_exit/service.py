from collections import Counter
from datetime import date

from app.modules.m1_intake.amounts import cents
from app.modules.m1_intake.service import transaction_key

HOLD_REASONS = {
    "EXIT_DUP": "同一柜台重复登记撤场",
    "EXIT_NO_DATE": "合同终止日缺失、无效或不在本账期",
    "EXIT_NO_DEPOSIT": "保证金金额缺失或无效",
    "EXIT_NO_CONTRACT": "柜台无合同或终止日非最终合同截止日",
    "EXIT_BAD_FIELD": "撤场登记字段无效",
}

# 保证金不足以同时覆盖时的扣款顺序：已确定债务优先于预留额度。
WATERFALL = ("旧账欠款", "末期租金及抽成", "迟到退单预留", "场地恢复预留", "遗留售后预留")


def hold(shop, code):
    return {"柜台": shop, "原因码": code, "说明": HOLD_REASONS[code]}


def parse_exits(records, final_ends, start, end):
    entries = []
    for index, item in enumerate(records, 1):
        shop = None
        if isinstance(item, dict):
            try:
                shop = transaction_key(item, "shop")
            except (ValueError, KeyError, TypeError):
                shop = None
        entries.append((index, shop, item))
    counts = Counter(shop for _, shop, _ in entries if shop)
    valid, holds = [], []
    for index, shop, item in entries:
        if shop is None:
            holds.append(hold(f"第{index}行", "EXIT_BAD_FIELD"))
            continue
        if counts[shop] > 1:
            holds.append(hold(shop, "EXIT_DUP"))
            continue
        try:
            terminated = date.fromisoformat(str(item.get("terminated", "")))
            if not start <= terminated < end:
                raise ValueError("终止日不在本账期")
        except ValueError:
            holds.append(hold(shop, "EXIT_NO_DATE"))
            continue
        try:
            deposit = cents(item.get("deposit"))
        except ValueError:
            holds.append(hold(shop, "EXIT_NO_DEPOSIT"))
            continue
        try:
            prior = cents(item.get("prior", "0"))
            restore = cents(item.get("restore", "0"))
            aftercare = cents(item.get("aftercare", "0"))
        except ValueError:
            holds.append(hold(shop, "EXIT_BAD_FIELD"))
            continue
        if final_ends.get(shop) != terminated:
            holds.append(hold(shop, "EXIT_NO_CONTRACT"))
            continue
        valid.append(
            dict(
                shop=shop,
                terminated=terminated,
                deposit=deposit,
                prior=prior,
                restore=restore,
                aftercare=aftercare,
            )
        )
    return valid, holds


def late_refunds(shop, events, sales, consumed, end):
    """撤场后（账期截止后）才入账的退单，按与原销售相同的规则核对后计入预留。"""
    total, refs = 0, []
    accum = Counter(consumed)
    pending = sorted(
        (e for e in events if e["kind"] == "refund" and e["shop"] == shop and e["day"] >= end),
        key=lambda e: (e["day"], e["id"]),
    )
    for event in pending:
        sale = sales.get(event["origin"])
        if sale is None or sale["shop"] != shop or event["day"] < sale["day"]:
            continue
        if accum[event["origin"]] + event["cents"] > sale["cents"]:
            continue
        accum[event["origin"]] += event["cents"]
        total += event["cents"]
        refs.append(f"{event['id']}（原单 {event['origin']}）")
    return total, refs


def settle_exits(records, *, contracts, period, window, reconcile_rows, sales, consumed, events):
    start, end = window
    final_ends = {}
    for contract in contracts:
        shop = contract["shop"]
        final_ends[shop] = max(final_ends.get(shop, contract["end"]), contract["end"])
    valid, holds = parse_exits(records, final_ends, start, end)
    reconcile_by_shop = {row["柜台"]: row for row in reconcile_rows}
    summary, details = [], []
    for entry in valid:
        shop = entry["shop"]
        row = reconcile_by_shop.get(shop)
        due = row["应缴分"] if row else 0
        paid = row["实缴分"] if row else 0
        note = ""
        if paid is None:
            paid, note = 0, " · 缺缴款单按实缴 0 计"
        difference = due - paid
        late_total, late_refs = late_refunds(shop, events, sales, consumed, end)
        lines = [
            ("旧账欠款", entry["prior"], "撤场登记 · 往期月结欠缴"),
            ("末期租金及抽成", max(difference, 0), f"账期 {period} 月结{note}"),
            ("迟到退单预留", late_total, "退单 " + "、".join(late_refs) if late_refs else "无"),
            ("场地恢复预留", entry["restore"], "撤场登记 · 场地恢复"),
            ("遗留售后预留", entry["aftercare"], "撤场登记 · 遗留售后"),
        ]
        remaining = entry["deposit"]
        charged = owed = 0
        for label, amount, trace in lines:
            if amount <= 0:
                continue
            taken = min(remaining, amount)
            remaining -= taken
            charged += taken
            owed += amount - taken
            details.append(
                {
                    "柜台": shop,
                    "项目": label,
                    "应扣分": amount,
                    "实扣分": taken,
                    "未覆盖分": amount - taken,
                    "溯源": trace,
                }
            )
        credit = max(-difference, 0)
        refund = remaining + credit
        summary.append(
            {
                "柜台": shop,
                "合同终止日": entry["terminated"].isoformat(),
                "保证金分": entry["deposit"],
                "应扣合计分": charged + owed,
                "实扣合计分": charged,
                "还欠分": owed,
                "多缴退回分": credit,
                "应退分": refund,
                "清算状态": "欠费挂账" if owed else ("结清退还" if refund else "结清无退"),
            }
        )
    summary.sort(key=lambda row: row["柜台"])
    totals = {
        "撤场柜台": len(summary),
        "挂起柜台": len(holds),
        "保证金合计分": sum(row["保证金分"] for row in summary),
        "实扣合计分": sum(row["实扣合计分"] for row in summary),
        "还欠合计分": sum(row["还欠分"] for row in summary),
        "应退合计分": sum(row["应退分"] for row in summary),
    }
    return summary, details, holds, totals
