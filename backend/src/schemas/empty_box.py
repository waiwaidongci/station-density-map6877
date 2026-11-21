from pydantic import BaseModel,Field
class EmptyBoxInput(BaseModel):
    ranges:dict
    name:str=Field(default='补测空区',max_length=160)
