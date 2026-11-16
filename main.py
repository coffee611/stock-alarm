# -*- coding: utf-8 -*-
"""
박스권 + 거래량 증가 종목 텔레그램 알림 (GitHub Actions용)
데이터 출처 : 1순위 네이버 증권 (m.stock.naver.com 공개 JSON 주소, 비공식)
              2순위(백업) 한국거래소 KRX 정보데이터시스템 (pykrx 라이브러리, KRX 회원 ID/PW 필요)
  - 종목 목록 : 시가총액 순위 (ETF/ETN/레버리지/인버스/스팩/리츠 제외)
  - 종가/거래량/기관 순매수량
  - DATA_SOURCE: auto(네이버 실패 시 KRX 전환) / naver / krx
알림 : 조건 1, 2, 3 각각 별도의 텔레그램 표 이미지 (기관 순매수 큰 순, 실패 시 글자 메시지로 대체)
추가 정보 : 박스 안 위치 / 급증일 가격 방향 / 60일선 방향(단순+엄격)
성과 기록 : history/alerts.csv 에 알림 종목을 저장하고 5·10·20일 뒤 수익률을 자동 계산
자료 신선도 : 매 실행마다 기준일/지연 여부를 history/freshness.csv 에 기록하고, 하루 이상 늦으면 알림에 경고 표시
장중 점검 : MODE=intraday (11:30) - 전날 저녁 후보의 현재가·박스 돌파·거래대금 속도 (표 이미지 1장)
도착 점검 : MODE=probe - 네이버/KRX 에 당일 기관 수급이 도착했는지 확인 (수동 실행용, 1분 내외)
별도 알림 : 「기관 5일 순매수 금액 TOP 40」 (1~20위, 21~40위 표 이미지 2장) - 위 조건과 무관한 현황판
"""
import os
import re
import time
import html as htmllib
import datetime as dt
from zoneinfo import ZoneInfo

import requests
import numpy as np
import pandas as pd

# =====================================================================
# 설정 (필요하면 숫자만 바꾸세요)
# =====================================================================
WINDOW = 20             # 분석 기간 (거래일)
BOX_MAX_RANGE = 0.20    # 박스권: 20일 종가 (최고-최저)/최저 가 20% 이하

COND1_RATIO = 0.30      # 조건1: 후반10일 평균 거래량이 전반10일 평균보다 30% 이상 증가
COND2_STREAK = 6        # 조건2: 전일 대비 거래량이 '중간에 끊기지 않고' 연속 증가한 일수
COND3_MULT = 2.0        # 조건3: 20일 평균 거래량의 2배 이상 터진 날이
COND3_COUNT = 3         #        3회 이상

MA_PERIOD = 60          # 이동평균선 기간
MA_FLAT_PCT = 2.0       # 60일선 20일 변화율이 ±2% 이내면 '횡보'
MA_STRICT_DAYS = 14     # 엄격 기준: 20일 중 60일선이 전일보다 오른(내린) 날이 14일(70%) 이상

# ⭐ 눌림목 패턴 (박스 하단 + 급증일 상승일 + 60일선 엄격 상승 + 종가가 60일선 근처)
PULLBACK_BOX_MAX = 33       # 박스 위치 33% 이하(하단)
PULLBACK_SPIKE_UP_MIN = 2   # 급증일 중 종가가 전일보다 오른 날 2일 이상
PULLBACK_MA_GAP = 3.0       # 종가가 60일선 대비 ±3% 이내

BASIS_MODE = "both"     # 조건 판정 기준: both(거래량·거래대금 나란히 비교) / volume(거래량만) / value(거래대금만)
MIN_AVG_VALUE_EOK = 30  # 유동성 필터: 20일 평균 일 거래대금이 이 값(억원) 미만이면 제외 (0이면 끔)
USE_INSTITUTION = True  # True: 기관 20일 누적 순매수량이 플러스인 종목만
PER_MARKET_COUNT = 400  # 코스피/코스닥 각각 시가총액 상위 몇 종목을 검사할지
MAX_PER_CONDITION = 30  # 조건별 메시지에 표시할 최대 종목 수
REQUEST_GAP = 0.3       # 요청 간격(초) - 너무 빠르면 차단될 수 있음

SEND_PERFORMANCE = False  # 저녁 성과 요약 메시지 전송 여부 (False 여도 history/alerts.csv 기록은 계속 쌓임)
HISTORY_PATH = "history/alerts.csv"  # 성과 기록 파일
MIN_SAMPLE = 5          # 성과 통계를 보여주기 위한 최소 표본 수
# =====================================================================

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
FORCE = os.environ.get("FORCE", "").lower() == "true"  # 수동 실행이면 휴장 체크 생략
DATA_SOURCE = (os.environ.get("DATA_SOURCE") or "auto").strip().lower()
# auto/krx_first = KRX 먼저, 실패하면 네이버 / naver_first = 네이버 먼저, 실패하면 KRX / naver, krx = 해당 출처만
PRIMARY = {"src": "naver"}   # 이번 실행에서 실제로 쓴 출처 (60일선·성과 이력 조회 순서에 사용)
NOTES_EXTRA = {"provisional": ""}  # KRX 20시 최종 확정 전 자료일 때 표시할 안내
MODE = (os.environ.get("MODE") or "main").strip().lower()                  # main | intraday | probe
KRX_ID = os.environ.get("KRX_ID", "")   # KRX 회원 ID  (GitHub Secrets 에 저장 - 코드에 직접 쓰지 마세요)
KRX_PW = os.environ.get("KRX_PW", "")   # KRX 비밀번호 (GitHub Secrets 에 저장)
KST = ZoneInfo("Asia/Seoul")
M_BASE = "https://m.stock.naver.com"
S_BASE = "https://stock.naver.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                   "Mobile/15E148 Safari/604.1"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://m.stock.naver.com/",
}
LAST_INFO = {"msg": "", "keys": ""}  # 실패 원인 기록 (진단용)
FAIL_LOG = []                        # 출처별 실패 기록 (모두 실패했을 때 알림에 사용)
FRESH = {}                           # 이번 실행의 자료 신선도 (예상 기준일 / 실제 기준일 / 지연 일수)
FRESH_PATH = "history/freshness.csv"
RAW_CAPS = {}                        # 네이버 응답의 시가총액 원본값 (TOP 40 시총 대비 %용)
TOP_AMT_EOK = {}                     # KRX 실제 '기관 순매수 거래대금' 5일 합계 (억원) - TOP 40 순위·금액에 사용
PF_AMT_EOK = {}                      # KRX 실제 '연기금 순매수 거래대금' 5일 합계 (억원)
PF_PANELS = {}                       # 연기금 일별 순매수 거래대금(원) 표 (연속 일수 계산용)
PF_ERR = {"msg": ""}                 # 연기금 자료 수집 실패 원인
CUR_TOP = {"who": "기관", "inv": "기관합계", "icon": "🏦"}  # TOP 40 표 이미지에 쓰는 이름 (연기금 표를 만들 때 잠시 바뀜)
KRX_PANELS = {}                      # KRX 날짜별 시장 전체 자료 (TOP 40 계산용)
CAPS_EOK = {}                        # KRX 시가총액 (억원)

TOP_N = 40              # 기관 순매수 TOP 몇 종목
TOP_DAYS = 5            # 최근 몇 거래일 기준
SEND_PF = True          # 연기금 순매수 TOP 40 + 기관·연기금 겹침 종목 메시지 전송 (KRX 출처에서만 가능)
OVERLAP_RANK = 20       # 겹침 종목: 기관 TOP N 과 연기금 TOP N 에 모두 든 종목
TOP_FORMAT = "image"    # TOP 40 표시 방식: image(표 이미지) / text(글자) / both  (이미지가 실패하면 자동으로 글자로 대체)
COND_FORMAT = "image"   # 조건 1·2·3 표시 방식: image(표 이미지) / text(글자) / both  (이미지가 실패하면 자동으로 글자로 대체)
CODE_RE = re.compile(r"^[0-9A-Z]{6}$")
BASIS_ON = {"vol": BASIS_MODE in ("both", "volume"), "val": BASIS_MODE in ("both", "value")}
BASIS_TXT = {"both": "거래량·거래대금", "volume": "거래량", "value": "거래대금"}.get(BASIS_MODE, "거래량·거래대금")
VALUE_SRC = {"actual": 0, "est": 0}   # 거래대금을 거래소 제공값으로 썼는지, 종가×거래량 추정으로 썼는지 집계

# ETF/ETN/레버리지/스팩/리츠 제외용 이름 규칙
EXCLUDE_PREFIX = ("KODEX", "TIGER", "ACE ", "SOL ", "RISE ", "KBSTAR", "HANARO",
                  "ARIRANG", "KOSEF", "PLUS ", "TIMEFOLIO", "WON ", "1Q ", "KIWOOM ",
                  "TRUSTON", "마이티", "히어로즈", "파워 ", "BNK ", "HK ")
EXCLUDE_WORD = ("ETN", "ETF", "레버리지", "인버스", "선물", "커버드콜", "스팩", "SPAC",
                "리츠", "REIT")


def is_excluded(name, type_text=""):
    n = name.upper()
    if type_text.lower() in ("etf", "etn", "elw"):
        return True
    if n.startswith(tuple(p.upper() for p in EXCLUDE_PREFIX)):
        return True
    return any(w.upper() in n for w in EXCLUDE_WORD)


# ---------------------------------------------------------------- 공통 도우미
def get_json(url, retry=3):
    for i in range(retry):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            time.sleep(REQUEST_GAP)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    LAST_INFO["msg"] = (f"JSON이 아닌 응답 / {url} / "
                                        + r.text[:120].replace("\n", " "))
            else:
                LAST_INFO["msg"] = (f"HTTP {r.status_code} / {url} / "
                                    + r.text[:120].replace("\n", " "))
                if r.status_code in (403, 429):  # 차단 신호 -> 재시도하지 않음
                    print("요청 실패:", LAST_INFO["msg"])
                    return None
        except Exception as e:
            LAST_INFO["msg"] = f"접속 오류 {type(e).__name__}: {str(e)[:120]} / {url}"
        print("요청 실패:", LAST_INFO["msg"])
        time.sleep(1 + i)
    return None


def find_list(obj):
    """응답 안에서 '딕셔너리 목록'을 찾아 돌려줌 (응답 모양이 조금 달라도 동작)"""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            return obj
        for v in obj:
            r = find_list(v)
            if r:
                return r
    elif isinstance(obj, dict):
        for key in ("stocks", "items", "datas", "data", "result", "list", "rows"):
            if key in obj:
                r = find_list(obj[key])
                if r:
                    return r
        for v in obj.values():
            r = find_list(v)
            if r:
                return r
    return []


def pick(d, names):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return None


def to_num(x):
    try:
        return float(str(x).replace(",", "").replace("+", "").strip())
    except Exception:
        return float("nan")


def inst_value(d):
    """기관 순매수량 항목을 이름으로 찾음"""
    v = pick(d, ["organPureBuyQuant", "organizationPureBuyQuant",
                 "orgnPureBuyQuant", "institutionPureBuyQuant"])
    if v is not None:
        return v
    for k, val in d.items():
        kl = k.lower()
        if (("organ" in kl or "orgn" in kl or "institution" in kl)
                and ("buy" in kl or "quant" in kl or "qty" in kl)
                and "ratio" not in kl and "amount" not in kl):
            return val
    return None


# ---------------------------------------------------------------- 종목 목록
def get_universe():
    stocks, seen, skipped = [], set(), 0
    pages = (PER_MARKET_COUNT + 99) // 100
    for market in ("KOSPI", "KOSDAQ"):
        for page in range(1, pages + 1):
            data = get_json(f"{M_BASE}/api/stocks/marketValue/{market}"
                            f"?page={page}&pageSize=100")
            lst = find_list(data) if data is not None else []
            if not lst:
                LAST_INFO["msg"] = LAST_INFO["msg"] or f"종목 목록이 비어 있음 ({market} {page}페이지)"
                break
            for d in lst:
                code = str(pick(d, ["itemCode", "stockCode", "code", "symbolCode"]) or "")
                name = str(pick(d, ["stockName", "itemName", "name"]) or code)
                tp = str(pick(d, ["stockEndType", "stockType", "endType", "typeCode"]) or "")
                if not CODE_RE.match(code) or code in seen:
                    continue
                seen.add(code)
                if is_excluded(name, tp):
                    skipped += 1
                    continue
                cap_raw = pick(d, ["marketValue", "marketCap", "marketCapitalization", "marketSum"])
                if cap_raw is not None:
                    RAW_CAPS[code] = to_num(cap_raw)
                stocks.append((code, name))
    print(f"ETF/레버리지/스팩/리츠 등 제외: {skipped}개")
    return stocks


# ---------------------------------------------------------------- 종목 데이터
VAL_KEYS = ["accumulatedTradingValue", "tradingValue", "accumulatedTradeValue",
            "accumulatedTradingAmount", "tradingAmount", "tradeAmount", "accAmount"]


def val_value(d):
    """응답에 거래대금 항목이 있으면 찾아서 돌려줌 (없으면 None)"""
    v = pick(d, VAL_KEYS)
    if v is not None:
        return v
    for k, val in d.items():
        kl = k.lower()
        if (("tradingvalue" in kl) or ("tradeamount" in kl) or ("tradingamount" in kl)) \
                and "ratio" not in kl and "rate" not in kl:
            return val
    return None


