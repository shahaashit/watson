from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db
from ..models import entry_dict
from ..schemas import AskIn
from ..services import asker, external_errors

router = APIRouter(prefix="/api", tags=["ask"])


@router.post("/ask")
def ask(payload: AskIn, conn=Depends(get_db)):
    question = payload.question.strip()
    try:
        result = asker.answer_question(conn, question)
    except Exception as exc:
        raise HTTPException(502, external_errors.details("anthropic", exc)) from None

    # hydrate the cited entries so the UI can render them as links
    cited = []
    for eid in result["entry_ids"]:
        row = conn.execute("SELECT * FROM entries WHERE id = ?", (eid,)).fetchone()
        if row:
            cited.append(entry_dict(row))
    return {"answer": result["answer"], "entries": cited}
