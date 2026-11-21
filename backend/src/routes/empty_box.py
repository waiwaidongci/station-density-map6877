from fastapi import APIRouter, Depends
from ..config.database import db_session
from ..schemas.empty_box import EmptyBoxInput
from ..services.auth import current_user, writer
from ..services import empty_boxes
router = APIRouter()


@router.get('/runs/{run_id}/empty-box')
def detail(run_id: int, db=Depends(db_session), u=Depends(current_user)):
    return {'search': empty_boxes.get_search(db, run_id)}


@router.post('/runs/{run_id}/empty-box')
def create(run_id: int, data: EmptyBoxInput, db=Depends(db_session), u=Depends(writer)):
    return {'search': empty_boxes.create_search(db, u, run_id, data)}


@router.post('/runs/{run_id}/empty-box/resume')
def resume(run_id: int, db=Depends(db_session), u=Depends(writer)):
    return {'search': empty_boxes.resume_search(db, u, run_id)}