def finalize_value(df):
    """거래대금(원) 열 확정: 거래소가 준 값이 있으면 단위를 자동 보정해서 사용, 없으면 종가×거래량으로 추정"""
    est = df["close"] * df["vol"]
    if "val" in df.columns:
        v = pd.to_numeric(df["val"], errors="coerce")
        ratio = (v / est).replace([float("inf"), float("-inf")], float("nan")).dropna()
        if v.notna().sum() >= max(3, len(df) // 2) and len(ratio) >= 3:
            med = float(ratio.median())
            for f in (1, 1e3, 1e4, 1e6, 1e8):  # 원 / 천원 / 만원 / 백만원 / 억원
                if 0.5 <= med * f <= 2.0:
                    out = df.copy()
                    out["val"] = (v * f).where(v.notna() & (v > 0), est)
                    VALUE_SRC["actual"] += 1
                    return out
    out = df.copy()
    out["val"] = est
    VALUE_SRC["est"] += 1
    return out


def rows_to_df(lst):
    out = []
    for d in lst:
        date = re.sub(r"\D", "", str(pick(d, ["bizdate", "localTradedAt", "tradeDate",
                                              "date", "businessDate"]) or ""))
        out.append((date,
                    to_num(pick(d, ["closePrice", "close"])),
                    to_num(pick(d, ["accumulatedTradingVolume", "tradingVolume", "volume"])),
                    to_num(inst_value(d)),
                    to_num(val_value(d))))
    df = pd.DataFrame(out, columns=["date", "close", "vol", "inst", "val"])
    df = df[df["date"].str.len() == 8]
    return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def get_rows(code):
    """최근 21거래일 (날짜, 종가, 거래량, 기관순매수량) -> DataFrame 또는 None"""
    need = WINDOW + 1
    urls = [f"{M_BASE}/api/stock/{code}/trend?pageSize={need}&page=1",
            f"{S_BASE}/api/domestic/detail/{code}/trend?tradeType=KRX&startIdx=0&pageSize={need}"]
    for url in urls:
        data = get_json(url, retry=2)
        lst = find_list(data) if data is not None else []
        if not lst:
            continue
        if not LAST_INFO["keys"]:
            LAST_INFO["keys"] = ", ".join(list(lst[0].keys())[:15])
        df = rows_to_df(lst)
        # 종가/거래량이 빠져 있으면 일별 시세에서 보충
        if df["close"].isna().all() or df["vol"].isna().all():
            pdata = get_json(f"{M_BASE}/api/stock/{code}/price?pageSize={need}&page=1", retry=2)
            plst = find_list(pdata) if pdata is not None else []
            if plst:
                pdf = rows_to_df(plst)[["date", "close", "vol"]]
                df = df[["date", "inst"]].merge(pdf, on="date", how="inner")
        if len(df) >= need:
            return finalize_value(df.tail(need).reset_index(drop=True))
    return None


def get_history(code, need):
    """종가 이력(오래된 날짜 -> 최신) 최소 need일치. 후보 종목에만 사용. 실패 시 None"""
    if DATA_SOURCE == "krx":
        return get_history_krx(code, need)
    if PRIMARY["src"] == "krx" and DATA_SOURCE != "naver":
        first = get_history_krx(code, need)
        if first is not None and len(first) >= need:
            return first
    rows = []
    df = None
    for page in (1, 2, 3):
        data = get_json(f"{M_BASE}/api/stock/{code}/price?pageSize=60&page={page}", retry=2)
        lst = find_list(data) if data is not None else []
        if not lst:
            break
        rows += lst
        df = rows_to_df(rows)
        df = df[df["close"].notna()]
        if len(df) >= need:
            break
    if df is None or len(df) < need:
        data = get_json(f"{M_BASE}/api/stock/{code}/trend?pageSize={need}&page=1", retry=2)
        lst = find_list(data) if data is not None else []
        if lst:
            df2 = rows_to_df(lst)
            df2 = df2[df2["close"].notna()]
            if df is None or len(df2) > len(df):
                df = df2
    if (df is None or len(df) < need) and DATA_SOURCE != "naver":
        alt = get_history_krx(code, need)  # 네이버가 부족하면 KRX로 보충
        if alt is not None and (df is None or len(alt) > len(df)):
            df = alt
    if df is None or df.empty:
        return None
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------- KRX (한국거래소) 백업 출처
def krx_cred_hint():
    if KRX_ID and KRX_PW:
        return ""
    return " ※ KRX_ID/KRX_PW Secrets 가 비어 있습니다 (GitHub Settings > Secrets and variables > Actions 에서 등록)"


def krx_import():
    try:
        from pykrx import stock
        return stock
    except Exception as e:
        LAST_INFO["msg"] = f"pykrx 불러오기 실패: {type(e).__name__}: {str(e)[:120]}"
        return None


def krx_ohlcv(stock, d, m):
    """하루치 시장 전체 종가/거래량 (index=종목코드)"""
    for fn in ("get_market_ohlcv_by_ticker", "get_market_ohlcv"):
        f = getattr(stock, fn, None)
        if f is None:
            continue
        try:
            df = f(d, market=m)
            if df is not None and not df.empty and "종가" in df.columns:
                return df
        except Exception as e:
            LAST_INFO["msg"] = f"KRX {fn} 오류 {type(e).__name__}: {str(e)[:120]}"
    return None


def krx_net(stock, d, m):
    """하루치 시장 전체 기관 순매수량 (index=종목코드)"""
    try:
        df = stock.get_market_net_purchases_of_equities(d, d, m, "기관합계")
    except Exception as e:
        LAST_INFO["msg"] = f"KRX 기관 순매수 오류 {type(e).__name__}: {str(e)[:120]}"
        return None
    if df is None or df.empty:
        return None
    col = next((c for c in df.columns if "순매수" in str(c) and "거래량" in str(c)), None)
    if col is None:
        LAST_INFO["msg"] = f"KRX 순매수 항목을 못 찾음: {list(df.columns)[:8]}"
        return None
    return pd.to_numeric(df[col], errors="coerce")


def krx_net_both(stock, d, m, investor="기관합계"):
    """하루치 시장 전체 순매수 (수량 Series, 거래대금(원) Series). 실패하면 (None, None), 거래대금 열이 없으면 (수량, None)"""
    try:
        df = stock.get_market_net_purchases_of_equities(d, d, m, investor)
    except Exception as e:
        LAST_INFO["msg"] = f"KRX 기관 순매수 오류 {type(e).__name__}: {str(e)[:120]}"
        return None, None
    if df is None or df.empty:
        return None, None
    qcol = next((c for c in df.columns if "순매수" in str(c) and "거래량" in str(c)), None)
    vcol = next((c for c in df.columns if "순매수" in str(c) and "거래대금" in str(c)), None)
    if qcol is None:
        LAST_INFO["msg"] = f"KRX 순매수 항목을 못 찾음: {list(df.columns)[:8]}"
        return None, None
    q = pd.to_numeric(df[qcol], errors="coerce")
    v = pd.to_numeric(df[vcol], errors="coerce") if vcol else None
    return q, v


def krx_net_df(stock, d, m):
    """하루치 시장 전체 기관 순매수 표와 '순매수 거래대금' 열 이름 -> (df, col) 또는 (None, None)"""
    try:
        df = stock.get_market_net_purchases_of_equities(d, d, m, "기관합계")
    except Exception as e:
        LAST_INFO["msg"] = f"KRX 기관 순매수 오류 {type(e).__name__}: {str(e)[:120]}"
        return None, None
    if df is None or df.empty:
        return None, None
    col = next((c for c in df.columns if "순매수" in str(c) and "거래대금" in str(c)), None)
    return (df, col) if col else (None, None)


def krx_collect(stock):
    """시장 전체를 날짜별로 받아 종목별 21거래일 DataFrame 으로 정리 -> {코드: df}"""
    today = dt.datetime.now(KST).date()
    start = today - dt.timedelta(days=60)
    ref = stock.get_market_ohlcv_by_date(start.strftime("%Y%m%d"),
                                         today.strftime("%Y%m%d"), "005930")
    dates = [d.strftime("%Y%m%d") for d in ref.index]
    need = WINDOW + 1
    if len(dates) < need:
        LAST_INFO["msg"] = LAST_INFO["msg"] or f"KRX 거래일 목록이 부족함 ({len(dates)}일)"
        return None

    markets = ("KOSPI", "KOSDAQ")
    panels = {}  # 날짜 -> DataFrame(index=종목코드, close/vol/inst/val)
    for d in reversed(dates):  # 최신 날짜부터
        frames = []
        for m in markets:
            oh = krx_ohlcv(stock, d, m)
            if oh is None:
                continue
            net, net_val = krx_net_both(stock, d, m)
            f = pd.DataFrame({"close": pd.to_numeric(oh["종가"], errors="coerce"),
                              "vol": pd.to_numeric(oh["거래량"], errors="coerce")})
            f["val"] = (pd.to_numeric(oh["거래대금"], errors="coerce")
                        if "거래대금" in oh.columns else float("nan"))
            f["inst"] = net.reindex(f.index).fillna(0) if net is not None else float("nan")
            f["inst_val"] = net_val.reindex(f.index).fillna(0) if net_val is not None else float("nan")
            f["mkt"] = m
            frames.append(f)
        panel = pd.concat(frames) if frames else None
        valid = panel is not None and not panel.empty and panel["inst"].notna().any()
        if not valid:
            if panels:  # 중간 날짜가 비면 이어 붙일 수 없음
                LAST_INFO["msg"] = LAST_INFO["msg"] or f"KRX {d} 자료가 비어 있음"
                return None
            continue    # 최신 날짜 수급이 아직 없으면 하루 전부터 사용
        panels[d] = panel
        if len(panels) >= need:
            break
    if len(panels) < need:
        LAST_INFO["msg"] = LAST_INFO["msg"] or f"KRX 자료가 부족함 ({len(panels)}일)"
        return None

    ds = sorted(panels)  # 오래된 -> 최신
    latest = ds[-1]
    # 시가총액 상위 종목 선택
    universe = []
    for m in markets:
        codes = None
        try:
            cap = stock.get_market_cap_by_ticker(latest, market=m)
            if cap is not None and not cap.empty and "시가총액" in cap.columns:
                codes = list(cap.sort_values("시가총액", ascending=False).index[:PER_MARKET_COUNT])
                for c_, v_ in cap["시가총액"].items():
                    CAPS_EOK[str(c_)] = float(v_) / 1e8  # 원 -> 억원
        except Exception as e:
            LAST_INFO["msg"] = f"KRX 시가총액 조회 오류 {type(e).__name__}: {str(e)[:100]}"
        if codes is None:  # 시가총액을 못 받으면 거래대금 순으로 대체
            sub = panels[latest]
            sub = sub[sub["mkt"] == m].sort_values("val", ascending=False)
            codes = list(sub.index[:PER_MARKET_COUNT])
        universe += codes

    close_df = pd.DataFrame({d: panels[d]["close"] for d in ds})
    vol_df = pd.DataFrame({d: panels[d]["vol"] for d in ds})
    inst_df = pd.DataFrame({d: panels[d]["inst"] for d in ds})
    val_df = pd.DataFrame({d: panels[d]["val"] for d in ds})

    # TOP 40 용: 시장 전체의 '기관 순매수 거래대금(실제 체결금액)' 5일 합계 (억원)
    KRX_PANELS.clear()
    TOP_AMT_EOK.clear()
    KRX_PANELS.update({"ds": ds, "close": close_df, "vol": vol_df, "inst": inst_df})
    try:
        iv_df = pd.DataFrame({d: panels[d]["inst_val"] for d in ds})
        last = iv_df[ds[-TOP_DAYS:]]
        ok = last.notna().all(axis=1)
        for code, v in (last.sum(axis=1)[ok] / 1e8).items():
            TOP_AMT_EOK[str(code)] = float(v)
    except Exception as e:
        print("KRX 순매수 거래대금 집계 실패:", type(e).__name__, e)
    krx_collect_pf(stock, ds, markets)   # 연기금 TOP 40 용 (KRX 출처에서만)
    inst_val_df = pd.DataFrame({d: panels[d]["inst_val"] for d in ds})  # 기관 순매수 거래대금(원), 실제 체결금액
    out = {}
    for code in dict.fromkeys(universe):
        if code not in close_df.index:
            continue
        out[code] = finalize_value(pd.DataFrame({
            "date": ds, "close": close_df.loc[code].values, "vol": vol_df.loc[code].values,
            "inst": inst_df.loc[code].values, "val": val_df.loc[code].values,
            "inst_val": inst_val_df.loc[code].values}))
    return out


def krx_collect_pf(stock, ds, markets):
    """연기금 일별 순매수 거래대금을 받아 5일 합계(억원)와 일별 표를 만든다. 실패해도 기관 TOP 40 등 다른 기능에는 영향 없음"""
    PF_AMT_EOK.clear()
    PF_PANELS.clear()
    PF_ERR["msg"] = ""
    try:
        investor, frames = None, {}
        for n_day, d in enumerate(ds):
            parts = []
            for m in markets:
                v = None
                for inv in ([investor] if investor else ["연기금", "연기금등"]):
                    _, v = krx_net_both(stock, d, m, inv)
                    if v is not None:
                        investor = inv
                        break
                if v is not None:
                    parts.append(v)
            if parts:
                frames[d] = pd.concat(parts)
            if n_day == 0 and not frames:   # 첫 날부터 못 받으면 투자자 이름이 맞지 않는 것 -> 바로 중단
                PF_ERR["msg"] = "연기금 순매수 거래대금을 받지 못함 / " + (LAST_INFO["msg"] or "응답 없음")
                return
        if len(frames) < len(ds):
            PF_ERR["msg"] = f"연기금 자료가 {len(frames)}/{len(ds)}일만 조회됨 / " + LAST_INFO["msg"]
            return
        pf_df = pd.DataFrame({d: frames[d] for d in ds}).fillna(0)
        last5 = pf_df[ds[-TOP_DAYS:]]
        for code, v in (last5.sum(axis=1) / 1e8).items():
            PF_AMT_EOK[str(code)] = float(v)
        PF_PANELS["val"] = pf_df
        PF_PANELS["ds"] = ds
    except Exception as e:
        PF_AMT_EOK.clear()
        PF_PANELS.clear()
        PF_ERR["msg"] = f"{type(e).__name__}: {str(e)[:120]}"


def krx_pf_rows(limit=80):
    """연기금 5일 순매수 상위 종목의 21거래일 자료 -> {코드: (코드, df)} (inst 열 = 연기금 일별 순매수 거래대금)"""
    P, Q = KRX_PANELS, PF_PANELS
    if not P or not Q or not PF_AMT_EOK:
        return None
    codes = sorted(PF_AMT_EOK, key=PF_AMT_EOK.get, reverse=True)[:limit]
    rows = {}
    for code in codes:
        if code in P["close"].index and code in Q["val"].index:
            rows[code] = (code, pd.DataFrame({
                "date": P["ds"], "close": P["close"].loc[code].values, "vol": P["vol"].loc[code].values,
                "inst": Q["val"].loc[code].reindex(P["ds"]).fillna(0).values}))
    return rows or None


def krx_top_rows(limit=80):
    """KRX 실제 순매수 거래대금 상위 종목의 21거래일 자료 -> {코드: (코드, df)}. 없으면 None"""
    P = KRX_PANELS
    if not P or not TOP_AMT_EOK:
        return None
    codes = sorted(TOP_AMT_EOK, key=TOP_AMT_EOK.get, reverse=True)[:limit]
    rows = {}
    for code in codes:
        if code in P["close"].index:
            rows[code] = (code, pd.DataFrame({
                "date": P["ds"], "close": P["close"].loc[code].values,
                "vol": P["vol"].loc[code].values, "inst": P["inst"].loc[code].values}))
    return rows or None


def krx_name(stock, code):
    try:
        n = stock.get_market_ticker_name(code)
        return str(n) if isinstance(n, str) and n else code
    except Exception:
        return code


def get_history_krx(code, need):
    """KRX 종가 이력 (후보 종목 60일선/성과 계산용). 실패 시 None"""
    stock = krx_import()
    if stock is None:
        return None
    try:
        today = dt.datetime.now(KST).date()
        start = today - dt.timedelta(days=int(need * 1.6) + 15)
        df = stock.get_market_ohlcv_by_date(start.strftime("%Y%m%d"),
                                            today.strftime("%Y%m%d"), code)
        if df is None or df.empty or "종가" not in df.columns:
            return None
        out = pd.DataFrame({"date": [d.strftime("%Y%m%d") for d in df.index],
                            "close": pd.to_numeric(df["종가"], errors="coerce").values,
                            "vol": pd.to_numeric(df["거래량"], errors="coerce").values
                            if "거래량" in df.columns else float("nan"),
                            "inst": float("nan")})
        out = out[out["close"].notna() & (out["close"] > 0)]
        return out.sort_values("date").reset_index(drop=True)
    except Exception as e:
        LAST_INFO["msg"] = f"KRX 이력 조회 오류 {type(e).__name__}: {str(e)[:100]}"
        return None


# ---------------------------------------------------------------- 조건 판단
def longest_streak(flags):
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def analyze(code, name):
    return analyze_rows(code, name, get_rows(code))


def basis_metrics(series21):
    """한 기준(거래량 또는 거래대금)의 조건 지표: 후반/전반 증가율(%), 연속 증가 일수, 급증일 표시"""
    s20 = series21.tail(WINDOW).reset_index(drop=True)
    first, last = s20.iloc[:10].mean(), s20.iloc[10:].mean()
    ratio = (last / first - 1) * 100 if first > 0 else float("nan")
    streak = longest_streak((series21.diff().iloc[1:] > 0).tolist())
    mask = (s20 >= s20.mean() * COND3_MULT).values
    return {"ratio": ratio, "streak": streak, "spikes": int(mask.sum()), "mask": mask}


def basis_hits(m):
    hits = []
    if m["ratio"] == m["ratio"] and m["ratio"] >= COND1_RATIO * 100:
        hits.append(1)
    if m["streak"] >= COND2_STREAK:
        hits.append(2)
    if m["spikes"] >= COND3_COUNT:
        hits.append(3)
    return hits


def inst_streak_of(inst_series):
    """가장 최근 날부터 같은 방향(순매수/순매도)이 이어진 일수 -> (방향 +1/-1/0, 일수, 전체 일수)"""
    vals = [x for x in inst_series.tolist()][::-1]
    if not vals or vals[0] != vals[0] or vals[0] == 0:
        return 0, 0, len(vals)
    sign = 1 if vals[0] > 0 else -1
    n = 0
    for x in vals:
        if x == x and x != 0 and (x > 0) == (sign > 0):
            n += 1
        else:
            break
    return sign, n, len(vals)


def inst_streak_text(r):
    n, sign, days = r.get("inst_streak", 0), r.get("inst_sign", 0), r.get("inst_days", 0)
    if not sign or not n:
        return "보합"
    plus = "+" if n >= days else ""
    return f"{n}{plus}일 {'매수' if sign > 0 else '매도'}"


def analyze_rows(code, name, d21):
    """반환: (결과dict 또는 None, 데이터상태 'ok'/'nodata', 기준일)"""
    if d21 is None:
        return None, "nodata", None
    d20 = d21.tail(WINDOW).reset_index(drop=True)
    last_date = d20["date"].iloc[-1]
    last_date = f"{last_date[:4]}-{last_date[4:6]}-{last_date[6:]}"

    # 자료 확인 (종가·거래량·거래대금이 없거나 0이면 제외)
    if (d21["vol"].isna().any() or (d21["vol"] <= 0).any() or d21["close"].isna().any()
            or d21["val"].isna().any() or (d21["val"] <= 0).any()):
        return None, "nodata", last_date

    # 박스권 (종가 기준)
    hi, lo = d20["close"].max(), d20["close"].min()
    if lo <= 0 or (hi - lo) / lo > BOX_MAX_RANGE:
        return None, "ok", last_date

    # 유동성 필터: 20일 평균 일 거래대금(억원)이 기준 미만이면 제외
    avg_val = float(d20["val"].mean()) / 1e8
    if MIN_AVG_VALUE_EOK > 0 and avg_val < MIN_AVG_VALUE_EOK:
        return None, "ok", last_date

    # 거래량·거래대금 두 기준을 각각 계산 (판정에 쓰는 기준은 BASIS_MODE 로 선택)
    mv, ma_ = basis_metrics(d21["vol"]), basis_metrics(d21["val"])
    hv = basis_hits(mv) if BASIS_ON["vol"] else []
    ha = basis_hits(ma_) if BASIS_ON["val"] else []
    hits = sorted(set(hv + ha))
    if not hits:
        return None, "ok", last_date
    n_best = max(len(hv), len(ha))  # 한 기준에서 충족한 조건 수 (🔴🟠 표시에 사용)

    # 기관 수급
    inst_sum = d20["inst"].sum()
    inst_amt = (d20["inst"] * d20["close"]).sum() / 1e8  # 억원(추정: 순매수 수량 x 종가)
    inst_amt_est, inst_act = inst_amt, False
    if "inst_val" in d20.columns and d20["inst_val"].notna().all():
        inst_amt, inst_act = float(d20["inst_val"].sum()) / 1e8, True  # 억원(KRX 실제 체결금액)
    if USE_INSTITUTION:
        if d20["inst"].isna().any():
            return None, "nodata", last_date
        if (inst_amt if inst_act else inst_sum) <= 0:
            return None, "ok", last_date

    # 급증일(두 기준 중 하나라도 급증)의 가격 방향
    mask = np.zeros(WINDOW, dtype=bool)
    if BASIS_ON["vol"]:
        mask |= mv["mask"]
    if BASIS_ON["val"]:
        mask |= ma_["mask"]
    spikes = int(mask.sum())
    closes21 = d21["close"].values
    up = dn = 0
    for j in range(WINDOW):
        if mask[j]:
            if closes21[j + 1] > closes21[j]:
                up += 1
            elif closes21[j + 1] < closes21[j]:
                dn += 1
    if spikes == 0:
        spike_dir = "없음"
    elif up > dn:
        spike_dir = "매수세 우위"
    elif dn > up:
        spike_dir = "매도세 우위"
    else:
        spike_dir = "중립"

    close = float(d20["close"].iloc[-1])
    box_pos = 50.0 if hi == lo else (close - lo) / (hi - lo) * 100
    prim = ma_ if BASIS_ON["val"] else mv  # 글자 메시지에서 대표로 보여줄 기준
    i_sign, i_n, i_days = inst_streak_of(d21["inst"])

    return {
        "code": code, "name": name, "hits": hits, "date": last_date,
        "hits_vol": hv, "hits_val": ha, "n_best": n_best,
        "m": {"vol": {k: v for k, v in mv.items() if k != "mask"},
              "val": {k: v for k, v in ma_.items() if k != "mask"}},
        "box": (hi - lo) / lo * 100, "box_pos": box_pos, "box_hi": float(hi), "box_lo": float(lo),
        "avg_val": avg_val, "avg_vol": float(d20["vol"].mean()),
        "ratio": prim["ratio"], "streak": prim["streak"], "spikes": spikes,
        "spike_up": up, "spike_dn": dn, "spike_dir": spike_dir,
        "close": int(close),
        "inst_qty": 0 if pd.isna(inst_sum) else inst_sum,
        "inst_amt": 0 if pd.isna(inst_amt) else inst_amt,
        "inst_amt_est": 0 if pd.isna(inst_amt_est) else inst_amt_est, "inst_amt_act": inst_act,
        "inst_sign": i_sign, "inst_streak": i_n, "inst_days": i_days,
        "ma_label": None, "ma_chg": None, "ma_up_days": None,
        "ma_dn_days": None, "ma_strict": "", "above_ma": None,
        "ma_gap": None, "pullback": False,
    }, "ok", last_date


# ---------------------------------------------------------------- 60일선 분석 (후보 종목만)
def enrich_ma(r):
    """60일선 방향(단순: 20일 변화율 / 엄격: 20일 중 상승일 수)과 종가 위치"""
    need = MA_PERIOD + WINDOW  # 80일치
    df = get_history(r["code"], need)
    if df is None or len(df) < MA_PERIOD:
        r["ma_label"] = "부족"
        return
    close = df["close"].reset_index(drop=True)
    ma = close.rolling(MA_PERIOD).mean()
    r["above_ma"] = bool(close.iloc[-1] >= ma.iloc[-1])
    r["ma_gap"] = float((close.iloc[-1] / ma.iloc[-1] - 1) * 100)  # 종가의 60일선 대비 거리(%)
    if len(df) < need:
        r["ma_label"] = "부족"
        return
    chg = (ma.iloc[-1] / ma.iloc[-(WINDOW + 1)] - 1) * 100
    diffs = ma.diff().iloc[-WINDOW:]
    up_days, dn_days = int((diffs > 0).sum()), int((diffs < 0).sum())
    r["ma_chg"], r["ma_up_days"], r["ma_dn_days"] = chg, up_days, dn_days
    if chg >= MA_FLAT_PCT:
        r["ma_label"] = "상승"
    elif chg <= -MA_FLAT_PCT:
        r["ma_label"] = "하락"
    else:
        r["ma_label"] = "횡보"
    if up_days >= MA_STRICT_DAYS:
        r["ma_strict"] = "up"
    elif dn_days >= MA_STRICT_DAYS:
        r["ma_strict"] = "down"


def is_pullback(r):
    """⭐ 눌림목 패턴: 60일선이 꾸준히 오르는 추세에서 종가가 60일선 근처(±3%)까지 내려와 있고,
    박스 하단이며, 거래량 급증일에는 주가가 오른 날이 많았던 종목"""
    gap = r.get("ma_gap")
    return bool(gap is not None
                and abs(gap) <= PULLBACK_MA_GAP
                and r["box_pos"] <= PULLBACK_BOX_MAX
                and r["spike_up"] >= PULLBACK_SPIKE_UP_MIN
                and r["ma_label"] == "상승"
                and r["ma_strict"] == "up")


# ---------------------------------------------------------------- 성과 기록
HIST_COLS = ["base_date", "code", "name", "conds", "n_conds", "close", "box_pos",
             "ma_label", "ma_chg", "ma_strict", "above_ma", "ma_gap", "spike_dir",
             "pullback2", "basis", "conds_vol", "conds_val", "avg_val", "avg_vol", "box_hi", "box_lo",
             "inst_streak", "inst_amt", "inst_src", "inst_amt_est", "r5", "r10", "r20"]
NUM_COLS = ["n_conds", "close", "box_pos", "ma_chg", "ma_gap", "avg_val", "avg_vol", "box_hi",
            "box_lo", "inst_streak", "inst_amt", "inst_amt_est", "r5", "r10", "r20"]


def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            df = pd.read_csv(HISTORY_PATH, encoding="utf-8-sig", dtype=str)
        except Exception as e:
            print("성과 기록 읽기 실패:", e)
            df = pd.DataFrame(columns=HIST_COLS)
    else:
        df = pd.DataFrame(columns=HIST_COLS)
    for c in HIST_COLS:
        if c not in df.columns:
            df[c] = ""
    df = df[HIST_COLS].copy()
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ("base_date", "code", "name", "conds", "ma_label", "ma_strict",
              "above_ma", "spike_dir", "pullback2", "basis", "conds_vol", "conds_val", "inst_src"):
        df[c] = df[c].fillna("").astype(str)
    # 이전(거래량 기준) 기록은 '거래량 기준으로 통과한 것'으로 간주해서 비교에 포함
    old = df["basis"] == ""
    df.loc[old, "conds_vol"] = df.loc[old, "conds"]
    df.loc[old, "basis"] = "volume"
    return df


def save_history(df):
    os.makedirs(os.path.dirname(HISTORY_PATH) or ".", exist_ok=True)
    df.to_csv(HISTORY_PATH, index=False, encoding="utf-8-sig")


def record_history(df, results):
    """오늘 알림 종목을 기록 (같은 기준일+종목은 한 번만)"""
    seen = set(zip(df["base_date"], df["code"]))
    rows = []
    for r in results:
        key = (r["date"], r["code"])
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "base_date": r["date"], "code": r["code"], "name": r["name"],
            "conds": ",".join(map(str, r["hits"])), "n_conds": r.get("n_best", len(r["hits"])),
            "basis": BASIS_MODE, "conds_vol": ",".join(map(str, r.get("hits_vol", []))),
            "conds_val": ",".join(map(str, r.get("hits_val", []))),
            "avg_val": round(r.get("avg_val", float("nan")), 1), "avg_vol": round(r.get("avg_vol", float("nan"))),
            "box_hi": round(r.get("box_hi", float("nan")), 1), "box_lo": round(r.get("box_lo", float("nan")), 1),
            "inst_streak": r.get("inst_sign", 0) * r.get("inst_streak", 0),
            "close": r["close"], "box_pos": round(r["box_pos"], 1),
            "ma_label": r["ma_label"] or "", "ma_chg": (round(r["ma_chg"], 2)
                                                        if r["ma_chg"] is not None else None),
            "ma_strict": r["ma_strict"], "above_ma": "" if r["above_ma"] is None
            else ("Y" if r["above_ma"] else "N"),
            "ma_gap": (round(r["ma_gap"], 2) if r.get("ma_gap") is not None else None),
            "spike_dir": r["spike_dir"], "pullback2": "Y" if r.get("pullback") else "N",
            "inst_amt": round(r["inst_amt"], 1),
            "inst_src": "KRX실제" if r.get("inst_amt_act") else "추정",
            "inst_amt_est": round(r.get("inst_amt_est", r["inst_amt"]), 1),
            "r5": None, "r10": None, "r20": None,
        })
    if rows:
        df = pd.concat([df, pd.DataFrame(rows, columns=HIST_COLS)], ignore_index=True)
        for c in NUM_COLS:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def update_returns(df):
    """알림일 종가 대비 5/10/20거래일 뒤 수익률을 채움 (아직 비어 있는 것만)"""
    today = dt.datetime.now(KST).date()
    pending = []
    for idx, row in df.iterrows():
        if not pd.isna(row["r20"]):
            continue
        try:
            age = (today - dt.date.fromisoformat(row["base_date"])).days
        except Exception:
            continue
        if 7 <= age <= 60:
            pending.append(idx)
    if not pending:
        return df
    for code in sorted(set(df.loc[pending, "code"])):
        hist = get_history(code, 70)
        if hist is None:
            continue
        dates, closes = list(hist["date"]), list(hist["close"])
        for idx in [i for i in pending if df.at[i, "code"] == code]:
            bd = df.at[idx, "base_date"].replace("-", "")
            if bd not in dates:
                continue
            i0 = dates.index(bd)
            base = closes[i0]
            if not base or pd.isna(base):
                continue
            for n, col in ((5, "r5"), (10, "r10"), (20, "r20")):
                if pd.isna(df.at[idx, col]) and i0 + n < len(closes):
                    df.at[idx, col] = round((closes[i0 + n] / base - 1) * 100, 2)
    return df


