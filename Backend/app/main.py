from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel
from typing import List
from datetime import date
from pathlib import Path


app = FastAPI(
    title="MoneyTrail API",
    description="Backend API for the MoneyTrail expense tracking application.",
    version="1.0.0"
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
    date: date
    source: str


class TransactionCreate(BaseModel):
    amount: float
    description: str
    category: str
    payment_method: str
    date: date
    source: str = "manual"


# --------------------------------------------------
# TEMPORARY STORAGE
# --------------------------------------------------
# This is only for the basic version.
# Later we will replace this with PostgreSQL.

transactions: List[Transaction] = [
    Transaction(
        id=1,
        amount=20,
        description="Auto",
        category="Transport",
        payment_method="Cash",
        date=date(2026, 8, 31),
        source="manual"
    ),
    Transaction(
        id=2,
        amount=350,
        description="Food Order",
        category="Food",
        payment_method="UPI",
        date=date(2026, 8, 30),
        source="imported"
    )
]


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
    return transactions


# --------------------------------------------------
# ADD TRANSACTION
# --------------------------------------------------

@app.post("/transactions", response_model=Transaction)
def add_transaction(transaction_data: TransactionCreate):

    new_id = len(transactions) + 1

    new_transaction = Transaction(
        id=new_id,
        amount=transaction_data.amount,
        description=transaction_data.description,
        category=transaction_data.category,
        payment_method=transaction_data.payment_method,
        date=transaction_data.date,
        source=transaction_data.source
    )

    transactions.append(new_transaction)

    return new_transaction


# --------------------------------------------------
# UPLOAD TRANSACTION STATEMENT
# --------------------------------------------------

UPLOAD_FOLDER = Path("uploads")
UPLOAD_FOLDER.mkdir(exist_ok=True)


@app.post("/upload")
async def upload_statement(file: UploadFile = File(...)):

    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="No file was provided."
        )

    file_path = UPLOAD_FOLDER / file.filename

    file_content = await file.read()

    with open(file_path, "wb") as f:
        f.write(file_content)

    return {
        "message": "Statement uploaded successfully.",
        "filename": file.filename,
        "status": "processing_pending"
    }