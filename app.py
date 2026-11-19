import base64
from concurrent.futures import ThreadPoolExecutor
import csv
from email.message import EmailMessage
import hashlib
import io
import json
import mimetypes
import os
import random
import re
import smtplib
import sqlite3
import threading
import time
from urllib.parse import quote, quote_plus
import xml.etree.ElementTree as ET

from dotenv import load_dotenv
from google import genai
from google.genai import types
from gtts import gTTS
import requests
import streamlit as st

# 1. Load Environment Variables & Cloud Secrets Safely
load_dotenv()


def _get_secret_or_env(key: str, default: str = "") -> str:
    env_val = os.getenv(key)
    if env_val:
        return env_val.strip()
    try:
        if key in st.secrets and st.secrets[key]:
            return str(st.secrets[key]).strip()
    except Exception:
        pass
    return default


API_KEY = _get_secret_or_env("GEMINI_API_KEY", "")
SMTP_HOST = _get_secret_or_env("SMTP_HOST", "")
SMTP_PORT = int(_get_secret_or_env("SMTP_PORT", "587") or "587")
SMTP_USER = _get_secret_or_env("SMTP_USER", "")
SMTP_PASS = _get_secret_or_env("SMTP_PASS", "")
SMTP_FROM = _get_secret_or_env("SMTP_FROM", SMTP_USER)
MAIL_RELAY_URL = _get_secret_or_env("MAIL_RELAY_URL", "")

_genai_client: genai.Client | None = None


def get_genai_client() -> genai.Client:
    global _genai_client
    key = API_KEY or _get_secret_or_env("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is missing. Add it to .env locally or to Streamlit Cloud Secrets.")
    if _genai_client is None:
        _genai_client = genai.Client(api_key=key)
    return _genai_client


# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "sister_ai.db")
BILLS_DIR = os.path.join(BASE_DIR, "bills")
os.makedirs(BILLS_DIR, exist_ok=True)

# 2. Page Configuration
st.set_page_config(
    page_title="SisterAI | Your Personal Medical Assistant",
    page_icon="🩺",
    layout="wide",
)

# 3. Hospital Departments & Enterprise Staff Directory
HOSPITAL_DEPARTMENTS = [
    "Radiology & Medical Imaging",
    "Cardiology",
    "Ophthalmology",
    "Emergency & Trauma (ER)",
    "Outpatient Medicine (OPD)",
    "Neurology & Neurosurgery",
    "Orthopedics",
    "Pediatrics & Neonatology",
    "Oncology & Radiotherapy",
    "Pathology & Laboratory Medicine",
    "Pharmacy & Pharmacology",
    "Billing, Insurance & RCM",
    "Central Reception & Registration",
]

PAYMENT_MODES = [
    "Online Payment (UPI)",
    "Card Payment (Debit / Credit)",
    "Cash (Offline)",
]

PROCEDURE_TARIFF_INR = {
    "mri": 6500,
    "ct": 3800,
    "pet": 14500,
    "ultrasound": 1400,
    "usg": 1400,
    "x-ray": 700,
    "xray": 700,
    "mammography": 2200,
    "echo": 2000,
    "ecg": 500,
    "oct": 1800,
    "fundus": 900,
    "eeg": 2500,
    "cbc": 450,
    "biopsy": 4200,
    "chemo": 9500,
    "radiotherapy": 8500,
}


def _hash_password(raw_pw: str) -> str:
    return hashlib.sha256(raw_pw.strip().encode("utf-8")).hexdigest()


def estimate_procedure_amount(procedure_name: str) -> int:
    low = (procedure_name or "").lower()
    for key, amt in PROCEDURE_TARIFF_INR.items():
        if key in low:
            return amt
    return 1800


def generate_uhid(appt_date: str = "2026-11-19") -> str:
    clean_d = re.sub(r"[^0-9]", "", appt_date) or "20261119"
    return f"UHID-{clean_d[:8]}-{random.randint(1000, 9999)}"


def _match_department_name(raw_dept: str) -> str | None:
    q = (raw_dept or "").strip().lower()
    if not q:
        return None
    for d in HOSPITAL_DEPARTMENTS:
        if q == d.lower() or q in d.lower() or d.lower() in q:
            return d
    return None


# 4. Pure-Python Valid PDF Bill Generator (ASCII-safe)
def _pdf_escape(text: str) -> str:
    ascii_safe = str(text or "").encode("ascii", errors="replace").decode("ascii")
    return ascii_safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def create_pdf_bill_file(bill_record: dict) -> tuple[str, bytes]:
    """Generates a valid PDF invoice/bill on disk and returns (file_path, pdf_bytes)."""
    lines = [
        "SISTERAI HOSPITAL - CLINICAL & DIAGNOSTIC BILL / RECEIPT",
        "-----------------------------------------------------------------------",
        f"Bill No: {bill_record['bill_no']}          Date Issued: {bill_record['issued_date']}",
        f"Department: {bill_record['department']}     Issued By: {bill_record['staff_name']}",
        f"Unique ID (UHID): {bill_record['unique_id']}   Appt ID: #{bill_record['appointment_id']}",
        f"Patient Name: {bill_record['patient_name']}     Age: {bill_record['age']} yrs",
        f"Visit Type: {bill_record['patient_type']}          Status: {bill_record['status']}",
        f"Phone: {bill_record['phone']}                 Email: {bill_record['email']}",
        f"Address: {bill_record['address']}",
        f"Referring Doctor: {bill_record['referring_doctor']}",
        f"Prescription Ref: {bill_record['prescription']}",
        "-----------------------------------------------------------------------",
        f"Scan / Procedure: {bill_record['scan_procedure']}",
        f"Clinical Reason: {bill_record['reason']}",
        f"Scheduled Slot: {bill_record['appt_date']} at {bill_record['appt_time']}",
        "-----------------------------------------------------------------------",
        f"Amount: INR {bill_record['amount_inr']}",
        f"Payment Mode: {bill_record['payment_mode']}",
        f"Payment Status: {bill_record['payment_status']}",
        f"Payment Note: {bill_record['payment_note']}",
    ]
    if bill_record.get("payment_link"):
        lines.append(f"Payment Link / Ref: {bill_record['payment_link']}")
    lines.extend([
        "-----------------------------------------------------------------------",
        "Generated by SisterAI - Personal Medical Assistant",
    ])

    stream_cmds = ["BT", "/F1 10 Tf", "45 760 Td", "15 TL"]
    for idx, line in enumerate(lines):
        if idx == 0:
            stream_cmds.append("/F1 13 Tf")
            stream_cmds.append(f"({_pdf_escape(line)}) Tj")
            stream_cmds.append("/F1 10 Tf")
        else:
            stream_cmds.append(f"T* ({_pdf_escape(line)}) Tj")
    stream_cmds.append("ET")
    content_stream = "\n".join(stream_cmds).encode("ascii", errors="replace")

    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
        ),
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        (
            f"5 0 obj\n<< /Length {len(content_stream)} >>\nstream\n".encode("ascii")
            + content_stream
            + b"\nendstream\nendobj\n"
        ),
    ]

    pdf_out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf_out))
        pdf_out.extend(obj)

    xref_pos = len(pdf_out)
    pdf_out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf_out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        pdf_out.extend(f"{off:010d} 00000 n \n".encode("ascii"))

    pdf_out.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("ascii")
    )

    pdf_bytes = bytes(pdf_out)
    safe_uhid = re.sub(r"[^A-Za-z0-9_-]", "_", bill_record["unique_id"])
    file_path = os.path.join(BILLS_DIR, f"Bill_{safe_uhid}_Appt{bill_record['appointment_id']}.pdf")
    try:
        with open(file_path, "wb") as f:
            f.write(pdf_bytes)
    except Exception:
        pass

    return file_path, pdf_bytes


