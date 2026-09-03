import csv
import io
import re
import os
import sqlite3
import shutil
import logging
import multiprocessing
from queue import Empty
import pandas as pd
import pdfplumber
import pytesseract
from pdfminer.pdfdocument import PDFDocument, PDFPasswordIncorrect
from pdfminer.pdfparser import PDFParser
from pdfplumber.utils.exceptions import PdfminerException
from sklearn.ensemble import IsolationForest
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List
from datetime import date as date_type, datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("moneytrail")

DATABASE_PATH = Path(__file__).resolve().parent.parent / "moneytrail.db"
MAX_STATEMENT_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 25
PDF_PROCESS_TIMEOUT_SECONDS = 90


app = FastAPI(
    title="MoneyTrail API",
    description="Backend API for the MoneyTrail expense tracking application.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------
# DATA MODEL
# --------------------------------------------------

class Transaction(BaseModel):
    id: int
    amount: float
    description: str
    category: str
    payment_method: str
    date: date_type
    source: str
    reference_number: str = ""
    withdrawal_amount: float = 0
    deposit_amount: float = 0
    closing_balance: float = 0


class TransactionCreate(BaseModel):
    amount: float
    description: str
    category: str
    payment_method: str
    transaction_type: str = "Withdrawn"
    date: date_type = Field(default_factory=date_type.today)
    source: str = "manual"


class InsightRequest(BaseModel):
    question: str = "What stands out about my spending?"


# --------------------------------------------------
# PERSISTENT STORAGE
# --------------------------------------------------
def initialize_database():
    database_exists = DATABASE_PATH.exists()
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL NOT NULL, description TEXT NOT NULL, category TEXT NOT NULL, payment_method TEXT NOT NULL, date TEXT NOT NULL, source TEXT NOT NULL)")
        existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(transactions)")}
        for column, definition in (("reference_number", "TEXT NOT NULL DEFAULT ''"), ("withdrawal_amount", "REAL NOT NULL DEFAULT 0"), ("deposit_amount", "REAL NOT NULL DEFAULT 0"), ("closing_balance", "REAL NOT NULL DEFAULT 0")):
            if column not in existing_columns:
                connection.execute(f"ALTER TABLE transactions ADD COLUMN {column} {definition}")
        if not database_exists:
            connection.executemany("INSERT INTO transactions (amount, description, category, payment_method, date, source) VALUES (?, ?, ?, ?, ?, ?)", [(20, "Auto", "Transport", "Cash", "2026-08-31", "manual"), (350, "Food Order", "Food & Dining", "UPI", "2026-08-30", "imported")])


def transaction_from_row(row) -> Transaction:
    return Transaction(id=row[0], amount=row[1], description=row[2], category=row[3], payment_method=row[4], date=date_type.fromisoformat(row[5]), source=row[6], reference_number=row[7], withdrawal_amount=row[8], deposit_amount=row[9], closing_balance=row[10])


def read_transactions() -> List[Transaction]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        rows = connection.execute("SELECT id, amount, description, category, payment_method, date, source, reference_number, withdrawal_amount, deposit_amount, closing_balance FROM transactions ORDER BY date DESC, id DESC").fetchall()
    return [transaction_from_row(row) for row in rows]


initialize_database()


# --------------------------------------------------
# ROOT
# --------------------------------------------------

@app.get("/")
def root():
    return {
        "message": "MoneyTrail API is running!"
    }


# --------------------------------------------------
# GET ALL TRANSACTIONS
# --------------------------------------------------

@app.get("/transactions", response_model=List[Transaction])
def get_transactions():
    return read_transactions()


@app.delete("/transactions/reset")
def reset_transactions():
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("DELETE FROM transactions")
        connection.execute("DELETE FROM sqlite_sequence WHERE name = 'transactions'")
    return {"message": "All transaction data has been deleted.", "deleted": True}


# --------------------------------------------------
# ADD TRANSACTION
# --------------------------------------------------

