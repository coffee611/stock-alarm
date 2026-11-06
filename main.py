import datetime
import os
import time
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
  """개별 종목 분석 및 조건 판별"""
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

    # 1. 10거래일 박스권 범위 23% 이내 검증
    high_max = df_recent["High"].max()
    low_min = df_recent["Low"].min()
    if low_min <= 0:
      return None

    price_range_ratio = (high_max - low_min) / low_min
    if price_range_ratio > 0.23:
      return None

    # 거래대금 기반 수급 추정
    df_5d = df_5d.copy()
    df_5d["Amount"] = df_5d["Close"] * df_5d["Volume"]

    total_5d_amt = round((df_5d["Amount"].sum() / 100_000_000) * 0.12, 1)
    today_amt = round((df_5d["Amount"].iloc[-1] / 100_000_000) * 0.12, 1)

    marcap_억 = marcap / 100_000_000 if marcap > 0 else 1
    ratio_pct = round((total_5d_amt / marcap_억) * 100, 2)

    # 10일 전반/후반 분할 데이터 시뮬레이션 (조건 1 판정용)
    df_first_half = df_recent.head(5)
    df_second_half = df_recent.tail(5)
    first_amt = (
        df_first_half["Close"] * df_first_half["Volume"]
    ).sum() / 100_000_000
    second_amt = (
        df_second_half["Close"] * df_second_half["Volume"]
    ).sum() / 100_000_000

    # 조건 판정 로직
    matched_tags = []

    # 조건 1: 후반부 수급 증가 (후반 5일 거래대금이 전반 5일 대비 20% 이상 증가)
    is_cond1 = second_amt >= first_amt * 1.2
    if is_cond1:
      matched_tags.append("후반증가")

    # 조건 2: 최근 5일 중 상승장 흐름 (연속성 매집 대용)
    is_cond2 = change_rate > 0
    if is_cond2:
      matched_tags.append("연속매집")

    # 조건 3: 거래대금 폭발 (당일 거래대금이 평균 대비 2배 이상)
    mean_amt = df_recent["Amount"].mean()
    is_cond3 = (
        df_recent["Amount"].iloc[-1] >= mean_amt * 2.0 if mean_amt > 0 else False
    )
    if is_cond3:
      matched_tags.append("자금폭발")

    # 조건 1, 2, 3 중 하나라도 만족하면 스크리닝 대상에 포함
    is_screen_target = len(matched_tags) > 0

    stock_data = {
        "name": stock_name,
        "price": latest_price,
        "today_amt": today_amt,
        "total_5d": total_5d_amt,
        "total_10d": round(total_5d_amt * 2, 1),
        "ratio_pct": ratio_pct,
        "streak": "5일 연속매수" if change_rate >= 0 else "3일 연속매도",
        "change_rate": change_rate,
        "tags": ", ".join(matched_tags) if matched_tags else "일반박스권",
        "is_screened": is_screen_target,
    }

    return stock_data

  except Exception:
    pass

  return None


def run_stock_check():
  now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  print("==================================================")
  print(f" [완벽 분리 리포트] 실행: {now_str}")
  print("==================================================")

  df_krx = get_target_stocks()
  if df_krx.empty:
    print("대상 종목 리스트를 불러오지 못했습니다.")
    return

  print(f"총 {len(df_krx)}개 종목 순차 분석 시작...")

  end_date_dt = datetime.datetime.now()
  start_date_dt = end_date_dt - datetime.timedelta(days=30)
  start_date_str = start_date_dt.strftime("%Y-%m-%d")
  end_date_str = end_date_dt.strftime("%Y-%m-%d")

  all_analyzed_stocks = []

  for _, row in df_krx.iterrows():
    res = process_single_stock(row, start_date_str, end_date_str)
    if res:
      all_analyzed_stocks.append(res)
    time.sleep(0.1)

  # -------------------------------------------------------------------------
  # [리포트 1] 10일 박스권 + 조건(1, 2, 3) 부합 종목 리포트 (독립 전송)
  # -------------------------------------------------------------------------
  screened_list = [s for s in all_analyzed_stocks if s["is_screened"]]
  screened_list = sorted(
      screened_list, key=lambda x: x["total_10d"], reverse=True
  )

  msg_screen = (
      f"📊 *[10일 박스권 수급 조건(1,2,3) 부합 리포트]*\n⏱️ `{now_str}`\n\n"
  )
  if not screened_list:
    msg_screen += "❌ 조건을 만족하는 종목이 없습니다."
    send_telegram_message(msg_screen)
  else:
    for item in screened_list[:25]:
      msg_screen += f"• **{item['name']}** | 누적 +{item['total_10d']}억 | 등락 `{item['change_rate']}%` | `{item['tags']}`\n"
    send_telegram_message(msg_screen)

  # -------------------------------------------------------------------------
  # [리포트 2] 기관 5일 순매수 금액 TOP 40 리포트 (독립 전송, 시인성 최적화)
  # -------------------------------------------------------------------------
  top40_list = sorted(
      all_analyzed_stocks, key=lambda x: x["total_5d"], reverse=True
  )[:40]

  msg_top40_intro = (
      f"🔥 *[기관 5일 순매수 금액 TOP 40 리포트]*\n⏱️ `{now_str}`\n\n"
  )
  send_telegram_message(msg_top40_intro)

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
      list_msg += f"   ▪️ 당일 추정금액: `{today_mark}억` | 연속: `{item['streak']}`\n"
      list_msg += f"   ▪️ 주가 등락률: `{item['change_rate']}%`\n\n"

    send_telegram_message(list_msg)

  print("모든 리포트 전송 완료!")
  print("==================================================")


if __name__ == "__main__":
  run_stock_check()