# 5. SQLite Database (Staff RBAC, Appointments, Referrals, Ops Incidents, Chat Sessions)
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 20000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _init_database_impl() -> bool:
    """Initializes and migrates all SQLite tables and seeds staff directory."""
    conn = get_db_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS staff_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                full_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                department TEXT NOT NULL,
                role TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0
            )
            """
        )

        default_pw = _hash_password("1234")
        staff_seed = [
            ("soumit", "Soumit Ghosh Chaudhury", default_pw, "Radiology & Medical Imaging", "Enterprise Hospital Admin", 1),
            ("rad_desk", "Ananya Das", default_pw, "Radiology & Medical Imaging", "Radiology Coordinator", 0),
            ("cardio_desk", "Dr. K. Chatterjee", default_pw, "Cardiology", "Cardiology Specialist / Desk", 0),
            ("opto_desk", "Priya Nair", default_pw, "Ophthalmology", "Ophthalmology Coordinator", 0),
            ("er_desk", "Rohan Verma", default_pw, "Emergency & Trauma (ER)", "ER Triage Lead", 0),
            ("opd_desk", "Dr. A. Sen", default_pw, "Outpatient Medicine (OPD)", "OPD Consultant", 0),
            ("neuro_desk", "Meera Menon", default_pw, "Neurology & Neurosurgery", "Neuro Desk Coordinator", 0),
            ("ortho_desk", "Vikram Rao", default_pw, "Orthopedics", "Orthopedics Coordinator", 0),
            ("peds_desk", "Sneha Kulkarni", default_pw, "Pediatrics & Neonatology", "Pediatrics Desk", 0),
            ("onco_desk", "Dr. R. Bose", default_pw, "Oncology & Radiotherapy", "Oncology & RT Lead", 0),
            ("lab_desk", "Arjun Nair", default_pw, "Pathology & Laboratory Medicine", "Lab Operations", 0),
            ("pharm_desk", "Kavita Iyer", default_pw, "Pharmacy & Pharmacology", "Chief Pharmacist", 0),
            ("rcm_desk", "Suresh Gupta", default_pw, "Billing, Insurance & RCM", "RCM & Claims Officer", 0),
            ("front_desk", "Neha Sharma", default_pw, "Central Reception & Registration", "Central Intake Officer", 0),
        ]
        for u in staff_seed:
            conn.execute(
                """
                INSERT OR IGNORE INTO staff_users (username, full_name, password_hash, department, role, is_admin)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                u,
            )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unique_id TEXT NOT NULL,
                patient_name TEXT NOT NULL,
                age INTEGER DEFAULT 30,
                prescription TEXT DEFAULT 'On File / Dept Intake',
                referring_doctor TEXT DEFAULT 'Departmental Referral',
                patient_type TEXT DEFAULT 'Outpatient',
                phone TEXT DEFAULT 'N/A',
                address TEXT DEFAULT 'Not Provided',
                email TEXT DEFAULT 'Not Provided',
                department TEXT NOT NULL,
                provider_or_resource TEXT NOT NULL,
                appt_date TEXT NOT NULL,
                appt_time TEXT NOT NULL,
                scan_procedure TEXT DEFAULT 'Diagnostic Evaluation',
                reason TEXT DEFAULT 'General Consultation',
                status TEXT DEFAULT 'Scheduled',
                payment_mode TEXT DEFAULT 'Online Payment (UPI)',
                payment_status TEXT DEFAULT 'PAID',
                payment_link TEXT DEFAULT '',
                bill_path TEXT DEFAULT '',
                created_by TEXT DEFAULT 'System',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(appointments)").fetchall()}
        migrations = {
            "unique_id": "TEXT DEFAULT 'UHID-LEGACY'",
            "age": "INTEGER DEFAULT 30",
            "prescription": "TEXT DEFAULT 'On File / Dept Intake'",
            "referring_doctor": "TEXT DEFAULT 'Departmental Referral'",
            "patient_type": "TEXT DEFAULT 'Outpatient'",
            "address": "TEXT DEFAULT 'Not Provided'",
            "email": "TEXT DEFAULT 'Not Provided'",
            "scan_procedure": "TEXT DEFAULT 'Diagnostic Evaluation'",
            "payment_mode": "TEXT DEFAULT 'Online Payment (UPI)'",
            "payment_status": "TEXT DEFAULT 'PAID'",
            "payment_link": "TEXT DEFAULT ''",
            "bill_path": "TEXT DEFAULT ''",
            "created_by": "TEXT DEFAULT 'System'",
        }
        for col_name, col_def in migrations.items():
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE appointments ADD COLUMN {col_name} {col_def}")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS department_referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unique_id TEXT NOT NULL,
                patient_name TEXT NOT NULL,
                from_department TEXT NOT NULL,
                to_department TEXT NOT NULL,
                requested_procedure TEXT NOT NULL,
                clinical_notes TEXT DEFAULT '',
                priority TEXT DEFAULT 'Routine',
                status TEXT DEFAULT 'Pending',
                referred_by TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ops_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                department_or_modality TEXT NOT NULL,
                summary TEXT NOT NULL,
                severity TEXT DEFAULT 'Medium',
                estimated_impact TEXT DEFAULT 'Unspecified',
                status TEXT DEFAULT 'Open',
                reported_by TEXT DEFAULT 'System',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        inc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(ops_incidents)").fetchall()}
        if "reported_by" not in inc_cols:
            conn.execute("ALTER TABLE ops_incidents ADD COLUMN reported_by TEXT DEFAULT 'System'")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                owner_username TEXT DEFAULT 'soumit',
                department TEXT DEFAULT 'Radiology & Medical Imaging',
                title TEXT NOT NULL,
                pinned INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        sess_cols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_sessions)").fetchall()}
        if "pinned" not in sess_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN pinned INTEGER DEFAULT 0")
        if "owner_username" not in sess_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN owner_username TEXT DEFAULT 'soumit'")
        if "department" not in sess_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN department TEXT DEFAULT 'Radiology & Medical Imaging'")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                audio_bytes BLOB,
                bill_pdf_bytes BLOB,
                bill_filename TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
            )
            """
        )
        msg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_messages)").fetchall()}
        if "bill_pdf_bytes" not in msg_cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN bill_pdf_bytes BLOB")
        if "bill_filename" not in msg_cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN bill_filename TEXT")

        appt_count = conn.execute("SELECT COUNT(*) AS cnt FROM appointments").fetchone()["cnt"]
        if appt_count == 0:
            seed_appts = [
                ("UHID-20261120-1001", "Ananya Das", 34, "Rx-4401", "Dr. S. Roy", "Outpatient", "9800112233", "Durgapur", "ananya@example.com", "Radiology & Medical Imaging", "CT Scanner 1", "2026-11-20", "09:30", "Contrast CT Chest", "Chest follow-up", "Scheduled", "Online Payment (UPI)", "PAID", "upi://pay?pa=sisterai.hospital@upi&pn=SisterAI&am=3800", "", "Ananya Das"),
                ("UHID-20261120-1002", "Rohan Verma", 54, "Rx-5501", "Dr. K. Chatterjee", "Outpatient", "9800112266", "Bengaluru", "rohan@example.com", "Cardiology", "Echo Lab 1", "2026-11-20", "14:00", "2D Echo & ECG", "Hypertension review", "Scheduled", "Card Payment (Debit / Credit)", "PAID", "https://pay.sisterai.local/card/UHID-20261120-1002", "", "Dr. K. Chatterjee"),
                ("UHID-20261120-1003", "Kavya Menon", 29, "Rx-6601", "Priya Nair", "Outpatient", "9800112277", "Indiranagar", "kavya@example.com", "Ophthalmology", "OCT Scanner A", "2026-11-20", "11:30", "Retinal OCT & Fundus", "Blurred vision", "Scheduled", "Cash (Offline)", "PAID", "", "", "Priya Nair"),
            ]
            conn.executemany(
                """
                INSERT INTO appointments (
                    unique_id, patient_name, age, prescription, referring_doctor,
                    patient_type, phone, address, email, department, provider_or_resource,
                    appt_date, appt_time, scan_procedure, reason, status,
                    payment_mode, payment_status, payment_link, bill_path, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                seed_appts,
            )

        conn.commit()
        return True
    finally:
        conn.close()


@st.cache_resource
def ensure_database_initialized() -> bool:
    return _init_database_impl()


ensure_database_initialized()


# Background Session Runner Registry (Survives st.rerun() when clicking New Chat)
@st.cache_resource
def _get_background_turn_registry() -> dict[str, dict]:
    return {}


def _submit_background_turn(
    session_id: str,
    display_label: str,
    turn_inputs: list,
    enable_voice_out: bool,
    live_voice_mode: bool,
    recent_history: list[dict],
    active_user: dict,
    active_dept: str,
    selected_payment_mode: str,
    is_admin_central: bool,
    mute_audio: bool,
) -> None:
    registry = _get_background_turn_registry()
    state = {"status": "running", "started_at": time.time(), "error": None}
    registry[session_id] = state

    def _worker() -> None:
        bill_holder: dict | None = None
        try:
            multi_contents = _build_multi_turn_contents_from_list(recent_history, turn_inputs)
            active_instruction = _build_system_instruction_for_context(
                user=active_user,
                dept=active_dept,
                pay_mode=selected_payment_mode,
                is_admin_central=is_admin_central,
                live_voice_mode=live_voice_mode,
            )
            response = generate_with_retry(
                contents=multi_contents,
                instruction=active_instruction,
                temperature=0.25 if live_voice_mode else 0.3,
                context_overrides={
                    "user": active_user,
                    "dept": active_dept,
                    "pay_mode": selected_payment_mode,
                    "is_admin_central": is_admin_central,
                    "bill_holder_ref": lambda val: None,
                },
                bill_sink=bill_holder,
            )
            reply_text = (getattr(response, "text", None) or "").strip() or "Completed request."

            should_speak = (enable_voice_out or live_voice_mode) and not mute_audio
            raw_audio_bytes = None
            if should_speak and reply_text:
                try:
                    _, raw_audio_bytes = generate_voice_response(reply_text)
                except Exception:
                    raw_audio_bytes = None

            bill_bytes = bill_holder["bytes"] if bill_holder else None
            bill_fname = bill_holder["filename"] if bill_holder else None

            save_message_to_db(session_id, "assistant", reply_text, raw_audio_bytes, bill_bytes, bill_fname)
            state["status"] = "done"
        except Exception as e:
            err_msg = f"System Error: {str(e)}"
            save_message_to_db(session_id, "assistant", err_msg, None, None, None)
            state["status"] = "error"
            state["error"] = err_msg

    threading.Thread(target=_worker, daemon=True).start()