def stats_line(label, sub):
    parts = []
    for col, nm in (("r5", "5일"), ("r10", "10일"), ("r20", "20일")):
        s = sub[col].dropna()
        if len(s) < MIN_SAMPLE:
            parts.append(f"{nm} 표본부족({len(s)})")
        else:
            parts.append(f"{nm} {s.mean():+.1f}% (승률 {(s > 0).mean() * 100:.0f}%, n={len(s)})")
    return f"<b>{esc(label)}</b>\n   " + " | ".join(parts)


def build_performance_message(df):
    ev = df[df["r5"].notna()]
    if ev.empty:
        return None

    def has_cond(k):
        return df["conds"].apply(lambda s: str(k) in str(s).split(","))

    g = df["ma_gap"]  # 종가의 60일선 대비 거리(%)

    def has_basis(col, k=None):
        if k is None:
            return df[col].apply(lambda s: str(s).strip() != "")
        return df[col].apply(lambda s: str(k) in str(s).split(","))

    v_any, a_any = has_basis("conds_vol"), has_basis("conds_val")
    groups = [
        ("【기준 비교】 거래량 기준 통과", df[v_any]),
        ("【기준 비교】 거래대금 기준 통과", df[a_any]),
        ("【기준 비교】 두 기준 모두 통과", df[v_any & a_any]),
        ("【기준 비교】 거래량만 통과", df[v_any & ~a_any]),
        ("【기준 비교】 거래대금만 통과", df[a_any & ~v_any]),
        ("조건1 · 거래량 기준", df[has_basis("conds_vol", 1)]),
        ("조건1 · 거래대금 기준", df[has_basis("conds_val", 1)]),
        ("조건2 · 거래량 기준", df[has_basis("conds_vol", 2)]),
        ("조건2 · 거래대금 기준", df[has_basis("conds_val", 2)]),
        ("조건3 · 거래량 기준", df[has_basis("conds_vol", 3)]),
        ("조건3 · 거래대금 기준", df[has_basis("conds_val", 3)]),
        ("전체 알림 종목", df),
        ("조건1", df[has_cond(1)]), ("조건2", df[has_cond(2)]), ("조건3", df[has_cond(3)]),
        ("🔴 3개 조건 충족", df[df["n_conds"] >= 3]),
        ("🟠 2개 조건 충족", df[df["n_conds"] == 2]),
        ("60일선 상승", df[df["ma_label"] == "상승"]),
        ("60일선 횡보", df[df["ma_label"] == "횡보"]),
        ("60일선 하락", df[df["ma_label"] == "하락"]),
        ("60일선 엄격 상승", df[df["ma_strict"] == "up"]),
        ("급증일 매수세 우위", df[df["spike_dir"] == "매수세 우위"]),
        ("급증일 매도세 우위", df[df["spike_dir"] == "매도세 우위"]),
        ("박스 상단(67%↑)", df[df["box_pos"] >= 67]),
        ("박스 하단(33%↓)", df[df["box_pos"] <= 33]),
        ("⭐ 눌림목 패턴 (60일선 ±3%)", df[df["pullback2"] == "Y"]),
        ("눌림목 아닌 종목 (비교용)", df[df["pullback2"] == "N"]),
        ("종가 60일선 대비 -10% 이하", df[g <= -10]),
        ("종가 60일선 대비 -10~-3%", df[(g > -10) & (g <= -3)]),
        ("종가 60일선 대비 -3~0%", df[(g > -3) & (g <= 0)]),
        ("종가 60일선 대비 0~+3%", df[(g > 0) & (g <= 3)]),
        ("종가 60일선 대비 +3~+10%", df[(g > 3) & (g <= 10)]),
        ("종가 60일선 대비 +10% 초과", df[g > 10]),
    ]
    lines = ["📈 <b>성과 기록</b> | 과거 알림 종목의 이후 수익률",
             f"누적 기록 {len(df)}건 / 5일 경과 {len(ev)}건",
             "알림 기준일 종가 대비 · 수수료/세금 미반영",
             "━━━━━━━━━━━━━━"]
    for label, sub in groups:
        lines.append(stats_line(label, sub))
    lines.append("")
    lines.append("※ 표본이 적으면 우연일 수 있습니다. 참고용이며 매수 추천이 아닙니다.")
    return "\n".join(lines)


# ---------------------------------------------------------------- 텔레그램
def tg_send(text, use_html=False):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[오류] 텔레그램 토큰/채팅ID(Secrets)가 설정되지 않았습니다.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True}
    if use_html:
        data["parse_mode"] = "HTML"
    try:
        r = requests.post(url, data=data, timeout=15)
        if r.status_code == 200:
            print("텔레그램 전송 성공")
        else:
            print("텔레그램 전송 실패:", r.status_code, r.text)
    except Exception as e:
        print("텔레그램 전송 실패:", e)
    time.sleep(1)


def tg_send_long(text, use_html=False):
    """줄 단위로 4000자 이하로 나눠 전송"""
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800 and chunk:
            tg_send(chunk, use_html=use_html)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        tg_send(chunk, use_html=use_html)


def send_plain(text):
    """오류/안내용 일반 텍스트 메시지"""
    tg_send(text[:3800], use_html=False)


def send_blocks(header, blocks, footer=""):
    """제목 + 종목 블록들을 4000자 이하로 나눠 전송"""
    chunks, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + 2 > 3500:
            chunks.append(cur)
            cur = "(이어서)\n"
        cur += "\n" + b
    if footer:
        cur += "\n\n" + footer
    chunks.append(cur)
    for c in chunks:
        tg_send(c, use_html=True)


def esc(s):
    return htmllib.escape(str(s))


COND_INFO = {
    1: ("🟢", "조건1", f"후반10일 평균 {BASIS_TXT}이 전반10일보다 {COND1_RATIO*100:.0f}% 이상 증가"),
    2: ("🟡", "조건2", f"{BASIS_TXT}이 전일 대비 끊기지 않고 연속 {COND2_STREAK}일 이상 증가"),
    3: ("🔴", "조건3", f"20일 평균 {BASIS_TXT}의 {COND3_MULT:g}배 이상 터진 날 {COND3_COUNT}회 이상"),
}


def value_src_text():
    if VALUE_SRC["actual"] and VALUE_SRC["actual"] >= VALUE_SRC["est"]:
        return "거래소 제공 거래대금 사용"
    return "종가×거래량으로 추정 (거래소 제공값 없음)"


def tier(r):
    """동시 충족 조건 수에 따른 강조 단계: 3개=🔴, 2개=🟠, 1개=없음"""
    n = r.get("n_best", len(r["hits"]))
    if n >= 3:
        return "🔴", "3개 조건 모두 충족"
    if n == 2:
        return "🟠", "2개 조건 충족"
    return "", ""


def box_label(pos):
    return "하단" if pos < 33 else ("중단" if pos < 67 else "상단")


def metric_of(r, basis, k):
    """(표시 문자열, 통과 여부) - basis: 'vol' 또는 'val'"""
    m = r["m"][basis]
    on = BASIS_ON[basis]
    if k == 1:
        v = m["ratio"]
        return (f"{v:+.0f}%" if v == v else "–"), (on and v == v and v >= COND1_RATIO * 100)
    if k == 2:
        return f"{m['streak']}일", (on and m["streak"] >= COND2_STREAK)
    return f"{m['spikes']}회", (on and m["spikes"] >= COND3_COUNT)


def detail_line(k, r):
    label = {1: "증가율(후반/전반)", 2: "연속 증가", 3: "급증"}[k]
    parts = []
    for basis, nm in (("vol", "거래량"), ("val", "거래대금")):
        if not BASIS_ON[basis]:
            continue
        txt, ok = metric_of(r, basis, k)
        parts.append(f"{nm} <b>{txt}</b>{' ✅' if ok else ''}")
    extra = ""
    if k == 3:
        extra = f" (▲{r['spike_up']} ▼{r['spike_dn']} → <b>{r['spike_dir']}</b>)"
    return f"📈 {label}: " + " · ".join(parts) + extra


def spike_line(r):
    if r["spikes"] < 1:
        return ""
    return (f"📊 급증일 {r['spikes']}회: ▲{r['spike_up']} ▼{r['spike_dn']} "
            f"→ <b>{r['spike_dir']}</b>\n")


def ma_line(r):
    lab = r.get("ma_label")
    pos = ""
    if r.get("ma_gap") is not None:
        pos = f" · 종가 60일선 대비 <b>{r['ma_gap']:+.1f}%</b>"
    elif r.get("above_ma") is not None:
        pos = " · 종가 60일선 " + ("위" if r["above_ma"] else "아래")
    if lab in (None, "부족"):
        return f"📈 60일선 방향 판정 불가(데이터 부족){pos}\n"
    icon = {"상승": "📈", "횡보": "➖", "하락": "📉"}[lab]
    s = f"{icon} 60일선 <b>{lab}</b> ({r['ma_chg']:+.1f}%) · 20일 중 {r['ma_up_days']}일 상승"
    if r["ma_strict"] == "up":
        s += " ✅엄격 상승"
    elif r["ma_strict"] == "down":
        s += " ⚠엄격 하락"
    return s + pos + "\n"


def send_condition_messages(results, total, nodata, base_date, source_label=""):
    now = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    for k in (1, 2, 3):
        icon, title, desc = COND_INFO[k]
        items = [r for r in results if k in r["hits"]]
        items.sort(key=lambda r: r["inst_amt"], reverse=True)  # 기관 순매수 큰 순
        shown = items[:MAX_PER_CONDITION]
        n3 = sum(1 for r in items if len(r["hits"]) >= 3)
        n2 = sum(1 for r in items if len(r["hits"]) == 2)
        npb = sum(1 for r in items if r.get("pullback"))
        tier_txt = ""
        if n3 or n2 or npb:
            parts = ([f"🔴 3개충족 {n3}"] if n3 else []) + ([f"🟠 2개충족 {n2}"] if n2 else []) \
                + ([f"⭐ 눌림목 {npb}"] if npb else [])
            tier_txt = " (" + " · ".join(parts) + ")"

        header = (f"{icon} <b>{title}</b> | {esc(desc)}\n"
                  f"🕘 {now}  |  📅 기준일 {esc(base_date)}\n"
                  f"🗂 출처: {esc(source_label)}\n"
                  f"{fresh_line()}"
                  f"🔎 검사 {total}종목 → <b>해당 {len(items)}종목</b>"
                  f"{tier_txt}\n"
                  f"↕ 기관 순매수 큰 순 정렬\n"
                  f"━━━━━━━━━━━━━━")
        blocks = []
        for n, r in enumerate(shown, 1):
            icon_t, label_t = tier(r)
            mark = (f"{icon_t} " if icon_t else "") + ("⭐ " if r.get("pullback") else "")
            pb_line = (f"⭐ <b>눌림목 패턴</b> (박스 하단 · 급증일 ▲{r['spike_up']}일 · "
                       f"60일선 엄격 상승 · 종가 60일선 대비 {r['ma_gap']:+.1f}%)\n" if r.get("pullback") else "")
            strong_line = (f"{icon_t} <b>{label_t}: 조건{'·'.join(map(str, r['hits']))}</b>\n"
                           if icon_t else "")
            blocks.append(
                f"{mark}<b>{n}. {esc(r['name'])}</b> ({esc(r['code'])})\n"
                f"{strong_line}"
                f"{pb_line}"
                f"💰 {r['close']:,}원  ·  📦 박스폭 {r['box']:.1f}%  ·  💵 일평균 거래대금 {r['avg_val']:,.0f}억\n"
                f"🧭 박스 위치 {r['box_pos']:.0f}% ({box_label(r['box_pos'])})\n"
                f"{detail_line(k, r)}\n"
                f"{spike_line(r) if k != 3 else ''}"
                f"{ma_line(r)}"
                f"🏦 기관 20일 순매수 <b>{r['inst_amt']:+,.0f}억</b> ({r['inst_qty']:+,.0f}주) · 연속 {inst_streak_text(r)}\n"
                f"──────────────")
        if not blocks:
            blocks.append("해당 종목이 없습니다.")
        footer = []
        if len(items) > len(shown):
            footer.append(f"… 외 {len(items) - len(shown)}종목 (상위 {len(shown)}개만 표시)")
        if k == 3:
            footer.append("🔴 = 조건 3개 모두 충족 / 🟠 = 조건 2개 충족 (규칙 기반 신호)\n"
                          f"⭐ = 눌림목 패턴: 박스 하단({PULLBACK_BOX_MAX}%↓) + 급증일 ▲{PULLBACK_SPIKE_UP_MIN}일↑ "
                          f"+ 60일선 엄격 상승 + 종가 60일선 ±{PULLBACK_MA_GAP:g}% 이내\n"
                          "🧭 박스 위치: 0%=박스 하단, 100%=박스 상단 (종가 기준)\n"
                          "📊 급증일 방향: 급증일 종가가 전일보다 올랐으면 ▲, 내렸으면 ▼\n"
                          f"📈 60일선: 20일 변화율 ±{MA_FLAT_PCT:g}% 기준 / "
                          f"엄격=20일 중 {MA_STRICT_DAYS}일 이상 같은 방향")
            if nodata:
                footer.append(f"⚠ 자료 미흡/수집 실패 제외: {nodata}개")
            if NOTES_EXTRA["provisional"]:
                footer.append("※ " + NOTES_EXTRA["provisional"])
            footer.append(f"※ 판정 기준: {BASIS_TXT} (한쪽이라도 통과하면 표시) / 20일 평균 거래대금 "
                          f"{MIN_AVG_VALUE_EOK:g}억 미만 종목 제외 / ETF·레버리지·스팩·리츠 제외\n"
                          f"※ 거래대금: {value_src_text()}\n"
                          "※ 투자 판단과 책임은 본인에게 있습니다.")
        if COND_FORMAT in ("image", "both") and shown:
            caption_head = header.replace("\n━━━━━━━━━━━━━━", "")
            if send_condition_images(k, shown, len(items), total, nodata, base_date,
                                     source_label, caption_head, (n3, n2, npb)):
                if COND_FORMAT == "image":
                    continue
            else:
                print(f"조건{k} 표 이미지를 만들거나 보내지 못해 글자 메시지로 대체합니다.")
        send_blocks(header, blocks, "\n".join(footer))


# ---------------------------------------------------------------- 조건 1·2·3 표 이미지
def wrap_px(draw, text, font, max_w):
    """픽셀 너비 기준 줄바꿈"""
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


DRED, PALE_RED = (176, 24, 24), (255, 223, 223)
PALE_BLUE, PALE_GOLD, WHITE = (221, 233, 255), (255, 241, 190), (255, 255, 255)


def draw_pill(d, text, font, x, cy, fg, bg, align="center", padx=10, pady=5):
    """글자 뒤에 둥근 배경(알약)을 그려 강조"""
    w = d.textlength(text, font=font)
    x0 = x if align == "left" else (x - w if align == "right" else x - w / 2)
    half = font.size * 0.62 + pady
    d.rounded_rectangle((x0 - padx, cy - half, x0 + w + padx, cy + half), radius=half, fill=bg)
    d.text((x0, cy), text, font=font, fill=fg, anchor="lm")


def draw_star(d, cx, cy, r, fill):
    import math
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    d.polygon(pts, fill=fill)


def render_condition_image(k, chunk, start_rank, n_items, n_hidden, total, nodata, base_date, source_label, badges=None):
    """조건 k 의 종목 표(PNG 바이트). 통과·신호 부분을 알약/막대로 강하게 강조. 글꼴/Pillow 가 없으면 None"""
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        print("Pillow 불러오기 실패:", e)
        return None
    f_title, f_sub = load_font(30, True), load_font(19)
    f_head, f_body, f_bold = load_font(17), load_font(21), load_font(22, True)
    f_small, f_badge = load_font(15), load_font(18, True)
    if not all([f_title, f_sub, f_head, f_body, f_bold, f_small, f_badge]):
        print("한글 글꼴을 찾지 못했습니다.")
        return None

    RED, BLUE, INK, GRAY = (214, 40, 40), (40, 98, 214), (29, 29, 31), (165, 170, 178)
    ORANGE, GOLD = (245, 140, 30), (232, 176, 20)
    LINE, HEAD_BG, PINK = (232, 234, 238), (246, 247, 249), (253, 230, 230)
    ROWBG, ROWBG2, ROWGOLD = (255, 255, 255), (249, 250, 252), (255, 246, 214)
    W, ROW, TOP_H, HEAD_H = 1250, 52, 132, 70
    actual20 = bool(chunk) and all(r.get("inst_amt_act") for r in chunk)
    amt20_txt = ("기관 20일 = KRX 기관합계 순매수 거래대금(실제 체결금액)의 20일 합계" if actual20
                 else "기관 20일 = 기관 일별 순매수 수량×종가의 20일 합계(추정)")
    notes = [f"빨강 알약 = 해당 기준 통과, 회색 = 미통과 / 20일 평균 거래대금 {MIN_AVG_VALUE_EOK:g}억 미만 종목 제외 / "
             "박스 위치 0%=하단, 100%=상단",
             amt20_txt + ", 기관 연속 = 최근일부터 연속 순매수(빨강)·순매도(파랑) 일수 / ETF·레버리지·스팩·리츠 제외 / "
             "매수 추천이 아니며 투자 판단과 책임은 본인에게 있습니다"]
    if NOTES_EXTRA["provisional"]:
        notes.insert(1, NOTES_EXTRA["provisional"])
    if n_hidden > 0:
        notes.append(f"외 {n_hidden}종목은 표시하지 않았습니다 (상위 {MAX_PER_CONDITION}개까지 표시)")
    if nodata:
        notes.append(f"자료 미흡/수집 실패로 제외된 종목 {nodata}개")
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    foot_lines = []
    for t in notes:
        foot_lines += wrap_px(tmp, t, f_small, W - 48)
    H = TOP_H + HEAD_H + ROW * len(chunk) + 54 + 24 * len(foot_lines) + 14
    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)

    def tw(txt, font):
        return d.textlength(txt, font=font)

    def put(x, y, txt, font, color, align="left"):
        w = tw(txt, font)
        x0 = x if align == "left" else (x - w if align == "right" else x - w / 2)
        d.text((x0, y), txt, font=font, fill=color, anchor="lm")

    icon, title, desc = COND_INFO[k]
    # 제목 + 오른쪽 요약 배지 (3개 충족 / 2개 충족 / 눌림목 개수)
    d.rounded_rectangle((24, 22, 62, 60), radius=8, fill=(228, 236, 255))
    put(43, 41, str(k), f_bold, BLUE, "center")
    put(76, 41, f"조건{k}", f_title, INK)
    put(76 + tw(f"조건{k}", f_title) + 14, 45, desc, f_sub, GRAY)
    if badges:
        n3, n2, npb = badges
        x = W - 24
        for txt, cnt, bg, fg in ((f"눌림목 {npb}", npb, GOLD, INK), (f"2개 충족 {n2}", n2, ORANGE, WHITE),
                                 (f"3개 충족 {n3}", n3, RED, WHITE)):
            if cnt:
                w = tw(txt, f_badge)
                draw_pill(d, txt, f_badge, x - w - 12, 41, fg, bg, "left", padx=12, pady=5)
                x -= w + 24 + 10
    put(24, 84, f"기준일 {base_date}  ·  출처 {source_label}  ·  해당 {n_items}종목  ·  기관 순매수 큰 순", f_small, GRAY)
    if FRESH and FRESH.get("lag", 0) > 0:
        put(24, 110, f"※ 최신 수급 미반영: 예상 기준일 {FRESH['expected']}, 실제 {FRESH['base']} ({FRESH['lag']}거래일 전 자료)", f_small, RED)
    elif FRESH:
        put(24, 110, "최신 자료 (예상 기준일과 일치)", f_small, GRAY)

    # 열 위치 (9개 열)
    C_RANK, C_SIG, X_NAME = 40, 78, 104
    R_PRICE, R_INST, C_STK = 392, 504, 612
    R_VOL, R_VAL = 744, 854
    R_BOXP, C_MA, R_GAP = 968, 1080, 1226
    sub = {1: "증가율", 2: "연속 증가", 3: "급증 횟수"}[k]
    y0 = TOP_H
    d.rectangle((0, y0, W, y0 + HEAD_H), fill=HEAD_BG)
    d.rectangle((402, y0, R_INST + 20, y0 + HEAD_H), fill=PINK)
    hy = y0 + HEAD_H // 2

    def head2(x, a, b, align, color=GRAY):
        put(x, hy - 12, a, f_head, color, align)
        put(x, hy + 12, b, f_head, color, align)

    put(C_RANK, hy, "#", f_head, GRAY, "center")
    put(X_NAME, hy, "종목명", f_head, GRAY)
    head2(R_PRICE, "현재가", "(원)", "right")
    head2(R_INST, "기관 20일", "(억)", "right", RED)
    head2(C_STK, "기관 연속", "순매수", "center")
    head2(R_VOL, "거래량", sub, "right")
    head2(R_VAL, "거래대금", sub, "right")
    head2(R_BOXP, "박스 위치", "(0%=하단)", "right")
    head2(C_MA, "60일선", "(20일 변화)", "center")
    head2(R_GAP, "종가 vs", "60일선", "right")

    for i, r in enumerate(chunk):
        top = y0 + HEAD_H + i * ROW
        cy = top + ROW // 2
        n_best = r.get("n_best", len(r["hits"]))
        d.rectangle((0, top, W, top + ROW), fill=ROWGOLD if r.get("pullback") else (ROWBG if i % 2 == 0 else ROWBG2))
        d.rectangle((402, top, R_INST + 20, top + ROW), fill=PINK)
        d.line((0, top + ROW, W, top + ROW), fill=LINE, width=1)
        # 왼쪽 강조 막대: 빨강(3개 충족) / 주황(2개 충족), 눌림목은 금색 막대 추가
        bx = 0
        if n_best >= 3:
            d.rectangle((bx, top, bx + 9, top + ROW), fill=RED)
            bx += 11
        elif n_best == 2:
            d.rectangle((bx, top, bx + 9, top + ROW), fill=ORANGE)
            bx += 11
        if r.get("pullback"):
            d.rectangle((bx, top, bx + 9, top + ROW), fill=GOLD)

        put(C_RANK, cy, str(start_rank + i), f_body, GRAY, "center")
        # 신호 아이콘 (크게)
        marks = []
        if n_best >= 3:
            marks.append(("dot", RED))
        elif n_best == 2:
            marks.append(("dot", ORANGE))
        if r.get("pullback"):
            marks.append(("star", GOLD))
        for j, (kind, col) in enumerate(marks):
            cx = C_SIG + (j - (len(marks) - 1) / 2) * 28
            if kind == "dot":
                d.ellipse((cx - 10, cy - 10, cx + 10, cy + 10), fill=col)
            else:
                draw_star(d, cx, cy, 14, col)
        nm = r["name"]
        while tw(nm, f_bold) > 210 and len(nm) > 2:
            nm = nm[:-1]
        if nm != r["name"]:
            nm = nm[:-1] + "…"
        put(X_NAME, cy, nm, f_bold, INK)

        put(R_PRICE, cy, f"{r['close']:,}", f_body, INK, "right")
        put(R_INST, cy, f"{r['inst_amt']:+,.0f}", f_bold, DRED, "right")

        # 기관 연속: 알약 (10일 이상은 진한 면)
        sg, n_st = r.get("inst_sign", 0), r.get("inst_streak", 0)
        stxt = inst_streak_text(r)
        if sg > 0 and n_st:
            if n_st >= 10:
                draw_pill(d, stxt, f_bold, C_STK, cy, WHITE, RED, "center")
            else:
                draw_pill(d, stxt, f_bold, C_STK, cy, DRED, PALE_RED, "center")
        elif sg < 0 and n_st:
            if n_st >= 10:
                draw_pill(d, stxt, f_bold, C_STK, cy, WHITE, BLUE, "center")
            else:
                draw_pill(d, stxt, f_bold, C_STK, cy, BLUE, PALE_BLUE, "center")
        else:
            put(C_STK, cy, stxt, f_body, GRAY, "center")

        # 거래량·거래대금 지표: 통과한 기준은 빨강 알약, 미통과는 회색 글씨
        for x, basis in ((R_VOL, "vol"), (R_VAL, "val")):
            if not BASIS_ON[basis]:
                put(x, cy, "–", f_body, GRAY, "right")
                continue
            txt, ok = metric_of(r, basis, k)
            if ok:
                draw_pill(d, txt, f_bold, x, cy, WHITE, RED, "right", padx=11)
            else:
                put(x, cy, txt, f_body, GRAY, "right")

        # 박스 위치: 상단은 빨강, 하단은 파랑으로 굵게
        lab = box_label(r["box_pos"])
        col = RED if lab == "상단" else (BLUE if lab == "하단" else INK)
        put(R_BOXP, cy, f"{r['box_pos']:.0f}% {lab}", f_bold if lab != "중단" else f_body, col, "right")

        # 60일선: 엄격 상승·하락은 알약
        ml = r.get("ma_label")
        if ml in ("상승", "횡보", "하락"):
            mtxt = f"{ml} {r['ma_chg']:+.1f}%"
            if r.get("ma_strict") == "up":
                draw_pill(d, mtxt, f_bold, C_MA, cy, DRED, PALE_RED, "center")
            elif r.get("ma_strict") == "down":
                draw_pill(d, mtxt, f_bold, C_MA, cy, BLUE, PALE_BLUE, "center")
            else:
                put(C_MA, cy, mtxt, f_body, RED if ml == "상승" else (BLUE if ml == "하락" else GRAY), "center")
        else:
            put(C_MA, cy, "–", f_body, GRAY, "center")

        # 종가 vs 60일선: ±3% 이내(눌림목 구간)는 금색 알약
        gap = r.get("ma_gap")
        if gap is None:
            put(R_GAP, cy, "–", f_body, GRAY, "right")
        elif abs(gap) <= PULLBACK_MA_GAP:
            draw_pill(d, f"{gap:+.1f}%", f_bold, R_GAP, cy, INK, GOLD, "right", padx=9)
        else:
            put(R_GAP, cy, f"{gap:+.1f}%", f_body, RED if gap > 0 else BLUE, "right")

    # 범례 + 안내
    ly = y0 + HEAD_H + ROW * len(chunk) + 28
    x = 24
    d.ellipse((x, ly - 9, x + 18, ly + 9), fill=RED)
    put(x + 25, ly, "조건 3개 충족", f_small, INK)
    x += 25 + tw("조건 3개 충족", f_small) + 22
    d.ellipse((x, ly - 9, x + 18, ly + 9), fill=ORANGE)
    put(x + 25, ly, "조건 2개 충족", f_small, INK)
    x += 25 + tw("조건 2개 충족", f_small) + 22
    draw_star(d, x + 9, ly, 12, GOLD)
    put(x + 28, ly, "눌림목 (노란 줄): 박스 하단 · 급증일 상승 · 60일선 엄격 상승 · 종가 60일선 근처", f_small, INK)
    fy = ly + 30
    for j, line in enumerate(foot_lines):
        put(24, fy + 24 * j, line, f_small, GRAY)
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def send_condition_images(k, shown, n_items, total, nodata, base_date, source_label, caption_head, badges=None):
    """조건 k 표 이미지 전송 (20종목씩 나눔). 하나라도 실패하면 False"""
    parts = [shown[i:i + 20] for i in range(0, len(shown), 20)]
    for pi, chunk in enumerate(parts):
        png = render_condition_image(k, chunk, pi * 20 + 1, n_items, max(0, n_items - len(shown)),
                                     total, nodata, base_date, source_label, badges)
        if png is None:
            return False
        caption = caption_head + (f"\n({pi + 1}/{len(parts)})" if len(parts) > 1 else "")
        if not tg_send_photo(png, caption[:1000]):
            return False
    return True


