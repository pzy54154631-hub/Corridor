"""S5: one closed negation/cancel gate for money-moving chat requests.

A request that would otherwise become a transfer, scheduled-transfer, split-bill or
birthday-plan card is held at clarification when a negation, cancel or stop word appears
anywhere in it: before or after the date phrase, in its own clause or at the very end
(别每月5号给房东转, 每月5号，停止给房东转, 给房东转2000，算了). The gate does not try to
resolve what the word negates, so a negation about something else (房东说不用了，给他转500)
also clarifies. It only ever removes a card, never creates one, and by itself it changes
nothing (no draft, no card, no dispatch): interrupting a plan or withdrawing a pending card
stays with the existing stop handling.

Only the closed compounds in _NEUTRAL are not negations: a reminder (别忘了 X asks for X),
politeness, the conjunction 不然 ("otherwise"), parking (停车), arithmetic (算了一下),
praise (不错), a clause opened by "but" (，不过…) and words in which 别 means "other" or
"apart" (分别, 特别, 别人).
"""

import re
import unicodedata

# Longest first, so the reported word is the whole negation (不要, not 不).
_LEXICON = (
    "没有必要",
    "当我没说",
    "没必要",
    "不需要",
    "不要",
    "不想",
    "不用",
    "不必",
    "先不",
    "先别",
    "无需",
    "无须",
    "免了",
    "免掉",
    "免啦",
    "免谈",
    "算了",
    "算啦",
    "算咯",
    "算喽",
    "暂停",
    "停止",
    "停掉",
    "叫停",
    "取消",
    "撤销",
    "撤回",
    "作废",
    "终止",
    "中止",
    "打住",
    "拒绝",
    # Dropping, ending, forbidding or taking back a transfer or a plan cancels it.
    "放弃",
    "退订",
    "禁止",
    "暂缓",
    "解除",
    "拉倒",
    "罢了",
    "作罢",
    "反悔",
    "收回",
    "休想",
    "没门",
    "没让",
    "没叫",
    # Removing or switching off a standing transfer cancels it.
    "删除",
    "删掉",
    "去掉",
    "撤掉",
    "关掉",
    "关闭",
    "关了",
    "关停",
    "不",
    "别",
    "甭",
    "勿",
    "毋",
    "停",
    # 撤 and 删 in any form (撤消, 撤了, 删了) withdraw or delete something.
    "撤",
    "删",
    chr(0x274C),
    chr(0x1F6AB),
    chr(0x26D4),
)
_WORDS = re.compile(
    "|".join(_LEXICON)
    # 结束 ends a transfer; 结束后 / 结束时 only dates one (会议结束后给房东转).
    + r"|结束(?!后|以后|之后|时|的时候|日期|时间)"
    # Forget it, or put it off (给房东转2000，忘了吧 / 以后再说 / 改天吧 / 先缓一缓). The
    # conversation page parses privacy-tokenized text, where 一 already reads as 1.
    + r"|忘[了掉]吧|缓[一1]?缓|缓[一1]下"
    + r"|(?:以后|回头|改天|等等|等会儿?|下次|下回|之后|晚点|过几天|过两天|有空)再说|再说吧"
    + r"|(?:以后|回头|改天|下次|下回)吧"
)
# English words count wherever they stand, also right next to Chinese text or digits
# (cancel给房东转, stop5号), so the edges are letters only, not regex word boundaries.
_ENGLISH = re.compile(
    r"(?<![a-z])(?:don['’]?t|do\s+not|doesn['’]?t|didn['’]?t|[a-z]+n['’]t|"
    r"never(?:\s*mind)?|not|nope|nah|no\s+need|no(?!\.\s*\d|\d)|"
    r"cancel(?:s|led|ed|ling|ing|lation)?|stop(?:s|ped|ping)?|pause[sd]?|hold\s+off|"
    r"forget\s+it|can['’]?t|cannot|won['’]?t|abort(?:s|ed)?|halt(?:s|ed)?|skip(?:s|ped)?|"
    r"undo|revoke[sd]?|scrap(?:s|ped)?|call(?:ed)?\s+off|delete[sd]?|remove[sd]?|"
    r"turn(?:ed)?\s+off|switch(?:ed)?\s+off)(?![a-z])",
    re.I,
)
# Closed compounds whose negation character does not negate the request.
_NEUTRAL = re.compile(
    r"(?:千万|可)?(?:别|不要|不能|不可以|不许|莫)忘(?:记|掉)?了?"
    r"|不好意思|对不起|不然|停车|算了(?=[一1]下|算)|不错|差不多"
    # "But" opens a clause; 不过 at the end or before 了 means "not celebrate" (生日不过了).
    r"|(?:^|(?<=[,.;:!?、。~～…]))不过(?![了啦]|[。.!?…~～]*$)"
    r"|特别|分别|区别|别人(?!民币)|个别|类别|级别|识别|性别|告别|差别"
    r"|(?:don['’]?t|do\s+not|never)\s+forget|no\s+(?:problem|worries)",
    re.I,
)
# S5 R8: five polite tags are not negations when they stand
# as their own clause after a comma, or end the sentence: 不用急 / 不着急 ("no rush") and
# 好不好 / 行不行 / 可以吗 ("OK?"), with their trailing particles and end punctuation. Nowhere
# else: a tag that opens the sentence, is followed by more of its own clause (不用急着转) or
# sits right after 先 / 暂 / 还 (先不用急, "not yet") still counts, and every other negation
# word in the sentence still holds it. The same tags no longer make a transfer request
# interrogative (domestic_intent): "给妈妈转500，可以吗" asks for the transfer politely.
#
# C1 (review of R8): attached to the end of the sentence (no separator), a tag is released
# only right after a part of the request itself, a digit, a numeral or a currency unit
# (给妈妈转500元不用急); any other word before it (暂时、现在、目前、的事、这事 …) keeps
# it a negation. ``negation_words`` decides on the folded text with every space and
# invisible character removed first, so 先 不着急 or 先<zero-width>不着急 is 先不着急.
_TAGS = r"(?:不用急|不着急|好不好|行不行|可以吗)"
_TAG_TAIL = r"[吧呀啊呢哈哦噢嘛啦的]*"
_TAG_SEP = r"[,，、;；]"
_TAG_END = r"[。.!！?？~～…\s]*$"
_REQUEST_PART = r"[0-9０-９零〇一二两三四五六七八九十百千万元块钱圆]"
_POLITE = re.compile(
    # A clause of its own between two separators: only the tag goes.
    rf"(?<={_TAG_SEP})\s*{_TAGS}{_TAG_TAIL}\s*(?={_TAG_SEP})"
    # The sentence's last clause, after a separator: the separator goes with it.
    rf"|{_TAG_SEP}\s*{_TAGS}{_TAG_TAIL}{_TAG_END}"
    # Attached to the end of the sentence, right after the amount of the request only.
    rf"|(?<={_REQUEST_PART})\s*{_TAGS}{_TAG_TAIL}{_TAG_END}"
)


