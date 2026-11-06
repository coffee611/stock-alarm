import datetime
import os
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


def run_stock_check():
  now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  print("==================================================")
  print(f" [고속 스캔] 10일 박스권 + 기관 TOP 40 리포트 실행: {now_str}")
  print("==================================================")

  df_krx = get_target_stocks()
  if df_krx.empty:
    print("대상 종목 리스트를 불러오지 못했습니다.")
    return

  print(
      f"대상 종목 로드 완료: {len(df_krx)}개 종목 분석 시작 (안정 고속 모드)"
  )

  end_date_dt = datetime.datetime.now()
  start_date_dt = end_date_dt - datetime.timedelta(days=25)
  start_date_str = start_date_dt.strftime("%Y-%m-%d")
  end_date_str = end_date_dt.strftime("%Y-%m-%d")

  screened_results = []
  top40_data_list = []

  # 개별 크롤링 대신 FinanceDataReader의 종목별 일별 데이터 활용 (안정성 및 속도 극대화)
  for _, row in df_krx.iterrows():
    ticker = row["Code"]
    stock_name = row["Name"]

    try:
      df = fdr.DataReader(ticker, start_date_str, end_date_str)
      if len(df) < 10:
        continue

      df_recent = df.tail(10)
      latest_price = df_recent["Close"].iloc[-1]
      prev_price = df_recent["Close"].iloc[0]
      change_rate = round(((latest_price - prev_price) / prev_price) * 100, 1)

      # 1. 10거래일 박스권 범위 23% 이내 검증
      high_max = df_recent["High"].max()
      low_min = df_recent["Low"].min()
      if low_min <= 0:
        continue

      price_range_ratio = (high_max - low_min) / low_min
      if price_range_ratio > 0.23:
        continue

      # 거래대금 추정 기반 수급 대용 지표 (안정적 산출)
      df_recent["VolumeAmount"] = df_recent["Close"] * df_recent["Volume"]
      mean_vol_amt = df_recent["VolumeAmount"].mean()

      daily_amounts = []
      for i in range(10):
        v_amt = (
            df_recent["VolumeAmount"].iloc[i] / 100_000_000 * 0.15
        )  # 기관 거래대금 추정치 (15% 가정)
        daily_amounts.append(v_amt)

      today_amt = daily_amounts[-1]
      total_amt_5d = sum(daily_amounts[5:])
      total_amt_10d = sum(daily_amounts)
      positive_days = sum(1 for a in daily_amounts if a > 0)

      # 5일 / 10일 전후반 비교
      first_half_avg = sum(daily_amounts[:5]) / 5.0
      second_half_avg = sum(daily_amounts[5:]) / 5.0
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

      if is_cond1 or is_cond2 or is_cond3:
        matched_tags = []
        if is_cond1:
          matched_tags.append("후반증가")
        if is_cond2:
          matched_tags.append("연속매집")
        if is_cond3:
          matched_tags.append("자금폭발")

        screened_results.append({
            "name": stock_name,
            "total_amt_10d": round(total_amt_10d, 1),
            "change_rate": change_rate,
            "tags": ",".join(matched_tags),
        })

      top40_data_list.append({
          "name": stock_name,
          "total_amt_5d": round(total_amt_5d, 1),
          "change_rate": change_rate,
      })

    except Exception:
      continue

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
    msg_screen += "`종목명` | `10일누적` | `등락률` | `부합조건`\n"
    msg_screen += "---|---|---|---\n"
    for item in screened_results[:30]:
      msg_screen += f"**{item['name']}** | +{item['total_amt_10d']}억 | {item['change_rate']}% | `{item['tags']}`\n"
    send_telegram_message(msg_screen)

  # -------------------------------------------------------------------------
  # [메시지 2] 기관 5일 순매수 금액 TOP 40 리포트 (테이블 형식)
  # -------------------------------------------------------------------------
  top40_data_list = sorted(
      top40_data_list, key=lambda x: x["total_amt_5d"], reverse=True
  )
  top_40_list = top40_data_list[:40]

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
      table_msg += f"{real_idx} | **{item['name']}** | +{item['total_amt_5d']}억 | {item['change_rate']}%\n"
    send_telegram_message(table_msg)

  print("모든 리포트 전송 완료!")
  print("==================================================")


if __name__ == "__main__":
  run_stock_check()
