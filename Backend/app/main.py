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
from datetime import date as date_type
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


class TransactionCreate(BaseModel):
    amount: float
    description: str
    category: str
    payment_method: str
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
        if not database_exists:
            connection.executemany("INSERT INTO transactions (amount, description, category, payment_method, date, source) VALUES (?, ?, ?, ?, ?, ?)", [(20, "Auto", "Transport", "Cash", "2026-08-31", "manual"), (350, "Food Order", "Food & Dining", "UPI", "2026-08-30", "imported")])


def transaction_from_row(row) -> Transaction:
    return Transaction(id=row[0], amount=row[1], description=row[2], category=row[3], payment_method=row[4], date=date_type.fromisoformat(row[5]), source=row[6])


def read_transactions() -> List[Transaction]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        rows = connection.execute("SELECT id, amount, description, category, payment_method, date, source FROM transactions ORDER BY date DESC, id DESC").fetchall()
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
        cursor = connection.execute("INSERT INTO transactions (amount, description, category, payment_method, date, source) VALUES (?, ?, ?, ?, ?, ?)", (transaction_data.amount, transaction_data.description, transaction_data.category, transaction_data.payment_method, transaction_data.date.isoformat(), transaction_data.source))
        new_id = cursor.lastrowid
    return Transaction(id=new_id, **transaction_data.model_dump())


@app.post("/upload")
async def upload_statement(
    file: UploadFile = File(...),
    statement_password: str = Form(default=""),
):

    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="No file was provided."
        )

    file_content = await file.read()
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
            raw_amount = row.get("amount") or row.get("Amount") or row.get("debit") or row.get("Debit") or row.get("withdrawal") or ""
            amount = parse_amount(raw_amount)
            if amount is None:
                continue
            raw_date = row.get("date") or row.get("Date") or str(date_type.today())
            try:
                transaction_date = date_type.fromisoformat(raw_date.strip())
            except ValueError:
                transaction_date = date_type.today()
            category = categorize(description)
            imported.append(Transaction(
                id=0,
                amount=amount,
                description=description,
                category=category,
                payment_method=row.get("payment_method") or row.get("Payment Method") or "Bank",
                date=transaction_date,
                source="imported",
            ))
        with sqlite3.connect(DATABASE_PATH) as connection:
            for transaction in imported:
                connection.execute("INSERT INTO transactions (amount, description, category, payment_method, date, source) VALUES (?, ?, ?, ?, ?, ?)", (transaction.amount, transaction.description, transaction.category, transaction.payment_method, transaction.date.isoformat(), transaction.source))
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
        for page_number, page in enumerate(pdf.pages, start=1):
            logger.info("Reading PDF page %s/%s", page_number, len(pdf.pages))
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
            for line in text.splitlines():
                amount_match = re.search(r"(?:₹|Rs\.?\s*)?([\d,]+(?:\.\d{1,2})?)\s*$", line)
                if amount_match:
                    parts = line[:amount_match.start()].split(None, 1)
                    rows.append({"date": parts[0] if parts else str(date_type.today()), "description": parts[1] if len(parts) > 1 else "PDF transaction", "amount": amount_match.group(1)})
    return rows


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
    prompt = f"You are MoneyTrail, a concise personal finance assistant. Use only these facts: {analysis}. Answer the question in 2-4 sentences, never invent facts, and do not provide investment advice. Question: {request.question}"
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
        "Food & Dining": ("swiggy", "zomato", "restaurant", "cafe", "food", "domino"),
        "Shopping": ("amazon", "flipkart", "myntra", "retail", "mall"),
        "Transport": ("uber", "ola", "metro", "fuel", "petrol", "rapido"),
        "Bills & Utilities": ("electric", "recharge", "airtel", "jio", "bill", "water"),
        "Entertainment": ("netflix", "spotify", "movie", "bookmyshow"),
        "Health": ("pharmacy", "hospital", "apollo", "doctor"),
    }
    for category, keywords in rules.items():
        if any(keyword in merchant for keyword in keywords):
            return category
    return "Other"
