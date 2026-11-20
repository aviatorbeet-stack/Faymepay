#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import re
import json
import time
import random
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import quote, urlparse, parse_qs
from http.server import BaseHTTPRequestHandler

# Firebase Imports
import firebase_admin
from firebase_admin import credentials, firestore

# ============================================================================
# 1. CONFIGURATION
# ============================================================================
CONFIG = {
    "fampay_upi": os.environ.get("FAMPAY_UPI_ID", "shahid4900@fam"),
    "gmail_email": os.environ.get("GMAIL_EMAIL", "shahid2005afridi@gmail.com"),
    "gmail_app_password": re.sub(r"\s+", "", os.environ.get("GMAIL_APP_PASSWORD", "fjrpfhfjzykefhga")),
    "merchant_name": os.environ.get("MERCHANT_NAME", "Dgtl Store"),
    "api_key": os.environ.get("FAMPAY_API_KEY", "fp_live_5dd46fe835de54e91b6c4f9aabc0ea3d"),
    "expiry_minutes": int(os.environ.get("EXPIRY_MINUTES", "15")),
}

FAMPAY_SENDER_HINTS = [
    "fampay", "famapp", "fam app", "fam pay", "yesfam", "yes bank",
    "support@famapp.in", "no-reply@famapp.in", "noreply@famapp.in",
    "you have successfully received", "successfully received",
    "transaction id", "utr", "credited", "received", "payment",
]

