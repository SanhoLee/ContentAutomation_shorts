#!/usr/bin/env python3
"""
5_feedback.py — Brain50 콘텐츠 피드백 & 인사이트 시스템

사용법:
  python 5_feedback.py rate                            현재 영상 평가 입력
  python 5_feedback.py update [video_key]              YouTube 지표 추가/수정
  python 5_feedback.py tag <key> <keyword> <+1|0|-1>   키워드 태깅
  python 5_feedback.py list [--limit N]                평가 목록
  python 5_feedback.py stats                           집계 통계
  python 5_feedback.py insights                        0_script.py 주입용 인사이트 생성
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime

# ── 경로 설정
WORK_DIR     = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
_DATA_DIR    = os.path.normpath(os.path.join(WORK_DIR, ".."))   # ~/brain50/data
DB_PATH      = os.environ.get("FEEDBACK_DB",       os.path.join(_DATA_DIR, "feedback.db"))
INSIGHTS_PATH = os.environ.get("FEEDBACK_INSIGHTS", os.path.join(_DATA_DIR, "feedback_insights.json"))
META_PATH    = os.path.join(WORK_DIR, "video_meta.json")


# ─────────────────────────────────────────────
# DB 초기화
# ─────────────────────────────────────────────

def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn


def _init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS videos (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        video_key     TEXT    UNIQUE NOT NULL,
        topic         TEXT,
        title         TEXT,
        hook_type     TEXT,
        hashtags      TEXT,
        summary       TEXT,
        posted_date   TEXT,
        rating        INTEGER,           -- 내 주관 평점 1-5
        notes         TEXT,
        yt_views      INTEGER,           -- YouTube 조회수
        yt_watch_pct  REAL,              -- 평균 시청률 % (0-100)
        yt_likes      INTEGER,
        yt_comments   INTEGER,
        yt_shares     INTEGER,
        created_at    TEXT DEFAULT (datetime('now','localtime')),
        updated_at    TEXT DEFAULT (datetime('now','localtime'))
    );

    CREATE TABLE IF NOT EXISTS keywords (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        video_key   TEXT    NOT NULL,
        keyword     TEXT    NOT NULL,
        ktype       TEXT    DEFAULT 'general',
        -- topic_word / hook_phrase / scene_expr / hashtag / visual
        sentiment   INTEGER DEFAULT 0,
        -- +1 좋음 / 0 중립 / -1 나쁨
        notes       TEXT,
        created_at  TEXT DEFAULT (datetime('now','localtime'))
    );
    """)
    conn.commit()


# ─────────────────────────────────────────────
# 입력 헬퍼
# ─────────────────────────────────────────────

def _input_str(msg, allow_empty=True, default=None):
    hint = f" [{default}]" if default is not None else ""
    while True:
        val = input(f"{msg}{hint}: ").strip()
        if not val and default is not None:
            return default
        if val or allow_empty:
            return val
        print("  값을 입력해주세요.")


def _input_int(msg, min_v=None, max_v=None, allow_empty=False):
    range_hint = f" ({min_v}-{max_v})" if min_v is not None and max_v is not None else ""
    while True:
        raw = input(f"{msg}{range_hint}: ").strip()
        if not raw:
            if allow_empty:
                return None
            print("  값을 입력해주세요.")
            continue
        try:
            val = int(raw)
        except ValueError:
            print("  숫자를 입력해주세요.")
            continue
        if min_v is not None and val < min_v:
            print(f"  {min_v} 이상이어야 합니다.")
            continue
        if max_v is not None and val > max_v:
            print(f"  {max_v} 이하이어야 합니다.")
            continue
        return val


def _input_float(msg, allow_empty=True, current=None):
    hint = f" [현재:{current}]" if current is not None else ""
    raw = input(f"{msg}{hint} (엔터={'유지' if current is not None else '건너뜀'}): ").strip()
    if not raw:
        return current
    try:
        return float(raw)
    except ValueError:
        print("  숫자를 입력해주세요. (예: 65.3)")
        return current


def _input_int_upd(msg, current=None, allow_empty=True):
    hint = f" [현재:{current}]" if current is not None else ""
    raw = input(f"{msg}{hint} (엔터={'유지' if current is not None else '건너뜀'}): ").strip()
    if not raw:
        return current
    try:
        return int(raw)
    except ValueError:
        return current


