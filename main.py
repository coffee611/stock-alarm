import datetime
import FinanceDataReader as fdr


def run_stock_check():
  now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  print("==================================================")
  print(f" [주식 조건 검색 리포트] 실행 시각: {now_str}")
  print("==================================================")

  try:
    df_krx = fdr.StockListing("KRX")
    target_stocks = df_krx[df_krx["Market"].isin(["KOSPI", "KOSDAQ"])]
    tickers = target_stocks["Code"].dropna().tolist()[:300]
  except Exception as e:
    tickers = ["005930", "000660", "035420", "035720", "051910"]

  results = []
  print(f"총 {len(tickers)}개 종목에 대해 조건(수급 + 박스권) 검색을 시작합니다...")

  for ticker in tickers:
    try:
      end_date = datetime.datetime.now()
      start_date = end_date - datetime.timedelta(days=25)
      df = fdr.DataReader(ticker, start_date, end_date)

      if len(df) < 10:
        continue

      df = df.tail(10)
      high_max = df["High"].max()
      low_min = df["Low"].min()
      price_range_ratio = (high_max - low_min) / low_min
      is_box = price_range_ratio <= 0.50

      vol_first = df["Volume"].iloc[:5].mean()
      vol_second = df["Volume"].iloc[5:].mean()
      is_vol_inc = vol_second > vol_first

      if is_box and is_vol_inc:
        latest_price = df["Close"].iloc[-1]
        stock_row = df_krx[df_krx["Code"] == ticker]
        stock_name = (
            stock_row["Name"].values[0] if not stock_row.empty else ticker
        )

        results.append(
            f" [포착!] {stock_name} ({ticker}) | 현재가: {latest_price:,}원 |"
            f" 변동폭: {round(price_range_ratio*100, 1)}%"
        )
    except Exception as e:
      pass

  if results:
    print("\n" + "\n".join(results))
  else:
    print(" 오늘 조건(수급+박스권)을 만족하는 종목이 없습니다.")

  print("==================================================")


if __name__ == "__main__":
  run_stock_check()