# 6. Authentication & Department Context Helpers
def authenticate_staff(identifier: str, password: str) -> dict | None:
    clean_id = (identifier or "").strip()
    if not clean_id or not password:
        return None
    pw_hash = _hash_password(password)
    conn = get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT id, username, full_name, department, role, is_admin
            FROM staff_users
            WHERE (LOWER(full_name) = LOWER(?) OR LOWER(username) = LOWER(?))
              AND password_hash = ?
            """,
            (clean_id, clean_id, pw_hash),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_all_staff_directory() -> list[dict]:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT username, full_name, department, role, is_admin FROM staff_users ORDER BY is_admin DESC, department ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_active_user() -> dict:
    return st.session_state["auth_user"]


def get_active_department() -> str:
    return st.session_state["active_department"]


def is_admin_or_central() -> bool:
    u = get_active_user()
    return bool(u.get("is_admin")) or get_active_department() in (
        "Central Reception & Registration",
        "Billing, Insurance & RCM",
    )


def fetch_department_dashboard_snapshot(department: str) -> dict:
    """Fetches scoped KPIs and live SQLite rows for the active department only."""
    conn = get_db_connection()
    try:
        appts = conn.execute(
            """
            SELECT id, unique_id, patient_name, age, patient_type, scan_procedure,
                   provider_or_resource, appt_date, appt_time, status,
                   payment_mode, payment_status, referring_doctor, phone, email, created_by
            FROM appointments
            WHERE department = ?
            ORDER BY appt_date ASC, appt_time ASC, id DESC
            """,
            (department,),
        ).fetchall()

        referrals = conn.execute(
            """
            SELECT id, unique_id, patient_name, from_department, to_department,
                   requested_procedure, priority, status, clinical_notes, referred_by, created_at
            FROM department_referrals
            WHERE from_department = ? OR to_department = ?
            ORDER BY id DESC
            """,
            (department, department),
        ).fetchall()

        incidents = conn.execute(
            """
            SELECT id, category, department_or_modality, summary, severity,
                   estimated_impact, status, reported_by, created_at
            FROM ops_incidents
            WHERE department_or_modality LIKE ?
            ORDER BY id DESC
            """,
            (f"{department}%",),
        ).fetchall()

        appt_list = [dict(r) for r in appts]
        ref_list = [dict(r) for r in referrals]
        inc_list = [dict(r) for r in incidents]

        paid_cnt = sum(1 for a in appt_list if (a["payment_status"] or "").upper() == "PAID")
        unpaid_cnt = sum(1 for a in appt_list if (a["payment_status"] or "").upper() == "UNPAID")
        open_inc_cnt = sum(1 for i in inc_list if (i["status"] or "").lower() == "open")

        return {
            "total_appts": len(appt_list),
            "paid_count": paid_cnt,
            "unpaid_count": unpaid_cnt,
            "referral_count": len(ref_list),
            "open_incidents": open_inc_cnt,
            "appointments": appt_list,
            "referrals": ref_list,
            "incidents": inc_list,
        }
    finally:
        conn.close()


# 7. Department-Scoped Chat Session Helpers
def session_exists_for_scope(session_id: str, username: str, department: str) -> bool:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT id FROM chat_sessions WHERE id = ? AND owner_username = ? AND department = ?",
            (session_id, username, department),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def list_chat_sessions(username: str, department: str) -> list[dict]:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, title, pinned, updated_at
            FROM chat_sessions
            WHERE owner_username = ? AND department = ?
            ORDER BY pinned DESC, updated_at DESC, rowid DESC
            """,
            (username, department),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_chat_session(username: str, department: str, title: str = "New Healthcare Chat") -> str:
    conn = get_db_connection()
    try:
        existing = conn.execute("SELECT id FROM chat_sessions").fetchall()
        existing_ids = {r["id"] for r in existing}
        idx = len(existing) + 1
        session_id = f"chat_{idx}"
        while session_id in existing_ids:
            idx += 1
            session_id = f"chat_{idx}"

        conn.execute(
            """
            INSERT INTO chat_sessions (id, owner_username, department, title, pinned)
            VALUES (?, ?, ?, ?, 0)
            """,
            (session_id, username, department, title),
        )
        conn.commit()
        return session_id
    finally:
        conn.close()


def toggle_pin_chat_session(session_id: str) -> None:
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT pinned FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is not None:
            new_pinned = 0 if row["pinned"] else 1
            conn.execute(
                "UPDATE chat_sessions SET pinned = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_pinned, session_id),
            )
            conn.commit()
    finally:
        conn.close()


