import os
import tempfile
import logging
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse

from extractor import extract_with_pdfplumber
from validator import verify_balance
from firefly_client import push_to_firefly

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="PDF Parser Service", version="1.0.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/parse")
async def parse_statement(
    file: UploadFile = File(...),
    account_name: str = Form(...),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    # Write upload to a temp file; delete immediately after processing
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name
        content = await file.read()
        tmp.write(content)

    try:
        logger.info("Extracting transactions from %s", file.filename)
        result = extract_with_pdfplumber(tmp_path, account_name)
        verification = verify_balance(result["transactions"], result.get("metadata", {}))

        response = {
            "extraction_method": result["method"],
            "confidence": result["confidence"],
            "metadata": result.get("metadata", {}),
            "transactions": result["transactions"],
            "balance_check": verification,
        }

        if verification["passed"] and result["confidence"] >= 0.7:
            response["status"] = "verified"
        else:
            response["status"] = "needs_review"

        return JSONResponse(content=response)

    finally:
        # Always delete the PDF immediately
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@app.post("/import")
async def import_to_firefly(payload: dict):
    """Push a verified transactions payload directly to Firefly importer."""
    transactions = payload.get("transactions", [])
    if not transactions:
        raise HTTPException(status_code=400, detail="No transactions provided")

    result = push_to_firefly(transactions)
    return result
