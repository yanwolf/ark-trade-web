"""
main.py — Flask dashboard
- 輸入今日建議(取代原本手動編輯CSV)
- 顯示建議 vs 目前持股 的比對清單
- 按下確認才會真的呼叫 Shioaji 下單
- 顯示歷史紀錄
"""
from flask import Flask, request, redirect, url_for, render_template, flash, session
import os

import db
import shioaji_client as sc

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "ark-trade-dashboard-secret")
ORDER_CONFIRM_PASSWORD = os.getenv("ORDER_CONFIRM_PASSWORD")
db.init_db()


@app.before_request
def require_site_login():
    """
    整個網站都要密碼才能進(跟下單確認共用同一組 ORDER_CONFIRM_PASSWORD)。
    沒設定這個環境變數的話,維持不用密碼,方便先測試。
    """
    if not ORDER_CONFIRM_PASSWORD:
        return None
    if request.endpoint in ("login", "static"):
        return None
    if not session.get("authed"):
        return redirect(url_for("login"))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ORDER_CONFIRM_PASSWORD:
            session["authed"] = True
            return redirect(url_for("dashboard"))
        error = "密碼錯誤"
    return f"""
    <!doctype html>
    <html lang="zh-Hant"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>登入</title>
    <style>
      body {{ font-family:-apple-system,sans-serif; background:#0e0f11; color:#eee;
             display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
      form {{ background:#17181b; border:1px solid #2a2b2f; border-radius:10px; padding:24px; width:280px; }}
      h1 {{ font-size:16px; margin:0 0 14px; }}
      input {{ width:100%; padding:10px; background:#1f2023; color:#eee; border:1px solid #3a3b3f;
               border-radius:8px; font-size:15px; box-sizing:border-box; margin-bottom:10px; }}
      button {{ width:100%; padding:10px; background:#d4a72c; color:#1a1300; border:none;
                border-radius:8px; font-weight:600; font-size:15px; }}
      p.err {{ color:#e6a23c; font-size:13px; }}
    </style></head><body>
    <form method="post">
      <h1>方舟運算 → 永豐下單</h1>
      {'<p class="err">' + error + '</p>' if error else ''}
      <input type="password" name="password" placeholder="密碼" autofocus required>
      <button type="submit">登入</button>
    </form>
    </body></html>
    """


@app.route("/")
def dashboard():
    suggestions = db.get_pending_suggestions()

    positions_error = None
    confirm_list = []
    position_details = []
    open_trades = []
    orders_error = None
    try:
        position_details = sc.get_position_details()
        current_positions = {p["code"]: p["quantity"] for p in position_details}
        for s in suggestions:
            confirm_list.append({
                **s,
                "held_qty": current_positions.get(s["code"], 0),
            })
    except Exception as e:
        positions_error = str(e)
        confirm_list = [{**s, "held_qty": "?"} for s in suggestions]

    try:
        open_trades = sc.list_open_trades()
    except Exception as e:
        orders_error = str(e)

    logs = db.get_recent_logs(30)

    return render_template(
        "dashboard.html",
        confirm_list=confirm_list,
        logs=logs,
        mode_label=sc.mode_label(),
        positions_error=positions_error,
        position_details=position_details,
        open_trades=open_trades,
        orders_error=orders_error,
        require_password=bool(ORDER_CONFIRM_PASSWORD),
    )


@app.route("/add", methods=["POST"])
def add_suggestion():
    code = request.form["code"].strip()
    action = request.form["action"].strip().lower()
    qty = int(request.form["qty"])
    note = request.form.get("note", "").strip()
    price_str = request.form.get("price", "").strip()
    price = float(price_str) if price_str else None
    if action in ("buy", "sell") and code and 0 < qty < 1000:
        db.add_suggestion(code, action, qty, note, price)
    else:
        flash("⚠️ 股數需介於 1~999(零股),請重新輸入")
    return redirect(url_for("dashboard"))


@app.route("/delete/<int:sid>", methods=["POST"])
def delete_suggestion(sid):
    db.delete_suggestion(sid)
    return redirect(url_for("dashboard"))


@app.route("/delete-bulk", methods=["POST"])
def delete_bulk_suggestions():
    ids = request.form.getlist("selected_ids")
    for sid in ids:
        db.delete_suggestion(int(sid))
    if ids:
        flash(f"🗑️ 已刪除 {len(ids)} 筆")
    return redirect(url_for("dashboard"))


@app.route("/confirm", methods=["POST"])
def confirm_orders():
    if ORDER_CONFIRM_PASSWORD:
        password = request.form.get("password", "")
        if password != ORDER_CONFIRM_PASSWORD:
            flash("❌ 密碼錯誤,已取消送出,沒有下任何單")
            return redirect(url_for("dashboard"))

    suggestions = db.get_pending_suggestions()
    if not suggestions:
        return redirect(url_for("dashboard"))

    current_positions = sc.get_current_positions()
    confirm_list = [
        {**s, "held_qty": current_positions.get(s["code"], 0)}
        for s in suggestions
    ]

    results = sc.place_orders(confirm_list)

    log_entries = [
        {
            "mode": sc.mode_label(),
            "code": r["code"],
            "action": r["action"],
            "qty": r["qty"],
            "held_qty": r["held_qty"],
            "status": r["status"],
            "error": r["error"],
            "note": r["note"],
            "price": r.get("price"),
        }
        for r in results
    ]
    db.write_log(log_entries)
    db.mark_consumed([s["id"] for s in suggestions])

    return redirect(url_for("dashboard"))


@app.route("/add-bulk", methods=["POST"])
def add_bulk_suggestions():
    """
    批次貼上: 一行一筆, 格式 代號,買賣,股數,價格(選填),備註(選填)
    價格留空就用參考價,不會自動追市價。
    例如:
    00878,sell,410
    00920,sell,126,10.5,方舟建議
    00876,buy,71,,位階保守
    """
    text = request.form.get("bulk_text", "")
    added, skipped = 0, []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            skipped.append(f"第{line_no}行格式錯誤: {raw_line}")
            continue
        code, action, qty_str = parts[0], parts[1].lower(), parts[2]
        try:
            qty = int(qty_str)
        except ValueError:
            skipped.append(f"第{line_no}行股數不是數字: {raw_line}")
            continue

        # 第4欄可能是價格(數字)或舊格式的備註(文字),自動判斷
        price, note = None, ""
        if len(parts) >= 4:
            if parts[3] == "":
                price = None
                note = parts[4] if len(parts) > 4 else ""
            else:
                try:
                    price = float(parts[3])
                    note = parts[4] if len(parts) > 4 else ""
                except ValueError:
                    note = parts[3]  # 舊格式: 第4欄是備註,沒有價格

        if action not in ("buy", "sell") or not code or not (0 < qty < 1000):
            skipped.append(f"第{line_no}行內容不合法(買賣需buy/sell,股數需1~999): {raw_line}")
            continue
        db.add_suggestion(code, action, qty, note, price)
        added += 1

    msg = f"✅ 已新增 {added} 筆"
    if skipped:
        msg += "\n⚠️ 略過以下行:\n" + "\n".join(skipped)
    flash(msg)
    return redirect(url_for("dashboard"))


@app.route("/cancel-order", methods=["POST"])
def cancel_order():
    order_id = request.form.get("order_id")
    try:
        ok = sc.cancel_trade(order_id)
        flash(f"✅ 已取消委託 {order_id}" if ok else f"⚠️ 找不到委託 {order_id}(可能已成交或已取消)")
    except Exception as e:
        flash(f"❌ 取消失敗: {e}")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
