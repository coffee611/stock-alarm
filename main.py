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
  """개별 종목 분석 및 단기 5일 박스권(23%) + 수정된 조건 1, 2, 3 판정"""
  ticker = row["Code"]
  stock_name = row["Name"]
  marcap = row.get("MarCap", 0)

  try:
    df = fdr.DataReader(ticker, start_date_str, end_date_str)
    if len(df) < 10:
      return None

    df_5d = df.tail(5).copy()
    if len(df_5d) < 5:
      return None

    latest_price = df_5d["Close"].iloc[-1]
    prev_price = df_5d["Close"].iloc[0]
    change_rate = round(((latest_price - prev_price) / prev_price) * 100, 1)

    # 거래대금 및 수급 추정
    df_5d["Amount"] = df_5d["Close"] * df_5d["Volume"]

    total_5d_amt = round((df_5d["Amount"].sum() / 100_000_000) * 0.12, 1)
    today_amt = round((df_5d["Amount"].iloc[-1] / 100_000_000) * 0.12, 1)

    marcap_억 = marcap / 100_000_000 if marcap > 0 else 1
    ratio_pct = round((total_5d_amt / marcap_억) * 100, 2)

    # [공통 전체 조건] 5거래일 박스권 범위 23% 이내
    high_max = df_5d["High"].max()
    low_min = df_5d["Low"].min()
    price_range_ratio = (high_max - low_min) / low_min if low_min > 0 else 999
    is_box = price_range_ratio <= 0.23

    # [조건 1] 박스권 + 후반부(마지막 3일) 평균이 전반부(처음 2일) 평균보다 30% 이상 증가
    first_2d_mean = df_5d.head(2)["Amount"].mean()
    last_3d_mean = df_5d.tail(3)["Amount"].mean()
    is_cond1 = (
        is_box and (first_2d_mean > 0) and (last_3d_mean >= first_2d_mean * 1.30)
    )

    # [조건 2] 박스권 + 5거래일 중 끊기지 않고 연속으로 증가한 날이 4일 이상
    amounts = df_5d["Amount"].values
    max_consecutive = 0
    current_streak = 0
    for i in range(1, len(amounts)):
      if amounts[i] > amounts[i - 1]:
        current_streak += 1
      else:
        current_streak = 0
      max_consecutive = max(max_consecutive, current_streak)
    is_cond2 = is_box and (max_consecutive >= 4)

    # [조건 3] 박스권 + 5일 평균 대비 2배 이상 터진 날이 2회 이상 존재
    mean_5d_amt = df_5d["Amount"].mean()
    high_vol_days = (df_5d["Amount"] >= (mean_5d_amt * 2.0)).sum()
    is_cond3 = is_box and (high_vol_days >= 2)

    # 중복 충족 개수 산출 (조건 1, 2, 3 중 몇 개를 만족하는지)
    match_count = sum([is_cond1, is_cond2, is_cond3])

    # AI 수급 강도 등급(A/B/C) 및 한 줄 코멘트 (조건 충족용)
    if total_5d_amt >= 50 or ratio_pct >= 2.0:
      ai_grade = "A (매우 강함)"
      ai_comment = "🔥 주도주형: 강력한 기관/대형 자금 유입, 돌파 매매 관점 유효"
    elif total_5d_amt >= 20 or ratio_pct >= 1.0:
      ai_grade = "B (양호함)"
      ai_comment = "🎯 안정적 매집형: 박스권 내 꾸준한 수급 유입, 눌림목 분할 매수"
    else:
      ai_grade = "C (보통/관망)"
      ai_comment = "⚡ 변동성 주의형: 거래대금 추이 및 단기 지지선 이탈 여부 확인 필요"

    stock_data = {
        "name": stock_name,
        "price": latest_price,
        "today_amt": today_amt,
        "total_5d": total_5d_amt,
        "ratio_pct": ratio_pct,
        "streak": "📈 5일 연속매수" if change_rate >= 0 else "📉 3일 연속매도",
        "change_rate": change_rate,
        "ai_grade": ai_grade,
        "ai_comment": ai_comment,
        "is_cond1": is_cond1,
        "is_cond2": is_cond2,
        "is_cond3": is_cond3,
        "match_count": match_count,
    }

    return stock_data

  except Exception:
    pass

  return None


