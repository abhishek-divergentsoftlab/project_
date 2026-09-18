from pydantic import BaseModel, EmailStr
from typing import Optional, List, Any

class Token(BaseModel):
    access_token: str
    token_type: str
    refresh_token: str

class TokenData(BaseModel):
    email: Optional[str] = None

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    name: str
    region: str
    phone: str
    role: str = "member"

class UserInDB(UserCreate):
    hashed_password: str

class UserResponse(BaseModel):
    id: str
    email: EmailStr
    name: str
    region: str
    phone: str
    role: str

class RFQBase(BaseModel):
    title: str
    description: str
    category: str
    price_target: Optional[float] = None
    deadline: Optional[str] = None
    quantity: Optional[int] = None

class RFQCreate(RFQBase):
    pass

class RFQResponse(RFQBase):
    id: str
    user_id: str

class ChatMessage(BaseModel):
    message: str

class ChatStreamChunk(BaseModel):
    id: int
    content: str
    type: str # 'text', 'steps', 'response', 'thinking'