def delete_chat_session(session_id: str) -> None:
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def build_share_token(session_id: str) -> str:
    conn = get_db_connection()
    try:
        sess = conn.execute("SELECT title, department FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
        msgs = conn.execute(
            "SELECT role, content FROM chat_messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        payload = {
            "sid": session_id,
            "dept": sess["department"] if sess else "",
            "title": sess["title"] if sess else "Shared Healthcare Chat",
            "messages": [{"role": m["role"], "content": m["content"]} for m in msgs],
        }
        raw_json = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(raw_json).decode("ascii")
    finally:
        conn.close()


def import_shared_token_if_needed(token: str, username: str, department: str) -> str | None:
    if not token:
        return None
    try:
        padded = token + "=" * ((4 - len(token) % 4) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        source_dept = decoded.get("dept", "")
        if source_dept and source_dept != department and not is_admin_or_central():
            return None

        title = decoded.get("title") or "Shared Healthcare Chat"
        msgs = decoded.get("messages") or []
        new_id = create_chat_session(username, department, f"{title} (Shared)")
        conn = get_db_connection()
        try:
            for m in msgs:
                if m.get("role") in ("user", "assistant") and m.get("content"):
                    conn.execute(
                        "INSERT INTO chat_messages (session_id, role, content, audio_bytes) VALUES (?, ?, ?, NULL)",
                        (new_id, m["role"], m["content"]),
                    )
            conn.commit()
        finally:
            conn.close()
        return new_id
    except Exception:
        return None


def load_session_messages(session_id: str) -> list[dict]:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT role, content, audio_bytes, bill_pdf_bytes, bill_filename
            FROM chat_messages
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
        return [
            {
                "role": r["role"],
                "content": r["content"],
                "audio": io.BytesIO(r["audio_bytes"]) if r["audio_bytes"] else None,
                "bill_pdf_bytes": r["bill_pdf_bytes"],
                "bill_filename": r["bill_filename"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def save_message_to_db(
    session_id: str,
    role: str,
    content: str,
    audio_bytes: bytes | None = None,
    bill_pdf_bytes: bytes | None = None,
    bill_filename: str | None = None,
) -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO chat_messages (session_id, role, content, audio_bytes, bill_pdf_bytes, bill_filename)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, role, content, audio_bytes, bill_pdf_bytes, bill_filename),
        )
        if role == "user":
            current = conn.execute("SELECT title FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
            if current and current["title"] == "New Healthcare Chat":
                clean_title = content.replace("🎙️", "").replace("📎", "").replace("•", "").strip()
                if clean_title:
                    short_title = (clean_title[:36] + "...") if len(clean_title) > 36 else clean_title
                    conn.execute(
                        "UPDATE chat_sessions SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (short_title, session_id),
                    )
        conn.execute(
            "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


# Thread-local runtime context so background worker threads run safely without touching st.session_state
_THREAD_CTX = threading.local()


def _current_runtime_ctx() -> dict:
    ctx = getattr(_THREAD_CTX, "ctx", None)
    if ctx is not None:
        return ctx
    return {
        "user": st.session_state["auth_user"],
        "dept": st.session_state["active_department"],
        "pay_mode": st.session_state.get("selected_payment_mode", PAYMENT_MODES[0]),
        "is_admin_central": is_admin_or_central(),
        "bill_sink": None,
    }


def _record_bill_in_ctx(bill_info: dict) -> None:
    ctx = _current_runtime_ctx()
    sink = ctx.get("bill_sink")
    if isinstance(sink, dict):
        sink.clear()
        sink.update(bill_info)
    try:
        st.session_state["pending_bill_pdf"] = bill_info
    except Exception:
        pass


# 8. Department-Enforced Registration, Payment Branching & Cross-Department Referrals
def _normalize_payment_mode(raw_mode: str) -> str:
    low = (raw_mode or "").strip().lower()
    if "cash" in low or "offline" in low:
        return "Cash (Offline)"
    if "card" in low or "debit" in low or "credit" in low:
        return "Card Payment (Debit / Credit)"
    if "online" in low or "upi" in low:
        return "Online Payment (UPI)"
    ui_choice = _current_runtime_ctx().get("pay_mode")
    return ui_choice if ui_choice in PAYMENT_MODES else "Online Payment (UPI)"


def _resolve_target_department(requested_dept: str) -> tuple[str, str | None]:
    ctx = _current_runtime_ctx()
    active_dept = ctx["dept"]
    user = ctx["user"]
    req = (requested_dept or "").strip()

    if ctx["is_admin_central"]:
        if not req:
            return active_dept, None
        matched = _match_department_name(req)
        return matched or req, None

    if req and req.lower() not in active_dept.lower() and active_dept.lower() not in req.lower():
        return active_dept, (
            f"ACCESS_DENIED: Workstation is locked to '{active_dept}' (Staff: {user['full_name']}). "
            f"You cannot directly view or book into '{req}'. Use `create_department_referral` to send a controlled referral handoff instead."
        )
    return active_dept, None


def register_and_schedule_patient(
    patient_name: str,
    appt_date: str,
    appt_time: str,
    scan_procedure: str,
    department: str = "",
    provider_or_resource: str = "Assigned Modality / Specialist",
    unique_id: str = "",
    age: int = 30,
    prescription: str = "",
    referring_doctor: str = "",
    patient_type: str = "Outpatient",
    phone: str = "",
    address: str = "",
    email: str = "",
    reason: str = "",
    status: str = "Scheduled",
    payment_mode: str = "",
) -> str:
    """Department-scoped patient registration & scheduling with self-check, auto-UHID,
    payment mode branching (Online/Card link vs Cash direct bill), under-13 UNPAID exception, and PDF bill.
    """
    target_dept, access_err = _resolve_target_department(department)
    if access_err:
        return access_err

    missing_fields = []
    if not (patient_name or "").strip():
        missing_fields.append("patient_name")
    if not (appt_date or "").strip():
        missing_fields.append("appt_date")
    if not (appt_time or "").strip():
        missing_fields.append("appt_time")
    if not (scan_procedure or "").strip():
        missing_fields.append("scan_procedure")

    if missing_fields:
        return (
            "INCOMPLETE_REGISTRATION: Missing required scheduling fields: "
            + ", ".join(missing_fields)
            + ". Ask for these details before finalizing."
        )

    recommended_missing = []
    if not (phone or "").strip():
        recommended_missing.append("phone")
    if not (email or "").strip():
        recommended_missing.append("email")
    if not (address or "").strip():
        recommended_missing.append("address")
    if not (referring_doctor or "").strip():
        recommended_missing.append("referring_doctor")
    if not (prescription or "").strip():
        recommended_missing.append("prescription")

    uhid_generated = False
    clean_uhid = (unique_id or "").strip()
    if not clean_uhid:
        clean_uhid = generate_uhid(appt_date.strip())
        uhid_generated = True

    clean_type = "Inpatient" if "in" in (patient_type or "").lower() else "Outpatient"
    try:
        clean_age = max(0, int(age)) if age is not None else 30
    except Exception:
        clean_age = 30
    amount_inr = estimate_procedure_amount(scan_procedure)
    chosen_mode = _normalize_payment_mode(payment_mode)
    staff_user = _current_runtime_ctx()["user"]

    if clean_age < 13:
        payment_status = "UNPAID"
        payment_note = "Child (<13 yrs) pay-later exception applied; settle later."
    else:
        payment_status = "PAID"
        payment_note = f"Settled via {chosen_mode}."

    if chosen_mode == "Cash (Offline)":
        payment_link = ""
        branch_line = "Cash (Offline) selected: No online/card link generated; printable PDF bill created directly."
    elif chosen_mode == "Card Payment (Debit / Credit)":
        payment_link = f"https://pay.sisterai.local/card/{clean_uhid}?amt={amount_inr}"
        branch_line = f"Card Payment selected: Inbuilt card link generated -> {payment_link}"
    else:
        payment_link = (
            f"upi://pay?pa=sisterai.hospital@upi&pn=SisterAI&am={amount_inr}&cu=INR&tn={clean_uhid}"
            f" | Card Link: https://pay.sisterai.local/card/{clean_uhid}"
        )
        branch_line = f"Online Payment (UPI) selected: Inbuilt UPI/card link generated -> {payment_link}"

    conn = get_db_connection()
    try:
        clean_res = (provider_or_resource or "").strip() or "Assigned Modality / Specialist"
        conflict = conn.execute(
            """
            SELECT id, patient_name FROM appointments
            WHERE department = ?
              AND appt_date = ?
              AND appt_time = ?
              AND LOWER(provider_or_resource) = LOWER(?)
              AND status != 'Cancelled'
            """,
            (target_dept, appt_date.strip(), appt_time.strip(), clean_res),
        ).fetchone()
        if conflict:
            return (
                f"CONFLICT in {target_dept}: {clean_res} is already booked at {appt_time} on {appt_date} "
                f"for {conflict['patient_name']} (Booking #{conflict['id']})."
            )

        cur = conn.execute(
            """
            INSERT INTO appointments (
                unique_id, patient_name, age, prescription, referring_doctor,
                patient_type, phone, address, email, department, provider_or_resource,
                appt_date, appt_time, scan_procedure, reason, status,
                payment_mode, payment_status, payment_link, bill_path, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
            """,
            (
                clean_uhid,
                patient_name.strip(),
                clean_age,
                (prescription or "").strip() or "Departmental Intake / Pending Rx",
                (referring_doctor or "").strip() or "Direct / Dept Referral",
                clean_type,
                (phone or "").strip() or "N/A",
                (address or "").strip() or "Not Provided",
                (email or "").strip() or "Not Provided",
                target_dept,
                clean_res,
                appt_date.strip(),
                appt_time.strip(),
                scan_procedure.strip(),
                (reason or "").strip() or scan_procedure.strip(),
                (status or "").strip() or "Scheduled",
                chosen_mode,
                payment_status,
                payment_link,
                staff_user["full_name"],
            ),
        )
        appt_id = cur.lastrowid

        bill_data = {
            "bill_no": f"BILL-{appt_id:05d}",
            "issued_date": "2026-11-19",
            "department": target_dept,
            "staff_name": staff_user["full_name"],
            "appointment_id": appt_id,
            "unique_id": clean_uhid,
            "patient_name": patient_name.strip(),
            "age": clean_age,
            "patient_type": clean_type,
            "phone": (phone or "").strip() or "N/A",
            "email": (email or "").strip() or "Not Provided",
            "address": (address or "").strip() or "Not Provided",
            "referring_doctor": (referring_doctor or "").strip() or "Direct / Dept Referral",
            "prescription": (prescription or "").strip() or "Departmental Intake / Pending Rx",
            "scan_procedure": scan_procedure.strip(),
            "reason": (reason or "").strip() or scan_procedure.strip(),
            "appt_date": appt_date.strip(),
            "appt_time": appt_time.strip(),
            "status": (status or "").strip() or "Scheduled",
            "amount_inr": amount_inr,
            "payment_mode": chosen_mode,
            "payment_status": payment_status,
            "payment_note": payment_note,
            "payment_link": payment_link,
        }
        bill_path, pdf_bytes = create_pdf_bill_file(bill_data)
        conn.execute("UPDATE appointments SET bill_path = ? WHERE id = ?", (bill_path, appt_id))
        conn.commit()

        _record_bill_in_ctx({
            "bytes": pdf_bytes,
            "filename": os.path.basename(bill_path),
            "patient_name": patient_name.strip(),
            "unique_id": clean_uhid,
            "payment_status": payment_status,
            "payment_mode": chosen_mode,
        })

        uhid_msg = f"{clean_uhid} (Auto-generated at {target_dept} desk)" if uhid_generated else clean_uhid
        rec_note = (
            f"Optional/recommended intake items still pending: {', '.join(recommended_missing)}."
            if recommended_missing
            else "All intake fields captured."
        )
        return (
            f"SCHEDULED_AND_BILLED [{target_dept}]:\n"
            f"- Appointment ID: #{appt_id} | Unique ID: {uhid_msg}\n"
            f"- Patient: {patient_name.strip()} | Age: {clean_age} | Type: {clean_type}\n"
            f"- Procedure: {scan_procedure.strip()} ({clean_res})\n"
            f"- Date & Time: {appt_date.strip()} at {appt_time.strip()} | Status: {(status or '').strip() or 'Scheduled'}\n"
            f"- Referring Dr: {(referring_doctor or '').strip() or 'Direct / Dept Referral'} | Rx: {(prescription or '').strip() or 'Departmental Intake / Pending Rx'}\n"
            f"- Contact: {(phone or '').strip() or 'N/A'} | Email: {(email or '').strip() or 'Not Provided'} | Address: {(address or '').strip() or 'Not Provided'}\n"
            f"- Billing: INR {amount_inr} | Mode: {chosen_mode} | Payment Status: {payment_status} ({payment_note})\n"
            f"- {branch_line}\n"
            f"- PDF Bill Saved: {os.path.basename(bill_path)}\n"
            f"- {rec_note}"
        )
    except Exception as e:
        return f"Registration/scheduling error: {str(e)}"
    finally:
        conn.close()


def create_department_referral(
    patient_name: str,
    to_department: str,
    requested_procedure: str,
    unique_id: str = "",
    clinical_notes: str = "",
    priority: str = "Routine",
) -> str:
    """Creates a controlled cross-department referral handoff from the active department to another hospital department."""
    ctx = _current_runtime_ctx()
    from_dept = ctx["dept"]
    user = ctx["user"]
    matched_target = _match_department_name(to_department)
    if not matched_target:
        return f"INVALID_DEPARTMENT: '{to_department}' is not a recognized hospital department."

    if matched_target == from_dept:
        return f"SAME_DEPARTMENT: Patient is already in {from_dept}. Book directly instead of creating an external referral."

    if not (patient_name or "").strip() or not (requested_procedure or "").strip():
        return "INCOMPLETE_REFERRAL: Both patient_name and requested_procedure are required."

    clean_uhid = (unique_id or "").strip() or generate_uhid("2026-11-19")
    clean_prio = (priority or "").strip().title() if (priority or "").strip() else "Routine"
    if clean_prio not in ("Routine", "Urgent", "Stat"):
        clean_prio = "Routine"

    conn = get_db_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO department_referrals (
                unique_id, patient_name, from_department, to_department,
                requested_procedure, clinical_notes, priority, status, referred_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending', ?)
            """,
            (
                clean_uhid,
                patient_name.strip(),
                from_dept,
                matched_target,
                requested_procedure.strip(),
                (clinical_notes or "").strip() or "Referred for departmental evaluation",
                "STAT" if clean_prio == "Stat" else clean_prio,
                user["full_name"],
            ),
        )
        conn.commit()
        return (
            f"REFERRAL_CREATED #REF-{cur.lastrowid:04d}:\n"
            f"- Handoff: {from_dept} -> {matched_target}\n"
            f"- Patient: {patient_name.strip()} | UHID: {clean_uhid}\n"
            f"- Requested Procedure/Consult: {requested_procedure.strip()} | Priority: {clean_prio}\n"
            f"- Clinical Notes: {(clinical_notes or '').strip() or 'Referred for departmental evaluation'}\n"
            f"- Referred By: {user['full_name']} | Status: Pending receiving desk intake"
        )
    except Exception as e:
        return f"Referral creation error: {str(e)}"
    finally:
        conn.close()


def list_department_referrals(direction: str = "all") -> str:
    """Lists incoming and outgoing cross-department referrals for the active department."""
    active_dept = _current_runtime_ctx()["dept"]
    conn = get_db_connection()
    try:
        d = (direction or "all").strip().lower()
        if d == "incoming":
            rows = conn.execute(
                "SELECT * FROM department_referrals WHERE to_department = ? ORDER BY id DESC LIMIT 20",
                (active_dept,),
            ).fetchall()
        elif d == "outgoing":
            rows = conn.execute(
                "SELECT * FROM department_referrals WHERE from_department = ? ORDER BY id DESC LIMIT 20",
                (active_dept,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM department_referrals
                WHERE from_department = ? OR to_department = ?
                ORDER BY id DESC LIMIT 20
                """,
                (active_dept, active_dept),
            ).fetchall()

        if not rows:
            return f"No cross-department referrals found for {active_dept}."
        return "\n".join(
            f"#REF-{r['id']:04d} | {r['from_department']} -> {r['to_department']} | "
            f"{r['patient_name']} ({r['unique_id']}) | {r['requested_procedure']} | "
            f"Priority: {r['priority']} | Status: {r['status']} | By: {r['referred_by']}"
            for r in rows
        )
    except Exception as e:
        return f"Referral list error: {str(e)}"
    finally:
        conn.close()


# 9. Open Email Tool
def send_email_notification(
    to_email: str,
    subject: str,
    body: str,
    attach_latest_bill: bool = False,
) -> str:
    """Sends an email to ANY recipient address via SMTP, HTTPS relay, or mailto fallback."""
    target = (to_email or "").strip()
    if not target or "@" not in target:
        return "EMAIL_FAILED: Please provide a valid recipient email address."

    clean_subject = (subject or "").strip() or "SisterAI Hospital Notification"
    clean_body = (body or "").strip()

    ctx = _current_runtime_ctx()
    bill_info = ctx.get("bill_sink") if (attach_latest_bill and isinstance(ctx.get("bill_sink"), dict) and ctx["bill_sink"]) else None
    mailto_url = f"mailto:{quote(target)}?subject={quote(clean_subject)}&body={quote(clean_body)}"

    if SMTP_HOST and SMTP_USER and SMTP_PASS:
        try:
            msg = EmailMessage()
            msg["From"] = SMTP_FROM or SMTP_USER
            msg["To"] = target
            msg["Subject"] = clean_subject
            msg.set_content(clean_body)

            if bill_info:
                msg.add_attachment(
                    bill_info["bytes"],
                    maintype="application",
                    subtype="pdf",
                    filename=bill_info["filename"],
                )

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=8) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)

            return f"EMAIL_SENT_SMTP: Sent email to {target} with subject '{clean_subject}'."
        except Exception as smtp_err:
            smtp_reason = str(smtp_err)
    else:
        smtp_reason = "SMTP credentials not set"

    if MAIL_RELAY_URL:
        try:
            payload = {
                "to": target,
                "from": SMTP_FROM or SMTP_USER or "noreply@sisterai.local",
                "subject": clean_subject,
                "body": clean_body,
                "bill_filename": bill_info["filename"] if bill_info else None,
                "bill_pdf_base64": (
                    base64.b64encode(bill_info["bytes"]).decode("ascii") if bill_info else None
                ),
            }
            r = requests.post(MAIL_RELAY_URL, json=payload, timeout=10)
            if r.status_code in (200, 201, 202):
                return f"EMAIL_SENT_RELAY: Sent email to {target} via HTTPS relay."
            relay_reason = f"Relay HTTP {r.status_code}"
        except Exception as rel_err:
            relay_reason = f"Relay error: {str(rel_err)}"
    else:
        relay_reason = "MAIL_RELAY_URL not set"

    return (
        f"EMAIL_NETWORK_BLOCKED_FALLBACK: Direct send to {target} did not complete ({smtp_reason}; {relay_reason}). "
        f"Use this direct mailto link to open your email client for {target}: {mailto_url}"
    )


# 10. Department-Scoped Scheduler & Ops Query Tools (SQLite)
def check_availability(
    appt_date: str,
    department: str = "",
    provider_or_resource: str = "",
) -> str:
    """Checks booked appointments on a YYYY-MM-DD date within the authorized department scope."""
    target_dept, access_err = _resolve_target_department(department)
    if access_err:
        return access_err

    conn = get_db_connection()
    try:
        query = """
            SELECT id, unique_id, patient_name, department, provider_or_resource,
                   appt_time, scan_procedure, status, payment_mode, payment_status
            FROM appointments
            WHERE appt_date = ? AND department = ? AND status != 'Cancelled'
        """
        params: list = [(appt_date or "").strip(), target_dept]
        if (provider_or_resource or "").strip():
            query += " AND LOWER(provider_or_resource) LIKE ?"
            params.append(f"%{provider_or_resource.strip().lower()}%")
        query += " ORDER BY appt_time ASC"

        rows = conn.execute(query, params).fetchall()
        if not rows:
            return f"No bookings in {target_dept} on {appt_date}. Slots 09:00–17:00 open."
        return "\n".join(
            f"- #{r['id']} ({r['unique_id']}) | {r['appt_time']} | {r['patient_name']} "
            f"| {r['scan_procedure']} ({r['provider_or_resource']}) | {r['status']} | {r['payment_mode']} [{r['payment_status']}]"
            for r in rows
        )
    except Exception as e:
        return f"Scheduler error: {str(e)}"
    finally:
        conn.close()


def reschedule_or_update_appointment(
    appointment_id: int,
    new_date: str = "",
    new_time: str = "",
    status: str = "",
    payment_status: str = "",
) -> str:
    """Reschedules or updates an appointment ID (enforces department access control)."""
    ctx = _current_runtime_ctx()
    active_dept = ctx["dept"]
    conn = get_db_connection()
    try:
        existing = conn.execute("SELECT * FROM appointments WHERE id = ?", (int(appointment_id),)).fetchone()
        if not existing:
            return f"No appointment #{appointment_id}."

        if existing["department"] != active_dept and not ctx["is_admin_central"]:
            return (
                f"ACCESS_DENIED: Appointment #{appointment_id} belongs to '{existing['department']}'. "
                f"Your workstation is restricted to '{active_dept}'."
            )

        target_date = (new_date or "").strip() if (new_date or "").strip() else existing["appt_date"]
        target_time = (new_time or "").strip() if (new_time or "").strip() else existing["appt_time"]
        target_status = (status or "").strip() if (status or "").strip() else existing["status"]
        target_pay = (payment_status or "").strip().upper() if (payment_status or "").strip() else existing["payment_status"]

        if target_status != "Cancelled":
            conflict = conn.execute(
                """
                SELECT id FROM appointments
                WHERE id != ?
                  AND department = ?
                  AND appt_date = ?
                  AND appt_time = ?
                  AND LOWER(provider_or_resource) = LOWER(?)
                  AND status != 'Cancelled'
                """,
                (int(appointment_id), existing["department"], target_date, target_time, existing["provider_or_resource"]),
            ).fetchone()
            if conflict:
                return f"CONFLICT: {existing['provider_or_resource']} busy at {target_time} on {target_date} (#{conflict['id']})."

        conn.execute(
            "UPDATE appointments SET appt_date = ?, appt_time = ?, status = ?, payment_status = ? WHERE id = ?",
            (target_date, target_time, target_status, target_pay, int(appointment_id)),
        )
        conn.commit()
        return (
            f"UPDATED #{appointment_id} [{existing['department']}] ({existing['unique_id']} - {existing['patient_name']}): "
            f"{target_status} on {target_date} at {target_time} | Payment: {target_pay}."
        )
    except Exception as e:
        return f"Update error: {str(e)}"
    finally:
        conn.close()


def list_upcoming_appointments(patient_or_date: str = "") -> str:
    """Lists appointments filtered by patient name, UHID, or YYYY-MM-DD date within authorized department scope."""
    active_dept = _current_runtime_ctx()["dept"]
    conn = get_db_connection()
    try:
        q = (patient_or_date or "").strip()
        if q:
            like_q = f"%{q.lower()}%"
            rows = conn.execute(
                """
                SELECT * FROM appointments
                WHERE department = ?
                  AND (LOWER(patient_name) LIKE ? OR LOWER(unique_id) LIKE ? OR appt_date = ?)
                ORDER BY appt_date ASC, appt_time ASC, id ASC
                """,
                (active_dept, like_q, like_q, q),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM appointments
                WHERE department = ?
                ORDER BY appt_date ASC, appt_time ASC, id ASC
                LIMIT 20
                """,
                (active_dept,),
            ).fetchall()

        if not rows:
            return f"No matching appointments in {active_dept}{f' for {q}' if q else ''}."
        return "\n".join(
            f"#{r['id']} [{r['department']}] | UHID: {r['unique_id']} | {r['appt_date']} {r['appt_time']} | "
            f"{r['patient_name']} (Age {r['age']}, {r['patient_type']}) | {r['scan_procedure']} | "
            f"Ref Dr: {r['referring_doctor']} | Rx: {r['prescription']} | Phone: {r['phone']} | "
            f"Email: {r['email']} | Status: {r['status']} | {r['payment_mode']} [{r['payment_status']}]"
            for r in rows
        )
    except Exception as e:
        return f"List error: {str(e)}"
    finally:
        conn.close()


def log_ops_or_rcm_incident(
    category: str,
    department_or_modality: str,
    summary: str,
    severity: str = "Medium",
    estimated_impact: str = "Unspecified",
) -> str:
    """Logs a department downtime, bottleneck, or claim denial/RCM issue in SQLite behind the scenes."""
    ctx = _current_runtime_ctx()
    active_dept = ctx["dept"]
    user = ctx["user"]
    scoped_target = f"{active_dept} - {(department_or_modality or '').strip()}" if (department_or_modality or "").strip() else active_dept
    conn = get_db_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO ops_incidents (
                category, department_or_modality, summary, severity, estimated_impact, status, reported_by
            ) VALUES (?, ?, ?, ?, ?, 'Open', ?)
            """,
            (
                (category or "").strip() or "Operations",
                scoped_target,
                (summary or "").strip(),
                (severity or "").strip() or "Medium",
                (estimated_impact or "").strip() or "Unspecified",
                user["full_name"],
            ),
        )
        conn.commit()
        return f"LOGGED INCIDENT #{cur.lastrowid} [{category} | {scoped_target}]: {summary}"
    except Exception as e:
        return f"Incident log error: {str(e)}"
    finally:
        conn.close()


def get_imaging_protocol_checklist(modality: str, contrast_or_implant: str = "") -> str:
    """Returns structured clinical/imaging safety, preparation, and throughput checks."""
    mod = (modality or "").strip().lower()
    extra = (contrast_or_implant or "").strip()
    base = [
        f"Department Context: {_current_runtime_ctx()['dept']}",
        f"Procedure/Modality: {(modality or '').strip() or 'Clinical / Diagnostic Protocol'}",
        f"Screening Focus: {extra or 'Standard protocol & safety check'}",
    ]
    if "mri" in mod:
        base.extend([
            "- Verify metallic implants, pacemakers/neurostimulators, aneurysm clips, or metallic foreign bodies against MR Conditional/Safe specs and scanner field strength (1.5T/3T).",
            "- Check renal function/eGFR if gadolinium contrast is planned.",
            "- Confirm ear protection, comfort/sedation needs, and scan slot duration.",
        ])
    elif "ct" in mod:
        base.extend([
            "- If iodinated contrast is planned: check baseline eGFR/renal function, contrast allergy history, and metformin status.",
            "- Confirm pregnancy screening where applicable, IV access patency, and radiation dose/protocol selection.",
        ])
    elif "echo" in mod or "cardio" in mod:
        base.extend([
            "- Verify baseline vitals, ECG leads/chest prep, prior echo comparison, and anticoagulation/medication list if stress or TEE study.",
        ])
    elif "oct" in mod or "opto" in mod or "fundus" in mod:
        base.extend([
            "- Confirm pupil dilation contraindications (angle-closure glaucoma check), intraocular pressure baseline, and visual acuity.",
        ])
    else:
        base.extend([
            "- Confirm clinical indication, prior studies comparison, patient prep instructions, and turnaround SLA.",
        ])
    return "\n".join(base)


# 11. Pharmaceutical Knowledge Tool (OpenFDA Live API - 100% Free)
def lookup_pharmaceutical_data(drug_name: str) -> str:
    """Queries live OpenFDA drug label data for indications, contraindications, and warnings."""
    clean_drug = (drug_name or "").strip()
    if not clean_drug:
        return "Please provide a medication or contrast agent name."
    try:
        url = (
            "https://api.fda.gov/drug/label.json?"
            f'search=openfda.brand_name:"{quote_plus(clean_drug)}"+openfda.generic_name:"{quote_plus(clean_drug)}"&limit=1'
        )
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("results"):
                res = data["results"][0]
                openfda = res.get("openfda", {})
                brand = ", ".join(openfda.get("brand_name", [clean_drug]))
                generic = ", ".join(openfda.get("generic_name", ["N/A"]))
                indications = " ".join(res.get("indications_and_usage", ["Not listed."]))[:500]
                warnings = " ".join(res.get("warnings", res.get("boxed_warning", ["No specific warnings listed."])))[:500]
                contraindications = " ".join(res.get("contraindications", ["None listed."]))[:400]
                return (
                    f"FDA LABEL — {brand} ({generic}):\n"
                    f"- Indications: {indications}\n"
                    f"- Contraindications: {contraindications}\n"
                    f"- Warnings: {warnings}"
                )
        return f"No exact FDA record found for '{clean_drug}'."
    except Exception as e:
        return f"OpenFDA lookup unavailable ({str(e)}). Provide general pharmacology guidance."


# 12. Focused Healthcare News, Medical Journals & Research Papers Tool (Parallelized)
def _fetch_pubmed_papers(clean_topic: str) -> str | None:
    try:
        esearch_url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
            f"?db=pubmed&term={quote_plus(clean_topic)}&retmode=json&retmax=3&sort=date"
        )
        s_resp = requests.get(esearch_url, timeout=5)
        if s_resp.status_code != 200:
            return None
        pmids = s_resp.json().get("esearchresult", {}).get("idlist", [])
        if not pmids:
            return None
        esum_url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
            f"?db=pubmed&id={','.join(pmids)}&retmode=json"
        )
        u_resp = requests.get(esum_url, timeout=5)
        if u_resp.status_code != 200:
            return None
        summary_data = u_resp.json().get("result", {})
        lines = ["[PubMed Journals & Papers]"]
        for pmid in pmids:
            item = summary_data.get(pmid, {})
            title = item.get("title", "Untitled").rstrip(".")
            journal = item.get("source", "Journal")
            pubdate = item.get("pubdate", "")
            lines.append(f"- {title} | {journal} ({pubdate}) | https://pubmed.ncbi.nlm.nih.gov/{pmid}/")
        return "\n".join(lines)
    except Exception:
        return None


def _fetch_healthcare_news(clean_topic: str, scope: str) -> str | None:
    try:
        sc = (scope or "both").lower()
        if sc == "national":
            site_filter = (
                "(site:mohfw.gov.in OR site:icmr.gov.in OR site:health.economictimes.indiatimes.com "
                "OR site:thehindu.com/sci-tech/health OR site:indianexpress.com/section/lifestyle/health)"
            )
            region_params = "hl=en-IN&gl=IN&ceid=IN:en"
        elif sc == "international":
            site_filter = (
                "(site:who.int OR site:nejm.org OR site:thelancet.com OR site:jamanetwork.com "
                "OR site:bmj.com OR site:rsna.org OR site:healthcareitnews.com)"
            )
            region_params = "hl=en-US&gl=US&ceid=US:en"
        else:
            site_filter = (
                "(site:mohfw.gov.in OR site:icmr.gov.in OR site:health.economictimes.indiatimes.com "
                "OR site:who.int OR site:nejm.org OR site:thelancet.com OR site:jamanetwork.com OR site:bmj.com)"
            )
            region_params = "hl=en-IN&gl=IN&ceid=IN:en"

        rss_url = f"https://news.google.com/rss/search?q={quote_plus(f'({clean_topic}) {site_filter}')}&{region_params}"
        rss_resp = requests.get(rss_url, timeout=5)
        if rss_resp.status_code != 200:
            return None
        root = ET.fromstring(rss_resp.text)
        items = root.findall("./channel/item")[:4]
        if not items:
            return None
        lines = [f"[Healthcare News ({scope})]"]
        for it in items:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub_date = (it.findtext("pubDate") or "").strip()
            source_name = (it.findtext("source") or "Healthcare Source").strip()
            lines.append(f"- {title} | {source_name} ({pub_date}) | {link}")
        return "\n".join(lines)
    except Exception:
        return None


def search_healthcare_literature_and_news(topic: str, scope: str = "both") -> str:
    """Searches healthcare news, medical journals, and papers across national (India) and international health sources."""
    clean_topic = (topic or "").strip()
    if not clean_topic:
        return "Provide a healthcare or medical research topic."

    chunks: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_papers = (
            pool.submit(_fetch_pubmed_papers, clean_topic)
            if (scope or "both").lower() in ("both", "international", "journals", "papers")
            else None
        )
        fut_news = pool.submit(_fetch_healthcare_news, clean_topic, scope or "both")

        if fut_papers:
            try:
                p_res = fut_papers.result(timeout=6)
                if p_res:
                    chunks.append(p_res)
            except Exception:
                pass
        try:
            n_res = fut_news.result(timeout=6)
            if n_res:
                chunks.append(n_res)
        except Exception:
            pass

    return "\n\n".join(chunks) if chunks else f"No recent medical journal/news items found for '{clean_topic}'."


# 13. Full-Length Text-to-Speech Helper
def _clean_for_speech(text: str) -> str:
    no_urls = re.sub(r"https?://\S+", "", text or "")
    no_md = re.sub(r"[*_#`~>\[\]\(\)]", " ", no_urls)
    no_emojis = re.sub(r"[^\w\s.,!?;:'\"-]", " ", no_md)
    return re.sub(r"\s+", " ", no_emojis).strip()


def _split_speech_chunks(text: str, max_chars: int = 900) -> list[str]:
    if len(text) <= max_chars:
        return [text] if text else []
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for sent in sentences:
        if len(current) + len(sent) + 1 <= max_chars:
            current = f"{current} {sent}".strip()
        else:
            if current:
                chunks.append(current)
            current = sent[:max_chars]
    if current:
        chunks.append(current)
    return chunks


def generate_voice_response(text: str) -> tuple[io.BytesIO | None, bytes | None]:
    """Converts the entire cleaned response to MP3 audio bytes without cutting off."""
    spoken_text = _clean_for_speech(text)
    if not spoken_text:
        return None, None

    chunks = _split_speech_chunks(spoken_text, max_chars=900)
    combined = io.BytesIO()
    for ch in chunks:
        tts = gTTS(text=ch, lang="en", slow=False)
        tts.write_to_fp(combined)

    raw_bytes = combined.getvalue()
    if not raw_bytes:
        return None, None
    stream = io.BytesIO(raw_bytes)
    stream.seek(0)
    return stream, raw_bytes


# 14. Uploaded File / Image / Document Helper
def build_file_parts(uploaded_files) -> list:
    parts = []
    if not uploaded_files:
        return parts

    for uploaded_file in uploaded_files:
        file_bytes = uploaded_file.read()
        mime_type = (
            uploaded_file.type
            or mimetypes.guess_type(uploaded_file.name)[0]
            or "application/octet-stream"
        )

        if mime_type.startswith("image/") or mime_type == "application/pdf":
            parts.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
        else:
            try:
                text_content = file_bytes.decode("utf-8", errors="replace")[:8000]
                parts.append(f"\n--- Attached File: {uploaded_file.name} ---\n{text_content}\n")
            except Exception:
                parts.append(f"\n[Could not read '{uploaded_file.name}' as text.]\n")
    return parts


# 15. Dynamic Department-Aware System Instructions & Tools
def _build_system_instruction_for_context(
    user: dict,
    dept: str,
    pay_mode: str,
    is_admin_central: bool,
    live_voice_mode: bool = False,
) -> str:
    access_scope = (
        "Enterprise Admin / Central Desk (can switch departments)"
        if is_admin_central
        else f"Department-Locked Workstation ({dept} ONLY)"
    )

    if live_voice_mode:
        return f"""
You are SisterAI in LIVE VOICE CONVERSATION mode as a Personal Medical Assistant.
Reference date: Thursday, November 19, 2026 (2026-11-19).
Logged-in Staff: {user['full_name']} ({user['role']}) | Active Department: {dept} | Access: {access_scope} | Default Payment Mode: {pay_mode}.
Speak naturally, warmly, and directly as if talking out loud in real time—no bullet walls, no markdown clutter, no robotic intros.
Keep spoken turns focused, clear, and conversational (around 3–6 crisp sentences unless details are asked).
Enforce strict department isolation: never access or book into unauthorized departments; use `create_department_referral` for cross-department handoffs. Tolerate hurried/messy speech without correcting grammar.
"""

    return f"""
You are SisterAI, an enterprise-grade Personal Medical Assistant.
Reference date: Thursday, November 19, 2026 (2026-11-19).
Logged-in Staff: {user['full_name']} ({user['role']}) | Active Department: {dept} | Access: {access_scope} | Selected Payment Mode: {pay_mode}.

Rules:
1. Healthcare-only domain. Politely decline unrelated topics.
2. Strict Departmental Access Control & Referrals: Operate within '{dept}'. If a non-admin staff member requests another department's schedule/records, explain the departmental boundary clearly and use `create_department_referral` (or `list_department_referrals`) for controlled cross-department handoffs.
3. Tolerate typos, slang, and shorthand immediately without correcting grammar.
4. Registration & Payment Branching: Call `register_and_schedule_patient` when registering/booking a patient. Auto-generate UHID if missing. Respect payment mode ('{pay_mode}' unless specified in prompt): Online/Card generates a payment link; Cash (Offline) skips the payment link and generates the PDF bill directly. Children under 13 (age < 13) get UNPAID status (pay-later); others PAID.
5. Open Email: Call `send_email_notification` whenever asked to send an email to any recipient address. Report actual delivery or the mailto fallback honestly.
6. Drugs/contrast: Call `lookup_pharmaceutical_data` first.
7. Healthcare news, journals, or papers (India national or international): Call `search_healthcare_literature_and_news`.
8. Clinical/imaging protocols: Call `get_imaging_protocol_checklist`; log operational/downtime/RCM issues quietly via `log_ops_or_rcm_incident`.
9. Keep answers crisp, high-signal, and fast.
"""


def build_system_instruction(live_voice_mode: bool = False) -> str:
    ctx = _current_runtime_ctx()
    return _build_system_instruction_for_context(
        user=ctx["user"],
        dept=ctx["dept"],
        pay_mode=ctx["pay_mode"],
        is_admin_central=ctx["is_admin_central"],
        live_voice_mode=live_voice_mode,
    )


SISTER_TOOLS = [
    register_and_schedule_patient,
    create_department_referral,
    list_department_referrals,
    send_email_notification,
    lookup_pharmaceutical_data,
    search_healthcare_literature_and_news,
    get_imaging_protocol_checklist,
    log_ops_or_rcm_incident,
    check_availability,
    reschedule_or_update_appointment,
    list_upcoming_appointments,
]


def _build_multi_turn_contents_from_list(history_messages: list[dict], new_contents: list) -> list:
    contents: list = []
    for m in history_messages[-2:]:
        role = "user" if m["role"] == "user" else "model"
        txt = m.get("content") or ""
        if role == "model" and len(txt) > 350:
            txt = txt[:350] + "..."
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=txt)]))

    current_parts = [
        item if isinstance(item, types.Part) else types.Part.from_text(text=str(item))
        for item in new_contents
    ]
    contents.append(types.Content(role="user", parts=current_parts))
    return contents


