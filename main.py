import datetime
import os
import time
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
      time.sleep(0.5)
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


def process_single_stock(row, start_date_str, end_date_str):
  """개별 종목 분석 및 조건 1, 2, 3 개별 판정"""
  ticker = row["Code"]
  stock_name = row["Name"]
  marcap = row.get("MarCap", 0)

  try:
    df = fdr.DataReader(ticker, start_date_str, end_date_str)
    if len(df) < 10:
      return None

    df_recent = df.tail(10)
    df_5d = df.tail(5)
    latest_price = df_5d["Close"].iloc[-1]
    prev_price = df_5d["Close"].iloc[0]
    change_rate = round(((latest_price - prev_price) / prev_price) * 100, 1)

    # 거래대금 기반 수급 추정
    df_recent = df_recent.copy()
    df_recent["Amount"] = df_recent["Close"] * df_recent["Volume"]
    df_5d = df_recent.tail(5)

    total_5d_amt = round((df_5d["Amount"].sum() / 100_000_000) * 0.15, 1)
    today_amt = round((df_5d["Amount"].iloc[-1] / 100_000_000) * 0.15, 1)

    marcap_억 = marcap / 100_000_000 if marcap > 0 else 1
    ratio_pct = round((total_5d_amt / marcap_억) * 100, 2)

    # 박스권 검증 (10일간 (고가-저가)/저가 <= 23%)
    high_max = df_recent["High"].max()
    low_min = df_recent["Low"].min()
    price_range_ratio = (high_max - low_min) / low_min if low_min > 0 else 999
    is_box = price_range_ratio <= 0.23

    # -------------------------------------------------------------------------
    # 조건별 개별 판정 로직
    # -------------------------------------------------------------------------
    df_first_half = df_recent.head(5)
    df_second_half = df_recent.tail(5)
    first_amt = df_first_half["Amount"].sum()
    second_amt = df_second_half["Amount"].sum()
    mean_amt = df_recent["Amount"].mean()

    # 조건 1: 10일 박스권 안에서 후반부(최근 5일) 거래대금/수급이 전반부 대비 15% 이상 증가
    is_cond1 = is_box and (second_amt >= first_amt * 1.15)

    # 조건 2: 최근 5일간 주가 흐름이 안정적이고 연속 매집 형태 (등락률 -2% 이상, 우상향 기조)
    is_cond2 = is_box and (change_rate >= -1.0) and (total_5d_amt >= 5.0)

    # 조건 3: 당일 거래대금 및 수급 유입 폭발 (당일 거래대금이 10일 평균 대비 1.8배 이상)
    is_cond3 = (
        is_box
        and (df_recent["Amount"].iloc[-1] >= mean_amt * 1.8)
        if mean_amt > 0
        else False
    )

    stock_data = {
        "name": stock_name,
        "price": latest_price,
        "today_amt": today_amt,
        "total_5d": total_5d_amt,
        "ratio_pct": ratio_pct,
        "streak": "5일 연속매수" if change_rate >= 0 else "조정 매집",
        "change_rate": change_rate,
        "is_cond1": is_cond1,
        "is_cond2": is_cond2,
        "is_cond3": is_cond3,
    }

    return stock_data

  except Exception:
    pass

  return None


def run_stock_check():
  now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  print("==================================================")
  print(f" [4개 독립 메시지 리포트] 실행: {now_str}")
  print("==================================================")

  df_krx = get_target_stocks()
  if df_krx.empty:
    print("대상 종목 리스트를 불러오지 못했습니다.")
    return

  print(f"총 {len(df_krx)}개 종목 병렬 분석 시작...")

  end_date_dt = datetime.datetime.now()
  start_date_dt = end_date_dt - datetime.timedelta(days=30)
  start_date_str = start_date_dt.strftime("%Y-%m-%d")
  end_date_str = end_date_dt.strftime("%Y-%m-%d")

  all_analyzed_stocks = []

  with ThreadPoolExecutor(max_workers=10) as executor:
    futures = [
        executor.submit(process_single_stock, row, start_date_str, end_date_str)
        for _, row in df_krx.iterrows()
    ]
    for future in as_completed(futures):
      res = future.result()
      if res:
        all_analyzed_stocks.append(res)

  # -------------------------------------------------------------------------
  # [메시지 1] 조건 1 부합 종목 리포트 (독립 전송)
  # -------------------------------------------------------------------------
  cond1_list = sorted(
      [s for s in all_analyzed_stocks if s["is_cond1"]],
      key=lambda x: x["total_5d"],
      reverse=True,
  )
  msg_cond1 = (
      f"📊 *[조건 1 부합 리포트: 후반부 수급 증가]*\n⏱️ `{now_str}`\n\n"
  )
  if not cond1_list:
    msg_cond1 += "❌ 조건을 만족하는 종목이 없습니다."
  else:
    for item in cond1_list[:20]:
      msg_cond1 += f"• **{item['name']}** | 5일 +{item['total_5d']}억 | 등락 `{item['change_rate']}%`\n"
  send_telegram_message(msg_cond1)
  time.sleep(0.5)

  # -------------------------------------------------------------------------
  # [메시지 2] 조건 2 부합 종목 리포트 (독립 전송)
  # -------------------------------------------------------------------------
  cond2_list = sorted(
      [s for s in all_analyzed_stocks if s["is_cond2"]],
      key=lambda x: x["total_5d"],
      reverse=True,
  )
  msg_cond2 = (
      f"📊 *[조건 2 부합 리포트: 박스권 안정 매집]*\n⏱️ `{now_str}`\n\n"
  )
  if not cond2_list:
    msg_cond2 += "❌ 조건을 만족하는 종목이 없습니다."
  else:
    for item in cond2_list[:20]:
      msg_cond2 += f"• **{item['name']}** | 5일 +{item['total_5d']}억 | 등락 `{item['change_rate']}%`\n"
  send_telegram_message(msg_cond2)
  time.sleep(0.5)

  # -------------------------------------------------------------------------
  # [메시지 3] 조건 3 부합 종목 리포트 (독립 전송)
  # -------------------------------------------------------------------------
  cond3_list = sorted(
      [s for s in all_analyzed_stocks if s["is_cond3"]],
      key=lambda x: x["total_5d"],
      reverse=True,
  )
  msg_cond3 = (
      f"📊 *[조건 3 부합 리포트: 당일 거래대금 폭발]*\n⏱️ `{now_str}`\n\n"
  )
  if not cond3_list:
    msg_cond3 += "❌ 조건을 만족하는 종목이 없습니다."
  else:
    for item in cond3_list[:20]:
      msg_cond3 += f"• **{item['name']}** | 5일 +{item['total_5d']}억 | 등락 `{item['change_rate']}%`\n"
  send_telegram_message(msg_cond3)
  time.sleep(0.5)

  # -------------------------------------------------------------------------
  # [메시지 4] 기관 5일 순매수 TOP 40 리포트 (조건 제한 없는 순수 순매수 크기순, 독립 전송)
  # -------------------------------------------------------------------------
  top40_list = sorted(
      all_analyzed_stocks, key=lambda x: x["total_5d"], reverse=True
  )[:40]

  msg_top40_intro = (
      f"🔥 *[기관 5일 순매수 금액 TOP 40 리포트]*\n⏱️ `{now_str}`\n\n"
  )
  send_telegram_message(msg_top40_intro)
  time.sleep(0.5)

  for chunk_idx, sub_list in enumerate(
      [top40_list[:20], top40_list[20:40]], 1
  ):
    if not sub_list:
      continue
    list_msg = (
        f"📋 *TOP 40 순위 ({1 if chunk_idx==1 else 21} ~"
        f" {20 if chunk_idx==1 else 40}위)*\n\n"
    )
    for idx, item in enumerate(sub_list, 1):
      real_idx = idx if chunk_idx == 1 else idx + 20
      today_mark = (
          f"+{item['today_amt']}" if item["today_amt"] > 0 else f"{item['today_amt']}"
      )

      list_msg += f"🔹 **{real_idx}. {item['name']}**\n"
      list_msg += f"   ▪️ 5일 순매수: `+{item['total_5d']}억` (시총비율 `{item['ratio_pct']}%`)\n"
      list_msg += f"   ▪️ 당일 추정금액: `{today_mark}억` | 상태: `{item['streak']}`\n"
      list_msg += f"   ▪️ 주가 등락률: `{item['change_rate']}%`\n\n"

    send_telegram_message(list_msg)
    time.sleep(0.5)

  print("모든 리포트 전송 완료!")
  print("==================================================")


if __name__ == "__main__":
  run_stock_check()
