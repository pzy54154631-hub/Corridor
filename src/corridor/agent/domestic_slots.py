"""Bounded exact language candidates, never signed inputs or execution authority."""

import calendar
import re
from datetime import date, timedelta
from fractions import Fraction

from corridor.schemas.money import fraction_string
from corridor.stores.bank import business_date

_DIGITS = {
    char: value
    for value, chars in enumerate(("零〇", "一", "二两", "三", "四", "五", "六", "七", "八", "九"))
    for char in chars
}
_NUMBER = r"[0-9零〇一二两三四五六七八九十百千万点.]+"
_TOKEN = re.compile(_NUMBER)
_MONEY_TOKEN = re.compile(rf"{_NUMBER}(?:[元块](?:{_NUMBER}[角毛](?:{_NUMBER}分)?|{_NUMBER}分))?")
_APPROXIMATE = re.compile(r"大概|大约|大致|约莫|差不多|左右")


def _spoken_transfer(text: str):
    """Only complete outgoing forms; names still require trusted contact resolution."""
    name = r"[\u4e00-\u9fffA-Za-z_]+?"
    amount = rf"(?P<amount>{_NUMBER})(?:元|块钱|块)?"
    for form in (
        rf"给(?P<name>{name})(?:转账|转|打){amount}",
        rf"(?:转账给|转给)(?P<name>{name}){amount}",
        rf"(?:转账|转){amount}给(?P<name>{name})",
    ):
        match = re.fullmatch(rf"(?:帮我|请|麻烦)?{form}(?:吧|啊|呀)?[。！!]?", text)
        if match:
            return match
    return None


def spoken_transfer(text: str) -> str:
    """Normalize a whole request, without supplying a recipient or amount."""
    match = _spoken_transfer(text)
    if match is None:
        return text
    return f"给{match.group('name')}转{match.group('amount')}元"


