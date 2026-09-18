from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from typing import Any
from models.schemas import UserCreate, UserResponse, Token, UserInDB
from core.security import get_password_hash, verify_password, create_access_token, create_refresh_token
from core.config import settings
import jwt
import uuid

router = APIRouter()

# Mock DB for users
fake_users_db = {}
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/auth/login")

def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception
        
    user = fake_users_db.get(email)
    if user is None:
        raise credentials_exception
    return user

@router.post("/signup", response_model=UserResponse)
def signup(user: UserCreate) -> Any:
    if user.email in fake_users_db:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    user_id = str(uuid.uuid4())
    hashed_password = get_password_hash(user.password)
    db_user = UserInDB(**user.model_dump(), hashed_password=hashed_password)
    fake_users_db[user.email] = {"id": user_id, **db_user.model_dump()}
    
    return UserResponse(id=user_id, email=user.email, name=user.name, region=user.region, phone=user.phone, role=user.role)

@router.post("/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends()) -> Any:
    user = fake_users_db.get(form_data.username)
    if not user or not verify_password(form_data.password, user["hashed_password"]):
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    
    access_token = create_access_token(data={"sub": user["email"]})
    refresh_token = create_refresh_token(data={"sub": user["email"]})
    
    return {"access_token": access_token, "token_type": "bearer", "refresh_token": refresh_token}

@router.post("/refresh", response_model=Token)
def refresh(refresh_token: str) -> Any:
    # Basic mock: normally verify token first
    access_token = create_access_token(data={"sub": "refresh"})
    return {"access_token": access_token, "token_type": "bearer", "refresh_token": refresh_token}

@router.post("/logout")
def logout() -> Any:
    return {"msg": "Successfully logged out"}

@router.get("/me", response_model=UserResponse)
def read_users_me(current_user: dict = Depends(get_current_user)) -> Any:
    return UserResponse(**current_user)