# ---------------------------------------------------------------- TOP 40 표 이미지
FONT_REG = ["/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "C:/Windows/Fonts/malgun.ttf", "/System/Library/Fonts/AppleSDGothicNeo.ttc"]
FONT_BOLD = ["/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
             "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf",
             "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
             "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
             "C:/Windows/Fonts/malgunbd.ttf", "/System/Library/Fonts/AppleSDGothicNeo.ttc"]


def load_font(size, bold=False):
    from PIL import ImageFont
    for path in (FONT_BOLD if bold else FONT_REG):
        if not os.path.exists(path):
            continue
        for idx in ((1, 0, 2) if path.endswith(".ttc") else (0,)):  # .ttc 는 한국어(KR) 서체 우선
            try:
                return ImageFont.truetype(path, size, index=idx)
            except Exception:
                continue
    return None


def render_top40_image(chunk, start_rank, caps, base_date, source_label, actual=False, cmp_txt=""):
    """표 이미지(PNG 바이트) 생성. 글꼴/Pillow 가 없으면 None"""
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        print("Pillow 불러오기 실패:", e)
        return None
    f_title, f_sub = load_font(30, True), load_font(19)
    f_head, f_body, f_bold = load_font(17), load_font(21), load_font(21, True)
    f_small = load_font(16)
    if not all([f_title, f_sub, f_head, f_body, f_bold, f_small]):
        print("한글 글꼴을 찾지 못했습니다.")
        return None

    RED, BLUE, INK, GRAY = (217, 58, 58), (47, 111, 214), (29, 29, 31), (138, 143, 152)
    LINE, HEAD_BG, PINK = (232, 234, 238), (246, 247, 249), (253, 238, 238)
    W, ROW, TOP_H, HEAD_H = 1000, 46, 136, 70
    who = CUR_TOP["who"]
    amt_txt = (f"금액 = KRX {CUR_TOP['inv']} 순매수 거래대금(실제 체결금액)의 {TOP_DAYS}일 합계" if actual
               else f"금액 = 기관 일별 순매수 수량 × 종가의 {TOP_DAYS}일 합계(추정)")
    foot = [amt_txt + f" / 시총 대비 = {TOP_DAYS}일 순매수 금액 ÷ 시가총액"
            + ("" if caps else " (시총 자료 없음 → –)"),
            (("정규장 마감 후 자료(20시 최종 확정 전, 수 % 이내 차이 가능)" if NOTES_EXTRA["provisional"]
              else "확정 데이터 기준(장중 추정 아님)")
             + " / ETF·레버리지·스팩·리츠 제외 / 투자 판단과 책임은 본인에게 있습니다")]
    if cmp_txt:
        foot.insert(1, cmp_txt)
    H = TOP_H + HEAD_H + ROW * len(chunk) + 20 + 26 * len(foot) + 14
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    def tw(txt, font):
        return d.textlength(txt, font=font)

    def put(x, y, txt, font, color, align="left"):
        w = tw(txt, font)
        x0 = x if align == "left" else (x - w if align == "right" else x - w / 2)
        d.text((x0, y), txt, font=font, fill=color, anchor="lm")

    # 제목
    d.rounded_rectangle((24, 22, 62, 60), radius=8, fill=(228, 236, 255))
    put(43, 41, "M", f_bold, BLUE, "center")
    put(76, 41, f"{who} 순매수 금액 TOP {TOP_N}", f_title, INK)
    rng = f"{start_rank}~{start_rank + len(chunk) - 1}위"
    put(76 + tw(f"{who} 순매수 금액 TOP {TOP_N}", f_title) + 12, 45, f"{rng} · 최근 {TOP_DAYS}거래일 {who}이(가) 가장 많이 사들인 종목", f_sub, GRAY)
    meta = f"기준일 {base_date}  ·  출처 {source_label}"
    put(24, 84, meta, f_small, GRAY)
    if FRESH and FRESH.get("lag", 0) > 0:
        put(24, 110, f"※ 최신 수급 미반영: 예상 기준일 {FRESH['expected']}, 실제 {FRESH['base']} ({FRESH['lag']}거래일 전 자료)", f_small, RED)
    elif FRESH:
        put(24, 110, "최신 자료 (예상 기준일과 일치)", f_small, GRAY)

    # 열 위치
    X_RANK, X_NAME = 40, 78
    R_AMT, R_PCT, C_STREAK, R_DAY, R_CHG = 432, 590, 716, 846, 966
    y0 = TOP_H
    d.rectangle((0, y0, W, y0 + HEAD_H), fill=HEAD_BG)
    d.rectangle((258, y0, R_AMT + 22, y0 + HEAD_H), fill=PINK)
    hy = y0 + HEAD_H // 2
    put(X_RANK, hy, "#", f_head, GRAY, "center")
    put(X_NAME, hy, "종목명", f_head, GRAY)
    put(R_AMT, hy - 12, f"{who} {TOP_DAYS}일", f_head, RED, "right")
    put(R_AMT, hy + 12, "(억)", f_head, RED, "right")
    put(R_PCT, hy - 12, "시총 대비", f_head, GRAY, "right")
    put(R_PCT, hy + 12, f"{who} 순매수 %", f_head, GRAY, "right")
    put(C_STREAK, hy - 12, f"{who} 연속", f_head, GRAY, "center")
    put(C_STREAK, hy + 12, "순매수", f_head, GRAY, "center")
    put(R_DAY, hy, "등락률", f_head, GRAY, "right")
    put(R_CHG, hy, f"{TOP_DAYS}거래일", f_head, GRAY, "right")

    def chg_color(v):
        return RED if v > 0 else (BLUE if v < 0 else INK)

    for i, (code, name, m) in enumerate(chunk):
        top = y0 + HEAD_H + i * ROW
        cy = top + ROW // 2
        d.rectangle((258, top, R_AMT + 22, top + ROW), fill=PINK)
        d.line((0, top + ROW, W, top + ROW), fill=LINE, width=1)
        nm = name
        while tw(nm, f_bold) > 190 and len(nm) > 2:
            nm = nm[:-1]
        if nm != name:
            nm = nm[:-1] + "…"
        put(X_RANK, cy, str(start_rank + i), f_body, GRAY, "center")
        put(X_NAME, cy, nm, f_bold, INK)
        put(R_AMT, cy, f"{m['amt']:+,.0f}", f_bold, INK, "right")
        cap = caps.get(code)
        if cap:
            pct = m["amt"] / cap * 100
            if pct >= 1.0:   # 시총 대비 1% 이상: 빨강 알약
                draw_pill(d, f"{pct:.2f}%", f_bold, R_PCT, cy, WHITE, RED, "right", padx=10)
            else:
                put(R_PCT, cy, f"{pct:.2f}%", f_body, INK, "right")
        else:
            put(R_PCT, cy, "–", f_body, GRAY, "right")
        plus = "+" if m["streak"] >= m["all_days"] else ""
        if m["sign"] > 0 and m["streak"]:
            stxt = f"{m['streak']}{plus}일 매수"
            if m["streak"] >= 10:
                draw_pill(d, stxt, f_bold, C_STREAK, cy, WHITE, RED, "center")
            else:
                draw_pill(d, stxt, f_bold, C_STREAK, cy, DRED, PALE_RED, "center")
        elif m["sign"] < 0 and m["streak"]:
            stxt = f"{m['streak']}{plus}일 매도"
            if m["streak"] >= 10:
                draw_pill(d, stxt, f_bold, C_STREAK, cy, WHITE, BLUE, "center")
            else:
                draw_pill(d, stxt, f_bold, C_STREAK, cy, BLUE, PALE_BLUE, "center")
        else:
            put(C_STREAK, cy, "보합", f_body, GRAY, "center")
        put(R_DAY, cy, f"{m['day']:+.1f}%", f_bold if abs(m["day"]) >= 5 else f_body, chg_color(m["day"]), "right")
        put(R_CHG, cy, f"{m['chg5']:+.1f}%", f_bold if abs(m["chg5"]) >= 10 else f_body, chg_color(m["chg5"]), "right")

    fy = y0 + HEAD_H + ROW * len(chunk) + 24
    for k, line in enumerate(foot):
        put(24, fy + 26 * k, line, f_small, GRAY)
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def tg_send_photo(png, caption):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("[오류] 텔레그램 토큰/채팅ID(Secrets)가 설정되지 않았습니다. (이미지 %d바이트)" % len(png))
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                          data={"chat_id": TG_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                          files={"photo": ("top40.png", png, "image/png")}, timeout=30)
        ok = r.status_code == 200
        print("텔레그램 사진 전송", "성공" if ok else f"실패: {r.status_code} {r.text[:150]}")
    except Exception as e:
        print("텔레그램 사진 전송 실패:", e)
        ok = False
    time.sleep(1)
    return ok


def send_top40_images(picked, caps, base_date, source_label, actual=False, cmp_txt=""):
    """표 이미지 2장(1~20위, 21~40위) 전송. 하나라도 만들지 못하면 False"""
    now = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    sent = 0
    for a, b in ((0, 20), (20, 40)):
        chunk = picked[a:b]
        if not chunk:
            continue
        png = render_top40_image(chunk, a + 1, caps, base_date, source_label, actual, cmp_txt)
        if png is None:
            return False
        caption = (f"{CUR_TOP['icon']} <b>{CUR_TOP['who']} 순매수 금액 TOP {TOP_N}</b> ({a + 1}~{a + len(chunk)}위)\n"
                   f"🕘 {now}  |  📅 기준일 {esc(base_date)}\n"
                   f"🗂 출처: {esc(source_label)}\n"
                   f"{fresh_line()}")
        if not tg_send_photo(png, caption):
            return False
        sent += 1
    return sent > 0


# ---------------------------------------------------------------- 기관 5일 순매수 TOP 40 (별도 현황판)
def caps_in_eok():
    """종목별 시가총액(억원). 단위를 확신할 수 없으면 빈 값 -> 시총 대비 %는 '–'로 표시"""
    caps = dict(CAPS_EOK)
    if not caps:
        vals = [v for v in RAW_CAPS.values() if v == v and v > 0]
        if not vals:
            return {}
        mx = max(vals)
        div = 1e8 if mx >= 1e12 else (100 if mx >= 5e7 else 1)  # 원 / 백만원 / 억원 추정
        caps = {c: v / div for c, v in RAW_CAPS.items() if v == v and v > 0}
    if not caps:
        return {}
    mx = max(caps.values())
    if not (1e6 <= mx <= 1e8):  # 1위 시총이 100조~1경 원이 아니면 단위 오판으로 보고 사용 안 함
        return {}
    return caps


def top_metrics(d21):
    """한 종목의 5일 기관 순매수 금액·연속 일수·등락률. 자료가 부족하면 None"""
    d = d21.reset_index(drop=True)
    if len(d) < TOP_DAYS + 1 or d["close"].isna().tail(TOP_DAYS + 1).any():
        return None
    inst5 = d["inst"].tail(TOP_DAYS)
    if inst5.isna().any():
        return None
    close = d["close"]
    amt = float((inst5 * close.tail(TOP_DAYS)).sum() / 1e8)  # 억원 (순매수 수량 x 종가, 추정)

    # 기관 연속 순매수/순매도 일수 (가장 최근 날부터 같은 방향이 이어진 일수)
    vals = [x for x in d["inst"].tolist()][::-1]
    sign = 1 if vals[0] > 0 else (-1 if vals[0] < 0 else 0)
    streak = 0
    if sign != 0:
        for x in vals:
            if x == x and (x > 0) == (sign > 0) and x != 0:
                streak += 1
            else:
                break
    return {
        "amt": amt, "sign": sign, "streak": streak, "all_days": len(vals),
        "day": (close.iloc[-1] / close.iloc[-2] - 1) * 100,
        "chg5": (close.iloc[-1] / close.iloc[-(TOP_DAYS + 1)] - 1) * 100,
    }


def streak_text(m):
    if m["sign"] == 0 or m["streak"] == 0:
        return "⚪ 보합"
    plus = "+" if m["streak"] >= m["all_days"] else ""
    return (f"🔴 {m['streak']}{plus}일 매수" if m["sign"] > 0
            else f"🔵 {m['streak']}{plus}일 매도")


def send_top40_messages(top_rows, base_date, source_label, krx_stock=None):
    rows = []
    for code, (name, d21) in top_rows.items():
        m = top_metrics(d21)
        if m is None:
            continue
        m["amt_est"] = m["amt"]                      # 수량 × 종가 추정값 (비교용으로 보관)
        act = TOP_AMT_EOK.get(code)
        if act is not None:                          # KRX 실제 순매수 거래대금이 있으면 그 값 사용
            m["amt"], m["amt_act"] = act, True
        if m["amt"] <= 0:
            continue
        rows.append((code, name, m))
    if not rows:
        print("TOP 40: 계산 가능한 종목이 없습니다.")
        return []
    rows.sort(key=lambda x: x[2]["amt"], reverse=True)

    caps = caps_in_eok()
    picked = []
    for code, name, m in rows:
        if krx_stock is not None and name == code:      # KRX 경로는 상위 종목만 이름 조회
            name = krx_name(krx_stock, code)
        if is_excluded(name):
            continue
        picked.append((code, name, m))
        if len(picked) >= TOP_N:
            break

    actual = bool(picked) and all(m.get("amt_act") for _, _, m in picked)
    cmp_txt = ""
    if actual:
        diffs = [(m["amt_est"] - m["amt"]) / abs(m["amt"]) * 100 for _, _, m in picked if m["amt"]]
        if diffs:
            cmp_txt = (f"수량×종가 추정치와의 차이: 평균 {sum(abs(x) for x in diffs) / len(diffs):.1f}% · "
                       f"최대 {max(abs(x) for x in diffs):.1f}%")
        try:  # 실제 금액 vs 추정 금액 비교 기록 (나중에 어느 쪽이 얼마나 다른지 분석용)
            cp = "history/amount_compare.csv"
            os.makedirs("history", exist_ok=True)
            pd.DataFrame([{"base_date": base_date, "code": c, "name": n, "amt_actual_eok": round(m["amt"], 1),
                           "amt_est_eok": round(m["amt_est"], 1),
                           "diff_pct": round((m["amt_est"] - m["amt"]) / abs(m["amt"]) * 100, 2)}
                          for c, n, m in picked]).to_csv(cp, mode="a", header=not os.path.exists(cp),
                                                           index=False, encoding="utf-8-sig")
        except Exception as e:
            print("금액 비교 기록 오류:", e)

    if TOP_FORMAT in ("image", "both"):
        if send_top40_images(picked, caps, base_date, source_label, actual, cmp_txt):
            if TOP_FORMAT == "image":
                return picked
        else:
            print("TOP 40 표 이미지를 만들거나 보내지 못해 글자 메시지로 대체합니다.")

    now = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    for part, (a, b) in enumerate(((0, 20), (20, 40))):
        chunk = picked[a:b]
        if not chunk:
            continue
        header = (f"🏦 <b>기관 순매수 금액 TOP {TOP_N}</b> ({a + 1}~{a + len(chunk)}위)\n"
                  f"최근 {TOP_DAYS}거래일 기관이 가장 많이 사들인 종목\n"
                  f"🕘 {now}  |  📅 기준일 {esc(base_date)}\n"
                  f"🗂 출처: {esc(source_label)}\n"
                  f"{fresh_line()}"
                  f"━━━━━━━━━━━━━━")
        blocks = []
        for i, (code, name, m) in enumerate(chunk, a + 1):
            cap = caps.get(code)
            pct = f"{m['amt'] / cap * 100:.2f}%" if cap else "–"
            blocks.append(
                f"<b>{i}. {esc(name)}</b> ({esc(code)})\n"
                f"🏦 {TOP_DAYS}일 순매수 <b>{m['amt']:+,.0f}억</b> · 시총 대비 {pct}\n"
                f"{streak_text(m)} · 당일 {m['day']:+.1f}% · {TOP_DAYS}일 {m['chg5']:+.1f}%\n"
                f"──────────────")
        footer = ""
        if part == 1 or len(picked) <= 20:
            lines = [(f"※ 금액 = KRX 기관합계 순매수 거래대금(실제 체결금액)의 {TOP_DAYS}일 합계"
                      + (f" ({cmp_txt})" if cmp_txt else "")) if actual
                     else f"※ 금액 = 기관 일별 순매수 수량 × 종가의 {TOP_DAYS}일 합계(추정)",
                     f"※ 시총 대비 = {TOP_DAYS}일 순매수 금액 ÷ 시가총액"
                     + ("" if caps else " (시가총액 자료를 못 받아 '–' 표시)"),
                     ("※ 정규장 마감 후 자료이며 20시 최종 확정 전 값입니다 (수 % 이내 차이 가능) / "
                      if NOTES_EXTRA["provisional"] else "※ 확정 데이터 기준이며 장중 추정치는 아닙니다 / ")
                     + ("코스피·코스닥 전체 대상" if actual else "검사 대상은 시가총액 상위 종목"),
                     "※ ETF·레버리지·스팩·리츠 제외 / 위 조건 스크리너와는 별개의 현황판입니다",
                     "※ 투자 판단과 책임은 본인에게 있습니다."]
            footer = "\n".join(lines)
        send_blocks(header, blocks, footer)
    return picked


# ---------------------------------------------------------------- 연기금 TOP 40 + 기관·연기금 겹침 종목
def pick_top_pf(krx_stock):
    """연기금 5일 순매수(실제 체결금액) 큰 순 TOP N (조건·필터 없음, ETF·스팩·리츠만 이름으로 제외)"""
    rows = krx_pf_rows()
    if not rows:
        return []
    cand = []
    for code, (name, d21) in rows.items():
        m = top_metrics(d21)
        if m is None:
            continue
        m["amt"], m["amt_act"] = PF_AMT_EOK[code], True
        if m["amt"] <= 0:
            continue
        cand.append((code, name, m))
    cand.sort(key=lambda x: x[2]["amt"], reverse=True)
    picked = []
    for code, name, m in cand:
        if krx_stock is not None and name == code:
            name = krx_name(krx_stock, code)
        if is_excluded(name):
            continue
        picked.append((code, name, m))
        if len(picked) >= TOP_N:
            break
    return picked


def render_overlap_image(rows, base_date, source_label):
    """기관 TOP N ∩ 연기금 TOP N 겹침 종목 표 (PNG 바이트). rows: (코드, 이름, 기관순위, 기관m, 연기금순위, 연기금m)"""
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        print("Pillow 불러오기 실패:", e)
        return None
    f_title, f_sub = load_font(30, True), load_font(19)
    f_head, f_body, f_bold = load_font(17), load_font(21), load_font(22, True)
    f_small = load_font(15)
    if not all([f_title, f_sub, f_head, f_body, f_bold, f_small]):
        return None
    RED, BLUE, INK, GRAY = (214, 40, 40), (40, 98, 214), (29, 29, 31), (165, 170, 178)
    PURPLE, PALE_PU = (112, 72, 190), (240, 232, 252)
    LINE, HEAD_BG, PINK = (232, 234, 238), (246, 247, 249), (253, 230, 230)
    ROWBG, ROWBG2 = (255, 255, 255), (249, 250, 252)
    W, ROW, TOP_H, HEAD_H = 1240, 54, 132, 70
    notes = [f"금액 = KRX 순매수 거래대금(실제 체결금액)의 {TOP_DAYS}일 합계 / 순위 = 각 TOP 40 안에서의 순위 "
             f"(기관 TOP {OVERLAP_RANK} ∩ 연기금 TOP {OVERLAP_RANK})",
             "기관합계에는 연기금이 포함되어 있어 두 순위는 완전히 독립적이지 않습니다 / ETF·레버리지·스팩·리츠 제외 / "
             "매수 추천이 아니며 투자 판단과 책임은 본인에게 있습니다"]
    if NOTES_EXTRA["provisional"]:
        notes.insert(1, NOTES_EXTRA["provisional"])
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    foot = []
    for t in notes:
        foot += wrap_px(tmp, t, f_small, W - 48)
    H = TOP_H + HEAD_H + ROW * len(rows) + 30 + 24 * len(foot) + 14
    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)

    def tw(txt, font):
        return d.textlength(txt, font=font)

    def put(x, y, txt, font, color, align="left"):
        w = tw(txt, font)
        x0 = x if align == "left" else (x - w if align == "right" else x - w / 2)
        d.text((x0, y), txt, font=font, fill=color, anchor="lm")

    def streak(x, cy, m):
        n, sg = m["streak"], m["sign"]
        plus = "+" if m["streak"] >= m["all_days"] else ""
        if sg > 0 and n:
            t = f"{n}{plus}일 매수"
            draw_pill(d, t, f_bold, x, cy, WHITE if n >= 10 else DRED, RED if n >= 10 else PALE_RED, "center")
        elif sg < 0 and n:
            t = f"{n}{plus}일 매도"
            draw_pill(d, t, f_bold, x, cy, WHITE if n >= 10 else BLUE, BLUE if n >= 10 else PALE_BLUE, "center")
        else:
            put(x, cy, "보합", f_body, GRAY, "center")

    d.rounded_rectangle((24, 22, 62, 60), radius=8, fill=(228, 236, 255))
    put(43, 41, "M", f_bold, BLUE, "center")
    put(76, 41, "기관·연기금 동시 순매수", f_title, INK)
    put(76 + tw("기관·연기금 동시 순매수", f_title) + 14, 45,
        f"기관 TOP {OVERLAP_RANK} ∩ 연기금 TOP {OVERLAP_RANK} · {len(rows)}종목", f_sub, GRAY)
    put(24, 84, f"기준일 {base_date}  ·  출처 {source_label}", f_small, GRAY)
    if FRESH and FRESH.get("lag", 0) > 0:
        put(24, 110, f"※ 최신 수급 미반영: 예상 기준일 {FRESH['expected']}, 실제 {FRESH['base']} ({FRESH['lag']}거래일 전 자료)", f_small, RED)
    elif FRESH:
        put(24, 110, "최신 자료 (예상 기준일과 일치)", f_small, GRAY)

    C_RANK, X_NAME = 36, 76
    R_IA, C_IR, R_PA, C_PR = 410, 480, 620, 690
    C_IS, C_PS, R_DAY, R_CHG = 818, 956, 1108, 1206
    y0 = TOP_H
    d.rectangle((0, y0, W, y0 + HEAD_H), fill=HEAD_BG)
    d.rectangle((300, y0, R_IA + 18, y0 + HEAD_H), fill=PINK)
    d.rectangle((510, y0, R_PA + 18, y0 + HEAD_H), fill=PALE_PU)
    hy = y0 + HEAD_H // 2

    def head2(x, a, b, align, color=GRAY):
        put(x, hy - 12, a, f_head, color, align)
        put(x, hy + 12, b, f_head, color, align)

    put(C_RANK, hy, "#", f_head, GRAY, "center")
    put(X_NAME, hy, "종목명", f_head, GRAY)
    head2(R_IA, f"기관 {TOP_DAYS}일", "(억)", "right", RED)
    head2(C_IR, "기관", "순위", "center")
    head2(R_PA, f"연기금 {TOP_DAYS}일", "(억)", "right", PURPLE)
    head2(C_PR, "연기금", "순위", "center")
    head2(C_IS, "기관 연속", "순매수", "center")
    head2(C_PS, "연기금 연속", "순매수", "center")
    head2(R_DAY, "등락률", "", "right")
    head2(R_CHG, f"{TOP_DAYS}거래일", "", "right")

    def chg_color(v):
        return RED if v > 0 else (BLUE if v < 0 else INK)

    for i, (code, name, ra, ma, rp, mp) in enumerate(rows):
        top = y0 + HEAD_H + i * ROW
        cy = top + ROW // 2
        d.rectangle((0, top, W, top + ROW), fill=ROWBG if i % 2 == 0 else ROWBG2)
        d.rectangle((300, top, R_IA + 18, top + ROW), fill=PINK)
        d.rectangle((510, top, R_PA + 18, top + ROW), fill=PALE_PU)
        d.line((0, top + ROW, W, top + ROW), fill=LINE, width=1)
        put(C_RANK, cy, str(i + 1), f_body, GRAY, "center")
        nm = name
        while tw(nm, f_bold) > 210 and len(nm) > 2:
            nm = nm[:-1]
        if nm != name:
            nm = nm[:-1] + "…"
        put(X_NAME, cy, nm, f_bold, INK)
        put(R_IA, cy, f"{ma['amt']:+,.0f}", f_bold, DRED, "right")
        put(R_PA, cy, f"{mp['amt']:+,.0f}", f_bold, PURPLE, "right")
        for x, rk in ((C_IR, ra), (C_PR, rp)):   # 순위: 5위 안이면 진한 면, 아니면 옅은 면
            if rk <= 5:
                draw_pill(d, f"{rk}위", f_bold, x, cy, WHITE, RED if x == C_IR else PURPLE, "center")
            else:
                draw_pill(d, f"{rk}위", f_body, x, cy, DRED if x == C_IR else PURPLE, PALE_RED if x == C_IR else PALE_PU, "center")
        streak(C_IS, cy, ma)
        streak(C_PS, cy, mp)
        put(R_DAY, cy, f"{ma['day']:+.1f}%", f_bold if abs(ma["day"]) >= 5 else f_body, chg_color(ma["day"]), "right")
        put(R_CHG, cy, f"{ma['chg5']:+.1f}%", f_bold if abs(ma["chg5"]) >= 10 else f_body, chg_color(ma["chg5"]), "right")

    fy = y0 + HEAD_H + ROW * len(rows) + 24
    for j, line in enumerate(foot):
        put(24, fy + 24 * j, line, f_small, GRAY)
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def send_overlap(picked_inst, picked_pf, base_date, source_label):
    n = OVERLAP_RANK
    a = {c: (i, nm, m) for i, (c, nm, m) in enumerate(picked_inst[:n], 1)}
    b = {c: (i, nm, m) for i, (c, nm, m) in enumerate(picked_pf[:n], 1)}
    common = sorted([c for c in a if c in b], key=lambda c: (a[c][0] + b[c][0], a[c][0]))
    if not common:
        send_plain(f"🤝 기관·연기금 겹치는 종목 없음 (기관 TOP {n} ∩ 연기금 TOP {n}, 기준일 {base_date})")
        return
    rows = [(c, a[c][1], a[c][0], a[c][2], b[c][0], b[c][2]) for c in common]
    caption = (f"🤝 <b>기관·연기금 동시 순매수</b> (기관 TOP {n} ∩ 연기금 TOP {n})\n"
               f"겹치는 종목 <b>{len(rows)}개</b> · 📅 기준일 {esc(base_date)}\n"
               f"🗂 출처: {esc(source_label)}\n{fresh_line()}")
    if TOP_FORMAT in ("image", "both"):
        png = render_overlap_image(rows, base_date, source_label)
        if png is not None and tg_send_photo(png, caption):
            if TOP_FORMAT == "image":
                return
    blocks = []
    for i, (c, nm, ra, ma, rp, mp) in enumerate(rows, 1):
        blocks.append(f"<b>{i}. {esc(nm)}</b> ({esc(c)})\n"
                      f"🏦 기관 {ra}위 <b>{ma['amt']:+,.0f}억</b> · {streak_text(ma)}\n"
                      f"🏛 연기금 {rp}위 <b>{mp['amt']:+,.0f}억</b> · {streak_text(mp)}\n──────────────")
    send_blocks(caption, blocks, "※ 기관합계에는 연기금이 포함됩니다. 투자 판단과 책임은 본인에게 있습니다.")