def chinese_number(text: str) -> Fraction | None:
    """Accept conventional descending units, including 三千二 = 3200."""
    if len(text) > 32:
        return None
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?万?", text):
        return Fraction(text.removesuffix("万")) * (10000 if text.endswith("万") else 1)
    if "万" in text:
        if text.count("万") != 1:
            return None
        high, low = text.split("万")
        leading = chinese_number(high)
        trailing = chinese_number(low) if low else Fraction(0)
        if leading is None or trailing is None or leading >= 10000 or trailing >= 10000:
            return None
        if len(low) == 1 and (low in _DIGITS or low in "0123456789"):
            trailing *= 1000
        return leading * 10000 + trailing
    if not text or any(char not in _DIGITS and char not in "十百千" for char in text):
        return None
    total, digit, last_unit, zero = 0, None, 10000, False
    for index, char in enumerate(text):
        if char in _DIGITS:
            current = _DIGITS[char]
            if current == 0:
                if digit is not None or zero:
                    return None
                zero = True
            else:
                if digit is not None:
                    return None
                digit = current
        else:
            unit = {"十": 10, "百": 100, "千": 1000}[char]
            if unit >= last_unit or (digit is None and not (index == 0 and unit == 10)):
                return None
            total += (digit or 1) * unit
            digit, last_unit, zero = None, unit, False
    if digit is not None:
        total += digit * (last_unit // 10 if 100 <= last_unit < 10000 and not zero else 1)
    elif zero:
        return None
    return Fraction(total)


def _money_match(text):
    if _APPROXIMATE.search(text) or re.search(r"分笔|分批", text):
        return None
    spoken = _spoken_transfer(text)
    if spoken is not None:
        return _MONEY_TOKEN.match(text, spoken.start("amount"))
    candidates = []
    for match in _MONEY_TOKEN.finditer(text):
        before, after = text[: match.start()], text[match.end() :]
        if (
            re.match(r"\s*(?:元|块|闲钱)", after)
            or re.search(r"[元块].*[角毛分]$", match.group())
            or re.search(r"(?:¥|￥|转|转账|汇款|打钱|买|调到|再给他|再给她)\s*$", before)
            or ("万" in match.group() and not re.match(r"[年月日天个]", after))
            or (re.search(r"aa", text, re.I) and re.search(r"(?:饭|账单|总共|共)\s*$", before))
        ):
            candidates.append(match)
    if len(candidates) != 1:
        return None
    match = candidates[0]
    before, after = text[: match.start()], text[match.end() :]
    # Qualifiers still qualify the amount when a currency unit separates them.
    after_currency = re.sub(r"^\s*(?:块钱|元|块)\s*", "", after)
    if (
        re.search(r"[\d.,，/+\-]\s*$", before)
        or re.match(r"\s*[\d.,，/+\-]", after)
        or re.search(r"(?:大概|大约|差不多|约|或|至|(?<!调)到)\s*$", before)
        or re.match(r"[\s，,、]*(?:多|来|左右|或者|或|至|到|亿|兆|点)", after_currency)
        or re.match(r"\s*(?:笔|次|下|个|人|份|角|毛|分)", after_currency)
        or re.match(rf"\s*{_NUMBER}", after_currency)
    ):
        return None
    return match


def money_span(text: str) -> tuple[int, int] | None:
    """C1: where the one money phrase stands in the text (start, end), or None."""
    match = _money_match(text)
    if match is None:
        return None
    end = match.end()
    unit = re.match(r"\s*(?:块钱|元|块|圆)", text[end:])
    return match.start(), end + (unit.end() if unit else 0)


def money_candidate(text: str, previous: str | None = None) -> Fraction | None:
    match = _money_match(text)
    if match is None:
        without_reference = text.replace("同样金额", "").replace("同一金额", "")
        if (
            previous
            and without_reference != text
            and _TOKEN.search(without_reference) is None
            and _APPROXIMATE.search(text) is None
        ):
            return Fraction(previous)
        return None
    compound = re.fullmatch(
        rf"({_NUMBER})[元块](?:({_NUMBER})[角毛](?:({_NUMBER})分)?|({_NUMBER})分)", match.group()
    )
    if compound:
        whole = chinese_number(compound.group(1))
        tenths = chinese_number(compound.group(2)) if compound.group(2) else Fraction(0)
        cents = (
            chinese_number(compound.group(3) or compound.group(4))
            if compound.group(3) or compound.group(4)
            else Fraction(0)
        )
        if (
            whole is None
            or tenths is None
            or cents is None
            or not (0 <= tenths < 10 and 0 <= cents < 10)
        ):
            return None
        value = whole + tenths / 10 + cents / 100
    else:
        value = chinese_number(match.group())
    return value if value is not None and value > 0 and (value * 100).denominator == 1 else None


def normalized_money(text: str) -> str:
    """Change only the unique amount span; preserve every recipient and instruction."""
    match = _money_match(text)
    value = money_candidate(text)
    if match is None or value is None:
        return text
    cents = int(value * 100)
    rendered = str(cents // 100) + (f".{cents % 100:02d}" if cents % 100 else "")
    after = text[match.end() :]
    if not re.match(r"\s*(?:元|块)", after):
        rendered += "元"
    return text[: match.start()].removesuffix("¥").removesuffix("￥") + rendered + after


def trusted_slot_context(state, principal):
    """Contact vocabulary and calendar facts come exclusively from trusted state."""
    return {
        "now": state.now,
        "contact_labels": {
            contact.id: [contact.id, contact.name_token, *contact.aliases]
            for contact in state.contacts.values()
            if contact.owner == principal and contact.verified_at is not None
        },
        "birthdays": [
            event.event_date
            for event in state.calendar.values()
            if event.owner == principal and event.event_type == "birthday"
        ],
        "products": [product.product_id for product in state.catalog.products],
    }


def date_candidate(text, context, *, billing=False):
    if not context.get("now"):
        return None
    today = date.fromisoformat(business_date(context["now"]))
    dates = []
    if "明天" in text:
        dates.append((today + timedelta(days=1)).isoformat())
    if "月底" in text:
        dates.append(today.replace(day=calendar.monthrange(today.year, today.month)[1]).isoformat())
    for weekday in re.findall(r"下周([一二三四五六日天])", text):
        offset = "一二三四五六日".index("日" if weekday == "天" else weekday)
        dates.append((today + timedelta(days=7 - today.weekday() + offset)).isoformat())
    if "生日前两天" in text or "生日前2天" in text:
        birthdays = context.get("birthdays", [])
        if len(birthdays) != 1:
            return None
        dates.append((date.fromisoformat(birthdays[0]) - timedelta(days=2)).isoformat())
    for explicit in re.findall(r"\d{4}-\d{2}-\d{2}", text):
        try:
            dates.append(date.fromisoformat(explicit).isoformat())
        except ValueError:
            return None
    if dates:
        return dates[0] if len(dates) == 1 else None
    if "下个月" in text:
        year, month = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
        return f"{year:04d}-{month:02d}"
    if billing:
        year, month = today.year, today.month
        if "上月" in text or "上个月" in text:
            year, month = (year, month - 1) if month > 1 else (year - 1, 12)
        year_match = re.search(r"(\d{4})年", text)
        if year_match:
            year = int(year_match.group(1))
        if "年" in text:
            return f"{year:04d}"
        month_match = re.search(r"(1[0-2]|[1-9])月", text)
        if month_match:
            month = int(month_match.group(1))
        elif "九月" in text:
            month = 9
        return f"{year:04d}-{month:02d}"
    return None


def has_date_request(text):
    """An unparsed date is unresolved scheduling intent, never an immediate transfer."""
    return any(
        word in text
        for word in (
            "下周",
            "明天",
            "明日",
            "后天",
            "月底",
            "月末",
            "月初",
            "下月",
            "下个月",
            "生日前",
            "定时",
            "周末",
            "年底",
            "星期",
            "礼拜",
            "后日",
            "今晚",
            "今早",
            "今夜",
            "今天",
            "昨日",
            "昨天",
            "早上",
            "早晨",
            "上午",
            "中午",
            "下午",
            "傍晚",
            "晚上",
            "半夜",
            "凌晨",
            "今年",
            "明年",
            "后年",
            "春节",
            "元旦",
            "国庆",
            "中秋",
            "端午",
            "节日",
            "假期",
            "明早",
            "明晚",
            "次日",
            "翌日",
            "稍后",
            "待会",
            "一会",
            "以后",
            "之后",
            "下次",
            "下旬",
            "上旬",
            "中旬",
            "月中",
            "下半年",
            "小时",
            "分钟",
            "秒后",
        )
    ) or bool(
        re.search(
            r"\d{4}\s*[-/]\s*\d{1,2}|\d{4}\.\d{1,2}\.\d{1,2}|"
            r"[零〇一二两三四五六七八九十半0-9]+(?:[年月日号点时]|(?:个)?[天周月年](?:之)?[前后])|"
            r"过[零〇一二两三四五六七八九十半0-9]+(?:个)?[天周月年]|"
            r"[零〇一二两三四五六七八九十半几0-9]+(?:个)?(?:小时|分钟|秒|天|周|月)[前后]|"
            r"周[一二三四五六日天]|\d{1,2}[:：]\d{2}",
            text,
        )
    )


def recipient_candidates(text, labels, previous=None):
    """Resolve complete recipient phrases; modifiers and plural referents abstain."""
    if re.search(r"其他|其它|他们|她们|别人|另外|另一个", text):
        return [], False
    text = spoken_transfer(text)
    # C20: only complete single-object forms share the existing object-first path.
    # Anchoring preserves incoming, plural, modified and otherwise unresolved phrases.
    for names in labels.values():
        for name in names:
            match = re.fullmatch(
                rf"(?:(?:转账给|转给|帮我转账给){re.escape(name)}|给我{re.escape(name)}转)"
                rf"(?P<amount>{_NUMBER}元)?",
                text,
            )
            if match:
                text = f"给{name}转{match.group('amount') or ''}"
    matched = set()
    for ref, names in labels.items():
        for name in names:
            for match in re.finditer(re.escape(name), text):
                before, after = text[: match.start()], text[match.end() :]
                # Vault replacement of a formatted phone consumes separators
                # between digits, but may leave its outer parenthesis behind.
                # Ignore only that wrapper around a trusted phone/PII token;
                # the same complete-object boundaries still apply underneath.
                if name.startswith(("PHONE_", "PII_")):
                    before = re.sub(r"[(（]$", "", before)
                    after = re.sub(r"^[)）]", "", after)
                if re.search(r"[A-Za-z0-9_]$", before) or re.match(r"[A-Za-z0-9_]", after):
                    continue
                # Chinese has no whitespace word boundary: require whole object
                # clauses, never suffixes of names or possessive phrases.
                if before and not re.search(r"(?:给|向|往|[\s，,。；;：:])$", before):
                    continue
                if after and not re.match(
                    r"(?:\s|[，,。；;！!？?]|转|汇|打钱|还钱|那边|这边|再转|付款)", after
                ):
                    continue
                matched.add(ref)
    contextual = bool(
        re.search(
            r"给[他她](?=\s*(?:转|汇|打钱|[0-9零〇一二两三四五六七八九十百千万]|同样金额|同一金额)|[，,。；;！!？?]|$)",
            text,
        )
    )
    if not matched and contextual and previous in labels:
        return [previous], True
    return sorted(matched), False


def extract_slots(text, context, *, intent):
    """Canonical projection for unsigned preview and independently frozen-label scoring."""
    contact_text = context.get("contact_text", text)
    contacts, contextual = (
        recipient_candidates(
            contact_text, context.get("contact_labels", {}), context.get("confirmed_contact")
        )
        if intent == "transfer"
        else ([], False)
    )
    amount = (
        money_candidate(text, context.get("confirmed_amount"))
        if intent in {"transfer", "investment", "card_limit", "birthday"}
        else None
    )
    products = [
        ref
        for ref in context.get("products", [])
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(ref)}(?![A-Za-z0-9_])", text)
    ]
    relevant_date = intent in {"transfer", "birthday", "bill_report", "subscription_query"}
    parameters = {
        "recipient": contacts[0] if len(contacts) == 1 else None,
        "amount": fraction_string(amount) if amount is not None else None,
        "date": date_candidate(text, context, billing=intent == "bill_report")
        if relevant_date
        else None,
        "product": products[0] if len(products) == 1 and intent == "investment" else None,
    }
    count = None
    if re.search(r"aa", text, re.I):
        match = re.search(rf"(?:我们|咱们|共)\s*({_NUMBER})\s*(?:个|人)", text)
        count = chinese_number(match.group(1)) if match else None
    share = amount / count if amount and count and count.denominator == 1 and count >= 2 else None
    return {
        "parameters": parameters,
        "recipient_candidates": contacts,
        "recipient_context": contextual,
        "aa": {"participants": int(count), "per_person": fraction_string(share)}
        if share is not None and (share * 100).denominator == 1
        else None,
    }
