ALLOWED_CURRENCIES = {"CNY", "USD", "EUR", "JPY"}

def assert_currency(currency: str) -> None:
    if currency not in ALLOWED_CURRENCIES:
        raise ValueError("unsupported currency")