def build_multi_turn_contents(history_messages: list[dict], new_contents: list) -> list:
    return _build_multi_turn_contents_from_list(history_messages, new_contents)


MODEL_FALLBACK_CHAIN = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
]


def generate_with_retry(
    contents: list,
    instruction: str,
    temperature: float,
    context_overrides: dict | None = None,
    bill_sink: dict | None = None,
):
    """Tries primary Gemini model and fails over automatically on 503/429/404 errors."""
    active_client = get_genai_client()
    base_ctx = _current_runtime_ctx().copy()
    if context_overrides:
        base_ctx.update(context_overrides)
    if bill_sink is not None:
        base_ctx["bill_sink"] = bill_sink

    _THREAD_CTX.ctx = base_ctx
    last_err = None
    try:
        for model_name in MODEL_FALLBACK_CHAIN:
            for _attempt in range(2):
                try:
                    return active_client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=instruction,
                            tools=SISTER_TOOLS,
                            temperature=temperature,
                        ),
                    )
                except Exception as e:
                    last_err = e
                    err_str = str(e)
                    if any(code in err_str for code in ("503", "UNAVAILABLE", "429", "404", "NOT_FOUND")):
                        time.sleep(0.25)
                        continue
                    raise
        raise last_err
    finally:
        _THREAD_CTX.ctx = None