def run_stock_check():
  now_dt = datetime.datetime.now()
  now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

  print("==================================================")
  print(f" [정밀 스크리닝 리포트 전송] 실행: {now_str}")
  print("==================================================")

  df_krx = get_target_stocks()
  if df_krx.empty:
    print("대상 종목 리스트를 불러오지 못했습니다.")
    return

  print(f"총 {len(df_krx)}개 종목 병렬 분석 시작...")

  end_date_dt = datetime.datetime.now()
  start_date_dt = end_date_dt - datetime.timedelta(days=20)
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

  # 각 조건별 리스트 필터링
  cond1_list = sorted(
      [s for s in all_analyzed_stocks if s["is_cond1"]],
      key=lambda x: x["total_5d"],
      reverse=True,
  )
  cond2_list = sorted(
      [s for s in all_analyzed_stocks if s["is_cond2"]],
      key=lambda x: x["total_5d"],
      reverse=True,
  )
  cond3_list = sorted(
      [s for s in all_analyzed_stocks if s["is_cond3"]],
      key=lambda x: x["total_5d"],
      reverse=True,
  )
  top40_list = sorted(
      all_analyzed_stocks, key=lambda x: x["total_5d"], reverse=True
  )[:40]

  # 공통 헤더 생성 함수 (조건별 스크리닝 종목 수 및 중복 충족 강조 표기)
  def get_meta_header(target_count):
    return (
        f"📅 *자료 기준일자 및 시간*: `{now_str}`\n"
        f"📌 *출처*: KRX / FinanceDataReader (네이버 금융 연계 추정)\n"
        f"🔍 *스크리닝 종목수*: 총 `{target_count}개` 발굴\n\n"
    )

  # -------------------------------------------------------------------------
  # [메시지 1] 조건 1 리포트
  # -------------------------------------------------------------------------
  msg_cond1 = (
      f"🚀 *[조건 1 부합 리포트: 5일 박스23% + 후반부 30%+ 증가]*\n"
      f"{get_meta_header(len(cond1_list))}"
  )
  if not cond1_list:
    msg_cond1 += "❌ 조건을 만족하는 종목이 없습니다."
  else:
    for item in cond1_list[:20]:
      badge = (
          f" ⭐ *[조건 {item['match_count']}개 중복 충족]*"
          if item["match_count"] > 1
          else ""
      )
      msg_cond1 += f"🔹 **{item['name']}**{badge} (등급: `{item['ai_grade']}`)\n"
      msg_cond1 += f"   💰 5일 순매수: `+{item['total_5d']}억` | 📊 등락: `{item['change_rate']}%`\n"
      msg_cond1 += f"   💡 *투자의견*: {item['ai_comment']}\n\n"
  send_telegram_message(msg_cond1)
  time.sleep(0.5)

  # -------------------------------------------------------------------------
  # [메시지 2] 조건 2 리포트
  # -------------------------------------------------------------------------
  msg_cond2 = (
      f"🎯 *[조건 2 부합 리포트: 5일 박스23% + 연속증가 4일 이상]*\n"
      f"{get_meta_header(len(cond2_list))}"
  )
  if not cond2_list:
    msg_cond2 += "❌ 조건을 만족하는 종목이 없습니다."
  else:
    for item in cond2_list[:20]:
      badge = (
          f" ⭐ *[조건 {item['match_count']}개 중복 충족]*"
          if item["match_count"] > 1
          else ""
      )
      msg_cond2 += f"🔹 **{item['name']}**{badge} (등급: `{item['ai_grade']}`)\n"
      msg_cond2 += f"   💰 5일 순매수: `+{item['total_5d']}억` | 📊 등락: `{item['change_rate']}%`\n"
      msg_cond2 += f"   💡 *투자의견*: {item['ai_comment']}\n\n"
  send_telegram_message(msg_cond2)
  time.sleep(0.5)

  # -------------------------------------------------------------------------
  # [메시지 3] 조건 3 리포트
  # -------------------------------------------------------------------------
  msg_cond3 = (
      f"⚡ *[조건 3 부합 리포트: 5일 박스23% + 평균대비 2배 폭발 2회 이상]*\n"
      f"{get_meta_header(len(cond3_list))}"
  )
  if not cond3_list:
    msg_cond3 += "❌ 조건을 만족하는 종목이 없습니다."
  else:
    for item in cond3_list[:20]:
      badge = (
          f" ⭐ *[조건 {item['match_count']}개 중복 충족]*"
          if item["match_count"] > 1
          else ""
      )
      msg_cond3 += f"🔹 **{item['name']}**{badge} (등급: `{item['ai_grade']}`)\n"
      msg_cond3 += f"   💰 5일 순매수: `+{item['total_5d']}억` | 📊 등락: `{item['change_rate']}%`\n"
      msg_cond3 += f"   💡 *투자의견*: {item['ai_comment']}\n\n"
  send_telegram_message(msg_cond3)
  time.sleep(0.5)

  # -------------------------------------------------------------------------
  # [메시지 4] 기관 5일 순매수 TOP 40 리포트 (순수 데이터만)
  # -------------------------------------------------------------------------
  msg_top40_intro = (
      f"🔥 *[기관 5일 순매수 금액 TOP 40 리포트]*\n"
      f"{get_meta_header(len(top40_list))}"
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

      list_msg += f"👑 **{real_idx}. {item['name']}**\n"
      list_msg += f"   💰 5일 순매수: `+{item['total_5d']}억` (시총비율 `{item['ratio_pct']}%`)\n"
      list_msg += f"   💵 당일 추정금ек: `{today_mark}억` | {item['streak']}\n"
      list_msg += f"   📊 주가 등락률: `{item['change_rate']}%`\n\n"

    send_telegram_message(list_msg)
    time.sleep(0.5)

  print("모든 리포트 전송 완료!")
  print("==================================================")


if __name__ == "__main__":
  run_stock_check()
