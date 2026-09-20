"""
shioaji_client.py — 封裝登入、抓持股、下單。
注意: shioaji 有底層 C 函式庫依賴,故意延後到真正需要時才 import,
避免它在某些環境裝不起來時,拖垮整個 Flask app 讓網站開不起來。
"""
import os

API_KEY = os.getenv("SJ_API_KEY")
SECRET_KEY = os.getenv("SJ_SECRET_KEY")
SIMULATION = os.getenv("SJ_SIMULATION", "true").lower() == "true"
CA_PATH = os.getenv("SJ_CA_PATH")
CA_BASE64 = os.getenv("SJ_CA_BASE64")
CA_PASSWORD = os.getenv("SJ_CA_PASSWORD")
CA_PERSON_ID = os.getenv("SJ_CA_PERSON_ID")

_api = None


def _resolve_ca_path():
    """
    正式環境需要 CA 憑證檔案路徑。Zeabur 沒有檔案上傳介面,
    所以優先支援 SJ_CA_BASE64(把 .pfx 檔轉成 base64 字串存進環境變數),
    程式啟動時還原成暫存檔;若你是在自己電腦跑,也可以直接用 SJ_CA_PATH 給真實路徑。
    """
    if CA_PATH:
        return CA_PATH
    if CA_BASE64:
        import base64

        pfx_bytes = base64.b64decode(CA_BASE64)
        tmp_path = "/tmp/sinopac_ca.pfx"
        with open(tmp_path, "wb") as f:
            f.write(pfx_bytes)
        return tmp_path
    return None


def get_api():
    """單例登入,避免每個請求都重新登入。"""
    global _api
    if _api is not None:
        return _api

    if not API_KEY or not SECRET_KEY:
        raise RuntimeError("尚未設定 SJ_API_KEY / SJ_SECRET_KEY 環境變數")

    try:
        import shioaji as sj
    except Exception as e:
        raise RuntimeError(f"shioaji 套件載入失敗(可能是環境缺少底層函式庫): {e}")

    api = sj.Shioaji(simulation=SIMULATION)
    api.login(api_key=API_KEY, secret_key=SECRET_KEY)

    if not SIMULATION:
        ca_path = _resolve_ca_path()
        if not ca_path:
            raise RuntimeError("正式環境需要 CA 憑證,請設定 SJ_CA_PATH 或 SJ_CA_BASE64")
        kwargs = {"ca_path": ca_path, "ca_passwd": CA_PASSWORD}
        if CA_PERSON_ID:
            kwargs["person_id"] = CA_PERSON_ID
        result = api.activate_ca(**kwargs)
        if not result:
            raise RuntimeError("CA 憑證啟用失敗,請確認 SJ_CA_BASE64/SJ_CA_PASSWORD/SJ_CA_PERSON_ID 是否正確")

    _api = api
    return _api


def get_version():
    import shioaji as sj
    return sj.__version__


def mode_label():
    return "模擬" if SIMULATION else "正式"


def get_current_positions():
    """
    回傳 {code: 股數} 目前持股(以「股」為單位,不是張)。
    重點: list_positions 預設 unit=Unit.Common 是用「張」回報數量,
    零股倉位不到1張幾乎都會變成0。要拿到正確股數必須指定 unit=Unit.Share。
    """
    from shioaji import Unit

    api = get_api()
    positions = api.list_positions(api.stock_account, unit=Unit.Share)
    current = {}
    for p in positions:
        current[p.code] = current.get(p.code, 0) + int(p.quantity)
    return current


def get_position_details():
    """
    回傳完整庫存清單(代號、股數、均價、現價、損益),給「查詢目前庫存」畫面用。
    同樣用 unit=Unit.Share 確保股數是真實股數,不是張數。
    欄位用 getattr 保護,不同版本 shioaji 回傳的欄位可能略有差異。
    """
    from shioaji import Unit

    api = get_api()
    positions = api.list_positions(api.stock_account, unit=Unit.Share)
    details = []
    for p in positions:
        details.append({
            "code": p.code,
            "quantity": int(p.quantity),
            "price": getattr(p, "price", None),
            "last_price": getattr(p, "last_price", None),
            "pnl": getattr(p, "pnl", None),
        })
    return details


def _tick_size(contract, price):
    """
    台股跳價單位(最小升降單位)。ETF(category 通常是 "00")用簡化的兩級距規則,
    一般股票用交易所的六級距規則。
    """
    is_etf = getattr(contract, "category", None) == "00"
    if is_etf:
        return 0.01 if price < 50 else 0.05
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1
    return 5


def _round_to_tick(contract, price):
    """把價格向下對齊到合法的跳價單位,避免因為價格不合法被拒單。"""
    if price is None:
        return price
    import math

    tick = _tick_size(contract, price)
    aligned = math.floor(price / tick + 1e-9) * tick
    decimals = 2 if tick < 1 else 0
    return round(aligned, decimals)


def _get_order_price(api, contract, side, target_price=None):
    """
    決定下單價格。方舟運算的邏輯是「用合理低價慢慢等,不是不計代價求今天成交」,
    所以預設不追市價:
    - 你指定 target_price(例如方舟建議的淨值價)就用那個價格
    - 沒指定才退回用參考價(contract.reference)
    價格會先對齊跳價單位,再夾在漲跌停範圍內,避免超出限制被交易所拒絕。
    """
    price = target_price if target_price else contract.reference
    price = _round_to_tick(contract, price)

    if side == "buy":
        price = min(price, contract.limit_up)
    else:
        price = max(price, contract.limit_down)

    return price


def place_orders(confirm_list):
    """
    confirm_list: [{code, action, qty, held_qty, note, price(可選)}]
    qty 單位是「股」,用盤中零股(IntradayOdd)下單。
    price 是你指定的目標價,沒填就用參考價,不會自動追市價。
    """
    from shioaji.constant import Action, StockPriceType, OrderType, StockOrderLot

    api = get_api()
    results = []
    for item in confirm_list:
        try:
            contract = api.Contracts.Stocks[item["code"]]
            action = Action.Buy if item["action"] == "buy" else Action.Sell
            price = _get_order_price(api, contract, item["action"], item.get("price"))
            order = api.Order(
                price=price,
                quantity=item["qty"],
                action=action,
                price_type=StockPriceType.LMT,
                order_type=OrderType.ROD,
                order_lot=StockOrderLot.IntradayOdd,
                account=api.stock_account,
            )
            trade = api.place_order(contract, order)
            status = trade.status.status
            results.append({**item, "status": str(status), "error": "", "price": price})
        except Exception as e:
            results.append({**item, "status": "Failed", "error": str(e), "price": None})
    return results


def list_open_trades():
    """
    查詢今天還沒成交/還在委託中的單子,方便決定要不要取消改價重掛。
    """
    api = get_api()
    api.update_status(api.stock_account)
    trades = api.list_trades()
    open_statuses = {"PendingSubmit", "PreSubmitted", "Submitted", "Filling"}
    open_trades = []
    for t in trades:
        status = str(t.status.status)
        if status in open_statuses:
            open_trades.append({
                "order_id": t.order.id,
                "code": t.contract.code,
                "action": str(t.order.action),
                "quantity": t.order.quantity,
                "price": t.order.price,
                "status": status,
            })
    return open_trades


def cancel_trade(order_id):
    """依 order_id 取消今天的委託單,取消後可以用新價格重新掛。"""
    api = get_api()
    api.update_status(api.stock_account)
    trades = api.list_trades()
    for t in trades:
        if t.order.id == order_id:
            api.cancel_order(t)
            return True
    return False
