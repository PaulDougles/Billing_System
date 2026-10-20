# Invoice Management System

A comprehensive **Flask-based Invoice Management System** with multi-role workflow for time-based billing, approval processes, and automated invoicing.

---

## 🚀 Features

- **Multi-Role System**: BU Users, Supervisors, Head of IT, Finance Controller, Payment Team, Record Assistant
- **4-Week Cycle Management**: Automated billing cycles with configurable auto-submit dates
- **Approval Workflow**: Supervisor → Head of IT → Finance Controller → Payment Team
- **Email Notifications**: OTP-based login, approval requests, invoice generation
- **User Onboarding Emails**: Welcome emails are attempted for users created individually or through bulk upload; bulk-upload results show each delivery outcome
- **Invoice Generation**: PDF invoices with professional formatting (ReportLab)
- **Excel Reports**: Export data with custom templates
- **Document Management**: Upload and manage supporting documents
- **AI Revenue Forecasting**: Statistical predictions using ARIMA/Exponential Smoothing
- **Comprehensive Audit Logs**: Track all system activities

---

## Database Configuration

Local development uses SQLite by default. Set `FLASK_CONFIG=development` in
`.env` and run the app with the project virtual environment. The database is
stored at `instance/invoice_system.db`; a PostgreSQL `DATABASE_URL` in `.env`
does not change the development database. To use a different database while
developing, set `DEV_DATABASE_URL`.

For UAT/production, set `FLASK_CONFIG=production` and provide
`DATABASE_URL` with the PostgreSQL connection string through the deployment
environment. Do not use the local SQLite database for UAT.

---

## 🛠️ Technology Stack

| Component | Technology |
|-----------|------------|
| Backend | Flask 2.3.3 |
| Database | PostgreSQL / SQLite |
| ORM | SQLAlchemy 2.0 |
| Authentication | OTP-based (Flask-Login) |
| Email | Flask-Mail |
| PDF Generation | ReportLab |
| Excel Export | OpenPyXL, Pandas |
| Scheduling | APScheduler |
| Frontend | Bootstrap 5, Jinja2, Chart.js |
| Forecasting | Statsmodels, NumPy |

---

## 📋 System Requirements

- Python 3.8+
- PostgreSQL (recommended for production) or SQLite (development)
- SMTP Server (for email notifications)

---

## Company Classification and Invoice API

On startup, the application applies additive schema updates for company classification and legacy invoice fields. Existing `LOLC Technologies` is used only for the one-time initial parent-company classification; update company types later from **Admin → Companies**. Choose **Parent** for a payment user who can view details for every company, or **Other** for a payment user restricted to their assigned company. Parent-company items route to the LOLC Technologies payment team; Other-company items route to the company payment team. The former Finance company type is treated as Other; finance-controller approval remains a separate workflow role.

The **Admin → Companies → Bulk Create** Excel template includes a `company_type` dropdown with **Parent** and **Other** options. If the column is omitted or left blank, the company is created as Other.

Service-item invoice records and PDFs can be handled separately:

1. `POST /api/payment/service-items/<service_item_id>/generate-invoice` creates the invoice number and records invoice activity.
2. `POST /api/invoices/<service_item_id>/generate-pdf` generates and stores the PDF, returning it as a download.
3. `GET /api/invoices/<service_item_id>/download-pdf` downloads a previously stored PDF.

These PDF endpoints use `ServiceItem.id`; legacy time-entry and consolidated invoice endpoints remain available for compatibility.

---