def run_sister_turn(
    session_id: str,
    display_label: str,
    turn_inputs: list,
    enable_voice_out: bool,
    live_voice_mode: bool = False,
) -> None:
    st.session_state["pending_bill_pdf"] = None
    recent_snapshot = [
        {"role": m["role"], "content": m.get("content", "")}
        for m in st.session_state.messages[-2:]
    ]
    st.session_state.messages.append(
        {"role": "user", "content": display_label, "audio": None, "bill_pdf_bytes": None, "bill_filename": None}
    )
    save_message_to_db(session_id, "user", display_label, None, None, None)

    _submit_background_turn(
        session_id=session_id,
        display_label=display_label,
        turn_inputs=turn_inputs,
        enable_voice_out=enable_voice_out,
        live_voice_mode=live_voice_mode,
        recent_history=recent_snapshot,
        active_user=st.session_state["auth_user"],
        active_dept=st.session_state["active_department"],
        selected_payment_mode=st.session_state.get("selected_payment_mode", PAYMENT_MODES[0]),
        is_admin_central=is_admin_or_central(),
        mute_audio=st.session_state.mute_current_audio,
    )

    with st.chat_message("user"):
        st.markdown(display_label)

    with st.chat_message("assistant"):
        spinner_msg = "SisterAI is speaking..." if live_voice_mode else "SisterAI is responding..."
        placeholder = st.empty()
        registry = _get_background_turn_registry()
        with st.spinner(spinner_msg):
            while registry.get(session_id, {}).get("status") == "running":
                if st.session_state.get("active_session_id") != session_id:
                    return
                time.sleep(0.35)

        if st.session_state.get("active_session_id") != session_id:
            return

        refreshed = load_session_messages(session_id)
        st.session_state.messages = refreshed
        if refreshed and refreshed[-1]["role"] == "assistant":
            last_msg = refreshed[-1]
            with placeholder.container():
                st.markdown(last_msg["content"])
                if last_msg.get("audio") is not None:
                    last_msg["audio"].seek(0)
                    st.audio(last_msg["audio"], format="audio/mp3", autoplay=True)
                if last_msg.get("bill_pdf_bytes") and last_msg.get("bill_filename"):
                    st.download_button(
                        label=f"🖨️ Download Printable PDF Bill ({last_msg['bill_filename']})",
                        data=last_msg["bill_pdf_bytes"],
                        file_name=last_msg["bill_filename"],
                        mime="application/pdf",
                        key=f"dl_bill_{session_id}_{len(refreshed)}_{last_msg['bill_filename']}",
                    )


