# MoneyTrail

MoneyTrail is a local-first personal finance dashboard for importing bank statements, categorizing transactions, tracking cash expenses, and generating spending insights.

## Features

- CSV and digital PDF statement import
- Scanned PDF extraction with Tesseract OCR
- Password-protected PDF support
- Rule-based merchant categorization
- Manual expense capture
- SQLite persistence
- Pandas spending analysis
- Isolation Forest anomaly detection
- Optional Groq-powered financial explanations
- Confirmed database reset from the dashboard

## Technology

| Layer | Technologies |
| --- | --- |
| Frontend | React 19, Vite, JavaScript, CSS |
| Backend | Python, FastAPI, Uvicorn, Pydantic |
| Analysis | Pandas, scikit-learn |
| Documents | pdfplumber, pytesseract, Tesseract OCR |
| AI | Groq API |
| Database | SQLite |

## Prerequisites

- Python 3.10 or newer
- Node.js 18 or newer with npm
- Tesseract OCR for scanned PDFs
- Groq API key for AI insights; optional for local analysis

## Installation

### Backend

From the repository root:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
source .venv/bin/activate
```

Install the pinned Python dependencies:

```bash
python -m pip install -r Backend/requirements.txt
```

Create `Backend/.env` from the example file and set the optional integrations:

```bash
cp Backend/.env.example Backend/.env
```

Windows PowerShell equivalent:

```powershell
Copy-Item Backend/.env.example Backend/.env
```

```env
GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-20b
TESSERACT_CMD=
```

`TESSERACT_CMD` is only required when the Tesseract executable is not available on PATH. On Windows it commonly points to `C:/Program Files/Tesseract-OCR/tesseract.exe`.

Start the backend from its directory:

```bash
cd Backend
uvicorn app.main:app --reload
```

The API runs at `http://127.0.0.1:8000`. API documentation is available at `http://127.0.0.1:8000/docs`.

### Frontend

Open a second terminal at the repository root:

```bash
cd Frontend
npm install
npm run dev
```

The dashboard runs at `http://127.0.0.1:5173`.

## Tesseract OCR

Tesseract is an operating-system dependency and is not installed by `requirements.txt`.

- Windows:`winget install UB-Mannheim.TesseractOCR` 
- macOS: `brew install tesseract`
- Debian/Ubuntu: `sudo apt install tesseract-ocr`

Restart the terminal after changing PATH. If the executable is still not discoverable, set `TESSERACT_CMD` in `Backend/.env`.

## Supported statements

CSV files should contain `date`, `description` or `merchant`, and `amount` columns. Optional columns include `payment_method`, `debit`, and `withdrawal`.

Digital PDFs are processed with `pdfplumber`. Scanned PDFs are rendered and processed with Tesseract. Bank statement layouts vary, so imported transactions should be reviewed.

## Data and privacy

The SQLite database is created at `Backend/moneytrail.db` and is ignored by Git. The dashboard's **Reset all data** action removes all transactions and resets the database sequence without removing the application configuration.

Groq analysis is optional. When enabled, only aggregated category totals, total spending, average transaction amount, and an anomaly count are sent to Groq. Merchant names, descriptions, dates, reference numbers, account details, and raw transaction records are excluded. Keep API keys in `Backend/.env`; never place them in frontend code or commit them to the repository.

## Development checks

```bash
cd Frontend
npm run lint
npm run build

cd ../Backend
python -m py_compile app/main.py
```
