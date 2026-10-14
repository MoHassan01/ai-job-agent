# 🤖 Autonomous AI Job Application Agent (Edge-Deployed on Raspberry Pi)

An end-to-end, human-in-the-loop AI agent that automates the job application workflow via Telegram. Hosted 24/7 on a headless Raspberry Pi, the system scrapes job postings, synthesizes tailored cover letters and WhatsApp pitches using Google Gemini LLMs, stages drafts with PDF CV attachments via the Gmail API, and tracks all applications in a local SQLite database.

## ✨ Key Features
- **Omnichannel Input:** Accepts raw job descriptions or LinkedIn URLs (bypassing anti-bot walls via `r.jina.ai` Markdown extraction).
- **Structured LLM Routing:** Uses Gemini (`google-genai`) with automatic multi-model fallback and exponential backoff to extract metadata (company, role, contact method) and generate zero-placeholder drafts.
- **Human-in-the-Loop Telegram UI:** Interactive inline keyboards to preview, manually edit, AI-revise, or 1-tap send Gmail drafts directly from Telegram.
- **Automated WhatsApp Deep-Linking:** Generates pre-encoded `wa.me` links for recruiters accepting WhatsApp applications.
- **SQLite Application Tracker:** Logs every application lifecycle (`DRAFTED`, `SENT`, `DISCARDED`), detects duplicate applications to the same recruiter, verifies Gmail delivery IDs, and exports history to CSV (`/history`, `/stats`, `/export`).
- **Self-Healing Edge Deployment:** Managed via Linux `systemd` with `curl` status hooks and a cron network watchdog for automatic recovery after power or ISP drops.

## 🏗️ Architecture
1. **Trigger:** User sends a LinkedIn URL or job text to the Telegram Bot (secured via Telegram ID whitelisting).
2. **Ingestion:** URLs are parsed into clean Markdown via Jina Reader; text is passed to the Gemini LLM pipeline.
3. **Synthesis:** Gemini outputs a strict JSON payload containing tailored Email and/or WhatsApp copy.
4. **Staging & Persistence:** A MIME multipart draft with the user's latest PDF CV is created via the Gmail OAuth2 API and logged in SQLite.
5. **Action:** User reviews the preview in Telegram, optionally requests AI revisions, and dispatches the email with a single tap—receiving an instant Gmail Message ID delivery receipt.

## 🚀 Quick Start
1. Clone the repository and create a virtual environment:
   ```bash
   git clone https://github.com/MoHassan01/autonomous-job-agent
   cd autonomous-job-agent
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt