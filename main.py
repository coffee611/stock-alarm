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


def get_naver_institutional_amount_10days(ticker, recent_df):
  """네이버 금융에서 최근 10거래일 기관 순매수 금액(억원) 추정 및 분석"""
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
          if len(df) < 10:
            continue

          amounts = []
          for i in range(10):
            row_val = df.iloc[i][inst_col]
            close_price = recent_df["Close"].iloc[-(10 - i)]
            try:
              val_str = str(row_val).replace(",", "").strip()
              shares = int(val_str[1:]) if val_str.startswith("+") else int(val_str)
              amount_억 = (shares * close_price) / 100_000_000
              amounts.append(amount_억)
            except:
              amounts.append(0.0)

          if not amounts:
            return 0.0, 0.0, 0.0, 0, []

          today_amount = amounts[0]
          total_5d = sum(amounts[:5])
          total_10d = sum(amounts)
          positive_days = sum(1 for a in amounts if a > 0)

          return today_amount, total_5d, total_10d, positive_days, amounts
  except Exception:
    pass
  return 0.0, 0.0, 0.0, 0, []


def process_single_stock(row, start_date_str, end_date_str):
  """개별 종목 데이터를 병렬로 분석하는 함수 (10일 기준)"""
  ticker = row["Code"]
  stock_name = row["Name"]

  try:
    df = fdr.DataReader(ticker, start_date_str, end_date_str)
    if len(df) < 10:
      return None, None

    df_recent = df.tail(10)
    latest_price = df_recent["Close"].iloc[-1]
    prev_price = df_recent["Close"].iloc[0]
    change_rate = round(((latest_price - prev_price) / prev_price) * 100, 1)

    # 1. 공통 전제 조건: 최근 10거래일 박스권 범위 23% 이내
    high_max = df_recent["High"].max()
    low_min = df_recent["Low"].min()
    if low_min <= 0:
      return None, None

    price_range_ratio = (high_max - low_min) / low_min
    if price_range_ratio > 0.23:
      return None, None

    today_amt, total_amt_5d, total_amt_10d, positive_days, daily_amounts = (
        get_naver_institutional_amount_10days(ticker, df_recent)
    )
    if not daily_amounts or len(daily_amounts) < 10:
      return None, None

    # 10일 데이터 분할 (전반 5일 vs 후반 5일)
    first_half_avg = sum(daily_amounts[5:]) / 5.0
    second_half_avg = sum(daily_amounts[:5]) / 5.0
    growth_ratio = (
        (second_half_avg - first_half_avg) / abs(first_half_avg)
        if first_half_avg != 0
        else 0
    )

    mean_amt = sum(daily_amounts) / 10.0
    spike_count = sum(
        1 for a in daily_amounts if mean_amt > 0 and a >= mean_amt * 2.0
    )

    is_cond1 = growth_ratio >= 0.20
    is_cond2 = positive_days >= 6
    is_cond3 = spike_count >= 2

    screen_result = None
    if is_cond1 or is_cond2 or is_cond3:
      matched_tags = []
      if is_cond1:
        matched_tags.append("후반증가")
      if is_cond2:
        matched_tags.append("연속매집")
      if is_cond3:
        matched_tags.append("자금폭발")

      screen_result = {
          "ticker": ticker,
          "name": stock_name,
          "price": latest_price,
          "box_ratio": round(price_range_ratio * 100, 1),
          "today_amt": today_amt,
          "total_amt_10d": total_amt_10d,
          "positive_days": positive_days,
          "change_rate": change_rate,
          "tags": ",".join(matched_tags),
      }

    top40_data = {
        "ticker": ticker,
        "name": stock_name,
        "price": latest_price,
        "total_amt_5d": total_amt_5d,
        "today_amt": today_amt,
        "positive_days": positive_days,
        "change_rate": change_rate,
    }

    return screen_result, top40_data

  except Exception:
    pass
  return None, None


def run_stock_check():
  now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  print("==================================================")
  print(f" [병렬 스캔] 10일 박스권 + 통합 테이블 및 TOP 40 리포트 실행: {now_str}")
  print("==================================================")

  df_krx = get_target_stocks()
  if df_krx.empty:
    print("대상 종목 리스트를 불러오지 못했습니다.")
    return

  end_date_dt = datetime.datetime.now()
  start_date_dt = end_date_dt - datetime.timedelta(days=30)
  start_date_str = start_date_dt.strftime("%Y-%m-%d")
  end_date_str = end_date_dt.strftime("%Y-%m-%d")

  screened_results = []
  all_top40_data = []

  with ThreadPoolExecutor(max_workers=15) as executor:
    futures = [
        executor.submit(
            process_single_stock, row, start_date_str, end_date_str
        )
        for _, row in df_krx.iterrows()
    ]
    for future in as_completed(futures):
      screen_res, top40_res = future.result()
      if screen_res:
        screened_results.append(screen_res)
      if top40_res:
        all_top40_data.append(top40_res)

  # -------------------------------------------------------------------------
  # [메시지 1] 조건 1, 2, 3 부합 종목 통합 테이블 리포트
  # -------------------------------------------------------------------------
  screened_results = sorted(
      screened_results, key=lambda x: x["total_amt_10d"], reverse=True
  )

  msg_screen = (
      f"📊 *[10일 박스권 수급 스크리닝 (조건 1,2,3 통합)]*\n"
      f"⏱️ `{now_str}` (10일 누적 순매수순)\n\n"
  )
  if not screened_results:
    msg_screen += "❌ 조건을 만족하는 종목이 없습니다."
    send_telegram_message(msg_screen)
  else:
    # 텔레그램 메시지 길이 제한을 고려하여 상위 30개 종목을 하나의 표로 전송
    msg_screen += "`종목명` | `10일누적` | `등락률` | `부합조건`\n"
    msg_screen += "---|---|---|---\n"
    for item in screened_results[:30]:
      msg_screen += f"**{item['name']}** | +{round(item['total_amt_10d'], 1)}억 | {item['change_rate']}% | `{item['tags']}`\n"
    send_telegram_message(msg_screen)

  # -------------------------------------------------------------------------
  # [메시지 2] 기관 5일 순매수 금액 TOP 40 리포트 (테이블 형식)
  # -------------------------------------------------------------------------
  all_top40_data = sorted(
      all_top40_data, key=lambda x: x["total_amt_5d"], reverse=True
  )
  top_40_list = all_top40_data[:40]

  msg_top40_intro = f"🔥 *[기관 5일 순매수 금액 TOP 40 리포트]*\n⏱️ `{now_str}`\n\n"
  send_telegram_message(msg_top40_intro)

  for chunk_idx, sub_list in enumerate(
      [top_40_list[:20], top_40_list[20:40]], 1
  ):
    if not sub_list:
      continue
    table_msg = (
        f"📊 *TOP 40 순위 ({1 if chunk_idx==1 else 21} ~"
        f" {20 if chunk_idx==1 else 40}위)*\n"
    )
    table_msg += "`#` | `종목명` | `기관 5일(억)` | `등락률`\n"
    table_msg += "---|---|---|---\n"
    for idx, item in enumerate(sub_list, 1):
      real_idx = idx if chunk_idx == 1 else idx + 20
      table_msg += f"{real_idx} | **{item['name']}** | +{round(item['total_amt_5d'], 1)}억 | {item['change_rate']}%\n"
    send_telegram_message(table_msg)

  print("모든 리포트 전송 완료!")
  print("==================================================")


if __name__ == "__main__":
  run_stock_check()