def _rows_to_csv_bytes(rows: list[dict]) -> bytes:
    if not rows:
        return b""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


# 16. Enterprise Staff Login Gate
if "auth_user" not in st.session_state:
    st.session_state["auth_user"] = None

if st.session_state["auth_user"] is None:
    st.title("🩺 SisterAI")
    st.caption("Your Personal Medical Assistant • Enterprise Departmental Access Portal")

    col_login, col_dir = st.columns([0.48, 0.52], gap="large")
    with col_login:
        with st.form("staff_login_form"):
            st.subheader("Staff Sign In")
            staff_id_input = st.text_input(
                "Staff Full Name or Desk ID",
                value="Soumit Ghosh Chaudhury",
                placeholder="e.g., Soumit Ghosh Chaudhury or cardio_desk",
            )
            staff_pw_input = st.text_input(
                "Password",
                type="password",
                value="",
                placeholder="Enter password (1234)",
            )
            login_submitted = st.form_submit_button("Sign In to Department Workspace", type="primary", use_container_width=True)

            if login_submitted:
                authed = authenticate_staff(staff_id_input, staff_pw_input)
                if authed:
                    st.session_state["auth_user"] = authed
                    st.session_state["active_department"] = authed["department"]
                    st.session_state["shared_chat_imported"] = False
                    st.session_state.pop("active_session_id", None)
                    st.session_state.pop("messages", None)
                    st.rerun()
                else:
                    st.error("Invalid Staff Name/ID or Password.")

    with col_dir:
        st.subheader("Hospital Test Staff Directory (Password: `1234`)")
        st.caption("Sign in as Admin (`Soumit Ghosh Chaudhury`) for full multi-department switching, or test strict department lock with any desk ID below:")
        staff_rows = list_all_staff_directory()
        st.dataframe(
            [
                {
                    "Staff Name": r["full_name"],
                    "Desk ID": r["username"],
                    "Department": r["department"],
                    "Access Level": "All Departments (Admin)" if r["is_admin"] else "Department Locked",
                }
                for r in staff_rows
            ],
            use_container_width=True,
            hide_index=True,
        )
    st.stop()

# 17. Authenticated Session Initialization (Scoped to Staff User & Active Department)
current_user = get_active_user()
active_dept = get_active_department()
query_params = st.query_params

if "shared_chat_imported" not in st.session_state:
    st.session_state.shared_chat_imported = False

if not st.session_state.shared_chat_imported:
    shared_token = query_params.get("share")
    chat_param = query_params.get("chat")
    if shared_token:
        imported_id = import_shared_token_if_needed(shared_token, current_user["username"], active_dept)
        if imported_id:
            st.session_state.active_session_id = imported_id
            st.session_state.messages = load_session_messages(imported_id)
        st.session_state.shared_chat_imported = True
    elif chat_param and session_exists_for_scope(chat_param, current_user["username"], active_dept):
        st.session_state.active_session_id = chat_param
        st.session_state.messages = load_session_messages(chat_param)
        st.session_state.shared_chat_imported = True

if "active_session_id" not in st.session_state:
    dept_sessions = list_chat_sessions(current_user["username"], active_dept)
    st.session_state.active_session_id = (
        dept_sessions[0]["id"]
        if dept_sessions
        else create_chat_session(current_user["username"], active_dept, "New Healthcare Chat")
    )

if "messages" not in st.session_state:
    st.session_state.messages = load_session_messages(st.session_state.active_session_id)

if "live_mode_active" not in st.session_state:
    st.session_state.live_mode_active = False

if "live_turn_counter" not in st.session_state:
    st.session_state.live_turn_counter = 0

if "share_link_for_session" not in st.session_state:
    st.session_state.share_link_for_session = None

if "mute_current_audio" not in st.session_state:
    st.session_state.mute_current_audio = False

if "selected_payment_mode" not in st.session_state:
    st.session_state.selected_payment_mode = PAYMENT_MODES[0]

