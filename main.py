import datetime
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import FinanceDataReader as fdr
import pandas as pd
import requests

# 깃허브 시크릿에서 텔레그램 토큰 및 챗 ID 가져오기
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram_message(message):
  """텔레그램 메시지 전송 함수 (글자 수 제한 방지를 위해 분할 전송)"""
  if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("[경고] 텔레그램 토큰 또는 챗 ID가 설정되지 않았습니다.")
    return

  url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

  if len(message) > 4000:
    chunks = [message[i : i + 4000] for i in range(0, len(message), 4000)]
    for chunk in chunks:
      payload = {
          "chat_id": TELEGRAM_CHAT_ID,
          "text": chunk,
          "parse_mode": "Markdown",
      }
      requests.post(url, data=payload, timeout=10)
    print("텔레그램 분할 알림 전송 성공!")
  else:
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    response = requests.post(url, data=payload, timeout=10)
    if response.status_code == 200:
      print("텔레그램 알림 전송 성공!")
    else:
      print(f"텔레그램 알림 전송 실패: {response.text}")


def get_target_stocks():
  """코스피, 코스닥 각 시총 상위 400개 추출 (ETF, ETN, 스팩, 리츠 등 제외 / 우선주 허용)"""
  exclude_keywords = [
      "ETF",
      "ETN",
      "스팩",
      "SPAC",
      "리츠",
      "KODEX",
      "TIGER",
      "ARIRANG",
      "KBSTAR",
      "SOL",
      "HANARO",
      "KOSEF",
      "PLUS",
      "TIMEFOLIO",
      "RISE",
      "WOORI",
      "BNK",
      "히어로즈",
  ]
  target_dfs = []

  for market in ["KOSPI", "KOSDAQ"]:
    try:
      df = fdr.StockListing(market)
      if "Name" in df.columns:
        for kw in exclude_keywords:
          df = df[~df["Name"].str.contains(kw, na=False)]
      if "MarCap" in df.columns:
        df = df.sort_values(by="MarCap", ascending=False)
      target_dfs.append(df.head(400))
    except Exception as e:
      print(f"{market} 종목 리스트 로드 오류: {e}")

  if target_dfs:
    combined_df = pd.concat(target_dfs, ignore_index=True)
    return combined_df
  return pd.DataFrame()


def get_naver_institutional_amount_5days(ticker, recent_df):
  """네이버 금융에서 최근 5거래일 기관 순매수 금액(억원) 추정 및 분석"""
  try:
    url = f"https://finance.naver.com/item/frgn.naver?code={ticker}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
    }
    res = requests.get(url, headers=headers, timeout=5)
    if res.status_code == 200:
      dfs = pd.read_html(res.text)
      for df in dfs:
        cols = [str(c) for c in df.columns]
        inst_col_candidates = [c for c in cols if "기관" in c]
        if inst_col_candidates:
          inst_col = inst_col_candidates[0]
          df = df.dropna(subset=[df.columns[0]])
          if len(df) < 5:
            continue

          amounts = []
          for i in range(5):
            row_val = df.iloc[i][inst_col]
            close_price = recent_df["Close"].iloc[-(5 - i)]
            try:
              val_str = str(row_val).replace(",", "").strip()
              shares = int(val_str[1:]) if val_str.startswith("+") else int(val_str)
              amount_억 = (shares * close_price) / 100_000_000
              amounts.append(amount_억)
            except:
              amounts.append(0.0)

          if not amounts:
            return 0.0, 0.0, 0, []

          today_amount = amounts[0]
          positive_days = sum(1 for a in amounts if a > 0)

          return today_amount, sum(amounts), positive_days, amounts
  except Exception:
    pass
  return 0.0, 0.0, 0, []


