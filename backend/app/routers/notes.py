"""Scratch-note routes. These only touch Watson's local SQLite notes table."""
from fastapi import APIRouter, Depends, HTTPException

from ..db import get_db
from ..schemas import NoteIn
from ..services import notes


router = APIRouter(prefix="/api/notes", tags=["notes"])


def _not_found(error: ValueError) -> None:
    raise HTTPException(404, str(error)) from error


@router.get("")
def list_notes(conn=Depends(get_db)):
    return {"notes": notes.list_notes(conn)}


@router.post("", status_code=201)
def create_note(payload: NoteIn, conn=Depends(get_db)):
    return {"note": notes.create_note(conn, payload.body)}


@router.patch("/{note_id}")
def update_note(note_id: int, payload: NoteIn, conn=Depends(get_db)):
    try:
        note = notes.update_note(conn, note_id, payload.body)
    except ValueError as error:
        _not_found(error)
    return {"note": note}


@router.delete("/{note_id}")
def delete_note(note_id: int, conn=Depends(get_db)):
    try:
        notes.delete_note(conn, note_id)
    except ValueError as error:
        _not_found(error)
    return {"deleted": True}
