import pandas as pd


def compute_rolling_pnl(
    ticker: str,
    prices: pd.DataFrame,
    expirations: pd.DataFrame,
    multiplier: float,
    is_on_prompt_contract: bool = True,
) -> pd.DataFrame:
    """Dollar P&L for one long contract, using the existing month-end roll rule.

    A position held at yesterday's close earns today's P&L; a roll occurs at
    today's close and takes effect on the next observation.
    """
    # Generic contracts 1, 2 and 3 are needed to follow relabeling at expiry.
    columns = [f"{ticker}{n} Comdty" for n in (1, 2, 3)]
    
    frame = prices[columns].copy().apply(pd.to_numeric, errors="raise")
    frame = frame.dropna(how="all")
    if frame.empty or not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ValueError(f"{ticker}: prices must have unique, sorted dates.")

    # Fill missing contracts only on observed settlement dates and within
    # the common coverage of all three contracts.
    complete = frame.notna().all(axis=1)
    if not complete.any():
        raise ValueError(f"{ticker}: no date has all three contract prices.")
    frame = frame.loc[complete[complete].index[0]:complete[complete].index[-1]]
    missing = frame.isna()
    for date, row in missing.loc[missing.any(axis=1)].iterrows():
        filled_columns = ", ".join(row.index[row])
        print(f"Warning: {ticker}: forward-filled {date:%Y-%m-%d}: {filled_columns}")
    frame = frame.ffill()
    
    # Expiration dates determine when generic contract labels shift.
    expiry = pd.DatetimeIndex(pd.to_datetime(expirations[ticker].dropna())).normalize()
    if expiry.empty:
        raise ValueError(f"{ticker}: no expiration dates.")
    if frame.index.max().normalize() > expiry.max():
        raise ValueError(f"{ticker}: expiration table does not cover the price sample.")
    
    # pnl_contract_at_close is the generic contract number used at that close
    # to calculate the next observation's P&L.
    # The first close establishes the holding; there is no preceding P&L.
    result = pd.DataFrame(index=frame.index, columns=["pnl", "pnl_contract_at_close"], dtype=float)
    
    for i in range(1, len(frame)):
        previous, current = frame.index[i - 1], frame.index[i]
        next_date = frame.index[i + 1] if i + 1 < len(frame) else current
        expired = expiry[(expiry >= previous.normalize()) & (expiry < current.normalize())]
        if len(expired) and (len(expired) != 1 or expired[0] != previous.normalize()):
            raise ValueError(f"{ticker}: expiration without a settlement on {previous.date()} to {current.date()}.")
        previous_was_expiry = len(expired) == 1
        entered_new_month = (previous.year, previous.month) != (current.year, current.month)
        current_is_month_end = (current.year, current.month) != (next_date.year, next_date.month)
        next_has_expiry = ((expiry.year == next_date.year) & (expiry.month == next_date.month)).any()
        current_has_expiry = ((expiry.year == current.year) & (expiry.month == current.month)).any()

        # Compare prices for the same underlying contract across relabeling.
        if previous_was_expiry:
            if entered_new_month and current_has_expiry:
                # Yesterday's third generic becomes today's second.
                previous_col, current_col = columns[2], columns[1]
                is_on_prompt_contract = False
            else:
                # Yesterday's second generic becomes today's first.
                previous_col, current_col = columns[1], columns[0]
                is_on_prompt_contract = not (current_is_month_end and next_has_expiry)
        elif is_on_prompt_contract:
            previous_col = current_col = columns[0]
            # Roll at month-end close when the following month has an expiry.
            if current_is_month_end and next_has_expiry:
                is_on_prompt_contract = False
        else:
            previous_col = current_col = columns[1]

        # Apply the contract multiplier to the daily price change.
        old_price = frame.at[previous, previous_col]
        new_price = frame.at[current, current_col]
        if pd.isna(old_price) or pd.isna(new_price):
            raise ValueError(f"{ticker}: missing settlement for {previous.date()} to {current.date()}.")
        result.at[current, "pnl"] = (float(new_price) - float(old_price)) * multiplier
        result.at[previous, "pnl_contract_at_close"] = columns.index(previous_col) + 1

    # No subsequent P&L interval exists to identify the final contract.
    result.at[frame.index[-1], "pnl_contract_at_close"] = 1 if is_on_prompt_contract else 2
    previous_expired = pd.Series(frame.index, index=frame.index).shift().isin(expiry)
    previous_contract = result["pnl_contract_at_close"].shift()
    # Expiry relabels the same contract; only a change after relabeling is a roll.
    result["roll_at_close"] = previous_contract.notna() & (
        previous_contract - previous_expired.astype(int) != result["pnl_contract_at_close"]
    )

    return result
