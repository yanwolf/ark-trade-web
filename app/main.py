"""
main.py — Flask dashboard
- 輸入今日建議(取代原本手動編輯CSV)
- 顯示建議 vs 目前持股 的比對清單
- 按下確認才會真的呼叫 Shioaji 下單
- 顯示歷史紀錄
"""
from flask import Flask, request, redirect, url_for, render_template, flash
import os

import db
import shioaji_client as sc

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "ark-trade-dashboard-secret")
db.init_db()


@app.route("/")
def dashboard():
    suggestions = db.get_pending_suggestions()

    positions_error = None
    confirm_list = []
    try:
        current_positions = sc.get_current_positions()
        for s in suggestions:
            confirm_list.append({
                **s,
                "held_qty": current_positions.get(s["code"], 0),
            })
    except Exception as e:
        positions_error = str(e)
        confirm_list = [{**s, "held_qty": "?"} for s in suggestions]

    logs = db.get_recent_logs(30)

    return render_template(
        "dashboard.html",
        confirm_list=confirm_list,
        logs=logs,
        mode_label=sc.mode_label(),
        positions_error=positions_error,
    )


@app.route("/add", methods=["POST"])
def add_suggestion():
    code = request.form["code"].strip()
    action = request.form["action"].strip().lower()
    qty = int(request.form["qty"])
    note = request.form.get("note", "").strip()
    if action in ("buy", "sell") and code and 0 < qty < 1000:
        db.add_suggestion(code, action, qty, note)
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
        }
        for r in results
    ]
    db.write_log(log_entries)
    db.mark_consumed([s["id"] for s in suggestions])

    return redirect(url_for("dashboard"))


@app.route("/add-bulk", methods=["POST"])
def add_bulk_suggestions():
    """
    批次貼上: 一行一筆, 格式 代號,買賣,股數,備註(選填)
    例如:
    00878,sell,410
    00920,sell,126,方舟建議
    00876,buy,71,位階保守
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
        note = parts[3] if len(parts) > 3 else ""
        try:
            qty = int(qty_str)
        except ValueError:
            skipped.append(f"第{line_no}行股數不是數字: {raw_line}")
            continue
        if action not in ("buy", "sell") or not code or not (0 < qty < 1000):
            skipped.append(f"第{line_no}行內容不合法(買賣需buy/sell,股數需1~999): {raw_line}")
            continue
        db.add_suggestion(code, action, qty, note)
        added += 1

    msg = f"✅ 已新增 {added} 筆"
    if skipped:
        msg += "\n⚠️ 略過以下行:\n" + "\n".join(skipped)
    flash(msg)
    return redirect(url_for("dashboard"))


@app.route("/api-test/login", methods=["POST"])
def api_test_login():
    try:
        version, accounts = sc.test_login()
        lines = [f"shioaji 版本: {version}", ""]
        for acc in accounts:
            signed = getattr(acc, "signed", None)
            mark = "✅ 簽署+API測試皆已通過" if signed else "❌ 尚未完成(簽署或API測試審核未過)"
            lines.append(f"{type(acc).__name__} {acc.account_id}: {mark}")
        flash("\n".join(lines))
    except Exception as e:
        flash(f"❌ 登入測試失敗: {e}")
    return redirect(url_for("dashboard"))


@app.route("/api-test/order", methods=["POST"])
def api_test_order():
    try:
        trade = sc.test_place_order()
        flash(f"✅ 下單測試已送出\n{trade}")
    except Exception as e:
        flash(f"❌ 下單測試失敗: {e}")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
