"""Signup, login, and token exchange bodies."""

from typing import Optional, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from models.enums import UserRole

# NIST SP 800-63B puts the floor at 8 characters and advises against
# composition rules, so length is the only requirement enforced here.
MIN_PASSWORD_LENGTH = 8


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)
    name: str = Field(min_length=1, max_length=160)
    role: UserRole = UserRole.BUYER

    company_name: Optional[str] = Field(default=None, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=32)
    city: Optional[str] = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _normalise_email(self) -> Self:
        # Stored lowercase so the unique index is effectively case-insensitive.
        self.email = self.email.strip().lower()
        return self


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(max_length=200)

    @model_validator(mode="after")
    def _normalise_email(self) -> Self:
        self.email = self.email.strip().lower()
        return self


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds.")
