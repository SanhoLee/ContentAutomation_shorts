"""
korean_grammar.py — Korean morphology heuristics for subtitle segmentation.

No dependencies and no morphological analyser: just curated tables that answer
"is this 어절 boundary safe to break?".  Distilled from the reference work at
github.com/Leedogin/korean-subtitle-splitter (MIT) and extended with the
dependent nouns this channel's narration actually leans on (수, 적, 것, 게),
which the reference tables do not carry.

Two properties of Korean drive the design:

  1. Endings fuse into the preceding syllable — 눌 + 어 → 눌러 — so a boundary
     cannot be judged from the left word's trailing character.  Judge it from
     the right word instead: is it an auxiliary verb, is it a dependent noun?
  2. The same fusion hits the stem tables — 버리 + ㅂ니다 → 버립니다 — so fused
     forms (버립, 버렸, 버려) sit alongside the bare stems.

Blocks come in two strengths, and callers must respect the difference:

  HARD  starts_with_dependent_noun(), breaks_compound_term()
        Never split here, not even after a clean ending.  These tables are
        matched precisely (exact particle suffixes), so they can afford to win.

  SOFT  is_auxiliary_verb(), is_no_split_after()
        Prefix matching over short stems, so they over-match by construction —
        is_auxiliary_verb("가지런히") is True.  Callers must check
        ends_with_ending() first and skip the soft blocks when it holds: a real
        ending always outranks them.  Used unguarded, they suppress good splits.
"""

import re

# ---------------------------------------------------------------------------
# Endings — ranked by how good a break after them is
# ---------------------------------------------------------------------------

# Best break point: the clause is finished.
SENTENCE_FINAL_SUFFIXES = (
    "습니다", "ㅂ니다", "니다", "입니다",
    "예요", "이에요", "에요", "거예요",
    "나요", "까요", "죠", "네요", "군요", "거든요", "잖아요", "더라고요",
    "세요", "십시오", "ㅂ시다", "읍시다",
)

# Second best: one clause ends, another begins.
CLAUSE_CONNECTIVE_SUFFIXES = (
    # "은데"는 "그런데"(런데)와 겹치지 않는다 — 접속부사는 여기 들어오면 안 된다.
    "는데", "은데", "지만", "니까", "면서", "거나", "든지", "아도", "어도",
    "고도", "라서", "여서", "으면", "면", "려면", "도록", "듯이",
    "는데도", "지마는", "고서", "다가", "자마자",
)

# Third: the verb is nominalised or turned into a modifier.
NOMINALIZING_SUFFIXES = (
    "는지", "ㄴ지", "인지", "할지", "되는지", "했는지",
    "기", "음", "함", "됨",
)

# 해요체 is everywhere in this channel's narration and mostly ends in 요, but a
# bare "요" test would call 중요 / 필요 / 주요 / 수요 sentence-final.  Require the
# syllable before 요 to be one that actually carries an ending.
_YO_PRECEDERS = set("어아에예거네군잖죠까나세게해워봐와려러셔으이지대래오우")

# Safe by punctuation alone.
BOUNDARY_PUNCTUATION = re.compile(r"[,，;:…]$|\.\.\.$")

_TRIM = "'\"“”‘’.,!?()[]… \t\n"


# ---------------------------------------------------------------------------
# Words that must not be stranded
# ---------------------------------------------------------------------------

# These head the *following* clause, so a line must not end on them.  Note this
# is the opposite of an ending: 그리고 belongs to what comes next.
CONNECTIVE_ADVERBS = (
    "그런데", "그리고", "하지만", "그래서", "다만", "또", "또한",
    "따라서", "그러나", "즉", "물론", "오히려", "심지어",
    "솔직히", "사실", "결국", "특히", "게다가", "그러니까",
)

# Modifiers: breaking after one leaves it pointing at nothing.
ADVERB_LIKE_WORDS = (
    "이제", "여기서", "먼저", "다시", "바로", "정말", "아주",
    "계속", "가장", "훨씬", "조금", "거의", "이미", "아직",
    "함께", "서로", "직접", "반드시", "무조건", "특히", "딱",
    "혹시", "이런", "저런", "그런", "어떤", "무슨",
)

# Determiners and negation — one syllable, meaningless on their own.
DETERMINER_WORDS = ("이", "그", "저", "안", "못", "한", "두", "세", "네", "몇")

NO_SPLIT_AFTER = CONNECTIVE_ADVERBS + ADVERB_LIKE_WORDS + DETERMINER_WORDS

# Auxiliary verb stems, fused forms included.  Judged on the *right* word, and
# soft — see the module docstring.
AUXILIARY_VERB_STEMS = (
    "버리", "버립", "버려", "버렸", "버릴",
    "드리", "드립", "드려", "드렸", "드릴",
    "주시", "주십", "주세", "주었", "줬", "줍니", "줄게", "줄까",
    "두세", "둡니", "뒀", "놓으", "놉니", "놨",
    "봅니", "봤", "보세", "볼까", "보이",
    "냅니", "냈", "내세",
    "않", "않습", "않아", "않은", "않는", "않았", "않겠", "않을",
    "못하", "못합", "못했",
    "싶", "싶습", "싶어",
    "있습", "있어", "있는", "있을", "있으",
    "집니", "졌", "지는", "지고", "진다",
    "말고", "맙니", "마세",
    "됩니", "된다", "되는", "가는", "갑니",
)


# ---------------------------------------------------------------------------
# Dependent nouns
# ---------------------------------------------------------------------------

