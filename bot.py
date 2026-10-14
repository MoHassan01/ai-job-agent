import os
import time
import base64
import json
import urllib.parse
import html
import requests
import re
import sqlite3
import csv
from datetime import datetime
from email.message import EmailMessage
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google import genai

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LLM_API_KEY = os.getenv("LLM_API_KEY")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
GITHUB_URL = os.getenv("GITHUB_URL", "")
LINKEDIN_URL = os.getenv("LINKEDIN_URL", "")

SCOPES = ['https://www.googleapis.com/auth/gmail.compose']
CV_FILENAME = "resume.pdf"
DB_FILENAME = "applications.db"

FALLBACK_MODELS = [
    'gemini-3.8-flash',
    'gemini-3.6-flash',
    'gemini-3.1-pro-preview',
    'gemini-2.5-flash'
]

# --- Database Management (SQLite) ---
def init_db():
    with sqlite3.connect(DB_FILENAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT,
                sent_at TEXT,
                company TEXT,
                role TEXT,
                method TEXT,
                recipient TEXT,
                subject TEXT,
                status TEXT,
                gmail_message_id TEXT,
                source_url TEXT
            )
        """)
        conn.commit()

def log_application(company, role, method, recipient, subject, status="DRAFTED", source_url=""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_FILENAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO applications (created_at, company, role, method, recipient, subject, status, source_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (now, company, role, method, recipient, subject, status, source_url))
        conn.commit()
        return cursor.lastrowid

def update_application_record(app_id, status=None, gmail_message_id=None, recipient=None, subject=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_FILENAME) as conn:
        cursor = conn.cursor()
        if status == "SENT":
            cursor.execute("""
                UPDATE applications 
                SET status = ?, sent_at = ?, gmail_message_id = COALESCE(?, gmail_message_id)
                WHERE id = ?
            """, (status, now, gmail_message_id, app_id))
        elif status:
            cursor.execute("UPDATE applications SET status = ? WHERE id = ?", (status, app_id))
            
        if recipient is not None or subject is not None:
            cursor.execute("""
                UPDATE applications 
                SET recipient = COALESCE(?, recipient), subject = COALESCE(?, subject)
                WHERE id = ?
            """, (recipient, subject, app_id))
        conn.commit()

def check_duplicate_application(recipient):
    if not recipient:
        return None
    with sqlite3.connect(DB_FILENAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT company, role, sent_at FROM applications 
            WHERE LOWER(recipient) = LOWER(?) AND status = 'SENT'
            ORDER BY id DESC LIMIT 1
        """, (recipient.strip(),))
        return cursor.fetchone()

