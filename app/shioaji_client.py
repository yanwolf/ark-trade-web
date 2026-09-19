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


def test_login():
    """
    永豐官方 API 測試專用登入(帳戶啟用 API 服務的必要步驟之一)。
    刻意不用快取的連線,確保每次按都是一次全新的 login() 呼叫。
    測試完立刻 logout,避免連線數疊加(永豐同帳號最多同時5個連線)
    或反覆按導致資源耗盡拖垮服務。
    """
    import shioaji as sj

    if not API_KEY or not SECRET_KEY:
        raise RuntimeError("尚未設定 SJ_API_KEY / SJ_SECRET_KEY 環境變數")

    api = sj.Shioaji(simulation=SIMULATION)
    accounts = api.login(api_key=API_KEY, secret_key=SECRET_KEY)
    version = sj.__version__
    try:
        api.logout()
    except Exception:
        pass
    return version, accounts


def check_contract_categories():
    """
    暫時性檢查用: 確認 api.Contracts 底下實際有哪些分類,
    用來驗證複委託/海外商品是否真的能透過 Shioaji API 操作。
    這個功能只是一次性排查,確認結果後這整段連同 API 測試區塊會一起下架。

    注意: 刻意不用 dir()/vars() 整個內省物件(可能很慢甚至拖垮服務),
    改用 hasattr 針對已知/可能的名稱逐一檢查,又快又安全。
    """
    api = get_api()
    contracts = api.Contracts
    known = ["Stocks", "Futures", "Options", "Indexs"]
    possible_foreign_names = [
        "ForeignStocks", "Overseas", "OverseasStocks", "SubBrokerage",
        "GlobalStocks", "Foreign", "USStocks",
    ]
    found_known = [k for k in known if hasattr(contracts, k)]
    found_foreign = [k for k in possible_foreign_names if hasattr(contracts, k)]
    return found_known, found_foreign


def mode_label():
    return "模擬" if SIMULATION else "正式"


def get_current_positions():
    """回傳 {code: 股數} 目前持股(以「股」為單位,不是張)。"""
    api = get_api()
    positions = api.list_positions(api.stock_account)
    current = {}
    for p in positions:
        current[p.code] = current.get(p.code, 0) + int(p.quantity)
    return current


def get_position_details():
    """
    回傳完整庫存清單(代號、股數、均價、現價、損益),給「查詢目前庫存」畫面用。
    欄位用 getattr 保護,不同版本 shioaji 回傳的欄位可能略有差異。
    """
    api = get_api()
    positions = api.list_positions(api.stock_account)
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


def place_orders(confirm_list):
    """
    confirm_list: [{code, action, qty, held_qty, note}]
    qty 單位是「股」,用盤中零股(IntradayOdd)下單。
    """
    from shioaji.constant import Action, StockPriceType, OrderType, StockOrderLot

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
                order_lot=StockOrderLot.IntradayOdd,
                account=api.stock_account,
            )
            trade = api.place_order(contract, order)
            status = trade.status.status
            results.append({**item, "status": str(status), "error": ""})
        except Exception as e:
            results.append({**item, "status": "Failed", "error": str(e)})
    return results


def test_place_order():
    """
    永豐官方 API 測試專用下單(帳戶啟用 API 服務的必要步驟之一)。
    比照官方文件範例: 2890, 價格28, 買進1股, ROD, 現股。
    這不是真的交易建議,純粹是讓永豐系統記錄一筆測試委託。
    """
    from shioaji.constant import Action, StockPriceType, OrderType, StockOrderLot, StockOrderCond

    api = get_api()
    contract = api.Contracts.Stocks["2890"]
    order = api.Order(
        price=28,
        quantity=1,
        action=Action.Buy,
        price_type=StockPriceType.LMT,
        order_type=OrderType.ROD,
        order_lot=StockOrderLot.Common,
        order_cond=StockOrderCond.Cash,
        account=api.stock_account,
    )
    trade = api.place_order(contract, order)
    return trade