@app.post("/transactions", response_model=Transaction)
def add_transaction(transaction_data: TransactionCreate):

    with sqlite3.connect(DATABASE_PATH) as connection:
        cursor = connection.execute(
            "INSERT INTO transactions (amount, description, category, payment_method, date, source, withdrawal_amount, deposit_amount) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                transaction_data.amount,
                transaction_data.description,
                transaction_data.category,
                transaction_data.payment_method,
                transaction_data.date.isoformat(),
                transaction_data.source,
                transaction_data.amount if transaction_data.transaction_type == "Withdrawn" else 0,
                transaction_data.amount if transaction_data.transaction_type == "Deposit" else 0
            )
        )
        new_id = cursor.lastrowid

    return Transaction(id=new_id, **transaction_data.model_dump())

@app.post("/upload")
async def upload_statement(
    file: UploadFile = File(...),
    statement_password: str = Form(default=""),
    start_date: str = Form(default=""),
    end_date: str = Form(default=""),
):

    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="No file was provided."
        )

    file_content = await file.read()
    selected_start = parse_optional_date(start_date)
    selected_end = parse_optional_date(end_date)
    if selected_start and selected_end and selected_start > selected_end:
        raise HTTPException(status_code=400, detail="The statement start date must be before its end date.")
    if len(file_content) > MAX_STATEMENT_BYTES:
        raise HTTPException(status_code=413, detail="Statement is too large. Upload a PDF smaller than 20 MB.")
    logger.info("Upload received: %s (%s bytes)", file.filename, len(file_content))

    try:
        if file.filename.lower().endswith(".pdf"):
            logger.info("Extracting PDF: %s", file.filename)
            if not can_open_pdf(file_content, statement_password):
                raise HTTPException(
                    status_code=422,
                    detail="This bank-statement PDF is password-protected. Enter its PDF password and upload it again.",
                )
            # PDF parsing and OCR are CPU-bound. Keep them off the async
            # request loop so other API requests stay responsive.
            rows = await run_in_threadpool(
                extract_pdf_rows_with_timeout,
                file_content,
                statement_password,
            )
        elif file.filename.lower().endswith(".csv"):
            rows = csv.DictReader(io.StringIO(file_content.decode("utf-8-sig")))
        else:
            raise HTTPException(status_code=400, detail="Please upload a CSV or PDF statement.")
        imported = []
        for row in rows:
            description = (row.get("description") or row.get("Description") or row.get("merchant") or row.get("Merchant") or row.get("details") or "Imported transaction").strip()
            withdrawal = parse_amount(row.get("withdrawal_amount") or row.get("Withdrawal Amount") or row.get("debit") or row.get("Debit") or "") or 0
            deposit = parse_amount(row.get("deposit_amount") or row.get("Deposit Amount") or row.get("credit") or row.get("Credit") or "") or 0
            amount = withdrawal or (parse_amount(row.get("amount") or row.get("Amount") or "") or 0)
            if amount == 0 and deposit == 0:
                continue
            raw_date = row.get("date") or row.get("Date") or str(date_type.today())
            transaction_date = parse_statement_date(raw_date)
            if selected_start and transaction_date < selected_start or selected_end and transaction_date > selected_end:
                continue
            category = categorize(description)
            imported.append(Transaction(
                id=0,
                amount=amount,
                description=description,
                category=category,
                payment_method=row.get("payment_method") or row.get("Payment Method") or "Bank",
                date=transaction_date,
                source="imported",
                reference_number=(row.get("reference_number") or row.get("Chq. / Ref No.") or row.get("ref_no") or "").strip(),
                withdrawal_amount=withdrawal or amount,
                deposit_amount=deposit,
                closing_balance=parse_amount(row.get("closing_balance") or row.get("Closing Balance") or "") or 0,
            ))
        with sqlite3.connect(DATABASE_PATH) as connection:
            for transaction in imported:
                connection.execute("INSERT INTO transactions (amount, description, category, payment_method, date, source, reference_number, withdrawal_amount, deposit_amount, closing_balance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (transaction.amount, transaction.description, transaction.category, transaction.payment_method, transaction.date.isoformat(), transaction.source, transaction.reference_number, transaction.withdrawal_amount, transaction.deposit_amount, transaction.closing_balance))
        logger.info("Import complete: %s rows from %s", len(imported), file.filename)
    except HTTPException:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        file_type = "PDF" if file.filename.lower().endswith(".pdf") else "CSV"
        raise HTTPException(status_code=400, detail=f"Could not read {file_type}: {exc}") from exc
    except Exception as exc:
        logger.exception("Statement import failed")
        raise HTTPException(status_code=400, detail=f"Could not process statement: {exc}") from exc

    return {
        "message": f"Imported {len(imported)} transactions.",
        "filename": file.filename,
        "imported": len(imported),
        "date_range": {"start": start_date or None, "end": end_date or None},
        "status": "complete",
    }


def can_open_pdf(file_content: bytes, statement_password: str) -> bool:
    """Fast encryption check before starting OCR or a worker process."""
    try:
        PDFDocument(PDFParser(io.BytesIO(file_content)), password=statement_password)
    except PDFPasswordIncorrect:
        return False
    return True


def parse_amount(raw_amount) -> float | None:
    """Return a numeric amount while ignoring blank and statement header cells."""
    if raw_amount is None:
        return None
    amount_match = re.search(r"-?[\d][\d,.]*", str(raw_amount).replace("₹", "").replace("Rs.", ""))
    if not amount_match:
        return None
    numeric_value = amount_match.group().replace(",", "")
    try:
        return abs(float(numeric_value))
    except ValueError:
        return None


def parse_statement_date(raw_date) -> date_type:
    value = str(raw_date or "").strip()
    for date_format in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    return date_type.today()


def parse_optional_date(raw_date: str) -> date_type | None:
    value = (raw_date or "").strip()
    if not value:
        return None
    try:
        return date_type.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Statement dates must use YYYY-MM-DD format.") from exc


def extract_pdf_rows_with_timeout(file_content: bytes, statement_password: str = "") -> list[dict]:
    """Run untrusted PDF work in a separate process that can be terminated."""
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_extract_pdf_rows_worker,
        args=(file_content, statement_password, result_queue),
    )
    process.start()
    process.join(PDF_PROCESS_TIMEOUT_SECONDS)

    if process.is_alive():
        process.terminate()
        process.join()
        raise HTTPException(
            status_code=422,
            detail="PDF processing timed out. Upload a smaller page range or download the statement as CSV from your bank.",
        )

    try:
        status, payload = result_queue.get(timeout=2)
    except Empty as exc:
        raise HTTPException(status_code=422, detail="Could not read this PDF statement. Try a CSV export from your bank.") from exc
    finally:
        result_queue.close()

    if status == "password_required":
        raise HTTPException(
            status_code=422,
            detail="This bank-statement PDF is password-protected. Enter its PDF password and upload it again.",
        )
    if status == "error":
        raise HTTPException(status_code=422, detail=f"Could not read this PDF statement: {payload}")
    return payload