def send_pf_messages(krx_stock, base_date, source_label, picked_inst):
    if not SEND_PF:
        return
    if krx_stock is None:
        send_plain("🏛 연기금 순매수 TOP 40: KRX 출처에서만 제공됩니다 (이번 실행은 KRX 자료가 없어 생략)")
        return
    if not PF_AMT_EOK:
        send_plain("⚠ 연기금 순매수 TOP 40: 연기금 자료를 받지 못했습니다.\n진단: " + (PF_ERR["msg"] or "원인 기록 없음"))
        return
    picked = pick_top_pf(krx_stock)
    if not picked:
        send_plain("🏛 연기금 순매수 TOP 40: 5일 순매수가 플러스인 종목이 없습니다")
        return
    caps = caps_in_eok()
    CUR_TOP.update(who="연기금", inv="연기금", icon="🏛")
    try:
        sent = False
        if TOP_FORMAT in ("image", "both"):
            sent = send_top40_images(picked, caps, base_date, source_label, True, "")
        if not (sent and TOP_FORMAT == "image"):
            now = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
            for a, b in ((0, 20), (20, 40)):
                chunk = picked[a:b]
                if not chunk:
                    continue
                header = (f"🏛 <b>연기금 순매수 금액 TOP {TOP_N}</b> ({a + 1}~{a + len(chunk)}위)\n"
                          f"🕘 {now}  |  📅 기준일 {esc(base_date)}\n🗂 출처: {esc(source_label)}\n{fresh_line()}")
                blocks = []
                for i, (code, name, m) in enumerate(chunk, a + 1):
                    cap = caps.get(code)
                    pct = f"{m['amt'] / cap * 100:.2f}%" if cap else "–"
                    blocks.append(f"<b>{i}. {esc(name)}</b> ({esc(code)})\n"
                                  f"🏛 {TOP_DAYS}일 순매수 <b>{m['amt']:+,.0f}억</b> · 시총 대비 {pct}\n"
                                  f"{streak_text(m)} · 당일 {m['day']:+.1f}% · {TOP_DAYS}일 {m['chg5']:+.1f}%\n──────────────")
                send_blocks(header, blocks, "※ 금액 = KRX 연기금 순매수 거래대금(실제 체결금액) 5일 합계 / 조건·필터 없는 현황판 / 투자 판단과 책임은 본인에게 있습니다.")
    finally:
        CUR_TOP.update(who="기관", inv="기관합계", icon="🏦")
    send_overlap(picked_inst, picked, base_date, source_label)


