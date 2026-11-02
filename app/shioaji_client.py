"""
shioaji_client.py — 封裝登入、抓持股、下單。
注意: shioaji 有底層 C 函式庫依賴,故意延後到真正需要時才 import,
避免它在某些環境裝不起來時,拖垮整個 Flask app 讓網站開不起來。

2026-11-02 新增「斷線自動重新登入」:
- session_dead(e) 判斷例外是不是登入/連線失效
- relogin() 用 lock 保證同時只有一個重新登入在跑,失敗會退避重試
- 查詢類 API(持股、委託、update_status)遇到斷線 → 重新登入後重試一次
- 下單(place_order)遇到斷線 → 只重新登入,絕不自動重送
- 背景健康檢查定期探測連線,壞了先重新登入
- 委託/成交回報只處理本程式送出的證券委託,期貨與其他程式的回報一律忽略
"""
import os
import re
import time
import datetime
import threading
import collections

API_KEY = os.getenv("SJ_API_KEY")
SECRET_KEY = os.getenv("SJ_SECRET_KEY")
SIMULATION = os.getenv("SJ_SIMULATION", "true").lower() == "true"
CA_PATH = os.getenv("SJ_CA_PATH")
CA_BASE64 = os.getenv("SJ_CA_BASE64")
CA_PASSWORD = os.getenv("SJ_CA_PASSWORD")
CA_PERSON_ID = os.getenv("SJ_CA_PERSON_ID")

# ── 重新登入參數 ─────────────────────────────────────────────
# 網頁請求裡觸發的重新登入:退避短一點,避免超過 gunicorn 120 秒 timeout
RELOGIN_BACKOFF_REQUEST = (5, 15)
# 背景健康檢查觸發的重新登入:完整退避
RELOGIN_BACKOFF_BACKGROUND = (5, 15, 60)
# 重新登入全部失敗後,這段時間內網頁請求不再自己重試(交給背景),避免每次開頁面都卡很久
FAILED_COOLDOWN_SEC = 60
# 剛登入時 Solace 連線可能還沒就緒,登入後先暖機探測
WARMUP_TRIES = 5
WARMUP_DELAY_SEC = 2
# 背景健康檢查間隔(秒),設 0 可關閉
HEALTH_INTERVAL_SEC = int(os.getenv("SJ_HEALTH_INTERVAL", "300"))
HEALTH_FIRST_DELAY_SEC = 20
LOGOUT_TIMEOUT_SEC = 5

ORDER_LOST_MSG = "下單時連線中斷,請先到委託查詢確認是否已送出,再決定要不要重下"

_api = None
_lock = threading.Lock()          # 登入 / 重新登入互斥
_state_lock = threading.Lock()    # 保護 _status / _events
_status = {
    "state": "idle",              # idle / connecting / connected / reconnecting / failed
    "last_relogin_at": None,
    "last_relogin_reason": None,
    "last_error": None,
    "last_attempt_at": None,
    "last_health_ok_at": None,
    "relogin_count": 0,
}
_events = collections.deque(maxlen=20)        # 登入/重新登入事件
_own_order_ids = set()                         # 本程式送出的委託 id / seqno / ordno
_order_events = collections.deque(maxlen=50)   # 本程式委託的回報(只記在記憶體與日誌)
_name_cache = {}
_monitor_started = False

STATE_LABELS = {
    "idle": "尚未登入",
    "connecting": "登入中",
    "connected": "已連線",
    "reconnecting": "重新登入中",
    "failed": "失敗",
}


# ── 工具 ─────────────────────────────────────────────────────
def _now():
    return datetime.datetime.now()