def without_polite_tags(text: str) -> str:
    """The text with each polite tag in an allowed position removed (see ``_POLITE``)."""
    return _POLITE.sub("", text)


# Invisible characters cannot split a word (取 消, or 取消 split by a zero-width or bidi
# mark): every default-ignorable code point, the interlinear annotation marks and the
# Hangul fillers are dropped before matching, and so is every format, combining,
# private-use or unassigned character (_DROPPED).
_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFFB),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_IGNORABLE = re.compile(
    "[" + "".join(f"{chr(low)}-{chr(high)}" for low, high in _IGNORABLE_RANGES) + "]+"
)
_DROPPED = frozenset({"Cf", "Mn", "Me", "Co", "Cn"})
_SPACE = re.compile(r"\s+")
# Traditional forms of the lexicon's characters (別給房东转 reads as 别给房东转).
_SIMPLIFIED = str.maketrans(
    "別銷刪關閉終無須沒廢絕暫結棄訂罷緩讓說門", "别销删关闭终无须没废绝暂结弃订罢缓让说门"
)
# Words that cancel a birthday plan, so a refund request may first interrupt it (S1).
PLAN_CANCEL = re.compile(r"取消|不办了|不过了|不用了|不要了|作废|算了|停止|停了|不需要了")
# A refund request may also say stop in any of the natural-stop forms (停一下，把预算退回来),
# end the plan in other words (放弃计划, 计划撤了) or name the release itself (撤销锁定,
# 把预算收回来).
_PLAN_STOP = re.compile(r"暂停|叫停|中止|终止|停(?:一下|一停|下来|下|住|掉)?")
_PLAN_END = re.compile(
    r"撤销锁定|解除锁定|收回|放弃|结束|作罢|罢了|拉倒|反悔|撤[了掉消除下]?|删[了掉除]?"
    r"|关[了掉停闭]|算[啦咯喽]"
)
# A cancel or stop word right before the return verb negates the refund itself (取消退回,
# 取消补偿).
_RETURN_CANCELLED = re.compile(
    r"(?:取消|停止|停掉|暂停|叫停|撤销|撤回|作废|终止|中止|放弃)(?:把|将)?(?:锁定|锁住)?的?"
    r"(?:预算|钱|资金)?的?"
    r"(?:退回|退还|返还|还回|放回|退到|转回|拿回|取回|释放|解锁|解除锁定|补偿)"
)


def _fold(text: str) -> str:
    # NFKD, so a combining mark that composes with a letter (n + U+0301) is dropped too.
    folded = _IGNORABLE.sub("", unicodedata.normalize("NFKD", text).casefold())
    kept = "".join(char for char in folded if unicodedata.category(char) not in _DROPPED)
    return kept.translate(_SIMPLIFIED)


def compact(text: str) -> str:
    """The folded text with every space and invisible character removed (C1)."""
    return _SPACE.sub("", _fold(text))


def negation_words(text: str) -> tuple[str, ...]:
    """Every negation, cancel or stop word in the text, outside the closed neutral list and
    the polite tags in their allowed places (R8), decided on the compact folded text."""
    folded = compact(text)
    released = _NEUTRAL.sub(" ", without_polite_tags(folded))
    spaced = _NEUTRAL.sub(" ", _fold(text))
    words = [match.group() for match in _WORDS.finditer(released)]
    words.extend(match.group() for match in _ENGLISH.finditer(spaced))
    return tuple(words)


def compensation_negation(text: str) -> tuple[str, ...]:
    """A refund keeps its plan-cancel words (取消计划，把预算退回来, 预算不用了，退回来);
    a negation left after removing them (不要退回, 别退, 不用把钱退回) holds the refund card,
    and so does a cancel word aimed at the return itself (取消退回, 停止退回预算)."""
    folded = _SPACE.sub("", _fold(text))
    cancelled = tuple(match.group() for match in _RETURN_CANCELLED.finditer(folded))
    rest = _PLAN_END.sub(" ", _PLAN_STOP.sub(" ", PLAN_CANCEL.sub(" ", folded)))
    return cancelled + negation_words(rest)
