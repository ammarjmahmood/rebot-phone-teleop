from typing import Literal
from pydantic import BaseModel, ConfigDict


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    role: Literal["viewer", "operator", "owner"]
    local: bool = False