# 18. Sidebar: Staff/Department Badge, Chat History (⋮ Menu), Payment Mode, Voice & Logout
with st.sidebar:
    st.markdown(f"**👤 {current_user['full_name']}**")
    st.caption(f"{current_user['role']}")

    if is_admin_or_central():
        selected_dept = st.selectbox(
            "🏥 Active Department",
            options=HOSPITAL_DEPARTMENTS,
            index=HOSPITAL_DEPARTMENTS.index(active_dept) if active_dept in HOSPITAL_DEPARTMENTS else 0,
        )
        if selected_dept != active_dept:
            st.session_state["active_department"] = selected_dept
            st.session_state["share_link_for_session"] = None
            dept_sessions = list_chat_sessions(current_user["username"], selected_dept)
            new_sid = (
                dept_sessions[0]["id"]
                if dept_sessions
                else create_chat_session(current_user["username"], selected_dept, "New Healthcare Chat")
            )
            st.session_state.active_session_id = new_sid
            st.session_state.messages = load_session_messages(new_sid)
            st.query_params["chat"] = new_sid
            st.rerun()
    else:
        st.info(f"🔒 Department: **{active_dept}**")

    if st.button("➕ Create New Chat", use_container_width=True, type="primary"):
        new_id = create_chat_session(current_user["username"], active_dept, "New Healthcare Chat")
        st.session_state.active_session_id = new_id
        st.session_state.messages = []
        st.session_state.share_link_for_session = None
        st.session_state.live_turn_counter += 1
        st.query_params["chat"] = new_id
        st.rerun()

    st.markdown("### Chat History")
    sessions = list_chat_sessions(current_user["username"], active_dept)
    bg_reg = _get_background_turn_registry()
    for s in sessions:
        sid = s["id"]
        is_current = sid == st.session_state.active_session_id
        is_pinned = bool(s["pinned"])
        running_badge = "⏳ " if bg_reg.get(sid, {}).get("status") == "running" else ""

        col_title, col_menu = st.columns([0.82, 0.18])
        with col_title:
            pin_prefix = "📌 " if is_pinned else ""
            active_prefix = "▸ " if is_current else ""
            if st.button(f"{active_prefix}{running_badge}{pin_prefix}{s['title']}", key=f"open_{sid}", use_container_width=True):
                st.session_state.active_session_id = sid
                st.session_state.messages = load_session_messages(sid)
                st.session_state.live_turn_counter += 1
                st.query_params["chat"] = sid
                st.rerun()

        with col_menu:
            with st.popover("⋮", use_container_width=True):
                pin_label = "Unpin from Top" if is_pinned else "Pin to Top"
                if st.button(pin_label, key=f"menu_pin_{sid}", use_container_width=True):
                    toggle_pin_chat_session(sid)
                    st.rerun()

                if st.button("Share Link", key=f"menu_share_{sid}", use_container_width=True):
                    token = build_share_token(sid)
                    st.session_state.share_link_for_session = (sid, s["title"], token)

                if len(sessions) > 1:
                    if st.button("Delete", key=f"menu_del_{sid}", use_container_width=True):
                        delete_chat_session(sid)
                        if st.session_state.share_link_for_session and st.session_state.share_link_for_session[0] == sid:
                            st.session_state.share_link_for_session = None
                        remaining = list_chat_sessions(current_user["username"], active_dept)
                        st.session_state.active_session_id = remaining[0]["id"]
                        st.session_state.messages = load_session_messages(st.session_state.active_session_id)
                        st.query_params["chat"] = st.session_state.active_session_id
                        st.rerun()

    if st.session_state.share_link_for_session:
        _, shared_title, share_token = st.session_state.share_link_for_session
        st.caption(f"Share link for **{shared_title}**:")
        st.code(f"https://sisterai.streamlit.app/?share={share_token}", language="text")

    st.divider()
    st.selectbox(
        "💳 Payment Mode",
        options=PAYMENT_MODES,
        key="selected_payment_mode",
    )
    enable_voice_out = st.toggle("🔊 Enable Voice Replies", value=False)
    live_toggle = st.toggle("🎙️ Live Conversation Mode", value=st.session_state.live_mode_active)
    if live_toggle != st.session_state.live_mode_active:
        st.session_state.live_mode_active = live_toggle
        st.session_state.mute_current_audio = False

    st.divider()
    if st.button("🚪 Sign Out / Switch Staff", use_container_width=True):
        st.session_state["auth_user"] = None
        st.session_state.pop("active_session_id", None)
        st.session_state.pop("messages", None)
        st.session_state["share_link_for_session"] = None
        st.rerun()

# 19. Main Header + Live Departmental KPIs + Collapsible SQLite Operations & Audit Drawer
st.title("🩺 SisterAI")
st.caption(f"Your Personal Medical Assistant • {active_dept}")

dept_snapshot = fetch_department_dashboard_snapshot(active_dept)
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("📅 Dept Bookings", dept_snapshot["total_appts"])
k2.metric("✅ Paid Bills", dept_snapshot["paid_count"])
k3.metric("⏳ Pay-Later (<13)", dept_snapshot["unpaid_count"])
k4.metric("🔁 Cross-Dept Referrals", dept_snapshot["referral_count"])
k5.metric("⚠️ Open Dept Incidents", dept_snapshot["open_incidents"])

with st.expander(f"📊 Live {active_dept} Queue, Referrals & SQLite Audit Log", expanded=False):
    tab_appts, tab_refs, tab_inc = st.tabs([
        f"Appointments & Billing ({dept_snapshot['total_appts']})",
        f"Cross-Dept Referrals ({dept_snapshot['referral_count']})",
        f"Open Incidents ({dept_snapshot['open_incidents']})",
    ])

    with tab_appts:
        if dept_snapshot["appointments"]:
            st.dataframe(dept_snapshot["appointments"], use_container_width=True, hide_index=True)
            csv_bytes = _rows_to_csv_bytes(dept_snapshot["appointments"])
            safe_dept_slug = re.sub(r"[^A-Za-z0-9]+", "_", active_dept).strip("_")
            st.download_button(
                label=f"⬇️ Export {active_dept} Schedule & Billing CSV",
                data=csv_bytes,
                file_name=f"{safe_dept_slug}_Schedule_Billing.csv",
                mime="text/csv",
                key=f"export_csv_{safe_dept_slug}",
            )
        else:
            st.caption(f"No appointments recorded yet in {active_dept}.")

    with tab_refs:
        if dept_snapshot["referrals"]:
            st.dataframe(dept_snapshot["referrals"], use_container_width=True, hide_index=True)
        else:
            st.caption(f"No incoming or outgoing referrals for {active_dept} yet.")

    with tab_inc:
        if dept_snapshot["incidents"]:
            st.dataframe(dept_snapshot["incidents"], use_container_width=True, hide_index=True)
        else:
            st.caption(f"No open operational or RCM incidents logged for {active_dept}.")

# Live Spoken Conversation Dock
if st.session_state.live_mode_active:
    with st.container(border=True):
        hdr_l, hdr_r = st.columns([0.78, 0.22])
        with hdr_l:
            st.markdown("#### 🎙️ Live Spoken Conversation")
            st.caption("Speak directly to SisterAI—she answers back by voice in real time.")
        with hdr_r:
            if st.button("🔇 Mute Voice", key="mute_live_btn", use_container_width=True):
                st.session_state.mute_current_audio = True
            elif st.session_state.mute_current_audio and st.button("🔊 Unmute", key="unmute_live_btn", use_container_width=True):
                st.session_state.mute_current_audio = False

        live_audio = st.audio_input(
            "Speak your next turn...",
            key=f"live_voice_{st.session_state.live_turn_counter}",
        )
        if live_audio is not None:
            live_bytes = live_audio.getvalue()
            if live_bytes:
                st.session_state.mute_current_audio = False
                st.session_state.live_turn_counter += 1
                run_sister_turn(
                    session_id=st.session_state.active_session_id,
                    display_label="🎙️ [Live Spoken Turn]",
                    turn_inputs=[
                        types.Part.from_bytes(data=live_bytes, mime_type="audio/wav"),
                        "Respond directly in natural spoken conversation to what I just said.",
                    ],
                    enable_voice_out=True,
                    live_voice_mode=True,
                )

# Display Active Chat History
for idx, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("audio") is not None:
            if isinstance(msg["audio"], (bytes, bytearray)):
                st.audio(io.BytesIO(msg["audio"]), format="audio/mp3")
            else:
                msg["audio"].seek(0)
                st.audio(msg["audio"], format="audio/mp3")
        if msg.get("bill_pdf_bytes") and msg.get("bill_filename"):
            st.download_button(
                label=f"🖨️ Download Printable PDF Bill ({msg['bill_filename']})",
                data=msg["bill_pdf_bytes"],
                file_name=msg["bill_filename"],
                mime="application/pdf",
                key=f"hist_bill_{st.session_state.active_session_id}_{idx}_{msg['bill_filename']}",
            )

# Integrated Chat + Voice + File/Image Input Box
submission = st.chat_input(
    f"Type, speak, register/schedule in {active_dept}, refer across depts, or attach files/images...",
    accept_audio=True,
    accept_file="multiple",
    file_type=["png", "jpg", "jpeg", "webp", "pdf", "txt", "csv", "md"],
)

if submission:
    st.session_state.mute_current_audio = False
    typed_text = submission.text.strip() if submission.text else ""
    audio_bytes = submission.audio.read() if getattr(submission, "audio", None) is not None else None
    attached_files = list(submission.files) if getattr(submission, "files", None) else []

    badges = []
    if audio_bytes:
        badges.append("🎙️ Voice")
    if attached_files:
        names = ", ".join(f.name for f in attached_files)
        badges.append(f"📎 {names}")
    if typed_text:
        badges.append(typed_text)

    if badges:
        user_display = " • ".join(badges)
        turn_inputs: list = []
        if audio_bytes:
            turn_inputs.append(types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"))
        if attached_files:
            turn_inputs.extend(build_file_parts(attached_files))
        if typed_text:
            turn_inputs.append(typed_text)
        elif audio_bytes or attached_files:
            turn_inputs.append(
                f"Analyze the attached audio, image, or document for {active_dept} operations, "
                "clinical context, medical literature/news, pharma, registration/scheduling, or hospital business/RCM."
            )

        run_sister_turn(
            session_id=st.session_state.active_session_id,
            display_label=user_display,
            turn_inputs=turn_inputs,
            enable_voice_out=enable_voice_out,
            live_voice_mode=st.session_state.live_mode_active,
        )
