#!/usr/bin/env python3
"""
Swiggy Cash Loot — Telegram Bot (Full CLI equivalent + Shrestho Pujo)
=====================================================================
Commands:
  /start          - Help
  /login <phone>  - OTP login
  /session        - Paste session JSON
  /run            - Join DEFAULT links (10 links)
  /join <link>    - Join one specific link
  /joinmany       - Paste text with multiple links
  /status         - Current loot status
  /invite [N]     - Invite nearby users
  /logout         - Clear session
  /cancel         - Cancel
  /nightcash      - Late night cash
  /pyo            - NEW 50 FC (Pick Your Offer)
  /karwachauth    - Karwa Chauth Pick Your Offer
  /bulk           - Bulk claim (up to 10 session JSONs)
  /pujo           - Shrestho Pujo auto vote + claim
  /pujoinvite     - Shrestho Pujo invite claim
"""

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import sys
import shutil
import signal
import subprocess
import time as _time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

# ─────────────────────────────────────────────────────────────────────────────
# BOT TOKEN + ADMIN
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "9081337647:AAFF2o1zWsOy_8TE6LkRQYkKx7_01QD9FzQ").strip()
ADMIN_IDS: List[int] = [5427735251]

SETTINGS_FILE = "bot_settings.json"
STATS_FILE = "bot_stats.json"
SESSIONS_DIR = "tg_sessions"
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "300"))
MAX_CONCURRENT_UPDATES = int(os.getenv("MAX_CONCURRENT_UPDATES", "32"))
os.makedirs(SESSIONS_DIR, exist_ok=True)


def load_settings() -> Dict:
    if os.path.isfile(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"force_join_enabled": False, "force_channels": [], "locked_buttons": []}