def _log(msg):
    print(f"[shioaji {_now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def _short(e, n=160):
    s = str(e).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def _set_status(**kw):
    with _state_lock:
        _status.update(kw)


def _add_event(kind, reason, ok, error=None):
    with _state_lock:
        _events.appendleft({
            "at": _now().strftime("%m-%d %H:%M:%S"),
            "kind": kind,
            "reason": reason,
            "ok": ok,
            "error": _short(error) if error else "",
        })


_DEAD_PATTERNS = [
    "sessionnotestablished",
    "notready",
    "unable to wait for session",
    "not logged in",
    "not login",
    "token",
    "expired",
    "unauthorized",
]
_RE_401 = re.compile(r"\b401\b")


def session_dead(e):
    """例外是否代表 Shioaji 連線 / 登入已失效(需要重新登入)。"""
    msg = str(e).lower() + " " + type(e).__name__.lower()
    if any(p in msg for p in _DEAD_PATTERNS):
        return True
    return bool(_RE_401.search(msg))


def _import_sj():
    try:
        import shioaji as sj
    except Exception as e:
        raise RuntimeError(f"shioaji 套件載入失敗(可能是環境缺少底層函式庫): {e}")
    return sj


def _resolve_ca_path():
    """
    正式環境需要 CA 憑證檔案路徑。優先支援 SJ_CA_BASE64(把 .pfx 檔轉成 base64 字串存進環境變數),
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


def _probe(api):
    """輕量的伺服器端查詢,用來確認連線是活的(list_accounts 是本地快取,測不出斷線)。"""
    from shioaji import Unit

    return api.list_positions(api.stock_account, unit=Unit.Share)


# ── 登入 / 重新登入 ───────────────────────────────────────────
def _login_once():
    """建立新的 Shioaji 物件 → 登入 → 啟用 CA → 註冊回報 callback → 暖機。失敗丟例外。"""
    sj = _import_sj()
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

    _register_callbacks(api)
    _warm_up(api)
    return api


def _warm_up(api):
    """
    登入剛完成時,永豐的 Solace 連線有時還沒就緒,查詢會噴 NotReady。
    這裡先等它就緒,讓「重新登入後重試一次」真的有機會成功。
    非連線類錯誤(例如沒有庫存資料)不影響登入結果。
    """
    for i in range(WARMUP_TRIES):
        try:
            _probe(api)
            return
        except Exception as e:
            if not session_dead(e):
                _log(f"暖機查詢出現非連線錯誤(忽略): {_short(e)}")
                return
            if i == WARMUP_TRIES - 1:
                raise
            time.sleep(WARMUP_DELAY_SEC)


def _safe_logout(api):
    """先登出舊連線,失敗或卡住都忽略(斷線時 logout 可能等很久,所以限時)。"""
    def _do():
        try:
            api.logout()
        except Exception as e:
            _log(f"logout 失敗(忽略): {_short(e)}")

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(LOGOUT_TIMEOUT_SEC)
    if t.is_alive():
        _log("logout 超過時限,略過")


def _do_login(reason, backoff, is_relogin):
    """呼叫前必須已持有 _lock。成功回傳新 api;全部失敗丟例外並把狀態設為 failed。"""
    global _api
    kind = "重新登入" if is_relogin else "登入"
    old = _api
    _set_status(state="reconnecting" if is_relogin else "connecting")
    _log(f"開始{kind},原因: {reason}")

    if old is not None:
        _safe_logout(old)
        _api = None

    delays = (0,) + tuple(backoff)
    last_err = None
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            _log(f"{kind}失敗,{delay} 秒後重試(第 {attempt}/{len(delays)} 次)")
            time.sleep(delay)
        _set_status(last_attempt_at=time.time())
        try:
            api = _login_once()
        except Exception as e:
            last_err = e
            _log(f"{kind}第 {attempt} 次失敗: {_short(e)}")
            continue

        _api = api
        with _state_lock:
            _status.update(state="connected", last_error=None)
            if is_relogin:
                _status["relogin_count"] += 1
                _status["last_relogin_at"] = _now()
                _status["last_relogin_reason"] = reason
        _add_event(kind, reason, True)
        _log(f"{kind}成功(第 {attempt} 次嘗試)")
        return api

    _set_status(state="failed", last_error=_short(last_err))
    _add_event(kind, reason, False, last_err)
    _log(f"{kind}全部失敗: {_short(last_err)}")
    raise RuntimeError(f"Shioaji {kind}失敗: {last_err}")


def _in_failed_cooldown():
    with _state_lock:
        if _status["state"] != "failed" or not _status["last_attempt_at"]:
            return False
        return time.time() - _status["last_attempt_at"] < FAILED_COOLDOWN_SEC


def get_api():
    """單例登入,避免每個請求都重新登入。"""
    if _api is not None:
        return _api
    if not API_KEY or not SECRET_KEY:
        raise RuntimeError("尚未設定 SJ_API_KEY / SJ_SECRET_KEY 環境變數")
    with _lock:
        if _api is not None:
            return _api
        if _in_failed_cooldown():
            raise RuntimeError(f"Shioaji 登入失敗,背景會持續重試: {_status['last_error']}")
        was_failed = _status["state"] == "failed"
        return _do_login("重新登入(上次失敗)" if was_failed else "首次登入",
                         RELOGIN_BACKOFF_REQUEST, is_relogin=was_failed)


def relogin(reason, failed_api, background=False):
    """
    重新登入。failed_api 是呼叫端剛剛用到、發現失效的那個 api 物件。
    多個請求同時斷線時,只有第一個真的重新登入,其他人等 lock 釋放後
    發現 _api 已經換新,就直接用新的,不會造成登入風暴。
    """
    with _lock:
        if _api is not None and _api is not failed_api:
            return _api
        if not background and _in_failed_cooldown():
            raise RuntimeError(f"Shioaji 重新登入失敗,背景會持續重試: {_status['last_error']}")
        backoff = RELOGIN_BACKOFF_BACKGROUND if background else RELOGIN_BACKOFF_REQUEST
        return _do_login(reason, backoff, is_relogin=True)


def _query(label, fn):
    """查詢類呼叫:遇到連線失效 → 重新登入 → 重試一次;仍失敗才丟出。"""
    api = get_api()
    try:
        return fn(api)
    except Exception as e:
        if not session_dead(e):
            raise
        _log(f"{label} 發現連線失效: {_short(e)}")
        new_api = relogin(f"{label}: {_short(e, 80)}", api)
        return fn(new_api)


# ── 狀態(給頁面用) ──────────────────────────────────────────
def get_status():
    with _state_lock:
        s = dict(_status)
        events = list(_events)
    last = s["last_relogin_at"]
    health = s["last_health_ok_at"]
    return {
        "state": s["state"],
        "label": STATE_LABELS.get(s["state"], s["state"]),
        "last_relogin_at": last.strftime("%m-%d %H:%M:%S") if last else None,
        "last_relogin_reason": s["last_relogin_reason"],
        "last_error": s["last_error"],
        "last_health_ok_at": health.strftime("%H:%M:%S") if health else None,
        "relogin_count": s["relogin_count"],
        "events": events,
    }


# ── 背景健康檢查 ─────────────────────────────────────────────
def health_check_once():
    """探測一次連線;壞了就重新登入(背景用完整退避)。"""
    api = _api
    if api is None:
        if not API_KEY or not SECRET_KEY:
            return
        with _lock:
            if _api is None:
                was_failed = _status["state"] == "failed"
                _do_login("背景健康檢查:" + ("上次登入失敗,重試" if was_failed else "預先登入"),
                          RELOGIN_BACKOFF_BACKGROUND, is_relogin=was_failed)
        _set_status(last_health_ok_at=_now())
        return
    try:
        _probe(api)
        _set_status(last_health_ok_at=_now())
    except Exception as e:
        if session_dead(e):
            _log(f"背景健康檢查發現連線失效: {_short(e)}")
            relogin(f"背景健康檢查: {_short(e, 80)}", api, background=True)
            _set_status(last_health_ok_at=_now())
        else:
            _log(f"背景健康檢查查詢錯誤(非連線問題,略過): {_short(e)}")


def _monitor_loop():
    time.sleep(HEALTH_FIRST_DELAY_SEC)
    while True:
        try:
            health_check_once()
        except Exception as e:
            _log(f"背景健康檢查失敗: {_short(e)}")
        time.sleep(HEALTH_INTERVAL_SEC)


def start_health_monitor():
    global _monitor_started
    if _monitor_started or HEALTH_INTERVAL_SEC <= 0 or not API_KEY or not SECRET_KEY:
        return
    _monitor_started = True
    threading.Thread(target=_monitor_loop, daemon=True, name="sj-health").start()
    _log(f"背景健康檢查已啟動,每 {HEALTH_INTERVAL_SEC} 秒一次")


# ── 委託 / 成交回報 ───────────────────────────────────────────
_STOCK_STATE_TOKENS = ("StockOrder", "StockDeal", "SORDER", "SDEAL", "TFTOrder", "TFTDeal")


def _register_callbacks(api):
    try:
        api.set_order_callback(handle_order_event)
    except Exception as e:
        _log(f"註冊委託回報 callback 失敗(不影響下單): {_short(e)}")


def _remember_trade(trade):
    order = getattr(trade, "order", None)
    for attr in ("id", "seqno", "ordno"):
        v = getattr(order, attr, None)
        if v:
            _own_order_ids.add(str(v))


def handle_order_event(stat, msg):
    """
    永豐會把同一帳戶「所有」委託/成交回報推給每個登入中的程式(包括 txf-sim 的期貨單)。
    這裡只處理本程式送出的證券委託,其他一律忽略。
    回報只記在記憶體與日誌,不寫 trade_log、不改任何狀態
    (trade_log 只在 /confirm 下單時由 place_orders 的結果寫入)。
    回傳 True 表示是本程式的回報。
    """
    try:
        tokens = f"{getattr(stat, 'name', '')} {getattr(stat, 'value', '')} {stat}"
        if not any(t in tokens for t in _STOCK_STATE_TOKENS):
            return False  # 期貨/選擇權(FuturesOrder/FuturesDeal)或未知類型

        msg = msg or {}
        contract = msg.get("contract") or {}
        sec_type = str(contract.get("security_type", "") or "")
        if sec_type and "STK" not in sec_type.upper():
            return False

        order = msg.get("order") or {}
        ids = {str(v) for v in (
            order.get("id"), order.get("seqno"), order.get("ordno"),
            msg.get("trade_id"), msg.get("seqno"), msg.get("ordno"),
        ) if v}
        if not ids & _own_order_ids:
            return False  # 不是本程式送出的委託

        code = contract.get("code") or msg.get("code")
        _order_events.appendleft({"at": _now().strftime("%H:%M:%S"), "type": str(stat), "code": code})
        _log(f"本程式委託回報 {stat} {code}")
        return True
    except Exception as e:
        _log(f"處理委託回報失敗(忽略): {_short(e)}")
        return False


# ── 對外功能 ─────────────────────────────────────────────────
def get_version():
    import shioaji as sj
    return sj.__version__


def mode_label():
    return "模擬" if SIMULATION else "正式"


def _stock_name(api, code):
    """中文股名(從已下載的商品檔查),查不到回空字串。"""
    if code in _name_cache:
        return _name_cache[code]
    try:
        name = getattr(api.Contracts.Stocks[code], "name", "") or ""
    except Exception:
        name = ""
    if name:
        _name_cache[code] = name
    return name


def get_current_positions():
    """
    回傳 {code: 股數} 目前持股(以「股」為單位,不是張)。
    list_positions 預設 unit=Unit.Common 是用「張」回報,零股必須指定 unit=Unit.Share。
    """
    positions = _query("list_positions", _probe)
    current = {}
    for p in positions:
        current[p.code] = current.get(p.code, 0) + int(p.quantity)
    return current


def get_position_details():
    """
    回傳完整庫存清單(代號、名稱、股數、均價、現價、損益),給「目前庫存」畫面用。
    欄位用 getattr 保護,不同版本 shioaji 回傳的欄位可能略有差異。
    """
    positions = _query("list_positions", _probe)
    api = get_api()
    details = []
    for p in positions:
        details.append({
            "code": p.code,
            "name": _stock_name(api, p.code),
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

    斷線處理(重要):place_order 遇到連線失效時「絕不自動重送」,
    因為無法確定那張有沒有到券商。只重新登入,該筆標記 ConnectionLost,
    後面還沒送的筆數標記 NotSent(consume=False,留在待確認清單)。
    """
    from shioaji.constant import Action, StockPriceType, OrderType, StockOrderLot

    api = get_api()
    results = []
    lost = False
    for item in confirm_list:
        if lost:
            results.append({**item, "status": "NotSent",
                            "error": "前面有一筆下單時連線中斷,本筆未送出(保留在待確認清單)",
                            "price": None, "consume": False})
            continue
        price = None
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
            _remember_trade(trade)
            status = trade.status.status
            results.append({**item, "status": str(status), "error": "", "price": price, "consume": True})
        except Exception as e:
            if session_dead(e):
                lost = True
                _log(f"下單 {item['code']} 時連線中斷(不重送): {_short(e)}")
                results.append({**item, "status": "ConnectionLost",
                                "error": f"{ORDER_LOST_MSG}({_short(e)})",
                                "price": price, "consume": True, "order_lost": True})
                try:
                    relogin(f"下單 {item['code']} 時連線中斷", api)
                except Exception as re_err:
                    _log(f"下單斷線後重新登入失敗: {_short(re_err)}")
            else:
                results.append({**item, "status": "Failed", "error": str(e), "price": None, "consume": True})
    return results


def _is_stock_trade(t):
    sec = str(getattr(getattr(t, "contract", None), "security_type", "") or "")
    return not sec or "STK" in sec.upper()


def list_open_trades():
    """查詢今天還沒成交/還在委託中的證券單,方便決定要不要取消改價重掛。"""
    def _fetch(api):
        api.update_status(api.stock_account)
        return api.list_trades()

    trades = _query("update_status", _fetch)
    open_statuses = {"PendingSubmit", "PreSubmitted", "Submitted", "Filling"}
    open_trades = []
    for t in trades:
        if not _is_stock_trade(t):
            continue
        status = str(t.status.status)
        if status.split(".")[-1] in open_statuses:
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
    """依 order_id 取消今天的委託單。查詢部分會自動重連;取消動作本身遇到斷線不自動重送。"""
    def _fetch(api):
        api.update_status(api.stock_account)
        return api, api.list_trades()

    api, trades = _query("update_status", _fetch)
    for t in trades:
        if t.order.id == order_id:
            try:
                api.cancel_order(t)
            except Exception as e:
                if session_dead(e):
                    try:
                        relogin("取消委託時連線中斷", api)
                    except Exception:
                        pass
                    raise RuntimeError("取消時連線中斷,已重新登入;請重新整理確認委託狀態後再決定是否重試") from e
                raise
            return True
    return False