def _extract_pdf_rows_worker(file_content: bytes, statement_password: str, result_queue) -> None:
    try:
        result_queue.put(("ok", extract_pdf_rows(file_content, statement_password)))
    except PDFPasswordIncorrect:
        result_queue.put(("password_required", None))
    except PdfminerException as exc:
        if isinstance(exc.__context__, PDFPasswordIncorrect):
            result_queue.put(("password_required", None))
        else:
            result_queue.put(("error", str(exc)))
    except Exception as exc:
        result_queue.put(("error", str(exc)))


def extract_pdf_rows(file_content: bytes, statement_password: str = "") -> list[dict]:
    rows = []
    with pdfplumber.open(io.BytesIO(file_content), password=statement_password or None) as pdf:
        if len(pdf.pages) > MAX_PDF_PAGES:
            raise HTTPException(
                status_code=422,
                detail=f"This statement has {len(pdf.pages)} pages. Please upload at most {MAX_PDF_PAGES} pages at a time.",
            )
        statement_columns = None
        for page_number, page in enumerate(pdf.pages, start=1):
            logger.info("Reading PDF page %s/%s", page_number, len(pdf.pages))
            tables = page.extract_tables()
            page_rows = []
            for table in tables or []:
                detected_columns = get_statement_columns(table)
                if detected_columns:
                    statement_columns = detected_columns
                if statement_columns:
                    page_rows.extend(extract_statement_table(table, statement_columns))
            rows.extend(page_rows)
            if page_rows:
                continue
            text = page.extract_text() or ""
            if not text.strip():
                if not shutil.which("tesseract") and not os.getenv("TESSERACT_CMD"):
                    raise HTTPException(status_code=422, detail="This PDF is scanned. Install Tesseract OCR, then set TESSERACT_CMD in Backend/.env if it is not on PATH.")
                if os.getenv("TESSERACT_CMD"):
                    pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]
                image = page.to_image(resolution=180).original
                try:
                    text = pytesseract.image_to_string(image, timeout=20)
                except RuntimeError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail=f"OCR timed out on PDF page {page_number}. Upload a text-based statement or a smaller page range.",
                    ) from exc
            for line in group_statement_lines(text):
                structured_row = parse_statement_text_row(line)
                if structured_row:
                    rows.append(structured_row)
                    continue
    return rows


