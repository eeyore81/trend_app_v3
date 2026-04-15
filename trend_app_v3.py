import argparse
import json
import logging
import os
import time
import random
import re
import requests
import io
# Ensure Matplotlib has a writable config/cache directory in CI (prevents
# permission issues and lets us rebuild the font cache reliably).
mpl_config_dir = os.path.join(os.getcwd(), '.mplconfig')
os.environ['MPLCONFIGDIR'] = mpl_config_dir
os.makedirs(mpl_config_dir, exist_ok=True)

import matplotlib
matplotlib.use('Agg')
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
# We no longer use vendored Korean fonts — clear any font prop placeholder.
_KOREAN_FONT_PROP = None
import platform
import pandas as pd
from matplotlib import rc
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET
import html

# urllib3 v2 compatibility: pytrends may pass deprecated method_whitelist to Retry
try:
    from urllib3.util import Retry as Urllib3Retry
    _orig_retry_init = Urllib3Retry.__init__
    def _compat_retry_init(self, *args, **kwargs):
        if 'method_whitelist' in kwargs:
            kwargs['allowed_methods'] = kwargs.pop('method_whitelist')
        return _orig_retry_init(self, *args, **kwargs)
    Urllib3Retry.__init__ = _compat_retry_init
except Exception:
    pass

from pytrends.request import TrendReq

# --- 1. 환경 설정 (반드시 수정하세요) ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')  # 환경변수로 관리하세요
CHAT_ID = os.getenv('CHAT_ID', '1677003257')
DB_FILE = "keywords.json"
TREND_TIMEFRAME = 'today 3-m'  # 90일(약 3개월) 데이터
GEO = ''  # 전세계
RECENT_WINDOW_DAYS = 3  # 최근 3일 평균으로 급등 판단
THRESHOLD_ACCEL = 5  # 3일 평균 변화 기준
THRESHOLD_GAP = 5  # 두 용어 간 간격 좁아짐 기준
THRESHOLD_HIGH = 90   # 고점 알람 기준
CACHE_TTL_SECONDS = 21600  # 상태 표시용 임시 캐시 유효 시간
DAILY_FETCH_INTERVAL = 10000  # 하루에 한 번만 새로 가져오기
TELEGRAM_POLL_INTERVAL = 5  # Telegram getUpdates 폴링 주기
CACHE_FILE = 'trend_cache.json'
STARTED_CHATS_FILE = 'started_chats.json'

USER_AGENTS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
]

def get_request_headers():
    ua = random.choice(USER_AGENTS)
    headers = {
        'User-Agent': ua,
        'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept': 'application/json, text/plain, */*', # 트렌드 API는 보통 JSON을 주고받음
        'Referer': 'https://trends.google.com/trends/explore',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'x-client-data': 'CIa2yQE=', # 구글 특유의 클라이언트 데이터 (선택사항)
    }
    return headers

# 필요 시 프록시를 추가하세요. 기본은 빈 딕셔너리입니다.
PROXIES = {}

# 로그 파일 설정
logging.basicConfig(
    filename='trend_app.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8'
)
logger = logging.getLogger(__name__)

plt.rcParams['axes.unicode_minus'] = False

# --- 2. 데이터 관리 함수 ---
def normalize_keyword(keyword):
    keyword = str(keyword).strip()
    if re.fullmatch(r'\s*medicube\s*\+\s*cosrx\s*', keyword, flags=re.IGNORECASE):
        return 'cosrx+medicube'
    return keyword


def load_keywords():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            raw_keywords = json.load(f)
        normalized = {normalize_keyword(str(kw)) for kw in raw_keywords if str(kw).strip()}
        if normalized != set(raw_keywords):
            save_keywords(normalized)
        return normalized
    return set()


def save_keywords(keywords):
    normalized = {normalize_keyword(str(kw)) for kw in keywords if str(kw).strip()}
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(list(normalized), f, ensure_ascii=False)
    cleanup_cache_data()


def cleanup_cache_data():
    valid_keywords = load_keywords()
    removed = [kw for kw in list(last_scores) if kw not in valid_keywords]
    for kw in removed:
        last_scores.pop(kw, None)
        last_fetch_time.pop(kw, None)
        last_trend_data.pop(kw, None)
    if removed:
        logger.info(f"Cleaned cached entries for removed keywords: {removed}")


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        for kw, score in cache.get('last_scores', {}).items():
            last_scores[kw] = int(score)
        for kw, ts in cache.get('last_fetch_time', {}).items():
            last_fetch_time[kw] = float(ts)
        for kw, payload in cache.get('last_trend_data', {}).items():
            dates = [pd.to_datetime(d) for d in payload.get('dates', [])]
            columns = payload.get('columns', [kw])
            values = payload.get('values', {})
            if dates and columns and values:
                if isinstance(values, dict):
                    data = {col: values.get(col, []) for col in columns}
                else:
                    data = {kw: values}
                df = pd.DataFrame(data, index=pd.DatetimeIndex(dates))
                last_trend_data[kw] = df
    except Exception as e:
        logger.warning(f"Failed to load cache: {e}")
    cleanup_cache_data()


