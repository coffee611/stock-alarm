import datetime
import os
import FinanceDataReader as fdr
from pykrx import stock
import requests

# 깃허브 시크릿에서 텔레그램 토큰 및 챗 ID 가져오기
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram_message(message):
  """텔레그램 메시지 전송 함수"""
  if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("[경고] 텔레그램 토큰 또는 챗 ID가 설정되지 않았습니다.")
    return

  url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
  payload = {
      "chat_id": TELEGRAM_CHAT_ID,
      "text": message,
      "parse_mode": "Markdown",
  }

  try:
    response = requests.post(url, data=payload, timeout=10)
    if response.status_code == 200:
      print("텔레그램 알림 전송 성공!")
    else:
      print(f"텔레그램 알림 전송 실패: {response.text}")
  except Exception as e:
    print(f"텔레그램 전송 중 오류 발생: {e}")


def run_stock_check():
  now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  print("==================================================")
  print(f" [박스권 20% + 수급 및 거래대금 조건별 스크리닝] 실행: {now_str}")
  print("==================================================")

  try:
    df_krx = fdr.StockListing("KRX")
    if "MarCap" in df_krx.columns:
      df_krx = df_krx.sort_values(by="MarCap", ascending=False)
      target_stocks = df_krx.head(200)
      tickers = target_stocks["Code"].dropna().tolist()
      print("시가총액 상위 200개 종목 대상 스캔 시작")
    else:
      tickers = df_krx["Code"].dropna().tolist()[:200]
  except Exception as e:
    print(f"종목 리스트 불러오기 오류: {e}")
    tickers = ["005930", "000660", "035420", "035720", "051910"]

  # pykrx를 통한 기관 순매수 데이터 가져오기 (출처: pykrx 라이브러리)
  end_date_dt = datetime.datetime.now()
  start_date_dt = end_date_dt - datetime.timedelta(days=35)
  start_str = start_date_dt.strftime("%Y%m%d")
  end_str = end_date_dt.strftime("%Y%m%d")

  inst_net_dict = {}
  try:
    df_kospi_inst = stock.get_market_net_purchases_of_equities_by_ticker(
        start_str, end_str, "KOSPI"
    )
    df_kosdaq_inst = stock.get_market_net_purchases_of_equities_by_ticker(
        start_str, end_str, "KOSDAQ"
    )
    for df_inst in [df_kospi_inst, df_kosdaq_inst]:
      if not df_inst.empty and "기관합계" in df_inst.columns:
        for ticker, row in df_inst.iterrows():
          inst_net_dict[ticker] = row["기관합계"]
  except Exception as e:
    print(f"pykrx 기관 순매수 데이터 로드 중 오류 발생: {e}")

  cond1_results = []  # 조건 1: 박스권(20%) + 후반부 거래대금/량 15%↑
  cond2_results = []  # 조건 2: 박스권(20%) + 증가일 10일 이상
  cond3_results = []  # 조건 3: 박스권(20%) + 2배 이상 폭발 1회 이상

  for ticker in tickers:
    try:
      df = fdr.DataReader(
          ticker,
          start_date_dt.strftime("%Y-%m-%d"),
          end_date_dt.strftime("%Y-%m-%d"),
      )
      if len(df) < 20:
        continue

      df_recent = df.tail(20)
      latest_price = df_recent["Close"].iloc[-1]

      stock_row = df_krx[df_krx["Code"] == ticker]
      stock_name = (
          stock_row["Name"].values[0] if not stock_row.empty else ticker
      )

      # 1. 공통 전제 조건: 20거래일 박스권 범위 20% 이내
      high_max = df_recent["High"].max()
      low_min = df_recent["Low"].min()
      price_range_ratio = (high_max - low_min) / low_min
      is_box = price_range_ratio <= 0.20

      if not is_box:
        continue

      # 2. 거래대금 우선 고려 (없으면 거래량 사용)
      if "Amount" in df_recent.columns:
        target_series = df_recent["Amount"]
        unit_str = "원"
      else:
        target_series = df_recent["Volume"]
        unit_str = "주"

      inst_net_buy = inst_net_dict.get(ticker, 0)
      inst_str = f"기관 누적 순매수: {inst_net_buy:,}주(원)"

      # --- [조건 1] 박스권 + 후반부 10일 평균이 전반부 10일 평균보다 15% 이상 증가 ---
      first_half = target_series.head(10).mean()
      second_half = target_series.tail(10).mean()
      growth_ratio = (
          (second_half - first_half) / first_half if first_half > 0 else 0
      )
      if growth_ratio >= 0.15:
        cond1_results.append(
            f"• *{stock_name}* (`{ticker}`)\n  - 현재가: {latest_price:,}원\n -"
            f" 20일 변동폭: {round(price_range_ratio*100, 1)}%\n  - 후반부 증가율:"
            f" +{round(growth_ratio*100, 1)}%\n  - 📊 {inst_str}"
        )

      # --- [조건 2] 박스권 + 20거래일 중 전일 대비 증가한 날이 10일 이상 ---
      vol_diff = target_series.diff() > 0
      inc_days_count = vol_diff.sum()
      if inc_days_count >= 10:
        cond2_results.append(
            f"• *{stock_name}* (`{ticker}`)\n  - 현재가: {latest_price:,}원\n -"
            f" 20일 변동폭: {round(price_range_ratio*100, 1)}%\n  - 증가일수:"
            f" {inc_days_count}/20일\n  - 📊 {inst_str}"
        )

      # --- [조건 3] 박스권 + 20일 평균 대비 2배 이상 터진 날이 1회 이상 존재 ---
      avg_val = target_series.mean()
      max_val = target_series.max()
      is_spike = avg_val > 0 and (max_val >= avg_val * 2.0)
      if is_spike:
        cond3_results.append(
            f"• *{stock_name}* (`{ticker}`)\n  - 현재가: {latest_price:,}원\n -"
            f" 20일 변동폭: {round(price_range_ratio*100, 1)}%\n  - 최대 폭발:"
            f" 평균 대비 {round(max_val / avg_val, 1)}배\n  - 📊 {inst_str}"
        )

    except Exception:
      pass

  # 텔레그램 메시지 구성
  msg_parts = [
      f"📊 *[박스권 20% + 조건별 수급/거래대금 리포트]* ({now_str})\n"
  ]
  msg_parts.append(
      f"💡 *수급 데이터 출처*: `pykrx` (KRX 공식 기관 순매수 연동)\n"
      f"💡 *거래 지표*: 거래대금 우선 적용 (없을 시 거래량)\n"
  )

  msg_parts.append(
      f"📦 *[조건 1] 박스권(20%) + 후반부 10일 평균 거래대금 15% 이상 증가*\n"
      + (
          "\n".join(cond1_results)
          if cond1_results
          else "조건을 만족하는 종목이 없습니다."
      )
      + "\n"
  )

  msg_parts.append(
      f"📈 *[조건 2] 박스권(20%) + 거래대금 증가일 10일 이상*\n"
      + (
          "\n".join(cond2_results)
          if cond2_results
          else "조건을 만족하는 종목이 없습니다."
      )
      + "\n"
  )

  msg_parts.append(
      f"⚡ *[조건 3] 박스권(20%) + 평균 거래대금 2배 이상 폭발 (1회 이상)*\n"
      + (
          "\n".join(cond3_results)
          if cond3_results
          else "조건을 만족하는 종목이 없습니다."
      )
  )

  final_msg = "\n".join(msg_parts)
  print(final_msg)
  send_telegram_message(final_msg)
  print("==================================================")


if __name__ == "__main__":
  run_stock_check()
