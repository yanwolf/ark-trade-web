"""
shioaji_client.py — 封裝登入、抓持股、下單。
"""
import os
import shioaji as sj
from shioaji.constant import Action, StockPriceType, OrderType

API_KEY = os.getenv("SJ_API_KEY")
SECRET_KEY = os.getenv("SJ_SECRET_KEY")
SIMULATION = os.getenv("SJ_SIMULATION", "true").lower() == "true"
CA_PATH = os.getenv("SJ_CA_PATH")
CA_PASSWORD = os.getenv("SJ_CA_PASSWORD")

_api = None


def get_api():
    """單例登入,避免每個請求都重新登入。"""
    global _api
    if _api is not None:
        return _api

    if not API_KEY or not SECRET_KEY:
        raise RuntimeError("尚未設定 SJ_API_KEY / SJ_SECRET_KEY 環境變數")

    api = sj.Shioaji(simulation=SIMULATION)
    api.login(api_key=API_KEY, secret_key=SECRET_KEY)

    if not SIMULATION and CA_PATH:
        api.activate_ca(ca_path=CA_PATH, ca_passwd=CA_PASSWORD)

    _api = api
    return _api


def mode_label():
    return "模擬" if SIMULATION else "正式"


def get_current_positions():
    api = get_api()
    positions = api.list_positions(api.stock_account)
    current = {}
    for p in positions:
        current[p.code] = current.get(p.code, 0) + int(p.quantity / 1000)
    return current


def place_orders(confirm_list):
    """confirm_list: [{code, action, qty, held_qty, note}]"""
    api = get_api()
    results = []
    for item in confirm_list:
        try:
            contract = api.Contracts.Stocks[item["code"]]
            action = Action.Buy if item["action"] == "buy" else Action.Sell
            order = api.Order(
                price=contract.reference,
                quantity=item["qty"],
                action=action,
                price_type=StockPriceType.LMT,
                order_type=OrderType.ROD,
                account=api.stock_account,
            )
            trade = api.place_order(contract, order)
            status = trade.status.status
            results.append({**item, "status": str(status), "error": ""})
        except Exception as e:
            results.append({**item, "status": "Failed", "error": str(e)})
    return results