def save_settings(data: Dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_stats() -> Dict:
    if os.path.isfile(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("users", {})
                data.setdefault("claims", [])
                data.setdefault("history", [])
                data.setdefault("total_claim_attempts", 0)
                data.setdefault("successful_claims", 0)
                data.setdefault("failed_claims", 0)
                return data
        except Exception:
            pass
    return {"users": {}, "claims": [], "history": [], "total_claim_attempts": 0,
            "successful_claims": 0, "failed_claims": 0}


BOT_SETTINGS = load_settings()
BOT_SETTINGS.setdefault("locked_buttons", [])
BOT_SETTINGS.setdefault("force_channels", [])
BOT_STATS = load_stats()
STATS_LOCK = asyncio.Lock()


def _session_path(chat_id: int) -> str:
    return os.path.join(SESSIONS_DIR, f"{chat_id}.json")


def save_user_session(chat_id: int, session: Dict):
    payload = {"session": session, "saved_at": datetime.now(timezone.utc).timestamp()}
    _atomic_json_write(_session_path(chat_id), payload)
    user_sessions[chat_id] = session
    user_session_times[chat_id] = payload["saved_at"]


def load_user_session(chat_id: int) -> Optional[Dict]:
    path = _session_path(chat_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            payload = json.load(f)
        saved_at = float(payload.get("saved_at") or 0)
        age = datetime.now(timezone.utc).timestamp() - saved_at
        if age > SESSION_TTL_SECONDS:
            try:
                os.remove(path)
            except Exception:
                pass
            user_sessions.pop(chat_id, None)
            user_session_times.pop(chat_id, None)
            return None
        session = payload.get("session") or {}
        user_sessions[chat_id] = session
        user_session_times[chat_id] = saved_at
        return session
    except Exception:
        return None


def touch_session(chat_id: int):
    if chat_id in user_sessions:
        save_user_session(chat_id, user_sessions[chat_id])


def clear_user_session(chat_id: int):
    user_sessions.pop(chat_id, None)
    user_session_times.pop(chat_id, None)
    path = _session_path(chat_id)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Campaign constants
# ─────────────────────────────────────────────────────────────────────────────
NOTIFY_URL = "https://events.swiggy.com/api/notify"

LN_CAMPAIGN_PREFIX = "byo-ln-v6-"
LN_PAGE_URL = "https://events.swiggy.com/pick-your-late-night-offer"
LN_OFFERS = {1: "₹50 Free Cash (min ₹199)", 2: "₹150 OFF Coupon (min ₹249)",
             3: "₹100 OFF Coupon (min ₹149)", 4: "Offer 4"}
LN_DAY_BOUNDARIES = [
    (1, "2026-11-01T11:00:00+05:30", "2026-11-02T06:00:00+05:30"),
    (2, "2026-11-02T11:00:00+05:30", "2026-11-03T06:00:00+05:30"),
    (3, "2026-11-03T11:00:00+05:30", "2026-11-04T06:00:00+05:30"),
    (4, "2026-11-04T11:00:00+05:30", "2026-11-05T06:00:00+05:30"),
    (5, "2026-11-05T11:00:00+05:30", "2026-11-06T06:00:00+05:30"),
    (6, "2026-11-06T11:00:00+05:30", "2026-11-07T06:00:00+05:30"),
    (7, "2026-11-07T11:00:00+05:30", "2026-11-08T06:00:00+05:30"),
    (8, "2026-11-08T11:00:00+05:30", "2026-11-09T06:00:00+05:30"),
    (9, "2026-11-09T11:00:00+05:30", "2026-11-10T06:00:00+05:30"),
    (10, "2026-11-10T11:00:00+05:30", "2026-11-11T06:00:00+05:30"),
    (11, "2026-11-11T11:00:00+05:30", "2026-11-12T06:00:00+05:30"),
    (12, "2026-11-12T11:00:00+05:30", "2026-11-13T06:00:00+05:30"),
    (13, "2026-11-13T11:00:00+05:30", "2026-11-14T06:00:00+05:30"),
    (14, "2026-11-14T11:00:00+05:30", "2026-11-15T06:00:00+05:30"),
    (15, "2026-11-15T11:00:00+05:30", "2026-11-16T06:00:00+05:30"),
    (16, "2026-11-16T11:00:00+05:30", "2026-11-17T06:00:00+05:30"),
    (17, "2026-11-17T11:00:00+05:30", "2026-11-18T06:00:00+05:30"),
    (18, "2026-11-18T11:00:00+05:30", "2026-11-19T06:00:00+05:30"),
    (19, "2026-11-19T11:00:00+05:30", "2026-11-20T06:00:00+05:30"),
    (20, "2026-11-20T11:00:00+05:30", "2026-11-21T06:00:00+05:30"),
    (21, "2026-11-21T11:00:00+05:30", "2026-11-22T06:00:00+05:30"),
    (22, "2026-11-22T11:00:00+05:30", "2026-11-23T06:00:00+05:30"),
    (23, "2026-11-23T11:00:00+05:30", "2026-11-24T06:00:00+05:30"),
    (24, "2026-11-24T11:00:00+05:30", "2026-11-25T06:00:00+05:30"),
    (25, "2026-11-25T11:00:00+05:30", "2026-11-26T06:00:00+05:30"),
    (26, "2026-11-26T11:00:00+05:30", "2026-11-27T06:00:00+05:30"),
    (27, "2026-11-27T11:00:00+05:30", "2026-11-28T06:00:00+05:30"),
    (28, "2026-11-28T11:00:00+05:30", "2026-11-29T06:00:00+05:30"),
    (29, "2026-11-29T11:00:00+05:30", "2026-11-30T06:00:00+05:30"),
    (30, "2026-11-30T11:00:00+05:30", "2026-12-01T06:00:00+05:30"),
]

BF_CAMPAIGN_PREFIX = "byo-bf-v11-"
BF_PAGE_URL = "https://events.swiggy.com/book-your-offer"
BF_OFFERS = {1: "₹50 FREE Cash (breakfast ≥ ₹199)", 2: "₹150 OFF Coupon (breakfast ≥ ₹249)",
             3: "₹100 OFF + ₹50 Free Cash (≥ ₹249)", 4: "₹100 OFF Coupon (breakfast ≥ ₹149)"}
BF_DAY_BOUNDARIES = [
    (1, "2026-11-01T11:00:00+05:30", "2026-11-02T11:00:00+05:30"),
    (2, "2026-11-02T11:00:00+05:30", "2026-11-03T11:00:00+05:30"),
    (3, "2026-11-03T11:00:00+05:30", "2026-11-04T11:00:00+05:30"),
    (4, "2026-11-04T11:00:00+05:30", "2026-11-05T11:00:00+05:30"),
    (5, "2026-11-05T11:00:00+05:30", "2026-11-06T11:00:00+05:30"),
    (6, "2026-11-06T11:00:00+05:30", "2026-11-07T11:00:00+05:30"),
    (7, "2026-11-07T11:00:00+05:30", "2026-11-08T11:00:00+05:30"),
    (8, "2026-11-08T11:00:00+05:30", "2026-11-09T11:00:00+05:30"),
    (9, "2026-11-09T11:00:00+05:30", "2026-11-10T11:00:00+05:30"),
    (10, "2026-11-10T11:00:00+05:30", "2026-11-11T11:00:00+05:30"),
    (11, "2026-11-11T11:00:00+05:30", "2026-11-12T11:00:00+05:30"),
    (12, "2026-11-12T11:00:00+05:30", "2026-11-13T11:00:00+05:30"),
    (13, "2026-11-13T11:00:00+05:30", "2026-11-14T11:00:00+05:30"),
    (14, "2026-11-14T11:00:00+05:30", "2026-11-15T11:00:00+05:30"),
    (15, "2026-11-15T11:00:00+05:30", "2026-11-16T11:00:00+05:30"),
    (16, "2026-11-16T11:00:00+05:30", "2026-11-17T11:00:00+05:30"),
    (17, "2026-11-17T11:00:00+05:30", "2026-11-18T11:00:00+05:30"),
    (18, "2026-11-18T11:00:00+05:30", "2026-11-19T11:00:00+05:30"),
    (19, "2026-11-19T11:00:00+05:30", "2026-11-20T11:00:00+05:30"),
    (20, "2026-11-20T11:00:00+05:30", "2026-11-21T11:00:00+05:30"),
    (21, "2026-11-21T11:00:00+05:30", "2026-11-22T11:00:00+05:30"),
    (22, "2026-11-22T11:00:00+05:30", "2026-11-23T11:00:00+05:30"),
    (23, "2026-11-23T11:00:00+05:30", "2026-11-24T11:00:00+05:30"),
    (24, "2026-11-24T11:00:00+05:30", "2026-11-25T11:00:00+05:30"),
    (25, "2026-11-25T11:00:00+05:30", "2026-11-26T11:00:00+05:30"),
    (26, "2026-11-26T11:00:00+05:30", "2026-11-27T11:00:00+05:30"),
    (27, "2026-11-27T11:00:00+05:30", "2026-11-28T11:00:00+05:30"),
    (28, "2026-11-28T11:00:00+05:30", "2026-11-29T11:00:00+05:30"),
    (29, "2026-11-29T11:00:00+05:30", "2026-11-30T11:00:00+05:30"),
    (30, "2026-11-30T11:00:00+05:30", "2026-12-01T11:00:00+05:30"),
]

# ── Pick Your Offer (NEW 50 FC) ──
PYO_PAGE_URL = "https://events.swiggy.com/pick-your-offer"
PYO_CAMPAIGN_ID = "pyo-v1-2"
PYO_OFFERS = {
    1: "₹50 FREE Cash (min ₹199)",
    2: "₹150 OFF Coupon (min ₹249)",
    3: "₹130 OFF Coupon (min ₹199)",
    4: "₹100 OFF Coupon (min ₹149)",
}


async def _fetch_pyo_campaign_id() -> str:
    """Refresh notifyCampaignId from live page when available."""
    global PYO_CAMPAIGN_ID
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            async with http.get(
                PYO_PAGE_URL,
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-IN"},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                html = await resp.text()
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
        if m:
            nd = json.loads(m.group(1)).get("props", {}).get("pageProps", {}).get("nexusData") or {}
            cid = str(nd.get("notifyCampaignId") or "").strip()
            if cid:
                PYO_CAMPAIGN_ID = cid
    except Exception as e:
        log.warning("PYO campaign fetch failed: %s", e)
    return PYO_CAMPAIGN_ID


async def pyo_get_status(session: Dict) -> Dict[str, Any]:
    campaign = await _fetch_pyo_campaign_id()
    headers = {
        "Content-Type": "application/json",
        "token": str(session.get("token") or ""),
        "tid": str(session.get("tid") or ""),
    }
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            async with http.get(
                f"{NOTIFY_URL}?campaign={campaign}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json(content_type=None)
                return {"ok": resp.status == 200, "status": resp.status, "data": data or {}, "campaign": campaign}
    except Exception as e:
        return {"ok": False, "status": 0, "data": {}, "msg": str(e), "campaign": campaign}


async def pyo_claim_offer(session: Dict, offer_id: int) -> Dict[str, Any]:
    """Real app flow:
    1) POST /api/pick-your-offer-coupon  (creates free-cash / coupon on account)
    2) POST or PATCH /api/notify with note.offerEndTime
    """
    campaign = await _fetch_pyo_campaign_id()
    token = str(session.get("token") or "")
    tid = str(session.get("tid") or "")
    headers_base = {
        "Content-Type": "application/json",
        "token": token,
        "tid": tid,
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://events.swiggy.com",
        "Referer": PYO_PAGE_URL,
    }
    steps: Dict[str, Any] = {"campaign": campaign, "offer_id": offer_id}
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            # Step 1: create coupon / free cash on account
            coupon_headers = dict(headers_base)
            coupon_headers["offerindex"] = str(offer_id)
            async with http.post(
                "https://events.swiggy.com/api/pick-your-offer-coupon",
                headers=coupon_headers,
                json={},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                try:
                    coupon_data = await resp.json(content_type=None)
                except Exception:
                    coupon_data = {"raw": (await resp.text())[:400]}
                steps["coupon_status"] = resp.status
                steps["coupon"] = coupon_data
                coupon_ok = resp.status in (200, 201) and not (
                    isinstance(coupon_data, dict) and coupon_data.get("error")
                )

            if not coupon_ok:
                return {
                    "ok": False,
                    "status": steps.get("coupon_status", 0),
                    "data": coupon_data if isinstance(coupon_data, dict) else {},
                    "msg": f"coupon create failed: {coupon_data}",
                    "campaign": campaign,
                    "steps": steps,
                }

            # Extract end_date for notify note (app sends offerEndTime)
            end_date = None
            if isinstance(coupon_data, dict):
                inner = coupon_data.get("data") if isinstance(coupon_data.get("data"), dict) else coupon_data
                if isinstance(inner, dict):
                    end_date = inner.get("end_date") or inner.get("endDate") or inner.get("offerEndTime")
                    steps["coupon_code"] = inner.get("coupon_code") or inner.get("couponCode") or inner.get("code")
                    steps["amount"] = inner.get("amount") or inner.get("value") or inner.get("offerValue")

            note = json.dumps({"offerEndTime": end_date}) if end_date is not None else None

            # Step 2: check existing notify status → POST or PATCH
            already_claimed = False
            async with http.get(
                f"{NOTIFY_URL}?campaign={campaign}",
                headers=headers_base,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                try:
                    st_data = await resp.json(content_type=None)
                except Exception:
                    st_data = {}
                steps["status_before"] = st_data
                if isinstance(st_data, dict) and st_data.get("value") not in (None, "", 0, "0"):
                    already_claimed = True

            notify_body: Dict[str, Any] = {"campaign": campaign, "value": offer_id}
            if note:
                notify_body["note"] = note
            method = "PATCH" if already_claimed else "POST"
            async with http.request(
                method,
                NOTIFY_URL,
                headers=headers_base,
                json=notify_body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                try:
                    notify_data = await resp.json(content_type=None)
                except Exception:
                    notify_data = {"raw": (await resp.text())[:300]}
                steps["notify_method"] = method
                steps["notify_status"] = resp.status
                steps["notify"] = notify_data
                notify_ok = resp.status == 200 and not (
                    isinstance(notify_data, dict) and notify_data.get("error")
                )

            # Verify again
            async with http.get(
                f"{NOTIFY_URL}?campaign={campaign}",
                headers=headers_base,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                try:
                    st_after = await resp.json(content_type=None)
                except Exception:
                    st_after = {}
                steps["status_after"] = st_after

            ok = bool(coupon_ok)
            return {
                "ok": ok,
                "status": steps.get("coupon_status", 0),
                "data": coupon_data if isinstance(coupon_data, dict) else {},
                "notify_ok": notify_ok,
                "campaign": campaign,
                "steps": steps,
                "coupon_code": steps.get("coupon_code"),
                "end_date": end_date,
            }
    except Exception as e:
        log.exception("pyo_claim_offer failed")
        return {"ok": False, "status": 0, "data": {}, "msg": str(e), "campaign": campaign, "steps": steps}



# ── Karwa Chauth BYO ──
KC_PAGE_URL = "https://events.swiggy.com/karwa-chauth-byo"
KC_CAMPAIGN_ID = "karwa-chauth-byo"
KC_OFFERS = {
    1: "₹50 FREE CASH (above ₹249)",
    2: "₹150 OFF Coupon (above ₹299)",
    3: "₹200 OFF Coupon (above ₹499)",
    4: "₹30 FREE Cash (above ₹149)",
}
KC_VOTE_START = "2026-10-27T00:00:00+05:30"
KC_VOTE_END = "2026-10-29T12:00:00+05:30"
KC_REDEEM_START = "2026-10-29T15:00:00+05:30"
KC_REDEEM_END = "2026-10-30T06:00:00+05:30"


async def _fetch_kc_config() -> Dict[str, Any]:
    """Refresh campaign + windows from live page."""
    global KC_CAMPAIGN_ID, KC_VOTE_START, KC_VOTE_END, KC_REDEEM_START, KC_REDEEM_END
    cfg: Dict[str, Any] = {"campaign": KC_CAMPAIGN_ID}
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            async with http.get(
                KC_PAGE_URL,
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-IN"},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                html = await resp.text()
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if m:
            nd = json.loads(m.group(1)).get("props", {}).get("pageProps", {}).get("nexusData") or {}
            cid = str(nd.get("notifyCampaignId") or "").strip()
            if cid:
                KC_CAMPAIGN_ID = cid
                cfg["campaign"] = cid
            vw = nd.get("votingWindow") or {}
            rw = nd.get("redeemWindow") or {}
            if vw.get("startTime"):
                KC_VOTE_START = str(vw["startTime"])
            if vw.get("endTime"):
                KC_VOTE_END = str(vw["endTime"])
            if rw.get("startTime"):
                KC_REDEEM_START = str(rw["startTime"])
            if rw.get("endTime"):
                KC_REDEEM_END = str(rw["endTime"])
            cfg["vote_start"] = KC_VOTE_START
            cfg["vote_end"] = KC_VOTE_END
            cfg["redeem_start"] = KC_REDEEM_START
            cfg["redeem_end"] = KC_REDEEM_END
    except Exception as e:
        log.warning("KC config fetch failed: %s", e)
    return cfg


def _kc_window_state() -> Dict[str, Any]:
    now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    try:
        vs = datetime.fromisoformat(KC_VOTE_START)
        ve = datetime.fromisoformat(KC_VOTE_END)
        rs = datetime.fromisoformat(KC_REDEEM_START)
        re_ = datetime.fromisoformat(KC_REDEEM_END)
    except Exception:
        return {"can_select": True, "can_redeem": False, "phase": "unknown", "now": now.isoformat()}
    if vs <= now < ve:
        return {"can_select": True, "can_redeem": False, "phase": "selection", "now": now.isoformat(),
                "vote_end": KC_VOTE_END, "redeem_start": KC_REDEEM_START}
    if rs <= now < re_:
        return {"can_select": False, "can_redeem": True, "phase": "redeem", "now": now.isoformat(),
                "redeem_end": KC_REDEEM_END}
    if now < vs:
        return {"can_select": False, "can_redeem": False, "phase": "upcoming", "now": now.isoformat(),
                "vote_start": KC_VOTE_START}
    if ve <= now < rs:
        return {"can_select": False, "can_redeem": False, "phase": "waiting_redeem", "now": now.isoformat(),
                "redeem_start": KC_REDEEM_START}
    return {"can_select": False, "can_redeem": False, "phase": "ended", "now": now.isoformat()}


async def kc_get_status(session: Dict) -> Dict[str, Any]:
    await _fetch_kc_config()
    campaign = KC_CAMPAIGN_ID
    headers = {
        "Content-Type": "application/json",
        "token": str(session.get("token") or ""),
        "tid": str(session.get("tid") or ""),
    }
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            async with http.get(
                f"{NOTIFY_URL}?campaign={campaign}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json(content_type=None)
                return {"ok": resp.status == 200, "status": resp.status, "data": data or {}, "campaign": campaign}
    except Exception as e:
        return {"ok": False, "status": 0, "data": {}, "msg": str(e), "campaign": campaign}


async def kc_claim_offer(session: Dict, offer_id: int) -> Dict[str, Any]:
    """Karwa Chauth real flow (same pattern as PYO BYO):
    1) Try coupon create endpoints
    2) POST/PATCH /api/notify with campaign + value
    """
    await _fetch_kc_config()
    campaign = KC_CAMPAIGN_ID
    token = str(session.get("token") or "")
    tid = str(session.get("tid") or "")
    headers_base = {
        "Content-Type": "application/json",
        "token": token,
        "tid": tid,
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://events.swiggy.com",
        "Referer": KC_PAGE_URL,
    }
    steps: Dict[str, Any] = {"campaign": campaign, "offer_id": offer_id}
    window = _kc_window_state()
    steps["window"] = window

    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            # Step 1: try coupon / free-cash create (multiple known BYO endpoints)
            coupon_endpoints = [
                "https://events.swiggy.com/api/pick-your-offer-coupon",
                "https://events.swiggy.com/api/byo-coupon",
                "https://events.swiggy.com/api/karwa-chauth-coupon",
            ]
            coupon_ok = False
            coupon_data: Any = {}
            for ep in coupon_endpoints:
                coupon_headers = dict(headers_base)
                coupon_headers["offerindex"] = str(offer_id)
                coupon_headers["offerIndex"] = str(offer_id)
                try:
                    async with http.post(
                        ep,
                        headers=coupon_headers,
                        json={"campaign": campaign, "offerId": offer_id, "value": offer_id},
                        timeout=aiohttp.ClientTimeout(total=18),
                    ) as resp:
                        try:
                            coupon_data = await resp.json(content_type=None)
                        except Exception:
                            coupon_data = {"raw": (await resp.text())[:400]}
                        steps.setdefault("coupon_tries", []).append({
                            "url": ep, "status": resp.status, "data": coupon_data
                        })
                        if resp.status in (200, 201) and not (
                            isinstance(coupon_data, dict) and coupon_data.get("error")
                        ):
                            coupon_ok = True
                            steps["coupon_endpoint"] = ep
                            steps["coupon_status"] = resp.status
                            steps["coupon"] = coupon_data
                            break
                except Exception as e:
                    steps.setdefault("coupon_tries", []).append({"url": ep, "error": str(e)})

            end_date = None
            if isinstance(coupon_data, dict):
                inner = coupon_data.get("data") if isinstance(coupon_data.get("data"), dict) else coupon_data
                if isinstance(inner, dict):
                    end_date = inner.get("end_date") or inner.get("endDate") or inner.get("offerEndTime")
                    steps["coupon_code"] = (
                        inner.get("coupon_code") or inner.get("couponCode") or inner.get("code")
                    )
                    steps["amount"] = inner.get("amount") or inner.get("value") or inner.get("offerValue")

            note = json.dumps({"offerEndTime": end_date}) if end_date is not None else None

            # Step 2: status → POST or PATCH notify (this is the real lock for BYO)
            already_claimed = False
            async with http.get(
                f"{NOTIFY_URL}?campaign={campaign}",
                headers=headers_base,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                try:
                    st_data = await resp.json(content_type=None)
                except Exception:
                    st_data = {}
                steps["status_before"] = st_data
                if isinstance(st_data, dict) and st_data.get("value") not in (None, "", 0, "0"):
                    already_claimed = True
                    steps["already"] = st_data.get("value")

            if already_claimed:
                return {
                    "ok": True,
                    "already": True,
                    "status": 200,
                    "data": steps.get("status_before") or {},
                    "campaign": campaign,
                    "steps": steps,
                    "msg": "already claimed",
                }

            notify_body: Dict[str, Any] = {"campaign": campaign, "value": offer_id}
            if note:
                notify_body["note"] = note
            method = "POST"
            async with http.request(
                method,
                NOTIFY_URL,
                headers=headers_base,
                json=notify_body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                try:
                    notify_data = await resp.json(content_type=None)
                except Exception:
                    notify_data = {"raw": (await resp.text())[:300]}
                steps["notify_method"] = method
                steps["notify_status"] = resp.status
                steps["notify"] = notify_data
                notify_ok = resp.status == 200 and not (
                    isinstance(notify_data, dict) and notify_data.get("error")
                )

            # Verify
            async with http.get(
                f"{NOTIFY_URL}?campaign={campaign}",
                headers=headers_base,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                try:
                    st_after = await resp.json(content_type=None)
                except Exception:
                    st_after = {}
                steps["status_after"] = st_after

            verified = False
            if isinstance(st_after, dict):
                val = st_after.get("value")
                try:
                    verified = int(val) == int(offer_id)
                except Exception:
                    verified = str(val) == str(offer_id) and val not in (None, "", 0, "0")

            ok = bool(notify_ok or verified or coupon_ok)
            return {
                "ok": ok,
                "status": steps.get("notify_status") or steps.get("coupon_status") or 0,
                "data": notify_data if isinstance(notify_data, dict) else {},
                "notify_ok": notify_ok,
                "verified": verified,
                "campaign": campaign,
                "steps": steps,
                "coupon_code": steps.get("coupon_code"),
                "end_date": end_date,
                "window": window,
            }
    except Exception as e:
        log.exception("kc_claim_offer failed")
        return {"ok": False, "status": 0, "data": {}, "msg": str(e), "campaign": campaign, "steps": steps}


# ── Shrestho Pujo ──
PUJO_CAMPAIGN = "shreshto_pujo"
PUJO_PAGE_URL = "https://events.swiggy.com/shreshto_pujo"
PUJO_VOTE_AMOUNT = 10
PUJO_VOTE_MAX = 50
PUJO_INVITE_AMOUNT = 10
PUJO_INVITE_MAX = 50
PUJO_TOTAL_MAX = 100
PUJO_DEFAULT_LAT, PUJO_DEFAULT_LNG = 22.5726, 88.3639

_OFFER_CFG_CACHE: Dict[str, Any] = {}
_OFFER_CFG_TS: Dict[str, float] = {}


def _offer_day(boundaries) -> Optional[int]:
    now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    for day, start_s, end_s in boundaries:
        start = datetime.fromisoformat(start_s)
        end = datetime.fromisoformat(end_s)
        if start <= now < end:
            return day
    return None


async def _fetch_byo_page_config(page_url: str) -> Dict[str, Any]:
    now = _time.time()
    if page_url in _OFFER_CFG_CACHE and now - _OFFER_CFG_TS.get(page_url, 0) < 300:
        return _OFFER_CFG_CACHE[page_url]
    cfg: Dict[str, Any] = {}
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            async with http.get(
                page_url,
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-IN"},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                html = await resp.text()
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
        if m:
            nd = json.loads(m.group(1)).get("props", {}).get("pageProps", {}).get("nexusData") or {}
            prefix = nd.get("notifyCampaignIdPrefix") or ""
            bounds = []
            for b in nd.get("dayBoundaries") or []:
                try:
                    bounds.append((int(b["day"]), str(b["startTime"]), str(b["endTime"])))
                except Exception:
                    continue
            cfg = {"prefix": prefix, "boundaries": bounds}
    except Exception as e:
        log.warning("BYO page fetch failed %s: %s", page_url, e)
    if cfg:
        _OFFER_CFG_CACHE[page_url] = cfg
        _OFFER_CFG_TS[page_url] = now
    return cfg


async def ln_current_day() -> Optional[int]:
    global LN_CAMPAIGN_PREFIX, LN_DAY_BOUNDARIES
    cfg = await _fetch_byo_page_config(LN_PAGE_URL)
    if cfg.get("prefix"):
        LN_CAMPAIGN_PREFIX = cfg["prefix"]
    if cfg.get("boundaries"):
        LN_DAY_BOUNDARIES = cfg["boundaries"]
    return _offer_day(LN_DAY_BOUNDARIES)


async def bf_current_day() -> Optional[int]:
    global BF_CAMPAIGN_PREFIX, BF_DAY_BOUNDARIES
    cfg = await _fetch_byo_page_config(BF_PAGE_URL)
    if cfg.get("prefix"):
        BF_CAMPAIGN_PREFIX = cfg["prefix"]
    if cfg.get("boundaries"):
        BF_DAY_BOUNDARIES = cfg["boundaries"]
    return _offer_day(BF_DAY_BOUNDARIES)


LN_NOTIFY_URL = NOTIFY_URL

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S", level=logging.INFO)
log = logging.getLogger("cash_loot_bot")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
SPNS_BASE_URL    = "https://spns.swiggy.com"
PROFILE_BASE_URL = "https://profile.swiggy.com"

CASHLOOT_CREATE_OFFERS_PATH = "/api/v1/proximity-offer/create-offers"
CASHLOOT_DISCOVER_PATH      = "/api/v1/proximity-offer/discover-users"
CASHLOOT_TRANSACTIONS_PATH  = "/api/v1/proximity-offer/campaigns/{campaign_id}/transactions"

DEFAULT_CAMPAIGN_ID = "ydiHi75"
CASHLOOT_CAMPAIGN_CAP = 100

DELHI_AREAS: List[Tuple[str, float, float]] = [
    ("Delhi - Central", 28.6429, 77.2191), ("Delhi - Connaught Place", 28.6315, 77.2167),
    ("Delhi - New Delhi", 28.6139, 77.2090), ("Delhi - Karol Bagh", 28.6519, 77.1909),
    ("Delhi - Saket", 28.5245, 77.2066), ("Delhi - Hauz Khas", 28.5433, 77.2066),
    ("Delhi - Nehru Place", 28.5481, 77.2546), ("Delhi - Dwarka", 28.5921, 77.0460),
    ("Delhi - Punjabi Bagh", 28.6747, 77.1310), ("Delhi - Janakpuri", 28.6293, 77.0777),
    ("Delhi - Pitampura", 28.6899, 77.1180), ("Delhi - Rohini", 28.7495, 77.0565),
    ("Delhi - Mayur Vihar", 28.5989, 77.2795), ("Delhi - Laxmi Nagar", 28.6300, 77.2430),
    ("Delhi - Vasant Kunj", 28.5200, 77.1591), ("Delhi - Aerocity", 28.5445, 77.1610),
    ("Delhi - Faridabad", 28.4089, 77.3178), ("Delhi - Gurgaon", 28.4595, 77.0266),
    ("Delhi - Noida", 28.5355, 77.3910), ("Delhi - Greater Noida", 28.4744, 77.5040),
    ("Delhi - Ghaziabad", 28.6692, 77.4538),
]

INVITE_CITIES: List[Tuple[str, float, float]] = DELHI_AREAS + [
    ("Mumbai", 19.0760, 72.8777), ("Bengaluru", 12.9716, 77.5946),
    ("Hyderabad", 17.3850, 78.4867), ("Chennai", 13.0827, 80.2707),
    ("Kolkata", 22.5726, 88.3639), ("Pune", 18.5204, 73.8567),
    ("Ahmedabad", 23.0225, 72.5714), ("Jaipur", 26.9124, 75.7873),
    ("Lucknow", 26.8467, 80.9462), ("Kochi", 9.9312, 76.2673),
    ("Chandigarh", 30.7333, 76.7794), ("Guwahati", 26.1445, 91.7362),
    ("Bhopal", 23.2599, 77.4126), ("Patna", 25.5941, 85.1376),
    ("Nagpur", 21.1458, 79.0882), ("Indore", 22.7196, 75.8577),
    ("Coimbatore", 11.0168, 76.9558), ("Visakhapatnam", 17.6868, 83.2185),
    ("Surat", 21.1702, 72.8311),
]

_LINK_NAMES = ["Salauddin", "Miraj Ali", "Rahul Sharma", "Priya Verma", "Amit Patel",
               "Sneha Reddy", "Vikram Singh", "Ananya Gupta", "Rohan Mehta", "Kavya Iyer",
               "Arjun Nair", "Ishita Khan", "Siddharth Joshi", "Neha Kapoor", "Aditya Rao"]

DEFAULT_LINKS: List[Tuple[str, str]] = [
    ("Salauddin",    "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1BK3e"),
    ("Miraj Ali",    "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1KmUr"),
    ("Rahul Sharma", "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1owO9"),
    ("Priya Verma",  "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1J1Rj"),
    ("Amit Patel",   "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1KwTj"),
    ("Sneha Reddy",  "https://r.swiggy.com/cashloot/ydiHi75-hYhN1G1KsXE"),
    ("Vikram Singh", "https://r.swiggy.com/cashloot/ydiHi75-hYhN1Ho45Ae"),
    ("Ananya Gupta", "https://r.swiggy.com/cashloot/ydiHi75-hYhN1HgTZYB"),
    ("Rohan Mehta",  "https://r.swiggy.com/cashloot/ydiHi75-hYhN1Hfw2iQ"),
    ("Kavya Iyer",   "https://r.swiggy.com/cashloot/ydiHi75-hYhN1He1NZi"),
]


def _random_link_name() -> str:
    return secrets.choice(_LINK_NAMES)


def shuffled_default_links() -> List[Tuple[str, str]]:
    names = list(_LINK_NAMES)
    secrets.SystemRandom().shuffle(names)
    urls = [u for _, u in DEFAULT_LINKS]
    out = []
    for i, url in enumerate(urls):
        out.append((names[i % len(names)], url))
    return out


_CASHLOOT_URL_RE = re.compile(r"r\.swiggy\.com/cashloot/([A-Za-z0-9_-]+)", re.IGNORECASE)

LINK_DELAY_SECONDS = 2.5
CASHLOOT_LEDGER_WAIT_SECONDS = 4.0
DEFAULT_RETRIES = 2
RETRY_DELAY_SECONDS = 4.0

SWIGGY_APP_HEADERS = {
    "user-agent": "Swiggy-Android",
    "content-type": "application/json; charset=utf-8",
    "accept": "application/json; charset=utf-8",
    "version-code": "1795",
    "app-version": "4.113.0",
    "manufacturer": "MOTOROLA",
    "model-name": "MOTO G(60)",
}

WAITING_OTP, WAITING_SESSION, WAITING_LINKS = range(3)

user_sessions: Dict[int, Dict] = {}
user_session_times: Dict[int, float] = {}
user_pending_otp: Dict[int, Dict] = {}

user_job_locks: Dict[int, asyncio.Lock] = {}
user_job_locks_guard = asyncio.Lock()


async def get_user_job_lock(chat_id: int) -> asyncio.Lock:
    async with user_job_locks_guard:
        lock = user_job_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            user_job_locks[chat_id] = lock
        return lock


def _atomic_json_write(path: str, payload: Dict[str, Any]) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


async def record_user_activity(update: Update, verified: bool = False) -> None:
    user = update.effective_user
    if not user:
        return
    uid = str(user.id)
    now = datetime.now(timezone.utc).isoformat()
    async with STATS_LOCK:
        entry = BOT_STATS.setdefault("users", {}).setdefault(uid, {
            "first_seen": now, "last_seen": now, "verified": False,
            "verified_at": None, "claims": 0, "successful_claims": 0,
        })
        entry["last_seen"] = now
        entry["username"] = user.username or ""
        entry["name"] = user.full_name or ""
        if verified:
            entry["verified"] = True
            entry["verified_at"] = entry.get("verified_at") or now
        _atomic_json_write(STATS_FILE, BOT_STATS)


async def record_claim(update: Update, day: int, offer_id: int, offer_name: str, ok: bool, error: str = "") -> None:
    user = update.effective_user
    if not user:
        return
    uid = str(user.id)
    now = datetime.now(timezone.utc).isoformat()
    async with STATS_LOCK:
        entry = BOT_STATS.setdefault("users", {}).setdefault(uid, {
            "first_seen": now, "last_seen": now, "verified": False,
            "verified_at": None, "claims": 0, "successful_claims": 0,
        })
        entry["last_seen"] = now
        entry["username"] = user.username or ""
        entry["name"] = user.full_name or ""
        entry["claims"] = int(entry.get("claims", 0)) + 1
        if ok:
            entry["successful_claims"] = int(entry.get("successful_claims", 0)) + 1
            BOT_STATS["successful_claims"] = int(BOT_STATS.get("successful_claims", 0)) + 1
        else:
            BOT_STATS["failed_claims"] = int(BOT_STATS.get("failed_claims", 0)) + 1
        BOT_STATS["total_claim_attempts"] = int(BOT_STATS.get("total_claim_attempts", 0)) + 1
        history = BOT_STATS.setdefault("claims", [])
        history.append({
            "time": now, "telegram_id": user.id, "username": user.username or "",
            "name": user.full_name or "", "day": day, "offer_id": offer_id,
            "offer": offer_name, "status": "success" if ok else "failed",
            "error": error[:300] if error else "",
        })
        if len(history) > 1000:
            del history[:-1000]
        _atomic_json_write(STATS_FILE, BOT_STATS)


async def record_join_history(update: Update, link: Dict[str, str], ok: bool, error: str = "", earned: float = 0.0) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    now = datetime.now(timezone.utc).isoformat()
    async with STATS_LOCK:
        history = BOT_STATS.setdefault("history", [])
        history.append({
            "time": now, "telegram_id": uid, "type": "join",
            "label": link.get("label") or link.get("referrer_id") or "link",
            "status": "success" if ok else "failed",
            "earned": float(earned or 0),
            "error": str(error or "")[:300],
        })
        if len(history) > 1000:
            del history[:-1000]
        _atomic_json_write(STATS_FILE, BOT_STATS)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _random_device_id() -> str:
    return secrets.token_hex(8)


def _decode_tid_payload(tid: str) -> Dict:
    try:
        parts = tid.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
    except Exception:
        return {}


def _userid_from_tid(tid: str) -> Optional[str]:
    payload = _decode_tid_payload(tid)
    uid = payload.get("user_id")
    return str(uid) if uid else None


def _account_user_id(session: Dict) -> str:
    return str(session.get("userid") or _userid_from_tid(session.get("tid", "")) or "")


def _walk_json_objects(value: Any, max_depth: int = 6):
    if max_depth < 0:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_objects(child, max_depth - 1)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_objects(child, max_depth - 1)


def _clean_session_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def parse_session_string(raw: str) -> Dict:
    cleaned = (raw or "").strip()
    if not cleaned:
        raise ValueError("Empty session input.")
    cleaned = re.sub(r"^\s*```[^\n]*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()

    session: Dict[str, Any] = {}
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = None

    if data is not None:
        for source in _walk_json_objects(data):
            aliases = {
                "token": ("token", "secret_token", "secrettoken", "access_token"),
                "tid": ("tid",), "sid": ("sid",),
                "x-oztok": ("x-oztok", "oztok"),
                "userid": ("userid", "userId", "user_id"),
                "customerId": ("customerId", "customer_id"),
                "mobile": ("mobile", "phone", "phoneNumber"),
                "deviceId": ("deviceId", "device_id", "deviceid", "swuid"),
                "name": ("name", "displayName", "userName"),
                "cookies": ("cookies",),
            }
            lowered = {str(k).lower(): k for k in source.keys()}
            for target, keys in aliases.items():
                if target in session:
                    continue
                for key in keys:
                    actual = lowered.get(key.lower())
                    if actual is not None:
                        value = _clean_session_value(source.get(actual))
                        if value:
                            session[target] = value
                            break
            hname = str(source.get("name", "")).strip().lower()
            hvalue = _clean_session_value(source.get("value"))
            if hname and hvalue:
                header_map = {"token": "token", "tid": "tid", "sid": "sid",
                              "x-oztok": "x-oztok", "oztok": "x-oztok",
                              "userid": "userid", "user-id": "userid",
                              "x-user-id": "userid", "authorization": "token"}
                target = header_map.get(hname)
                if target:
                    if target == "token" and hvalue.lower().startswith("bearer "):
                        hvalue = hvalue[7:].strip()
                    session.setdefault(target, hvalue)

    if not session:
        patterns = {
            "token": r'["\']?(?:token|secret_token|secrettoken|access_token|authorization)["\']?\s*[:=]\s*["\']?([^"\',\s}]+)',
            "tid": r'["\']?tid["\']?\s*[:=]\s*["\']?([^"\',\s}]+)',
            "sid": r'["\']?sid["\']?\s*[:=]\s*["\']?([^"\',\s}]+)',
            "x-oztok": r'["\']?(?:x-oztok|oztok)["\']?\s*[:=]\s*["\']?([^"\',\s}]+)',
            "userid": r'["\']?(?:userid|userId|user_id)["\']?\s*[:=]\s*["\']?([^"\',\s}]+)',
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, cleaned, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                if key == "token" and value.lower().startswith("bearer"):
                    value = value.split(None, 1)[-1]
                session[key] = value

    if session.get("customerId") not in (None, ""):
        session["customerId"] = str(session["customerId"])
    if session.get("mobile") not in (None, ""):
        session["mobile"] = str(session["mobile"])
    if session.get("userid") not in (None, ""):
        session["userid"] = str(session["userid"])
    if "x-oztok" not in session and session.get("oztok"):
        session["x-oztok"] = session.pop("oztok")
    if not session.get("token") and not session.get("tid"):
        raise ValueError('No usable token/tid found.')
    if not session.get("userid") and session.get("tid"):
        uid = _userid_from_tid(str(session["tid"]))
        if uid:
            session["userid"] = uid
    return session


def parse_cashloot_link(raw: str) -> Tuple[str, str]:
    value = raw.strip()
    match = _CASHLOOT_URL_RE.search(value)
    if match:
        value = match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return "", ""
    parts = [part for part in value.split("-") if part]
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


def extract_cashloot_links(text: str) -> List[str]:
    seen, links = set(), []
    for token in _CASHLOOT_URL_RE.findall(text):
        key = token.lower()
        if key not in seen:
            seen.add(key)
            links.append(f"https://r.swiggy.com/cashloot/{token}")
    return links


def normalize_links(pairs: List[Tuple[str, str]]) -> List[Dict[str, str]]:
    resolved: List[Dict[str, str]] = []
    for label, url in pairs:
        campaign_id, referrer_id = parse_cashloot_link(url)
        resolved.append({"label": label, "url": url.strip(),
                         "campaign_id": campaign_id, "referrer_id": referrer_id})
    return resolved

# ─────────────────────────────────────────────────────────────────────────────
# Cash Loot API
# ─────────────────────────────────────────────────────────────────────────────

def build_headers(session: Dict) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "user-agent": SWIGGY_APP_HEADERS["user-agent"],
        "userAgent": SWIGGY_APP_HEADERS["user-agent"],
        "platform": "Swiggy-Android",
        "versionCode": SWIGGY_APP_HEADERS["version-code"],
        "tid": str(session.get("tid") or ""),
        "token": str(session.get("token") or ""),
        "userId": _account_user_id(session),
    }


def _money(value: float) -> str:
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.2f}"


def _amount(value: Any) -> float:
    if isinstance(value, dict):
        value = value.get("units", value.get("amount", 0))
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def _request(session: Dict, http: aiohttp.ClientSession, method: str, path: str,
                   body: Optional[Dict] = None) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {"headers": build_headers(session),
                              "timeout": aiohttp.ClientTimeout(total=20)}
    if body is not None:
        kwargs["json"] = body
    try:
        async with http.request(method, SPNS_BASE_URL + path, **kwargs) as resp:
            raw_text = await resp.text()
            try:
                data = json.loads(raw_text) if raw_text else {}
            except (TypeError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            status_code = data.get("statusCode")
            ok = 200 <= resp.status < 300 and status_code in (None, 0, "0")
            return {"ok": ok, "status": resp.status,
                    "msg": str(data.get("statusMessage") or ("OK" if ok else f"HTTP {resp.status}")),
                    "data": data.get("data") if isinstance(data.get("data"), dict) else {}}
    except asyncio.TimeoutError:
        return {"ok": False, "status": 0, "msg": "Request timed out", "data": {}}
    except Exception as exc:
        return {"ok": False, "status": 0, "msg": str(exc), "data": {}}


def _offer_outcome(data: Dict[str, Any]) -> Dict[str, Any]:
    outcome: Dict[str, Any] = {"joined": False, "error": "", "referrer_name": "", "detail": ""}
    raw_meta = data.get("uiMetadata")
    if isinstance(raw_meta, str) and raw_meta:
        try:
            outcome["referrer_name"] = str(json.loads(raw_meta).get("displayName", ""))
        except ValueError:
            pass
    lines: List[Tuple[str, str, float, str]] = []
    for entry in data.get("receiverResults") or []:
        if not isinstance(entry, dict):
            continue
        for bl, offer in (entry.get("offersByBL") or {}).items():
            if isinstance(offer, dict):
                lines.append((str(bl), str(offer.get("status") or "UNKNOWN"),
                              _amount(offer.get("offerValue")), str(offer.get("errorCode") or "")))
    if not lines:
        return outcome
    outcome["joined"] = any(status == "SUCCESS" for _, status, _, _ in lines)
    failures = [(bl, err) for bl, status, _, err in lines if status == "FAILURE" and err]
    limits = [bl for bl, status, _, _ in lines if status == "LIMIT_REACHED"]
    if failures:
        outcome["error"] = failures[0][1]
    elif limits and not outcome["joined"]:
        outcome["error"] = "limit reached (" + ", ".join(limits) + ")"
    outcome["detail"] = ", ".join(
        f"{bl} {status}" + (f" ({err})" if err else "") + (f" Rs.{_money(value)}" if value else "")
        for bl, status, value, err in lines)
    return outcome


def _looks_joined(message: str, error: str = "") -> bool:
    lowered = f"{message} {error}".lower()
    return any(word in lowered for word in ("already", "duplicate", "exist"))


async def join_referral(session: Dict, campaign_id: str, referrer_id: str, http: aiohttp.ClientSession) -> Dict[str, Any]:
    result = await _request(session, http, "POST", CASHLOOT_CREATE_OFFERS_PATH, body={
        "campaignId": campaign_id, "senderUserId": referrer_id,
        "isReferralFlow": True, "receivers": [{"userId": _account_user_id(session)}]})
    result.update(_offer_outcome(result["data"]))
    result["already_joined"] = _looks_joined(result["msg"], result["error"])
    return result


def _succeeded(result: Dict[str, Any]) -> bool:
    return bool(result["ok"]) and bool(result["joined"] or result["already_joined"])


async def join_with_retries(session: Dict, campaign_id: str, referrer_id: str,
                            http: aiohttp.ClientSession, retries: int = DEFAULT_RETRIES) -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": False, "joined": False, "already_joined": False,
                              "error": "", "detail": "", "referrer_name": "",
                              "msg": "", "attempts": 0}
    for attempt in range(1, max(1, retries) + 1):
        result = await join_referral(session, campaign_id, referrer_id, http)
        result["attempts"] = attempt
        if _succeeded(result):
            break
        if attempt < retries:
            await asyncio.sleep(RETRY_DELAY_SECONDS)
    return result


async def fetch_ledger(session: Dict, campaign_id: str, http: aiohttp.ClientSession) -> Dict[str, Any]:
    result = await _request(session, http, "GET",
                            CASHLOOT_TRANSACTIONS_PATH.format(campaign_id=campaign_id))
    data = result["data"]
    transactions = data.get("transactions")
    by_service = data.get("totalEarnedByBL")
    by_service = by_service if isinstance(by_service, dict) else {}
    earned = _amount(data.get("totalCashEarned")) or sum(_amount(v) for v in by_service.values())
    return {"ok": result["ok"], "msg": result["msg"], "earned": earned,
            "transactions": transactions if isinstance(transactions, list) else [],
            "by_service": by_service}


async def discover_users(session: Dict, location: Tuple[float, float], campaign_id: str,
                         http: aiohttp.ClientSession, seen: Optional[List[str]] = None) -> Dict[str, Any]:
    result = await _request(session, http, "POST", CASHLOOT_DISCOVER_PATH, body={
        "location": {"latitude": location[0], "longitude": location[1]},
        "tid": str(session.get("tid") or ""), "previousNearbyUserIds": seen or [],
        "campaignId": campaign_id, "userId": _account_user_id(session),
        "isFreshLocation": True})
    data = result["data"]
    users = data.get("nearbyUsers")
    stats = data.get("offerStatsByBL")
    return {"ok": result["ok"], "msg": result["msg"],
            "users": [u for u in (users or []) if isinstance(u, dict)],
            "stats": stats if isinstance(stats, dict) else {}}


async def invite_user(session: Dict, campaign_id: str, user: Dict[str, Any],
                      location: Tuple[float, float], http: aiohttp.ClientSession) -> Dict[str, Any]:
    result = await _request(session, http, "POST", CASHLOOT_CREATE_OFFERS_PATH, body={
        "campaignId": campaign_id, "senderUserId": _account_user_id(session),
        "senderLocation": {"latitude": location[0], "longitude": location[1]},
        "receivers": [{"userId": str(user.get("userId") or ""),
                       "userName": str(user.get("userName") or "")}]})
    result.update(_offer_outcome(result["data"]))
    result["already_joined"] = _looks_joined(result["msg"], result["error"])
    return result

# ─────────────────────────────────────────────────────────────────────────────
# OTP
# ─────────────────────────────────────────────────────────────────────────────

async def request_otp(phone: str) -> Dict:
    phone = re.sub(r"\D", "", str(phone or ""))
    if phone.startswith("91") and len(phone) == 12:
        phone = phone[2:]
    if len(phone) != 10:
        return {"ok": False, "msg": "Invalid phone — 10 digit Indian number required"}
    device_id = _random_device_id()
    headers = dict(SWIGGY_APP_HEADERS)
    headers["swuid"] = device_id
    headers["deviceid"] = device_id
    headers["deviceId"] = device_id
    headers["accessibility_enabled"] = "false"
    headers["screen-name"] = "LOGIN"
    headers["os-version"] = "13"
    headers["requestId"] = secrets.token_hex(8)
    # try primary endpoint then fallback
    endpoints = [
        (f"{PROFILE_BASE_URL}/api/v3/app/sms_otp", {"mobile": phone}),
        (f"{PROFILE_BASE_URL}/api/v1/user/sms-otp", {"mobile": phone, "countryCode": "91"}),
    ]
    last_msg = "OTP request failed"
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
                                     connector=aiohttp.TCPConnector(ssl=False)) as http:
        for url, params in endpoints:
            try:
                async with http.get(url, params=params, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=18)) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = {}
                    if resp.status == 200 and (data.get("statusCode") in (0, "0", None) or data.get("tid") or data.get("data")):
                        # normalize response shapes
                        inner = data.get("data") if isinstance(data.get("data"), dict) else data
                        tid = inner.get("tid") or data.get("tid")
                        sid = inner.get("sid") or data.get("sid")
                        device = str(inner.get("deviceId") or data.get("deviceId") or device_id)
                        if not sid and tid:
                            sid = _decode_tid_payload(str(tid)).get("sid")
                        if tid:
                            return {"ok": True, "tid": tid, "sid": sid, "deviceId": device, "phone": phone}
                    last_msg = data.get("statusMessage") or data.get("message") or f"HTTP {resp.status}"
            except Exception as e:
                last_msg = str(e)
                continue
    return {"ok": False, "msg": last_msg}


async def verify_otp(pending: Dict, otp: str) -> Dict:
    try:
        return await _verify_otp_inner(pending, otp)
    except Exception as e:
        log.exception("verify_otp failed")
        return {"ok": False, "msg": str(e)}


async def _verify_otp_inner(pending: Dict, otp: str) -> Dict:
    otp = re.sub(r"\D", "", str(otp or ""))
    headers = dict(SWIGGY_APP_HEADERS)
    headers["swuid"] = pending["deviceId"]
    headers["deviceid"] = pending["deviceId"]
    headers["deviceId"] = pending["deviceId"]
    headers["accessibility_enabled"] = "false"
    headers["screen-name"] = "LOGIN"
    headers["os-version"] = "13"
    headers["requestId"] = secrets.token_hex(8)
    if pending.get("tid"):
        headers["tid"] = str(pending["tid"])
    if pending.get("sid"):
        headers["sid"] = str(pending["sid"])
    bodies = [
        {"cloningSignalsData": {
            "appFilesDirPathInvalid": 0, "developerModeEnabled": 0,
            "deviceModelVmos": 0, "emulatorStatus": 0,
            "packageName": "in.swiggy.android",
            "workProfileEnabled": 0},
         "otp": otp},
        {"otp": otp, "otp_source": "Sms-manual"},
    ]
    urls = [
        (f"{PROFILE_BASE_URL}/api/v3/app/login/verify", {"otp_source": "Sms-manual"}),
        (f"{PROFILE_BASE_URL}/api/v1/user/login/otp-verify", {}),
    ]
    last_msg = "OTP verify failed"
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
                                     connector=aiohttp.TCPConnector(ssl=False)) as http:
        for url, params in urls:
            for body in bodies:
                try:
                    async with http.post(url, params=params or None, json=body,
                                         headers=headers, timeout=aiohttp.ClientTimeout(total=18)) as resp:
                        try:
                            data = await resp.json(content_type=None)
                        except Exception:
                            data = {}
                        status_ok = resp.status == 200 and data.get("statusCode") in (0, "0", None)
                        # sometimes statusCode missing but token present
                        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
                        has_token = bool(
                            (inner.get("token") or data.get("token") or inner.get("tid") or data.get("tid"))
                        )
                        if not (status_ok or (resp.status == 200 and has_token)):
                            last_msg = data.get("statusMessage") or data.get("message") or f"HTTP {resp.status}"
                            continue
                        session: Dict = {}
                        for key in ("token", "tid", "sid"):
                            if inner.get(key):
                                session[key] = inner[key]
                            elif data.get(key):
                                session[key] = data[key]
                        # cookies fallback
                        try:
                            for c in http.cookie_jar:
                                n = (c.key or c.name or "").lower()
                                if n in ("token", "tid", "sid") and n not in session:
                                    session[n] = c.value
                        except Exception:
                            pass
                        session["deviceId"] = str(
                            data.get("deviceId") or inner.get("deviceId") or pending["deviceId"]
                        )
                        user_id = _userid_from_tid(str(session.get("tid") or ""))
                        if not user_id or user_id == "0":
                            user_id = str(
                                inner.get("userId") or inner.get("user_id")
                                or data.get("userId") or data.get("user_id") or ""
                            )
                        if not user_id or user_id == "0":
                            last_msg = "No valid user ID returned"
                            continue
                        session["userid"] = user_id
                        session["customerId"] = str(
                            inner.get("customer_id") or inner.get("customerId") or user_id
                        )
                        mobile = str(inner.get("mobile") or pending.get("phone") or "")
                        session["mobile"] = mobile
                        session["phone"] = mobile
                        if not session.get("token") and not session.get("tid"):
                            last_msg = "Login ok but token/tid missing"
                            continue
                        return {"ok": True, "session": session}
                except Exception as e:
                    last_msg = str(e)
                    continue
    return {"ok": False, "msg": last_msg}

# ─────────────────────────────────────────────────────────────────────────────
# Run full join
# ─────────────────────────────────────────────────────────────────────────────

async def run_full_join(session: Dict, links: List[Dict[str, str]],
                        progress_callback=None, history_callback=None) -> str:
    lines = ["⚡ SWIGGY CASH LOOT  —  JOIN & LOOT",
             f"{datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}",
             f"Account   : {_account_user_id(session) or '--'}",
             f"Links     : {len(links)}",
             f"Campaigns : {', '.join(sorted({l['campaign_id'] or '?' for l in links}))}", ""]
    for link in links:
        if not link["campaign_id"] or not link["referrer_id"]:
            lines.append(f"❌ {link['label']}: invalid link")
            return "\n".join(lines)
        lines.append(f"-- {link['label']}  campaign {link['campaign_id']}  referrer {link['referrer_id']}")

    campaigns = sorted({link["campaign_id"] for link in links})
    results: List[Dict[str, Any]] = []

    jar = aiohttp.CookieJar(unsafe=True)
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(cookie_jar=jar, connector=connector) as http:
        lines.append("")
        lines.append("──── LOOT STATUS BEFORE ────")
        before: Dict[str, Dict[str, Any]] = {}
        for campaign_id in campaigns:
            before[campaign_id] = await fetch_ledger(session, campaign_id, http)
            entry = before[campaign_id]
            if entry["ok"]:
                lines.append(f"OK  {campaign_id}: Rs.{_money(entry['earned'])} looted, {len(entry['transactions'])} transaction(s)")
            else:
                lines.append(f"FAIL  {campaign_id}: {entry['msg']}")

        lines.append("")
        lines.append("──── JOINING LINKS ────")
        for idx, link in enumerate(links, 1):
            if progress_callback:
                ordinal = {1: "1st", 2: "2nd", 3: "3rd"}.get(idx, f"{idx}th")
                await progress_callback(
                    f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n"
                    f"⏳ *{ordinal} CLAIM*\n🔗 {link['label']}\n[{idx}/{len(links)}] processing...")
            result = await join_with_retries(session, link["campaign_id"], link["referrer_id"], http, DEFAULT_RETRIES)
            joined = _succeeded(result)
            name = f" ({result['referrer_name']})" if result.get("referrer_name") else ""
            tries = "" if result.get("attempts", 1) <= 1 else f" [{result['attempts']} tries]"
            results.append({**link, "joined": joined, "error": result.get("error", ""),
                            "detail": result.get("detail", ""),
                            "already_joined": result.get("already_joined", False),
                            "attempts": result.get("attempts", 1)})
            if joined and result.get("already_joined"):
                lines.append("✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦")
                lines.append(f"✅ ALREADY  {link['label']}{name}")
                lines.append(f"   {result.get('detail') or result.get('error') or 'exists'}{tries}")
                lines.append("✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦")
            elif joined:
                lines.append("✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦")
                lines.append(f"✅ COMPLETE  {link['label']}{name}")
                lines.append(f"   {result.get('detail') or 'Joined'}{tries}")
                lines.append("✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦")
            else:
                lines.append("✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦")
                lines.append(f"❌ FAIL  {link['label']}{name}")
                lines.append(f"   {result.get('error') or result.get('detail') or result.get('msg') or 'unknown'}{tries}")
                lines.append("✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦")
            if progress_callback:
                try:
                    tag = "✅ ALREADY" if (joined and result.get("already_joined")) else ("✅ COMPLETE" if joined else "❌ FAIL")
                    await progress_callback(f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n{tag}\n🔗 {link['label']}\n[{idx}/{len(links)}]")
                except Exception:
                    pass
            if history_callback:
                try:
                    await history_callback(link, joined,
                                           result.get("error") or result.get("detail") or result.get("msg") or "")
                except Exception:
                    log.exception("join history callback failed")
            if idx < len(links):
                await asyncio.sleep(LINK_DELAY_SECONDS)

        if any(r["joined"] for r in results):
            lines.append("")
            lines.append(f"-- Waiting {CASHLOOT_LEDGER_WAIT_SECONDS:g}s for the joins to settle...")
            await asyncio.sleep(CASHLOOT_LEDGER_WAIT_SECONDS)

        lines.append("")
        lines.append("──── LOOT STATUS AFTER ────")
        after: Dict[str, Dict[str, Any]] = {}
        for campaign_id in campaigns:
            after[campaign_id] = await fetch_ledger(session, campaign_id, http)
            entry, start = after[campaign_id], before.get(campaign_id, {})
            if not entry["ok"]:
                lines.append(f"FAIL  {campaign_id}: {entry['msg']}")
                continue
            gained = entry["earned"] - start.get("earned", 0) if start.get("ok") else entry["earned"]
            lines.append(f"OK  {campaign_id}: Rs.{_money(entry['earned'])} looted total "
                         f"(+Rs.{_money(gained)} this run) | {len(entry['transactions'])} transaction(s)")
            for txn in entry["transactions"][-5:]:
                if isinstance(txn, dict):
                    who = txn.get("associatedUserName") or txn.get("userName") or "--"
                    value = 0.0; bl = ""; expires = 0
                    for b, offer in (txn.get("offersByBL") or {}).items():
                        if isinstance(offer, dict) and _amount(offer.get("offerValue")) > value:
                            bl = str(b); value = _amount(offer.get("offerValue"))
                            expires = int(offer.get("offerEndTime") or 0)
                    until = datetime.fromtimestamp(expires).strftime("%d %b") if expires else "--"
                    lines.append(f"  -- {who:<16} Rs.{_money(value):<4} {bl:<10} till {until}")

    joined_count = sum(1 for r in results if r["joined"])
    failed_count = len(results) - joined_count
    looted = sum(entry["earned"] for entry in after.values() if entry.get("ok"))
    attempts = sum(r.get("attempts", 1) for r in results)

    lines.append("")
    lines.append("✦•┈┈┈•✦  SUMMARY  ✦•┈┈┈•✦")
    lines.append(f"Links        : {len(results)}")
    lines.append(f"✅ Complete  : {joined_count}")
    lines.append(f"❌ Fail      : {failed_count}")
    lines.append(f"Attempts     : {attempts}")
    lines.append(f"💰 Cash      : Rs.{_money(looted)}")
    for r in results:
        mark = "✓" if r["joined"] else "✗"
        note = r["referrer_id"] if r["joined"] else (r.get("error") or "--")
        lines.append(f"  {mark} {r['label'][:18]:<18} {note[:36]}")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────────────────────
# BYO Notify (Late Night / Breakfast)
# ─────────────────────────────────────────────────────────────────────────────

async def notify_get_status(session: Dict, prefix: str, day: int) -> Dict[str, Any]:
    campaign = f"{prefix}{day}"
    headers = {"Content-Type": "application/json",
               "token": str(session.get("token") or ""),
               "tid": str(session.get("tid") or "")}
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            async with http.get(f"{NOTIFY_URL}?campaign={campaign}", headers=headers,
                                timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json(content_type=None)
                return {"ok": resp.status == 200, "status": resp.status, "data": data}
    except Exception as e:
        return {"ok": False, "status": 0, "data": {}, "msg": str(e)}


async def notify_claim_offer(session: Dict, prefix: str, day: int, offer_id: int,
                             page_url: str = "") -> Dict[str, Any]:
    """Claim BYO (Late Night / Breakfast) offer.
    Real flow: optional coupon create + notify POST/PATCH so Free Cash / coupon
    actually applies on the account (not just status flag).
    """
    campaign = f"{prefix}{day}"
    token = str(session.get("token") or "")
    tid = str(session.get("tid") or "")
    referer = page_url or (
        LN_PAGE_URL if prefix.startswith("byo-ln") else BF_PAGE_URL
    )
    headers_base = {
        "Content-Type": "application/json",
        "token": token,
        "tid": tid,
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Accept-Language": "en-IN,en;q=0.9",
        "Origin": "https://events.swiggy.com",
        "Referer": referer,
    }
    steps: Dict[str, Any] = {"campaign": campaign, "offer_id": offer_id}
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            # Step 1: try coupon / free-cash create (same family as PYO)
            coupon_ok = False
            coupon_data: Any = {}
            for ep in (
                "https://events.swiggy.com/api/pick-your-offer-coupon",
                "https://events.swiggy.com/api/byo-coupon",
            ):
                coupon_headers = dict(headers_base)
                coupon_headers["offerindex"] = str(offer_id)
                coupon_headers["offerIndex"] = str(offer_id)
                try:
                    async with http.post(
                        ep,
                        headers=coupon_headers,
                        json={"campaign": campaign, "offerId": offer_id, "value": offer_id},
                        timeout=aiohttp.ClientTimeout(total=18),
                    ) as resp:
                        try:
                            coupon_data = await resp.json(content_type=None)
                        except Exception:
                            coupon_data = {"raw": (await resp.text())[:400]}
                        steps.setdefault("coupon_tries", []).append(
                            {"url": ep, "status": resp.status, "data": coupon_data}
                        )
                        if resp.status in (200, 201) and not (
                            isinstance(coupon_data, dict) and coupon_data.get("error")
                        ):
                            coupon_ok = True
                            steps["coupon_endpoint"] = ep
                            steps["coupon"] = coupon_data
                            break
                except Exception as e:
                    steps.setdefault("coupon_tries", []).append({"url": ep, "error": str(e)})

            end_date = None
            if isinstance(coupon_data, dict):
                inner = coupon_data.get("data") if isinstance(coupon_data.get("data"), dict) else coupon_data
                if isinstance(inner, dict):
                    end_date = inner.get("end_date") or inner.get("endDate") or inner.get("offerEndTime")
                    steps["coupon_code"] = (
                        inner.get("coupon_code") or inner.get("couponCode") or inner.get("code")
                    )

            note = json.dumps({"offerEndTime": end_date}) if end_date is not None else None

            # Step 2: status → POST or PATCH notify (locks selection + applies)
            already_claimed = False
            async with http.get(
                f"{NOTIFY_URL}?campaign={campaign}",
                headers=headers_base,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                try:
                    st_data = await resp.json(content_type=None)
                except Exception:
                    st_data = {}
                steps["status_before"] = st_data
                if isinstance(st_data, dict) and st_data.get("value") not in (None, "", 0, "0"):
                    already_claimed = True
                    steps["already"] = st_data.get("value")

            if already_claimed:
                return {
                    "ok": True,
                    "already": True,
                    "status": 200,
                    "data": steps.get("status_before") or {},
                    "campaign": campaign,
                    "steps": steps,
                    "msg": "already claimed",
                    "coupon_code": steps.get("coupon_code"),
                }

            notify_body: Dict[str, Any] = {"campaign": campaign, "value": offer_id}
            if note:
                notify_body["note"] = note
            method = "POST"
            async with http.request(
                method,
                NOTIFY_URL,
                headers=headers_base,
                json=notify_body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                try:
                    notify_data = await resp.json(content_type=None)
                except Exception:
                    notify_data = {"raw": (await resp.text())[:300]}
                steps["notify_method"] = method
                steps["notify_status"] = resp.status
                steps["notify"] = notify_data
                notify_ok = resp.status == 200 and not (
                    isinstance(notify_data, dict) and notify_data.get("error")
                )

            # Verify
            async with http.get(
                f"{NOTIFY_URL}?campaign={campaign}",
                headers=headers_base,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                try:
                    st_after = await resp.json(content_type=None)
                except Exception:
                    st_after = {}
                steps["status_after"] = st_after

            verified = False
            if isinstance(st_after, dict):
                val = st_after.get("value")
                try:
                    verified = int(val) == int(offer_id)
                except Exception:
                    verified = str(val) == str(offer_id) and val not in (None, "", 0, "0")

            ok = bool(notify_ok or verified or coupon_ok)
            return {
                "ok": ok,
                "already": False,
                "status": steps.get("notify_status") or steps.get("coupon_status") or 0,
                "data": notify_data if isinstance(notify_data, dict) else {},
                "notify_ok": notify_ok,
                "verified": verified,
                "campaign": campaign,
                "steps": steps,
                "coupon_code": steps.get("coupon_code"),
            }
    except Exception as e:
        log.exception("notify_claim_offer failed")
        return {"ok": False, "status": 0, "data": {}, "msg": str(e), "campaign": campaign, "steps": steps}


async def ln_get_status(session: Dict, day: int) -> Dict[str, Any]:
    return await notify_get_status(session, LN_CAMPAIGN_PREFIX, day)


async def ln_claim_offer(session: Dict, day: int, offer_id: int) -> Dict[str, Any]:
    return await notify_claim_offer(session, LN_CAMPAIGN_PREFIX, day, offer_id, page_url=LN_PAGE_URL)


async def bf_get_status(session: Dict, day: int) -> Dict[str, Any]:
    return await notify_get_status(session, BF_CAMPAIGN_PREFIX, day)


async def bf_claim_offer(session: Dict, day: int, offer_id: int) -> Dict[str, Any]:
    return await notify_claim_offer(session, BF_CAMPAIGN_PREFIX, day, offer_id, page_url=BF_PAGE_URL)

# ─────────────────────────────────────────────────────────────────────────────
# PawParazzi (₹200 OFF Coupon)
# ─────────────────────────────────────────────────────────────────────────────
PAW_CAMPAIGN = "pawparazzi"
PAW_TEM_ID = "StHE-vpu94n"
PAW_TEM_ID_ALT = "3r5R-Lvkt1m"
PAW_DEFAULT_LAT, PAW_DEFAULT_LNG = 28.6139, 77.2090
PAW_MIN_CART = 499
PAW_DISCOUNT = 200


async def paw_check_played(session: Dict) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json",
               "token": str(session.get("token") or ""),
               "tid": str(session.get("tid") or "")}
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            async with http.get(f"{NOTIFY_URL}?campaign={PAW_CAMPAIGN}", headers=headers,
                                timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json(content_type=None)
                return {"ok": resp.status == 200, "status": resp.status, "data": data or {}}
    except Exception as e:
        return {"ok": False, "data": {}, "msg": str(e)}


def _paw_pet_image_candidates() -> list:
    import base64 as _b64
    import urllib.request
    urls = [
        "https://images.unsplash.com/photo-1543466835-00a7907e9de1?w=640&h=640&fit=crop",
        "https://images.unsplash.com/photo-1587300003388-59208cc962f0?w=640&h=640&fit=crop",
        "https://images.unsplash.com/photo-1514888286974-6c03e2ca1dba?w=640&h=640&fit=crop",
        "https://images.unsplash.com/photo-1574158622682-e40e69881006?w=640&h=640&fit=crop",
        "https://images.unsplash.com/photo-1552053831-71594a27632d?w=640&h=640&fit=crop",
    ]
    out = []
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                data = r.read()
            if data[:3] == b"\xff\xd8\xff" and len(data) > 4000:
                out.append(_b64.b64encode(data).decode("ascii"))
        except Exception:
            continue
    if not out:
        try:
            out.append(_paw_auto_pet_b64())
        except Exception:
            pass
    return out


def _paw_auto_pet_b64() -> str:
    cands = _paw_pet_image_candidates()
    if cands:
        return cands[0]
    import base64 as _b64
    return _b64.b64encode(bytes([0xFF, 0xD8, 0xFF, 0xD9])).decode("ascii")


def _paw_extract_min_cart(redeem) -> int:
    if not isinstance(redeem, dict):
        return PAW_MIN_CART
    candidates = []
    stack = [redeem]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                lk = str(k).lower()
                if any(x in lk for x in ("min", "mov", "cart", "order", "bill")):
                    try:
                        num = int(float(str(v).replace(",", "").replace("₹", "").strip()))
                        if 50 <= num <= 5000:
                            candidates.append(num)
                    except Exception:
                        pass
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return min(candidates) if candidates else PAW_MIN_CART


def _paw_coupon_card(min_cart: int, code: str = "", userid: str = "", tem: str = "", extra: str = "") -> str:
    code_line = f"🎫 Code      : `{code}`\n" if code else ""
    user_line = f"👤 Account   : `{userid}`\n" if userid else ""
    tem_line = f"🔑 Template  : `{tem}`\n" if tem else ""
    if min_cart and min_cart > 0:
        mov_line = f"🛒 Min Cart  : *₹{min_cart}*\n"
        use_line = f"• Cart value ≥ *₹{min_cart}* rakho\n"
    else:
        mov_line = "🛒 Min Cart  : *App mein check karo* (API ne value nahi di)\n"
        use_line = "• Swiggy → Coupons mein exact MOV dekho\n"
    return ("✦•┈┈┈┈┈┈┈┈┈┈┈•✦\n"
            "🎁 *PAWPARAZZI COUPON*\n"
            "✦•┈┈┈┈┈┈┈┈┈┈┈•✦\n\n"
            f"💸 Discount  : *₹{PAW_DISCOUNT} OFF*\n{mov_line}{code_line}{user_line}{tem_line}{extra}\n"
            "📌 *Kaise use karein:*\n"
            f"{use_line}"
            "• Swiggy → Coupons / Offers\n"
            f"• *₹{PAW_DISCOUNT} OFF* apply karke order karo\n\n"
            "✦•┈┈┈┈┈┈┈┈┈┈┈•✦")


async def paw_claim_coupon(session: Dict, user_images_b64: Optional[List[str]] = None) -> Dict[str, Any]:
    token = str(session.get("token") or "")
    tid = str(session.get("tid") or "")
    userid = _account_user_id(session) or str(session.get("customerId") or "")
    headers = {"Content-Type": "application/json", "token": token, "tid": tid}
    steps: Dict[str, Any] = {}
    try:
        is_india1 = False
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http0:
                async with http0.get("https://events.swiggy.com/pawparazzi",
                                     headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                                                           "Chrome/120.0.0.0 Mobile Safari/537.36",
                                              "Accept-Language": "en-IN,en;q=0.9"},
                                     timeout=aiohttp.ClientTimeout(total=12)) as rp:
                    html = await rp.text()
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
            if m:
                nd = json.loads(m.group(1))
                is_india1 = bool(nd.get("props", {}).get("pageProps", {}).get("isIndia1City", False))
        except Exception as e:
            log.warning("isIndia1City detect failed: %s", e)
        if session.get("paw_force_india1") is True:
            is_india1 = True
        if session.get("paw_force_india1") is False:
            is_india1 = False
        tem_id = PAW_TEM_ID if is_india1 else PAW_TEM_ID_ALT
        steps["isIndia1City"] = is_india1
        steps["tem_id"] = tem_id
        lat = float(session.get("lat") or (28.6139 if is_india1 else 19.0760))
        lng = float(session.get("lng") or (77.2090 if is_india1 else 72.8777))

        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            candidates: List[str] = []
            if user_images_b64:
                candidates.extend([x for x in user_images_b64 if x])
            if not candidates:
                candidates = _paw_pet_image_candidates()
            if not candidates:
                candidates = [_paw_auto_pet_b64()]

            result_block: Dict[str, Any] = {}
            score_data: Dict[str, Any] = {}
            last_fail = ""
            accepted = False

            for img_i, b64 in enumerate(candidates[:5]):
                if b64.startswith("data:"):
                    b64 = b64.split(",", 1)[-1]
                steps[f"try_image_{img_i}"] = len(b64)
                async with http.post("https://events.swiggy.com/api/upload-image",
                                     headers=headers,
                                     json={"campaign": PAW_CAMPAIGN, "base64Image": b64},
                                     timeout=aiohttp.ClientTimeout(total=40)) as resp:
                    try:
                        up = await resp.json(content_type=None)
                    except Exception:
                        up = {}
                    if resp.status not in (200, 201):
                        last_fail = f"upload HTTP {resp.status}"
                        continue
                file_path = ""
                if isinstance(up, dict):
                    file_path = str(up.get("url") or up.get("file_path")
                                    or (up.get("data") or {}).get("url") or "")
                for prefix in ("https://ik.imagekit.io/marketing", "http://ik.imagekit.io/marketing"):
                    if file_path.startswith(prefix):
                        file_path = file_path[len(prefix):]
                if not file_path:
                    last_fail = "no file_path"
                    continue
                async with http.post("https://events.swiggy.com/api/pawparazzi-submit",
                                     headers=headers, json={"file_path": file_path},
                                     timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    try:
                        sub = await resp.json(content_type=None)
                    except Exception:
                        sub = {}
                    if resp.status not in (200, 201):
                        last_fail = f"submit HTTP {resp.status}"
                        continue
                jobid = ""
                if isinstance(sub, dict):
                    inner = sub.get("data") if isinstance(sub.get("data"), dict) else {}
                    jobid = str((inner or {}).get("jobid") or sub.get("jobid") or "")
                if not jobid:
                    last_fail = "no jobid"
                    continue
                for _ in range(18):
                    await asyncio.sleep(2.0)
                    async with http.post("https://events.swiggy.com/api/pawparazzi-check",
                                         headers=headers, json={"jobid": jobid},
                                         timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        try:
                            chk = await resp.json(content_type=None)
                        except Exception:
                            chk = {}
                        score_data = chk if isinstance(chk, dict) else {}
                        data = score_data.get("data") if isinstance(score_data.get("data"), dict) else {}
                        jobstatus = str(data.get("jobstatus") or "").upper()
                        if jobstatus == "COMPLETED":
                            result_block = data.get("result") if isinstance(data.get("result"), dict) else {}
                            break
                        if jobstatus == "FAILED":
                            last_fail = str(data.get("subtitle") or "job failed")
                            result_block = {}
                            break
                else:
                    last_fail = "poll timeout"
                    continue
                is_valid = str(result_block.get("is_valid_pet_image") or "").lower()
                animal = str(result_block.get("detected_animal") or "").lower()
                steps["is_valid_pet_image"] = is_valid
                steps["detected_animal"] = animal
                steps["confidence"] = result_block.get("confidence")
                steps["score"] = score_data
                if is_valid in ("yes", "true", "1"):
                    accepted = True
                    steps["image_index_accepted"] = img_i
                    break
                last_fail = f"is_valid_pet_image={is_valid}, animal={animal}"

            if not accepted:
                return {"ok": False,
                        "msg": ("Pet image Swiggy ne valid nahi maani. "
                                f"Last: {last_fail}. Naya unique real pet photo chahiye."),
                        "steps": steps}

            async with http.post(NOTIFY_URL, headers=headers,
                                 json={"campaign": PAW_CAMPAIGN,
                                       "note": json.dumps({"isIndia1City": is_india1})},
                                 timeout=aiohttp.ClientTimeout(total=15)) as resp:
                try:
                    notify_data = await resp.json(content_type=None)
                except Exception:
                    notify_data = {}
                notify_ok = resp.status == 200
                steps["notify"] = notify_data

            redeem_headers = {"campaignid": tem_id, "tid": tid, "userid": userid,
                              "lat": str(lat), "lng": str(lng)}
            async with http.post("https://events.swiggy.com/api/benefits-redeem",
                                 headers=redeem_headers,
                                 timeout=aiohttp.ClientTimeout(total=15)) as resp2:
                try:
                    redeem_data = await resp2.json(content_type=None)
                except Exception:
                    redeem_data = {"raw": (await resp2.text())[:400]}
                redeem_ok = resp2.status == 200 and not (isinstance(redeem_data, dict) and redeem_data.get("error"))
                steps["redeem"] = redeem_data
                steps["redeem_status"] = resp2.status
                steps["redeem_tem"] = tem_id

            min_from_api = _paw_extract_min_cart(redeem_data if isinstance(redeem_data, dict) else {})
            min_cart_actual = None
            if isinstance(redeem_data, dict):
                for key in ("minOrderValue", "min_order_value", "mov", "minCart", "minimumOrderValue"):
                    def _find(o, k):
                        if isinstance(o, dict):
                            if k in o:
                                return o[k]
                            for v in o.values():
                                r = _find(v, k)
                                if r is not None:
                                    return r
                        return None
                    val = _find(redeem_data, key)
                    if val is not None:
                        try:
                            min_cart_actual = int(float(str(val).replace(",", "").replace("₹", "")))
                            break
                        except Exception:
                            pass
            ok = bool(notify_ok or redeem_ok)
            return {"ok": ok, "notify_ok": notify_ok, "redeem_ok": redeem_ok,
                    "notify": notify_data, "redeem": redeem_data if isinstance(redeem_data, dict) else {},
                    "userid": userid, "steps": steps,
                    "min_cart_actual": min_cart_actual,
                    "min_cart_hint": min_from_api if min_cart_actual is None else min_cart_actual,
                    "tem_id": tem_id, "isIndia1City": is_india1}
    except Exception as e:
        log.exception("paw_claim_coupon failed")
        return {"ok": False, "msg": str(e), "steps": steps}

# ═════════════════════════════════════════════════════════════════════════════
# SHRESTHO PUJO — auto vote + auto invite claim
# ═════════════════════════════════════════════════════════════════════════════
PUJO_BENEFITS_URL = "https://events.swiggy.com/api/benefits-redeem"
PUJO_NOTIFY_URL = NOTIFY_URL
PUJO_VOTE_API = "https://events.swiggy.com/api/benefits-redeem"


def _pujo_headers(session: Dict, lat: float, lng: float) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "token": str(session.get("token") or ""),
        "tid": str(session.get("tid") or ""),
        "userid": _account_user_id(session) or str(session.get("customerId") or ""),
        "lat": str(lat),
        "lng": str(lng),
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Accept-Language": "en-IN,en;q=0.9",
    }


async def _pujo_fetch_pandals() -> List[Dict[str, Any]]:
    pandals: List[Dict[str, Any]] = []
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            async with http.get(PUJO_PAGE_URL,
                                headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) "
                                                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                       "Chrome/120.0.0.0 Mobile Safari/537.36",
                                         "Accept-Language": "en-IN,en;q=0.9"},
                                timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text()
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
        if not m:
            return pandals
        nd = json.loads(m.group(1))
        pp = nd.get("props", {}).get("pageProps", {})

        def walk(o):
            if isinstance(o, dict):
                if any(k.lower() in {kk.lower() for kk in o.keys()}
                       for k in ["pandalId", "pandal_id", "id"]) and \
                   any(k.lower() in {kk.lower() for kk in o.keys()}
                       for k in ["pandalName", "name", "title"]):
                    pid = o.get("pandalId") or o.get("pandal_id") or o.get("id")
                    name = o.get("pandalName") or o.get("name") or o.get("title")
                    if pid and name:
                        pandals.append({"id": str(pid), "name": str(name)})
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(pp)
        walk(pp.get("nexusData") or {})
    except Exception as e:
        log.warning("pujo pandal fetch failed: %s", e)

    seen = set()
    out = []
    for p in pandals:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        out.append(p)
    return out


async def _pujo_get_status(session: Dict) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json",
               "token": str(session.get("token") or ""),
               "tid": str(session.get("tid") or "")}
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            async with http.get(f"{NOTIFY_URL}?campaign={PUJO_CAMPAIGN}", headers=headers,
                                timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json(content_type=None)
                return {"ok": resp.status == 200, "status": resp.status, "data": data or {}}
    except Exception as e:
        return {"ok": False, "data": {}, "msg": str(e)}


async def _pujo_vote_once(session: Dict, pandal_id: str, lat: float, lng: float) -> Dict[str, Any]:
    headers = _pujo_headers(session, lat, lng)
    attempts = [
        ("POST", PUJO_BENEFITS_URL,
         {"campaignid": PUJO_CAMPAIGN},
         {"campaign": PUJO_CAMPAIGN, "pandalId": pandal_id, "action": "vote"}),
        ("POST", PUJO_NOTIFY_URL,
         None,
         {"campaign": PUJO_CAMPAIGN, "value": 1, "pandalId": pandal_id}),
        ("POST", "https://events.swiggy.com/api/pujo-vote",
         None,
         {"campaign": PUJO_CAMPAIGN, "pandalId": pandal_id}),
    ]
    last_err = ""
    for method, url, hdr_extra, body in attempts:
        h = dict(headers)
        if hdr_extra:
            h.update(hdr_extra)
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
                async with http.request(method, url, headers=h, json=body,
                                        timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = {"raw": (await resp.text())[:300]}
                    if resp.status == 200 and not (isinstance(data, dict) and data.get("error")):
                        return {"ok": True, "status": resp.status, "data": data, "url": url}
                    last_err = f"{url} -> HTTP {resp.status} {str(data)[:120]}"
        except Exception as e:
            last_err = f"{url} -> {e}"
            continue
    return {"ok": False, "msg": last_err or "unknown", "data": {}}


async def _pujo_invite_once(session: Dict, lat: float, lng: float) -> Dict[str, Any]:
    headers = _pujo_headers(session, lat, lng)
    body = {"campaign": PUJO_CAMPAIGN, "action": "invite"}
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            async with http.post(PUJO_BENEFITS_URL,
                                 headers={**headers, "campaignid": PUJO_CAMPAIGN},
                                 json=body, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {"raw": (await resp.text())[:300]}
                return {"ok": resp.status == 200 and not (isinstance(data, dict) and data.get("error")),
                        "status": resp.status, "data": data}
    except Exception as e:
        return {"ok": False, "msg": str(e), "data": {}}


async def _pujo_notify_played(session: Dict) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json",
               "token": str(session.get("token") or ""),
               "tid": str(session.get("tid") or "")}
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as http:
            async with http.post(PUJO_NOTIFY_URL, headers=headers,
                                 json={"campaign": PUJO_CAMPAIGN},
                                 timeout=aiohttp.ClientTimeout(total=15)) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {}
                return {"ok": resp.status == 200, "data": data}
    except Exception as e:
        return {"ok": False, "msg": str(e), "data": {}}


async def pujo_full_claim(session: Dict, lat: float = PUJO_DEFAULT_LAT,
                          lng: float = PUJO_DEFAULT_LNG,
                          progress_cb=None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "vote_ok": 0, "vote_fail": 0, "vote_details": [],
        "invite_ok": False, "invite_data": None,
        "notify_ok": False, "notify_data": None,
        "pandals_found": 0, "session_ok": True, "errors": [],
    }
    userid = _account_user_id(session)
    if not userid:
        result["session_ok"] = False
        result["errors"].append("no userid in session")
        return result

    if progress_cb:
        try:
            await progress_cb("🔍 Fetching pandal list...")
        except Exception:
            pass
    pandals = await _pujo_fetch_pandals()
    result["pandals_found"] = len(pandals)
    if not pandals:
        pandals = [{"id": str(i), "name": f"pandal_{i}"} for i in range(1, 6)]

    max_votes = PUJO_VOTE_MAX // PUJO_VOTE_AMOUNT
    voted = 0
    for idx, p in enumerate(pandals[:max_votes], 1):
        if progress_cb:
            try:
                await progress_cb(f"🗳️ Vote {idx}/{max_votes}: {p['name'][:30]}")
            except Exception:
                pass
        r = await _pujo_vote_once(session, p["id"], lat, lng)
        if r.get("ok"):
            voted += 1
            result["vote_ok"] = voted
            result["vote_details"].append({"pandal": p["name"], "ok": True})
        else:
            result["vote_fail"] += 1
            result["vote_details"].append({"pandal": p["name"], "ok": False,
                                           "err": str(r.get("msg") or r.get("data"))[:120]})
        await asyncio.sleep(1.2)

    if progress_cb:
        try:
            await progress_cb("📨 Claiming invite bonus...")
        except Exception:
            pass
    inv = await _pujo_invite_once(session, lat, lng)
    result["invite_ok"] = bool(inv.get("ok"))
    result["invite_data"] = inv.get("data")
    if not inv.get("ok"):
        result["errors"].append(f"invite: {inv.get('msg') or inv.get('data')}")

    if progress_cb:
        try:
            await progress_cb("🔔 Notifying campaign server...")
        except Exception:
            pass
    n = await _pujo_notify_played(session)
    result["notify_ok"] = bool(n.get("ok"))
    result["notify_data"] = n.get("data")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Force Join
# ─────────────────────────────────────────────────────────────────────────────

async def _force_join_missing(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> List[Tuple[str, Optional[str]]]:
    if not BOT_SETTINGS.get("force_join_enabled"):
        return []
    channels = BOT_SETTINGS.get("force_channels") or []
    if not channels or is_admin(user_id):
        return []
    missing: List[Tuple[str, Optional[str]]] = []
    for raw_channel in channels:
        ch = str(raw_channel).strip()
        if not ch:
            continue
        try:
            chat = await context.bot.get_chat(ch)
            chat_id = chat.id
            username = getattr(chat, "username", None)
        except Exception as exc:
            log.warning("Force-join get_chat failed for %s: %s", ch, exc)
            chat_id = ch
            username = ch[1:] if ch.startswith("@") else None
        joined = False
        for attempt in range(3):
            try:
                member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                status = getattr(member, "status", "")
                joined = status in {"member", "administrator", "creator"}
                if status == "restricted" and bool(getattr(member, "is_member", False)):
                    joined = True
                if status in {"left", "kicked"}:
                    joined = False
                if joined:
                    break
            except Exception as exc:
                log.warning("Force-join member check failed for %s (attempt %s): %s", ch, attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(0.5)
        if not joined:
            if username:
                join_url = f"https://t.me/{str(username).lstrip('@')}"
            elif ch.startswith("https://t.me/"):
                join_url = ch
            else:
                join_url = None
            missing.append((ch, join_url))
    return missing


async def _send_force_join_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    missing = await _force_join_missing(update.effective_user.id, context)
    buttons: List[List[InlineKeyboardButton]] = []
    for ch, join_url in missing:
        if join_url:
            buttons.append([InlineKeyboardButton(f"📢 Join {ch}", url=join_url)])
    buttons.append([InlineKeyboardButton("✅ I've Joined — Verify", callback_data="fj_check")])
    markup = InlineKeyboardMarkup(buttons)
    text = ("⚠️ *Force Join required*\n\n"
            "Pehle neeche diye gaye channel/group join karo.\n"
            "Join karne ke baad *I've Joined — Verify* dabao.\n\n"
            "🔒 Verification ke bina bot access locked rahega.")
    target = update.effective_message
    if not target:
        return
    try:
        await target.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    except Exception:
        try:
            await target.reply_text(text, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            log.exception("Unable to send force-join prompt")


async def check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    await record_user_activity(update)
    if not BOT_SETTINGS.get("force_join_enabled") or not BOT_SETTINGS.get("force_channels"):
        return True
    missing = await _force_join_missing(user.id, context)
    if not missing:
        await record_user_activity(update, verified=True)
        return True
    await _send_force_join_prompt(update, context)
    return False


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ─────────────────────────────────────────────────────────────────────────────
# Telegram Handlers
# ─────────────────────────────────────────────────────────────────────────────

def get_session(chat_id: int) -> Optional[Dict]:
    if chat_id in user_sessions:
        saved_at = user_session_times.get(chat_id, 0)
        age = datetime.now(timezone.utc).timestamp() - saved_at
        if age > SESSION_TTL_SECONDS:
            clear_user_session(chat_id)
            return None
        return user_sessions[chat_id]
    return load_user_session(chat_id)


def _kb(text: str, style: Optional[str] = None) -> KeyboardButton:
    if not style:
        return KeyboardButton(text)
    try:
        return KeyboardButton(text, style=style)
    except TypeError:
        pass
    try:
        return KeyboardButton(text, api_kwargs={"style": style})
    except Exception:
        return KeyboardButton(text)


OFFER_BUTTONS = [
    "⚡ RUN (10 links)",
    "Late Night Cash",
    "🍳 Breakfast Cash",
    "💰 NEW 50 FC",
    "🪔 Karwa Chauth",
    "📦 Bulk Claim",
]


def is_button_locked(label: str) -> bool:
    locked = BOT_SETTINGS.get("locked_buttons") or []
    return label in locked


def main_keyboard(colored: bool = True, user_id: Optional[int] = None):
    locked = set(BOT_SETTINGS.get("locked_buttons") or [])
    show_all = user_id is not None and is_admin(user_id)

    def ok(label: str) -> bool:
        return show_all or label not in locked

    # Compact layout: exactly 2 buttons per row
    order = [
        ("⚡ RUN (10 links)", "primary"),
        ("Late Night Cash", "success"),
        ("🍳 Breakfast Cash", "success"),
        ("💰 NEW 50 FC", "success"),
        ("🪔 Karwa Chauth", "success"),
        ("📦 Bulk Claim", "primary"),
        ("📊 Status", "primary"),
        ("🔐 Login", "primary"),
        ("📤 Session", "primary"),
        ("📜 History", "success"),
        ("🚪 Logout", "danger"),
    ]
    visible = [(lab, sty) for lab, sty in order if ok(lab)]
    keyboard = []
    if colored:
        row = []
        for lab, sty in visible:
            row.append(_kb(lab, sty))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
    else:
        row = []
        for lab, _sty in visible:
            row.append(KeyboardButton(lab))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
    try:
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)
    except TypeError:
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)



def admin_keyboard():
    fj = "ON ✅" if BOT_SETTINGS.get("force_join_enabled") else "OFF ❌"
    locked = set(BOT_SETTINGS.get("locked_buttons") or [])

    def lock_label(name: str) -> str:
        short = {
            "⚡ RUN (10 links)": "RUN",
            "Late Night Cash": "Night",
            "🍳 Breakfast Cash": "Breakfast",
            "💰 NEW 50 FC": "50 FC",
            "🪔 Karwa Chauth": "Karwa",
            "📦 Bulk Claim": "Bulk",
        }.get(name, name[:12])
        return f"🔒 {short}" if name in locked else f"🔓 {short}"

    keyboard = [
        [_kb("🔧 Force Join: " + fj, "primary")],
        [_kb("🎛 Lock/Unlock Offers", "primary")],
        [_kb(lock_label("⚡ RUN (10 links)"), "primary"),
         _kb(lock_label("Late Night Cash"), "success")],
        [_kb(lock_label("🍳 Breakfast Cash"), "success"),
         _kb(lock_label("💰 NEW 50 FC"), "success")],
        [_kb(lock_label("🪔 Karwa Chauth"), "success"),
         _kb(lock_label("📦 Bulk Claim"), "primary")],
        [_kb("➕ Add Channel", "success"), _kb("➖ Remove Channel", "danger")],
        [_kb("📋 List Channels", "primary")],
        [_kb("👥 Users & Claims", "primary"), _kb("📜 Claim History", "success")],
        [_kb("🔙 Back", "danger")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def safe_reply(message, text: str, parse_mode: Optional[str] = "Markdown", reply_markup=None, **kwargs):
    try:
        return await message.reply_text(text, parse_mode=parse_mode,
                                        reply_markup=reply_markup or main_keyboard(), **kwargs)
    except Exception:
        try:
            return await message.reply_text(text, parse_mode=None,
                                            reply_markup=main_keyboard(colored=False), **kwargs)
        except Exception as e:
            log.warning("safe_reply failed: %s", e)
            return await message.reply_text(str(text)[:500])


async def safe_edit(msg, text: str, parse_mode: Optional[str] = "Markdown", reply_markup=None, **kwargs):
    try:
        return await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup, **kwargs)
    except Exception:
        try:
            return await msg.edit_text(text, parse_mode=None, reply_markup=reply_markup)
        except Exception:
            try:
                return await msg.edit_text(text[:500])
            except Exception as e:
                log.warning("safe_edit failed: %s", e)
                return None


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = get_session(chat_id)
    logged = "✅ Logged in" if session else "❌ Not logged in"
    uid = _account_user_id(session) if session else "--"
    admin_badge = " · 👑 Admin" if user and is_admin(user.id) else ""
    text = (
        "✦•┈┈┈┈┈┈┈┈┈┈┈┈┈┈•✦\n"
        "⚡ *SWIGGY CASH LOOT*\n"
        "✦•┈┈┈┈┈┈┈┈┈┈┈┈┈┈•✦\n\n"
        f"Status  : {logged}{admin_badge}\n"
        f"User ID : `{uid}`\n"
        f"Session : TTL auto-logout\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Quick actions*\n"
        "⚡ *RUN* — 10 cashloot links\n"
        "🌙 *Late Night Cash* — night BYO\n"
        "🍳 *Breakfast Cash* — morning lock\n"
        "💰 *NEW 50 FC* — Pick Your Offer\n"
        "🪔 *Karwa Chauth* — festive BYO\n"
        "📦 *Bulk Claim* — multi-session\n"
        "📊 *Status* · 📜 *History*\n"
        "🔐 *Login* · 📤 *Session* · 🚪 *Logout*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "_Premium flow · auto apply · live campaigns_"
    )
    kb = main_keyboard(user_id=user.id if user else None)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_phone", None)
    context.user_data.pop("awaiting_session", None)
    context.user_data.pop("awaiting_channel", None)

    text = (update.message.text or "").strip()
    uid = update.effective_user.id

    if not is_admin(uid):
        locked_hit = None
        if is_button_locked("⚡ RUN (10 links)") and (
                text in ("⚡ RUN (10 links)", "RUN", "/run") or text.startswith("⚡ RUN")):
            locked_hit = "⚡ RUN (10 links)"
        elif is_button_locked("Late Night Cash") and text == "Late Night Cash":
            locked_hit = "Late Night Cash"
        elif is_button_locked("🍳 Breakfast Cash") and text in ("🍳 Breakfast Cash", "Breakfast Cash"):
            locked_hit = "🍳 Breakfast Cash"
        elif is_button_locked("💰 NEW 50 FC") and text in ("💰 NEW 50 FC", "NEW 50 FC"):
            locked_hit = "💰 NEW 50 FC"
        elif is_button_locked("🪔 Karwa Chauth") and text in ("🪔 Karwa Chauth", "Karwa Chauth", "Karwachauth"):
            locked_hit = "🪔 Karwa Chauth"
        elif is_button_locked("📦 Bulk Claim") and text in ("📦 Bulk Claim", "Bulk Claim", "Bulk"):
            locked_hit = "📦 Bulk Claim"
        if locked_hit:
            await update.message.reply_text(
                f"🔒 *{locked_hit}* abhi admin ne lock kiya hai.\nBaad mein try karo.",
                parse_mode="Markdown", reply_markup=main_keyboard(user_id=uid))
            return

    if text.startswith("🔧 Force Join:"):
        if not is_admin(uid):
            await update.message.reply_text("❌ Admin only."); return
        BOT_SETTINGS["force_join_enabled"] = not BOT_SETTINGS.get("force_join_enabled")
        save_settings(BOT_SETTINGS)
        state = "ON ✅" if BOT_SETTINGS["force_join_enabled"] else "OFF ❌"
        await update.message.reply_text(f"Force Join ab *{state}*",
                                        parse_mode="Markdown", reply_markup=admin_keyboard())
        return
    if text == "🎛 Lock/Unlock Offers":
        if not is_admin(uid):
            return
        locked = BOT_SETTINGS.get("locked_buttons") or []
        lines = ["*Offer buttons status:*\n"]
        for b in OFFER_BUTTONS:
            st = "🔒 LOCKED (hidden)" if b in locked else "🔓 OPEN"
            lines.append(f"• {b}: *{st}*")
        lines.append("\nNeeche short buttons se toggle karo.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=admin_keyboard())
        return
    _lock_map = {
        "🔒 RUN": "⚡ RUN (10 links)", "🔓 RUN": "⚡ RUN (10 links)",
        "🔒 Night": "Late Night Cash", "🔓 Night": "Late Night Cash",
        "🔒 Breakfast": "🍳 Breakfast Cash", "🔓 Breakfast": "🍳 Breakfast Cash",
        "🔒 50 FC": "💰 NEW 50 FC", "🔓 50 FC": "💰 NEW 50 FC",
        "🔒 Karwa": "🪔 Karwa Chauth", "🔓 Karwa": "🪔 Karwa Chauth",
        "🔒 Bulk": "📦 Bulk Claim", "🔓 Bulk": "📦 Bulk Claim",
    }
    if text in _lock_map:
        if not is_admin(uid):
            return
        target = _lock_map[text]
        locked = list(BOT_SETTINGS.get("locked_buttons") or [])
        if target in locked:
            locked.remove(target)
            msg = f"🔓 *Unlocked:* `{target}`\nAb sab users ko dikhega."
        else:
            locked.append(target)
            msg = f"🔒 *Locked:* `{target}`\nAb users se hide ho jayega."
        BOT_SETTINGS["locked_buttons"] = locked
        save_settings(BOT_SETTINGS)
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=admin_keyboard())
        return
    if text == "➕ Add Channel":
        if not is_admin(uid):
            return
        context.user_data["awaiting_channel"] = "add"
        await update.message.reply_text(
            "Channel/Group username bhejo:\nExample: `@mychannel`\nBot ko us channel mein *admin* banao pehle.")
        return
    if text == "➖ Remove Channel":
        if not is_admin(uid):
            return
        context.user_data["awaiting_channel"] = "remove"
        await update.message.reply_text("Remove karne ke liye `@channel` bhejo.")
        return
    if text == "📋 List Channels":
        if not is_admin(uid):
            return
        chs = BOT_SETTINGS.get("force_channels") or []
        fj = "ON" if BOT_SETTINGS.get("force_join_enabled") else "OFF"
        msg = f"Force Join: *{fj}*\nChannels ({len(chs)}):\n"
        msg += "\n".join(f"• `{c}`" for c in chs) if chs else "• (none)"
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=admin_keyboard())
        return
    if text == "👥 Users & Claims":
        if not is_admin(uid):
            return
        await update.message.reply_text(_admin_user_stats_text(), parse_mode="Markdown", reply_markup=admin_keyboard())
        return
    if text == "📜 Claim History":
        if not is_admin(uid):
            return
        await update.message.reply_text(_admin_claim_history_text(), parse_mode="Markdown", reply_markup=admin_keyboard())
        return
        return
    if text == "🔙 Back":
        await update.message.reply_text("Main menu", reply_markup=main_keyboard())
        return

    if text in {"⚡ RUN (10 links)", "Late Night Cash", "🍳 Breakfast Cash", "Breakfast Cash",
                "📊 Status", "🔐 Login", "📤 Session", "📜 History", "🚪 Logout",
                "💰 NEW 50 FC", "NEW 50 FC", "🪔 Karwa Chauth", "Karwa Chauth", "Karwachauth",
                "📦 Bulk Claim", "Bulk Claim", "Bulk"} \
            and not await check_force_join(update, context):
        return

    if text == "⚡ RUN (10 links)":
        await run_cmd(update, context)
    elif text == "Late Night Cash":
        await night_cash_cmd(update, context)
    elif text in ("🍳 Breakfast Cash", "Breakfast Cash"):
        await breakfast_cash_cmd(update, context)
    elif text in ("💰 NEW 50 FC", "NEW 50 FC"):
        await pyo_cash_cmd(update, context)
    elif text in ("🪔 Karwa Chauth", "Karwa Chauth", "Karwachauth"):
        await kc_cash_cmd(update, context)
    elif text in ("📦 Bulk Claim", "Bulk Claim", "Bulk"):
        await bulk_claim_cmd(update, context)
    elif text == "📊 Status":
        await status_cmd(update, context)
    elif text == "📜 History":
        await history_cmd(update, context)
    elif text == "🔐 Login":
        await update.message.reply_text(
            "🔐 *Login method select karo:*", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📄 JSON Session", callback_data="login_json")],
                [InlineKeyboardButton("📱 Mobile + OTP", callback_data="login_phone")],
            ]))
    elif text == "📤 Session":
        context.user_data["awaiting_session"] = True
        await update.message.reply_text(
            "Session JSON paste karo:\n\n`{\"token\":\"...\",\"tid\":\"eyJ...\"}`\n\n"
            "Ya HAR / export wala full JSON.", parse_mode="Markdown")
    elif text == "🚪 Logout":
        await logout_cmd(update, context)


async def _do_login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
    chat_id = update.effective_chat.id
    if not re.fullmatch(r"\d{10}", phone):
        await update.message.reply_text("❌ Phone must be exactly 10 digits.")
        return ConversationHandler.END
    msg = await update.message.reply_text(f"📲 Requesting OTP for `{phone}`...", parse_mode="Markdown")
    result = await request_otp(phone)
    if not result["ok"]:
        await msg.edit_text(f"❌ Failed to send OTP:\n{result.get('msg', 'unknown')}")
        return ConversationHandler.END
    user_pending_otp[chat_id] = result
    context.user_data.pop("awaiting_phone", None)
    await msg.edit_text(f"✅ SMS sent to `{phone}`\n\nAb OTP bhejo (sirf number):\n`938528`", parse_mode="Markdown")
    return WAITING_OTP


async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return ConversationHandler.END
    if not context.args:
        await update.message.reply_text("Phone number bhejo (10 digit):\nExample: `9876543210`", parse_mode="Markdown")
        context.user_data["awaiting_phone"] = True
        return ConversationHandler.END
    return await _do_login_phone(update, context, context.args[0].strip())


async def otp_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    otp = update.message.text.strip()
    pending = user_pending_otp.get(chat_id)
    if not pending:
        return ConversationHandler.END
    if not re.match(r"^\d{4,6}$", otp):
        await update.message.reply_text("❌ Invalid OTP. 4-6 digits bhejo.")
        return WAITING_OTP
    msg = await update.message.reply_text("🔄 Verifying OTP...")
    try:
        result = await verify_otp(pending, otp)
    except Exception as e:
        log.exception("verify_otp error")
        await safe_edit(msg, f"❌ OTP verify error:\n{e}")
        return ConversationHandler.END
    if not result.get("ok"):
        await safe_edit(msg, f"❌ OTP failed:\n{result.get('msg', 'unknown')}")
        return ConversationHandler.END
    session = result["session"]
    try:
        save_user_session(chat_id, session)
    except Exception as e:
        log.exception("save session failed")
        await safe_edit(msg, f"❌ Session save failed:\n{e}")
        return ConversationHandler.END
    user_pending_otp.pop(chat_id, None)
    try:
        await record_user_activity(update, verified=True)
    except Exception:
        pass
    await safe_edit(msg, f"✅ *Login successful!*\n(auto-logout 5 min)\n\n"
                         f"Customer ID: `{session.get('customerId')}`\n"
                         f"Mobile: `{session.get('mobile')}`\n"
                         f"User ID: `{_account_user_id(session)}`\n\n"
                         "Ab ⚡ *RUN* ya *Late Night Cash* use karo",
                    reply_markup=main_keyboard())
    return ConversationHandler.END


async def session_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return ConversationHandler.END
    context.user_data.pop("awaiting_phone", None)
    context.user_data["awaiting_session"] = True
    await update.message.reply_text(
        "Session JSON paste karo:\n\n`{\"token\":\"...\",\"tid\":\"eyJ...\"}`\n\n"
        "Ya HAR / export wala full JSON.", parse_mode="Markdown")
    return WAITING_SESSION


async def session_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    raw = update.message.text.strip()
    try:
        session = parse_session_string(raw)
        user_id = _account_user_id(session)
        if not user_id:
            uid = _userid_from_tid(session.get("tid", ""))
            if uid:
                session["userid"] = uid
                user_id = uid
        save_user_session(chat_id, session)
        await update.message.reply_text(
            f"✅ Session saved!\n(session expires after configured TTL)\n\n"
            f"User ID: `{user_id or 'unknown'}`\n"
            f"Mobile: `{session.get('mobile') or session.get('phone') or 'N/A'}`\n\n"
            "Ab ⚡ *RUN* ya *Late Night Cash* use karo",
            parse_mode="Markdown", reply_markup=main_keyboard())
    except Exception as e:
        await update.message.reply_text(f"❌ Invalid session:\n{e}")
    return ConversationHandler.END


async def session_expiry_watcher(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(timezone.utc).timestamp()
    for path in list(Path(SESSIONS_DIR).glob("*.json")):
        try:
            chat_id = int(path.stem)
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            saved_at = float(payload.get("saved_at") or 0)
            if saved_at and now - saved_at > SESSION_TTL_SECONDS:
                session_data = payload.get("session") or {}
                phone = str(session_data.get("mobile") or session_data.get("phone") or "N/A").strip()
                clear_user_session(chat_id)
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=("✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n⏱️ *SESSION EXPIRED*\n"
                              "✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n"
                              f"📱 Number: `{phone}`\n\n"
                              "This number has been *automatically logged out*\nafter 5 minutes of inactivity.\n\n"
                              "Please login again to continue."),
                        parse_mode="Markdown", reply_markup=main_keyboard())
                except Exception:
                    pass
        except Exception:
            log.exception("Session expiry check failed for %s", path)


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if session:
        phone = str(session.get("mobile") or session.get("phone") or "N/A").strip()
        clear_user_session(chat_id)
        await update.message.reply_text(
            "✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n🚪 *LOGGED OUT*\n✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n"
            f"📱 Number: `{phone}`\n\nThis number has been *logged out*.\n"
            "Session cleared for security.\n\nWapas use karne ke liye *Login* karo.",
            parse_mode="Markdown", reply_markup=main_keyboard())
    else:
        await update.message.reply_text("No session was saved.", reply_markup=main_keyboard())


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    uid = update.effective_user.id
    claims = [x for x in (BOT_STATS.get("claims") or []) if x.get("telegram_id") == uid]
    joins = [x for x in (BOT_STATS.get("history") or []) if x.get("telegram_id") == uid]
    records = claims + joins
    records.sort(key=lambda x: x.get("time") or "", reverse=True)
    records = records[:20]
    if not records:
        await update.message.reply_text("📜 *Claim History*\n\nNo claim/join history yet.",
                                        parse_mode="Markdown", reply_markup=main_keyboard())
        return
    ist = timezone(timedelta(hours=5, minutes=30))
    lines = ["📜 *Claim History*", "━━━━━━━━━━━━━━━━━━━━"]
    for i, item in enumerate(records, 1):
        raw = item.get("time") or ""
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ist)
            stamp = dt.strftime("%d-%m-%Y %I:%M %p IST")
        except Exception:
            stamp = str(raw)
        status = "✅ COMPLETE" if item.get("status") == "success" else "❌ FAIL"
        if item.get("type") == "join":
            label = item.get("label") or "Link"
            amount = _money(item.get("earned") or 0)
            lines.append(f"{i}. {status} — Join\n   Link: {label}\n   Cash: Rs.{amount}\n   Time: {stamp}")
        else:
            lines.append(f"{i}. {status} — Claim\n   Offer: {item.get('offer') or '--'}\n   Time: {stamp}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_keyboard())


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo:\n`/login 9876543210` ya `/session`", parse_mode="Markdown")
        return
    msg = await update.message.reply_text("📊 Fetching loot status...")
    campaign_id = DEFAULT_CAMPAIGN_ID
    jar = aiohttp.CookieJar(unsafe=True)
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(cookie_jar=jar, connector=connector) as http:
        ledger = await fetch_ledger(session, campaign_id, http)
    if not ledger["ok"]:
        await msg.edit_text(f"❌ Failed: {ledger['msg']}")
        return
    by_service = ledger.get("by_service") or {}
    text_lines = [f"💰 *Official Cash (Swiggy):* Rs.`{_money(ledger['earned'])}`",
                  f"Campaign: `{campaign_id}`",
                  f"Transactions: {len(ledger.get('transactions') or [])}"]
    if by_service:
        text_lines.append("\n*By Service:*")
        for k, v in by_service.items():
            text_lines.append(f"  • {k}: Rs.`{_money(_amount(v))}`")
    txns = ledger.get("transactions") or []
    if txns:
        text_lines.append("\n*Recent:*")
        for txn in txns[-5:]:
            if isinstance(txn, dict):
                who = txn.get("associatedUserName") or txn.get("userName") or "--"
                value = 0.0; bl = ""
                for b, offer in (txn.get("offersByBL") or {}).items():
                    if isinstance(offer, dict) and _amount(offer.get("offerValue")) > value:
                        bl = str(b); value = _amount(offer.get("offerValue"))
                text_lines.append(f"  • {who} — Rs.{_money(value)} ({bl})")
    await msg.edit_text("\n".join(text_lines), parse_mode="Markdown")


async def night_cash_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo (🔐 Login ya 📤 Session)\n"
                                        "Session expires after configured TTL.",
                                        reply_markup=main_keyboard())
        return
    touch_session(chat_id)
    day = await ln_current_day()
    if not day:
        await update.message.reply_text(
            f"🌙 Night Cash offer *is window mein active nahi*.\n\n"
            f"Window: *roz 11:00 AM → next day 6:00 AM IST*\n"
            f"Campaign: `{LN_CAMPAIGN_PREFIX}`\n\n"
            "Agar Swiggy app mein chal raha hai to *bot restart* karke phir try karo.",
            parse_mode="Markdown", reply_markup=main_keyboard())
        return
    msg = await update.message.reply_text(f"🌙 Checking Day {day} offers...")
    status = await ln_get_status(session, day)
    data = status.get("data") or {}
    already = data.get("value")
    if already:
        offer_name = LN_OFFERS.get(int(already), f"Offer {already}")
        await msg.edit_text(f"🌙 *Day {day}* — pehle se claimed!\n\nSelected: *{offer_name}*\n\nRaw: `{data}`",
                            parse_mode="Markdown")
        return
    rows = []
    for oid, name in LN_OFFERS.items():
        rows.append([InlineKeyboardButton(f"{oid}. {name}", callback_data=f"ln_pick_{day}_{oid}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="ln_cancel")])
    await msg.edit_text(f"🌙 *Night Cash — Day {day}*\n\nEk offer select karo (confirm baad mein hoga):",
                        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


async def paw_coupon_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo (🔐 Login / 📤 Session)", reply_markup=main_keyboard())
        return
    touch_session(chat_id)
    status = await paw_check_played(session)
    data = status.get("data") or {}
    already = False
    if isinstance(data, dict):
        if data.get("shouldNotify") or data.get("value") or data.get("played"):
            already = True
        note = data.get("note") or ""
        if "isIndia1City" in str(note) or data.get("exists"):
            already = True
    if already:
        await update.message.reply_text(
            "✅ *Already claimed* on this account\n\nSwiggy: *One coupon per user*.\n"
            "App → Coupons mein check karo.",
            parse_mode="Markdown", reply_markup=main_keyboard())
        return
    context.user_data["awaiting_paw_photo"] = True
    await update.message.reply_text(
        "✦•┈┈┈┈┈┈┈┈┈┈┈•✦\n🎁 *200 OFF COUPON*\n✦•┈┈┈┈┈┈┈┈┈┈┈•✦\n\n"
        "Swiggy *stock / google photos reject* karta hai.\n"
        "Isliye *apna real pet photo* bhejo (camera se best).\n\n"
        "📷 Abhi *ek photo* bhejo (dog / cat / pet).\nPhoto aate hi auto:\n"
        "1) Upload\n2) Score verify\n3) Coupon claim\n4) *Real min cart* app jaisa\n\n"
        "Cancel: /cancel",
        parse_mode="Markdown", reply_markup=main_keyboard())


async def _run_paw_claim_with_images(update: Update, context: ContextTypes.DEFAULT_TYPE, images_b64: List[str]) -> None:
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Session expired. Login again.", reply_markup=main_keyboard())
        return
    touch_session(chat_id)
    msg = await update.message.reply_text(
        "✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n⏳ *CLAIMING*\n"
        "Photo upload → score → coupon...\n15–40 sec lag sakte hain.",
        parse_mode="Markdown")
    result = await paw_claim_coupon(session, user_images_b64=images_b64)
    ok = bool(result.get("ok"))
    try:
        await record_claim(update, 0, 200, "PawParazzi ₹200 OFF", ok,
                           "" if ok else str(result.get("msg") or result.get("redeem") or ""))
    except Exception:
        pass
    if ok:
        redeem = result.get("redeem") or {}
        coupon_code = ""
        if isinstance(redeem, dict):
            coupon_code = str(redeem.get("couponCode") or redeem.get("code") or redeem.get("coupon") or "")
            if not coupon_code and isinstance(redeem.get("data"), dict):
                coupon_code = str(redeem["data"].get("couponCode") or redeem["data"].get("code") or "")
        min_cart = result.get("min_cart_actual")
        if min_cart is None:
            extracted = _paw_extract_min_cart(redeem if isinstance(redeem, dict) else {})
            min_cart = extracted if extracted and extracted != PAW_MIN_CART else 0
        card = _paw_coupon_card(min_cart or 0, coupon_code, str(result.get("userid") or ""),
                                str(result.get("tem_id") or ""),
                                f"🏙 India1City: `{result.get('isIndia1City')}`\n"
                                f"✅ Pet valid: `{result.get('steps', {}).get('is_valid_pet_image')}`\n")
        await msg.edit_text("✅ *COMPLETE* — Coupon unlocked!\n\n" + card, parse_mode="Markdown")
    else:
        err = result.get("msg") or result.get("redeem") or result
        await msg.edit_text(
            "✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n❌ *FAIL*\n\n"
            f"`{err}`\n\nDobara *clear real pet photo* bhejo (camera se).",
            parse_mode="Markdown")


async def paw_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_paw_photo"):
        return
    if not update.message or not update.message.photo:
        return
    if not await check_force_join(update, context):
        context.user_data.pop("awaiting_paw_photo", None)
        return
    context.user_data.pop("awaiting_paw_photo", None)
    photo = update.message.photo[-1]
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        raw = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        await update.message.reply_text(f"❌ Photo download fail: {e}")
        return
    if len(raw) < 1000:
        await update.message.reply_text("❌ Photo bahut chhoti hai. Clear pet photo bhejo.")
        context.user_data["awaiting_paw_photo"] = True
        return
    import base64 as _b64
    b64 = _b64.b64encode(raw).decode("ascii")
    await _run_paw_claim_with_images(update, context, [b64])


async def breakfast_cash_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo (🔐 Login ya 📤 Session)", reply_markup=main_keyboard())
        return
    touch_session(chat_id)
    day = await bf_current_day()
    if not day:
        await update.message.reply_text("🍳 Breakfast Cash offer abhi active nahi hai.\n"
                                        "Window: roz 11:00 AM – next day 11:00 AM IST",
                                        reply_markup=main_keyboard())
        return
    msg = await update.message.reply_text(f"🍳 Checking Breakfast Day {day} offers...")
    status = await bf_get_status(session, day)
    data = status.get("data") or {}
    already = data.get("value")
    if already:
        try:
            aid = int(already)
        except Exception:
            aid = already
        offer_name = BF_OFFERS.get(aid, f"Offer {already}")
        await msg.edit_text(f"🍳 *Breakfast Day {day}* — pehle se locked!\n\n"
                            f"Selected: *{offer_name}*\n\n`{data}`", parse_mode="Markdown")
        return
    rows = []
    for oid, name in BF_OFFERS.items():
        rows.append([InlineKeyboardButton(f"{oid}. {name}", callback_data=f"bf_pick_{day}_{oid}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="bf_cancel")])
    await msg.edit_text(
        f"🍳 *Breakfast Cash — Day {day}*\n\n*Tap to lock your offer:*\n"
        f"1. ₹50 FREE Cash (breakfast ≥ ₹199)\n2. ₹150 OFF Coupon (≥ ₹249)\n"
        f"3. ₹100 OFF + ₹50 Free Cash (≥ ₹249)\n4. ₹100 OFF Coupon (≥ ₹149)\n\n"
        f"Select karke confirm karo:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))


# ═════════════════════════════════════════════════════════════════════════════
# Shrestho Pujo handlers
# ═════════════════════════════════════════════════════════════════════════════


async def pyo_cash_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text(
            "❌ Pehle login karo (🔐 Login ya 📤 Session)",
            reply_markup=main_keyboard(user_id=update.effective_user.id if update.effective_user else None),
        )
        return
    touch_session(chat_id)
    msg = await update.message.reply_text("💰 Checking *NEW 50 FC / Pick Your Offer*...", parse_mode="Markdown")
    status = await pyo_get_status(session)
    data = status.get("data") or {}
    campaign = status.get("campaign") or PYO_CAMPAIGN_ID
    already = data.get("value")
    if already not in (None, "", 0, "0"):
        try:
            aid = int(already)
        except Exception:
            aid = already
        offer_name = PYO_OFFERS.get(aid, f"Offer {already}")
        await msg.edit_text(
            f"💰 *NEW 50 FC* — pehle se claimed!\n\n"
            f"Selected: *{offer_name}*\n\n"
            "Swiggy app → Coupons / Free Cash mein check karo.",
            parse_mode="Markdown",
        )
        return
    rows = []
    for oid, name in PYO_OFFERS.items():
        rows.append([InlineKeyboardButton(f"{oid}. {name}", callback_data=f"pyo_pick_{oid}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="pyo_cancel")])
    await msg.edit_text(
        "💰 *NEW 50 FC — Pick Your Offer*\n\n"
        "Ek offer select karo (confirm baad mein hoga):\n"
        "1. ₹50 FREE Cash (order ≥ ₹199)\n"
        "2. ₹150 OFF Coupon (≥ ₹249)\n"
        "3. ₹130 OFF Coupon (≥ ₹199)\n"
        "4. ₹100 OFF Coupon (≥ ₹149)\n\n"
        "Select karke confirm karo — account pe apply ho jayega.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def pyo_cash_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    await query.answer()
    if data == "pyo_cancel":
        await query.edit_message_text("Cancelled.")
        return
    if data.startswith("pyo_pick_"):
        offer_id = int(data.split("_")[2])
        offer_name = PYO_OFFERS.get(offer_id, f"Offer {offer_id}")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Confirm: {offer_name}", callback_data=f"pyo_confirm_{offer_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="pyo_back")],
        ])
        await query.edit_message_text(
            f"💰 Confirm karo:\n\nOffer: *{offer_name}*\n\n"
            "Confirm ke baad claim ho jayega aur account pe apply hoga.",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return
    if data == "pyo_back":
        rows = []
        for oid, name in PYO_OFFERS.items():
            rows.append([InlineKeyboardButton(f"{oid}. {name}", callback_data=f"pyo_pick_{oid}")])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="pyo_cancel")])
        await query.edit_message_text(
            "💰 *NEW 50 FC — Pick Your Offer*\n\nEk offer select karo:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return
    if data.startswith("pyo_confirm_"):
        if not await check_force_join(update, context):
            return
        offer_id = int(data.split("_")[2])
        offer_name = PYO_OFFERS.get(offer_id, f"Offer {offer_id}")
        chat_id = update.effective_chat.id
        session = get_session(chat_id)
        if not session:
            await query.edit_message_text("❌ Session lost. Login again.")
            return
        try:
            await query.edit_message_text(
                f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n⏳ *CLAIMING*\n🔗 {offer_name}",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        result = await pyo_claim_offer(session, offer_id)
        claim_ok = bool(result.get("ok"))
        claim_error = "" if claim_ok else str(result.get("data") or result.get("msg") or result)
        await record_claim(update, 0, offer_id, f"PYO:{offer_name}", claim_ok, claim_error)
        if claim_ok:
            code = result.get("coupon_code") or ""
            code_line = f"🎫 Code: `{code}`\n" if code else ""
            await query.edit_message_text(
                f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n✅ *COMPLETE* — Claim successful\n\n"
                f"Offer: *{offer_name}*\n"
                f"{code_line}\n"
                "Swiggy app → Coupons / Free Cash mein check karo.\n"
                "Free Cash apply hone mein 1–2 min lag sakte hain.",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n❌ *FAIL* — Claim failed\n\n"
                f"`{claim_error or 'Unknown error'}`",
                parse_mode="Markdown",
            )
        return



async def kc_cash_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text(
            "❌ Pehle login karo (🔐 Login ya 📤 Session)",
            reply_markup=main_keyboard(user_id=update.effective_user.id if update.effective_user else None),
        )
        return
    touch_session(chat_id)
    msg = await update.message.reply_text("🪔 Checking *Karwa Chauth* offers...", parse_mode="Markdown")
    await _fetch_kc_config()
    window = _kc_window_state()
    status = await kc_get_status(session)
    data = status.get("data") or {}
    already = data.get("value")
    if already not in (None, "", 0, "0"):
        try:
            aid = int(already)
        except Exception:
            aid = already
        offer_name = KC_OFFERS.get(aid, f"Offer {already}")
        phase = window.get("phase")
        extra = ""
        if phase == "redeem":
            extra = "\n✅ Redeem window open — app mein Free Cash / Coupons check karo."
        elif phase == "waiting_redeem":
            extra = f"\n⏳ Redeem starts: `{window.get('redeem_start')}`"
        await msg.edit_text(
            f"🪔 *Karwa Chauth* — pehle se selected!\n\n"
            f"Selected: *{offer_name}*\n"
            f"{extra}\n\n"
            "Swiggy app → Coupons / Free Cash mein verify karo.",
            parse_mode="Markdown",
        )
        return

    if not window.get("can_select"):
        phase = window.get("phase")
        if phase == "upcoming":
            await msg.edit_text(
                f"🪔 *Karwa Chauth*\n\nSelection abhi start nahi hua.\nStarts: `{window.get('vote_start')}`",
                parse_mode="Markdown",
            )
            return
        if phase == "waiting_redeem":
            await msg.edit_text(
                f"🪔 *Karwa Chauth*\n\nSelection band. Redeem: `{window.get('redeem_start')}` se.",
                parse_mode="Markdown",
            )
            return
        if phase == "redeem":
            await msg.edit_text(
                "🪔 *Karwa Chauth*\n\nSelection window khatam. Agar pehle select kiya tha to redeem window mein cash/coupon apply hoga.",
                parse_mode="Markdown",
            )
            return
        await msg.edit_text("🪔 *Karwa Chauth* offer window closed.", parse_mode="Markdown")
        return

    rows = []
    for oid, name in KC_OFFERS.items():
        rows.append([InlineKeyboardButton(f"{oid}. {name}", callback_data=f"kc_pick_{oid}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="kc_cancel")])
    await msg.edit_text(
        "🪔 *Karwa Chauth — Pick Your Offer*\n\n"
        "Ek offer select karo (confirm baad mein):\n"
        "1. ₹50 FREE CASH (above ₹249)\n"
        "2. ₹150 OFF Coupon (above ₹299)\n"
        "3. ₹200 OFF Coupon (above ₹499)\n"
        "4. ₹30 FREE Cash (above ₹149)\n\n"
        "Confirm ke baad account pe lock ho jayega.\n"
        f"Selection till: `{window.get('vote_end')}`\n"
        f"Redeem from: `{window.get('redeem_start')}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def kc_cash_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    await query.answer()
    if data == "kc_cancel":
        await query.edit_message_text("Cancelled.")
        return
    if data.startswith("kc_pick_"):
        offer_id = int(data.split("_")[2])
        offer_name = KC_OFFERS.get(offer_id, f"Offer {offer_id}")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Confirm: {offer_name}", callback_data=f"kc_confirm_{offer_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="kc_back")],
        ])
        await query.edit_message_text(
            f"🪔 Confirm karo:\n\nOffer: *{offer_name}*\n\n"
            "Confirm ke baad claim/lock ho jayega.",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return
    if data == "kc_back":
        rows = []
        for oid, name in KC_OFFERS.items():
            rows.append([InlineKeyboardButton(f"{oid}. {name}", callback_data=f"kc_pick_{oid}")])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="kc_cancel")])
        await query.edit_message_text(
            "🪔 *Karwa Chauth — Pick Your Offer*\n\nEk offer select karo:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return
    if data.startswith("kc_confirm_"):
        if not await check_force_join(update, context):
            return
        offer_id = int(data.split("_")[2])
        offer_name = KC_OFFERS.get(offer_id, f"Offer {offer_id}")
        chat_id = update.effective_chat.id
        session = get_session(chat_id)
        if not session:
            await query.edit_message_text("❌ Session lost. Login again.")
            return
        try:
            await query.edit_message_text(
                f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n⏳ *CLAIMING*\n🔗 {offer_name}",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        result = await kc_claim_offer(session, offer_id)
        claim_ok = bool(result.get("ok"))
        already = bool(result.get("already"))
        claim_error = "" if claim_ok else str(result.get("data") or result.get("msg") or result)
        await record_claim(update, 0, offer_id, f"KC:{offer_name}", claim_ok, claim_error)
        if claim_ok:
            code = result.get("coupon_code") or ""
            code_line = f"🎫 Code: `{code}`\n" if code else ""
            already_line = "_(already selected)_\n" if already else ""
            window = result.get("window") or _kc_window_state()
            redeem_line = f"\nRedeem from: `{window.get('redeem_start') or KC_REDEEM_START}`"
            await query.edit_message_text(
                f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n✅ *SUCCESS*\n\n"
                f"Offer: *{offer_name}*\n"
                f"{already_line}{code_line}"
                f"{redeem_line}\n\n"
                "Swiggy app → Coupons / Free Cash mein check karo.\n"
                "Cash/coupon redeem window mein apply hota hai.",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n❌ *FAIL*\n\n"
                f"`{claim_error or 'Unknown error'}`",
                parse_mode="Markdown",
            )
        return


# ── Bulk Claim (up to 10 session JSONs) ──
WAITING_BULK = 99  # extra state (ConversationHandler optional; we use user_data flag)


async def bulk_claim_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bulk real claim for ALL active offers (KC / PYO / Night / Breakfast)."""
    try:
        if not await check_force_join(update, context):
            return
        # Resolve current night / breakfast day (may be None if window closed)
        try:
            ln_day = await ln_current_day()
        except Exception:
            ln_day = None
        try:
            bf_day = await bf_current_day()
        except Exception:
            bf_day = None

        rows = [
            [InlineKeyboardButton("—— Karwa Chauth ——", callback_data="bulk_noop")],
            [InlineKeyboardButton("KC1 ₹50 FREE", callback_data="bulk_pick_kc_1"),
             InlineKeyboardButton("KC2 ₹150 OFF", callback_data="bulk_pick_kc_2")],
            [InlineKeyboardButton("KC3 ₹200 OFF", callback_data="bulk_pick_kc_3"),
             InlineKeyboardButton("KC4 ₹30 FREE", callback_data="bulk_pick_kc_4")],
            [InlineKeyboardButton("—— NEW 50 FC ——", callback_data="bulk_noop")],
            [InlineKeyboardButton("50FC ₹50 FREE", callback_data="bulk_pick_pyo_1"),
             InlineKeyboardButton("50FC ₹150 OFF", callback_data="bulk_pick_pyo_2")],
            [InlineKeyboardButton("50FC ₹130 OFF", callback_data="bulk_pick_pyo_3"),
             InlineKeyboardButton("50FC ₹100 OFF", callback_data="bulk_pick_pyo_4")],
        ]
        # Night cash (day-based)
        if ln_day:
            rows.append([InlineKeyboardButton(f"—— Night Cash D{ln_day} ——", callback_data="bulk_noop")])
            rows.append([
                InlineKeyboardButton("Night ₹50 FC", callback_data=f"bulk_pick_ln_{ln_day}_1"),
                InlineKeyboardButton("Night ₹150 OFF", callback_data=f"bulk_pick_ln_{ln_day}_2"),
            ])
            rows.append([
                InlineKeyboardButton("Night ₹100 OFF", callback_data=f"bulk_pick_ln_{ln_day}_3"),
                InlineKeyboardButton("Night Offer 4", callback_data=f"bulk_pick_ln_{ln_day}_4"),
            ])
        else:
            rows.append([InlineKeyboardButton("Night Cash (window closed)", callback_data="bulk_noop")])

        if bf_day:
            rows.append([InlineKeyboardButton(f"—— Breakfast D{bf_day} ——", callback_data="bulk_noop")])
            rows.append([
                InlineKeyboardButton("BF ₹50 FREE", callback_data=f"bulk_pick_bf_{bf_day}_1"),
                InlineKeyboardButton("BF ₹150 OFF", callback_data=f"bulk_pick_bf_{bf_day}_2"),
            ])
            rows.append([
                InlineKeyboardButton("BF ₹100+₹50", callback_data=f"bulk_pick_bf_{bf_day}_3"),
                InlineKeyboardButton("BF ₹100 OFF", callback_data=f"bulk_pick_bf_{bf_day}_4"),
            ])
        else:
            rows.append([InlineKeyboardButton("Breakfast (window closed)", callback_data="bulk_noop")])

        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="bulk_cancel")])

        await update.message.reply_text(
            "📦 *Bulk Claim — ALL OFFERS*\n\n"
            "Pehle offer select karo, phir upto *10 session JSON* paste karo.\n"
            "Har account pe *real claim* hoga — fake status nahi.\n\n"
            "Supports:\n"
            "• Karwa Chauth (1–4)\n"
            "• NEW 50 FC / PYO (1–4)\n"
            "• Late Night Cash (current day)\n"
            "• Breakfast Cash (current day)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except Exception as e:
        log.exception("bulk_claim_cmd failed")
        try:
            await update.message.reply_text(f"❌ Bulk open fail: `{e}`", parse_mode="Markdown")
        except Exception:
            pass


async def bulk_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    try:
        await query.answer()
    except Exception:
        pass
    try:
        if data in ("bulk_cancel",):
            context.user_data.pop("bulk_offer", None)
            context.user_data.pop("awaiting_bulk", None)
            await query.edit_message_text("Cancelled.")
            return
        if data == "bulk_noop":
            return

        if not data.startswith("bulk_pick_"):
            return

        # Formats:
        #   bulk_pick_kc_1
        #   bulk_pick_pyo_2
        #   bulk_pick_ln_{day}_{oid}
        #   bulk_pick_bf_{day}_{oid}
        parts = data.split("_")
        # ["bulk","pick","kc","1"] or ["bulk","pick","ln","3","2"]
        if len(parts) < 4:
            await query.edit_message_text("❌ Invalid selection.")
            return
        kind = parts[2]

        if kind in ("kc", "pyo"):
            oid = int(parts[3])
            day = 0
            if kind == "kc":
                name = KC_OFFERS.get(oid, f"KC Offer {oid}")
                label = f"Karwa Chauth — {name}"
            else:
                name = PYO_OFFERS.get(oid, f"PYO Offer {oid}")
                label = f"NEW 50 FC — {name}"
        elif kind in ("ln", "bf"):
            if len(parts) < 5:
                await query.edit_message_text("❌ Invalid day/offer.")
                return
            day = int(parts[3])
            oid = int(parts[4])
            if kind == "ln":
                name = LN_OFFERS.get(oid, f"Offer {oid}")
                label = f"Night Cash D{day} — {name}"
            else:
                name = BF_OFFERS.get(oid, f"Offer {oid}")
                label = f"Breakfast D{day} — {name}"
        else:
            await query.edit_message_text("❌ Unknown offer type.")
            return

        context.user_data["bulk_offer"] = {
            "kind": kind,
            "offer_id": oid,
            "day": day,
            "label": label,
        }
        context.user_data["awaiting_bulk"] = True
        await query.edit_message_text(
            f"📦 Selected: *{label}*\n\n"
            "Ab *upto 10 session JSON* paste karo.\n"
            "• `{\"token\":\"...\",\"tid\":\"...\"}`\n"
            "• Multiple JSONs OK\n"
            "• Max 10 accounts\n\n"
            "Real claim chalega — status accurate aayega.\n"
            "/cancel se band.",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.exception("bulk_callback failed")
        try:
            await query.edit_message_text(f"❌ Error: `{e}`", parse_mode="Markdown")
        except Exception:
            pass


def _split_session_blobs(text: str) -> List[str]:
    blobs: List[str] = []
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text or "", re.I):
        chunk = (m.group(1) or "").strip()
        if chunk:
            blobs.append(chunk)
    if not blobs:
        depth = 0
        start = None
        for i, ch in enumerate(text or ""):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    blobs.append(text[start:i + 1])
                    start = None
    if not blobs:
        for line in (text or "").splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                blobs.append(line)
    seen = set()
    out: List[str] = []
    for b in blobs:
        key = b.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
        if len(out) >= 10:
            break
    return out[:10]


async def _bulk_run_one(session: Dict, kind: str, offer_id: int, day: int) -> Dict[str, Any]:
    """Dispatch to the real claim function for the selected offer type."""
    if kind == "kc":
        return await kc_claim_offer(session, offer_id)
    if kind == "pyo":
        return await pyo_claim_offer(session, offer_id)
    if kind == "ln":
        # real night-cash notify claim for current day
        if not day:
            day = await ln_current_day() or 0
        if not day:
            return {"ok": False, "msg": "Night Cash window not active"}
        return await ln_claim_offer(session, day, offer_id)
    if kind == "bf":
        if not day:
            day = await bf_current_day() or 0
        if not day:
            return {"ok": False, "msg": "Breakfast window not active"}
        return await bf_claim_offer(session, day, offer_id)
    return {"ok": False, "msg": f"unknown kind {kind}"}


async def bulk_sessions_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get("awaiting_bulk"):
        return False
    try:
        if not await check_force_join(update, context):
            context.user_data.pop("awaiting_bulk", None)
            context.user_data.pop("bulk_offer", None)
            return True
    except Exception:
        pass

    offer = context.user_data.get("bulk_offer") or {}
    kind = str(offer.get("kind") or "kc")
    try:
        offer_id = int(offer.get("offer_id") or 0)
    except Exception:
        offer_id = 0
    try:
        day = int(offer.get("day") or 0)
    except Exception:
        day = 0
    label = offer.get("label") or "offer"
    raw = (update.message.text if update.message else "") or ""
    blobs = _split_session_blobs(raw)
    if not blobs:
        await update.message.reply_text(
            "❌ Koi valid JSON session nahi mila.\n"
            "Example: `{\"token\":\"...\",\"tid\":\"...\"}` (max 10)",
            parse_mode="Markdown",
        )
        return True

    context.user_data.pop("awaiting_bulk", None)
    context.user_data.pop("bulk_offer", None)

    msg = await update.message.reply_text(
        f"📦 Bulk *{label}*\nSessions: `{len(blobs)}`\nReal claiming...",
        parse_mode="Markdown",
    )
    lines = [f"📦 *Bulk History — {label}*", f"Total accounts: `{len(blobs)}`", ""]
    ok_n = fail_n = already_n = 0

    for i, blob in enumerate(blobs, 1):
        try:
            session = parse_session_string(blob)
            if not _account_user_id(session) and session.get("tid"):
                uid = _userid_from_tid(str(session.get("tid")))
                if uid:
                    session["userid"] = uid
        except Exception as e:
            fail_n += 1
            lines.append(f"{i}. ❌ parse: `{str(e)[:90]}`")
            continue

        uid = _account_user_id(session) or "?"
        mobile = str(session.get("mobile") or session.get("phone") or "")
        try:
            result = await _bulk_run_one(session, kind, offer_id, day)
            claim_ok = bool(result.get("ok"))
            already = bool(result.get("already"))
            # Night/BF sometimes return ok without 'already' — detect from data
            if not already and isinstance(result.get("data"), dict):
                val = result["data"].get("value")
                if val not in (None, "", 0, "0") and str(val) == str(offer_id):
                    already = True
            if claim_ok:
                if already:
                    already_n += 1
                    tag = "ALREADY"
                else:
                    ok_n += 1
                    tag = "CLAIMED"
                code = result.get("coupon_code") or ""
                code_bit = f" · `{code}`" if code else ""
                lines.append(f"{i}. ✅ *{tag}* uid=`{uid}` {mobile}{code_bit}")
            else:
                fail_n += 1
                err = str(result.get("msg") or result.get("data") or "fail")[:140]
                lines.append(f"{i}. ❌ uid=`{uid}` {mobile}\n    `{err}`")
            try:
                await record_claim(
                    update, day, offer_id, f"BULK:{label}", claim_ok,
                    "" if claim_ok else str(result.get("msg") or "")[:200],
                )
            except Exception:
                pass
        except Exception as e:
            fail_n += 1
            lines.append(f"{i}. ❌ uid=`{uid}` error: `{str(e)[:90]}`")
            log.exception("bulk item failed")

        await asyncio.sleep(1.1)
        if i % 2 == 0 or i == len(blobs):
            try:
                await msg.edit_text(
                    f"📦 Progress `{i}/{len(blobs)}`\n"
                    f"✅ {ok_n}  ♻️ {already_n}  ❌ {fail_n}",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    lines.append("")
    lines.append(f"✅ Claimed: `{ok_n}`   ♻️ Already: `{already_n}`   ❌ Fail: `{fail_n}`")
    lines.append("Swiggy app → Coupons / Free Cash mein har account verify karo.")
    report = "\n".join(lines)
    try:
        if len(report) > 3900:
            parts = [report[j:j + 3900] for j in range(0, len(report), 3900)]
            await msg.edit_text(parts[0], parse_mode="Markdown")
            for p in parts[1:]:
                await update.message.reply_text(p, parse_mode="Markdown")
        else:
            await msg.edit_text(report, parse_mode="Markdown")
    except Exception:
        try:
            await update.message.reply_text(report[:4000])
        except Exception:
            pass
    return True


async def pujo_cash_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo (🔐 Login / 📤 Session)",
                                        reply_markup=main_keyboard())
        return
    touch_session(chat_id)

    msg = await update.message.reply_text(
        "✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n🪔 *SHRESTHO PUJO*\n✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n"
        "⏳ Step 1: Pandal list fetch\n"
        "⏳ Step 2: Auto vote (5x)\n"
        "⏳ Step 3: Invite claim\n"
        "⏳ Step 4: Notify server\n\n"
        "Please wait...",
        parse_mode="Markdown")

    async def _cb(text):
        try:
            await msg.edit_text(
                "✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n🪔 *SHRESTHO PUJO*\n✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n" + text,
                parse_mode="Markdown")
        except Exception:
            pass

    lat = float(session.get("lat") or PUJO_DEFAULT_LAT)
    lng = float(session.get("lng") or PUJO_DEFAULT_LNG)

    result = await pujo_full_claim(session, lat, lng, progress_cb=_cb)

    if not result.get("session_ok"):
        await msg.edit_text("❌ Session invalid — login again.")
        return

    vote_ok = result.get("vote_ok", 0)
    vote_fail = result.get("vote_fail", 0)
    invite_ok = result.get("invite_ok")
    notify_ok = result.get("notify_ok")
    pandals = result.get("pandals_found", 0)

    earned_est = min(vote_ok * PUJO_VOTE_AMOUNT, PUJO_VOTE_MAX) + \
                 (PUJO_INVITE_MAX if invite_ok else 0)

    lines = [
        "✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦",
        "🪔 *SHRESTHO PUJO — DONE*",
        "✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦",
        "",
        f"🏛 Pandals found : `{pandals}`",
        f"🗳 Votes OK     : `{vote_ok}`  (❌ {vote_fail})",
        f"📨 Invite claim : {'✅' if invite_ok else '❌'}",
        f"🔔 Notify       : {'✅' if notify_ok else '❌'}",
        "",
        f"💰 *Estimated Cash: ₹{earned_est}*",
        f"   (cap ₹{PUJO_TOTAL_MAX} per user)",
        "",
    ]

    if result.get("vote_details"):
        lines.append("*Votes:*")
        for v in result["vote_details"][:5]:
            if v.get("ok"):
                lines.append(f"  ✅ {v['pandal'][:28]}")
            else:
                lines.append(f"  ❌ {v['pandal'][:28]} — {v.get('err','')[:40]}")
        lines.append("")

    if invite_ok:
        lines.append("🎉 *Invite bonus claimed*")
    elif result.get("invite_data"):
        lines.append(f"⚠️ Invite: `{str(result['invite_data'])[:120]}`")

    if not vote_ok and not invite_ok and not notify_ok:
        lines.append("\n❌ _Koi bhi claim success nahi hua. Campaign ID / endpoint "
                     "change ho sakta hai. App mein check karo._")

    lines.append("\nSwiggy app → Coupons / Free Cash mein verify karo.")
    lines.append("✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦")

    try:
        await record_claim(update, 0, 0, "Shrestho Pujo Auto", vote_ok > 0 or invite_ok,
                           "" if (vote_ok > 0 or invite_ok) else str(result.get("errors")))
    except Exception:
        pass

    await msg.edit_text("\n".join(lines), parse_mode="Markdown")


async def pujo_invite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo.", reply_markup=main_keyboard())
        return
    touch_session(chat_id)
    msg = await update.message.reply_text("📨 Claiming invite bonus...")
    lat = float(session.get("lat") or PUJO_DEFAULT_LAT)
    lng = float(session.get("lng") or PUJO_DEFAULT_LNG)
    r = await _pujo_invite_once(session, lat, lng)
    if r.get("ok"):
        await msg.edit_text(
            "✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n✅ *INVITE BONUS CLAIMED*\n"
            "💰 ₹50 Free Cash (if eligible)\n✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦",
            parse_mode="Markdown")
    else:
        await msg.edit_text(
            f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n⚠️ *Invite claim failed*\n\n`{r.get('msg') or r.get('data')}`\n"
            "✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦", parse_mode="Markdown")


async def pujo_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo.", reply_markup=main_keyboard())
        return
    st = await _pujo_get_status(session)
    await update.message.reply_text(
        "🪔 *Shrestho Pujo Status*\n\n"
        f"`{json.dumps(st.get('data') or {}, indent=2)[:1500]}`",
        parse_mode="Markdown")


async def login_method_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if query.data == "login_json":
        context.user_data.pop("awaiting_phone", None)
        context.user_data["awaiting_session"] = True
        await query.edit_message_text("📄 *JSON Session selected.*\n\n"
                                      "Ab apna session JSON paste karo.\n"
                                      "Example: `{\"token\":\"...\",\"tid\":\"...\"}`", parse_mode="Markdown")
    elif query.data == "login_phone":
        context.user_data.pop("awaiting_session", None)
        context.user_data["awaiting_phone"] = True
        await query.edit_message_text("📱 *Mobile + OTP selected.*\n\n"
                                      "10 digit mobile number bhejo.\nExample: `9876543210`",
                                      parse_mode="Markdown")


async def force_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer("Checking membership…")
    except Exception:
        pass
    user = update.effective_user
    if not user:
        return
    missing = await _force_join_missing(user.id, context)
    if missing:
        try:
            await query.edit_message_text(
                "❌ *Not verified yet.*\n\nJoin all required channel/group(s), then tap "
                "*I've Joined — Verify* again.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    *[[InlineKeyboardButton(f"📢 Join {ch}", url=url)] for ch, url in missing if url],
                    [InlineKeyboardButton("🔄 Verify Again", callback_data="fj_check")]]))
        except Exception:
            log.exception("Force join callback UI update failed")
        return
    await record_user_activity(update, verified=True)
    try:
        await query.edit_message_text("✅ *Membership verified!\n\nBot is ready.*", parse_mode="Markdown")
    except Exception:
        pass
    try:
        await context.bot.send_message(chat_id=update.effective_chat.id,
                                       text="✅ Verification complete. Ab bot ke buttons use karo.",
                                       reply_markup=main_keyboard())
    except Exception:
        log.exception("Unable to send unlocked keyboard")


async def night_cash_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    await query.answer()
    if data == "ln_cancel":
        await query.edit_message_text("Cancelled.")
        return
    if data.startswith("ln_pick_"):
        parts = data.split("_")
        day = int(parts[2]); offer_id = int(parts[3])
        offer_name = LN_OFFERS.get(offer_id, f"Offer {offer_id}")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Confirm: {offer_name}", callback_data=f"ln_confirm_{day}_{offer_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"ln_back_{day}")]])
        await query.edit_message_text(
            f"🌙 Confirm karo:\n\nDay: *{day}*\nOffer: *{offer_name}*\n\n"
            "Confirm ke baad claim ho jayega.", parse_mode="Markdown", reply_markup=kb)
        return
    if data.startswith("ln_back_"):
        day = int(data.split("_")[2])
        rows = []
        for oid, name in LN_OFFERS.items():
            rows.append([InlineKeyboardButton(f"{oid}. {name}", callback_data=f"ln_pick_{day}_{oid}")])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="ln_cancel")])
        await query.edit_message_text(f"🌙 *Night Cash — Day {day}*\n\nEk offer select karo:",
                                      parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
        return
    if data.startswith("ln_confirm_"):
        if not await check_force_join(update, context):
            return
        parts = data.split("_")
        day = int(parts[2]); offer_id = int(parts[3])
        offer_name = LN_OFFERS.get(offer_id, f"Offer {offer_id}")
        chat_id = update.effective_chat.id
        session = get_session(chat_id)
        if not session:
            await query.edit_message_text("❌ Session lost. Login again."); return
        try:
            await query.edit_message_text(
                f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n⏳ *CLAIMING*\n🔗 {offer_name}",
                parse_mode="Markdown")
        except Exception:
            pass
        result = await ln_claim_offer(session, day, offer_id)
        claim_ok = bool(result.get("ok"))
        claim_error = "" if claim_ok else str(result.get("data") or result.get("msg") or result)
        await record_claim(update, day, offer_id, offer_name, claim_ok, claim_error)
        if claim_ok:
            await query.edit_message_text(
                f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n✅ *COMPLETE* — Claim successful\n\n"
                f"Day: {day}\nOffer: *{offer_name}*\n\n"
                "Swiggy app mein check karo (Late Night deals).", parse_mode="Markdown")
        else:
            await query.edit_message_text(
                f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n❌ *FAIL* — Claim failed\n\n`{claim_error or 'Unknown error'}`",
                parse_mode="Markdown")
        return


async def breakfast_cash_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "bf_cancel":
        await query.edit_message_text("Cancelled."); return
    if data.startswith("bf_pick_"):
        parts = data.split("_")
        day = int(parts[2]); offer_id = int(parts[3])
        offer_name = BF_OFFERS.get(offer_id, f"Offer {offer_id}")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Confirm lock: {offer_name}", callback_data=f"bf_confirm_{day}_{offer_id}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"bf_back_{day}")]])
        await query.edit_message_text(
            f"🍳 *Confirm lock*\n\nDay: *{day}*\nOffer: *{offer_name}*\n\n"
            "Confirm ke baad offer lock + claim ho jayega.", parse_mode="Markdown", reply_markup=kb)
        return
    if data.startswith("bf_back_"):
        day = int(data.split("_")[2])
        rows = []
        for oid, name in BF_OFFERS.items():
            rows.append([InlineKeyboardButton(f"{oid}. {name}", callback_data=f"bf_pick_{day}_{oid}")])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="bf_cancel")])
        await query.edit_message_text(f"🍳 *Breakfast Cash — Day {day}*\n\nEk offer select karo:",
                                      parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
        return
    if data.startswith("bf_confirm_"):
        if not await check_force_join(update, context):
            return
        parts = data.split("_")
        day = int(parts[2]); offer_id = int(parts[3])
        offer_name = BF_OFFERS.get(offer_id, f"Offer {offer_id}")
        chat_id = update.effective_chat.id
        session = get_session(chat_id)
        if not session:
            await query.edit_message_text("❌ Session lost. Login again."); return
        try:
            await query.edit_message_text(
                f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n⏳ *LOCKING*\n🔗 {offer_name}",
                parse_mode="Markdown")
        except Exception:
            pass
        result = await bf_claim_offer(session, day, offer_id)
        claim_ok = bool(result.get("ok"))
        claim_error = "" if claim_ok else str(result.get("data") or result.get("msg") or result)
        await record_claim(update, day, offer_id, f"BF:{offer_name}", claim_ok, claim_error)
        if claim_ok:
            await query.edit_message_text(
                f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n✅ *COMPLETE* — Offer locked\n\n"
                f"Day: {day}\nOffer: *{offer_name}*\n\nSwiggy app → Breakfast pe apply hoga.",
                parse_mode="Markdown")
        else:
            await query.edit_message_text(
                f"✦•┈┈┈•✦ 𓆩××𓆪 ✦•┈┈┈•✦\n\n❌ *FAIL* — Lock failed\n\n`{claim_error or 'unknown'}`",
                parse_mode="Markdown")
        return


def _admin_user_stats_text() -> str:
    users = BOT_STATS.get("users", {})
    now = datetime.now(timezone.utc)
    active_24h = 0
    verified = 0
    for entry in users.values():
        if entry.get("verified"):
            verified += 1
        try:
            last = datetime.fromisoformat(entry.get("last_seen", ""))
            if now - last <= timedelta(hours=24):
                active_24h += 1
        except Exception:
            pass
    return ("👥 *Bot Analytics*\n\n"
            f"Unique users: `{len(users)}`\n"
            f"Verified users: `{verified}`\n"
            f"Active (24h): `{active_24h}`\n"
            f"Claim attempts: `{BOT_STATS.get('total_claim_attempts', 0)}`\n"
            f"Successful claims: `{BOT_STATS.get('successful_claims', 0)}`\n"
            f"Failed claims: `{BOT_STATS.get('failed_claims', 0)}`")


def _admin_claim_history_text(limit: int = 15) -> str:
    history = list(BOT_STATS.get("claims", []))[-limit:]
    if not history:
        return "📜 *Claim History*\n\nNo claim records yet."
    lines = ["📜 *Latest Claim History*", ""]
    for item in reversed(history):
        status = "✅" if item.get("status") == "success" else "❌"
        name = item.get("username") or item.get("name") or str(item.get("telegram_id"))
        lines.append(f"{status} `{item.get('time','')[:19]}` — {name}\n"
                     f"   Day {item.get('day')} • {item.get('offer')} • TG `{item.get('telegram_id')}`")
    return "\n".join(lines)







async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        # silent for non-admin — no UI leak
        try:
            await update.message.reply_text("🔒 This command is restricted.")
        except Exception:
            pass
        return
    fj = "ON ✅" if BOT_SETTINGS.get("force_join_enabled") else "OFF ❌"
    chs = len(BOT_SETTINGS.get("force_channels") or [])
    locked = len(BOT_SETTINGS.get("locked_buttons") or [])
    text = (
        "✦•┈┈┈┈┈┈┈┈┈┈┈┈┈┈•✦\n"
        "👑 *ADMIN CONTROL*\n"
        "✦•┈┈┈┈┈┈┈┈┈┈┈┈┈┈•✦\n\n"
        f"Force Join : *{fj}*\n"
        f"Channels   : `{chs}`\n"
        f"Locked btns: `{locked}`\n\n"
        "Use panel buttons below.\n"
        "Only you can see this menu."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=admin_keyboard())


async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram ID: `{update.effective_user.id}`", parse_mode="Markdown")


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo (🔐 Login / 📤 Session)\n"
                                        "Session expires after configured TTL.",
                                        reply_markup=main_keyboard())
        return
    touch_session(chat_id)
    msg = await update.message.reply_text("⚡ Starting 10 links join...\n(full ~Rs.100 target)\n\nPlease wait...")
    links = normalize_links(shuffled_default_links())

    async def progress(text):
        try:
            await msg.edit_text(text, parse_mode="Markdown")
        except Exception:
            try:
                await msg.edit_text(text)
            except Exception:
                pass

    async def history_callback(link, ok, error):
        await record_join_history(update, link, ok, error)

    try:
        lock = await get_user_job_lock(chat_id)
        if lock.locked():
            await msg.edit_text("⏳ Your previous job is still running. Please wait..."); return
        async with lock:
            report = await run_full_join(session, links, progress_callback=progress, history_callback=history_callback)
        if len(report) > 4000:
            parts = [report[i:i+4000] for i in range(0, len(report), 4000)]
            await msg.edit_text(f"```\n{parts[0]}\n```", parse_mode="Markdown")
            for part in parts[1:]:
                await update.message.reply_text(f"```\n{part}\n```", parse_mode="Markdown")
        else:
            await msg.edit_text(f"```\n{report}\n```", parse_mode="Markdown")
    except Exception as e:
        log.exception("run_cmd failed")
        await msg.edit_text(f"❌ Error:\n{e}")


async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo.", parse_mode="Markdown"); return
    if not context.args:
        await update.message.reply_text(
            "Usage:\n`/join https://r.swiggy.com/cashloot/ydiHi75-xxxxx`\n\n"
            "Ya default links ke liye `/run` use karo.", parse_mode="Markdown")
        return
    url = " ".join(context.args).strip()
    campaign_id, referrer_id = parse_cashloot_link(url)
    if not campaign_id or not referrer_id:
        await update.message.reply_text("❌ Invalid Cash Loot link."); return
    links = normalize_links([("custom", url)])
    msg = await update.message.reply_text(f"🔗 Joining `{referrer_id}`...", parse_mode="Markdown")

    async def progress(text):
        try:
            await msg.edit_text(text, parse_mode="Markdown")
        except Exception:
            pass

    async def history_callback(link, ok, error):
        await record_join_history(update, link, ok, error)

    try:
        lock = await get_user_job_lock(chat_id)
        if lock.locked():
            await msg.edit_text("⏳ Your previous job is still running. Please wait..."); return
        async with lock:
            report = await run_full_join(session, links, progress_callback=progress, history_callback=history_callback)
        await msg.edit_text(f"```\n{report}\n```", parse_mode="Markdown")
    except Exception:
        log.exception("join_cmd failed")
        await msg.edit_text("❌ Temporary error while processing the request.")


async def joinmany_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo.")
        return ConversationHandler.END
    await update.message.reply_text("Ab text bhejo jisme Cash Loot links hon.\n"
                                    "(chat export / multiple links)\nDuplicates auto-remove ho jayenge.")
    return WAITING_LINKS


async def links_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Session lost. Login again.")
        return ConversationHandler.END
    text = update.message.text or ""
    found = extract_cashloot_links(text)
    if not found:
        await update.message.reply_text("❌ Koi valid Cash Loot link nahi mila.")
        return ConversationHandler.END
    pairs = [(os.path.basename(url.rstrip("/")), url) for url in found]
    links = normalize_links(pairs)
    msg = await update.message.reply_text(f"🔗 Found {len(links)} unique links. Joining...")

    async def progress(text):
        try:
            await msg.edit_text(text, parse_mode="Markdown")
        except Exception:
            pass

    async def history_callback(link, ok, error):
        await record_join_history(update, link, ok, error)

    try:
        lock = await get_user_job_lock(chat_id)
        if lock.locked():
            await msg.edit_text("⏳ Your previous job is still running. Please wait...")
            return ConversationHandler.END
        async with lock:
            report = await run_full_join(session, links, progress_callback=progress, history_callback=history_callback)
        if len(report) > 4000:
            parts = [report[i:i+4000] for i in range(0, len(report), 4000)]
            await msg.edit_text(f"```\n{parts[0]}\n```", parse_mode="Markdown")
            for part in parts[1:]:
                await update.message.reply_text(f"```\n{part}\n```", parse_mode="Markdown")
        else:
            await msg.edit_text(f"```\n{report}\n```", parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Error:\n{e}")
    return ConversationHandler.END


async def invite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return
    chat_id = update.effective_chat.id
    session = get_session(chat_id)
    if not session:
        await update.message.reply_text("❌ Pehle login karo."); return
    target = 10
    if context.args:
        try:
            target = max(1, int(context.args[0]))
        except ValueError:
            pass
    msg = await update.message.reply_text(
        f"📡 Invite mode starting...\nTarget: `{target}` associations\n"
        f"Areas: Delhi + metros\n\nPlease wait (yeh thoda time lega)...")
    campaign_id = DEFAULT_CAMPAIGN_ID
    cities = INVITE_CITIES
    joined = 0
    tried: set = set()
    report_lines = []
    jar = aiohttp.CookieJar(unsafe=True)
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(cookie_jar=jar, connector=connector) as http:
        before = await fetch_ledger(session, campaign_id, http)
        for city, lat, lng in cities:
            if joined >= target:
                break
            found = await discover_users(session, (lat, lng), campaign_id, http, list(tried))
            if not found["ok"]:
                continue
            for user in found["users"]:
                if joined >= target:
                    break
                user_id = str(user.get("userId") or "")
                name = str(user.get("userName") or user_id or "--")
                if not user_id or user_id in tried:
                    continue
                tried.add(user_id)
                if (user.get("status") or {}).get("isAssociated"):
                    continue
                result = await invite_user(session, campaign_id, user, (lat, lng), http)
                if result.get("joined"):
                    joined += 1
                    report_lines.append(f"✅ {name}")
                    try:
                        await msg.edit_text(f"📡 Progress: {joined}/{target}\nLast: {name}\nTried: {len(tried)}")
                    except Exception:
                        pass
                await asyncio.sleep(0.5)
        if joined:
            await asyncio.sleep(CASHLOOT_LEDGER_WAIT_SECONDS)
        after = await fetch_ledger(session, campaign_id, http)
    gained = 0
    if before.get("ok") and after.get("ok"):
        gained = after["earned"] - before["earned"]
    text = (f"*Invite Done*\n\nAssociated : `{joined}`\nUsers tried: `{len(tried)}`\n"
            f"Cash total : Rs.`{_money(after.get('earned', 0) if after.get('ok') else 0)}`\n"
            f"This run   : +Rs.`{_money(gained)}`\n\n")
    if report_lines:
        text += "Joined:\n" + "\n".join(report_lines[:15])
    await msg.edit_text(text, parse_mode="Markdown")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_pending_otp.pop(chat_id, None)
    context.user_data.pop("awaiting_paw_photo", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if (not BOT_TOKEN or ":" not in BOT_TOKEN or BOT_TOKEN == "PASTE_NEW_BOT_TOKEN_HERE"):
        print("ERROR: Set BOT_TOKEN environment variable to your new Telegram bot token.")
        sys.exit(1)

    app = (Application.builder().token(BOT_TOKEN)
           .concurrent_updates(MAX_CONCURRENT_UPDATES).build())

    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_cmd)],
        states={WAITING_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp_received)]},
        fallbacks=[CommandHandler("cancel", cancel_cmd)], allow_reentry=True)
    session_conv = ConversationHandler(
        entry_points=[CommandHandler("session", session_cmd)],
        states={WAITING_SESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, session_received)]},
        fallbacks=[CommandHandler("cancel", cancel_cmd)], allow_reentry=True)
    links_conv = ConversationHandler(
        entry_points=[CommandHandler("joinmany", joinmany_cmd)],
        states={WAITING_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, links_received)]},
        fallbacks=[CommandHandler("cancel", cancel_cmd)], allow_reentry=True)

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(login_conv)
    app.add_handler(session_conv)
    app.add_handler(links_conv)
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("run", run_cmd))
    app.add_handler(CommandHandler("join", join_cmd))
    app.add_handler(CommandHandler("invite", invite_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("nightcash", night_cash_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("pyo", pyo_cash_cmd))
    app.add_handler(CommandHandler("karwachauth", kc_cash_cmd))
    app.add_handler(CommandHandler("bulk", bulk_claim_cmd))
    app.add_handler(CommandHandler("pujo", pujo_cash_cmd))
    app.add_handler(CommandHandler("pujoinvite", pujo_invite_cmd))
    app.add_handler(CommandHandler("pujostatus", pujo_status_cmd))

    app.add_handler(MessageHandler(
        filters.Regex(
            r"^(⚡ RUN \(10 links\)|Late Night Cash|🍳 Breakfast Cash|Breakfast Cash|"
            r"💰 NEW 50 FC|NEW 50 FC|"
            r"🪔 Karwa Chauth|Karwa Chauth|Karwachauth|"
            r"📦 Bulk Claim|Bulk Claim|"
            r"📊 Status|🔐 Login|📤 Session|🚪 Logout|📜 History|"
            r"🔙 Back|➕ Add Channel|➖ Remove Channel|📋 List Channels|"
            r"👥 Users & Claims|📜 Claim History|"
            r"🔧 Force Join:.*|🎛 Lock/Unlock Offers|"
            r"🔒 RUN|🔓 RUN|🔒 Night|🔓 Night|🔒 Breakfast|🔓 Breakfast|"
            r"🔒 50 FC|🔓 50 FC|"
            r")$"),
        button_handler))

    app.add_handler(CallbackQueryHandler(login_method_callback, pattern=r"^login_(json|phone)$"))
    app.add_handler(CallbackQueryHandler(force_join_callback, pattern=r"^fj_check$"))
    app.add_handler(CallbackQueryHandler(night_cash_callback, pattern=r"^ln_"))
    app.add_handler(CallbackQueryHandler(breakfast_cash_callback, pattern=r"^bf_"))
    app.add_handler(CallbackQueryHandler(pyo_cash_callback, pattern=r"^pyo_"))
    app.add_handler(CallbackQueryHandler(kc_cash_callback, pattern=r"^kc_"))
    app.add_handler(CallbackQueryHandler(bulk_callback, pattern=r"^bulk_"))

    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))

    app.add_handler(MessageHandler(filters.PHOTO, paw_photo_received), group=0)

    async def text_catcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        text = (update.message.text or "").strip()

        if context.user_data.get("awaiting_bulk"):
            handled = await bulk_sessions_received(update, context)
            if handled:
                return
        if text in (
            "⚡ RUN (10 links)", "Late Night Cash", "🍳 Breakfast Cash", "Breakfast Cash",
            "💰 NEW 50 FC", "NEW 50 FC",
            "🪔 Karwa Chauth", "Karwa Chauth", "Karwachauth",
            
            "📦 Bulk Claim", "Bulk Claim",
            "📊 Status", "🔐 Login", "📤 Session", "🚪 Logout", "📜 History",
            "🔙 Back", "➕ Add Channel", "➖ Remove Channel", "📋 List Channels",
        ) or text.startswith("🔧 Force Join:"):
            return

        if context.user_data.get("awaiting_channel") and is_admin(update.effective_user.id):
            action = context.user_data.pop("awaiting_channel")
            ch = text.strip()
            if not ch.startswith("@") and not ch.startswith("-"):
                ch = "@" + ch
            channels = BOT_SETTINGS.setdefault("force_channels", [])
            if action == "add":
                if ch not in channels:
                    channels.append(ch); save_settings(BOT_SETTINGS)
                    await update.message.reply_text(f"✅ Added `{ch}`", parse_mode="Markdown", reply_markup=admin_keyboard())
                else:
                    await update.message.reply_text("Already in list.", reply_markup=admin_keyboard())
            elif action == "remove":
                if ch in channels:
                    channels.remove(ch); save_settings(BOT_SETTINGS)
                    await update.message.reply_text(f"✅ Removed `{ch}`", parse_mode="Markdown", reply_markup=admin_keyboard())
                else:
                    await update.message.reply_text("Not found.", reply_markup=admin_keyboard())
            return

        if chat_id in user_pending_otp:
            if not await check_force_join(update, context):
                return
            if re.match(r"^\d{4,6}$", text):
                msg = await update.message.reply_text("🔄 Verifying OTP...")
                try:
                    result = await verify_otp(user_pending_otp[chat_id], text)
                except Exception as e:
                    log.exception("verify_otp error")
                    await safe_edit(msg, f"❌ OTP verify error:\n{e}"); return
                if not result.get("ok"):
                    await safe_edit(msg, f"❌ OTP failed:\n{result.get('msg', 'unknown')}"); return
                session = result["session"]
                try:
                    save_user_session(chat_id, session)
                except Exception as e:
                    log.exception("save session failed")
                    await safe_edit(msg, f"❌ Session save failed:\n{e}"); return
                user_pending_otp.pop(chat_id, None)
                try:
                    await record_user_activity(update, verified=True)
                except Exception:
                    pass
                await safe_edit(msg, f"✅ *Login successful!*\n(auto-logout 5 min)\n\n"
                                     f"Customer ID: `{session.get('customerId')}`\n"
                                     f"Mobile: `{session.get('mobile')}`\n"
                                     f"User ID: `{_account_user_id(session)}`\n\n"
                                     "Ab ⚡ *RUN* ya *Late Night Cash* use karo",
                                reply_markup=main_keyboard())
            else:
                await update.message.reply_text("❌ Invalid OTP. 4-6 digits bhejo.")
            return

        if context.user_data.get("awaiting_phone"):
            if not await check_force_join(update, context):
                context.user_data.pop("awaiting_phone", None); return
            if re.fullmatch(r"\d{10}", text):
                context.user_data.pop("awaiting_phone", None)
                await _do_login_phone(update, context, text)
            else:
                await update.message.reply_text("❌ 10 digit phone bhejo. Example: `9876543210`", parse_mode="Markdown")
            return

        if context.user_data.get("awaiting_session"):
            if not await check_force_join(update, context):
                context.user_data.pop("awaiting_session", None); return
            context.user_data.pop("awaiting_session", None)
            try:
                session = parse_session_string(text)
                user_id = _account_user_id(session)
                if not user_id:
                    uid = _userid_from_tid(session.get("tid", ""))
                    if uid:
                        session["userid"] = uid; user_id = uid
                save_user_session(chat_id, session)
                await update.message.reply_text(
                    f"✅ Session saved!\n(session expires after configured TTL)\n\n"
                    f"User ID: `{user_id or 'unknown'}`\n"
                    f"Mobile: `{session.get('mobile') or session.get('phone') or 'N/A'}`\n\n"
                    "Ab ⚡ *RUN* ya *Late Night Cash* use karo",
                    parse_mode="Markdown", reply_markup=main_keyboard())
            except Exception as e:
                await update.message.reply_text(f"❌ Invalid session:\n{e}\n\n"
                                                "Phir se 📤 Session dabao aur sahi JSON bhejo.")
            return

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_catcher), group=1)

    async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
        err = context.error
        log.exception("Unhandled Telegram update error", exc_info=err)
        try:
            if isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text("⚠️ Temporary bot error. Please retry in a few seconds.")
        except Exception:
            pass

    app.add_error_handler(global_error_handler)
    if app.job_queue:
        app.job_queue.run_repeating(session_expiry_watcher, interval=30, first=30)

    print("🤖 Swiggy Cash Loot Telegram Bot starting...")
    print(f"Token: {BOT_TOKEN[:20]}...")
    print(f"Admin ID: {ADMIN_IDS}")
    print("Buttons: RUN / Night / Breakfast / NEW50FC / Karwa / Bulk / Status / Login")
    # drop_pending_updates + close old webhook avoids Conflict
    # when a previous process / host still holds getUpdates
    async def _post_init(application: Application):
        try:
            await application.bot.delete_webhook(drop_pending_updates=True)
            print("✅ Webhook cleared / pending updates dropped")
        except Exception as e:
            print(f"⚠️ delete_webhook: {e}")

    app.post_init = _post_init
    try:
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            close_loop=False,
        )
    except Exception as e:
        if "Conflict" in type(e).__name__ or "Conflict" in str(e):
            print("❌ Conflict: another bot instance is already polling this token.")
            print("   Stop the other process (old host / duplicate worker), wait 5–10s, restart.")
        raise


if __name__ == "__main__":
    main()