def _input_date(msg, default_today=True):
    default = date.today().isoformat() if default_today else None
    hint = "오늘" if default_today else "건너뜀"
    while True:
        raw = input(f"{msg} [YYYY-MM-DD, 엔터={hint}]: ").strip()
        if not raw:
            return default
        try:
            datetime.strptime(raw, "%Y-%m-%d")
            return raw
        except ValueError:
            print("  형식: YYYY-MM-DD (예: 2026-06-25)")


def _make_video_key(topic, posted_date=None):
    date_str = posted_date or date.today().isoformat()
    safe = "".join(c for c in topic if c.isalnum() or c in " _-")[:20].strip().replace(" ", "_")
    return f"{date_str}_{safe}"


# ─────────────────────────────────────────────
# rate — 현재 영상 평가 입력
# ─────────────────────────────────────────────

def cmd_rate(args):
    if not os.path.exists(META_PATH):
        print(f"오류: {META_PATH} 없음. 먼저 0_script.py를 실행하세요.")
        sys.exit(1)

    with open(META_PATH, "r", encoding="utf-8") as f:
        meta = json.load(f)

    topic     = meta.get("topic", "")
    title     = meta.get("title", "")
    hook_type = meta.get("hook_type", "")
    hashtags  = meta.get("hashtags", "")
    summary   = meta.get("summary", "")

    print("\n" + "=" * 52)
    print("  Brain50 영상 피드백 입력")
    print("=" * 52)
    print(f"  주제    : {topic}")
    print(f"  제목    : {title}")
    print(f"  훅 유형 : {hook_type}")
    print(f"  해시태그: {hashtags}")
    print("=" * 52)

    posted_date = _input_date("YouTube 게시일", default_today=True)
    video_key   = _make_video_key(topic, posted_date)

    conn = get_conn()
    c    = conn.cursor()

    existing = c.execute("SELECT id FROM videos WHERE video_key=?", (video_key,)).fetchone()
    if existing:
        ow = input(f"\n이미 '{video_key}' 평가가 있습니다. 덮어쓸까요? (y/N): ").strip().lower()
        if ow != "y":
            print("취소.")
            conn.close()
            return

    print("\n─── 내 평가 ───")
    rating = _input_int("전반적 평점", 1, 5)
    notes  = _input_str("메모 (선택)", allow_empty=True)

    print("\n─── YouTube 지표 (선택 — 나중에 update로 추가 가능) ───")
    yt_views     = _input_int("조회수",     allow_empty=True)
    yt_watch_pct = _input_float("평균 시청률 %  (예: 65.2)")
    yt_likes     = _input_int("좋아요",    allow_empty=True)
    yt_comments  = _input_int("댓글",      allow_empty=True)
    yt_shares    = _input_int("공유",      allow_empty=True)

    if existing:
        c.execute("""
            UPDATE videos SET
                title=?, hook_type=?, hashtags=?, summary=?,
                posted_date=?, rating=?, notes=?,
                yt_views=?, yt_watch_pct=?, yt_likes=?, yt_comments=?, yt_shares=?,
                updated_at=datetime('now','localtime')
            WHERE video_key=?
        """, (title, hook_type, hashtags, summary,
              posted_date, rating, notes,
              yt_views, yt_watch_pct, yt_likes, yt_comments, yt_shares,
              video_key))
    else:
        c.execute("""
            INSERT INTO videos
                (video_key, topic, title, hook_type, hashtags, summary,
                 posted_date, rating, notes,
                 yt_views, yt_watch_pct, yt_likes, yt_comments, yt_shares)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (video_key, topic, title, hook_type, hashtags, summary,
              posted_date, rating, notes,
              yt_views, yt_watch_pct, yt_likes, yt_comments, yt_shares))
    conn.commit()

    print("\n─── 키워드 태깅 (선택사항) ───")
    _interactive_tagging(conn, video_key)

    conn.close()
    print(f"\n✅ 피드백 저장 완료! (video_key: {video_key})")
    print(f"   DB: {DB_PATH}")
    print("   인사이트 갱신: python 5_feedback.py insights")


# ─────────────────────────────────────────────
# update — YouTube 지표 나중에 업데이트
# ─────────────────────────────────────────────

def cmd_update(args):
    conn = get_conn()
    c    = conn.cursor()

    video_key = getattr(args, "video_key", None)
    if not video_key:
        rows = c.execute("""
            SELECT video_key, topic, posted_date, rating
            FROM videos ORDER BY created_at DESC LIMIT 10
        """).fetchall()
        if not rows:
            print("저장된 영상이 없습니다.")
            conn.close()
            return
        print("\n최근 영상 목록:")
        for i, r in enumerate(rows, 1):
            print(f"  {i}. {r['video_key']}  [{r['rating'] or '-'}/5]  {r['topic']}")
        idx = _input_int("업데이트할 번호", 1, len(rows))
        video_key = rows[idx - 1]["video_key"]

    row = c.execute("SELECT * FROM videos WHERE video_key=?", (video_key,)).fetchone()
    if not row:
        print(f"오류: '{video_key}' 없음.")
        conn.close()
        return

    print(f"\n업데이트: {video_key}  ({row['topic']})")
    yt_views     = _input_int_upd("조회수",      current=row["yt_views"])
    yt_watch_pct = _input_float("평균 시청률 %", current=row["yt_watch_pct"])
    yt_likes     = _input_int_upd("좋아요",      current=row["yt_likes"])
    yt_comments  = _input_int_upd("댓글",        current=row["yt_comments"])
    yt_shares    = _input_int_upd("공유",        current=row["yt_shares"])
    rating_raw   = input(f"평점 [현재:{row['rating']}] (엔터=유지): ").strip()
    rating       = int(rating_raw) if rating_raw.isdigit() else row["rating"]
    notes_raw    = input(f"메모 [현재:{row['notes']}] (엔터=유지): ").strip()
    notes        = notes_raw or row["notes"]

    c.execute("""
        UPDATE videos SET
            yt_views=?, yt_watch_pct=?, yt_likes=?, yt_comments=?, yt_shares=?,
            rating=?, notes=?, updated_at=datetime('now','localtime')
        WHERE video_key=?
    """, (yt_views, yt_watch_pct, yt_likes, yt_comments, yt_shares, rating, notes, video_key))
    conn.commit()

    print("\n─── 추가 태깅 (선택사항) ───")
    _interactive_tagging(conn, video_key)

    conn.close()
    print(f"\n✅ 업데이트 완료 ({video_key})")


# ─────────────────────────────────────────────
# 키워드 태깅
# ─────────────────────────────────────────────

_KTYPE_MAP = {
    "1": "topic_word",
    "2": "hook_phrase",
    "3": "scene_expr",
    "4": "hashtag",
    "5": "visual",
}
_KTYPE_LABELS = "1=주제어 2=훅표현 3=장면표현 4=해시태그 5=비주얼"

def _interactive_tagging(conn, video_key):
    print(f"단어/표현을 태깅하세요 (엔터=종료) — 유형: {_KTYPE_LABELS}")
    while True:
        keyword = input("  키워드 (엔터=종료): ").strip()
        if not keyword:
            break
        raw_s = input("  평가 [+1=좋음 / 0=중립 / -1=나쁨, 기본+1]: ").strip()
        sentiment = int(raw_s) if raw_s in ("+1", "1", "0", "-1") else 1
        raw_k = input(f"  유형 [{_KTYPE_LABELS}, 기본1]: ").strip()
        ktype = _KTYPE_MAP.get(raw_k, "topic_word")
        notes = input("  메모 (선택): ").strip()
        conn.execute(
            "INSERT INTO keywords (video_key, keyword, ktype, sentiment, notes) VALUES (?,?,?,?,?)",
            (video_key, keyword, ktype, sentiment, notes),
        )
        conn.commit()
        icon = "✅" if sentiment == 1 else ("⚠️" if sentiment == 0 else "❌")
        print(f"  {icon} '{keyword}' [{ktype}] 저장")


def cmd_tag(args):
    conn = get_conn()
    if not conn.execute("SELECT id FROM videos WHERE video_key=?", (args.video_key,)).fetchone():
        print(f"오류: '{args.video_key}' 없음.")
        conn.close()
        sys.exit(1)
    sentiment_map = {"+1": 1, "1": 1, "0": 0, "-1": -1}
    sentiment = sentiment_map.get(args.sentiment, 1)
    conn.execute(
        "INSERT INTO keywords (video_key, keyword, ktype, sentiment, notes) VALUES (?,?,?,?,?)",
        (args.video_key, args.keyword, args.ktype or "topic_word", sentiment, args.notes or ""),
    )
    conn.commit()
    conn.close()
    icon = "✅" if sentiment == 1 else ("⚠️" if sentiment == 0 else "❌")
    print(f"{icon} 태깅: '{args.keyword}' [{args.ktype}] → {args.video_key}")


# ─────────────────────────────────────────────
# list — 목록
# ─────────────────────────────────────────────

def cmd_list(args):
    conn = get_conn()
    rows = conn.execute("""
        SELECT video_key, topic, posted_date, rating,
               yt_views, yt_watch_pct, hook_type
        FROM videos
        ORDER BY COALESCE(posted_date,'') DESC
        LIMIT ?
    """, (args.limit or 20,)).fetchall()
    conn.close()

    if not rows:
        print("저장된 피드백이 없습니다.")
        return

    print(f"\n{'video_key':<34} {'평점':>4} {'조회':>8} {'시청%':>6}  {'훅':^10}  주제")
    print("-" * 95)
    for r in rows:
        rating = f"{r['rating']}/5" if r["rating"] else "  -  "
        views  = f"{r['yt_views']:,}" if r["yt_views"] else "-"
        watch  = f"{r['yt_watch_pct']:.0f}%" if r["yt_watch_pct"] else "-"
        hook   = (r["hook_type"] or "-")[:10]
        print(f"{r['video_key']:<34} {rating:>4} {views:>8} {watch:>6}  {hook:<10}  {r['topic'] or ''}")


# ─────────────────────────────────────────────
# stats — 집계 통계
# ─────────────────────────────────────────────

def cmd_stats(args):
    conn = get_conn()
    c    = conn.cursor()
    total = c.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    rated = c.execute("SELECT COUNT(*) FROM videos WHERE rating IS NOT NULL").fetchone()[0]

    print(f"\n=== Brain50 피드백 통계 (총 {total}개 / 평가 완료 {rated}개) ===")
    if rated == 0:
        print("아직 평가 데이터가 없습니다.")
        conn.close()
        return

    avg_r = c.execute("SELECT AVG(rating) FROM videos WHERE rating IS NOT NULL").fetchone()[0]
    print(f"평균 평점: {avg_r:.2f}/5")

    vr = c.execute("SELECT AVG(yt_views), MAX(yt_views), MIN(yt_views) FROM videos WHERE yt_views IS NOT NULL").fetchone()
    if vr and vr[0]:
        print(f"조회수: 평균 {int(vr[0]):,} / 최고 {int(vr[1]):,} / 최저 {int(vr[2]):,}")

    wr = c.execute("SELECT AVG(yt_watch_pct) FROM videos WHERE yt_watch_pct IS NOT NULL").fetchone()
    if wr and wr[0]:
        print(f"평균 시청 완료율: {wr[0]:.1f}%")

    print("\n[훅 유형별 평점]")
    for row in c.execute("""
        SELECT hook_type, AVG(rating) AS avg_r, COUNT(*) AS cnt
        FROM videos WHERE rating IS NOT NULL AND hook_type != '' AND hook_type IS NOT NULL
        GROUP BY hook_type ORDER BY avg_r DESC
    """).fetchall():
        bar = "★" * round(row["avg_r"]) + "☆" * (5 - round(row["avg_r"]))
        print(f"  {row['hook_type']:<10} {bar}  {row['avg_r']:.1f}/5  ({row['cnt']}회)")

    good_kw = c.execute("""
        SELECT keyword, COUNT(*) FROM keywords WHERE sentiment=1
        GROUP BY keyword ORDER BY COUNT(*) DESC LIMIT 10
    """).fetchall()
    if good_kw:
        print(f"\n[누적 좋은 키워드]  {', '.join(r[0] for r in good_kw)}")

    bad_kw = c.execute("""
        SELECT keyword, COUNT(*) FROM keywords WHERE sentiment=-1
        GROUP BY keyword ORDER BY COUNT(*) DESC LIMIT 6
    """).fetchall()
    if bad_kw:
        print(f"[누적 나쁜 키워드]  {', '.join(r[0] for r in bad_kw)}")

    conn.close()


# ─────────────────────────────────────────────
# insights — 인사이트 생성 & 저장
# ─────────────────────────────────────────────

def generate_insights(conn):
    """
    피드백 DB에서 인사이트를 추출한다.
    returns: (prompt_text: str, data: dict)
    샘플이 없으면 ("", {}) 반환.
    """
    c     = conn.cursor()
    total = c.execute("SELECT COUNT(*) FROM videos WHERE rating IS NOT NULL").fetchone()[0]
    if total == 0:
        return "", {"total_videos": 0}

    lines = [f"=== Brain50 콘텐츠 학습 데이터 (누적 {total}개 영상 평가 기반) ==="]
    data  = {"total_videos": total, "generated_at": datetime.now().isoformat()}

    # ── 훅 유형 성과
    hook_rows = c.execute("""
        SELECT hook_type,
               ROUND(AVG(rating), 2)        AS avg_r,
               COUNT(*)                     AS cnt,
               ROUND(AVG(yt_views))         AS avg_views,
               ROUND(AVG(yt_watch_pct), 1)  AS avg_watch
        FROM videos
        WHERE rating IS NOT NULL AND hook_type != '' AND hook_type IS NOT NULL
        GROUP BY hook_type
        ORDER BY avg_r DESC
    """).fetchall()

    if hook_rows:
        lines.append("\n[훅 유형별 성과]")
        hook_data = []
        for r in hook_rows:
            lbl = f"  {r['hook_type']}: {r['avg_r']}/5 ({r['cnt']}회)"
            if r["avg_views"]:
                lbl += f"  조회수 {int(r['avg_views']):,}"
            if r["avg_watch"]:
                lbl += f"  시청률 {r['avg_watch']}%"
            lines.append(lbl)
            hook_data.append({k: r[k] for k in r.keys()})
        data["hook_performance"] = hook_data

        best = hook_rows[0]
        if best["cnt"] >= 2:
            data["best_hook_type"] = best["hook_type"]
            lines.append(f"  → 효과 좋은 훅: {best['hook_type']} (평균 {best['avg_r']}/5, {best['cnt']}회)")
        worst = hook_rows[-1]
        if worst["cnt"] >= 2 and worst["avg_r"] < 3.0:
            data["avoid_hook_type"] = worst["hook_type"]
            lines.append(f"  → 피해야 할 훅: {worst['hook_type']} (평균 {worst['avg_r']}/5)")

    # ── 좋은 키워드
    good_kw = c.execute("""
        SELECT k.keyword, COUNT(*) AS cnt, ROUND(AVG(v.rating), 1) AS avg_r
        FROM keywords k JOIN videos v ON k.video_key = v.video_key
        WHERE k.sentiment = 1
        GROUP BY k.keyword
        ORDER BY cnt DESC, avg_r DESC
        LIMIT 12
    """).fetchall()
    if good_kw:
        words = [r["keyword"] for r in good_kw]
        lines.append(f"\n[반응 좋았던 단어/표현]\n  {', '.join(words)}")
        data["good_keywords"] = words

    # ── 나쁜 키워드
    bad_kw = c.execute("""
        SELECT keyword, COUNT(*) AS cnt FROM keywords
        WHERE sentiment = -1
        GROUP BY keyword ORDER BY cnt DESC LIMIT 8
    """).fetchall()
    if bad_kw:
        words = [r["keyword"] for r in bad_kw]
        lines.append(f"\n[피해야 할 단어/표현]\n  {', '.join(words)}")
        data["bad_keywords"] = words

    # ── 고평가 주제
    top = c.execute("""
        SELECT topic, title, rating, yt_views, yt_watch_pct
        FROM videos WHERE rating >= 4
        ORDER BY COALESCE(yt_views, 0) DESC, rating DESC
        LIMIT 5
    """).fetchall()
    if top:
        lines.append("\n[평점 4-5 영상 주제 (잘 반응한 방향)]")
        top_list = []
        for r in top:
            lbl = f"  [{r['rating']}/5] {r['topic']}"
            if r["yt_views"]:
                lbl += f"  ({r['yt_views']:,}회)"
            if r["yt_watch_pct"]:
                lbl += f"  시청률{r['yt_watch_pct']:.0f}%"
            lines.append(lbl)
            top_list.append({"topic": r["topic"], "rating": r["rating"]})
        data["top_topics"] = top_list

    # ── 저평가 주제 방향
    low = c.execute("""
        SELECT topic FROM videos WHERE rating <= 2 ORDER BY rating ASC LIMIT 5
    """).fetchall()
    if low:
        low_topics = [r["topic"] for r in low]
        lines.append(f"\n[반응 낮았던 주제 방향 (피할 것)]\n  {', '.join(low_topics)}")
        data["low_topics"] = low_topics

    # ── 시청 완료율 높은 패턴
    hw = c.execute("""
        SELECT topic, hook_type, yt_watch_pct FROM videos
        WHERE yt_watch_pct IS NOT NULL AND yt_watch_pct >= 65
        ORDER BY yt_watch_pct DESC LIMIT 3
    """).fetchall()
    if hw:
        lines.append("\n[시청 완료율 65%+ 패턴]")
        for r in hw:
            lines.append(f"  {r['topic']} | {r['hook_type']} | {r['yt_watch_pct']:.0f}%")

    lines.append("\n※ 샘플 수가 적을수록 불확실성이 높습니다. 항상 주제 특성과 근거 자료를 우선하세요.")
    lines.append("=" * 50)

    return "\n".join(lines), data


def cmd_insights(args):
    conn = get_conn()
    prompt_text, data = generate_insights(conn)
    conn.close()

    if not prompt_text:
        print("저장된 평가 데이터가 없습니다. 먼저 'rate' 명령으로 피드백을 입력하세요.")
        return

    os.makedirs(os.path.dirname(INSIGHTS_PATH), exist_ok=True)
    with open(INSIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump({"prompt_text": prompt_text, "data": data}, f, ensure_ascii=False, indent=2)

    print(prompt_text)
    print(f"\n✅ 인사이트 저장: {INSIGHTS_PATH}")
    print("   다음 0_script.py 실행 시 자동으로 프롬프트에 반영됩니다.")


# ─────────────────────────────────────────────
# 외부 호출 인터페이스 (0_script.py에서 사용)
# ─────────────────────────────────────────────

def load_insights_for_prompt(insights_path=None):
    """
    0_script.py의 build_prompt()에서 호출.
    insights 파일이 존재하면 prompt_text를 반환, 없으면 빈 문자열.
    """
    path = insights_path or INSIGHTS_PATH
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("prompt_text", "")
    except Exception:
        return ""


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Brain50 피드백 & 인사이트 시스템",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python 5_feedback.py rate
  python 5_feedback.py update 2026-06-25_수면치매
  python 5_feedback.py tag 2026-06-25_수면치매 "치매 예방" +1 --ktype topic_word
  python 5_feedback.py list --limit 10
  python 5_feedback.py stats
  python 5_feedback.py insights
        """,
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("rate", help="현재 작업 디렉토리 영상 평가 입력")

    upd = sub.add_parser("update", help="YouTube 지표 업데이트")
    upd.add_argument("video_key", nargs="?", help="video_key (생략 시 대화형 선택)")

    tag = sub.add_parser("tag", help="키워드 태깅")
    tag.add_argument("video_key")
    tag.add_argument("keyword")
    tag.add_argument("sentiment", choices=["+1", "1", "0", "-1"])
    tag.add_argument("--ktype", default="topic_word",
                     choices=["topic_word", "hook_phrase", "scene_expr", "hashtag", "visual"])
    tag.add_argument("--notes", default="")

    lst = sub.add_parser("list", help="평가 목록 조회")
    lst.add_argument("--limit", type=int, default=20)

    sub.add_parser("stats",    help="집계 통계")
    sub.add_parser("insights", help="0_script.py 주입용 인사이트 생성")

    return parser.parse_args()


def main():
    args = parse_args()
    if not args.cmd:
        print("사용법: python 5_feedback.py {rate|update|tag|list|stats|insights} --help")
        sys.exit(0)
    {
        "rate":     cmd_rate,
        "update":   cmd_update,
        "tag":      cmd_tag,
        "list":     cmd_list,
        "stats":    cmd_stats,
        "insights": cmd_insights,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