def process_single_stock(row, start_date_str, end_date_str):
  """개별 종목 데이터를 병렬로 분석하는 함수"""
  ticker = row["Code"]
  stock_name = row["Name"]

  try:
    df = fdr.DataReader(ticker, start_date_str, end_date_str)
    if len(df) < 5:
      return None

    df_recent = df.tail(5)
    latest_price = df_recent["Close"].iloc[-1]

    # 1. 공통 전제 조건: 최근 5거래일 박스권 범위 23% 이내
    high_max = df_recent["High"].max()
    low_min = df_recent["Low"].min()
    if low_min <= 0:
      return None

    price_range_ratio = (high_max - low_min) / low_min
    if price_range_ratio > 0.23:
      return None

    today_amt, total_amt_5d, positive_days, daily_amounts = (
        get_naver_institutional_amount_5days(ticker, df_recent)
    )
    if not daily_amounts or len(daily_amounts) < 5:
      return None

    # 5일 데이터 분할 (전반 2일 vs 후반 3일)
    first_half_avg = sum(daily_amounts[:2]) / 2.0
    second_half_avg = sum(daily_amounts[2:]) / 3.0
    growth_ratio = (
        (second_half_avg - first_half_avg) / abs(first_half_avg)
        if first_half_avg != 0
        else 0
    )

    mean_amt = sum(daily_amounts) / 5.0
    spike_count = sum(
        1 for a in daily_amounts if mean_amt > 0 and a >= mean_amt * 2.0
    )

    # 조건 검증 (박스권 23%, 후반부 20% 증가, 3일 이상 순매수 유입, 2배 폭발 2회 이상)
    is_cond1 = growth_ratio >= 0.20
    is_cond2 = positive_days >= 3
    is_cond3 = spike_count >= 2

    if is_cond1 or is_cond2 or is_cond3:
      matched_tags = []
      if is_cond1:
        matched_tags.append("📦후반증가")
      if is_cond2:
        matched_tags.append("📈연속매집")
      if is_cond3:
        matched_tags.append("⚡자금폭발")

      return {
          "ticker": ticker,
          "name": stock_name,
          "price": latest_price,
          "box_ratio": round(price_range_ratio * 100, 1),
          "today_amt": today_amt,
          "total_amt_5d": total_amt_5d,
          "positive_days": positive_days,
          "tags": " ".join(matched_tags),
      }
  except Exception:
    pass
  return None


def run_stock_check():
  now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  print("==================================================")
  print(
      f" [병렬 스캔] 단기 5일 박스권(23%) + 우선주 포함 + 기관 수급 정렬 스크리닝"
      f" 실행: {now_str}"
  )
  print("==================================================")

  df_krx = get_target_stocks()
  if df_krx.empty:
    print("대상 종목 리스트를 불러오지 못했습니다.")
    return

  print(
      f"총 필터링된 타겟 종목 수: {len(df_krx)}개 (우선주 포함, 코스피/코스닥"
      " 각 상위 400개)"
  )

  end_date_dt = datetime.datetime.now()
  start_date_dt = end_date_dt - datetime.timedelta(days=20)
  start_date_str = start_date_dt.strftime("%Y-%m-%d")
  end_date_str = end_date_dt.strftime("%Y-%m-%d")

  results = []

  # 멀티스레딩(ThreadPoolExecutor)을 통한 고속 병렬 처리
  print("ThreadPoolExecutor 병렬 스캔 시작...")
  with ThreadPoolExecutor(max_workers=15) as executor:
    futures = [
        executor.submit(
            process_single_stock, row, start_date_str, end_date_str
        )
        for _, row in df_krx.iterrows()
    ]
    for future in as_completed(futures):
      res = future.result()
      if res:
        results.append(res)

  # 정렬 기준: 기관 순매수 5일 누적 금액 큰 순으로 정렬 (내림차순)
  results = sorted(results, key=lambda x: x["total_amt_5d"], reverse=True)

  # 시인성을 높인 리포트 작성 및 전송
  msg_intro = (
      f"📊 *[단기 5일 박스권(23%) + 우선주 포함 + 기관 수급 TOP 리포트]*\n"
      f"⏱️ 실행 시간: `{now_str}` (고속 멀티스레딩 적용)\n"
      f"🎯 정렬 기준: **기관 5일 누적 순매수 금액 큰 순**\n"
      f"────────────────────────"
  )
  send_telegram_message(msg_intro)

  if not results:
    send_telegram_message(
        "❌ 조건을 만족하는 종목이 없습니다 (엄격한 수급 필터 적용 중)."
    )
  else:
    chunk_msg = "🏆 *기관 순매수 상위 스크리닝 결과 (우선주 포함)*\n\n"
    for idx, item in enumerate(results, 1):
      today_mark = (
          f"🔥 +{round(item['today_amt'], 1)}억"
          if item["today_amt"] > 0
          else f"💤 {round(item['today_amt'], 1)}억"
      )

      line = (
          f"*{idx}. {item['name']}* (`{item['ticker']}`)\n"
          f" • 현재가: `{item['price']:,}원` (변동폭: {item['box_ratio']}%)\n"
          f" • 5일 누적: `+{round(item['total_amt_5d'], 1)}억`"
          f" ({item['positive_days']}/5일 유입)\n"
          f" • 당일 추정: {today_mark}\n"
          f" • 부합 조건: `{item['tags']}`\n\n"
      )

      if len(chunk_msg) + len(line) > 3800:
        send_telegram_message(chunk_msg)
        chunk_msg = ""

      chunk_msg += line

    if chunk_msg:
      send_telegram_message(chunk_msg)

  print("모든 리포트 전송 완료!")
  print("==================================================")


if __name__ == "__main__":
  run_stock_check()
