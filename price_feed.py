"""
Price data sourced directly from Robinhood's Crypto market data endpoint,
so the price you see in the bot always matches what you'd get filled at.
"""
from robinhood_client import RobinhoodCryptoClient

SYMBOL = "ETH-USD"


def get_eth_price(client: RobinhoodCryptoClient) -> dict:
    """Returns {'bid': float, 'ask': float, 'mid': float}."""
    data = client.get_best_bid_ask([SYMBOL])
    result = data["results"][0]
    bid = float(result["bid_inclusive_of_sell_spread"])
    ask = float(result["ask_inclusive_of_buy_spread"])
    return {
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2,
    }
