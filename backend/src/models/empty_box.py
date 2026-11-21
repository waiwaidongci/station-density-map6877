from sqlalchemy import Column,Integer,String,Text,JSON,ForeignKey,UniqueConstraint
from ..config.database import Base
from ..config.settings import CONFIG
class EmptyBoxSearch(Base):
    __tablename__=CONFIG['empty_box_table']
    __table_args__=(UniqueConstraint('run_id',name='uq_empty_box_run'),)
    id=Column(Integer,primary_key=True)
    run_id=Column(Integer,ForeignKey(CONFIG['run_table']+'.id'),nullable=False)
    state=Column(String(20),default='queued',nullable=False)
    revision=Column(Integer,default=1,nullable=False)
    ranges=Column(JSON,nullable=False)
    checkpoint=Column(JSON,nullable=True)
    result=Column(JSON,nullable=True)
    error=Column(Text,default='')
    requested_by=Column(String(120),default='')
    worker_token=Column(String(64),default='')
    created_at=Column(String(40),nullable=False)
    updated_at=Column(String(40),nullable=False)
    completed_at=Column(String(40),default='')