def save_cache():
    try:
        serialized = {
            'last_scores': {kw: score for kw, score in last_scores.items()},
            'last_fetch_time': {kw: ts for kw, ts in last_fetch_time.items()},
            'last_trend_data': {}
        }
        for kw, df in last_trend_data.items():
            serialized['last_trend_data'][kw] = {
                'dates': [idx.isoformat() for idx in df.index],
                'columns': list(df.columns),
                'values': {col: [int(v) for v in df[col].tolist()] for col in df.columns}
            }
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(serialized, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to save cache: {e}")


def load_started_chats():
    global started_chats
    started_chats.clear()
    if not os.path.exists(STARTED_CHATS_FILE):
        return
    try:
        with open(STARTED_CHATS_FILE, 'r', encoding='utf-8') as f:
            items = json.load(f)
        for it in items:
            if it:
                started_chats.add(str(it))
    except Exception as e:
        logger.warning(f"Failed to load started chats: {e}")


def save_started_chats():
    try:
        with open(STARTED_CHATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(started_chats), f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to save started chats: {e}")


def fetch_news_headlines(keyword, max_results=5):
    """Google News RSS에서 제목+링크를 가져옵니다."""
    try:
        rss_url = 'https://news.google.com/rss/search'
        params = {
            'q': f'{keyword} when:7d',
            'hl': 'ko',
            'gl': 'KR',
            'ceid': 'KR:ko'
        }
        resp = requests.get(rss_url, params=params, headers=get_request_headers(), timeout=10)
        if not resp.ok:
            logger.warning(f"Failed to fetch news RSS for {keyword}: {resp.status_code}")
            return []
        root = ET.fromstring(resp.content)
        items = root.findall('.//item')
        headlines = []
        for item in items[:max_results]:
            title = item.findtext('title')
            link = item.findtext('link') or item.findtext('guid')
            if title and link:
                headlines.append({'title': title, 'link': link})
        if headlines:
            return headlines

        # fallback: 검색어만으로 다시 시도
        params['q'] = keyword
        resp = requests.get(rss_url, params=params, headers=get_request_headers(), timeout=10)
        if resp.ok:
            root = ET.fromstring(resp.content)
            items = root.findall('.//item')
            for item in items[:max_results]:
                title = item.findtext('title')
                link = item.findtext('link') or item.findtext('guid')
                if title and link:
                    headlines.append({'title': title, 'link': link})
        return headlines
    except Exception as e:
        logger.warning(f"Failed to fetch news RSS for {keyword}: {e}")
        return []


# --- 공통 유틸: 그래프 생성 및 텔레그램 전송 헬퍼 ---
def plot_trend_image(keyword, data, title=None, ymax=100, fill_between_col=None):
    plt.figure(figsize=(10, 5))
    # drop pytrends' isPartial column and plot only numeric columns to avoid
    # glyph/font issues and incorrect plotting of the boolean column.
    df = data.copy()
    if 'isPartial' in df.columns:
        df = df.drop(columns=['isPartial'])

    # prefer numeric columns; fall back to remaining columns if none found
    numeric_cols = list(df.select_dtypes(include=['number']).columns)
    terms = numeric_cols if numeric_cols else list(df.columns)

    ax = plt.gca()
    for term in terms:
        ax.plot(df.index, df[term], marker='o', linewidth=2, label=term)

    # only fill if the requested column exists in the filtered dataframe
    if fill_between_col is not None and fill_between_col in df.columns:
        plt.fill_between(df.index, df[fill_between_col], color='#ff4500', alpha=0.1)

    # Titles/labels in English (no vendored font used)
    ax.set_title(title or f"📈 Keyword monitoring: {keyword} (last 90 days)")
    ax.set_xlabel("Period (last 90 days)")
    ax.set_ylabel("Interest score (0-100)")
    plt.grid(True, axis='y', linestyle='--', alpha=0.7)
    plt.ylim(0, ymax)
    ax.axhline(THRESHOLD_HIGH, color='red', linestyle='--', linewidth=1, alpha=0.7, label=f'Peak threshold ({THRESHOLD_HIGH})')
    plt.legend(loc='upper left')

    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close()
    return buf


def send_telegram_photo(buf, caption, chat_id=None, filename='graph.png'):
    buf.seek(0, io.SEEK_END)
    if buf.tell() == 0:
        logger.warning("Telegram photo buffer is empty, falling back to text message")
        if chat_id:
            send_text_message(caption, chat_id=chat_id)
        else:
            send_text_message(caption)
        return False
    buf.seek(0)

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    files = {'photo': (filename, buf, 'image/png')}
    targets = [chat_id] if chat_id else (list(started_chats) if started_chats else [CHAT_ID])
    any_success = False
    for t in targets:
        try:
            resp = requests.post(url, data={'chat_id': t, 'caption': caption}, files=files, timeout=10)
            if resp.ok:
                logger.info(f"Telegram photo sent, chat_id={t}")
                print(f"✅ 텔레그램 전송 성공: chat_id={t}")
                any_success = True
            else:
                logger.error(f"Telegram photo failed: status={resp.status_code}, response={resp.text}, chat_id={t}")
                print(f"❌ 텔레그램 전송 실패: status={resp.status_code}, response={resp.text}, chat_id={t}")
                if resp.status_code == 400:
                    logger.warning("Bad request on sendPhoto, falling back to text message")
                    send_text_message(caption, chat_id=t)
        except Exception as e:
            logger.exception("Telegram photo exception")
            print(f"❌ 전송 실패 (chat_id={t}): {e}")
    return any_success

# --- 3. 그래프 생성 및 텔레그램 전송 ---
def send_trend_report(keyword, data, score, diff=None, gap=None, is_first=False, chat_id=None):
    """그래프를 그려서 텔레그램으로 전송"""
    # 이미지 생성
    buf = plot_trend_image(keyword, data, title=f"📈 Keyword monitoring: {keyword} (last 90 days)", fill_between_col=list(data.columns)[-1] if data.columns.any() else None)

    # 메시지 구성
    if is_first:
        caption = f"✅ [Monitoring started] '{keyword}'\nCurrent trend score: {score} pts"
    elif gap is not None and abs(gap) >= THRESHOLD_GAP:
        if gap > 0:
            caption = f"⚡ [Gap narrowing] '{keyword}' (gap reduced by {gap} pts)"
        else:
            caption = f"⚡ [Gap widening] '{keyword}' (gap increased by {-gap} pts)"
    elif diff is not None and abs(diff) >= THRESHOLD_ACCEL:
        caption = f"🚀 [Acceleration] '{keyword}' \nCurrent: {score} ({diff} change)"
    else:
        caption = f"🔥 [High alert] '{keyword}' current score: {score}"

    # 텔레그램 전송
    send_telegram_photo(buf, caption, chat_id=chat_id, filename='graph.png')


def build_status_text():
    keywords = load_keywords()
    if not keywords:
        return "📌 현재 감시 중인 키워드가 없습니다."

    parts = ["📌 현재 감시 상태"]
    for kw in sorted(keywords):
        score = last_scores.get(kw)
        if score is None:
            parts.append(f"- {kw}: 조회 안됨")
        else:
            note = ""
            if should_fetch_daily(kw):
                note = " (24시간 경과, 캐시)"
            elif not is_cache_fresh(kw):
                note = " (캐시된 데이터)"
            parts.append(f"- {kw}: {score}점{note}")
    parts.append(f"\n🔔 고점 기준: {THRESHOLD_HIGH}점")
    return "\n".join(parts)


def is_cache_fresh(keyword):
    if keyword not in last_fetch_time:
        return False
    return (time.time() - last_fetch_time[keyword]) < CACHE_TTL_SECONDS


def parse_keyword_terms(keyword):
    keyword = keyword.strip()
    if re.search(r'\b(and)\b', keyword, flags=re.IGNORECASE) or '*' in keyword:
        parts = re.split(r'\s*(?:\*|\band\b)\s*', keyword, flags=re.IGNORECASE)
        combined = ' '.join(part.strip() for part in parts if part.strip())
        return [combined] if combined else []

    parts = re.split(r'\s*(?:,|\+|\|)\s*', keyword)
    return [part.strip() for part in parts if part.strip()]


def get_primary_scoring_terms(keyword, terms):
    if '+' in keyword:
        plus_parts = [part.strip() for part in keyword.split('+') if part.strip()]
        if len(plus_parts) == 2:
            return [plus_parts[0]]
    return terms


def compute_pairwise_gap(data, terms, window=RECENT_WINDOW_DAYS):
    valid_terms = [term for term in terms if term in data]
    if len(valid_terms) != 2:
        return None, None

    gap_values = []
    prev_gap_values = []
    for term in valid_terms:
        values = data[term].dropna()
        if len(values) <= 1:
            return None, None
        values = values.iloc[:-1]
        if values.empty:
            return None, None

        if len(values) >= window:
            current = float(values.iloc[-window:].mean())
        else:
            current = float(values.iloc[-1])

        if len(values) >= 2 * window:
            prev = float(values.iloc[-2 * window:-window].mean())
        elif len(values) >= window + 1:
            prev = float(values.iloc[-(window + 1):-window].mean())
        elif len(values) >= 2:
            prev = float(values.iloc[-2])
        else:
            return None, None

        gap_values.append(current)
        prev_gap_values.append(prev)

    return abs(gap_values[0] - gap_values[1]), abs(prev_gap_values[0] - prev_gap_values[1])


def get_recent_average(data, terms, window=RECENT_WINDOW_DAYS, scoring_terms=None):
    if scoring_terms is None:
        scoring_terms = terms

    valid_terms = [term for term in scoring_terms if term in data]
    if not valid_terms:
        return None, None, None, None

    current_avgs = []
    prev_avgs = []

    for term in valid_terms:
        values = data[term].dropna()
        if len(values) <= 1:
            continue
        values = values.iloc[:-1]
        if values.empty:
            continue

        if len(values) >= window:
            current_avgs.append(float(values.iloc[-window:].mean()))
        else:
            current_avgs.append(float(values.iloc[-1]))

        if len(values) >= 2 * window:
            prev_avgs.append(float(values.iloc[-2 * window:-window].mean()))
        elif len(values) >= window + 1:
            prev_avgs.append(float(values.iloc[-(window + 1):-window].mean()))
        elif len(values) >= 2:
            prev_avgs.append(float(values.iloc[-2]))

    if not current_avgs:
        return None, None, None, None

    current_avg = max(current_avgs)
    prev_avg = max(prev_avgs) if prev_avgs else None
    current_gap, prev_gap = compute_pairwise_gap(data, terms, window)
    return current_avg, prev_avg, current_gap, prev_gap


def linear_slope(values):
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    numerator = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(values))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    return numerator / denominator if denominator != 0 else 0.0


def get_trend_metrics(data, terms, window=RECENT_WINDOW_DAYS):
    valid_terms = [term for term in terms if term in data]
    if not valid_terms:
        return None, None, None, None, 0.0, False

    df = data[valid_terms].copy()
    if 'isPartial' in df.columns:
        df = df.drop(columns=['isPartial'])
    if df.empty:
        return None, None, None, None, 0.0, False

    interest = df.max(axis=1).dropna()
    if len(interest) <= 1:
        return None, None, None, None, 0.0, False
    interest = interest.iloc[:-1]
    if interest.empty:
        return None, None, None, None, 0.0, False

    if len(interest) >= window:
        current_avg = float(interest.iloc[-window:].mean())
    else:
        current_avg = float(interest.iloc[-1])

    prev_avg = None
    if len(interest) >= 2 * window:
        prev_avg = float(interest.iloc[-2 * window:-window].mean())
    elif len(interest) >= window + 1:
        prev_avg = float(interest.iloc[-(window + 1):-window].mean())

    diff = current_avg - prev_avg if prev_avg is not None else None
    pct_change = (diff / prev_avg * 100) if prev_avg and prev_avg != 0 else None
    slope = linear_slope(interest.tolist())

    recent = interest.tail(30)
    spike = False
    if len(recent) >= 3:
        spike = current_avg > (recent.mean() + 2 * recent.std(ddof=0))

    return current_avg, prev_avg, diff, pct_change, slope, spike


def format_trend_reason(current_avg, prev_avg, diff, pct_change, spike):
    reasons = []
    if prev_avg is not None and diff is not None:
        if abs(diff) >= THRESHOLD_ACCEL:
            reasons.append('가속')
        if diff < 0:
            reasons.append('감소')
        if pct_change is not None and abs(pct_change) >= 20:
            reasons.append(f'{pct_change:+.1f}%')
    if current_avg is not None and current_avg >= THRESHOLD_HIGH:
        reasons.append('고점')
    if spike:
        reasons.append('스파이크')
    return ', '.join(reasons) if reasons else '보통'


def fetch_keyword_score(keyword):
    terms = parse_keyword_terms(keyword)
    score_terms = get_primary_scoring_terms(keyword, terms)
    try:
        if keyword in last_scores and keyword in last_trend_data and is_cache_fresh(keyword):
            logger.info(f"Using cached score for {keyword}")
            return last_scores[keyword]

        pytrends = create_pytrends()
        time.sleep(random.uniform(2.5, 5.0))

        retry_attempts = 3
        wait_seconds = 1.0
        data = None

        for attempt in range(1, retry_attempts + 1):
            try:
                pytrends.build_payload(terms, timeframe=TREND_TIMEFRAME, geo=GEO)
                data = pytrends.interest_over_time()
                break
            except Exception as err:
                err_text = str(err)
                if '429' in err_text or 'Too Many Requests' in err_text:
                    logger.warning(f"429 detected during status fetch: {keyword}, attempt {attempt}/{retry_attempts}, wait {wait_seconds:.1f}s")
                    print(f"⚠️ 429 감지(조회 시도): {keyword}, 시도 {attempt}/{retry_attempts}, 대기 {wait_seconds:.1f}s")
                    if attempt == retry_attempts:
                        # send_text_message(f"⚠️ Google Trend 429 오류 발생\n키워드: {keyword}\n에러: {err_text}")
                        logger.error(f"Google Trend 429 after retries: {keyword}, error={err_text}")
                        return None
                    time.sleep(wait_seconds)
                    wait_seconds *= 2
                    continue
                logger.error(f"{keyword} status fetch error: {err_text}")
                print(f"⚠️ {keyword} 상태 조회 오류: {err_text}")
                return None

        if data is not None and not data.empty and all(term in data for term in terms):
            latest_values = [int(data[term].iloc[-1]) for term in score_terms if term in data]
            if not latest_values:
                latest_values = [int(data[term].iloc[-1]) for term in terms if term in data]
            score = int(latest_values[0]) if len(latest_values) == 1 else int(max(latest_values))
            last_scores[keyword] = score
            last_trend_data[keyword] = data
            last_fetch_time[keyword] = time.time()
            save_cache()
            logger.info(f"Fetched score for {keyword}: {score}")
            return score
        return None
    except Exception as e:
        error_text = str(e)
        logger.exception(f"{keyword} status fetch exception: {error_text}")
        print(f"⚠️ {keyword} 상태 조회 예외: {error_text}")
        if '429' in error_text or 'Too Many Requests' in error_text:
            send_text_message(f"⚠️ Google Trend 429 오류 발생\n키워드: {keyword}\n에러: {error_text}")
        return None


def should_fetch_daily(keyword):
    if keyword not in last_fetch_time:
        return True
    return (time.time() - last_fetch_time[keyword]) >= DAILY_FETCH_INTERVAL



def send_status_trend_graph(keyword, data, score, chat_id=None):
    # exclude isPartial from displayed query list
    terms = [c for c in data.columns if c != 'isPartial']
    buf = plot_trend_image(keyword, data, title=f"📈 {keyword} - Last 90 days trend", fill_between_col=terms[0] if len(terms) == 1 else None)

    caption = (
        f"📈 '{keyword}' last 90 days trend\n"
        f"Current score: {score}\n"
        f"Queries: {', '.join(terms)}\n"
        f"Geo: {'Korea' if GEO == 'KR' else 'Worldwide'}"
    )
    send_telegram_photo(buf, caption, chat_id=chat_id, filename='status_trend.png')


def send_status_trend_graphs(chat_id=None):
    keywords = sorted(load_keywords())
    for kw in keywords:
        if kw in last_trend_data and kw in last_scores:
            send_status_trend_graph(kw, last_trend_data[kw], last_scores[kw], chat_id=chat_id)


def create_dummy_trend_dataframe(keyword, num_days=90):
    end_date = pd.Timestamp.today().normalize()
    dates = pd.date_range(end=end_date, periods=num_days)
    values = [max(5, min(70, int(round(50 + random.gauss(0, 12))))) for _ in range(num_days)]
    return pd.DataFrame({keyword: values}, index=dates)


def build_hot_test_data(keyword, original_data=None):
    if original_data is None or original_data.empty:
        data = create_dummy_trend_dataframe(keyword)
    else:
        data = original_data.copy()
        data = data.astype(float)
        if data.isnull().values.any():
            data = data.fillna(method='ffill').fillna(0)

    for col in data.columns:
        values = [float(v) for v in data[col].tolist()]
        if len(values) < RECENT_WINDOW_DAYS + 3:
            while len(values) < RECENT_WINDOW_DAYS + 3:
                values.insert(0, 20.0)

        for i in range(len(values) - RECENT_WINDOW_DAYS):
            values[i] = min(70.0, max(0.0, values[i]))

        hot_start = len(values) - RECENT_WINDOW_DAYS
        for idx in range(hot_start, len(values)):
            values[idx] = 95.0 + min(5.0, idx - hot_start)

        data[col] = [min(100, max(0, int(round(v)))) for v in values]

    return data


def send_test_trend(keyword, chat_id=None):
    keywords = load_keywords()
    if keyword not in keywords:
        send_text_message(f"🧪 테스트 실패: '{keyword}'이(가) 감시 목록에 없습니다. 먼저 키워드를 추가하세요.", chat_id=chat_id)
        return

    original_data = last_trend_data.get(keyword)
    synthetic_data = build_hot_test_data(keyword, original_data)
    terms = list(synthetic_data.columns)
    score_terms = get_primary_scoring_terms(keyword, terms)
    current_avg, prev_avg, current_gap, prev_gap = get_recent_average(synthetic_data, terms, scoring_terms=score_terms)
    score = int(round(current_avg)) if current_avg is not None else 100
    diff = int(round(current_avg - prev_avg)) if prev_avg is not None else None
    gap = int(round(prev_gap - current_gap)) if current_gap is not None and prev_gap is not None else None
    send_text_message(f"🧪 테스트용 핫 트렌드 전송: '{keyword}'", chat_id=chat_id)
    send_trend_report(keyword, synthetic_data, score, diff=diff, gap=gap, chat_id=chat_id)


def get_keyword_summary_metrics():
    metrics = []
    for kw in sorted(load_keywords()):
        data = last_trend_data.get(kw)
        if data is None or data.empty:
            continue
        terms = parse_keyword_terms(kw)
        score_terms = get_primary_scoring_terms(kw, terms)
        current_avg, prev_avg, diff, pct_change, slope, spike = get_trend_metrics(data, score_terms)
        if current_avg is None:
            continue
        metrics.append({
            'keyword': kw,
            'score': int(round(current_avg)),
            'diff': diff,
            'pct_change': pct_change,
            'slope': slope,
            'spike': spike,
            'reason': format_trend_reason(current_avg, prev_avg, diff, pct_change, spike)
        })
    return metrics


def build_summary_text():
    keywords = sorted(load_keywords())
    if not keywords:
        return '📌 현재 감시 중인 키워드가 없습니다.'

    metrics = get_keyword_summary_metrics()
    if not metrics:
        return '📌 감시 중인 키워드가 있으나 최신 트렌드 데이터를 불러올 수 없습니다.'

    rising = sorted(metrics, key=lambda item: (item['pct_change'] if item['pct_change'] is not None else (item['diff'] or 0)), reverse=True)
    falling = sorted(metrics, key=lambda item: (item['pct_change'] if item['pct_change'] is not None else (item['diff'] or 0)))
    alerts = [item for item in metrics if item['reason'] != '보통']

    lines = [
        '📊 트렌드 요약 리포트',
        f'총 감시 키워드: {len(keywords)}',
        f'유효 데이터 키워드: {len(metrics)}',
        ''
    ]

    if rising:
        lines.append('🔺 상승 TOP 3')
        for item in rising[:3]:
            pct = f'{item["pct_change"]:+.1f}%' if item['pct_change'] is not None else 'N/A'
            reason = item['reason']
            if item['pct_change'] is not None and pct in reason:
                reason = ', '.join(part for part in reason.split(', ') if part != pct)
            lines.append(f'- {item["keyword"]}: {item["score"]}점 ({pct}, {reason})')
        lines.append('')

    if falling:
        lines.append('🔻 하락 TOP 3')
        for item in falling[:3]:
            pct = f'{item["pct_change"]:+.1f}%' if item['pct_change'] is not None else 'N/A'
            reason = item['reason']
            if item['pct_change'] is not None and pct in reason:
                reason = ', '.join(part for part in reason.split(', ') if part != pct)
            lines.append(f'- {item["keyword"]}: {item["score"]}점 ({pct}, {reason})')
        lines.append('')

    if alerts:
        lines.append('🔔 주의 키워드')
        for item in alerts[:3]:
            lines.append(f'- {item["keyword"]}: {item["reason"]} (현재 {item["score"]}점)')
    else:
        lines.append('✅ 현재 주목할 급등/급락 키워드가 없습니다.')

    lines.append('')
    lines.append('📰 아모레퍼시픽 최신 기사')
    amore_headlines = fetch_news_headlines('아모레퍼시픽', max_results=10)
    if amore_headlines:
        for item in amore_headlines:
            title = html.escape(item['title'], quote=False)
            url = html.escape(item['link'], quote=True)
            lines.append(f'- <a href="{url}">{title}</a>')
    else:
        lines.append('⚠️ 아모레퍼시픽 관련 기사를 찾을 수 없습니다.')

    return '\n'.join(lines)


def send_summary_trend_graphs(chat_id=None, top_n=3):
    metrics = get_keyword_summary_metrics()
    if not metrics:
        return
    top_keywords = sorted(metrics, key=lambda item: (item['pct_change'] if item['pct_change'] is not None else (item['diff'] or 0)), reverse=True)[:top_n]
    for item in top_keywords:
        kw = item['keyword']
        if kw in last_trend_data and kw in last_scores:
            send_status_trend_graph(kw, last_trend_data[kw], last_scores[kw], chat_id=chat_id)


def send_summary_report(chat_id=None):
    text = build_summary_text()
    send_text_message(text, chat_id=chat_id, parse_mode='HTML')
    send_summary_trend_graphs(chat_id=chat_id)


def send_status_report(chat_id=None):
    text = build_status_text()
    send_text_message(text, chat_id=chat_id)
    send_status_trend_graphs(chat_id=chat_id)


def send_text_message(text, chat_id=None, parse_mode=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    targets = [chat_id] if chat_id else (list(started_chats) if started_chats else [CHAT_ID])
    any_success = False
    for t in targets:
        try:
            data = {'chat_id': t, 'text': text}
            if parse_mode:
                data['parse_mode'] = parse_mode
            resp = requests.post(url, data=data, timeout=10)
            if resp.ok:
                logger.info(f"Telegram text sent successfully to {t}")
                print(f"✅ 텔레그램 텍스트 전송 성공: chat_id={t}")
                any_success = True
            else:
                logger.error(f"Telegram text failed: status={resp.status_code}, response={resp.text}, chat_id={t}")
                print(f"❌ 텔레그램 텍스트 전송 실패: status={resp.status_code}, response={resp.text}, chat_id={t}")
        except Exception as e:
            logger.exception("Telegram text send exception")
            print(f"❌ 텔레그램 텍스트 전송 에러 (chat_id={t}): {e}")
    return any_success


def get_telegram_updates(offset=None, timeout=20):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {'timeout': timeout}
    if offset is not None:
        params['offset'] = offset
    try:
        resp = requests.get(url, params=params, timeout=timeout + 5)
        if resp.ok:
            return resp.json().get('result', [])
    except Exception as e:
        logger.exception(f"Telegram getUpdates error: {e}")
    return []


def handle_command(cmd, arg, chat_id):
    global started_chats
    # '시작' 명령: 이 채팅 ID를 저장하여 이후 chat_id 미지정 시 해당 집합으로 전송
    if cmd in ('start','시작'):
        if not chat_id:
            send_text_message("❗ 이 명령어는 Telegram 채팅에서 실행해야 합니다.", chat_id=chat_id)
            return
        started_chats.add(str(chat_id))
        save_started_chats()
        send_text_message(f"✅ 이 채팅을 시작 목록에 추가했습니다. (chat_id={chat_id})", chat_id=chat_id)
        return
    if cmd in ('add', '추가') and arg:
        arg = normalize_keyword(arg)
        keywords = load_keywords()
        keywords.add(arg)
        save_keywords(keywords)
        send_text_message(f"➕ '{arg}' 추가됨. 그래프를 전송합니다.", chat_id=chat_id)
        analyze_trend(arg, True, chat_id=chat_id)
    elif cmd in ('del', '삭제') and arg:
        arg = normalize_keyword(arg)
        keywords = load_keywords()
        if arg in keywords:
            keywords.remove(arg)
            save_keywords(keywords)
            last_scores.pop(arg, None)
            send_text_message(f"🗑️ '{arg}' 감시 종료.", chat_id=chat_id)
        else:
            send_text_message("목록에 없는 키워드입니다.", chat_id=chat_id)
    elif cmd in ('list', '목록'):
        keywords = load_keywords()
        send_text_message(f"📋 현재 감시 중: {list(keywords)}", chat_id=chat_id)
    elif cmd in ('status', '상태'):
        send_status_report(chat_id=chat_id)
    elif cmd in ('summary', '요약'):
        send_summary_report(chat_id=chat_id)
    elif cmd in ('test', '테스트'):
        if arg:
            send_test_trend(arg, chat_id=chat_id)
        else:
            keywords = sorted(load_keywords())
            if not keywords:
                send_text_message("🧪 테스트할 감시 키워드가 없습니다. 먼저 키워드를 추가하세요.", chat_id=chat_id)
            else:
                send_test_trend(random.choice(keywords), chat_id=chat_id)
    elif cmd in ('help', 'start', '도움', '도움말'):
        send_text_message(
            "명령어: 추가 [단어], 삭제 [단어], 목록, 상태, 요약, 테스트 [단어], 시작 (이 채팅을 알림 대상에 추가)",
            chat_id=chat_id
        )
    else:
        send_text_message("알 수 없는 명령어입니다. 사용법: 추가, 삭제, 목록, 상태, 테스트", chat_id=chat_id)


def process_telegram_update(update):
    message = update.get('message') or update.get('edited_message')
    if not message:
        return
    text = message.get('text')
    if not text:
        return
    chat_id = str(message['chat']['id'])
    text = text.strip()
    if text.startswith('/'):
        text = text[1:]
    parts = text.split()
    if not parts:
        return
    cmd = parts[0].lower()
    arg = ' '.join(parts[1:]) if len(parts) > 1 else None
    handle_command(cmd, arg, chat_id)


    

# --- 4. 트렌드 분석 엔진 ---
last_scores = {}
last_trend_data = {}
last_fetch_time = {}
started_chats = set()

load_cache()
load_started_chats()

def create_pytrends():
    return TrendReq(
        hl='ko-KR',
        tz=540,
        timeout=(10, 25),
        retries=3,
        requests_args={
            'headers': get_request_headers(),
            'proxies': PROXIES,
            'verify': True,
        }
    )


def analyze_trend(keyword, is_first=False, chat_id=None):
    try:
        terms = parse_keyword_terms(keyword)
        score_terms = get_primary_scoring_terms(keyword, terms)
        if not is_first and keyword in last_trend_data and is_cache_fresh(keyword):
            data = last_trend_data[keyword]
            current_avg, prev_avg, current_diff, prev_diff = get_recent_average(data, terms, scoring_terms=score_terms)
            current_score = int(round(current_avg)) if current_avg is not None else None
            logger.info(f"Using cached trend data for {keyword}: current_avg={current_avg}, prev_avg={prev_avg}")
        else:
            pytrends = create_pytrends()
            time.sleep(random.uniform(2.5, 5.0))

            retry_attempts = 1
            wait_seconds = 30.0
            data = None

            for attempt in range(1, retry_attempts + 1):
                try:
                    pytrends.build_payload(terms, timeframe=TREND_TIMEFRAME, geo=GEO)
                    data = pytrends.interest_over_time()
                    break
                except Exception as err:
                    err_text = str(err)
                    if '429' in err_text or 'Too Many Requests' in err_text:
                        logger.warning(f"429 detected during analyze_trend: {keyword}, attempt {attempt}/{retry_attempts}, wait {wait_seconds:.1f}s")
                        print(f"⚠️ 429 감지: 시도 {attempt}/{retry_attempts}, 대기 {wait_seconds:.1f}s")
                        if attempt == retry_attempts:
                            send_text_message(f"⚠️ Google Trend 429 오류 발생\n키워드: {keyword}", chat_id=chat_id)
                            logger.error(f"Google Trend 429 after retries: {keyword}, error={err_text}")
                            raise
                        time.sleep(wait_seconds)
                        wait_seconds *= 2
                        continue
                    raise

            if data is None or data.empty:
                current_score = None
                prev_avg = None
            else:
                current_avg, prev_avg, current_diff, prev_diff = get_recent_average(data, terms, scoring_terms=score_terms)
                current_score = int(round(current_avg)) if current_avg is not None else None
                last_trend_data[keyword] = data
                last_fetch_time[keyword] = time.time()
                save_cache()

        if data is not None and not data.empty:
            logger.info(f"Analyzed trend for {keyword}: current_avg={current_avg}, prev_avg={prev_avg}")
            
            should_send = False
            diff = None
            gap = None

            if is_first:
                should_send = True
            elif prev_avg is not None:
                diff = current_avg - prev_avg
                gap = prev_diff - current_diff if current_diff is not None and prev_diff is not None else None
                if abs(diff) >= THRESHOLD_ACCEL or current_score >= THRESHOLD_HIGH or (gap is not None and abs(gap) >= THRESHOLD_GAP):
                    should_send = True
            
            if should_send:
                send_trend_report(keyword, data, current_score, diff, gap, is_first, chat_id=chat_id)
            
            last_scores[keyword] = current_score
    except Exception as e:
        error_text = str(e)
        logger.exception(f"{keyword} analyze_trend exception: {error_text}")
        print(f"⚠️ {keyword} 조회 오류: {error_text}")
        # if '429' in error_text or 'Too Many Requests' in error_text:
        #     send_text_message(f"⚠️ Google Trend 429 오류 발생\n키워드: {keyword}\n에러: {error_text}", chat_id=chat_id)

# --- 5. 백그라운드 루프 & 메인 CLI ---


def monitor_once():
    keywords = load_keywords()

    for kw in list(keywords):
        # if should_fetch_daily(kw):
        analyze_trend(kw)
        time.sleep(5)


def send_scheduled_summary(chat_id=None):
    """스케줄 호출 시 요약 리포트를 전송합니다."""
    for kw in sorted(load_keywords()):
        fetch_keyword_score(kw)
        time.sleep(5)
    send_summary_report(chat_id=chat_id)


def main():
    # 1. 외부 인자 받기 설정
    parser = argparse.ArgumentParser()
    parser.add_argument('--message', type=str, default=os.getenv('TELEGRAM_MSG'), help='Full text from Telegram via GAS')
    parser.add_argument('--chat_id', type=str, default=os.getenv('CHAT_ID'), help='Telegram chat ID')
    args = parser.parse_args()

    if not TELEGRAM_TOKEN:
        print("ERROR: TELEGRAM_TOKEN is not configured. Set the TELEGRAM_TOKEN environment variable.")
        return

    if not args.message:
        print("전달된 메시지가 없습니다. 스케줄 호출로 요약 리포트를 전송합니다.")
        send_scheduled_summary(chat_id=args.chat_id)
        return

    # 2. 메시지 전처리 (기존 process_telegram_update 로직 활용)
    text = args.message.strip()
    if text.startswith('/'):
        text = text[1:]
    
    parts = text.split()
    if not parts:
        return

    cmd = parts[0].lower()
    arg = ' '.join(parts[1:]) if len(parts) > 1 else None
    
    # 3. 명령어 실행 (chat_id는 GAS에서 넘겨받거나 환경변수로 고정)
    # 만약 chat_id도 GAS에서 넘긴다면 args.chat_id를 사용하세요.
    chat_id = args.chat_id or CHAT_ID
    
    handle_command(cmd, arg, chat_id)

    # (기존의 대화형 루프 및 데몬 스레드 시작 코드는 제거되어 간결화되었습니다.)

if __name__ == "__main__":
    main()