# Cannot stand alone, so they must never start a line.  Matched against the
# right word: the noun itself, or the noun plus an exact particle.
#
# 수 / 적 / 것 / 게 are the four that matter most here — "할 수 있어요",
# "이런 적 있으셨나요", "바라보는 것만으로도", "더하는 게 중요합니다" are this
# channel's most common phrasings, and every one of them used to be splittable.
DEPENDENT_NOUN_HEADS = (
    "수", "적", "것", "게", "줄", "리", "중", "때", "대로", "만큼",
    "뿐", "채", "등", "및", "쪽", "편", "가지", "무렵", "번째",
)

# Prefix-matched instead, because no free noun begins with these.
ALWAYS_DEPENDENT_HEADS = ("때문", "덕분", "탓", "따름", "텐데", "테니", "터라", "셈", "듯")

# Exact suffixes allowed after a dependent noun.  Exact matching is what keeps
# 중요 / 수술 / 게임 / 줄이고 out — a char-class test would let them through.
_PARTICLES = frozenset((
    "", "은", "는", "이", "가", "을", "를", "에", "의", "도", "만", "과", "와",
    "로", "으로", "에서", "에도", "에는", "에만", "에게", "이나", "나",
    "만으로", "만으로도", "만은", "만이", "만도", "부터", "까지", "처럼",
    "보다", "라도", "이라도", "밖에", "조차", "마저", "과는", "와는",
    "과도", "와도", "이란", "란", "이라는", "라는", "이야",
))

# 계사가 붙어 활용한 꼴 — "성장 중이기 때문에", "그런 것이었어요", "이런 적이라서".
# 활용형을 전부 나열할 수 없으므로 "이…"로 시작하면 계사로 본다. 다만 아래
# 세 글자는 동사·일반명사와 충돌이 잦아("줄이고", "게이트") 제외한다.
_NO_COPULA_HEADS = frozenset(("줄", "게", "리"))

# Written with spaces but read as one idea.  Domain vocabulary — extend freely.
COMPOUND_TERMS = (
    "인지 기능", "인지 저하", "경도 인지 장애", "혈관성 치매", "알츠하이머 병",
    "수면 무호흡", "깊은 잠", "렘 수면", "생체 리듬",
    "혈당 지수", "체질량 지수", "대사 증후군", "관상동맥 질환",
    "신경 세포", "신경 회로", "뇌 혈류", "해마 부피", "회백질 두께",
    "만성 염증", "산화 스트레스", "장 건강",
    "유산소 운동", "근력 운동", "지중해 식단",
)

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

def strip_edges(word: str) -> str:
    """Drop quotes and punctuation so tables match the bare 어절."""
    return word.strip(_TRIM)


def _ends_with_any(word: str, suffixes: tuple[str, ...]) -> bool:
    return any(word.endswith(suffix) for suffix in suffixes)


def is_sentence_final(word: str) -> bool:
    """'좋습니다' / '좋아요' → True, '중요' → False."""
    bare = strip_edges(word)
    if not bare:
        return False
    if _ends_with_any(bare, SENTENCE_FINAL_SUFFIXES):
        return True
    return len(bare) >= 2 and bare.endswith("요") and bare[-2] in _YO_PRECEDERS


def is_clause_connective(word: str) -> bool:
    return _ends_with_any(strip_edges(word), CLAUSE_CONNECTIVE_SUFFIXES)


def is_nominalizing(word: str) -> bool:
    return _ends_with_any(strip_edges(word), NOMINALIZING_SUFFIXES)


def ends_with_ending(word: str) -> bool:
    """Any real ending — the gate that lets a boundary outrank the soft blocks."""
    return is_sentence_final(word) or is_clause_connective(word) or is_nominalizing(word)


def is_auxiliary_verb(word: str) -> bool:
    """SOFT. '버립니다' → True, and so does '가지런히'. Gate on ends_with_ending()."""
    bare = strip_edges(word)
    return any(bare.startswith(stem) for stem in AUXILIARY_VERB_STEMS)


def is_no_split_after(word: str) -> bool:
    """SOFT. Connective adverbs, plain adverbs, determiners."""
    return strip_edges(word) in NO_SPLIT_AFTER


def starts_with_dependent_noun(word: str) -> bool:
    """HARD. '수 있어요' / '것만으로도' → True, '수술' / '중요' → False."""
    bare = strip_edges(word)
    if not bare:
        return False
    if any(bare.startswith(head) for head in ALWAYS_DEPENDENT_HEADS):
        return True
    for noun in DEPENDENT_NOUN_HEADS:
        if not bare.startswith(noun):
            continue
        rest = bare[len(noun):]
        if rest in _PARTICLES:
            return True
        if noun not in _NO_COPULA_HEADS and len(rest) >= 2 and rest.startswith("이"):
            return True
    return False


def breaks_compound_term(words: list[str], index: int) -> bool:
    """HARD. Does cutting before words[index] land inside a compound term?"""
    joined = " ".join(words)
    cut = len(" ".join(words[:index]))
    for term in COMPOUND_TERMS:
        start = joined.find(term)
        while start != -1:
            if start < cut < start + len(term):
                return True
            start = joined.find(term, start + 1)
    return False


def split_priority(prev_word: str) -> int:
    """How good a break after prev_word is: 3 best, 0 no signal."""
    if BOUNDARY_PUNCTUATION.search(prev_word):
        return 3
    if is_sentence_final(prev_word):
        return 3
    if is_clause_connective(prev_word):
        return 2
    if is_nominalizing(prev_word):
        return 1
    return 0


def verify_numbers_preserved(source: str, subtitles) -> bool:
    """Every number in the source survives into the captions, in order."""
    if isinstance(subtitles, str):
        subtitles = [subtitles]
    return _NUMBER.findall(source) == _NUMBER.findall("".join(subtitles))
