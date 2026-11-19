# SisterAI — Enterprise Departmental Healthcare Assistant

Multi-department healthcare operations, patient registration & auto-UHID intake, payment mode branching, printable PDF billing, cross-department referral handoffs, clinical/imaging protocols, OpenFDA drug lookup, PubMed & healthcare news search, and voice/multimodal workflows built with **Python**, **Streamlit**, **SQLite**, and **Google GenAI**.

## Demo Login Credentials (Password: `1234`)
- **Enterprise Hospital Admin:** `Soumit Ghosh Chaudhury` (`soumit`) — Full access across all 13 hospital departments
- **Cardiology Desk:** `Dr. K. Chatterjee` (`cardio_desk`) — Locked to Cardiology
- **Ophthalmology Desk:** `Priya Nair` (`opto_desk`) — Locked to Ophthalmology
- **Radiology Desk:** `Ananya Das` (`rad_desk`) — Locked to Radiology & Medical Imaging

## Local Setup
```bash
git clone [https://github.com/soumitanalytics/SisterAI.git](https://github.com/soumitanalytics/SisterAI.git)
cd SisterAI
pip install -r requirements.txt
```
Copy `.env.example` to `.env`, add your `GEMINI_API_KEY`, and run:
```bash
streamlit run app.py
```