# ---------------------------------------------------------------- 자료 신선도 (기준일 지연 점검)
def prev_trading_day(d):
    """d 보다 이전의 가장 가까운 거래일"""
    d = d - dt.timedelta(days=1)
    while is_market_closed(d):
        d -= dt.timedelta(days=1)
    return d


def expected_base_date(now):
    """지금 시각에 '있어야 정상'인 기준일: 거래일 16시 이후면 오늘, 그 외에는 직전 거래일"""
    today = now.date()
    if not is_market_closed(today) and now.hour >= 16:
        return today
    return prev_trading_day(today)


def trading_lag(base, expected):
    """base 가 expected 보다 몇 거래일 늦은지 (같거나 최신이면 0)"""
    n, d = 0, base
    while d < expected:
        d += dt.timedelta(days=1)
        if not is_market_closed(d):
            n += 1
    return n


def compute_fresh(now, base_date):
    try:
        base = dt.date.fromisoformat(str(base_date))
    except Exception:
        return {}
    exp = expected_base_date(now)
    return {"expected": exp.isoformat(), "base": base.isoformat(), "lag": trading_lag(base, exp)}


def fresh_line():
    if not FRESH:
        return ""
    if FRESH["lag"] > 0:
        return (f"⚠ <b>최신 수급 미반영</b>: 예상 기준일 {FRESH['expected']}, "
                f"실제 {FRESH['base']} ({FRESH['lag']}거래일 전 자료)\n")
    return "✅ 최신 자료 (예상 기준일과 일치)\n"


FRESH_COLS = ["run_at", "source", "expected_base", "actual_base", "lag_days",
              "stocks_at_base_pct", "total", "nodata", "candidates", "calc_min", "status"]


def record_freshness(now, source, base_dates, total, nodata, candidates, status, t0):
    """실행마다 한 줄 기록 -> 몇 주 쌓이면 어느 시각에 최신 자료가 나오는지 확인 가능"""
    try:
        os.makedirs(os.path.dirname(FRESH_PATH) or ".", exist_ok=True)
        top = max(base_dates) if base_dates else ""
        pct = round(base_dates.count(top) / len(base_dates) * 100, 1) if base_dates else ""
        row = {
            "run_at": now.strftime("%Y-%m-%d %H:%M"), "source": source,
            "expected_base": FRESH.get("expected", expected_base_date(now).isoformat()),
            "actual_base": top, "lag_days": FRESH.get("lag", ""),
            "stocks_at_base_pct": pct, "total": total, "nodata": nodata,
            "candidates": candidates, "calc_min": round((time.time() - t0) / 60, 1),
            "status": status,
        }
        new_file = not os.path.exists(FRESH_PATH)
        pd.DataFrame([row], columns=FRESH_COLS).to_csv(
            FRESH_PATH, mode="a", header=new_file, index=False, encoding="utf-8-sig")
    except Exception as e:
        print("신선도 기록 오류:", type(e).__name__, e)


# ---------------------------------------------------------------- 11:30 장중 점검 (전날 저녁 후보 대상)
RT_CODE = ["itemCode", "stockCode", "code", "symbolCode", "cd"]
RT_PRICE = ["closePrice", "currentPrice", "nowPrice", "tradePrice", "price", "nv"]
RT_VOL = ["accumulatedTradingVolume", "tradingVolume", "volume", "accVolume", "aq"]


def fetch_realtime(codes):
    """여러 종목의 현재가·누적 거래량·누적 거래대금을 한 번에 조회 -> {코드: dict}"""
    out = {}
    for i in range(0, len(codes), 20):
        batch = codes[i:i + 20]
        data = get_json(f"{S_BASE}/api/polling/domestic/stock?itemCodes={','.join(batch)}", retry=2)
        lst = find_list(data) if data is not None else []
        for d in lst:
            code = str(pick(d, RT_CODE) or "")
            if not code:
                continue
            if not LAST_INFO["keys"]:
                LAST_INFO["keys"] = ", ".join(list(d.keys())[:20])
            out[code] = {"price": to_num(pick(d, RT_PRICE)), "vol": to_num(pick(d, RT_VOL)),
                         "val_raw": to_num(val_value(d))}
    # 거래대금 단위 보정 (없으면 현재가 x 누적 거래량으로 추정)
    ratios = [v["val_raw"] / (v["price"] * v["vol"]) for v in out.values()
              if v["val_raw"] == v["val_raw"] and v["val_raw"] > 0 and v["price"] > 0 and v["vol"] > 0]
    factor, est = 1.0, True
    if len(ratios) >= 3:
        med = float(np.median(ratios))
        for f in (1, 1e3, 1e4, 1e6, 1e8):
            if 0.5 <= med * f <= 2.0:
                factor, est = f, False
                break
    for v in out.values():
        if not est and v["val_raw"] == v["val_raw"] and v["val_raw"] > 0:
            v["val"] = v["val_raw"] * factor
        else:
            v["val"] = v["price"] * v["vol"] if v["price"] > 0 and v["vol"] > 0 else float("nan")
    return out


def intraday_rows(cand, rt):
    rows = []
    for _, c in cand.iterrows():
        v = rt.get(c["code"])
        row = {"code": c["code"], "name": c["name"], "ycl": c["close"], "n_conds": c["n_conds"],
               "pullback": c["pullback2"] == "Y", "price": None, "chg": None, "state": "–", "rank": 3,
               "pos": None, "val_eok": None, "pace_val": None, "pace_vol": None}
        if v and v["price"] == v["price"] and v["price"] > 0:
            row["price"] = v["price"]
            if c["close"] and c["close"] > 0:
                row["chg"] = (v["price"] / c["close"] - 1) * 100
            hi, lo = c["box_hi"], c["box_lo"]
            if hi == hi and lo == lo and hi > lo:
                if v["price"] > hi:
                    row["state"], row["rank"] = "상단 돌파", 0
                elif v["price"] < lo:
                    row["state"], row["rank"] = "하단 이탈", 1
                else:
                    row["pos"] = (v["price"] - lo) / (hi - lo) * 100
                    row["state"], row["rank"] = f"박스 안 {row['pos']:.0f}%", 2
            if v.get("val") == v.get("val") and v.get("val"):
                row["val_eok"] = v["val"] / 1e8
                if c["avg_val"] == c["avg_val"] and c["avg_val"] > 0:
                    row["pace_val"] = row["val_eok"] / c["avg_val"] * 100
            if v["vol"] == v["vol"] and c["avg_vol"] == c["avg_vol"] and c["avg_vol"] > 0:
                row["pace_vol"] = v["vol"] / c["avg_vol"] * 100
        rows.append(row)
    rows.sort(key=lambda r: (r["rank"], -(r["pace_val"] or 0)))
    return rows


def render_intraday_image(rows, shown, base_date, now_txt, n_cand):
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        print("Pillow 불러오기 실패:", e)
        return None
    f_title, f_sub = load_font(30, True), load_font(19)
    f_head, f_body, f_bold = load_font(17), load_font(21), load_font(21, True)
    f_small = load_font(15)
    if not all([f_title, f_sub, f_head, f_body, f_bold, f_small]):
        return None
    RED, BLUE, INK, GRAY = (217, 58, 58), (47, 111, 214), (29, 29, 31), (150, 155, 163)
    ORANGE, GOLD = (245, 140, 30), (232, 176, 20)
    LINE, HEAD_BG, YEL, LBLUE = (232, 234, 238), (246, 247, 249), (255, 249, 226), (234, 241, 255)
    W, ROW, TOP_H, HEAD_H = 1100, 50, 112, 70
    notes = ["속도 = 오늘 누적 ÷ 어제까지 20일 평균 하루치 (100% 이상이면 오전에 이미 하루 평균을 넘긴 것) / "
             "박스 = 20일 종가 범위 (상단 돌파·하단 이탈)",
             "장중 값은 계속 변하며 확정 자료가 아닙니다. 매수 추천이 아니며 투자 판단과 책임은 본인에게 있습니다."]
    if len(rows) > len(shown):
        notes.append(f"후보 {n_cand}종목 중 돌파·이탈·거래대금 속도 순으로 {len(shown)}종목만 표시했습니다")
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    foot = []
    for t in notes:
        foot += wrap_px(tmp, t, f_small, W - 48)
    H = TOP_H + HEAD_H + ROW * len(shown) + 50 + 24 * len(foot) + 14
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    def tw(txt, font):
        return d.textlength(txt, font=font)

    def put(x, y, txt, font, color, align="left"):
        w = tw(txt, font)
        x0 = x if align == "left" else (x - w if align == "right" else x - w / 2)
        d.text((x0, y), txt, font=font, fill=color, anchor="lm")

    d.rounded_rectangle((24, 22, 62, 60), radius=8, fill=(228, 236, 255))
    put(43, 41, "M", f_bold, BLUE, "center")
    put(76, 41, "장중 점검", f_title, INK)
    put(76 + tw("장중 점검", f_title) + 14, 45, f"어제 저녁 후보 {n_cand}종목의 현재 상태", f_sub, GRAY)
    put(24, 86, f"후보 기준일 {base_date}  ·  조회 {now_txt}  ·  출처 네이버 증권 실시간 시세", f_small, GRAY)

    C_RANK, C_SIG, X_NAME = 34, 74, 100
    R_PRICE, R_CHG, C_BOX, R_VAL, R_PV, R_VV = 450, 556, 664, 846, 958, 1070
    y0 = TOP_H
    d.rectangle((0, y0, W, y0 + HEAD_H), fill=HEAD_BG)
    hy = y0 + HEAD_H // 2

    def head2(x, a, b, align):
        put(x, hy - 12, a, f_head, GRAY, align)
        put(x, hy + 12, b, f_head, GRAY, align)

    put(C_RANK, hy, "#", f_head, GRAY, "center")
    put(X_NAME, hy, "종목명", f_head, GRAY)
    head2(R_PRICE, "현재가", "(원)", "right")
    head2(R_CHG, "어제 대비", "", "right")
    head2(C_BOX, "박스 상태", "(20일 종가)", "center")
    head2(R_VAL, "오늘 거래대금", "(억)", "right")
    head2(R_PV, "거래대금", "속도", "right")
    head2(R_VV, "거래량", "속도", "right")

    for i, r in enumerate(shown):
        top = y0 + HEAD_H + i * ROW
        cy = top + ROW // 2
        if r["rank"] == 0:
            d.rectangle((0, top, W, top + ROW), fill=YEL)
        elif r["rank"] == 1:
            d.rectangle((0, top, W, top + ROW), fill=LBLUE)
        d.line((0, top + ROW, W, top + ROW), fill=LINE, width=1)
        put(C_RANK, cy, str(i + 1), f_body, GRAY, "center")
        marks = []
        if r["n_conds"] >= 3:
            marks.append(("dot", RED))
        elif r["n_conds"] == 2:
            marks.append(("dot", ORANGE))
        if r["pullback"]:
            marks.append(("star", GOLD))
        for j, (kind, col) in enumerate(marks):
            cx = C_SIG + (j - (len(marks) - 1) / 2) * 26
            if kind == "dot":
                d.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), fill=col)
            else:
                draw_star(d, cx, cy, 11, col)
        nm = r["name"]
        while tw(nm, f_bold) > 250 and len(nm) > 2:
            nm = nm[:-1]
        if nm != r["name"]:
            nm = nm[:-1] + "…"
        put(X_NAME, cy, nm, f_bold, INK)
        if r["price"] is None:
            put(R_PRICE, cy, "조회 실패", f_body, GRAY, "right")
            continue
        put(R_PRICE, cy, f"{r['price']:,.0f}", f_body, INK, "right")
        if r["chg"] is not None:
            put(R_CHG, cy, f"{r['chg']:+.1f}%", f_bold, RED if r["chg"] > 0 else (BLUE if r["chg"] < 0 else INK), "right")
        if r["rank"] == 0:
            draw_pill(d, r["state"], f_bold, C_BOX, cy, WHITE, RED, "center", padx=12)
        elif r["rank"] == 1:
            draw_pill(d, r["state"], f_bold, C_BOX, cy, WHITE, BLUE, "center", padx=12)
        else:
            put(C_BOX, cy, r["state"], f_body, INK, "center")
        put(R_VAL, cy, f"{r['val_eok']:,.0f}" if r["val_eok"] is not None else "–", f_body, INK, "right")
        for x, key in ((R_PV, "pace_val"), (R_VV, "pace_vol")):
            v = r[key]
            if v is None:
                put(x, cy, "–", f_body, GRAY, "right")
            elif v >= 100:
                draw_pill(d, f"{v:.0f}%", f_bold, x, cy, WHITE, RED, "right", padx=10)
            else:
                put(x, cy, f"{v:.0f}%", f_body, INK if v >= 50 else GRAY, "right")

    ly = y0 + HEAD_H + ROW * len(shown) + 26
    x = 24
    d.ellipse((x, ly - 8, x + 16, ly + 8), fill=RED)
    put(x + 22, ly, "어제 조건 3개 충족", f_small, INK)
    x += 22 + tw("어제 조건 3개 충족", f_small) + 20
    d.ellipse((x, ly - 8, x + 16, ly + 8), fill=ORANGE)
    put(x + 22, ly, "2개 충족", f_small, INK)
    x += 22 + tw("2개 충족", f_small) + 20
    draw_star(d, x + 8, ly, 10, GOLD)
    put(x + 24, ly, "눌림목", f_small, INK)
    x += 24 + tw("눌림목", f_small) + 24
    d.rectangle((x, ly - 8, x + 16, ly + 8), fill=YEL, outline=LINE)
    put(x + 22, ly, "박스 상단 돌파", f_small, INK)
    x += 22 + tw("박스 상단 돌파", f_small) + 20
    d.rectangle((x, ly - 8, x + 16, ly + 8), fill=LBLUE, outline=LINE)
    put(x + 22, ly, "박스 하단 이탈", f_small, INK)
    for j, line in enumerate(foot):
        put(24, ly + 28 + 24 * j, line, f_small, GRAY)
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def run_intraday():
    now = dt.datetime.now(KST)
    if not FORCE and is_market_closed(now.date()):
        print("휴장일이라 장중 점검을 건너뜁니다:", now.date())
        return
    hist = load_history()
    if hist.empty or not hist["base_date"].astype(bool).any():
        send_plain("⏱ 장중 점검: 어제 저녁 후보 기록(history/alerts.csv)이 없습니다.")
        return
    last = hist["base_date"].max()
    cand = hist[hist["base_date"] == last].copy()
    cand = cand.sort_values(["n_conds", "inst_amt"], ascending=False)
    codes = list(cand["code"])
    print(f"장중 점검: 후보 {len(codes)}종목 (기준일 {last})")
    rt = fetch_realtime(codes)
    if not rt:
        send_plain("⚠ 장중 점검: 실시간 시세를 받지 못했습니다.\n진단: "
                   + (LAST_INFO["msg"] or "원인 기록 없음") + f"\n응답 항목: {LAST_INFO['keys'] or '없음'}")
        return
    rows = intraday_rows(cand, rt)
    shown = rows[:20]
    n_break = sum(1 for r in rows if r["rank"] == 0)
    n_out = sum(1 for r in rows if r["rank"] == 1)
    n_fast = sum(1 for r in rows if (r["pace_val"] or 0) >= 100)
    n_fail = sum(1 for r in rows if r["price"] is None)
    now_txt = now.strftime("%m/%d %H:%M")
    caption = (f"⏱ <b>장중 점검</b> ({esc(now_txt)})\n"
               f"어제 저녁 후보 {len(rows)}종목 (기준일 {esc(last)})\n"
               f"📈 박스 상단 돌파 <b>{n_break}</b> · 📉 하단 이탈 <b>{n_out}</b> · "
               f"🔥 거래대금 평균 초과 <b>{n_fast}</b>" + (f" · 조회 실패 {n_fail}" if n_fail else ""))
    png = render_intraday_image(rows, shown, last, now_txt, len(rows))
    if png is not None and tg_send_photo(png, caption):
        return
    print("장중 점검 표 이미지를 만들거나 보내지 못해 글자 메시지로 대체합니다.")
    blocks = []
    for i, r in enumerate(shown, 1):
        if r["price"] is None:
            blocks.append(f"{i}. {esc(r['name'])} - 조회 실패")
            continue
        pv = f"{r['pace_val']:.0f}%" if r["pace_val"] is not None else "–"
        ch = f"{r['chg']:+.1f}%" if r["chg"] is not None else "–"
        blocks.append(f"<b>{i}. {esc(r['name'])}</b> {r['price']:,.0f}원 ({ch}) · {esc(r['state'])} · 거래대금 속도 {pv}")
    send_blocks(caption, blocks, "※ 장중 값은 확정 자료가 아니며 매수 추천이 아닙니다.")


# ---------------------------------------------------------------- 도착 점검 (네이버/KRX 에 당일 수급이 도착했는지)
def run_probe():
    now = dt.datetime.now(KST)
    # 도착 여부는 '오늘 자료'가 왔는지로 판단 (휴장일이면 직전 거래일)
    exp = now.date() if not is_market_closed(now.date()) else prev_trading_day(now.date())
    lines = [f"🔎 자료 도착 점검 ({now.strftime('%m/%d %H:%M')})", f"오늘 자료 기준일: {exp.isoformat()}"]
    naver_date, krx_date = "", ""

    # 네이버 (삼성전자로 확인)
    try:
        df = get_rows("005930")
        if df is None:
            lines.append("• 네이버 증권: 조회 실패 - " + (LAST_INFO["msg"] or "원인 기록 없음"))
        else:
            ok = df[df["inst"].notna()]
            naver_date = f"{ok['date'].iloc[-1][:4]}-{ok['date'].iloc[-1][4:6]}-{ok['date'].iloc[-1][6:]}" if len(ok) else ""
            latest_any = df["date"].iloc[-1]
            latest_any = f"{latest_any[:4]}-{latest_any[4:6]}-{latest_any[6:]}"
            mark = "✅ 도착" if naver_date and dt.date.fromisoformat(naver_date) >= exp else "⚠ 미도착"
            extra = "" if latest_any == naver_date else f" (시세는 {latest_any}까지 있으나 기관 수급은 미반영)"
            lines.append(f"• 네이버 증권: 기관 수급 최신 기준일 {naver_date or '없음'} {mark}{extra}")
    except Exception as e:
        lines.append(f"• 네이버 증권: 오류 {type(e).__name__}: {str(e)[:100]}")

    # KRX
    lines.append("• KRX 계정 정보(Secrets): " + ("설정됨" if (KRX_ID and KRX_PW) else "비어 있음 ⚠"))
    LAST_INFO["msg"] = ""
    stock = krx_import()
    if stock is None:
        lines.append("• KRX: 조회 실패 - " + LAST_INFO["msg"])
    else:
        try:
            ref = stock.get_market_ohlcv_by_date((now.date() - dt.timedelta(days=15)).strftime("%Y%m%d"),
                                                 now.date().strftime("%Y%m%d"), "005930")
            dates = [d.strftime("%Y%m%d") for d in ref.index]
            for d in reversed(dates[-6:]):
                net = krx_net(stock, d, "KOSPI")
                if net is not None and len(net) > 0:
                    krx_date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                    break
            if krx_date:
                mark = "✅ 도착" if dt.date.fromisoformat(krx_date) >= exp else "⚠ 미도착"
                lines.append(f"• KRX: 기관 수급 최신 기준일 {krx_date} {mark}")
                # 시각별 비교용: 기관 순매수 금액 합계와 상위 종목 (15:50 값 vs 20시 이후 최종값 비교)
                snap_rows, totals, top_lines = [], [], []
                dkey = krx_date.replace("-", "")
                for m in ("KOSPI", "KOSDAQ"):
                    sdf, col = krx_net_df(stock, dkey, m)
                    if sdf is None:
                        continue
                    amt = pd.to_numeric(sdf[col], errors="coerce") / 1e8
                    totals.append(f"{m} {amt.sum():+,.0f}억")
                    top = amt.sort_values(ascending=False).head(20)
                    for rk, (code, v) in enumerate(top.items(), 1):
                        nm = str(sdf.loc[code, "종목명"]) if "종목명" in sdf.columns else code
                        snap_rows.append({"checked_at": now.strftime("%Y-%m-%d %H:%M"), "date": krx_date,
                                          "market": m, "rank": rk, "code": code, "name": nm, "net_eok": round(float(v), 1)})
                    if len(top):
                        top_lines.append(f"   {m} 상위: " + ", ".join(
                            f"{str(sdf.loc[c, '종목명']) if '종목명' in sdf.columns else c} {v:+,.0f}억"
                            for c, v in list(top.items())[:3]))
                if totals:
                    lines.append("   기관 순매수 합계: " + " · ".join(totals))
                lines.extend(top_lines)
                if snap_rows:
                    try:
                        sp = "history/inst_snapshot.csv"
                        os.makedirs("history", exist_ok=True)
                        pd.DataFrame(snap_rows).to_csv(sp, mode="a", header=not os.path.exists(sp),
                                                       index=False, encoding="utf-8-sig")
                    except Exception as e:
                        print("스냅샷 기록 오류:", e)
            else:
                lines.append("• KRX: 기관 수급을 받지 못함 - " + (LAST_INFO["msg"] or "자료 없음 (로그인/접속 확인 필요)"))
        except Exception as e:
            lines.append(f"• KRX: 오류 {type(e).__name__}: {str(e)[:120]}")
    lines.append("※ 같은 점검을 시각을 바꿔 여러 번 실행하면 당일 자료가 처음 도착하는 시각을 알 수 있습니다.")

    # 기록 (history/arrival.csv)
    try:
        path = "history/arrival.csv"
        os.makedirs("history", exist_ok=True)
        row = {"checked_at": now.strftime("%Y-%m-%d %H:%M"), "expected": exp.isoformat(),
               "naver_date": naver_date, "krx_date": krx_date}
        pd.DataFrame([row]).to_csv(path, mode="a", header=not os.path.exists(path),
                                   index=False, encoding="utf-8-sig")
    except Exception as e:
        print("도착 기록 오류:", e)
    send_plain("\n".join(lines))


# ---------------------------------------------------------------- 실행
SEND_MORNING = (7, 50)   # 오전 전송 시각 (한국시간)
SEND_EVENING = (18, 50)  # 오후 전송 시각 (한국시간) - 11/9 실측: KRX 는 17시에도 당일 자료 도착, 네이버는 18:47에도 미도착


def wait_until_send_time():
    """계산을 먼저 끝내 두고, 정해진 시각(오후 18:50)까지 기다렸다가 전송.
    이미 그 시각이 지났으면 기다리지 않고 바로 전송. 수동 실행(FORCE)은 바로 전송."""
    if FORCE:
        return
    now = dt.datetime.now(KST)
    h, m = SEND_MORNING if now.hour < 12 else SEND_EVENING
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    wait = (target - now).total_seconds()
    if 0 < wait <= 45 * 60:  # 최대 45분까지만 대기
        print(f"{h:02d}:{m:02d}까지 {int(wait)}초 대기")
        time.sleep(wait)


def is_market_closed(today):
    if today.weekday() >= 5:
        return True
    if (today.month, today.day) == (12, 31):  # 연말 휴장
        return True
    try:
        import holidays
        return today in holidays.KR(years=today.year)
    except Exception:
        return False


def fail_message(prefix):
    lines = [f"⚠ {prefix}"]
    for x in FAIL_LOG:
        lines.append("- " + x)
    if not FAIL_LOG:
        lines.append(f"진단: {LAST_INFO['msg'] or '원인 기록 없음'}")
    lines.append(f"응답 항목: {LAST_INFO['keys'] or '없음'}")
    return "\n".join(lines)


def scan_naver():
    """1순위: 네이버 증권. 실패하면 None (알림은 보내지 않고 다음 출처로 넘김)"""
    LAST_INFO["msg"] = ""
    stocks = get_universe()
    if not stocks:
        FAIL_LOG.append("네이버: 종목 목록 실패 / " + (LAST_INFO["msg"] or "원인 기록 없음"))
        return None
    print("[네이버] 검사 종목 수:", len(stocks))

    results, nodata, base_dates = [], 0, []
    top_rows = {}  # TOP 40 현황판용 (코드 -> (이름, 21일 데이터))
    for i, (code, name) in enumerate(stocks, 1):
        try:
            d21 = get_rows(code)
            if d21 is not None:
                top_rows[code] = (name, d21)
            r, status, last = analyze_rows(code, name, d21)
        except Exception as e:
            print("오류", code, e)
            r, status, last = None, "nodata", None
        if status == "nodata":
            nodata += 1
        if last:
            base_dates.append(last)
        if r:
            results.append(r)
        if i == 30 and nodata >= 28:  # 처음부터 거의 다 실패 -> 빨리 중단
            FAIL_LOG.append("네이버: 종목별 데이터를 거의 읽지 못함 / " + (LAST_INFO["msg"] or "원인 기록 없음"))
            return None
        if i % 100 == 0:
            print(f"  진행 {i}/{len(stocks)}")
    if nodata > len(stocks) * 0.5:
        FAIL_LOG.append(f"네이버: 데이터 수집 실패 {nodata}/{len(stocks)} / " + (LAST_INFO["msg"] or ""))
        return None
    return {"results": results, "total": len(stocks), "nodata": nodata, "base_dates": base_dates,
            "top_rows": top_rows, "krx_stock": None}


def scan_krx():
    """2순위(또는 지정 시): 한국거래소 KRX (pykrx). 실패하면 None"""
    LAST_INFO["msg"] = ""
    stock = krx_import()
    if stock is None:
        FAIL_LOG.append("KRX: " + LAST_INFO["msg"])
        return None
    try:
        data = krx_collect(stock)
    except Exception as e:
        FAIL_LOG.append(f"KRX: 수집 오류 {type(e).__name__}: {str(e)[:150]}"
                        + (f" / {LAST_INFO['msg']}" if LAST_INFO["msg"] else "") + krx_cred_hint())
        return None
    if not data:
        FAIL_LOG.append("KRX: " + (LAST_INFO["msg"] or "자료 없음 (KRX 로그인/접속 확인 필요)") + krx_cred_hint())
        return None
    print("[KRX] 검사 종목 수:", len(data))

    results, nodata, base_dates = [], 0, []
    top_rows = krx_top_rows() or {code: (code, d21) for code, d21 in data.items()}  # TOP 40 현황판용
    for code, d21 in data.items():
        try:
            r, status, last = analyze_rows(code, code, d21)
        except Exception as e:
            print("오류", code, e)
            r, status, last = None, "nodata", None
        if status == "nodata":
            nodata += 1
        if last:
            base_dates.append(last)
        if r:
            results.append(r)
    # 종목명 확인 + ETF/스팩/리츠 등 제외 (후보 종목에만 조회)
    final = []
    for r in results:
        name = krx_name(stock, r["code"])
        if is_excluded(name):
            continue
        r["name"] = name
        final.append(r)
    if nodata > len(data) * 0.5:
        FAIL_LOG.append(f"KRX: 데이터 수집 실패 {nodata}/{len(data)}")
        return None
    return {"results": final, "total": len(data), "nodata": nodata, "base_dates": base_dates,
            "top_rows": top_rows, "krx_stock": stock}


def wait_for_krx_today(now, deadline_hm=(18, 35), step=120):
    """거래일 저녁 실행 전용: KRX 에 오늘 기관 수급이 올라올 때까지 기다림. 마감 시각이 되면 있는 자료로 진행"""
    if is_market_closed(now.date()):
        return
    stock = krx_import()
    if stock is None:
        return
    d = now.date().strftime("%Y%m%d")
    deadline = now.replace(hour=deadline_hm[0], minute=deadline_hm[1], second=0, microsecond=0)
    while True:
        df, col = krx_net_df(stock, d, "KOSPI")
        if df is not None and len(df) > 0:
            print("KRX 오늘 기관 수급 도착 확인")
            return
        cur = dt.datetime.now(KST)
        if cur >= deadline:
            print("KRX 오늘 자료 미도착 - 있는 자료로 진행합니다")
            return
        print(f"KRX 오늘 자료 대기 중... ({cur.strftime('%H:%M')})")
        time.sleep(step)


def main():
    if MODE == "probe":
        run_probe()
        return
    if MODE == "intraday":
        run_intraday()
        return
    t0 = time.time()
    now = dt.datetime.now(KST)
    if not FORCE and is_market_closed(now.date()):
        print("휴장일이라 건너뜁니다:", now.date())
        return

    order = {"auto": ["krx", "naver"], "krx_first": ["krx", "naver"], "naver_first": ["naver", "krx"],
             "naver": ["naver"], "krx": ["krx"]}.get(DATA_SOURCE, ["krx", "naver"])
    if order[0] == "krx" and not FORCE and now.hour >= 16:
        wait_for_krx_today(now)  # 저녁 실행: KRX 에 오늘 자료가 올라올 때까지 (최대 18:35)
    out, label = None, ""
    for i, src in enumerate(order):
        out = scan_krx() if src == "krx" else scan_naver()
        label = "한국거래소(KRX) 정보데이터시스템" if src == "krx" else "네이버 증권"
        if out is not None:
            if i > 0:
                label += " ※" + ("네이버" if src == "krx" else "KRX") + " 실패로 전환"
            PRIMARY["src"] = src
            break
        if i + 1 < len(order):
            print(f"{label} 수집 실패 -> 다음 출처로 전환합니다.")
    NOTES_EXTRA["provisional"] = ""
    if out is None:
        record_freshness(now, "-", [], 0, 0, 0, "fail", t0)
        send_plain(fail_message("모든 데이터 출처에서 수집에 실패했습니다."))
        return

    results, total, nodata = out["results"], out["total"], out["nodata"]
    base_dates = out["base_dates"]
    base_date = max(base_dates) if base_dates else "알 수 없음"
    FRESH.clear()
    FRESH.update(compute_fresh(now, base_date))
    if label.startswith("한국거래소") and base_date == dt.datetime.now(KST).date().isoformat() \
            and dt.datetime.now(KST).hour < 20:
        NOTES_EXTRA["provisional"] = "기관 수급은 20시 최종 확정 전 자료입니다 (시간외 반영 전이라 최종값과 수 % 이내 차이 가능)"
    if FRESH.get("lag", 0) > 0:
        print(f"⚠ 최신 수급 미반영: 예상 {FRESH['expected']} / 실제 {FRESH['base']} ({FRESH['lag']}거래일)")

    # 후보 종목에만 60일선 분석 (요청 수를 아끼기 위해)
    print(f"60일선 분석: {len(results)}종목")
    for r in results:
        try:
            enrich_ma(r)
        except Exception as e:
            print("60일선 분석 오류", r["code"], e)
            r["ma_label"] = "부족"
        r["pullback"] = is_pullback(r)
    print("⭐ 눌림목 패턴:", sum(1 for r in results if r["pullback"]), "종목")

    # 성과 기록 저장 + 과거 알림 종목 수익률 갱신
    perf_msg = None
    try:
        hist = load_history()
        hist = record_history(hist, results)
        hist = update_returns(hist)
        save_history(hist)
        perf_msg = build_performance_message(hist)
    except Exception as e:
        print("성과 기록 처리 오류:", e)

    record_freshness(now, label, base_dates, total, nodata, len(results), "ok", t0)
    wait_until_send_time()
    send_condition_messages(results, total, nodata, base_date, label)
    picked_inst = []
    try:  # 별도 현황판: 실패해도 기존 알림에는 영향 없음
        picked_inst = send_top40_messages(out.get("top_rows") or {}, base_date, label, out.get("krx_stock")) or []
    except Exception as e:
        print("TOP 40 처리 오류:", type(e).__name__, e)
    try:  # 연기금 TOP 40 + 기관·연기금 겹침 종목
        send_pf_messages(out.get("krx_stock"), base_date, label, picked_inst)
    except Exception as e:
        print("연기금 TOP 40 처리 오류:", type(e).__name__, e)
    if perf_msg and SEND_PERFORMANCE and (FORCE or now.hour >= 12):  # 성과 요약은 저녁 알림에만
        tg_send_long(perf_msg, use_html=True)
    print("완료. 해당 종목:", len(results))


if __name__ == "__main__":
    main()
