"""S6 extension (R7): the phrase gate for money-moving requests.

A money-moving request whose words hit both closed lists, an impersonated public authority
(``AUTHORITY``) and a freeze / safe-account / transfer-now demand (``PRESSURE`` and the
transfer-now rule), is held at clarification before any planner call: no draft, no card,
no dispatch, and the reply is a fixed anti-fraud message. It is the negation gate's
mechanism (the lexical router holds the request; nothing is prepared or signed). It works
only in this interface's chat and may hold a legitimate payment for one more step; it
does not change the frozen kernel's rules or signed cards, and it does not cover paths
that bypass the chat. C2: it is decided before every branch that can move money (birthday
plans, purchases), and a sentence it holds does not trigger the S1 interrupt.

It does not decide whether anyone is a fraudster. Fixed lists miss paraphrases, typos,
other names and wording split over two turns, and they also hold some legitimate wording
(a police station named next to "pay right away"). Added after F9, it is not part of the
F9 measurement, and there is no evidence yet that it identifies fraud or reduces losses.

Only fixed labels are reported (``AUTHORITY_LABELS`` / ``PRESSURE_LABELS``), never a
fragment of the user's words.
"""

import re
import unicodedata

# Public authorities whose names scammers borrow; each entry reports itself.
AUTHORITY = (
    "反诈中心",
    "反诈专员",
    "反诈民警",
    "公安局",
    "公安",
    "派出所",
    "刑警",
    "网警",
    "民警",
    "警察",
    "警官",
    "检察院",
    "检察官",
    "法院",
    "法官",
    "金融监管",
    "银保监",
    "人民银行",
    "央行",
    # C2: the common impersonation names the first lists missed.
    "公检法",
    "最高检",
    "最高法",
    "国安",
    "国家安全",
    "银监会",
    "证监会",
    "银行客服",
    "anti-fraud centre",
    "anti-fraud center",
    "police",
    "prosecutor",
    "court",
)
AUTHORITY_LABELS = AUTHORITY
# A freeze, a "safe" or checking account, or a demand to transfer now.
PRESSURE = (
    ("冻结", "冻结"),
    ("安全账户", "安全账户"),
    ("安全账号", "安全账户"),
    ("验证专户", "验证专户"),
    ("资金核查", "资金核查"),
    ("清查账户", "资金核查"),
    ("frozen", "freeze"),
    ("freeze", "freeze"),
    ("safe account", "safe account"),
    ("verification account", "safe account"),
)
NOW_LABEL = "立即转账"
NOW_LABEL_EN = "transfer now"
PRESSURE_LABELS = tuple(dict.fromkeys(label for _, label in PRESSURE)) + (NOW_LABEL, NOW_LABEL_EN)
# "Transfer now": a hurry word and then, within the same clause, a transfer verb.
_NOW = re.compile(
    r"(?:立即|马上|立刻|赶紧|赶快|尽快|限时|现在就)[^,.;:!?。、~…]{0,12}?(?:转|汇|打钱|付款|付钱)"
)
_NOW_EN = re.compile(
    r"(?:immediately|right now|right away|at once)\b|\btransfer\s+(?:it\s+)?(?:now|today)\b"
)
# Words that move money; the gate applies only to such a request. C2: buying a product
# moves money too (申购, 买入安全账户理财), and 打款 / 汇钱 name a transfer.
_MONEY = (
    "转",
    "汇款",
    "汇钱",
    "打钱",
    "打款",
    "还钱",
    "付款",
    "付钱",
    "申购",
    "认购",
    "买入",
    "购买",
    "买",
    "transfer",
    "send money",
    "pay ",
)
# C2: English entries are whole words (court, not courtesy or Courtney; freeze, not freezer).
_ENGLISH_WORD = re.compile(r"[a-z]")
# Traditional forms of the characters the lists use (反詐中心讓我立即轉帳 reads as simplified).
_SIMPLIFIED = str.maketrans(
    "詐訊讓轉帳賬戶凍結驗證專資這銀監檢審緊盡關說",
    "诈讯让转账账户冻结验证专资这银监检审紧尽关说",
)


def _fold(text: str) -> str:
    """NFKC, casefold, traditional to simplified, no spaces or invisible format marks."""
    folded = unicodedata.normalize("NFKC", text).casefold().translate(_SIMPLIFIED)
    return "".join(
        char
        for char in folded
        if not char.isspace() and unicodedata.category(char) not in {"Cf", "Mn", "Me"}
    )


def _spaced(text: str) -> str:
    """The same folding with single spaces kept, for the English phrases."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(folded.split())


def money_words(text: str) -> bool:
    folded, spaced = _fold(text), _spaced(text) + " "
    return any(word in folded or word in spaced for word in _MONEY)


def _english(word: str, spaced: str) -> bool:
    """An English entry as a whole word or phrase of the spaced text (C2)."""
    return bool(re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", spaced))


def _hit(word: str, folded: str, spaced: str) -> bool:
    if _ENGLISH_WORD.match(word):
        return _english(word, spaced)
    return word.replace(" ", "") in folded


def scam_phrase(text: str) -> dict[str, list[str]] | None:
    """The fixed labels both lists matched, or None unless both did (and money moves)."""
    if not isinstance(text, str) or not text.strip() or not money_words(text):
        return None
    folded, spaced = _fold(text), _spaced(text)
    authority = [
        word
        for word in AUTHORITY
        if _hit(word, folded, spaced)
        and not any(
            other != word and word in other and _hit(other, folded, spaced) for other in AUTHORITY
        )
    ]
    pressure: list[str] = []
    for word, label in PRESSURE:
        if _hit(word, folded, spaced) and label not in pressure:
            pressure.append(label)
    if _NOW.search(folded) and NOW_LABEL not in pressure:
        pressure.append(NOW_LABEL)
    if _NOW_EN.search(spaced) and NOW_LABEL_EN not in pressure:
        pressure.append(NOW_LABEL_EN)
    if not authority or not pressure:
        return None
    return {"authority": authority, "pressure": pressure}