# ============================================================================
# 2. FIREBASE INITIALIZATION
# ============================================================================
def get_db():
    try:
        if not firebase_admin._apps:
            project_id = os.environ.get("FIREBASE_PROJECT_ID")
            client_email = os.environ.get("FIREBASE_CLIENT_EMAIL")
            private_key = os.environ.get("FIREBASE_PRIVATE_KEY")
            
            # Handle escaped newlines in private key (Vercel ke liye zaroori hai)
            if private_key:
                private_key = private_key.replace('\\n', '\n')

            cred_dict = {
                "type": "service_account",
                "project_id": project_id,
                "private_key": private_key,
                "client_email": client_email,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        print("Firebase Init Error:", e)
        return None

db = get_db()

def save_order_db(order_id, order_data):
    if db:
        try:
            db.collection("orders").document(order_id).set(order_data)
        except Exception as e:
            print("Firebase Save Error:", e)

def get_order_db(order_id):
    if db:
        try:
            doc = db.collection("orders").document(order_id).get()
            if doc.exists:
                return doc.to_dict()
        except Exception as e:
            print("Firebase Get Error:", e)
    return None

# ============================================================================
# 3. HELPERS
# ============================================================================
def reconstruct_serverless_order(order_id):
    m = re.match(r"^ord_(\d{10})_(\d+)_(\d+)$", str(order_id or "").strip())
    if not m: return None
    created_ts = int(m.group(1))
    paise = int(m.group(2))
    reference = m.group(3)
    amount = round(paise / 100.0, 2)
    exp_ts = created_ts + (CONFIG["expiry_minutes"] * 60)
    return {
        "id": order_id, "amount": amount, "payable_amount": amount,
        "provider": "fampay", "upi_id": CONFIG["fampay_upi"], "reference": reference,
        "utr": None, "status": "pending", "created_ts": created_ts, "expires_ts": exp_ts,
        "created_at": datetime.fromtimestamp(created_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": datetime.fromtimestamp(exp_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "paid_at": None,
    }

def build_upi_link(upi_id, amount, merchant_name, order_id, reference):
    note = order_id or reference or "Payment"
    tr = reference or order_id or ""
    return f"upi://pay?pa={quote(upi_id)}&pn={quote(merchant_name)}&am={amount:.2f}&cu=INR&tn={quote(note)}&tr={quote(tr)}"

# ============================================================================
# 4. EMAIL PARSING
# ============================================================================
def looks_like_fampay(sender, subject, body_preview):
    haystack = f"{sender} {subject} {body_preview[:1000]}".lower()
    return any(hint in haystack for hint in FAMPAY_SENDER_HINTS)

def extract_fampay_txn_id(text):
    m = re.search(r"\b(FMP[A-Z0-9]{8,24})\b", text, re.IGNORECASE)
    return m.group(1) if m else None

def extract_utr(text):
    explicit = re.search(r"UTR\s*[:\s#]*([0-9]{12})", text, re.IGNORECASE)
    if explicit: return explicit.group(1)
    fmp = extract_fampay_txn_id(text)
    if fmp: return fmp
    bare = re.search(r"(?:^|[^A-Za-z0-9])([0-9]{12})(?:[^A-Za-z0-9]|$)", text)
    return bare.group(1) if bare else None

def extract_amount(text):
    patterns = [
        r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)",
        r"received\s*(?:Rs\.?|INR|₹)?\s*([\d,]+(?:\.\d{1,2})?)",
        r"₹\s*([\d,]+(?:\.\d{1,2})?)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m and m.group(1):
            try:
                val = float(m.group(1).replace(",", ""))
                if val > 0: return val
            except ValueError: pass
    return None

def extract_purpose(text):
    direct = re.search(r"\b((?:test_)?ord_?[a-zA-Z0-9]{6,32})\b", text, re.IGNORECASE)
    return direct.group(1) if direct else None

def decode_mime_header_str(value):
    if not value: return ""
    parts = decode_header(value)
    decoded = []
    for content, enc in parts:
        if isinstance(content, bytes): decoded.append(content.decode(enc or "utf-8", errors="replace"))
        else: decoded.append(str(content))
    return "".join(decoded)

def extract_message_body(msg):
    collected = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload: collected.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload: collected.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
    raw_text = "\n".join(collected)
    raw_text = re.sub(r"<[^>]+>", " ", raw_text)
    return re.sub(r"\s+", " ", raw_text).strip()

def amount_in_paise(val):
    try: return int(round(float(val) * 100))
    except: return None

# ============================================================================
# 5. MATCHING & FETCH
# ============================================================================
def is_eligible_mail(mail, order):
    email_paise = amount_in_paise(mail.get("amount"))
    order_paise = amount_in_paise(order.get("payable_amount"))
    if email_paise is None or order_paise is None or email_paise != order_paise: return False
    rec_ts = int(mail.get("received_ts") or 0)
    cre_ts = int(order.get("created_ts") or 0)
    exp_ts = int(order.get("expires_ts") or (cre_ts + 900))
    return (cre_ts - 7200) <= rec_ts <= (exp_ts + 7200)

def matches_explicit_reference(mail, order):
    if not is_eligible_mail(mail, order): return False
    order_id = re.sub(r"[^a-z0-9]", "", str(order.get("id", "")).lower())
    pay_ref = re.sub(r"[^a-z0-9]", "", str(order.get("reference", "")).lower())
    m_raw = re.sub(r"[^a-z0-9]", "", str(mail.get("raw", "")).lower())
    m_sub = re.sub(r"[^a-z0-9]", "", str(mail.get("subject", "")).lower())
    
    if order_id and (order_id in m_raw or order_id in m_sub): return True
    if pay_ref and len(pay_ref) >= 6 and (pay_ref in m_raw or pay_ref in m_sub): return True
    return True # Amount matched

def fetch_fampay_emails():
    user, pw = CONFIG["gmail_email"].strip().lower(), re.sub(r"\s+", "", CONFIG["gmail_app_password"])
    debug = {"connected": False, "totalScanned": 0, "famMatched": 0, "error": None}
    results = []
    if not user or not pw: return {"emails": results, "debug": debug}
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=10)
        imap.login(user, pw)
        debug["connected"] = True
        imap.select("INBOX", readonly=True)
        typ, data = imap.search(None, "ALL")
        msg_ids = data[0].split() if (typ == "OK" and data and data[0]) else []
        newest_ids = list(reversed(msg_ids[-40:]))
        debug["totalScanned"] = len(newest_ids)

        for mid in newest_ids:
            typ, msg_data = imap.fetch(mid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not msg_data[0]: continue
            msg = email.message_from_bytes(msg_data[0][1])
            subj, sender = decode_mime_header_str(msg.get("Subject", "")), decode_mime_header_str(msg.get("From", ""))
            try: rec_ts = int(parsedate_to_datetime(msg.get("Date", "")).timestamp())
            except: rec_ts = int(time.time())
            
            body = extract_message_body(msg)
            if looks_like_fampay(sender, subj, body):
                debug["famMatched"] += 1
                full_text = f"{subj}\n{body}"
                amt = extract_amount(full_text)
                if amt:
                    results.append({"utr": extract_utr(full_text), "orderId": extract_purpose(full_text), "amount": amt, "subject": subj, "received_ts": rec_ts, "raw": body[:800]})
            if len(results) >= 10: break
        imap.logout()
    except Exception as exc: debug["error"] = str(exc)
    return {"emails": results, "debug": debug}

def verify_order_against_mailbox(order):
    scan = fetch_fampay_emails()
    for mail in scan["emails"]:
        if matches_explicit_reference(mail, order):
            order["status"] = "paid"
            order["utr"] = mail.get("utr") or order.get("reference")
            order["paid_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            save_order_db(order["id"], order)
            return {"paid": True, "debug": scan["debug"]}
    return {"paid": False, "debug": scan["debug"]}

# ============================================================================
# 6. HANDLER
# ============================================================================
class handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self): self._handle_request()
    def do_POST(self): self._handle_request()

    def _handle_request(self):
        parsed = urlparse(self.path)
        query = {k.lower(): v[0].strip() for k, v in parse_qs(parsed.query).items()}
        raw_len = int(self.headers.get("Content-Length", "0") or "0")
        body = json.loads(self.rfile.read(raw_len).decode("utf-8")) if raw_len > 0 else {}

        action = (query.get("action") or body.get("action") or "").lower()
        if not action:
            if "/check" in parsed.path: action = "check"
            elif query.get("order_id") or body.get("order_id"): action = "check"
            else: action = "create"

        if action == "health":
            return self._send_json({"status": "ok", "mode": "vercel_firebase", "firebase_ready": db is not None})

        if action in ("check", "verify"):
            order_id = (query.get("order_id") or body.get("order_id") or "").strip()
            if not order_id: return self._send_json({"status": "error", "message": "order_id required"}, 400)
            
            # DB se order lao
            order = get_order_db(order_id) or reconstruct_serverless_order(order_id)
            if not order: return self._send_json({"status": "error", "message": "Order not found"}, 404)

            if order.get("status") == "pending" and int(time.time()) > int(order.get("expires_ts", 0)):
                order["status"] = "expired"
                save_order_db(order_id, order)

            verify_debug = None
            if order.get("status") == "pending":
                res = verify_order_against_mailbox(order)
                verify_debug = res.get("debug")

            return self._send_json({
                "status": order["status"], "order_id": order["id"], "amount": order["amount"],
                "utr": order.get("utr"), "paid_at": order.get("paid_at"), "debug": verify_debug
            })

        # Create Order
        req_key = self.headers.get("X-API-Key") or query.get("api_key") or body.get("api_key") or ""
        if CONFIG["api_key"] and req_key != CONFIG["api_key"]:
            return self._send_json({"status": "error", "code": "INVALID_API_KEY"}, 401)

        try: amount = round(float(body.get("amount") or query.get("amount") or 0), 2)
        except: amount = 0.0
        if amount < 1.0: return self._send_json({"status": "error", "message": "Min amount ₹1"}, 400)

        now_ts = int(time.time())
        exp_ts = now_ts + (CONFIG["expiry_minutes"] * 60)
        paise_val = int(round(amount * 100))
        reference = str(random.randint(100000000000, 999999999999))
        order_id = f"ord_{now_ts}_{paise_val}_{reference}"
        
        upi_link = build_upi_link(CONFIG["fampay_upi"], amount, CONFIG["merchant_name"], order_id, reference)
        qr_url = f"https://quickchart.io/qr?text={quote(upi_link)}&size=300"
        
        host = self.headers.get("Host", "localhost")
        check_url = f"https://{host}/api/index?action=check&order_id={quote(order_id)}"

        new_order = {
            "id": order_id, "amount": amount, "payable_amount": amount, "provider": "fampay",
            "upi_id": CONFIG["fampay_upi"], "reference": reference, "status": "pending",
            "created_ts": now_ts, "expires_ts": exp_ts, "paid_at": None,
        }

        save_order_db(order_id, new_order) # DB me save karo

        return self._send_json({
            "status": "success", "order_id": order_id, "amount": amount, "upi_link": upi_link,
            "qr_url": qr_url, "check_status_url": check_url, "expires_at": datetime.fromtimestamp(exp_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        }, 201)