def group_statement_lines(text: str) -> list[str]:
    """Join wrapped continuation lines belonging to the same headerless-page row."""
    grouped = []
    current = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if looks_like_transaction_line(line):
            if current:
                grouped.append(" ".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        grouped.append(" ".join(current))
    return grouped


def parse_statement_text_row(line: str) -> dict | None:
    """Parse continuation-page rows when the PDF text layer loses table columns."""
    date_match = re.match(r"\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\s+", line)
    if not date_match:
        return None
    row_pattern = re.compile(
        r"(?P<description>.+?)\s+"
        r"(?P<reference>\d{8,16})\s+"
        r"(?P<value_date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s+"
        r"(?P<withdrawal>[\d,]+(?:\.\d{1,2})?)\s+"
        r"(?P<deposit>[\d,]+(?:\.\d{1,2})?)\s+"
        r"(?P<closing>[\d,]+(?:\.\d{1,2})?)\s*$"
    )
    matches = list(row_pattern.finditer(line[date_match.end():]))
    if not matches:
        return None
    match = matches[-1]
    return {
        "date": date_match.group(1),
        "description": match.group("description").strip(),
        "reference_number": match.group("reference"),
        "withdrawal_amount": match.group("withdrawal"),
        "deposit_amount": match.group("deposit"),
        "closing_balance": match.group("closing"),
    }


def get_statement_columns(table: list[list]) -> dict | None:
    """Find the statement schema on the first page where headers are present."""
    if not table:
        return None
    headers = [re.sub(r"\s+", " ", str(value or "").strip().lower()) for value in table[0]]
    def column(*names):
        return next((index for index, header in enumerate(headers) if any(name in header for name in names)), None)
    columns = {"date": column("date"), "description": column("narration", "description", "particular"), "reference": column("ref", "cheque", "chq"), "withdrawal": column("withdrawal", "debit"), "deposit": column("deposit", "credit"), "closing": column("closing balance")}
    if columns["date"] is None or (columns["withdrawal"] is None and columns["deposit"] is None):
        return None
    return columns


def extract_statement_table(table: list[list], columns: dict) -> list[dict]:
    """Map current-page rows using the first page's statement column schema."""
    if not table:
        return []
    date_index = columns["date"]
    description_index = columns["description"]
    reference_index = columns["reference"]
    withdrawal_index = columns["withdrawal"]
    deposit_index = columns["deposit"]
    closing_index = columns["closing"]
    if date_index is None or (withdrawal_index is None and deposit_index is None):
        return []
    extracted = []
    start_index = 1 if get_statement_columns(table) else 0
    for values in table[start_index:]:
        cells = [str(value or "").strip() for value in values]
        if date_index >= len(cells):
            continue
        withdrawal = parse_amount(cells[withdrawal_index]) if withdrawal_index is not None and withdrawal_index < len(cells) else 0
        deposit = parse_amount(cells[deposit_index]) if deposit_index is not None and deposit_index < len(cells) else 0
        if not withdrawal and not deposit:
            continue
        extracted.append({
            "date": cells[date_index],
            "description": cells[description_index] if description_index is not None and description_index < len(cells) else "Bank transaction",
            "reference_number": cells[reference_index] if reference_index is not None and reference_index < len(cells) else "",
            "withdrawal_amount": withdrawal or "",
            "deposit_amount": deposit or "",
            "closing_balance": parse_amount(cells[closing_index]) if closing_index is not None and closing_index < len(cells) else 0,
        })
    return extracted


def looks_like_transaction_line(line: str) -> bool:
    """Ignore PDF text lines that do not begin with a transaction date."""
    first_token = line.strip().split(maxsplit=1)[0] if line.strip() else ""
    return bool(re.fullmatch(r"(?:\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|\d{4}[/-]\d{1,2}[/-]\d{1,2})", first_token))


def find_text_amount(line: str) -> str | None:
    """Choose a short money-like value and reject long reference or account IDs."""
    candidates = re.findall(r"(?:₹|Rs\.?\s*)?[-]?\d[\d,]*(?:\.\d{1,2})?", line)
    candidates = [candidate for candidate in candidates if parse_amount(candidate) is not None]
    if not candidates:
        return None
    marked = [candidate for candidate in candidates if re.search(r"₹|Rs|\.", candidate)]
    if marked:
        return marked[-1]
    short_candidates = [candidate for candidate in candidates if len(re.sub(r"\D", "", candidate)) <= 6]
    return short_candidates[-1] if short_candidates else None


@app.get("/analysis")
def get_analysis():
    current_transactions = read_transactions()
    frame = pd.DataFrame([transaction.model_dump() for transaction in current_transactions])
    if frame.empty:
        return {"total": 0, "average": 0, "categories": [], "anomalies": []}
    frame["amount"] = pd.to_numeric(frame["amount"])
    category_frame = frame.groupby("category", as_index=False)["amount"].sum().sort_values("amount", ascending=False)
    anomalies = []
    if len(frame) >= 5:
        detector = IsolationForest(contamination="auto", random_state=42)
        frame["is_anomaly"] = detector.fit_predict(frame[["amount"]]) == -1
        anomalies = frame[frame["is_anomaly"]][["description", "amount"]].to_dict("records")
    return {"total": float(frame["amount"].sum()), "average": float(frame["amount"].mean()), "categories": category_frame.to_dict("records"), "anomalies": anomalies}


@app.post("/insights")
def generate_insight(request: InsightRequest):
    if not os.getenv("GROQ_API_KEY"):
        return {"configured": False, "answer": "Add your GROQ_API_KEY to Backend/.env to enable AI-powered insights. Local spending analysis is still available."}
    from groq import Groq
    analysis = get_analysis()
    groq_context = {
        "total_spent": round(analysis["total"], 2),
        "average_transaction": round(analysis["average"], 2),
        "category_totals": [
            {"category": item["category"], "amount": round(item["amount"], 2)}
            for item in analysis["categories"]
        ],
        "unusual_transaction_count": len(analysis["anomalies"]),
    }
    prompt = f"You are MoneyTrail, a concise personal finance assistant. Use only these aggregated facts: {groq_context}. No merchant names, descriptions, dates, reference numbers, account details, or raw transaction records are available. Answer the question in 2-4 sentences, never invent facts, and do not provide investment advice. Question: {request.question}"
    try:
        response = Groq(api_key=os.environ["GROQ_API_KEY"]).chat.completions.create(model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"), messages=[{"role": "user", "content": prompt}], temperature=0.2, max_tokens=180)
        return {"configured": True, "answer": response.choices[0].message.content}
    except Exception as exc:
        if "model_not_found" in str(exc) or "does not exist" in str(exc):
            raise HTTPException(status_code=502, detail="The configured Groq model is unavailable. Set GROQ_MODEL to a model enabled in your Groq console, then restart the backend.") from exc
        raise HTTPException(status_code=502, detail=f"Groq request failed: {exc}") from exc


def categorize(description: str) -> str:
    merchant = description.lower()
    rules = {
        "Food & Dining": ("swiggy", "zomato", "restaurant", "cafe", "food", "domino", "nestle"),
        "Shopping": ("amazon", "flipkart", "myntra", "retail", "mall", "store", "nykaa", "fashion"),
        "Transport": ("uber", "ola", "metro", "fuel", "petrol", "rapido"),
        "Bills & Utilities": ("electric", "recharge", "airtel", "jio", "bill", "water", "interest", "card"),
        "Entertainment": ("netflix", "spotify", "movie", "bookmyshow", "youtube"),
        "Health": ("pharmacy", "hospital", "apollo", "doctor"),
    }
    for category, keywords in rules.items():
        if any(keyword in merchant for keyword in keywords):
            return category
    return "Other"