def get_application_stats():
    with sqlite3.connect(DB_FILENAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM applications WHERE status = 'SENT'")
        total_sent = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM applications WHERE status = 'SENT' AND method = 'EMAIL'")
        email_sent = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM applications WHERE status = 'SENT' AND method = 'WHATSAPP'")
        wa_sent = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM applications WHERE status = 'DRAFTED'")
        drafts = cursor.fetchone()[0]
        return total_sent, email_sent, wa_sent, drafts

def get_recent_applications(limit=10):
    with sqlite3.connect(DB_FILENAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, COALESCE(sent_at, created_at), company, role, method, recipient, status 
            FROM applications 
            ORDER BY id DESC LIMIT ?
        """, (limit,))
        return cursor.fetchall()

def export_to_csv(filepath="applications_export.csv"):
    with sqlite3.connect(DB_FILENAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM applications ORDER BY id DESC")
        rows = cursor.fetchall()
        headers = [description[0] for description in cursor.description]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    return filepath

# --- Link Extraction ---
def extract_linkedin_post(url):
    jina_url = f"https://r.jina.ai/{url}"
    headers = {
        "Accept": "text/plain",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    try:
        response = requests.get(jina_url, headers=headers, timeout=25)
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestException as e:
        raise Exception(f"Failed to read link: {e}")

# --- Gmail Auth & Operations ---
def get_gmail_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('pi_python_creds.json', SCOPES)
            creds = flow.run_local_server(port=8080, open_browser=False)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

def build_raw_email(subject, body, to_email=""):
    message = EmailMessage()
    message.set_content(body)
    message['To'] = to_email
    message['From'] = 'me'
    message['Subject'] = subject

    if os.path.exists(CV_FILENAME):
        with open(CV_FILENAME, 'rb') as f:
            cv_data = f.read()
        message.add_attachment(
            cv_data, 
            maintype='application', 
            subtype='pdf', 
            filename='Mohamed_Hassan_CV.pdf'
        )

    return base64.urlsafe_b64encode(message.as_bytes()).decode()

def create_gmail_draft(service, subject, body, to_email=""):
    encoded_message = build_raw_email(subject, body, to_email)
    create_message = {'message': {'raw': encoded_message}}
    return service.users().drafts().create(userId="me", body=create_message).execute()

def update_gmail_draft(service, draft_id, subject, body, to_email=""):
    encoded_message = build_raw_email(subject, body, to_email)
    update_body = {'id': draft_id, 'message': {'raw': encoded_message}}
    return service.users().drafts().update(userId="me", id=draft_id, body=update_body).execute()

def send_gmail_draft(service, draft_id):
    return service.users().drafts().send(userId="me", body={'id': draft_id}).execute()

def delete_gmail_draft(service, draft_id):
    return service.users().drafts().delete(userId="me", id=draft_id).execute()

# --- LLM Integration ---
def call_gemini_with_fallback(prompt):
    client = genai.Client(api_key=LLM_API_KEY)
    errors = []
    
    for model_name in FALLBACK_MODELS:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                return response.text
            except Exception as e:
                err_str = str(e)
                errors.append(f"{model_name} (try {attempt+1}): {err_str}")
                if any(code in err_str for code in ["503", "429", "500", "504"]):
                    time.sleep(4 * (attempt + 1))
                else:
                    break 
                    
    raise Exception(f"All models failed. Details: {' | '.join(errors[-2:])}")

def generate_cover_letter(job_description):
    portfolio_context = ""
    if GITHUB_URL:
        portfolio_context += f"\n    - GitHub Portfolio: {GITHUB_URL}"
    if LINKEDIN_URL:
        portfolio_context += f"\n    - LinkedIn Profile: {LINKEDIN_URL}"

    prompt = f"""
    You are an expert executive assistant drafting an application for Mohamed Hassan, an AI & ML Engineer.
    
    Mohamed's Background & Context:
    - Worked as AI Engineer at Masarat Misr, specializing in building production-ready RAG architectures, LLM automation tools (LangChain), and end-to-end ML solutions.
    - Strong background in Computer Vision, having deployed real-time automated license plate recognition using Ultralytics YOLOv11 and OpenCV, as well as ID card recognition pipelines using Tesseract OCR.
    - Tech stack includes Python, PyTorch, TensorFlow, FastAPI, Docker, SQL, and Streamlit.
    - Uniquely combines a Mechanical Engineering foundation with advanced Computer Science study (currently pursuing a Postgraduate Diploma at Cairo University) to design efficient, scalable system architectures.{portfolio_context}
    
    Job Description: 
    {job_description}
    
    Task: Analyze the job description to extract metadata and draft tailored application messages.
    
    CRITICAL INSTRUCTIONS:
    - NEVER use placeholders like [Insert Link], [GitHub], or [Company Name]. 
    - Do not include a GitHub or LinkedIn URL unless explicitly provided in the context above.
    - Output the final, ready-to-send text ONLY.
    
    OUTPUT REQUIREMENT: 
    Return EXACTLY a valid JSON object with the following structure, and NO other text or markdown formatting:
    {{
        "company_name": "extracted company name or 'Unknown Company'",
        "job_title": "extracted job role/title or 'AI/ML Engineer'",
        "application_method": "email", "whatsapp", or "both",
        "email_address": "extracted email or null",
        "whatsapp_number": "extracted phone number (digits only, include country code if present) or null",
        "email_subject": "tailored email subject line or null",
        "email_body": "body text or null",
        "whatsapp_message": "whatsapp text or null"
    }}
    """
    return call_gemini_with_fallback(prompt)

def revise_email_body(current_body, user_instructions):
    prompt = f"""
    You are editing a job application email for Mohamed Hassan (AI & ML Engineer).
    
    Current Email Draft:
    {current_body}
    
    User's Revision Instructions:
    {user_instructions}
    
    Rules:
    - Apply the requested changes cleanly while keeping a concise, professional tone.
    - NEVER use placeholders or brackets like [Insert Link].
    - Return ONLY the revised email body text, with no commentary or markdown blocks.
    """
    return call_gemini_with_fallback(prompt).strip()

# --- UI Helpers ---
def get_draft_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("🚀 Send Email", callback_data="action_send"),
            InlineKeyboardButton("🗑️ Discard", callback_data="action_discard"),
        ],
        [
            InlineKeyboardButton("✏️ Edit Manually", callback_data="action_edit_body"),
            InlineKeyboardButton("🤖 AI Revise", callback_data="action_edit_ai"),
        ],
        [
            InlineKeyboardButton("📧 Edit Recipient / Subject", callback_data="action_edit_meta"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def format_draft_preview(draft_info, header="📝 <b>Email Draft Created!</b>"):
    company = draft_info.get("company") or "Unknown Company"
    role = draft_info.get("role") or "AI/ML Role"
    to_email = draft_info.get("to_email") or ""
    subject = draft_info.get("subject") or ""
    body = draft_info.get("body") or ""
    draft_id = draft_info.get("draft_id") or ""
    cv_status = "📎 CV attached" if os.path.exists(CV_FILENAME) else "⚠️ No CV found"

    dup_warning = ""
    dup = check_duplicate_application(to_email)
    if dup:
        dup_company, dup_role, dup_date = dup
        dup_warning = f"⚠️ <b>DUPLICATE WARNING:</b> You already sent an application to <code>{html.escape(to_email)}</code> on {dup_date} ({html.escape(dup_role)} at {html.escape(dup_company)})!\n\n"

    return (
        f"{header}\n\n"
        f"{dup_warning}"
        f"🏢 <b>Company:</b> {html.escape(company)}\n"
        f"💼 <b>Role:</b> {html.escape(role)}\n"
        f"📧 <b>To:</b> {html.escape(to_email) if to_email else '<i>[Missing - Tap Edit Recipient]</i>'}\n"
        f"📌 <b>Subject:</b> {html.escape(subject)}\n"
        f"🗂️ <b>Status:</b> {cv_status} (Draft ID: <code>{draft_id}</code>)\n\n"
        f"<b>--- Draft Preview ---</b>\n"
        f"<i>{html.escape(body)}</i>"
    )

# --- Telegram Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ALLOWED_USER_ID:
        return
    msg = (
        "🤖 <b>Autonomous Job Application Agent</b>\n\n"
        "• Send a <b>LinkedIn link</b> or <b>job text</b> to generate tailored drafts.\n"
        "• Upload a <b>PDF</b> to update your CV.\n\n"
        "📊 <b>Tracking Commands:</b>\n"
        "/history - View your last 10 applications\n"
        "/stats - View total application metrics\n"
        "/export - Download full application history as CSV\n"
        "/cancel - Exit draft edit mode"
    )
    await update.message.reply_text(msg, parse_mode="HTML")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ALLOWED_USER_ID:
        return
    rows = get_recent_applications(limit=10)
    if not rows:
        await update.message.reply_text("📭 No applications logged yet.")
        return

    text = "📋 <b>Last 10 Applications:</b>\n\n"
    status_icons = {"SENT": "✅", "DRAFTED": "📝", "DISCARDED": "🗑️"}
    for app_id, date_str, company, role, method, recipient, status in rows:
        icon = status_icons.get(status, "🔹")
        short_date = date_str[:16] if date_str else "N/A"
        text += (
            f"{icon} <b>#{app_id} | {html.escape(role or 'Role')}</b> @ {html.escape(company or 'Company')}\n"
            f"   └ {method} to <code>{html.escape(recipient or 'N/A')}</code> ({status} - {short_date})\n\n"
        )
    await update.message.reply_text(text.strip(), parse_mode="HTML")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ALLOWED_USER_ID:
        return
    total_sent, email_sent, wa_sent, drafts = get_application_stats()
    text = (
        "📊 <b>Application Pipeline Metrics</b>\n\n"
        f"🚀 <b>Total Applications Sent:</b> {total_sent}\n"
        f"   ├ 📧 Via Email: {email_sent}\n"
        f"   └ 📱 Via WhatsApp: {wa_sent}\n\n"
        f"📝 <b>Pending Drafts:</b> {drafts}"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ALLOWED_USER_ID:
        return
    filepath = export_to_csv()
    await update.message.reply_document(
        document=open(filepath, "rb"),
        filename=f"job_applications_{datetime.now().strftime('%Y%m%d')}.csv",
        caption="📂 Full export of your job application database."
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ALLOWED_USER_ID:
        return
    context.user_data.pop("edit_mode", None)
    await update.message.reply_text("✅ Edit mode cancelled.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ALLOWED_USER_ID:
        return 
    document = update.message.document
    if document.mime_type == 'application/pdf':
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(CV_FILENAME)
        await update.message.reply_text("✅ Your CV has been updated on the server and will be attached to future drafts!")
    else:
        await update.message.reply_text("Please upload your CV strictly in PDF format.")

# --- Callback Query Handler (Buttons) ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ALLOWED_USER_ID:
        await query.answer("Unauthorized.", show_alert=True)
        return

    await query.answer()
    action = query.data

    # Handle WhatsApp "Mark as Sent" button
    if action.startswith("wa_sent:"):
        app_id = int(action.split(":")[1])
        update_application_record(app_id, status="SENT")
        total_sent, _, _, _ = get_application_stats()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"✅ <b>WhatsApp Application #{app_id} logged as SENT!</b>\n"
            f"🏆 Total Applications Completed: <b>{total_sent}</b>",
            parse_mode="HTML"
        )
        return

    draft_info = context.user_data.get("current_draft")
    if not draft_info:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⚠️ This draft session expired from memory. Please resend the job post.")
        return

    if action == "action_send":
        to_email = (draft_info.get("to_email") or "").strip()
        if not to_email:
            await query.message.reply_text(
                "⚠️ <b>Cannot send yet:</b> Recipient email ('To') is empty!\n"
                "Tap <b>📧 Edit Recipient / Subject</b> first.",
                parse_mode="HTML"
            )
            return
        try:
            service = get_gmail_service()
            sent_response = send_gmail_draft(service, draft_info["draft_id"])
            gmail_msg_id = sent_response.get("id", "Verified")
            sent_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Log to SQLite
            update_application_record(
                draft_info["app_id"],
                status="SENT",
                gmail_message_id=gmail_msg_id
            )
            total_sent, _, _, _ = get_application_stats()

            context.user_data.pop("current_draft", None)
            context.user_data.pop("edit_mode", None)
            
            receipt_text = (
                f"✅ <b>EMAIL SENT SUCCESSFULLY!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏢 <b>Company:</b> {html.escape(draft_info.get('company', 'N/A'))}\n"
                f"💼 <b>Role:</b> {html.escape(draft_info.get('role', 'N/A'))}\n"
                f"📧 <b>Recipient:</b> <code>{html.escape(to_email)}</code>\n"
                f"📌 <b>Subject:</b> {html.escape(draft_info.get('subject', ''))}\n"
                f"🕒 <b>Sent At:</b> {sent_time}\n"
                f"🆔 <b>Gmail Msg ID:</b> <code>{gmail_msg_id}</code>\n"
                f"🏆 <b>Total Jobs Applied:</b> {total_sent}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>{html.escape(draft_info.get('body', ''))}</i>"
            )
            await query.edit_message_text(receipt_text, parse_mode="HTML")
        except Exception as e:
            await query.message.reply_text(f"❌ Failed to send email: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

    elif action == "action_discard":
        try:
            service = get_gmail_service()
            delete_gmail_draft(service, draft_info["draft_id"])
        except Exception:
            pass
        update_application_record(draft_info["app_id"], status="DISCARDED")
        context.user_data.pop("current_draft", None)
        context.user_data.pop("edit_mode", None)
        await query.edit_message_text("🗑️ <b>Draft discarded and marked as cancelled in tracker.</b>", parse_mode="HTML")

    elif action == "action_edit_body":
        context.user_data["edit_mode"] = "manual_body"
        await query.message.reply_text(
            "✏️ <b>Manual Edit Mode</b>\n"
            "Copy the text below, edit it, and send it back (or /cancel):",
            parse_mode="HTML"
        )
        await query.message.reply_text(draft_info["body"])

    elif action == "action_edit_ai":
        context.user_data["edit_mode"] = "ai_body"
        await query.message.reply_text(
            "🤖 <b>AI Revision Mode</b>\n"
            "Reply with instructions (e.g., <i>'Highlight my FastAPI experience'</i>), or /cancel:",
            parse_mode="HTML"
        )

    elif action == "action_edit_meta":
        context.user_data["edit_mode"] = "edit_meta"
        await query.message.reply_text(
            "📧 <b>Edit Recipient / Subject</b>\n"
            "• Reply with an email address (e.g., <code>hr@company.com</code>)\n"
            "• Or update both with <code>|</code>:\n"
            "<code>hr@company.com | AI Engineer - Mohamed Hassan</code>\n\n"
            "Send /cancel to abort.",
            parse_mode="HTML"
        )

# --- Main Message Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ALLOWED_USER_ID:
        return 
        
    input_text = update.message.text.strip()
    edit_mode = context.user_data.get("edit_mode")
    draft_info = context.user_data.get("current_draft")

    # 1. Handle Active Edit Mode
    if edit_mode and draft_info and not (input_text.startswith("http://") or input_text.startswith("https://")):
        try:
            if edit_mode == "manual_body":
                draft_info["body"] = input_text
            elif edit_mode == "ai_body":
                await update.message.reply_text("🤖 Revising draft with AI...")
                draft_info["body"] = revise_email_body(draft_info["body"], input_text)
            elif edit_mode == "edit_meta":
                if "|" in input_text:
                    parts = [p.strip() for p in input_text.split("|", 1)]
                    if "@" in parts[0]:
                        draft_info["to_email"] = parts[0]
                    draft_info["subject"] = parts[1]
                elif "@" in input_text and " " not in input_text:
                    draft_info["to_email"] = input_text
                else:
                    draft_info["subject"] = input_text

            service = get_gmail_service()
            updated = update_gmail_draft(
                service,
                draft_info["draft_id"],
                draft_info["subject"],
                draft_info["body"],
                draft_info["to_email"]
            )
            draft_info["draft_id"] = updated["id"]
            update_application_record(
                draft_info["app_id"],
                recipient=draft_info["to_email"],
                subject=draft_info["subject"]
            )
            context.user_data["edit_mode"] = None

            preview_text = format_draft_preview(draft_info, header="✅ <b>Draft Updated in Gmail!</b>")
            await update.message.reply_text(preview_text, parse_mode="HTML", reply_markup=get_draft_keyboard())
        except Exception as e:
            await update.message.reply_text(f"❌ Error updating draft: <code>{html.escape(str(e))}</code>", parse_mode="HTML")
        return

    # 2. Extract Job Post (Link or Raw Text)
    context.user_data.pop("edit_mode", None)
    source_url = ""

    if "http://" in input_text or "https://" in input_text:
        await update.message.reply_text("🔗 Link detected! Extracting job post...")
        urls = re.findall(r'(https?://[^\s]+)', input_text)
        source_url = urls[0] if urls else input_text
        
        try:
            job_text = extract_linkedin_post(source_url)
            if not job_text or "Failed to" in job_text:
                raise Exception(job_text)
        except Exception as e:
            error_msg = (
                "❌ <b>Extraction Failed</b>\n"
                f"Error: <code>{html.escape(str(e))}</code>\n\n"
                "👉 <b>Fallback:</b> Please copy and paste the raw job description text directly into the chat."
            )
            await update.message.reply_text(error_msg, parse_mode='HTML')
            return 
    else:
        job_text = input_text

    await update.message.reply_text("🧠 Analyzing requirements and generating tailored application...")
    
    try:
        raw_response = generate_cover_letter(job_text)
        
        clean_json = raw_response.strip()
        if clean_json.startswith("```json"):
            clean_json = clean_json[7:-3].strip()
        elif clean_json.startswith("```"):
            clean_json = clean_json[3:-3].strip()
            
        data = json.loads(clean_json)
        company = data.get("company_name") or "Unknown Company"
        role = data.get("job_title") or "AI/ML Engineer"
        
        # Handle Email Draft
        if data.get("application_method") in ["email", "both"] or data.get("email_address"):
            service = get_gmail_service()
            to_email = data.get("email_address") or ""
            subject = data.get("email_subject") or f"Application for {role} - Mohamed Hassan"
            body = data.get("email_body") or ""
            
            draft = create_gmail_draft(service, subject, body, to_email=to_email)
            app_id = log_application(company, role, "EMAIL", to_email, subject, status="DRAFTED", source_url=source_url)
            
            draft_info = {
                "app_id": app_id,
                "draft_id": draft["id"],
                "company": company,
                "role": role,
                "to_email": to_email,
                "subject": subject,
                "body": body
            }
            context.user_data["current_draft"] = draft_info
            
            email_preview = format_draft_preview(draft_info)
            await update.message.reply_text(
                email_preview,
                parse_mode='HTML',
                reply_markup=get_draft_keyboard()
            )

        # Handle WhatsApp Link
        if data.get("application_method") in ["whatsapp", "both"] or data.get("whatsapp_number"):
            phone = data.get("whatsapp_number")
            msg = data.get("whatsapp_message")
            
            if phone and msg:
                clean_phone = ''.join(filter(str.isdigit, str(phone)))
                encoded_msg = urllib.parse.quote(msg)
                wa_link = f"https://wa.me/{clean_phone}?text={encoded_msg}"
                safe_msg = html.escape(msg)
                
                wa_app_id = log_application(company, role, "WHATSAPP", clean_phone, role, status="DRAFTED", source_url=source_url)
                wa_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Mark WhatsApp as Sent", callback_data=f"wa_sent:{wa_app_id}")]
                ])
                
                wa_reply = (
                    f"📱 <b>WhatsApp Application (#{wa_app_id})</b>\n"
                    f"🏢 <b>Company:</b> {html.escape(company)} ({html.escape(role)})\n"
                    f"👉 <a href='{wa_link}'>Tap Here to Open in WhatsApp</a>\n\n"
                    f"<b>Message Preview:</b>\n<i>{safe_msg}</i>"
                )
                await update.message.reply_text(wa_reply, parse_mode='HTML', reply_markup=wa_keyboard)
        
    except json.JSONDecodeError:
        await update.message.reply_text(f"Error parsing LLM response into JSON:\n{html.escape(raw_response)}")
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {html.escape(str(e))}")

if __name__ == '__main__':
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).connect_timeout(30).read_timeout(30).write_timeout(30).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    print("Agent is listening...")
    app.run_polling()